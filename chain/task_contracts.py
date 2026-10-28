"""Pure v1 Agent execution contracts and semantic input commitments.

These validators do not authorize a task, load an Agent, accept its result into
history, or decide whether an otherwise valid completion is current. Those are
separate task-owner/Worker responsibilities. Provider output Schema validation
also remains separate from the public declared-field check below.
"""
from __future__ import annotations

import copy

from .compiler import canonical_hash
from .errors import ConfigError
from .runtime_contracts import (canonical_source_refs, timestamp,
                                validate_identity, validate_scope,
                                validate_snapshot, _hash, _integer,
                                _shape, _text)
from .shape import check_json

REQUEST_SCHEMA = "chain-fact-agent-request/v1"
RESULT_SCHEMA = "chain-fact-agent-result/v1"
_COMMON = {"contract_schema", "identity", "engine_run_id", "run_pin"}
_EXECUTION = {"task_id", "execution_id", "request_id", "input_revision",
              "predecessor_execution_id", "agent_id", "agent_version",
              "implementation_hash", "mode"}
_REQUEST = _COMMON | _EXECUTION | {"snapshot", "input_fingerprint", "scope",
    "dependencies", "prerequisite_result_ids", "evaluated_at", "verification_ids"}
_RESULT = _COMMON | _EXECUTION | {"input_fingerprint", "input_snapshot_id",
    "status", "result", "evidence", "produced_at"}
_WORK_EXECUTION = _EXECUTION - {"agent_id", "agent_version", "implementation_hash", "mode"}
_WORK_REQUEST = (_REQUEST - _EXECUTION) | _WORK_EXECUTION | {"task_definition_id", "kind", "task"}
_WORK_RESULT = (_RESULT - _EXECUTION - {"result", "evidence"}) | _WORK_EXECUTION | {"receipt"}


def _fail(code, path, message):
    raise ConfigError(code, path, message) from None


def _strings(values, path, *, hashes=False):
    if not isinstance(values, list):
        _fail("CONTRACT_SCHEMA_INVALID", path, "Expected a canonical string list")
    for value in values:
        (_hash if hashes else _text)(value, path)
    if values != sorted(values) or len(values) != len(set(values)):
        _fail("CONTRACT_SCHEMA_INVALID", path, "Expected sorted unique references")


def _identity(value):
    validate_identity(value["identity"])
    for key in ("engine_run_id", "task_id", "execution_id", "request_id",
                "agent_id", "agent_version", "mode"):
        _text(value[key], key)
    for key in ("run_pin", "implementation_hash", "input_fingerprint"):
        _hash(value[key], key)
    _integer(value["input_revision"], "input_revision")
    if value["predecessor_execution_id"] is not None:
        _text(value["predecessor_execution_id"], "predecessor_execution_id")
        if value["predecessor_execution_id"] == value["execution_id"]:
            _fail("REQUEST_EXECUTION_MISMATCH", "predecessor_execution_id", "An execution cannot precede itself")
    if value["request_id"] != value["execution_id"]:
        _fail("REQUEST_EXECUTION_MISMATCH", "request_id", "Request and execution IDs must match")


def _refs(values, path):
    refs = canonical_source_refs(values)
    if refs != values:
        _fail("NONCANONICAL_SOURCE_REFS", path, "Expected canonical source references")
    return refs


def _snapshot_refs(snapshot):
    refs = {}
    for fact in snapshot["facts"].values():
        for ref in fact["source_refs"]:
            refs[canonical_hash(ref)] = ref
    return canonical_source_refs(list(refs.values()))


def semantic_input(snapshot):
    """Return scoped value/validity/provenance, excluding observation metadata.

    The input must still be a valid immutable Snapshot. Different Snapshot IDs
    can have the same semantic input when only source/knowledge times differ;
    a source version, value, validity, unit or confirmation change is material.
    """
    snapshot = validate_snapshot(snapshot)
    fields = {"value", "validity", "source_refs", "unit", "confirmation_status"}
    return {name: {key: copy.deepcopy(value) for key, value in fact.items() if key in fields}
            for name, fact in sorted(snapshot["facts"].items())}


def input_fingerprint(request):
    """Commit to clinical inputs and implementation, never request IDs/time.

    This helper may run while constructing a request, before its fingerprint
    field exists. Full wire validation is performed by validate_agent_request.
    """
    check_json(request, path="request")
    if not isinstance(request, dict):
        _fail("CONTRACT_SCHEMA_INVALID", "request", "Expected an Agent request")
    fields = {"contract_schema", "snapshot", "scope", "prerequisite_result_ids",
              "agent_id", "agent_version", "implementation_hash", "mode"}
    if not fields <= set(request):
        _fail("CONTRACT_SCHEMA_INVALID", "request", "Missing input commitment fields")
    if request["contract_schema"] != REQUEST_SCHEMA:
        _fail("UNSUPPORTED_CONTRACT", "contract_schema", "Unsupported Agent request contract")
    scope = validate_scope(request["scope"])
    _strings(request["prerequisite_result_ids"], "prerequisite_result_ids")
    for field in ("agent_id", "agent_version", "mode"):
        _text(request[field], field)
    _hash(request["implementation_hash"], "implementation_hash")
    facts = semantic_input(request["snapshot"])
    if set(scope) != set(facts):
        _fail("CONTRACT_SCOPE_INVALID", "scope", "Snapshot fields must equal requested scope")
    return canonical_hash({"contract_schema": request["contract_schema"],
        "facts": facts, "scope": scope,
        "prerequisite_result_ids": request["prerequisite_result_ids"],
        **{field: request[field] for field in
           ("agent_id", "agent_version", "implementation_hash", "mode")}})


def validate_agent_request(value, *, module=None):
    """Check a closed, scoped execution request against optional public metadata."""
    check_json(value, path="request")
    _shape(value, _REQUEST, "request")
    if value["contract_schema"] != REQUEST_SCHEMA:
        _fail("UNSUPPORTED_CONTRACT", "contract_schema", "Unsupported Agent request contract")
    _identity(value)
    snapshot = validate_snapshot(value["snapshot"])
    for key in ("identity", "engine_run_id", "run_pin"):
        if value[key] != snapshot[key]:
            _fail("IDENTITY_MISMATCH", "snapshot." + key, "Snapshot belongs to another pinned Episode run")
    scope = validate_scope(value["scope"])
    if value["scope"] != scope or set(scope) != set(snapshot["facts"]):
        _fail("CONTRACT_SCOPE_INVALID", "scope", "Expected sorted scope equal to Snapshot fields")
    if timestamp(value["evaluated_at"], path="evaluated_at") < timestamp(snapshot["known_at"]):
        _fail("FUTURE_INPUT", "evaluated_at", "Input was not available at evaluation time")
    dependencies = _refs(value["dependencies"], "dependencies")
    if dependencies != _snapshot_refs(snapshot):
        _fail("DEPENDENCY_SCOPE_MISMATCH", "dependencies", "Dependencies must equal the Snapshot source union")
    _strings(value["prerequisite_result_ids"], "prerequisite_result_ids")
    if value["execution_id"] in value["prerequisite_result_ids"]:
        _fail("DEPENDENCY_CYCLE", "prerequisite_result_ids", "An execution cannot depend on itself")
    _strings(value["verification_ids"], "verification_ids", hashes=True)
    if value["input_fingerprint"] != input_fingerprint(value):
        _fail("INPUT_FINGERPRINT_MISMATCH", "input_fingerprint", "Input differs from its semantic commitment")
    if module is not None:
        check_json(module, path="module")
        if (not isinstance(module, dict) or not isinstance(module.get("modes"), dict)
                or module.get("version") != value["agent_version"]
                or module.get("implementation_hash") != value["implementation_hash"]
                or value["mode"] not in module["modes"]):
            _fail("AGENT_IMPLEMENTATION_MISMATCH", "module", "Request differs from public installed implementation metadata")
    return copy.deepcopy(value)


def validate_agent_result(value, request, *, output_fields=None):
    """Validate completion identity/evidence; do not classify its current use."""
    request = validate_agent_request(request)
    check_json(value, path="result")
    _shape(value, _RESULT, "result", {"error_code"})
    if value["contract_schema"] != RESULT_SCHEMA:
        _fail("UNSUPPORTED_CONTRACT", "contract_schema", "Unsupported Agent result contract")
    _identity(value)
    for key in (_COMMON - {"contract_schema"}) | _EXECUTION | {"input_fingerprint"}:
        if value[key] != request[key]:
            _fail("RESULT_CONTRACT_MISMATCH", key, "Completion differs from its issued execution request")
    _hash(value["input_snapshot_id"], "input_snapshot_id")
    if value["input_snapshot_id"] != request["snapshot"]["snapshot_id"]:
        _fail("RESULT_CONTRACT_MISMATCH", "input_snapshot_id", "Completion used another input Snapshot")
    if timestamp(value["produced_at"], path="produced_at") < timestamp(request["evaluated_at"]):
        _fail("TIMESTAMP_INVALID", "produced_at", "Completion precedes its issued request")
    evidence = _refs(value["evidence"], "evidence")
    allowed = {canonical_hash(ref) for ref in request["dependencies"]}
    if any(canonical_hash(ref) not in allowed for ref in evidence):
        _fail("DEPENDENCY_SCOPE_MISMATCH", "evidence", "Evidence exceeds the issued input scope")
    if value["status"] == "SUCCESS":
        if not isinstance(value["result"], dict) or "error_code" in value:
            _fail("AGENT_OUTPUT_INVALID", "result", "Successful completion requires an object without error_code")
        if output_fields is not None:
            check_json(output_fields, path="output_fields")
            if not isinstance(output_fields, list) or any(not isinstance(item, str) or not item for item in output_fields):
                _fail("RESULT_CONTRACT_MISMATCH", "output_fields", "Expected declared output field names")
            if len(set(output_fields)) != len(output_fields) or set(value["result"]) != set(output_fields):
                _fail("AGENT_OUTPUT_INVALID", "result", "Result fields differ from the declared mode contract")
    elif value["status"] == "FAILED":
        if value["result"] is not None or "error_code" not in value:
            _fail("AGENT_OUTPUT_INVALID", "result", "Failure requires null result and an explicit error code")
        _text(value["error_code"], "error_code")
    else:
        _fail("AGENT_OUTPUT_INVALID", "status", "Expected explicit SUCCESS or FAILED")
    return copy.deepcopy(value)


def _work_identity(value):
    validate_identity(value["identity"])
    for key in ("engine_run_id", "task_id", "execution_id", "request_id"):
        _text(value[key], key)
    _hash(value["run_pin"], "run_pin")
    _hash(value["input_fingerprint"], "input_fingerprint")
    _integer(value["input_revision"], "input_revision")
    if value["request_id"] != value["execution_id"]:
        _fail("REQUEST_EXECUTION_MISMATCH", "request_id", "Request and execution IDs must match")
    if value["predecessor_execution_id"] is not None:
        _text(value["predecessor_execution_id"], "predecessor_execution_id")
        if value["predecessor_execution_id"] == value["execution_id"]:
            _fail("REQUEST_EXECUTION_MISMATCH", "predecessor_execution_id", "An execution cannot precede itself")


def _task_declaration(task, kind):
    fields = {"hitl": {"decision"}, "notify": {"template", "recipient", "context"},
              "effect": {"operation", "resource", "target", "context"}}
    if not isinstance(kind, str) or kind not in fields:
        _fail("TASK_KIND_UNSUPPORTED", "kind", "Only declared HITL, notification and display work is supported")
    base = {"kind", "identity", "after", "completion_effect", "origin_refs"}
    _shape(task, base | fields[kind], "task")
    if task["kind"] != kind:
        _fail("TASK_KIND_MISMATCH", "task.kind", "Work kind differs from its task declaration")
    for field in fields[kind]:
        _text(task[field], "task." + field)
    if kind == "effect" and task["operation"] not in {"SET_FLAG", "CLEAR_FLAG", "SET_DASHBOARD", "CLEAR_DASHBOARD"}:
        _fail("TASK_OPERATION_UNSUPPORTED", "task.operation", "Unsupported display operation")
    if type(task["completion_effect"]) is not bool or kind == "hitl" and task["completion_effect"]:
        _fail("CONTRACT_SCHEMA_INVALID", "task.completion_effect", "Invalid completion-effect declaration")
    for key in ("after", "origin_refs"):
        values = task[key]
        if not isinstance(values, list):
            _fail("CONTRACT_SCHEMA_INVALID", "task." + key, "Expected unique references")
        for value in values:
            _text(value, "task." + key)
        if len(values) != len(set(values)) or key == "origin_refs" and not values:
            _fail("CONTRACT_SCHEMA_INVALID", "task." + key, "Expected unique nonempty origin references")
    identity = task["identity"]
    if (not isinstance(identity, dict) or not isinstance(identity.get("subject"), str)
            or identity["subject"] not in {"episode", "cause"}):
        _fail("CONTRACT_SCHEMA_INVALID", "task.identity", "Expected episode or explicit cause identity")
    _shape(identity, {"subject", "repeat"} | ({"subject_ref"} if identity["subject"] == "cause" else set()), "task.identity")
    if not isinstance(identity["repeat"], str) or identity["repeat"] not in {"once", "input_change", "condition_reentry", "cause_change"}:
        _fail("CONTRACT_SCHEMA_INVALID", "task.identity.repeat", "Unsupported repeat rule")
    if identity["subject"] == "cause":
        ref = identity["subject_ref"]
        _shape(ref, {"kind", "namespace", "name", "path"}, "task.identity.subject_ref")
        _text(ref["name"], "task.identity.subject_ref.name")
        if ref["kind"] != "ref" or not isinstance(ref["path"], list):
            _fail("CONTRACT_SCHEMA_INVALID", "task.identity.subject_ref", "Expected a compiled logical reference")
        for part in ref["path"]:
            _text(part, "task.identity.subject_ref.path")
        ns, path = ref["namespace"], ref["path"]
        valid = (ns in ("facts", "milestones") and not path
                 or ns == "results" and len(path) == 1
                 or ns == "decisions" and path == ["value"]
                 or ns == "state_entries" and path in (["entered"], ["generation"]))
        if not valid:
            _fail("CONTRACT_SCHEMA_INVALID", "task.identity.subject_ref", "Unsupported logical target reference")


def work_input_fingerprint(request):
    """Commit non-Agent work to its declaration and scoped semantic input."""
    check_json(request, path="request")
    required = {"contract_schema", "snapshot", "task", "kind", "scope", "prerequisite_result_ids"}
    if not isinstance(request, dict) or not required <= set(request):
        _fail("CONTRACT_SCHEMA_INVALID", "request", "Missing work commitment fields")
    if request["contract_schema"] != "chain-fact-work-request/v1":
        _fail("UNSUPPORTED_CONTRACT", "contract_schema", "Unsupported work request contract")
    _task_declaration(request["task"], request["kind"])
    scope = validate_scope(request["scope"])
    _strings(request["prerequisite_result_ids"], "prerequisite_result_ids")
    facts = semantic_input(request["snapshot"])
    if set(scope) != set(facts):
        _fail("CONTRACT_SCOPE_INVALID", "scope", "Snapshot fields must equal requested scope")
    return canonical_hash({"contract_schema": request["contract_schema"], "facts": facts,
        "task": request["task"], "scope": scope, "prerequisite_result_ids": request["prerequisite_result_ids"]})


def validate_work_request(value):
    """Validate prepared work; HITL opening/response acceptance is phase 6."""
    check_json(value, path="request")
    _shape(value, _WORK_REQUEST, "request", {"effect_version"})
    if value["contract_schema"] != "chain-fact-work-request/v1":
        _fail("UNSUPPORTED_CONTRACT", "contract_schema", "Unsupported work request contract")
    _work_identity(value)
    _text(value["task_definition_id"], "task_definition_id")
    _task_declaration(value["task"], value["kind"])
    if value["kind"] == "effect":
        if "effect_version" not in value:
            _fail("CONTRACT_SCHEMA_INVALID", "effect_version", "Display work requires an issued ordering version")
        _integer(value["effect_version"], "effect_version")
    elif "effect_version" in value:
        _fail("CONTRACT_SCHEMA_INVALID", "effect_version", "Ordering version applies only to display effects")
    snapshot = validate_snapshot(value["snapshot"])
    for key in ("identity", "engine_run_id", "run_pin"):
        if value[key] != snapshot[key]:
            _fail("IDENTITY_MISMATCH", "snapshot." + key, "Snapshot belongs to another pinned Episode run")
    scope = validate_scope(value["scope"])
    if value["scope"] != scope or set(scope) != set(snapshot["facts"]):
        _fail("CONTRACT_SCOPE_INVALID", "scope", "Expected sorted scope equal to Snapshot fields")
    if timestamp(value["evaluated_at"], path="evaluated_at") < timestamp(snapshot["known_at"]):
        _fail("FUTURE_INPUT", "evaluated_at", "Input was not available at evaluation time")
    dependencies = _refs(value["dependencies"], "dependencies")
    if dependencies != _snapshot_refs(snapshot):
        _fail("DEPENDENCY_SCOPE_MISMATCH", "dependencies", "Dependencies must equal the Snapshot source union")
    _strings(value["prerequisite_result_ids"], "prerequisite_result_ids")
    if value["execution_id"] in value["prerequisite_result_ids"]:
        _fail("DEPENDENCY_CYCLE", "prerequisite_result_ids", "An execution cannot depend on itself")
    _strings(value["verification_ids"], "verification_ids", hashes=True)
    if value["input_fingerprint"] != work_input_fingerprint(value):
        _fail("INPUT_FINGERPRINT_MISMATCH", "input_fingerprint", "Work differs from its semantic commitment")
    return copy.deepcopy(value)


def validate_work_result(value, request):
    """Check a notification/display completion envelope, not its API receipt."""
    request = validate_work_request(request)
    if request["kind"] == "hitl":
        _fail("HITL_EXECUTION_NOT_IMPLEMENTED", "kind", "HITL lifecycle and decision acceptance are phase 6")
    check_json(value, path="result")
    _shape(value, _WORK_RESULT, "result", {"error_code"})
    if value["contract_schema"] != "chain-fact-work-result/v1":
        _fail("UNSUPPORTED_CONTRACT", "contract_schema", "Unsupported work result contract")
    _work_identity(value)
    for key in (_COMMON - {"contract_schema"}) | _WORK_EXECUTION | {"input_fingerprint"}:
        if value[key] != request[key]:
            _fail("RESULT_CONTRACT_MISMATCH", key, "Completion differs from its issued work request")
    _hash(value["input_snapshot_id"], "input_snapshot_id")
    if value["input_snapshot_id"] != request["snapshot"]["snapshot_id"]:
        _fail("RESULT_CONTRACT_MISMATCH", "input_snapshot_id", "Completion used another input Snapshot")
    if timestamp(value["produced_at"], path="produced_at") < timestamp(request["evaluated_at"]):
        _fail("TIMESTAMP_INVALID", "produced_at", "Completion precedes its issued request")
    if value["status"] == "SUCCESS":
        if not isinstance(value["receipt"], dict) or "error_code" in value:
            _fail("WORK_OUTPUT_INVALID", "receipt", "Successful work requires a receipt object without error_code")
    elif value["status"] == "FAILED":
        if value["receipt"] is not None or "error_code" not in value:
            _fail("WORK_OUTPUT_INVALID", "receipt", "Failure requires null receipt and an explicit error code")
        _text(value["error_code"], "error_code")
    else:
        _fail("WORK_OUTPUT_INVALID", "status", "Expected explicit SUCCESS or FAILED")
    return copy.deepcopy(value)
