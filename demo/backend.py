"""Published-only backend double shared by polling and task preparation.

This is a synchronous in-memory substitute for CHAIN's backend API client. It
has no FHIR/network access and never reads a scenario or unpublished source.
The backend retains exact versions; a later resolve never queries 'latest'.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from threading import RLock

from chain.backend_source import validate_fetch_request
from chain.compiler import canonical_hash


class PublishedBackendClient:
    def __init__(self, store, *, now=None, calls=None):
        self.store = store
        self.now = now or (lambda: datetime.now(timezone.utc).isoformat())
        self.calls = calls if calls is not None else []
        self._lock = RLock()

    @staticmethod
    def _checkpoint(source, rows):
        return {"schema": "published-source-cursor/v1", "source_id": source, "offset": len(rows),
                "prefix_hash": canonical_hash([row["event"] for row in rows])}

    def fetch_source(self, request):
        request = validate_fetch_request(request)
        if request["identity"] != self.store.identity:
            raise ValueError("FACT_DEMO_BACKEND_IDENTITY_MISMATCH")
        source = request["source"]
        if source not in self.store.site["sources"]:
            raise ValueError("FACT_DEMO_BACKEND_SOURCE_NOT_DECLARED")
        allowed = {name for name, bindings in self.store.site["fact_bindings"].items()
                   if any(binding["source"] == source for binding in bindings)}
        if not set(request["scope"]) <= allowed:
            raise ValueError("FACT_DEMO_BACKEND_SCOPE_NOT_DECLARED")
        checked_at = self.now()
        with self._lock:
            self.calls.append({"method": "fetch_source", "request": copy.deepcopy(request), "at": checked_at})
        # Snapshot the immutable publication list once. New publications are
        # deliberately left for the next query rather than changing this page.
        rows = self.store.events_for_source(source)
        checkpoint = self._checkpoint(source, rows)
        if request["purpose"] == "RECONCILIATION":
            previous = request["basis"]["checkpoint"]
            offset = 0
            if previous is not None:
                if (not isinstance(previous, dict)
                        or set(previous) != {"schema", "source_id", "offset", "prefix_hash"}
                        or previous["schema"] != "published-source-cursor/v1"
                        or previous["source_id"] != source or type(previous["offset"]) is not int
                        or not 0 <= previous["offset"] <= len(rows)
                        or previous != self._checkpoint(source, rows[:previous["offset"]])):
                    raise ValueError("FACT_DEMO_BACKEND_CHECKPOINT_MISMATCH")
                offset = previous["offset"]
            changed = rows[offset:]
        else:
            known = {}
            for reference in request["basis"]["known_versions"]:
                key = reference["system"], reference["record_id"]
                known[key] = max(known.get(key, 0), reference["version"])
            changed = [row for row in rows
                if set(row["completion"]["facts"]) & set(request["scope"])
                and row["event"]["source_ref"]["version"] > known.get((
                    row["event"]["source_ref"]["system"], row["event"]["source_ref"]["record_id"]), 0)]
        return {"contract_schema": "chain-fact-source-fetch-result/v1",
            **{key: copy.deepcopy(request[key]) for key in
               ("request_id", "identity", "engine_run_id", "run_pin", "purpose", "source")},
            "status": "COMPLETE", "coverage": [{"field": field, "verification": "VERIFIED"}
                for field in request["scope"]],
            "events": [{"source_id": source, "event": copy.deepcopy(row["event"])} for row in changed],
            "checkpoint": checkpoint,
            "source_basis": {"kind": "OBSERVATION", "token": None, "observed_at": checked_at},
            "checked_at": checked_at, "errors": []}

    def resolve_exact(self, event):
        return self.store.resolve_exact(event)
