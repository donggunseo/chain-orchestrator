"""Demo transports for the same pinned Episode and Activity contracts.

The local transport reuses the native inbox/receipt logic and supplies an
ordinary asyncio clock. The Temporal transport registers the unchanged native
Workflow. Neither transport reads a scenario, fixture answer or expected path.
"""
import asyncio
import copy
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from threading import RLock
from time import monotonic
from uuid import uuid4

from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Replayer, Worker

from chain.activities import FactActivities
from chain.compiler import canonical_hash
from chain.episode import EpisodeEngine
from chain.execution import build_start, queue_for_configuration, validate_start
from chain.fact_ledger import FactLedger
from chain.reconciliation import ReconciliationOwner
from chain.runtime_contracts import timestamp
from chain.temporal_workflow import FactEpisodeWorkflow
from chain.worker_runtime import FactWorkerRuntime
from chain.views import hitl_from_snapshot, progress_from_snapshot


class DemoQueryError(RuntimeError):
    """Transport diagnostics expose the stage/code, never patient payloads."""
    def __init__(self, stage, code):
        self.stage, self.code = stage, code
        super().__init__("Query " + stage + " [" + code + "]")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class DemoActivities(FactActivities):
    """Journal exact requests/results outside the Workflow and its reducer."""
    def __init__(self, runtime, path=None):
        super().__init__(runtime)
        self.path = Path(path) if path else None
        self.lock = RLock()

    def _append(self, value):
        if self.path:
            with self.lock, self.path.open("a", encoding="utf8") as stream:
                stream.write(json.dumps(value, ensure_ascii=False) + "\n")

    async def _record(self, kind, callback, envelope):
        metadata = None
        if activity.in_activity():
            info = activity.info()
            metadata = {"activity_id": info.activity_id, "attempt": info.attempt}
        self._append({"phase": "REQUEST", "kind": kind, "at": utc_now(),
            "envelope": envelope, "temporal": metadata})
        try:
            result = await callback(envelope)
        except ApplicationError as error:
            self._append({"phase": "ERROR", "kind": kind, "at": utc_now(),
                "request_id": envelope.get("request", {}).get("request_id"),
                "code": error.type, "non_retryable": error.non_retryable, "temporal": metadata})
            raise
        self._append({"phase": "RESULT", "kind": kind, "at": utc_now(), "result": result, "temporal": metadata})
        return result

    @activity.defn(name="chain.fact.resolve_event_v1")
    async def resolve_event(self, envelope):
        return await self._record("resolve", super().resolve_event, envelope)

    @activity.defn(name="chain.fact.preflight_v1")
    async def preflight(self, envelope):
        return await self._record("preflight", super().preflight, envelope)

    @activity.defn(name="chain.fact.reconcile_v1")
    async def reconcile(self, envelope):
        return await self._record("reconcile", super().reconcile, envelope)

    @activity.defn(name="chain.fact.run_agent_v1")
    async def run_agent(self, envelope):
        return await self._record("agent", super().run_agent, envelope)

    @activity.defn(name="chain.fact.run_work_v1")
    async def run_work(self, envelope):
        return await self._record("work", super().run_work, envelope)


class _LocalInbox(FactEpisodeWorkflow):
    """An ordinary local object, never registered as a Temporal Workflow.

Inherited pure input/completion routing keeps the native receipts and retry
owners. Only the scheduler, clock and Activity transport are replaced here.
"""
    def __init__(self, activities):
        super().__init__()
        self.activities = activities
        self._tick = None

    def _now(self):
        # One admitted message is one reducer timestamp, matching Temporal's
        # recorded activation clock even when source discovery has many Events.
        return self._tick or utc_now()

    async def _activity(self, key, generation, kind, payload, timeout, attempts):
        names = {"resolve": "resolve_event", "preflight": "preflight", "reconcile": "reconcile",
                 "agent": "run_agent", "notify": "run_work", "effect": "run_work"}
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            for attempt in range(attempts):
                try:
                    async with asyncio.timeout(max(.001, deadline - asyncio.get_running_loop().time())):
                        result = await getattr(self.activities, names[kind])(payload)
                    self.inbox.append(("completed", {"key": key, "generation": generation, "result": result}))
                    return
                except (ApplicationError, TimeoutError) as error:
                    retryable = not isinstance(error, ApplicationError) or not error.non_retryable
                    code = error.type if isinstance(error, ApplicationError) else "ACTIVITY_TIMEOUT"
                    if not retryable or attempt + 1 >= attempts or asyncio.get_running_loop().time() >= deadline:
                        self.inbox.append(("completed", {"key": key, "generation": generation,
                            "error": code, "retryable": retryable}))
                        return
                    await asyncio.sleep(min(.05, max(0, deadline - asyncio.get_running_loop().time())))
        except asyncio.CancelledError:
            pass

    def _reconcile(self, requests):
        for request in requests:
            state = self._reconciliation.status(request["source"])
            self._launch("reconcile:" + request["request_id"], state["attempts"], "reconcile",
                {"request": request}, timeout=(timestamp(state["cycle"]["deadline"]) - timestamp(self._now())).total_seconds())

    async def _timer(self, payload):
        try:
            await asyncio.sleep(max(0, (timestamp(payload["due_at"]) - timestamp(self._now())).total_seconds()))
            self.inbox.append(("timer", payload))
        except asyncio.CancelledError:
            pass

    async def _sleep_until(self, at):
        try:
            await asyncio.sleep(max(0, (timestamp(at) - timestamp(self._now())).total_seconds()))
            self.inbox.append(("wake", at))
        except asyncio.CancelledError:
            pass

    def _commands(self, commands):
        for command in commands:
            kind, request = command["kind"], command["request"]
            if kind == "timer_start":
                if request["timer_id"] not in self._timers:
                    self._timers[request["timer_id"]] = asyncio.create_task(self._timer(request))
            elif kind == "timer_cancel":
                task = self._timers.pop(request["timer_id"], None)
                if task:
                    task.cancel()
            elif kind == "preflight":
                self._launch("preflight:" + request["request_id"], 1, kind, {"request": request},
                    timeout=request["limits"]["timeout_ms"] / 1000, attempts=request["limits"]["max_attempts"])
            elif kind in {"agent", "notify", "effect"}:
                row = self.engine.tasks.executions[request["execution_id"]]
                payload = {"request": request}
                if kind == "agent":
                    payload.update({key: command[key] for key in ("prerequisite_results", "verifications")})
                self._launch("work:" + request["execution_id"], row["attempt_no"], kind, payload,
                    timeout=(timestamp(row["deadline"]) - timestamp(self._now())).total_seconds())
            elif kind not in {"hitl_opened", "hitl_updated", "hitl_status"}:
                raise ValueError("Unsupported command: " + kind)

    def _resolutions(self):
        for row in self.engine.ledger.resolution_requests():
            self._launch("resolve:" + row["event"]["event_id"], row["attempt_no"], "resolve",
                {"source_id": row["source_id"], "event": row["event"]},
                timeout=(timestamp(row["deadline"]) - timestamp(self._now())).total_seconds())

    async def run_local(self, value):
        value = validate_start(value)
        self._manifest, self._definition = value["run_manifest"], value["public_definition"]
        ledger = FactLedger(value["initial"], definition=self._definition, source_systems=self._manifest["source_systems"],
            engine_run_id=self._manifest["engine_run_id"], run_pin=self._manifest["run_pin"], known_at=self._now(),
            resolution_limits=self._definition["execution"]["activity"])
        self.engine = EpisodeEngine(self._definition, ledger)
        limits = self._definition["execution"]["preparation"]
        sources = {name: {"scope": fields, "authorization_ref": canonical_hash({"source": name,
            "approval": self._definition["policy"]["query_approvals"][name]})}
            for name, fields in self._definition["source_coverage"].items()}
        self._reconciliation = ReconciliationOwner(ledger.identity, ledger.engine_run_id, ledger.run_pin,
            sources=sources, limits={"timeout_ms": max(1, int(limits["timeout_s"] * 1000)),
                "max_attempts": limits["maximum_attempts"], "max_pages": limits["maximum_pages"],
                "max_preparation_rounds": limits["maximum_rounds"]},
            reconciliation=self._definition["execution"]["reconciliation"], event_receipt=ledger.event_receipt)
        self._reconcile(self._reconciliation.on_startup(self._now()))
        try:
            while True:
                self._tick = utc_now()
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
                self._tick = None
                while not self.inbox:
                    await asyncio.sleep(.01)
        finally:
            self._cancel_all()


class FactLocalRuntime:
    def __init__(self, configuration, initial, *, services, installation_root, output=None, timer_scale=1, **_):
        self.configuration, self.initial = configuration, initial
        self.output = Path(output) if output else None
        native = FactWorkerRuntime(configuration, installation_root, services=services)
        self.activities = DemoActivities(native, self.output / "commands.jsonl" if self.output else None)
        self.controller = _LocalInbox(self.activities)
        self.run_id = "chain-fact-demo-" + uuid4().hex
        self.start = build_start(configuration, initial, engine_run_id=self.run_id)
        self.result = None
        self.closed = False
        self.timer_scale = timer_scale

    now = staticmethod(utc_now)

    async def __aenter__(self):
        self.result = asyncio.create_task(self.controller.run_local(self.start))
        await asyncio.sleep(0)
        if self.result.done():
            await self.result
        return self

    async def snapshot(self):
        return self.controller.snapshot()

    async def progress(self):
        return self.controller.progress()

    async def hitl_view(self, request_id, revision=None):
        return self.controller.hitl_view(request_id, revision)

    async def submit_event(self, value):
        return self._submit("event", value)

    async def submit_decision(self, value):
        return self._submit("decision", value)

    async def request_hitl_resume(self, value):
        return self._submit("resume", value)

    async def source_reconnected(self, value):
        return self._submit("reconnect", value)

    def _submit(self, kind, value):
        if self.closed or not self.result or self.result.done():
            raise RuntimeError("Runtime is not accepting inputs")
        self.controller.inbox.append((kind, copy.deepcopy(value)))
        return {"status": "SUBMITTED_AWAITING_ENGINE"}

    async def timer_status(self, view=None):
        from .timers import timer_views
        rows = timer_views(await self.snapshot() if view is None else view, now=self.now())
        for row in rows:
            row["original_seconds"] = row["seconds"] / self.timer_scale
        return rows

    async def close(self):
        self.closed = True
        if self.result:
            if not self.result.done():
                self.result.cancel()
            await asyncio.gather(self.result, return_exceptions=True)

    async def __aexit__(self, *_):
        await self.close()


class FactTemporalRuntime:
    def __init__(self, configuration, initial, *, services, installation_root, address=None, start_local=False,
                 output=None, write=print, timer_scale=1, **_):
        self.configuration, self.initial = configuration, initial
        self.services, self.installation_root = services, installation_root
        self.address, self.start_local = address, start_local
        self.output = Path(output) if output else None
        self.write, self.stack = write, AsyncExitStack()
        self.handle = self.result = None
        self.cached = {"state": None, "ready": False}
        self.closed = False
        self.timer_scale = timer_scale
        self._query_lock = asyncio.Lock()
        self._query_at = float("-inf")
        self._progress_at = float("-inf")
        self._progress_cached = {"state": None, "ready": False}
        self.progress_query_error = None
        self.query_diagnostic = None
        self.query_diagnostics = []
        self.close_diagnostics = []
        self._query_diagnostics_saved = False
        self.query_error = None
        self.run_id = "chain-fact-demo-" + uuid4().hex
        self.start = build_start(configuration, initial, engine_run_id=self.run_id)

    now = staticmethod(utc_now)

    def _save(self, name, value):
        if self.output:
            with (self.output / name).open("x", encoding="utf8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)

    async def __aenter__(self):
        try:
            if self.start_local:
                binary = Path(os.getenv("CHAIN_TEMPORAL_SERVER_PATH", "/tmp/temporal-sdk-python-1.31.0"))
                if not binary.is_file():
                    raise FileNotFoundError("Set CHAIN_TEMPORAL_SERVER_PATH to an installed Temporal CLI; this demo does not download")
                environment = await asyncio.wait_for(WorkflowEnvironment.start_local(
                    dev_server_existing_path=str(binary), ui=False), timeout=60)
                await self.stack.enter_async_context(environment)
                client = environment.client
            else:
                client = await asyncio.wait_for(Client.connect(self.address or "localhost:7233"), timeout=10)
            if not await client.service_client.check_health():
                raise RuntimeError("Temporal Service health check failed")
            self.address = client.service_client.config.target_host
            native = FactWorkerRuntime(self.configuration, self.installation_root, services=self.services)
            activities = DemoActivities(native, self.output / "commands.jsonl" if self.output else None)
            await self.stack.enter_async_context(Worker(client, task_queue=queue_for_configuration(self.configuration),
                workflows=[FactEpisodeWorkflow], activities=activities.registered))
            self.handle = await client.start_workflow(FactEpisodeWorkflow.run, self.start, id=self.run_id,
                task_queue=queue_for_configuration(self.configuration))
            self.result = asyncio.create_task(self.handle.result())
            async with asyncio.timeout(30):
                while not (await self.progress()).get("ready"):
                    if self.result.done():
                        await self.result
                    await asyncio.sleep(.05)
            self._save("temporal_connection.json", {"address": self.address, "namespace": client.namespace,
                "workflow_id": self.run_id, "task_queue": queue_for_configuration(self.configuration),
                "owned_in_memory_service": self.start_local,
                "close_policy": "CLIENT_AND_EMBEDDED_WORKER_STOP; no cancel/terminate Signal"})
            self.write("REAL_TEMPORAL_ADDRESS | " + self.address)
            return self
        except BaseException as error:
            self._close_primary = {"stage": error.stage if isinstance(error, DemoQueryError) else "startup",
                                   "code": self._diagnostic(error)}
            try:
                await self.close()
            except BaseException:
                # Keep startup/Query admission failure primary; close records
                # its own failures without replacing that original exception.
                pass
            raise

    def _completed_snapshot(self):
        """Read a completed Workflow result; never turn its failure into data."""
        try:
            value = self.result.result()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = type(error).__name__
            self.query_diagnostic = {"stage": "workflow_result", "code": code}
            raise DemoQueryError("workflow_result", code) from None
        if (not isinstance(value, dict) or value.get("query_schema") == "chain-fact-progress/v1"
                or value.get("termination_phase") != "FINALIZED"):
            self.query_diagnostic = {"stage": "workflow_result", "code": "WORKFLOW_RESULT_INVALID"}
            raise DemoQueryError("workflow_result", "WORKFLOW_RESULT_INVALID")
        self.cached = value
        self.query_error = self.progress_query_error = None
        self.query_diagnostic = None
        return value

    async def _query_or_completed(self, name, *, args=None):
        """Race only the read-only RPC against the independently owned result.

        Cancelling the losing RPC task never calls Workflow cancel/terminate.
        The result task belongs to the runtime and must outlive each reader.
        """
        if self.result is not None and self.result.done():
            return True, self._completed_snapshot()
        call = self.handle.query(name) if args is None else self.handle.query(name, args=args)
        query_task = asyncio.create_task(call)
        try:
            if self.result is None:
                return False, await query_task
            await asyncio.wait((query_task, self.result), return_when=asyncio.FIRST_COMPLETED)
            # A failed Workflow must also win when both futures became ready.
            if self.result.done():
                return True, self._completed_snapshot()
            return False, query_task.result()
        finally:
            if not query_task.done():
                query_task.cancel()
            await asyncio.gather(query_task, return_exceptions=True)

    async def snapshot(self):
        if self.handle is None or self.closed:
            return copy.deepcopy(self.cached)
        if self.result and self.result.done() and not self.result.cancelled():
            self.cached = self._completed_snapshot()
            return copy.deepcopy(self.cached)
        # Coalesce only UI/Simulator Queries, never source preflight results.
        # Multiple pollers must not fill the Service's consistent-query buffer.
        deadline = monotonic() + 5
        try:
            # Queueing behind another reader consumes this same budget.
            async with asyncio.timeout(5):
                async with self._query_lock:
                    if self.result is not None and self.result.done():
                        return copy.deepcopy(self._completed_snapshot())
                    if monotonic() - self._query_at < .1:
                        return copy.deepcopy(self.cached)
                    while True:
                        try:
                            _, self.cached = await self._query_or_completed("snapshot")
                            self.query_error = None
                            self.query_diagnostic = None
                            self._query_at = monotonic()
                            return copy.deepcopy(self.cached)
                        except RPCError as error:
                            self.query_error = error.status.name
                            self.query_diagnostic = {"stage": "snapshot", "code": error.status.name}
                            if error.status not in {RPCStatusCode.RESOURCE_EXHAUSTED, RPCStatusCode.UNAVAILABLE,
                                                    RPCStatusCode.DEADLINE_EXCEEDED} or monotonic() >= deadline:
                                raise
                            await asyncio.sleep(min(.1, max(0, deadline - monotonic())))
        except TimeoutError:
            if self.result is not None and self.result.done():
                return copy.deepcopy(self._completed_snapshot())
            self.query_error = "QUERY_TIMEOUT"
            self.query_diagnostic = {"stage": "snapshot", "code": "QUERY_TIMEOUT"}
            raise

    def _record_query_attempt(self, *, stage, attempt, status, code=None):
        self.query_diagnostics.append({"stage": stage, "attempt": attempt, "maximum_attempts": 3,
                                       "status": status, "code": code})

    async def _progress_attempt(self):
        # One read-only RPC per attempt; queueing consumes the same five-second
        # budget. Only progress has bounded recovery across these attempts.
        async with asyncio.timeout(5):
            async with self._query_lock:
                if self.result is not None and self.result.done():
                    return progress_from_snapshot(self._completed_snapshot())
                if monotonic() - self._progress_at < .1:
                    return copy.deepcopy(self._progress_cached)
                completed, value = await self._query_or_completed("progress")
                self._progress_cached = progress_from_snapshot(value) if completed else value
                self._progress_at = monotonic()
                return copy.deepcopy(self._progress_cached)

    async def progress(self):
        if self.handle is None or self.closed:
            return progress_from_snapshot(self.cached)
        if self.result and self.result.done() and not self.result.cancelled():
            return progress_from_snapshot(self._completed_snapshot())
        transient = {"QUERY_TIMEOUT", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED"}
        for attempt in range(1, 4):
            try:
                value = await self._progress_attempt()
                self.progress_query_error = None
                self.query_diagnostic = None
                if attempt > 1:
                    self._record_query_attempt(stage="progress", attempt=attempt, status="RECOVERED")
                return value
            except Exception as error:
                if not isinstance(error, DemoQueryError) and self.result is not None and self.result.done():
                    # A terminal result can arrive at the read deadline.
                    value = progress_from_snapshot(self._completed_snapshot())
                    self._record_query_attempt(stage="progress", attempt=attempt, status="RECOVERED_FROM_RESULT")
                    return value
                stage = error.stage if isinstance(error, DemoQueryError) else "progress"
                code = error.code if isinstance(error, DemoQueryError) else (
                    "QUERY_TIMEOUT" if isinstance(error, TimeoutError) else
                    error.status.name if isinstance(error, RPCError) else type(error).__name__)
                retryable = stage == "progress" and code in transient
                status = "EXHAUSTED" if retryable and attempt == 3 else "RETRYABLE_FAILURE" if retryable else "PERMANENT_FAILURE"
                self.progress_query_error = code
                self.query_diagnostic = {"stage": stage, "code": code}
                self._record_query_attempt(stage=stage, attempt=attempt, status=status, code=code)
                if not retryable or attempt == 3:
                    raise DemoQueryError(stage, code) from None
                self.write("QUERY_RETRY | stage=progress | code=" + code + " | attempt=" + str(attempt) + "/3")
                await asyncio.sleep(.1)

    async def hitl_view(self, request_id, revision=None):
        if self.handle is None or self.closed or self.result and self.result.done() and not self.result.cancelled():
            return hitl_from_snapshot(await self.snapshot(), request_id, revision)
        deadline = monotonic() + 5
        try:
            async with asyncio.timeout(5):
                async with self._query_lock:
                    while True:
                        try:
                            completed, value = await self._query_or_completed("hitl_view", args=[request_id, revision])
                            return hitl_from_snapshot(value, request_id, revision) if completed else value
                        except RPCError as error:
                            if error.status not in {RPCStatusCode.RESOURCE_EXHAUSTED, RPCStatusCode.UNAVAILABLE,
                                                    RPCStatusCode.DEADLINE_EXCEEDED} or monotonic() >= deadline:
                                raise
                            await asyncio.sleep(min(.1, max(0, deadline - monotonic())))
        except Exception as error:
            if isinstance(error, DemoQueryError):
                raise
            if self.result is not None and self.result.done():
                return hitl_from_snapshot(self._completed_snapshot(), request_id, revision)
            code = "QUERY_TIMEOUT" if isinstance(error, TimeoutError) else (
                error.status.name if isinstance(error, RPCError) else type(error).__name__)
            self.query_diagnostic = {"stage": "hitl_view", "code": code}
            raise DemoQueryError("hitl_view", code) from None

    async def submit_event(self, value):
        await self.handle.signal("submit_event", value)
        return {"status": "SUBMITTED_AWAITING_ENGINE"}

    async def submit_decision(self, value):
        await self.handle.signal("submit_decision", value)
        return {"status": "SUBMITTED_AWAITING_ENGINE"}

    async def request_hitl_resume(self, value):
        await self.handle.signal("request_hitl_resume", value)
        return {"status": "SUBMITTED_AWAITING_ENGINE"}

    async def source_reconnected(self, value):
        await self.handle.signal("source_reconnected", value)

    async def timer_status(self, view=None):
        from .timers import timer_views
        if view is None:
            try:
                view = await self.snapshot()
            except (RPCError, TimeoutError):
                view = self.cached
            verified = self.query_error is None
        else:
            verified = self.progress_query_error is None
        rows = timer_views(view, now=self.now(), verified=verified,
                           clock_kind="TEMPORAL_DURABLE")
        for row in rows:
            row["original_seconds"] = row["seconds"] / self.timer_scale
        return rows

    @staticmethod
    def _diagnostic(error):
        return (error.code if isinstance(error, DemoQueryError) else
            error.status.name if isinstance(error, RPCError) else
            "QUERY_TIMEOUT" if isinstance(error, TimeoutError) else type(error).__name__)

    def _close_stage(self, stage, error=None):
        self.close_diagnostics.append({"stage": stage, "status": "FAILED" if error else "COLLECTED",
                                       "code": self._diagnostic(error) if error else None})

    def _save_query_diagnostics(self):
        if self.output and not self._query_diagnostics_saved:
            self._save("query-diagnostics.json", {"schema": "chain-fact-demo-query-diagnostics/v1",
                "progress_maximum_attempts": 3, "query_attempt_timeout_s": 5,
                "retry_interval_s": .1, "attempts": copy.deepcopy(self.query_diagnostics)})
            self._query_diagnostics_saved = True

    async def close(self):
        if self.closed:
            return
        history, history_json, description = None, None, None
        replay_status = "NOT_ATTEMPTED"
        try:
            if self.handle:
                try:
                    self.cached = await self.snapshot()
                    self._close_stage("snapshot")
                except Exception as error:
                    self._close_stage("snapshot", error)
                # History is a separate Service read. A stalled consistent
                # Query must not prevent retaining the recorded failure input.
                try:
                    history = await asyncio.wait_for(self.handle.fetch_history(), timeout=5)
                    self._close_stage("history")
                except Exception as error:
                    self._close_stage("history", error)
                if history is not None:
                    try:
                        history_json = history.to_json()
                        self._save("temporal_history.json", json.loads(history_json))
                        self._close_stage("history_save")
                    except Exception as error:
                        self._close_stage("history_save", error)
                try:
                    description = await asyncio.wait_for(self.handle.describe(), timeout=5)
                    self._save("temporal_execution.json", {"workflow_id": self.run_id, "run_id": description.run_id,
                        "status": description.status.name, "history_events": len(history.events) if history else None})
                    self._close_stage("describe")
                except Exception as error:
                    self._close_stage("describe", error)
                if history is not None:
                    report = {"status": "FAILED", "workflow_id": self.run_id,
                        "history_event_count": len(history.events),
                        "history_json_bytes": len(history_json.encode("utf8")) if history_json else None}
                    try:
                        await asyncio.wait_for(Replayer(workflows=[FactEpisodeWorkflow]).replay_workflow(history), timeout=90)
                        report["status"] = "PASSED"
                        self._close_stage("replay")
                    except Exception as error:
                        report["code"] = self._diagnostic(error)
                        self._close_stage("replay", error)
                    replay_status = report["status"]
                else:
                    report = {"status": "UNAVAILABLE", "workflow_id": self.run_id, "code": "HISTORY_UNAVAILABLE"}
                    replay_status = "UNAVAILABLE"
                try:
                    self._save("replay.json", report)
                    self._close_stage("replay_save")
                except Exception as error:
                    self._close_stage("replay_save", error)
                if replay_status == "PASSED":
                    self.write("REAL_HISTORY_REPLAY | PASSED | " +
                               (description.status.name if description else "STATUS_UNAVAILABLE"))
        finally:
            self.closed = True
            try:
                if self.result and not self.result.done():
                    self.result.cancel()  # Cancel only the result reader, never the Workflow.
                    await asyncio.gather(self.result, return_exceptions=True)
                await self.stack.aclose()
                self._close_stage("cleanup")
            except Exception as error:
                self._close_stage("cleanup", error)
            try:
                self._save_query_diagnostics()
            except Exception as error:
                self._close_stage("query_diagnostics_save", error)
            errors = [row for row in self.close_diagnostics if row["status"] == "FAILED"]
            try:
                self._save("close-diagnostics.json", {"schema": "chain-fact-demo-close-diagnostics/v1",
                    "status": "FAILED" if errors else "COLLECTED", "history_available": history is not None,
                    "replay_status": replay_status, "primary_exception": getattr(self, "_close_primary", None),
                    "stages": copy.deepcopy(self.close_diagnostics)})
            except Exception as error:
                self._close_stage("close_diagnostics_save", error)
        errors = [row for row in self.close_diagnostics if row["status"] == "FAILED"]
        if errors:
            raise DemoQueryError("close." + errors[0]["stage"], errors[0]["code"])

    async def __aexit__(self, exc_type, exc, traceback):
        if exc is not None:
            self._close_primary = {"stage": exc.stage if isinstance(exc, DemoQueryError) else "body",
                                   "code": self._diagnostic(exc)}
        try:
            await self.close()
        except BaseException:
            if exc is None:
                raise
            # Returning false leaves the original body exception in flight.
            # Secondary close failures are retained separately, not substituted.
        return False
