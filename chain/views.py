"""Detached read models; never mutate the Episode or query a source.

Progress is deliberately not a complete Snapshot/Agent result. Large source
objects, document text, execution inputs and result bodies belong to an exact
HITL detail view or the existing diagnostic snapshot. Select fields before
copying: calling the owners' complete-copy properties here defeats the purpose.
"""
from __future__ import annotations

import copy

from .errors import ConfigError


PROGRESS_SCHEMA = "chain-fact-progress/v1"
HITL_VIEW_SCHEMA = "chain-fact-hitl-view/v1"


def _fields(value, names):
    return {name: copy.deepcopy(value[name]) for name in names if name in value}


def _cell(value):
    result = _fields(value, ("validity", "source_status", "source_refs", "source_time",
                             "known_at", "unit", "confirmation_status"))
    content = value.get("value")
    if isinstance(content, (dict, list)):
        result.update(value_kind="object" if isinstance(content, dict) else "array", value_omitted=True)
    elif isinstance(content, str) and len(content) > 180:
        result.update(value=content[:179] + "…", value_truncated=True)
    else:
        result["value"] = content
    return result


def _execution(value):
    request, result = value["request"], value.get("result")
    summary = _fields(value, ("attempt_no", "result_use"))
    summary["request"] = _fields(request, ("task_id", "agent_id", "kind"))
    summary["result"] = None if result is None else _fields(result, ("status", "error_code"))
    if result is not None and isinstance(result.get("receipt"), dict):
        summary["result"]["receipt"] = _fields(result["receipt"], ("status",))
    return summary


def _build(*, state, lifecycle, termination_phase, view_seq, facts, projection, tasks,
           executions, questions, evidence, timers, decisions, changes, manifest,
           receipts, event_receipts):
    latest = {}
    for question in questions.values():
        if question["revision_ids"]:
            identifier = question["revision_ids"][-1]
            item = evidence[identifier]
            latest[identifier] = {"request_id": item["request_id"], "revision": item["revision"],
                "evidence_id": item["evidence_id"], "snapshot_id": item["snapshot"]["snapshot_id"]}
    return {"query_schema": PROGRESS_SCHEMA, "ready": True, "state": state,
        "lifecycle": lifecycle, "termination_phase": termination_phase, "view_seq": view_seq,
        "run_manifest": _fields(manifest, ("identity", "engine_run_id", "run_pin")),
        "facts": {"facts": {name: _cell(cell) for name, cell in facts.items()}},
        "projection": {"milestones": {name: {"cell": _cell(value["cell"])}
            for name, value in (projection or {}).get("milestones", {}).items()}},
        "tasks": {name: _fields(value, ("task_definition_id",)) for name, value in tasks.items()},
        "executions": {name: _execution(value) for name, value in executions.items()},
        "hitl": {"requests": copy.deepcopy(questions), "evidence": latest},
        "timers": {name: _fields(value, ("payload", "status", "anchor_at")) for name, value in timers.items()},
        "decisions": {name: {"record": _fields(value["record"], ("decision", "decision_definition_id", "request_id"))}
                      for name, value in decisions.items()},
        "changes": [copy.deepcopy(value) for value in changes if value["kind"] == "STATE_ENTERED"],
        "ingress_receipts": copy.deepcopy(receipts), "event_receipts": copy.deepcopy(event_receipts)}


def progress_view(engine, *, run_manifest, receipts=()):
    """Project owned state without full-history properties or hash validation."""
    if engine is None:
        return {"query_schema": PROGRESS_SCHEMA, "ready": False, "state": None}
    event_ids = {row["event_id"] for row in receipts if row.get("kind") == "event" and row.get("event_id")}
    return _build(state=engine._projection["state"] if engine._projection else None,
        lifecycle=engine._lifecycle, termination_phase=engine._termination_phase, view_seq=engine._view_seq,
        facts=engine.ledger._facts, projection=engine._projection, tasks=engine.tasks._tasks,
        executions=engine.tasks._executions, questions=engine.hitl._questions, evidence=engine.hitl._evidence,
        timers=engine.timers._records, decisions=engine.ledger._decisions, changes=engine._changes,
        manifest=run_manifest, receipts=receipts,
        event_receipts={key: engine.ledger.event_receipt(key) for key in event_ids})


def progress_from_snapshot(snapshot):
    """Project a single final/closed transport snapshot outside Workflow I/O."""
    if not snapshot.get("ready", True) or "termination_phase" not in snapshot:
        return {"query_schema": PROGRESS_SCHEMA, "ready": False, "state": snapshot.get("state")}
    return _build(state=snapshot["state"], lifecycle=snapshot["lifecycle"],
        termination_phase=snapshot["termination_phase"], view_seq=snapshot["view_seq"],
        facts=snapshot.get("facts", {}).get("facts", {}), projection=snapshot.get("projection"),
        tasks=snapshot["tasks"], executions=snapshot["executions"], questions=snapshot["hitl"]["requests"],
        evidence=snapshot["hitl"]["evidence"], timers=snapshot["timers"], decisions=snapshot["decisions"],
        changes=snapshot["changes"], manifest=snapshot.get("run_manifest", {}),
        receipts=snapshot.get("ingress_receipts", []), event_receipts=snapshot.get("event_receipts", {}))


def hitl_view(engine, request_id, revision=None):
    """One exact immutable evidence revision and its referenced sidecars."""
    if engine is None:
        raise ConfigError("HITL_VIEW_NOT_FOUND", "hitl_view.request_id", "Unknown HITL request")
    return _hitl_view(engine.hitl._questions, engine.hitl._evidence, engine.tasks._executions,
                      engine.tasks._get_verification, request_id, revision)


def hitl_from_snapshot(snapshot, request_id, revision=None):
    """Select from a final diagnostic snapshot without returning its history."""
    return _hitl_view(snapshot.get("hitl", {}).get("requests", {}),
        snapshot.get("hitl", {}).get("evidence", {}), snapshot.get("executions", {}),
        lambda key: copy.deepcopy(snapshot.get("verifications", {})[key]), request_id, revision)


def _hitl_view(questions, evidence_records, executions, verification, request_id, revision):
    if not isinstance(request_id, str) or request_id not in questions:
        raise ConfigError("HITL_VIEW_NOT_FOUND", "hitl_view.request_id", "Unknown HITL request")
    question = questions[request_id]
    selected = question["current_revision"] if revision is None else revision
    if type(selected) is not int or not 1 <= selected <= len(question["revision_ids"]):
        raise ConfigError("HITL_VIEW_NOT_FOUND", "hitl_view.revision", "Unknown evidence revision")
    identifier = question["revision_ids"][selected - 1]
    evidence = evidence_records[identifier]
    result_ids = set(evidence["required_result_ids"]) | set(evidence["reference_result_ids"])
    return {"query_schema": HITL_VIEW_SCHEMA,
        "hitl": {"requests": {request_id: copy.deepcopy(question)}, "evidence": {identifier: copy.deepcopy(evidence)}},
        "executions": {key: {"result": copy.deepcopy(executions[key]["result"])}
                       for key in result_ids if key in executions},
        "verifications": {key: verification(key) for key in evidence["verification_ids"]}}
