"""Pure phase-3 wire contracts; no source access, clinical predicates or I/O.

Validators return detached JSON copies. They reject unknown fields and never
repair identities, versions, hashes or clinical values. Scope lists and request
reference sets are canonicalized. Snapshots instead require already canonical
source references because changing a hashed Snapshot would change its identity.

``normalize_completion`` only projects a verified v0.3 resolution into v1 facts;
the caller owns Event/completion matching, declared source coverage, revision
lineage and history. source_definition is exactly {system, status_mapping?}.
status_mapping is independently supplied Worker normalization approval, not an
authorization automatically issued by Clinical YAML. No clinical source priority
or string/bool conversion is inferred. Raw source documents are handled by the
caller's separate exact-version document history, never inserted as extra facts.
"""
from __future__ import annotations

import copy
from datetime import datetime
import math
import re

from .common.source_contracts import validate_completion as _validate_v03_completion

from .compiler import canonical_hash
from .errors import ConfigError
from .shape import check_json


IDENTITY_FIELDS = {"site_id", "patient_id", "encounter_id", "episode_id"}
INITIAL_FIELDS = IDENTITY_FIELDS | {"age", "sex", "bed", "arrival_time"}
COMMON_FIELDS = {"contract_schema", "request_id", "identity", "engine_run_id", "run_pin"}
VALIDITIES = {"KNOWN", "ABSENT_CONFIRMED", "UNKNOWN", "PENDING", "CONFLICT", "ERROR", "RETRACTED", "INVALIDATED"}
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TYPES = {"string", "number", "integer", "boolean", "object"}
_RESERVED = {k: k for k in ("UNKNOWN", "PENDING", "CONFLICT", "ERROR", "RETRACTED", "INVALIDATED")}
_DEFAULT_MAP = {"AVAILABLE": "KNOWN", "COMPLETED": "KNOWN", **_RESERVED}


def _fail(code, path, message):
    raise ConfigError(code, path, message) from None


def _shape(value, required, path, optional=()):
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        _fail("CONTRACT_SCHEMA_INVALID", path, "Expected an object with string keys")
    if set(value) - set(required) - set(optional) or set(required) - set(value):
        _fail("CONTRACT_SCHEMA_INVALID", path, "Missing or unsupported contract fields")


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        _fail("CONTRACT_SCHEMA_INVALID", path, "Expected a nonempty string")
    return value


def _integer(value, path, minimum=1):
    if type(value) is not int or value < minimum:
        _fail("CONTRACT_SCHEMA_INVALID", path, "Expected an integer within the allowed range")
    return value


def _hash(value, path):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _fail("CONTRACT_HASH_INVALID", path, "Expected a SHA-256 content hash")
    return value


def _enum(value, allowed, path, *, code="CONTRACT_SCHEMA_INVALID"):
    if not isinstance(value, str) or value not in allowed:
        _fail(code, path, "Unsupported contract value")
    return value


def timestamp(value, *, path="timestamp"):
    """Return an aware datetime; retain the original string in wire objects."""
    _text(value, path)
    try:
        result = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        _fail("TIMESTAMP_INVALID", path, "Expected an ISO 8601 timestamp")
    if result.utcoffset() is None:
        _fail("TIMESTAMP_INVALID", path, "Timestamp requires a timezone")
    return result


def validate_identity(value):
    check_json(value, path="identity")
    _shape(value, IDENTITY_FIELDS, "identity")
    for key in sorted(IDENTITY_FIELDS):
        _text(value[key], "identity." + key)
    return copy.deepcopy(value)


def validate_initial(value):
    check_json(value, path="initial")
    _shape(value, INITIAL_FIELDS, "initial")
    validate_identity({key: value[key] for key in IDENTITY_FIELDS})
    _integer(value["age"], "initial.age", minimum=0)
    for key in ("sex", "bed"):
        _text(value[key], "initial." + key)
    timestamp(value["arrival_time"], path="initial.arrival_time")
    return copy.deepcopy(value)


def source_ref(value, *, field=False):
    check_json(value, path="source_ref")
    _shape(value, {"system", "record_id", "version"} | ({"field"} if field else set()), "source_ref")
    for key in ("system", "record_id"):
        _text(value[key], "source_ref." + key)
    _integer(value["version"], "source_ref.version")
    if field:
        _text(value["field"], "source_ref.field")
    return copy.deepcopy(value)


def canonical_source_refs(value, *, field=True):
    check_json(value, path="source_refs")
    if not isinstance(value, list):
        _fail("CONTRACT_SCHEMA_INVALID", "source_refs", "Expected a source reference list")
    result = [source_ref(ref, field=field) for ref in value]
    keys = [(r["system"], r["record_id"], r["version"], r.get("field", "")) for r in result]
    if len(keys) != len(set(keys)):
        _fail("DUPLICATE_SOURCE_REF", "source_refs", "Duplicate source reference")
    return [result[index] for index in sorted(range(len(result)), key=keys.__getitem__)]


def validate_scope(value, *, path="scope", allowed=None):
    check_json(value, path=path)
    if not isinstance(value, list) or not value:
        _fail("CONTRACT_SCOPE_INVALID", path, "Expected a nonempty scope list")
    for item in value:
        _text(item, path)
    if len(value) != len(set(value)):
        _fail("CONTRACT_SCOPE_INVALID", path, "Duplicate scope member")
    if allowed is not None and not set(value) <= set(allowed):
        _fail("CONTRACT_SCOPE_INVALID", path, "Scope exceeds the permitted fields")
    return sorted(value)


def _type_valid(value, kind):
    return {
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float) and (type(value) is int or math.isfinite(value)),
        "boolean": type(value) is bool,
        "object": isinstance(value, dict),
    }.get(kind, False)


def source_validity(source_status, source_definition):
    """Resolve a source status using independently approved normalization only.

    This shared pure guard applies both to raw v0.3 normalization and compact
    v1 replay/Activity results. It does not choose a clinical source priority.
    """
    check_json({"source_status": source_status, "source_definition": source_definition})
    _text(source_status, "source_status")
    _shape(source_definition, {"system"}, "source_definition", {"status_mapping"})
    _text(source_definition["system"], "source_definition.system")
    mapping = dict(_DEFAULT_MAP)
    if "status_mapping" in source_definition:
        supplied = source_definition["status_mapping"]
        if not isinstance(supplied, dict):
            _fail("SOURCE_MAPPING_INVALID", "source_definition.status_mapping", "Expected an approved status mapping")
        for status, validity in supplied.items():
            _text(status, "source_definition.status_mapping")
            if not isinstance(validity, str) or validity not in VALIDITIES or status in _RESERVED and validity != _RESERVED[status]:
                _fail("SOURCE_MAPPING_INVALID", "source_definition.status_mapping", "Mapping changes a reserved or unsupported validity")
            mapping[status] = validity
    return mapping.get(source_status, "UNKNOWN")


def normalize_completion(value, *, fact_types, source_id, source_definition, applied_at):
    """Return {declared_name: v1Fact}; preserve the original completion separately.

    The only stripped undeclared field is document:<this source record_id>.
    Other undeclared fields, including another document ID, are rejected. Known
    values must have the declared type; nonusable source states project to null.
    """
    check_json(value, path="completion")
    check_json({"fact_types": fact_types, "source_definition": source_definition})
    _text(source_id, "source_id")
    # Validate even an empty completion's supplied normalization contract.
    source_validity("UNKNOWN", source_definition)
    if not isinstance(fact_types, dict) or any(not isinstance(kind, str) or kind not in _TYPES for kind in fact_types.values()):
        _fail("FACT_TYPE_INVALID", "fact_types", "Expected declared supported fact types")
    for name in fact_types:
        _text(name, "fact_types")
    try:
        _validate_v03_completion(value)
    except (ValueError, KeyError, TypeError, UnicodeError):
        _fail("COMPLETION_INVALID", "completion", "Invalid exact-version v0.3 source completion")
    applied = timestamp(applied_at, path="applied_at")
    if applied < timestamp(value["received_at"]):
        _fail("TIMESTAMP_INVALID", "applied_at", "Application precedes source receipt")
    if value["source_ref"]["system"] != source_definition["system"]:
        _fail("SOURCE_IDENTITY_MISMATCH", "completion.source_ref", "Source system differs from the declared source")
    result = {}
    document_name = "document:" + value["source_ref"]["record_id"]
    for name, fact in sorted(value["facts"].items()):
        if name not in fact_types:
            if name == document_name:
                continue
            _fail("UNDECLARED_FACT", "completion.facts", "Source completion contains an undeclared fact")
        validity = source_validity(fact["status"], source_definition)
        content = copy.deepcopy(fact["value"]) if validity == "KNOWN" else None
        if validity == "KNOWN" and not _type_valid(content, fact_types[name]):
            _fail("FACT_TYPE_MISMATCH", "completion.facts." + name, "Known source value differs from its declared type")
        # v0.3 resolved dependencies may already include the source's own ref.
        refs = {canonical_hash(ref): copy.deepcopy(ref)
                for ref in [fact["source_ref"], *fact.get("dependencies", [])]}
        normalized = {"value": content, "validity": validity, "source_status": fact["status"],
                      "source_refs": canonical_source_refs(list(refs.values())),
                      "source_time": fact["source_time"], "known_at": applied_at}
        normalized.update({key: fact[key] for key in ("unit", "confirmation_status") if key in fact})
        result[name] = normalized
    return copy.deepcopy(result)


def validate_snapshot(value):
    check_json(value, path="snapshot")
    _shape(value, {"contract_schema", "identity", "engine_run_id", "run_pin", "snapshot_id", "known_at", "facts"}, "snapshot")
    if value["contract_schema"] != "chain-fact-snapshot/v1":
        _fail("UNSUPPORTED_CONTRACT", "snapshot.contract_schema", "Unsupported Snapshot contract")
    validate_identity(value["identity"])
    _text(value["engine_run_id"], "snapshot.engine_run_id")
    _hash(value["run_pin"], "snapshot.run_pin")
    known = timestamp(value["known_at"], path="snapshot.known_at")
    if not isinstance(value["facts"], dict):
        _fail("CONTRACT_SCHEMA_INVALID", "snapshot.facts", "Expected fact objects")
    for name, fact in sorted(value["facts"].items()):
        path = "snapshot.facts." + _text(name, "snapshot.facts")
        _shape(fact, {"value", "validity", "source_status", "source_refs", "source_time", "known_at"},
               path, {"unit", "confirmation_status"})
        _enum(fact["validity"], VALIDITIES, path + ".validity", code="FACT_VALIDITY_INVALID")
        if (fact["validity"] == "KNOWN") == (fact["value"] is None):
            _fail("FACT_VALUE_INVALID", path, "Known facts need a value; unavailable facts must be null")
        if fact["validity"] == "KNOWN" and type(fact["value"]) not in (str, int, float, bool, dict):
            _fail("FACT_TYPE_MISMATCH", path, "Known facts require a supported declared value type")
        _text(fact["source_status"], path + ".source_status")
        refs = canonical_source_refs(fact["source_refs"])
        if refs != fact["source_refs"]:
            _fail("NONCANONICAL_SOURCE_REFS", path + ".source_refs", "Snapshot source references must already be canonical")
        if not refs and not (fact["validity"] == "UNKNOWN" and fact["source_status"] == "NOT_RECEIVED"):
            _fail("FACT_PROVENANCE_MISSING", path + ".source_refs", "Only explicitly unreceived facts can omit provenance")
        if not timestamp(fact["source_time"], path=path + ".source_time") <= timestamp(
                fact["known_at"], path=path + ".known_at") <= known:
            _fail("TIMESTAMP_INVALID", path, "Fact source/known times exceed Snapshot availability")
        for key in ("unit", "confirmation_status"):
            if key in fact:
                _text(fact[key], path + "." + key)
    _hash(value["snapshot_id"], "snapshot.snapshot_id")
    if value["snapshot_id"] != canonical_hash({key: item for key, item in value.items() if key != "snapshot_id"}):
        _fail("SNAPSHOT_HASH_MISMATCH", "snapshot.snapshot_id", "Snapshot content differs from its hash")
    return copy.deepcopy(value)


def _common(value, schema, extra):
    check_json(value)
    _shape(value, COMMON_FIELDS | set(extra), schema)
    if value["contract_schema"] != schema:
        _fail("UNSUPPORTED_CONTRACT", "contract_schema", "Unsupported Request/Result contract")
    validate_identity(value["identity"])
    _text(value["request_id"], "request_id")
    _text(value["engine_run_id"], "engine_run_id")
    _hash(value["run_pin"], "run_pin")


def _limits(value):
    _shape(value, {"timeout_ms", "max_attempts", "max_pages", "max_preparation_rounds"}, "limits")
    for key in sorted(value):
        _integer(value[key], "limits." + key)


def _errors(value, allowed=None):
    if not isinstance(value, list):
        _fail("CONTRACT_SCHEMA_INVALID", "errors", "Expected structured source errors")
    scopes = set()
    result = []
    for item in value:
        _shape(item, {"scope", "code", "retryable"}, "errors")
        scope = validate_scope(item["scope"], path="errors.scope", allowed=allowed)
        _text(item["code"], "errors.code")
        if type(item["retryable"]) is not bool:
            _fail("CONTRACT_SCHEMA_INVALID", "errors.retryable", "Expected a boolean retryability marker")
        scopes.update(scope)
        result.append({**copy.deepcopy(item), "scope": scope})
    return result, scopes


def _event_refs(value):
    if not isinstance(value, list):
        _fail("CONTRACT_SCHEMA_INVALID", "event_refs", "Expected discovered Event references")
    result, ids, versions = [], set(), {}
    for item in value:
        _shape(item, {"event_id", "source_ref", "content_hash"}, "event_refs")
        event_id = _text(item["event_id"], "event_refs.event_id")
        if event_id in ids:
            _fail("DUPLICATE_EVENT_REF", "event_refs", "Duplicate discovered Event identity")
        ids.add(event_id)
        ref = source_ref(item["source_ref"])
        digest = _hash(item["content_hash"], "event_refs.content_hash")
        key = (ref["system"], ref["record_id"], ref["version"])
        if key in versions and versions[key] != digest:
            _fail("SOURCE_VERSION_CONFLICT", "event_refs", "Conflicting content for one source version")
        versions[key] = digest
        result.append({"event_id": event_id, "source_ref": ref, "content_hash": digest})
    return sorted(result, key=lambda item: item["event_id"])


def _match(result, request, *, source=False):
    for key in ("request_id", "identity", "engine_run_id", "run_pin") + (("source",) if source else ()):
        if result[key] != request[key]:
            _fail("REQUEST_RESULT_MISMATCH", key, "Result differs from the issued request")


def validate_preflight_request(value):
    _common(value, "chain-fact-preflight-request/v1", {"preparation_id", "parallel_batch_id", "consumer_task_ids",
            "purpose", "source", "scope", "authorization_ref", "basis", "limits"})
    result = copy.deepcopy(value)
    for key in ("preparation_id", "parallel_batch_id", "source", "authorization_ref"):
        _text(value[key], key)
    result["scope"] = validate_scope(value["scope"])
    result["consumer_task_ids"] = validate_scope(value["consumer_task_ids"], path="consumer_task_ids")
    _enum(value["purpose"], {"AGENT_INPUT", "HITL_EVIDENCE", "DELAY_NOTICE", "EHR_EFFECT"},
          "purpose", code="CONTRACT_PURPOSE_INVALID")
    _shape(value["basis"], {"data_epoch", "known_versions", "source_cursor"}, "basis")
    _integer(value["basis"]["data_epoch"], "basis.data_epoch", minimum=0)
    result["basis"]["known_versions"] = canonical_source_refs(value["basis"]["known_versions"], field=False)
    _limits(value["limits"])
    return result


def validate_preflight_result(value, request=None):
    _common(value, "chain-fact-preflight-result/v1", {"status", "coverage", "event_refs", "source_basis", "checked_at", "errors"})
    checked = timestamp(value["checked_at"], path="checked_at")
    status = value["status"]
    _enum(status, {"NO_CHANGE", "CHANGES_FOUND", "PARTIAL", "FAILED"}, "status", code="CONTRACT_STATUS_INVALID")
    if not isinstance(value["coverage"], list) or not value["coverage"]:
        _fail("CONTRACT_SCOPE_INVALID", "coverage", "Expected complete scope coverage")
    covered, unconfirmed = set(), set()
    for entry in value["coverage"]:
        _shape(entry, {"field", "verification"}, "coverage")
        name = _text(entry["field"], "coverage.field")
        _enum(entry["verification"], {"VERIFIED", "UNCONFIRMED"}, "coverage.verification", code="CONTRACT_SCOPE_INVALID")
        if name in covered:
            _fail("CONTRACT_SCOPE_INVALID", "coverage", "Duplicate or invalid coverage member")
        covered.add(name)
        if entry["verification"] == "UNCONFIRMED":
            unconfirmed.add(name)
    errors, error_scope = _errors(value["errors"], unconfirmed)
    if error_scope != unconfirmed:
        _fail("CONTRACT_SCOPE_INVALID", "errors", "Errors must explain exactly the unconfirmed scope")
    refs = _event_refs(value["event_refs"])
    if ((status in {"NO_CHANGE", "CHANGES_FOUND"} and unconfirmed)
            or status == "NO_CHANGE" and refs
            or status == "CHANGES_FOUND" and not refs
            or status == "PARTIAL" and (not unconfirmed or unconfirmed == covered)
            or status == "FAILED" and unconfirmed != covered):
        _fail("CONTRACT_STATUS_INVALID", "status", "Verification status conflicts with coverage or discoveries")
    basis = value["source_basis"]
    _shape(basis, {"kind", "token", "observed_at"}, "source_basis")
    _enum(basis["kind"], {"VERSION_TOKEN", "CURSOR", "OBSERVATION"}, "source_basis.kind", code="SOURCE_BASIS_INVALID")
    if timestamp(basis["observed_at"], path="source_basis.observed_at") > checked:
        _fail("TIMESTAMP_INVALID", "source_basis", "Source observation follows verification completion")
    if status == "FAILED" or basis["kind"] == "OBSERVATION":
        if basis["token"] is not None:
            _fail("SOURCE_BASIS_INVALID", "source_basis.token", "Unverified or observational checks cannot claim a version token")
    else:
        _text(basis["token"], "source_basis.token")
    if request is not None:
        request = validate_preflight_request(request)
        _match(value, request)
        if covered != set(request["scope"]):
            _fail("CONTRACT_SCOPE_INVALID", "coverage", "Coverage differs from the issued scope")
    result = copy.deepcopy(value)
    result.update(coverage=sorted(result["coverage"], key=lambda item: item["field"]), event_refs=refs, errors=errors)
    return result


def validate_reconcile_request(value):
    _common(value, "chain-fact-reconcile-request/v1", {"source", "scope", "authorization_ref", "checkpoint", "limits"})
    _text(value["source"], "source")
    _text(value["authorization_ref"], "authorization_ref")
    result = copy.deepcopy(value)
    result["scope"] = validate_scope(value["scope"])
    _limits(value["limits"])
    return result


def validate_reconcile_result(value, request=None):
    _common(value, "chain-fact-reconcile-result/v1", {"source", "status", "from_checkpoint", "candidate_checkpoint", "event_refs", "errors"})
    _text(value["source"], "source")
    _enum(value["status"], {"COMPLETE", "PARTIAL", "FAILED"}, "status", code="CONTRACT_STATUS_INVALID")
    if request is not None:
        request = validate_reconcile_request(request)
        _match(value, request, source=True)
        if canonical_hash(value["from_checkpoint"]) != canonical_hash(request["checkpoint"]):
            _fail("CHECKPOINT_MISMATCH", "from_checkpoint", "Result begins at a different source checkpoint")
    errors, _ = _errors(value["errors"], request["scope"] if request is not None else None)
    if (value["status"] == "COMPLETE" and errors) or (value["status"] == "FAILED" and not errors):
        _fail("CONTRACT_STATUS_INVALID", "errors", "Reconciliation errors differ from completion status")
    result = copy.deepcopy(value)
    result.update(event_refs=_event_refs(value["event_refs"]), errors=errors)
    return result
