"""Pure v0.3 wire-contract validation. Legacy runtime migration is a later step.

These checks validate identity, provenance, scope and snapshot binding. They do
not decide clinical eligibility, parse prose or perform file/network I/O.
"""
from __future__ import annotations

import copy

from .validation import canonical_hash, fields, hash_value, json_value, positive, strings, text, timestamp


def source_ref(ref, *, field=False):
    required = {"system", "record_id", "version"} | ({"field"} if field else set())
    fields(ref, required, where="source_ref")
    text(ref["system"]); text(ref["record_id"])
    positive(ref["version"], "source_ref.version", integer=True)
    if field:
        text(ref["field"])


def _schema(obj, expected):
    if obj["contract_schema"] != expected:
        raise ValueError(f"Unsupported contract_schema: {obj['contract_schema']}")


def _refs(refs):
    if not isinstance(refs, list):
        raise ValueError("dependencies/evidence must be a list")
    for ref in refs:
        source_ref(ref, field=True)
    ids = [canonical_hash(ref) for ref in refs]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate evidence dependency")
    return set(ids)


def validate_event(event):
    json_value(event, "Event")
    fields(event, {"contract_schema", "event_id", "event_type", "site_id", "patient_id",
                   "encounter_id", "episode_id", "source_ref", "sequence_no", "source_event_time",
                   "published_at", "emitted_time", "received_at", "payload", "payload_hash"},
           {"source_sequence_no"}, "Event")
    _schema(event, "chain-event/v0.3")
    for key in ("event_id", "event_type", "site_id", "patient_id", "encounter_id", "episode_id"):
        text(event[key], key)
    source_ref(event["source_ref"])
    positive(event["sequence_no"], "sequence_no", integer=True)
    if "source_sequence_no" in event:
        positive(event["source_sequence_no"], "source_sequence_no", integer=True)
    timestamp(event["source_event_time"])
    published, emitted, received = [timestamp(event[k], k) for k in ("published_at", "emitted_time", "received_at")]
    if not published <= emitted <= received:
        raise ValueError("Event publication must precede emission and receipt")
    if not isinstance(event["payload"], dict):
        raise ValueError("Event payload must be an object")
    if any(k.startswith("mock_") or k in {"expected_state_history", "recorded_hitl_events"} for k in event["payload"]):
        raise ValueError("Test sidecars are not Event payload fields")
    hash_value(event["payload_hash"])
    if canonical_hash(event["payload"]) != event["payload_hash"]:
        raise ValueError("Event payload hash mismatch")


def make_snapshot(facts: dict, known_at: str) -> dict:
    snapshot = {"contract_schema": "chain-context/v0.3", "known_at": known_at, "facts": copy.deepcopy(facts)}
    snapshot["snapshot_id"] = canonical_hash(snapshot)
    validate_snapshot(snapshot)
    return snapshot


def validate_snapshot(snapshot):
    fields(snapshot, {"contract_schema", "snapshot_id", "known_at", "facts"}, where="Snapshot")
    _schema(snapshot, "chain-context/v0.3")
    known = timestamp(snapshot["known_at"])
    if not isinstance(snapshot["facts"], dict):
        raise ValueError("Snapshot facts must be an object")
    for name, fact in snapshot["facts"].items():
        text(name)
        fields(fact, {"value", "status", "source_ref", "source_time", "known_at"},
               {"unit", "confirmation_status", "dependencies"}, "Fact")
        json_value(fact["value"])
        text(fact["status"])
        source_ref(fact["source_ref"], field=True)
        if "dependencies" in fact:
            if not fact["dependencies"]:
                raise ValueError("Snapshot dependencies cannot discard source provenance")
            _refs(fact["dependencies"])
        timestamp(fact["source_time"])
        if timestamp(fact["known_at"]) > known:
            raise ValueError("Snapshot contains a fact not yet known")
        for key in ("unit", "confirmation_status"):
            if key in fact:
                text(fact[key], key)
    hash_value(snapshot["snapshot_id"])
    if snapshot["snapshot_id"] != canonical_hash({k: v for k, v in snapshot.items() if k != "snapshot_id"}):
        raise ValueError("Snapshot content hash mismatch")


def _agent_request_shape(request):
    fields(request, {"contract_schema", "request_id", "agent_id", "agent_version", "manifest_hash",
                     "mode", "input_snapshot_id", "scope", "dependencies"}, {"evaluated_at"}, "Agent Request")
    _schema(request, "chain-agent-request/v0.3")
    for key in ("request_id", "agent_id", "agent_version", "mode"):
        text(request[key], key)
    hash_value(request["manifest_hash"])
    hash_value(request["input_snapshot_id"])
    strings(request["scope"], "scope", nonempty=True)
    _refs(request["dependencies"])
    if "evaluated_at" in request:
        timestamp(request["evaluated_at"])


def validate_agent_request(request, snapshot):
    _agent_request_shape(request)
    validate_snapshot(snapshot)
    if "evaluated_at" in request and timestamp(request["evaluated_at"]) < timestamp(snapshot["known_at"]):
        raise ValueError("Agent evaluation precedes its input Snapshot")
    if "evaluated_at" in request and any(timestamp(f["source_time"]) > timestamp(request["evaluated_at"])
                                          for f in snapshot["facts"].values()):
        raise ValueError("Agent input contains future source evidence")
    if request["input_snapshot_id"] != snapshot["snapshot_id"]:
        raise ValueError("Agent input Snapshot mismatch")
    scope = strings(request["scope"], "scope", nonempty=True)
    if set(scope) - snapshot["facts"].keys():
        raise ValueError("Agent scope must exist in its input Snapshot (unknown facts may be explicit)")
    permitted = {canonical_hash(ref) for name in scope for ref in
                 snapshot["facts"][name].get("dependencies", [snapshot["facts"][name]["source_ref"]])}
    if _refs(request["dependencies"]) != permitted:
        raise ValueError("Agent dependencies must match scoped Snapshot provenance")


def _agent_result_shape(result):
    fields(result, {"contract_schema", "request_id", "agent_id", "agent_version", "manifest_hash",
                    "input_snapshot_id", "status", "result", "evidence", "produced_time"}, {"error"}, "Agent Result")
    _schema(result, "chain-agent-result/v0.3")
    for key in ("request_id", "agent_id", "agent_version"):
        text(result[key], key)
    hash_value(result["manifest_hash"])
    hash_value(result["input_snapshot_id"])
    timestamp(result["produced_time"])
    _refs(result["evidence"])
    if result["status"] == "SUCCESS":
        if not isinstance(result["result"], dict) or "error" in result:
            raise ValueError("SUCCESS requires a result object without error")
        json_value(result["result"])
    elif result["status"] == "FAILED":
        if result["result"] is not None:
            raise ValueError("FAILED cannot carry a success result")
        text(result.get("error"), "error")
    else:
        raise ValueError("Agent status must be SUCCESS or FAILED")


def validate_agent_result(result, request):
    _agent_request_shape(request)
    _agent_result_shape(result)
    for key in ("request_id", "agent_id", "agent_version", "manifest_hash", "input_snapshot_id"):
        if result[key] != request[key]:
            raise ValueError(f"Agent Result {key} mismatch")
    if _refs(result["evidence"]) - _refs(request["dependencies"]):
        raise ValueError("Agent Result evidence outside request dependencies")
    if result["status"] == "SUCCESS" and request["dependencies"] and not result["evidence"]:
        raise ValueError("SUCCESS requires evidence for its scoped input")


def validate_hitl_request(request, snapshot, results):
    fields(request, {"contract_schema", "request_id", "checkpoint", "evidence_snapshot_id",
                     "dependencies", "result_ids", "roles", "options", "required_confirmations",
                     "confirmations_on_decisions", "question"}, where="HITL Request")
    _schema(request, "chain-hitl-request/v0.3")
    for key in ("request_id", "checkpoint", "question"):
        text(request[key], key)
    validate_snapshot(snapshot)
    if request["evidence_snapshot_id"] != snapshot["snapshot_id"]:
        raise ValueError("HITL evidence Snapshot mismatch")
    dependencies = _refs(request["dependencies"])
    if dependencies - {canonical_hash(ref) for f in snapshot["facts"].values()
                       for ref in f.get("dependencies", [f["source_ref"]])}:
        raise ValueError("HITL dependency outside evidence Snapshot")
    for key in ("roles", "options", "result_ids"):
        strings(request[key], key, nonempty=True)
    strings(request["required_confirmations"])
    strings(request["confirmations_on_decisions"])
    if set(request["confirmations_on_decisions"]) - set(request["options"]):
        raise ValueError("Confirmations reference an unknown decision")
    if not isinstance(results, list):
        raise ValueError("HITL Agent results must be a list")
    for result in results:
        _agent_result_shape(result)
    by_id = {result["request_id"]: result for result in results}
    if len(by_id) != len(results):
        raise ValueError("Duplicate Agent Result identity")
    for result_id in request["result_ids"]:
        result = by_id.get(result_id)
        if not result or result["status"] != "SUCCESS" or result["input_snapshot_id"] != snapshot["snapshot_id"]:
            raise ValueError("HITL needs accepted results bound to the evidence Snapshot")
        if _refs(result["evidence"]) - dependencies:
            raise ValueError("HITL dependencies omit Agent evidence")


def validate_hitl_resume_shape(request):
    """Request a new question for a recorded hold; this is not a decision."""
    json_value(request, "HITL Resume")
    fields(request, {"contract_schema", "command_id", "prior_request_id", "actor", "requested_at"},
           where="HITL Resume")
    _schema(request, "chain-hitl-resume/v0.3")
    text(request["command_id"], "command_id")
    text(request["prior_request_id"], "prior_request_id")
    fields(request["actor"], {"role", "staff_id"}, where="actor")
    text(request["actor"]["role"])
    text(request["actor"]["staff_id"])
    timestamp(request["requested_at"])


def validate_hitl_decision_shape(decision):
    """Check wire shape before queuing; fixed-evidence semantics are checked later."""
    json_value(decision, "HITL Decision")
    fields(decision, {"contract_schema", "request_id", "evidence_snapshot_id", "decision", "actor",
                      "confirmed_items", "evidence_viewed", "comment", "recorded_at"}, where="HITL Decision")
    _schema(decision, "chain-hitl-decision/v0.3")
    for key in ("request_id", "evidence_snapshot_id", "decision"):
        text(decision[key], key)
    fields(decision["actor"], {"role", "staff_id"}, where="actor")
    text(decision["actor"]["role"])
    text(decision["actor"]["staff_id"])
    strings(decision["confirmed_items"])
    strings(decision["evidence_viewed"], nonempty=True)
    text(decision["comment"])
    timestamp(decision["recorded_at"])


def validate_hitl_decision(decision, request):
    validate_hitl_decision_shape(decision)
    for key in ("request_id", "evidence_snapshot_id"):
        if decision[key] != request[key]:
            raise ValueError(f"HITL Decision {key} mismatch")
    if decision["decision"] not in request["options"]:
        raise ValueError("Unknown HITL decision")
    if decision["actor"]["role"] not in request["roles"]:
        raise ValueError("Unauthorized HITL role (synthetic role check only)")
    confirmed = strings(decision["confirmed_items"])
    if set(confirmed) - set(request["required_confirmations"]):
        raise ValueError("Unknown confirmation")
    if decision["decision"] in request["confirmations_on_decisions"] and set(request["required_confirmations"]) - set(confirmed):
        raise ValueError("HITL confirmations missing")
    viewed = strings(decision["evidence_viewed"], nonempty=True)
    if not set(viewed).issubset(set(request["result_ids"]) | {request["evidence_snapshot_id"]}):
        raise ValueError("HITL viewed evidence is outside its fixed bundle")
