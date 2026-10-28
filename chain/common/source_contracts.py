"""Pure initial/source/resolution contracts; no clinical eligibility decisions."""
from .contracts import source_ref, validate_snapshot
from .validation import canonical_hash, fields, hash_value, json_value, positive, text, timestamp

IDENTITY = ("site_id", "patient_id", "encounter_id", "episode_id")
INITIAL_FIELDS = set(IDENTITY) | {"age", "sex", "bed", "arrival_time"}
REVISION_EVENTS = {"CORRECTION":"SOURCE_CORRECTED", "ERROR":"SOURCE_ERROR_REPORTED",
                   "RETRACTION":"SOURCE_RETRACTED", "CONFLICT":"SOURCE_CONFLICT_DECLARED"}
REVISION_STATUSES = {"ERROR":"ERROR", "RETRACTION":"RETRACTED", "CONFLICT":"CONFLICT"}


def validate_revision(obj):
    """Same-record, explicitly targeted revisions; a version bump alone is not one."""
    revision=obj.get("revision")
    if revision is None:
        if "revision" in obj:raise ValueError("Revision must be an explicit object")
        if obj["event_type"] in REVISION_EVENTS.values():raise ValueError("Revision Event requires explicit targets")
        return
    fields(revision,{"kind","targets","reason"},where="Source revision")
    kind=text(revision["kind"]);text(revision["reason"])
    if kind not in REVISION_EVENTS or obj["event_type"]!=REVISION_EVENTS[kind]:raise ValueError("Revision kind/Event mismatch")
    targets=revision["targets"]
    if not isinstance(targets,list) or not targets:raise ValueError("Revision targets required")
    seen=set()
    for target in targets:
        source_ref(target,field=True)
        if any(target[k]!=obj["source_ref"][k] for k in ("system","record_id")) or target["version"]>=obj["source_ref"]["version"]:
            raise ValueError("Revision must replace an earlier version of the same source record")
        key=canonical_hash(target)
        if key in seen:raise ValueError("Duplicate revision target")
        seen.add(key)


def identity(obj):
    for key in IDENTITY:
        text(obj.get(key), key)
    return {key: obj[key] for key in IDENTITY}


def external_ref(ref):
    source_ref(ref)
    if ref["system"] in {"CHAIN_INITIAL", "CHAIN_CONTEXT", "CHAIN_MISSING"}:
        raise ValueError("External source cannot claim an internal Context namespace")


def validate_initial(obj):
    fields(obj, INITIAL_FIELDS, where="initial")
    identity(obj)
    if type(obj["age"]) is not int or obj["age"] < 0:
        raise ValueError("initial.age: nonnegative integer required")
    for key in ("sex", "bed"):
        text(obj[key], key)
    timestamp(obj["arrival_time"])


def reject_sidecars(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.startswith("mock_") or key in {"fixtures", "executions", "recorded_hitl_events",
                                                 "expected_state_history", "screen_positive", "screening_findings"}:
                raise ValueError(f"Test/Agent sidecar is not source Context: {key}")
            reject_sidecars(value)
    elif isinstance(obj, list):
        for value in obj:
            reject_sidecars(value)


def validate_record(record):
    fields(record, {"record_schema", "synthetic", *IDENTITY, "source_ref", "event_type", "source_time",
                    "payload", "raw", "facts", "annotation", "content_hash"}, {"source_sequence_no","revision"}, "Source record")
    if record["record_schema"] != "chain-source/v0.3" or record["synthetic"] is not True:
        raise ValueError("Only synthetic chain-source/v0.3 records supported")
    identity(record); external_ref(record["source_ref"]); text(record["event_type"])
    validate_revision(record)
    source_time = timestamp(record["source_time"])
    text(record["annotation"])
    if "source_sequence_no" in record:
        positive(record["source_sequence_no"], integer=True)
    for key in ("payload", "raw", "facts"):
        if not isinstance(record[key], dict):
            raise ValueError(f"Source {key}: object required")
    json_value(record); reject_sidecars(record)
    for name, fact in record["facts"].items():
        text(name)
        fields(fact, {"value", "status", "source_time"}, {"unit", "confirmation_status","dependencies"}, "Source fact")
        json_value(fact["value"]); text(fact["status"])
        if timestamp(fact["source_time"]) > source_time:
            raise ValueError("Source fact is from the future")
        for key in ("unit", "confirmation_status"):
            if key in fact:
                text(fact[key], key)
        if "dependencies" in fact:
            refs=fact["dependencies"]
            if not isinstance(refs,list) or not refs:raise ValueError("Derived source dependencies required")
            seen=set()
            for ref in refs:
                source_ref(ref,field=True)
                if ref["system"] in {"CHAIN_CONTEXT","CHAIN_MISSING"} or ref=={**record["source_ref"],"field":name}:
                    raise ValueError("Derived source requires concrete, non-self input references")
                key=canonical_hash(ref)
                if key in seen:raise ValueError("Duplicate source dependency")
                seen.add(key)
    hash_value(record["content_hash"])
    if record["content_hash"] != canonical_hash({k:v for k,v in record.items() if k != "content_hash"}):
        raise ValueError("Source content hash mismatch")


def validate_completion(obj):
    fields(obj, {"completion_schema", *IDENTITY, "event_id", "event_type", "sequence_no", "source_ref",
                 "source_time", "published_at", "received_at", "content_hash", "facts", "payload", "completion_hash"},
           {"revision"}, "Context resolution")
    if obj["completion_schema"] != "chain-context-resolved/v0.3":
        raise ValueError("Unsupported Context resolution schema")
    identity(obj); text(obj["event_id"]); text(obj["event_type"]); external_ref(obj["source_ref"])
    validate_revision(obj)
    positive(obj["sequence_no"], integer=True)
    if not timestamp(obj["source_time"]) <= timestamp(obj["published_at"]) <= timestamp(obj["received_at"]):
        raise ValueError("Invalid Context resolution times")
    hash_value(obj["content_hash"])
    if not isinstance(obj["payload"], dict):
        raise ValueError("Resolved payload must be an object")
    json_value(obj["payload"]); reject_sidecars(obj)
    snapshot = {"contract_schema": "chain-context/v0.3", "known_at": obj["received_at"], "facts": obj["facts"]}
    snapshot["snapshot_id"] = canonical_hash(snapshot)
    validate_snapshot(snapshot)
    for name, fact in obj["facts"].items():
        if fact["source_ref"] != {**obj["source_ref"], "field": name} or fact["known_at"] != obj["received_at"]:
            raise ValueError("Resolution provenance/known time mismatch")
        if timestamp(fact["source_time"]) > timestamp(obj["source_time"]):
            raise ValueError("Resolution contains future source facts")
    for target in obj.get("revision",{}).get("targets",[]):
        fact=obj["facts"].get(target["field"])
        if fact is None:raise ValueError("Revised field must have a replacement or explicit unavailable marker")
        status=REVISION_STATUSES.get(obj["revision"]["kind"])
        if status and (fact["value"] is not None or fact["status"]!=status):
            raise ValueError("Error/retraction/conflict must not retain a usable success value")
    hash_value(obj["completion_hash"])
    if obj["completion_hash"] != canonical_hash({k:v for k,v in obj.items() if k != "completion_hash"}):
        raise ValueError("Context resolution hash mismatch")
