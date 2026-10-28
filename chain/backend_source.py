"""Worker-side source bridge for periodic and work-preparation backend reads.

The trusted backend client owns HTTP/FHIR mapping and durable, immutable source
versions. Both entrypoints issue one ``fetch_source`` call; there is no polling
cache or implicit retry here. Discovery returns ordinary Ingress Events, never
a Context patch. Resolving an Event must return that exact published version,
including after another Worker starts or the source has subsequently changed.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import inspect

from .common.contracts import validate_event
from .common.source_contracts import identity, validate_completion
from .errors import BackendUnavailable, ConfigError
from .runtime_contracts import (
    _common, _enum, _errors, _event_refs, _hash, _limits, _match, _shape,
    _text, canonical_source_refs, timestamp, validate_preflight_request,
    validate_preflight_result, validate_reconcile_request,
    validate_reconcile_result, validate_scope,
)
from .shape import check_json


FETCH_REQUEST_SCHEMA = "chain-fact-source-fetch-request/v1"
FETCH_RESULT_SCHEMA = "chain-fact-source-fetch-result/v1"
PURPOSES = {"RECONCILIATION", "AGENT_INPUT", "HITL_EVIDENCE", "DELAY_NOTICE", "EHR_EFFECT"}
_COMMON = ("request_id", "identity", "engine_run_id", "run_pin")


def _fail(code, path, message):
    raise ConfigError(code, path, message) from None


def validate_fetch_request(value):
    """Validate the shared backend request; returned copies contain no clients."""
    _common(value, FETCH_REQUEST_SCHEMA,
            {"purpose", "source", "scope", "authorization_ref", "basis", "limits"})
    for key in ("source", "authorization_ref"):
        _text(value[key], key)
    _enum(value["purpose"], PURPOSES, "purpose")
    result = copy.deepcopy(value)
    result["scope"] = validate_scope(value["scope"])
    _shape(value["basis"], {"known_versions", "checkpoint"}, "basis")
    result["basis"]["known_versions"] = canonical_source_refs(value["basis"]["known_versions"], field=False)
    _limits(value["limits"])
    return result


def _source_event(value, *, system, expected_identity=None):
    check_json(value, path="event")
    try:
        validate_event(value)
    except (ValueError, TypeError, KeyError):
        _fail("BACKEND_EVENT_INVALID", "events", "Backend returned an invalid standard Event")
    if (expected_identity is not None and identity(value) != expected_identity
            or value["source_ref"]["system"] != system):
        _fail("BACKEND_IDENTITY_MISMATCH", "events", "Backend Event belongs to another patient, Episode or source system")
    if set(value["payload"]) != {"data_ref", "content_hash"} or value["payload"]["data_ref"] != value["source_ref"]:
        _fail("BACKEND_EVENT_INVALID", "events.payload", "Backend Event must identify an exact source version")
    _hash(value["payload"]["content_hash"], "events.payload.content_hash")


def validate_fetch_result(value, request, *, system):
    """Reject invalid backend data instead of relabeling it as a query failure.

    PARTIAL may be a successful intermediate page with all fields VERIFIED;
    that is not sufficient to claim a complete preflight verification.
    An empty result says nothing about deletion: explicit versioned revisions
    must arrive as ordinary source Events and exact completions.
    """
    request = validate_fetch_request(request)
    _common(value, FETCH_RESULT_SCHEMA,
            {"purpose", "source", "status", "coverage", "events", "checkpoint",
             "source_basis", "checked_at", "errors"})
    _match(value, request, source=True)
    if value["purpose"] != request["purpose"]:
        _fail("REQUEST_RESULT_MISMATCH", "purpose", "Backend result differs from the requested purpose")
    _enum(value["status"], {"COMPLETE", "PARTIAL", "FAILED"}, "status")
    checked = timestamp(value["checked_at"], path="checked_at")
    if not isinstance(value["coverage"], list):
        _fail("BACKEND_COVERAGE_INVALID", "coverage", "Expected verification for every requested field")
    covered, unconfirmed = set(), set()
    for entry in value["coverage"]:
        _shape(entry, {"field", "verification"}, "coverage")
        name = _text(entry["field"], "coverage.field")
        _enum(entry["verification"], {"VERIFIED", "UNCONFIRMED"}, "coverage.verification")
        if name in covered:
            _fail("BACKEND_COVERAGE_INVALID", "coverage", "Duplicate verification field")
        covered.add(name)
        if entry["verification"] == "UNCONFIRMED":
            unconfirmed.add(name)
    if covered != set(request["scope"]):
        _fail("BACKEND_COVERAGE_INVALID", "coverage", "Backend verification differs from the requested scope")
    errors, error_scope = _errors(value["errors"], unconfirmed)
    if error_scope != unconfirmed:
        _fail("BACKEND_COVERAGE_INVALID", "errors", "Errors must explain exactly the unconfirmed fields")
    if value["status"] == "COMPLETE" and unconfirmed or value["status"] == "FAILED" and unconfirmed != covered:
        _fail("BACKEND_STATUS_INVALID", "status", "Backend completion status contradicts field verification")
    basis = value["source_basis"]
    _shape(basis, {"kind", "token", "observed_at"}, "source_basis")
    _enum(basis["kind"], {"VERSION_TOKEN", "CURSOR", "OBSERVATION"}, "source_basis.kind")
    if timestamp(basis["observed_at"], path="source_basis.observed_at") > checked:
        _fail("TIMESTAMP_INVALID", "source_basis", "Backend observation follows query completion")
    if value["status"] == "FAILED" or basis["kind"] == "OBSERVATION":
        if basis["token"] is not None:
            _fail("SOURCE_BASIS_INVALID", "source_basis.token", "Failed or observational reads cannot claim a version token")
    else:
        _text(basis["token"], "source_basis.token")
    if not isinstance(value["events"], list):
        _fail("BACKEND_EVENT_INVALID", "events", "Expected discovered standard Events")
    refs = []
    for envelope in value["events"]:
        _shape(envelope, {"source_id", "event"}, "events")
        if envelope["source_id"] != request["source"]:
            _fail("BACKEND_IDENTITY_MISMATCH", "events.source_id", "Backend Event belongs to another logical source")
        event = envelope["event"]
        _source_event(event, system=system, expected_identity=request["identity"])
        refs.append({"event_id": event["event_id"], "source_ref": event["source_ref"],
                     "content_hash": event["payload"]["content_hash"]})
    _event_refs(refs)  # Also rejects duplicate IDs and conflicting hashes for one source version.
    result = copy.deepcopy(value)
    result["coverage"] = sorted(result["coverage"], key=lambda item: item["field"])
    result["errors"] = errors
    return result


class BackendSourceProvider:
    """Installed source provider using one application-owned backend client.

    The backend must keep stable Event IDs/timestamps and immutable versions.
    It may serve multiple Episodes; all query state is carried by each request,
    never in a provider-wide last-patient or last-cursor variable. Its client is
    responsible for safe concurrent I/O and any shared rate/concurrency limits.
    """
    def __init__(self, source_id, backend, *, system, now=None):
        self.source_id, self.system = _text(source_id, "source_id"), _text(system, "system")
        for method in ("fetch_source", "resolve_exact"):
            callback = getattr(backend, method, None)
            if not callable(callback) or inspect.iscoroutinefunction(callback):
                _fail("BACKEND_CLIENT_INVALID", method, "Expected a synchronous backend client callback")
        self.backend = backend
        if now is not None and not callable(now):
            _fail("BACKEND_CLOCK_INVALID", "now", "Expected a Worker clock callback")
        self._clock = now if now is not None else (lambda: datetime.now(timezone.utc).isoformat())

    def _request(self, request, *, reconcile):
        request = (validate_reconcile_request if reconcile else validate_preflight_request)(request)
        if request["source"] != self.source_id:
            _fail("BACKEND_IDENTITY_MISMATCH", "source", "Request belongs to another installed source")
        result = {"contract_schema": FETCH_REQUEST_SCHEMA,
            **{key: copy.deepcopy(request[key]) for key in _COMMON},
            "purpose": "RECONCILIATION" if reconcile else request["purpose"],
            "source": self.source_id, "scope": request["scope"],
            "authorization_ref": request["authorization_ref"], "limits": request["limits"],
            "basis": {"known_versions": [] if reconcile else request["basis"]["known_versions"],
                      "checkpoint": request["checkpoint"] if reconcile else request["basis"]["source_cursor"]}}
        return request, validate_fetch_request(result)

    def _fetch(self, request):
        try:
            response = self.backend.fetch_source(copy.deepcopy(request))
        except BackendUnavailable as error:
            now = self._clock()
            response = {"contract_schema": FETCH_RESULT_SCHEMA,
                **{key: copy.deepcopy(request[key]) for key in (*_COMMON, "purpose", "source")},
                "status": "FAILED",
                "coverage": [{"field": field, "verification": "UNCONFIRMED"} for field in request["scope"]],
                "events": [], "checkpoint": copy.deepcopy(request["basis"]["checkpoint"]),
                "source_basis": {"kind": "OBSERVATION", "token": None, "observed_at": now},
                "checked_at": now, "errors": [{"scope": request["scope"], "code": error.code, "retryable": error.retryable}]}
        return validate_fetch_result(response, request, system=self.system)

    @staticmethod
    def _refs(events):
        return [{"event_id": row["event"]["event_id"], "source_ref": copy.deepcopy(row["event"]["source_ref"]),
                 "content_hash": row["event"]["payload"]["content_hash"]} for row in events]

    def query_latest(self, request):
        original, query = self._request(request, reconcile=False)
        fetched = self._fetch(query)
        coverage, errors = fetched["coverage"], fetched["errors"]
        if fetched["status"] == "PARTIAL" and all(row["verification"] == "VERIFIED" for row in coverage):
            # An intermediate page cannot prove that the remaining pages had no changes.
            coverage = [{"field": field, "verification": "UNCONFIRMED"} for field in query["scope"]]
            errors = [{"scope": query["scope"], "code": "SOURCE_SCAN_INCOMPLETE", "retryable": True}]
        count = sum(row["verification"] == "UNCONFIRMED" for row in coverage)
        status = ("FAILED" if count == len(coverage) else "PARTIAL") if count else (
            "CHANGES_FOUND" if fetched["events"] else "NO_CHANGE")
        source_basis = copy.deepcopy(fetched["source_basis"])
        if status == "FAILED":
            source_basis.update(kind="OBSERVATION", token=None)
        result = {"contract_schema": "chain-fact-preflight-result/v1",
            **{key: copy.deepcopy(original[key]) for key in _COMMON}, "status": status,
            "coverage": coverage, "event_refs": self._refs(fetched["events"]),
            "source_basis": source_basis, "checked_at": fetched["checked_at"], "errors": errors}
        return {"result": validate_preflight_result(result, original), "events": copy.deepcopy(fetched["events"])}

    def reconcile_changes(self, request):
        original, query = self._request(request, reconcile=True)
        fetched = self._fetch(query)
        result = {"contract_schema": "chain-fact-reconcile-result/v1",
            **{key: copy.deepcopy(original[key]) for key in (*_COMMON, "source")},
            "status": fetched["status"], "from_checkpoint": copy.deepcopy(original["checkpoint"]),
            "candidate_checkpoint": copy.deepcopy(fetched["checkpoint"]),
            "event_refs": self._refs(fetched["events"]), "errors": fetched["errors"]}
        return {"result": validate_reconcile_result(result, original), "events": copy.deepcopy(fetched["events"])}

    def resolve_exact(self, event):
        _source_event(event, system=self.system)
        try:
            completion = self.backend.resolve_exact(copy.deepcopy(event))
        except BackendUnavailable as error:
            # Preserve the existing Activity boundary's retry classification.
            # This does not retry, alter the Event or fall back to latest data.
            if error.retryable:
                raise ConnectionError(error.code) from None
            _fail(error.code, "backend.resolve_exact", "Exact source version is unavailable")
        check_json(completion, path="completion")
        try:
            validate_completion(completion)
        except (ValueError, TypeError, KeyError):
            _fail("BACKEND_COMPLETION_INVALID", "completion", "Backend returned an invalid exact-version completion")
        if (identity(completion) != identity(event)
                or any(completion[key] != event[key] for key in (
                    "event_id", "event_type", "sequence_no", "source_ref", "published_at", "received_at"))
                or completion["content_hash"] != event["payload"]["content_hash"]
                or completion["source_time"] != event["source_event_time"]):
            _fail("BACKEND_VERSION_MISMATCH", "completion", "Backend returned a different Event or source version")
        return copy.deepcopy(completion)


def provider_factory(binding, services):
    """Native source entrypoint; HTTP clients/credentials stay in Worker services."""
    if binding.get("kind") != "source" or "source_backend" not in services:
        _fail("BACKEND_CLIENT_MISSING", "source_backend", "Provide an application-owned source backend in Worker services")
    return BackendSourceProvider(binding["logical_id"], services["source_backend"],
                                 system=binding["system"], now=services.get("now"))
