"""Published-only synthetic source boundary for the native Fact demo.

The Simulator opens a source file at its causal release and hands its record to
publish(). This store never opens an Episode directory or a future schedule.
Site's field bindings select the native profile's declared facts; original raw
documents and exact converted records remain separate from Agent input scope.
"""
from __future__ import annotations

import copy
from threading import RLock

from demo.mocks.adapters.ingress import resolve_source
from demo.mocks.adapters.store import PublishedStore
from chain.common.contracts import validate_event
from chain.common.source_contracts import identity, validate_record
from chain.common.validation import canonical_hash, timestamp


def _pointer(value, pointer):
    current = value
    for key in pointer.strip("/").split("/"):
        key = key.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return copy.deepcopy(current)


def project_record(record, site, source_id):
    """Project an already released record; retain original status and values.

    No string/bool inference or clinical summary is performed. NO_EVIDENCE
    remains that source status; the core normalizer displays it as UNKNOWN.
    """
    validate_record(record)
    source = site["sources"].get(source_id)
    if source is None or record["source_ref"]["system"] != source["system"]:
        raise ValueError("FACT_DEMO_SOURCE_SYSTEM_MISMATCH")
    result = copy.deepcopy(record)
    result["facts"] = {}
    for name, bindings in site["fact_bindings"].items():
        for binding in bindings:
            if binding["source"] != source_id:
                continue
            if binding.get("record_type") and record["payload"].get("document_type") != binding["record_type"]:
                continue
            value = _pointer(record, binding["field"])
            if value is None:
                continue
            if binding["field"].startswith("/facts/"):
                result["facts"][name] = value
            else:
                result["facts"][name] = {"value": value, "status": "AVAILABLE",
                    "source_time": value.get("saved_time", record["source_time"]) if isinstance(value, dict) else record["source_time"]}
    if any(target["field"] not in result["facts"] for target in result.get("revision", {}).get("targets", [])):
        raise ValueError("FACT_DEMO_REVISION_FIELD_OUTSIDE_PROFILE")
    result["raw"]["fact_demo_projection"] = {
        "original_content_hash": record["content_hash"], "site_id": site["site_id"],
        "rule": "EXPLICIT_SITE_FIELD_BINDINGS_NO_CLINICAL_INFERENCE"}
    result["annotation"] += "; FACT_DEMO_DECLARED_FIELD_PROJECTION"
    result["content_hash"] = canonical_hash({key: value for key, value in result.items() if key != "content_hash"})
    validate_record(result)
    return result


class PublishedSourceStore:
    def __init__(self, initial, site):
        self.identity = identity(initial)
        if self.identity["site_id"] != site["site_id"]:
            raise ValueError("FACT_DEMO_SITE_MISMATCH")
        self.site = copy.deepcopy(site)
        self._store = PublishedStore(initial)
        self._events = {}
        self._sequence = 0
        self._lock = RLock()
        self.publications = []
        self.calls = []

    def publish(self, record, *, source_id, event_id, at):
        if identity(record) != self.identity:
            raise ValueError("FACT_DEMO_SOURCE_IDENTITY_MISMATCH")
        projected = project_record(record, self.site, source_id)
        with self._lock:
            existing = self._events.get(event_id)
            if existing:
                if (existing["source_id"] != source_id or existing["event"]["payload"]["content_hash"] != projected["content_hash"]):
                    raise ValueError("FACT_DEMO_EVENT_ID_CONFLICT")
                return copy.deepcopy(existing["event"])
            entry = self._store.publish(projected, at=at)
            self._sequence += 1
            payload = {"data_ref": copy.deepcopy(projected["source_ref"]), "content_hash": projected["content_hash"]}
            event = {"contract_schema": "chain-event/v0.3", **self.identity,
                "event_id": event_id, "event_type": projected["event_type"],
                "source_ref": copy.deepcopy(projected["source_ref"]), "sequence_no": self._sequence,
                "source_event_time": projected["source_time"], "published_at": entry["published_at"],
                "emitted_time": at, "received_at": at, "payload": payload, "payload_hash": canonical_hash(payload)}
            if "source_sequence_no" in projected:
                event["source_sequence_no"] = projected["source_sequence_no"]
            validate_event(event)
            completion = resolve_source(event, self._store)
            self._events[event_id] = {"source_id": source_id, "event": copy.deepcopy(event),
                                      "completion": completion}
            self.publications.append({"source_id": source_id, "event": copy.deepcopy(event),
                "original_content_hash": record["content_hash"], "projected_content_hash": projected["content_hash"]})
            return copy.deepcopy(event)

    def resolve_exact(self, event):
        validate_event(event)
        with self._lock:
            row = self._events.get(event["event_id"])
            if row is None or row["event"] != event:
                raise ValueError("FACT_DEMO_EXACT_SOURCE_NOT_PUBLISHED")
            return copy.deepcopy(row["completion"])

    def events_for_source(self, source_id):
        if source_id not in self.site["sources"]:
            raise ValueError("FACT_DEMO_SOURCE_NOT_DECLARED")
        with self._lock:
            return [copy.deepcopy(row) for row in self._events.values() if row["source_id"] == source_id]

    def record_call(self, value):
        with self._lock:
            self.calls.append(copy.deepcopy(value))

