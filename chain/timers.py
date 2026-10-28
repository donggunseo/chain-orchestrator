"""Pure Clinical timer ownership. No clock reads, sleeps, I/O or State moves.

The caller synchronizes current owners and cancellation facts before accepting
a firing. An accepted firing returns a declared action; its execution and the
usual work preparation belong to the caller, not this module.
"""
from __future__ import annotations

import copy
from datetime import timedelta
import math

from .compiler import canonical_hash
from .errors import ConfigError
from .projections import StateProjector
from .runtime_contracts import (_hash, _integer, _shape, _text, timestamp,
                                validate_identity)
from .shape import check_json


_PAYLOAD_FIELDS = {"contract_schema", "identity", "engine_run_id", "run_pin", "timer_id",
                   "timer_definition_id", "purpose", "owner", "due_at", "firing_no"}
_LIFECYCLES = {"ACTIVE", "DORMANT", "CLINICAL_CLOSED", "ADMIN_CLOSING", "COMPLETED"}
_DECISION_STATUSES = {"OPEN", "DEFERRED", "PREPARING", "CLOSED", "DECIDED", "PREPARATION_FAILED"}


def _fail(code, path, message):
    raise ConfigError(code, path, message) from None


def validate_timer(payload):
    """Validate the closed wire payload; ownership still needs TimerOwner."""
    check_json(payload, path="timer")
    _shape(payload, _PAYLOAD_FIELDS, "timer")
    if payload["contract_schema"] != "chain-fact-timer/v1":
        _fail("TIMER_CONTRACT_INVALID", "contract_schema", "Unsupported timer contract")
    validate_identity(payload["identity"])
    for field in ("engine_run_id", "timer_id", "timer_definition_id"):
        _text(payload[field], "timer." + field)
    _hash(payload["run_pin"], "timer.run_pin")
    _shape(payload["owner"], {"kind", "id", "epoch"}, "timer.owner")
    if not isinstance(payload["owner"]["kind"], str) or payload["owner"]["kind"] not in {"STATE_RESIDENCY", "DECISION_CYCLE"}:
        _fail("TIMER_OWNER_UNSUPPORTED", "timer.owner.kind", "Only declared State/decision owners are supported")
    _text(payload["owner"]["id"], "timer.owner.id")
    _integer(payload["owner"]["epoch"], "timer.owner.epoch")
    _integer(payload["firing_no"], "timer.firing_no")
    if payload["firing_no"] != 1 or not isinstance(payload["purpose"], str) or payload["purpose"] not in {"HITL_RETRY", "TASK_TRIGGER"}:
        _fail("TIMER_CONTRACT_INVALID", "timer", "Unsupported purpose or firing number")
    timestamp(payload["due_at"], path="timer.due_at")
    return copy.deepcopy(payload)


class TimerOwner:
    """Own immutable timer epochs and return each declared action at most once.

    This owner has no autonomous clock. ``sync`` and ``fire`` must be serialized
    with the Episode reducer. A duplicate accepted firing has duplicate=True
    and on_expire=None; the original acceptance remains in records/history.
    """
    def __init__(self, definition, *, identity, engine_run_id, run_pin):
        self.identity = validate_identity(identity)
        self.engine_run_id = _text(engine_run_id, "engine_run_id")
        self.run_pin = _hash(run_pin, "run_pin")
        projector = StateProjector(definition)
        if self.identity["site_id"] != definition["site_id"]:
            _fail("IDENTITY_MISMATCH", "identity.site_id", "Timer site differs from definition")
        self._definition = copy.deepcopy(definition)
        self.definition_hash = canonical_hash(definition)
        self._timers = copy.deepcopy(definition.get("timers"))
        if not isinstance(self._timers, dict):
            _fail("TIMER_DEFINITION_INVALID", "timers", "Expected declared timers")
        for name, spec in self._timers.items():
            _text(name, "timer_definition_id")
            _shape(spec, {"owner", "anchor", "after", "on_expire", "cancel_when", "reset_on", "repeat", "origin_refs"}, "timers." + name)
            _shape(spec["owner"], {"kind", "id"}, "timer.owner")
            kind, owner_id = spec["owner"]["kind"], spec["owner"]["id"]
            tables = {"state": definition["states"], "decision": definition["decisions"]}
            if not isinstance(kind, str) or kind not in tables or not isinstance(owner_id, str) or owner_id not in tables[kind]:
                _fail("TIMER_OWNER_UNSUPPORTED", name, "Timer owner must be a declared State/decision")
            if not isinstance(spec["anchor"], str) or spec["anchor"] not in ({"state_entered"} if kind == "state" else {"decision_opened", "decision_deferred"}):
                _fail("TIMER_ANCHOR_INVALID", name, "Timer anchor differs from its owner kind")
            _shape(spec["after"], {"value", "unit"}, "timer.after")
            amount, unit = spec["after"]["value"], spec["after"]["unit"]
            if (type(amount) not in (int, float) or type(amount) is float and not math.isfinite(amount)
                    or amount <= 0 or not isinstance(unit, str) or unit not in {"s", "min", "h"}):
                _fail("TIMER_DURATION_INVALID", name, "Expected a positive finite Clinical duration")
            _shape(spec["repeat"], {"max_firings"}, "timer.repeat")
            if type(spec["repeat"]["max_firings"]) is not int or spec["repeat"]["max_firings"] != 1:
                _fail("TIMER_REPEAT_UNSUPPORTED", name, "v1 permits one firing per owner epoch")
            if spec["reset_on"] != "new_owner_generation":
                _fail("TIMER_RESET_UNSUPPORTED", name, "Timer reset requires a new owner generation")
            _shape(spec["on_expire"], {"kind", "id"}, "timer.on_expire")
            action = spec["on_expire"]
            if (not isinstance(action["kind"], str) or action["kind"] not in {"task", "resume_decision"}
                    or not isinstance(action["id"], str)
                    or action["id"] not in (definition["tasks"] if action["kind"] == "task" else definition["decisions"])):
                _fail("TIMER_ACTION_UNSUPPORTED", name, "Timer action must reference a declared task/decision")
            if action["kind"] == "resume_decision" and (kind != "decision" or action["id"] != owner_id):
                _fail("TIMER_ACTION_UNSUPPORTED", name, "Resume action must match its decision owner")
            projector._validate_expression(spec["cancel_when"])
        self._records, self._anchor_index, self._journal = {}, {}, []
        self._clock = None

    @property
    def records(self):
        return copy.deepcopy(self._records)

    def pending(self):
        return [copy.deepcopy(record["payload"]) for _, record in sorted(self._records.items())
                if record["status"] == "ARMED"]

    def _time(self, now):
        instant = timestamp(now, path="now")
        if self._clock is not None and instant < timestamp(self._clock):
            _fail("INVALID_TIME", "now", "Timer processing time cannot move backwards")
        return instant

    def _note(self, operation, body, now, result):
        row = {"sequence": len(self._journal) + 1, "operation": operation,
               "body": copy.deepcopy(body), "at": now, "result": copy.deepcopy(result),
               "previous_hash": self._journal[-1]["entry_hash"] if self._journal else None}
        row["entry_hash"] = canonical_hash(row)
        self._journal.append(row)
        self._clock = now

    def _frames(self, state_entries, decision_cycles, now):
        check_json(state_entries, path="state_entries"); check_json(decision_cycles, path="decision_cycles")
        if not isinstance(state_entries, dict) or not isinstance(decision_cycles, dict):
            _fail("TIMER_OWNER_INVALID", "owners", "Expected explicit named owner records")
        for state, entry in state_entries.items():
            _shape(entry, {"id", "epoch", "entered_at", "active"}, "state_entries." + state)
            if state not in self._definition["states"] or entry["id"] != state or type(entry["active"]) is not bool:
                _fail("TIMER_OWNER_INVALID", state, "State owner identity/active flag differs")
            _integer(entry["epoch"], "state_entry.epoch")
            if timestamp(entry["entered_at"]) > now:
                _fail("FUTURE_INPUT", state, "State entry is not known at processing time")
        for name, entry in decision_cycles.items():
            _shape(entry, {"id", "epoch", "opened_at", "deferred_at", "status"}, "decision_cycles." + name)
            if (name not in self._definition["decisions"] or not isinstance(entry["status"], str)
                    or entry["status"] not in _DECISION_STATUSES):
                _fail("TIMER_OWNER_INVALID", name, "Unknown decision owner or status")
            _text(entry["id"], "decision_cycle.id"); _integer(entry["epoch"], "decision_cycle.epoch")
            opened = timestamp(entry["opened_at"])
            if opened > now:
                _fail("FUTURE_INPUT", name, "Decision opening is not known at processing time")
            if entry["deferred_at"] is not None:
                deferred = timestamp(entry["deferred_at"])
                if not opened <= deferred <= now:
                    _fail("INVALID_TIME", name, "Deferral time is outside this decision cycle")
            if entry["status"] == "DEFERRED" and entry["deferred_at"] is None:
                _fail("TIMER_ANCHOR_INVALID", name, "Deferred owner requires its actual deferral time")
        return copy.deepcopy(state_entries), copy.deepcopy(decision_cycles)

    def _owner(self, spec, states, decisions):
        declaration = spec["owner"]
        if declaration["kind"] == "state":
            entry = states.get(declaration["id"])
            if not entry or not entry["active"]:
                return None
            return {"kind": "STATE_RESIDENCY", "id": entry["id"], "epoch": entry["epoch"]}, entry["entered_at"]
        entry = decisions.get(declaration["id"])
        needed = "DEFERRED" if spec["anchor"] == "decision_deferred" else "OPEN"
        if not entry or entry["status"] != needed:
            return None
        anchor = entry["deferred_at"] if needed == "DEFERRED" else entry["opened_at"]
        return {"kind": "DECISION_CYCLE", "id": entry["id"], "epoch": entry["epoch"]}, anchor

    def _payload(self, name, owner, anchor):
        spec = self._timers[name]
        multiplier = {"s": 1, "min": 60, "h": 3600}[spec["after"]["unit"]]
        try:
            due = timestamp(anchor) + timedelta(seconds=spec["after"]["value"] * multiplier)
        except (OverflowError, ValueError):
            _fail("TIMER_DURATION_INVALID", name, "Clinical duration exceeds timestamp range")
        payload = {"contract_schema": "chain-fact-timer/v1", "identity": copy.deepcopy(self.identity),
                   "engine_run_id": self.engine_run_id, "run_pin": self.run_pin,
                   "timer_definition_id": name, "purpose": "HITL_RETRY" if spec["on_expire"]["kind"] == "resume_decision" else "TASK_TRIGGER",
                   "owner": copy.deepcopy(owner), "due_at": due.isoformat(), "firing_no": 1}
        payload["timer_id"] = canonical_hash({**payload, "definition_hash": self.definition_hash,
                                              "anchor_at": timestamp(anchor).isoformat()})
        return validate_timer(payload)

    def sync(self, *, now, state_entries, decision_cycles, evaluate, lifecycle="ACTIVE"):
        """Synchronize facts/owners and return newly armed payloads only."""
        if not callable(evaluate):
            _fail("TIMER_EVALUATOR_INVALID", "evaluate", "Provide the current pure condition evaluator")
        proofs = {name: copy.deepcopy(evaluate(copy.deepcopy(spec["cancel_when"])))
                  for name, spec in sorted(self._timers.items())}
        return self._sync(now=now, state_entries=state_entries, decision_cycles=decision_cycles,
                          proofs=proofs, lifecycle=lifecycle)

    def _sync(self, *, now, state_entries, decision_cycles, proofs, lifecycle):
        instant = self._time(now)
        if not isinstance(lifecycle, str) or lifecycle not in _LIFECYCLES:
            _fail("LIFECYCLE_INVALID", "lifecycle", "Unknown Episode lifecycle")
        states, decisions = self._frames(state_entries, decision_cycles, instant)
        check_json(proofs, path="timer_conditions")
        if not isinstance(proofs, dict) or set(proofs) != set(self._timers):
            _fail("TIMER_EVALUATOR_INVALID", "proofs", "Expected one cancellation proof per declaration")
        desired = {}
        anchors = copy.deepcopy(self._anchor_index)
        for name, spec in sorted(self._timers.items()):
            proof = proofs[name]
            if not isinstance(proof, dict) or not isinstance(proof.get("truth"), str) or proof["truth"] not in {"TRUE", "FALSE", "UNKNOWN"}:
                _fail("TIMER_EVALUATOR_INVALID", name, "Expected a three-valued cancellation proof")
            selected = self._owner(spec, states, decisions)
            if not selected:
                continue
            owner, anchor = selected
            key = canonical_hash({"timer": name, "owner": owner})
            normalized_anchor = timestamp(anchor).isoformat()
            if key in anchors and anchors[key] != normalized_anchor:
                _fail("OWNER_ANCHOR_CONFLICT", name, "An owner epoch cannot change its actual anchor")
            anchors[key] = normalized_anchor
            if lifecycle == "ACTIVE" and proof["truth"] != "TRUE":
                desired[name] = (self._payload(name, owner, anchor), normalized_anchor)
        self._anchor_index = anchors
        for record in self._records.values():
            if record["status"] != "ARMED":
                continue
            name = record["payload"]["timer_definition_id"]
            current = desired.get(name)
            if current is None or current[0] != record["payload"]:
                record.update(status="CANCELLED", cancelled_at=now,
                              cancel_code="LIFECYCLE_NOT_ACTIVE" if lifecycle != "ACTIVE" else
                              "CANCEL_CONDITION_TRUE" if proofs[name]["truth"] == "TRUE" else "OWNER_NO_LONGER_CURRENT")
        armed = []
        for payload, anchor in desired.values():
            identifier = payload["timer_id"]
            if identifier not in self._records:
                self._records[identifier] = {"payload": payload, "status": "ARMED", "anchor_at": anchor,
                    "armed_at": now, "cancelled_at": None, "cancel_code": None, "receipt": None}
                armed.append(copy.deepcopy(payload))
        self._note("sync", {"state_entries": states, "decision_cycles": decisions,
            "proofs": proofs, "lifecycle": lifecycle}, now, armed)
        return armed

    def fire(self, payload, *, now):
        """Accept a due current owner once, returning no external side effect."""
        payload = validate_timer(payload)
        for field, expected in (("identity", self.identity), ("engine_run_id", self.engine_run_id), ("run_pin", self.run_pin)):
            if payload[field] != expected:
                _fail("IDENTITY_MISMATCH", field, "Timer firing belongs to another run")
        instant = self._time(now)
        record = self._records.get(payload["timer_id"])
        if record and payload != record["payload"]:
            _fail("TIMER_ID_CONFLICT", "timer_id", "Registered timer payload cannot be changed")
        receipt = {"contract_schema": "chain-fact-timer-receipt/v1", "timer_id": payload["timer_id"],
                   "status": "IGNORED", "code": "TIMER_NOT_REGISTERED", "duplicate": False,
                   "on_expire": None, "processed_at": now}
        if record:
            if record["receipt"] is not None:
                receipt.update(status=record["receipt"]["status"], code="TIMER_ALREADY_HANDLED", duplicate=True)
            elif record["status"] == "CANCELLED":
                receipt["code"] = "TIMER_CANCELLED"
                record["receipt"] = copy.deepcopy(receipt)
            elif instant < timestamp(payload["due_at"]):
                receipt["code"] = "TIMER_NOT_DUE"
            else:
                receipt.update(status="ACCEPTED", code="TIMER_FIRED",
                               on_expire=copy.deepcopy(self._timers[payload["timer_definition_id"]]["on_expire"]))
                record.update(status="FIRED", receipt=copy.deepcopy(receipt))
        self._note("fire", {"payload": payload}, now, receipt)
        return receipt

    def export_state(self):
        state = {"contract_schema": "chain-fact-timer-owner/v1", "definition_hash": self.definition_hash,
                 "identity": copy.deepcopy(self.identity), "engine_run_id": self.engine_run_id,
                 "run_pin": self.run_pin, "journal": copy.deepcopy(self._journal)}
        state["state_hash"] = canonical_hash(state)
        return state

    @classmethod
    def from_state(cls, state, *, definition):
        try:
            check_json(state, path="timer_owner_state")
            _shape(state, {"contract_schema", "definition_hash", "identity", "engine_run_id", "run_pin", "journal", "state_hash"}, "timer_owner_state")
            if (state["contract_schema"] != "chain-fact-timer-owner/v1" or not isinstance(state["journal"], list)
                    or state["definition_hash"] != canonical_hash(definition)
                    or state["state_hash"] != canonical_hash({k: v for k, v in state.items() if k != "state_hash"})):
                _fail("STATE_INTEGRITY_MISMATCH", "timer_owner_state", "Saved definition or timer history hash differs")
            owner = cls(definition, identity=state["identity"], engine_run_id=state["engine_run_id"], run_pin=state["run_pin"])
            for row in state["journal"]:
                _shape(row, {"sequence", "operation", "body", "at", "result", "previous_hash", "entry_hash"}, "timer_journal")
                if row["entry_hash"] != canonical_hash({k: v for k, v in row.items() if k != "entry_hash"}):
                    _fail("STATE_INTEGRITY_MISMATCH", "timer_journal", "Saved timer entry hash differs")
                if row["operation"] == "sync":
                    owner._sync(now=row["at"], **copy.deepcopy(row["body"]))
                elif row["operation"] == "fire":
                    owner.fire(row["body"]["payload"], now=row["at"])
                else:
                    _fail("STATE_INTEGRITY_MISMATCH", "timer_journal", "Unknown timer history operation")
                if owner._journal[-1] != row:
                    _fail("STATE_INTEGRITY_MISMATCH", "timer_journal", "Replayed timer result/sequence/chain differs")
            return owner
        except ConfigError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError):
            _fail("STATE_INTEGRITY_MISMATCH", "timer_owner_state", "Malformed timer owner history")
