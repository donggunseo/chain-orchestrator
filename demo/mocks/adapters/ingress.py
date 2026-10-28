"""Ingress receipt and Worker-side exact-version resolution, outside Workflow."""
import copy
import hashlib

from chain.common.contracts import validate_event
from chain.common.source_contracts import identity, validate_completion, REVISION_STATUSES
from chain.common.validation import canonical_hash, fields, hash_value, text, timestamp


def resolve_source(event, store):
    validate_event(event)
    if identity(event) != store.identity:
        raise ValueError("Event identity mismatch")
    payload = fields(event["payload"], {"data_ref", "content_hash"}, where="source Event payload")
    hash_value(payload["content_hash"])
    if payload["data_ref"] != event["source_ref"]:
        raise ValueError("Event source/data reference mismatch")
    entry = store.resolve(payload["data_ref"], at=event["received_at"], content_hash=payload["content_hash"])
    record = entry["record"]
    if (record["event_type"] != event["event_type"] or record["source_time"] != event["source_event_time"]
            or entry["published_at"] != event["published_at"]):
        raise ValueError("Event publication metadata differs from exact source")
    facts = {name: {**copy.deepcopy(fact), "source_ref": {**record["source_ref"], "field": name},
                    "known_at": event["received_at"]} for name, fact in record["facts"].items()}
    for fact in facts.values():
        if "dependencies" in fact:
            refs={canonical_hash(r):r for r in [fact["source_ref"],*fact["dependencies"]]}
            fact["dependencies"]=[refs[k] for k in sorted(refs)]
    document = record["raw"].get("document")
    if document is not None:
        required = {"document_id", "version", "source_system", "saved_time", "text", "text_hash"}
        if not isinstance(document, dict) or required - document.keys():
            raise ValueError("Published document is missing required metadata")
        ref = record["source_ref"]
        if (document["document_id"] != ref["record_id"] or type(document["version"]) is not int
                or document["version"] != ref["version"]
                or document["source_system"] != ref["system"]
                or timestamp(document["saved_time"]) > timestamp(record["source_time"])):
            raise ValueError("Published document identity/version/time mismatch")
        content = text(document["text"], "document text")
        if document["text_hash"] != "sha256:" + hashlib.sha256(content.encode("utf8")).hexdigest():
            raise ValueError("Published document text hash mismatch")
        name = "document:" + ref["record_id"]
        if name in facts:
            raise ValueError("Duplicate normalized document field")
        facts[name] = {"value": content, "status": "AVAILABLE", "source_ref": {**ref, "field": name},
                       "source_time": document["saved_time"], "known_at": event["received_at"]}
    revision=record.get("revision")
    if revision and revision["kind"] in REVISION_STATUSES:
        for target in revision["targets"]:
            name=target["field"]
            facts[name]={"value":None,"status":REVISION_STATUSES[revision["kind"]],
                         "source_ref":{**record["source_ref"],"field":name},
                         "source_time":record["source_time"],"known_at":event["received_at"]}
    completion = {"completion_schema": "chain-context-resolved/v0.3", **identity(event),
                  "event_id": event["event_id"], "event_type": event["event_type"],
                  "sequence_no": event["sequence_no"], "source_ref": copy.deepcopy(record["source_ref"]),
                  "source_time": record["source_time"], "published_at": entry["published_at"],
                  "received_at": event["received_at"], "content_hash": record["content_hash"],
                  "facts": facts, "payload": copy.deepcopy(record["payload"])}
    if revision:completion["revision"]=copy.deepcopy(revision)
    completion["completion_hash"] = canonical_hash(completion)
    validate_completion(completion)
    return completion

