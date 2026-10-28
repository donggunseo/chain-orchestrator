"""Approved synthetic modules for the native demo; no scenario/answer lookup.

Source adapters see only already-published records. Agents select committed
input hashes from immutable fixture JSON. Missing/ambiguous inputs stay errors.
The Summary module composes requested structured cells; it does not run NLP.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path

from demo.mocks.effects import MockPresentationEffects
from demo.mocks.notifier import MockNotifier
from demo.backend import PublishedBackendClient
from chain.backend_source import BackendSourceProvider
from chain.task_runtime import StructuredSummaryProvider, V1FixtureProvider

ROOT = Path(__file__).resolve().parents[1]


class PublishedSource(BackendSourceProvider):
    def __init__(self, source_id, services):
        self.source_id = source_id
        self.services = services
        self.store = services["store"]
        if "source_backend" not in services:
            services["source_backend"] = PublishedBackendClient(self.store, now=self._now,
                calls=services.setdefault("backend_calls", []))
        super().__init__(source_id, services["source_backend"],
            system=self.store.site["sources"][source_id]["system"], now=self._now)

    def _now(self):
        return self.services.get("now", lambda: datetime.now(timezone.utc).isoformat())()

    def _record(self, method, request):
        row = {"source": self.source_id, "method": method, "request": copy.deepcopy(request), "at": self._now()}
        try:
            from temporalio import activity
            info = activity.info()
            row.update(activity_id=info.activity_id, sdk_attempt=info.attempt)
        except RuntimeError:
            row.update(activity_id=None, sdk_attempt=None)
        self.store.record_call(row)
        if "source_calls" in self.services:
            self.services["source_calls"].append(copy.deepcopy(row))

    def resolve_exact(self, event):
        self._record("resolve_exact", event)
        return super().resolve_exact(event)

    def query_latest(self, request):
        self._record("query_latest", request)
        return super().query_latest(request)

    def reconcile_changes(self, request):
        self._record("reconcile_changes", request)
        return super().reconcile_changes(request)


def provider_factory(binding, services):
    kind, metadata = binding["kind"], binding["descriptor"]["metadata"]
    if kind == "source":
        return PublishedSource(binding["logical_id"], services)
    if kind == "notifier":
        return MockNotifier(services["notifier_path"], metadata["templates"], write=services.get("write", print))
    if kind == "effect":
        return MockPresentationEffects(services["effects_path"], metadata["operations"], write=services.get("write", print))
    if kind != "agent":
        raise ValueError("FACT_DEMO_PROVIDER_KIND_UNSUPPORTED")
    contract = metadata["contract"]
    common = {"agent_id": metadata["agent_id"], "version": contract["version"],
              "implementation_hash": binding["implementation_hash"]}
    if metadata["backend"]["kind"] == "structured":
        return StructuredSummaryProvider(**common, return_cache_metadata=True)
    path = (ROOT / metadata["backend"]["config_ref"]).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError("FACT_DEMO_FIXTURE_PATH_ESCAPED")
    fixtures = json.loads(path.read_text(encoding="utf8"))[metadata["agent_id"]]

    def validate_input(request, snapshot):
        if set(request["scope"]) != set(snapshot["facts"]):
            raise ValueError("FACT_DEMO_INPUT_SCOPE_MISMATCH")

    def validate_output(payload, request, snapshot):
        if set(payload) != set(contract["modes"][request["mode"]]["output_fields"]) or payload.get("mock_only") is not True:
            raise ValueError("FACT_DEMO_OUTPUT_SCHEMA_INVALID")

    return V1FixtureProvider(fixtures, **common, modes=list(contract["modes"]),
        input_validator=validate_input, output_validator=validate_output)
