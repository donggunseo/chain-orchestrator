"""Versioned Entity Workflow. Only this inbox mutates the pure Episode engine.

Configuration/plugin I/O belongs to the Worker. Activity completions, Signals
and native Timers are recorded by Temporal and enter one serialized reducer.
This type is intentionally separate from ChainOrchestratorV03 and its queue.
"""
import asyncio
import copy
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from .compiler import canonical_hash
    from .episode import EpisodeEngine
    from .errors import ConfigError
    from .execution import WORKFLOW_TYPE, validate_start
    from .execution_failures import query_failure, work_failure
    from .fact_ledger import FactLedger
    from .reconciliation import ReconciliationOwner
    from .runtime_contracts import timestamp
    from .shape import check_json
    from .views import hitl_view as exact_hitl_view, progress_view


ACTIVITIES = {
    "resolve": "chain.fact.resolve_event_v1", "preflight": "chain.fact.preflight_v1",
    "reconcile": "chain.fact.reconcile_v1", "agent": "chain.fact.run_agent_v1",
    "notify": "chain.fact.run_work_v1", "effect": "chain.fact.run_work_v1",
}


def activity_error(error):
    """Return only stable diagnostics, never provider exception text/data."""
    cause = error
    while getattr(cause, "cause", None) is not None:
        cause = cause.cause
    if isinstance(cause, ApplicationError):
        return cause.type or "ACTIVITY_FAILED", not cause.non_retryable
    return "ACTIVITY_TIMEOUT" if "Timeout" in type(cause).__name__ else "ACTIVITY_FAILED", True


@workflow.defn(name=WORKFLOW_TYPE)
class FactEpisodeWorkflow:
    def __init__(self):
        self.engine = None
        self.inbox = []
        self._pending = {}
        self._timers = {}
        self._wake = None
        self._wake_at = None
        self._manifest = None
        self._reconciliation = None
        self._activity_progress = {}
        self._receipts = []

    @workflow.signal
    def submit_event(self, value):
        self.inbox.append(("event", value))

    @workflow.signal
    def submit_decision(self, value):
        self.inbox.append(("decision", value))

    @workflow.signal
    def request_hitl_resume(self, value):
        self.inbox.append(("resume", value))

    @workflow.signal
    def source_reconnected(self, source_id):
        self.inbox.append(("reconnect", source_id))

    @workflow.query
    def snapshot(self) -> dict:
        if self.engine is None:
            return {"state": None, "ready": False}
        result = self.engine.query()
        result.update(ready=True, run_manifest=copy.deepcopy(self._manifest),
            ingress_receipts=copy.deepcopy(self._receipts),
            event_receipts={item["event_id"]: self.engine.ledger.event_receipt(item["event_id"])
                            for item in self._receipts if item.get("kind") == "event" and item.get("event_id")},
            activity_progress=copy.deepcopy(self._activity_progress),
            verifications=self.engine.tasks.verifications,
            source_reconciliation={source: self._reconciliation.status(source)
                                   for source in sorted(self._reconciliation.sources)},
            facts=self.engine.ledger.snapshot())
        return result

    @workflow.query
    def changes_since(self, view_seq: int = 0) -> list:
        return self.engine.changes_since(view_seq) if self.engine else []

    @workflow.query
    def progress(self) -> dict:
        return progress_view(self.engine, run_manifest=self._manifest, receipts=self._receipts)

    @workflow.query
    def hitl_view(self, request_id: str, revision: int | None = None) -> dict:
        return exact_hitl_view(self.engine, request_id, revision)

    def _now(self):
        return workflow.now().isoformat()

    def _record(self, kind, value, receipt):
        key = "event_id" if kind == "event" else "decision_id" if kind == "decision" else "command_id"
        body = value.get("event", {}) if kind == "event" and isinstance(value, dict) else value
        identifier = body.get(key) if isinstance(body, dict) else None
        self._receipts.append({"seq": len(self._receipts) + 1, "kind": kind,
            key: identifier if isinstance(identifier, str) else None,
            "receipt": copy.deepcopy(receipt), "at": self._now()})

    def _event(self, value):
        check_json(value)
        if not isinstance(value, dict) or set(value) != {"source_id", "event"}:
            raise ConfigError("INGRESS_SHAPE_INVALID", "submit_event", "Expected logical source_id and unchanged wire Event")
        return self.engine.receive(value["event"], source_id=value["source_id"], now=self._now())

    def _input(self, kind, value):
        try:
            if kind == "event":
                receipt = self._event(value)
            elif kind == "decision":
                receipt = self.engine.submit_hitl(value, now=self._now())
            elif kind == "resume":
                receipt = self.engine.resume_hitl(value, now=self._now())
            else:
                if not isinstance(value, str) or value not in self._reconciliation.sources:
                    raise ConfigError("SOURCE_NOT_DECLARED", "source_reconnected", "Expected a configured logical source")
                # The owner restores each source's own checkpoint/budgets. It
                # does not discard other in-flight scans on a reconnect.
                self._reconcile(self._reconciliation.on_reconnect(self._now()))
                receipt = {"status": "ACCEPTED", "source_id": value}
        except ConfigError as error:
            receipt = {"status": "REJECTED", "code": error.code, "path": error.path}
        self._record(kind, value, receipt)

    async def _activity(self, key, generation, kind, payload, timeout, attempts):
        try:
            result = await workflow.execute_activity(ACTIVITIES[kind], payload,
                activity_id=key + ":" + str(generation),
                start_to_close_timeout=timedelta(seconds=timeout),
                schedule_to_close_timeout=timedelta(seconds=timeout),
                retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=50),
                    backoff_coefficient=2, maximum_interval=timedelta(seconds=1), maximum_attempts=attempts))
            self.inbox.append(("completed", {"key": key, "generation": generation, "result": result}))
        except ActivityError as error:
            code, retryable = activity_error(error)
            self.inbox.append(("completed", {"key": key, "generation": generation,
                "error": code, "retryable": retryable}))
        except asyncio.CancelledError:
            pass

    def _launch(self, key, generation, kind, payload, *, timeout, attempts=1):
        existing = self._pending.get(key)
        if existing and existing["generation"] == generation:
            return
        if existing:
            existing["task"].cancel()
        payload = {"run_manifest": self._manifest, **copy.deepcopy(payload)}
        task = asyncio.create_task(self._activity(key, generation, kind, payload, max(.001, timeout), attempts))
        self._pending[key] = {"generation": generation, "kind": kind, "payload": payload, "task": task}
        request = payload.get("request", {})
        self._activity_progress[key] = {"kind": kind, "attempt_no": generation, "status": "SCHEDULED",
            "request_id": request.get("request_id"), "source": request.get("source", payload.get("source_id")),
            "scope": request.get("scope"), "issued_at": self._now()}

    def _reconcile(self, requests):
        for request in requests:
            state = self._reconciliation.status(request["source"])
            cycle = state["cycle"]
            remaining = (timestamp(cycle["deadline"]) - workflow.now()).total_seconds()
            self._launch("reconcile:" + request["request_id"], state["attempts"], "reconcile",
                {"request": request}, timeout=remaining)

    async def _timer(self, payload):
        try:
            await workflow.sleep(max(0, (timestamp(payload["due_at"]) - workflow.now()).total_seconds()))
            self.inbox.append(("timer", payload))
        except asyncio.CancelledError:
            pass

    def _commands(self, commands):
        for command in commands:
            kind, request = command["kind"], command["request"]
            if kind == "timer_start":
                key = request["timer_id"]
                if key not in self._timers:
                    self._timers[key] = asyncio.create_task(self._timer(request))
            elif kind == "timer_cancel":
                task = self._timers.pop(request["timer_id"], None)
                if task:
                    task.cancel()
            elif kind == "preflight":
                self._launch("preflight:" + request["request_id"], 1, kind,
                    {"request": request}, timeout=request["limits"]["timeout_ms"] / 1000,
                    attempts=request["limits"]["max_attempts"])
            elif kind in {"agent", "notify", "effect"}:
                row = self.engine.tasks.executions[request["execution_id"]]
                payload = {"request": request}
                if kind == "agent":
                    payload.update({k: copy.deepcopy(command[k]) for k in ("prerequisite_results", "verifications")})
                self._launch("work:" + request["execution_id"], row["attempt_no"], kind,
                    payload,
                    timeout=(timestamp(row["deadline"]) - workflow.now()).total_seconds())
            elif kind not in {"hitl_opened", "hitl_updated", "hitl_status"}:
                raise ConfigError("COMMAND_UNSUPPORTED", kind, "No Temporal handler for this primitive")

    def _completed(self, message):
        key, generation = message["key"], message["generation"]
        pending = self._pending.get(key)
        if pending is None or pending["generation"] != generation:
            return  # A recorded completion from a cancelled older attempt.
        self._pending.pop(key)
        kind, payload = pending["kind"], pending["payload"]
        request, now = payload.get("request"), self._now()
        code = message.get("error")
        self._activity_progress[key].update(status="FAILED" if code else "COMPLETED", completed_at=now, error_code=code)
        if kind in {"agent", "notify", "effect"}:
            execution = self.engine.tasks.executions[request["execution_id"]]
            if execution["attempt_no"] != generation or execution["result"] is not None:
                return
            if code and message["retryable"] and generation < self._definition["execution"]["activity"]["maximum_attempts"]:
                self._commands([self.engine.tasks.retry(request["execution_id"], now=now)])
                return
            result = work_failure(request, code=code, now=now) if code else message["result"]
            self._activity_progress[key]["result_status"] = result["status"]
            accept = self.engine.accept_agent_result if kind == "agent" else self.engine.accept_work_result
            accept(result, now=now)
        elif kind == "resolve":
            eid = payload["event"]["event_id"]
            current = self.engine.ledger.event_receipt(eid)
            if current["status"] != "PENDING":
                return
            if code:
                self.engine.ledger.fail_resolution(eid, code=code, at=now)
                if message["retryable"]:
                    self.engine.ledger.retry_resolution(eid, at=now)
            else:
                self.engine.resolve(message["result"], now=now)
        else:
            reply = message.get("result")
            result = query_failure(request, code=code, now=now, retryable=message.get("retryable", False)) if code else reply["result"]
            self._activity_progress[key].update(result_status=result["status"],
                checked_at=result.get("checked_at"), source_basis=result.get("source_basis"),
                coverage=result.get("coverage"), errors=copy.deepcopy(result["errors"]))
            # Transport receipt is not application. Discovered documents go
            # through the same validation/resolve path as a manual Signal.
            for event in reply.get("events", []) if reply else []:
                self._input("event", event)
            if kind == "preflight":
                self.engine.accept_preflight(result, now=now)
            else:
                self._reconciliation.accept_result(result, now=now, attempt_no=generation)

    def _resolutions(self):
        for row in self.engine.ledger.resolution_requests():
            self._launch("resolve:" + row["event"]["event_id"], row["attempt_no"], "resolve",
                {"source_id": row["source_id"], "event": row["event"]},
                timeout=(timestamp(row["deadline"]) - workflow.now()).total_seconds())

    async def _sleep_until(self, at):
        try:
            await workflow.sleep(max(0, (timestamp(at) - workflow.now()).total_seconds()))
            self.inbox.append(("wake", at))
        except asyncio.CancelledError:
            pass

    def _deadline(self):
        values = [self.engine.next_deadline] if self.engine.next_deadline else []
        for source in sorted(self._reconciliation.sources):
            row = self._reconciliation.status(source)
            at = row["cycle"]["deadline"] if row["cycle"] else row["next_poll_at"]
            if at:
                values.append(at)
        at = min(values, key=timestamp) if values else None
        if at == self._wake_at:
            return
        if self._wake:
            self._wake.cancel()
        self._wake_at = at
        self._wake = asyncio.create_task(self._sleep_until(at)) if at else None

    def _cancel_all(self):
        for item in self._pending.values():
            item["task"].cancel()
        for task in self._timers.values():
            task.cancel()
        if self._wake:
            self._wake.cancel()

    @workflow.run
    async def run(self, start) -> dict:
        try:
            value = validate_start(start)
            # Fix the version at startup, including while replaying histories
            # which predate this marker. Never reinterpret their command order.
            bounded_turns = workflow.patched("chain-fact-inbox-turn-v1")
            activity_turns = workflow.patched("chain-fact-inbox-activity-turn-v1")
            turn_inputs = 0
            turn_sequence = 0
            self._manifest = value["run_manifest"]
            # Task Queue is enforced by build_start/create_worker and the
            # pinned Activity envelope. SDK Replayer uses a synthetic queue;
            # that runtime-local routing value cannot be a reducer input.
            if workflow.info().workflow_id != self._manifest["engine_run_id"]:
                raise ConfigError("WORKFLOW_BINDING_MISMATCH", "run_manifest", "Workflow ID differs from the pinned start")
            self._definition = value["public_definition"]
            ledger = FactLedger(value["initial"], definition=self._definition,
                source_systems=self._manifest["source_systems"], engine_run_id=self._manifest["engine_run_id"],
                run_pin=self._manifest["run_pin"], known_at=self._now(),
                resolution_limits=self._definition["execution"]["activity"])
            self.engine = EpisodeEngine(self._definition, ledger)
            preparation = self._definition["execution"]["preparation"]
            sources = {name: {"scope": fields, "authorization_ref": canonical_hash({"source": name,
                       "approval": self._definition["policy"]["query_approvals"][name]})}
                       for name, fields in self._definition["source_coverage"].items()}
            self._reconciliation = ReconciliationOwner(ledger.identity, ledger.engine_run_id, ledger.run_pin,
                sources=sources, limits={"timeout_ms": max(1, int(preparation["timeout_s"] * 1000)),
                    "max_attempts": preparation["maximum_attempts"], "max_pages": preparation["maximum_pages"],
                    "max_preparation_rounds": preparation["maximum_rounds"]},
                reconciliation=self._definition["execution"]["reconciliation"], event_receipt=ledger.event_receipt)
            self._reconcile(self._reconciliation.on_startup(self._now()))
            while True:
                had_input = bool(self.inbox)
                if self.inbox:
                    kind, body = self.inbox.pop(0)
                    if kind == "completed":
                        self._completed(body)
                    elif kind == "timer":
                        self._timers.pop(body["timer_id"], None)
                        self.engine.fire_timer(body, now=self._now())
                    elif kind == "wake":
                        if body == self._wake_at:
                            self._wake_at = None
                    else:
                        self._input(kind, body)
                self._commands(self.engine.advance(now=self._now()))
                if self.engine.termination_phase == "FINALIZED":
                    return self.snapshot()
                self._resolutions()
                self._reconcile(self._reconciliation.poll(self._now()))
                self._deadline()
                if bounded_turns and had_input:
                    turn_inputs += 1
                    if turn_inputs == 2:
                        turn_inputs = 0
                        # A ready wait_condition alone can keep draining an
                        # activation. A recorded handoff returns control
                        # after two inputs, even when inbox is currently empty:
                        # ready callbacks may still append in this activation.
                        # This is transport scheduling, not a clinical Timer or
                        # a clinical time-based gate.
                        if activity_turns:
                            turn_sequence += 1
                            request = {key: self._manifest[key] for key in
                                ("engine_run_id", "configuration_hash", "run_pin")}
                            request["turn"] = turn_sequence
                            result = await workflow.execute_activity("chain.fact.yield_turn_v1", request,
                                activity_id=f"turn:{turn_sequence}",
                                start_to_close_timeout=timedelta(seconds=10),
                                schedule_to_close_timeout=timedelta(seconds=30),
                                retry_policy=RetryPolicy(maximum_attempts=2,
                                    initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=1)),
                                summary="CHAIN inbox turn")
                            if (not isinstance(result, dict) or type(result.get("turn")) is not int
                                    or result != {"contract_schema": "chain-fact-turn-result/v1", **request}):
                                raise ConfigError("TURN_RESULT_INVALID", "turn", "Execution handoff acknowledgment differs")
                        else:
                            # Compatibility for the retained Timer prototype's
                            # History. New executions use the immediate ack.
                            await workflow.sleep(.001, summary="CHAIN inbox turn")
                await workflow.wait_condition(lambda: bool(self.inbox))
        except ConfigError as error:
            raise ApplicationError(error.message, type=error.code, non_retryable=True) from error
        finally:
            self._cancel_all()
