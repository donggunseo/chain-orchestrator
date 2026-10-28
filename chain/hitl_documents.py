"""Pure exact-version source text for a v1 HITL evidence revision.

Document text is a separate viewing bundle, never an expanded Agent input or
an additional clinical gate. Callers supply records already admitted by the
source ledger; this module performs no lookup or I/O.
"""
from __future__ import annotations

import copy

from .common.contracts import make_snapshot as make_legacy_snapshot

from .compiler import canonical_hash
from .errors import ConfigError
from .runtime_contracts import (canonical_source_refs, source_ref, timestamp,
                                validate_snapshot)
from .shape import check_json


def _fail(code, path, message):
    raise ConfigError(code, path, message)


def _groups(request, snapshot):
    snapshot = validate_snapshot(snapshot)
    if not isinstance(request, dict) or not {"request_id", "evidence_snapshot_id", "evidence_revision", "dependencies"} <= set(request):
        _fail("HITL_DOCUMENTS_INVALID", "request", "A revision-bound HITL reference is required")
    if not isinstance(request["request_id"], str) or not request["request_id"]:
        _fail("HITL_DOCUMENTS_INVALID", "request_id", "Expected a request identifier")
    if (request["evidence_snapshot_id"] != snapshot["snapshot_id"]
            or type(request["evidence_revision"]) is not int or request["evidence_revision"] < 1):
        _fail("HITL_DOCUMENTS_INVALID", "request", "Source text must bind its exact evidence revision")
    permitted = {canonical_hash(ref) for fact in snapshot["facts"].values() for ref in fact["source_refs"]}
    refs = canonical_source_refs(request["dependencies"])
    if refs != request["dependencies"] or any(canonical_hash(ref) not in permitted for ref in refs):
        _fail("HITL_DOCUMENTS_SCOPE", "dependencies", "Source text cannot expand fixed evidence dependencies")
    grouped = {}
    for ref in refs:
        grouped.setdefault((ref["system"], ref["record_id"], ref["version"]), set()).add(ref["field"])
    return snapshot, grouped


def freeze_source_documents(request, snapshot, source_records, *, frozen_at):
    """Freeze exact admitted original v0.3 text, including explicit absences.

    ``source_records`` maps (system, record_id, version) to ``facts`` and its
    actual ledger ``applied_at``. The old text Fact remains a v0.3 object in the
    v1 viewing bundle. No replacement with another source/version is allowed.
    """
    snapshot, grouped = _groups(request, snapshot)
    if not isinstance(source_records, dict):
        _fail("HITL_DOCUMENTS_INVALID", "source_records", "Expected admitted exact-version records")
    cutoff = timestamp(snapshot["known_at"])
    if timestamp(frozen_at) < cutoff:
        _fail("INVALID_TIME", "frozen_at", "Document freeze precedes evidence knowledge")
    entries = []
    for key, fields in sorted(grouped.items()):
        ref = dict(zip(("system", "record_id", "version"), key))
        entry = {"source_ref":ref, "referenced_fields":sorted(fields)}
        record = source_records.get(key)
        if record is None:
            entry.update(status="UNAVAILABLE", reason="SOURCE_NOT_RECORDED")
        elif not isinstance(record, dict) or not {"facts", "applied_at"} <= set(record):
            _fail("HITL_DOCUMENTS_INVALID", "source_records", "An admitted record needs original facts and admission time")
        elif timestamp(record["applied_at"]) > cutoff:
            entry.update(status="UNAVAILABLE", reason="DOCUMENT_NOT_KNOWN_AT_EVIDENCE_TIME")
        else:
            name = "document:" + ref["record_id"]
            document = record["facts"].get(name)
            if document is None:
                entry.update(status="NO_DOCUMENT", reason="NO_DOCUMENT")
            elif (not isinstance(document, dict) or document.get("status") != "AVAILABLE"
                  or not isinstance(document.get("value"), str) or not document["value"].strip()):
                entry.update(status="UNAVAILABLE", reason="DOCUMENT_UNAVAILABLE")
            else:
                document = copy.deepcopy(document)
                document["known_at"] = record["applied_at"]
                entry.update(status="AVAILABLE", document=document)
        entries.append(entry)
    bundle = {"contract_schema":"chain-fact-hitl-documents/v1",
        **{field:copy.deepcopy(snapshot[field]) for field in ("identity", "engine_run_id", "run_pin")},
        "request_id":request["request_id"], "evidence_snapshot_id":snapshot["snapshot_id"],
        "evidence_revision":request["evidence_revision"], "frozen_at":frozen_at,
        "purpose":"SOURCE_REFERENCE", "entries":entries}
    bundle["bundle_id"] = canonical_hash(bundle)
    return validate_source_documents(bundle, request, snapshot)


def validate_source_documents(bundle, request, snapshot):
    snapshot, grouped = _groups(request, snapshot)
    check_json(bundle, path="source_documents")
    fields = {"contract_schema", "identity", "engine_run_id", "run_pin", "request_id", "evidence_snapshot_id",
              "evidence_revision", "frozen_at", "purpose", "entries", "bundle_id"}
    if not isinstance(bundle, dict) or set(bundle) != fields:
        _fail("HITL_DOCUMENTS_INVALID", "source_documents", "Expected a closed v1 viewing bundle")
    if bundle["contract_schema"] != "chain-fact-hitl-documents/v1" or bundle["purpose"] != "SOURCE_REFERENCE":
        _fail("HITL_DOCUMENTS_INVALID", "contract_schema", "Unsupported source document contract")
    for key in ("identity", "engine_run_id", "run_pin"):
        if bundle[key] != snapshot[key]:
            _fail("IDENTITY_MISMATCH", "source_documents."+key, "Document belongs to another pinned run")
    if any(bundle[key] != request[key] for key in ("request_id", "evidence_snapshot_id", "evidence_revision")):
        _fail("HITL_DOCUMENTS_INVALID", "source_documents", "Bundle belongs to another evidence revision")
    if timestamp(bundle["frozen_at"]) < timestamp(snapshot["known_at"]) or not isinstance(bundle["entries"], list):
        _fail("HITL_DOCUMENTS_INVALID", "source_documents", "Invalid freeze time or entries")
    seen = set()
    for entry in bundle["entries"]:
        if not isinstance(entry, dict) or not {"source_ref", "referenced_fields", "status"} <= set(entry):
            _fail("HITL_DOCUMENTS_INVALID", "entry", "Invalid source document entry")
        if set(entry)-{"source_ref", "referenced_fields", "status", "document", "reason"}:
            _fail("HITL_DOCUMENTS_INVALID", "entry", "Unknown document entry field")
        source_ref(entry["source_ref"])
        ref = entry["source_ref"]
        key = (ref["system"], ref["record_id"], ref["version"])
        if (key in seen or key not in grouped or not isinstance(entry["referenced_fields"], list)
                or entry["referenced_fields"] != sorted(grouped[key])):
            _fail("HITL_DOCUMENTS_SCOPE", "entries", "Document entry must cover exact fixed provenance")
        seen.add(key)
        if entry["status"] == "AVAILABLE":
            if "document" not in entry or "reason" in entry:
                _fail("HITL_DOCUMENTS_INVALID", "entry", "Available document needs text without a missing reason")
            name = "document:"+ref["record_id"]
            document = entry["document"]
            try:
                make_legacy_snapshot({name:document}, snapshot["known_at"])
            except (ValueError, KeyError, TypeError):
                _fail("HITL_DOCUMENTS_INVALID", "document", "Invalid original text Fact")
            if (document["source_ref"] != {**ref,"field":name} or document["status"] != "AVAILABLE"
                    or not isinstance(document["value"],str) or not document["value"].strip()
                    or timestamp(document["known_at"]) > timestamp(snapshot["known_at"])
                    or timestamp(document["source_time"]) > timestamp(document["known_at"])):
                _fail("HITL_DOCUMENTS_INVALID", "document", "Text identity/version/availability differs from this evidence")
        else:
            allowed = {"NO_DOCUMENT":{"NO_DOCUMENT"}, "UNAVAILABLE":{"SOURCE_NOT_RECORDED",
                "DOCUMENT_NOT_KNOWN_AT_EVIDENCE_TIME", "DOCUMENT_UNAVAILABLE"}}
            if (entry["status"] not in allowed or "document" in entry
                    or entry.get("reason") not in allowed[entry["status"]]):
                _fail("HITL_DOCUMENTS_INVALID", "entry", "Missing document requires an explicit known reason")
    if seen != set(grouped):
        _fail("HITL_DOCUMENTS_SCOPE", "entries", "Fixed provenance is missing from the viewing bundle")
    if bundle["bundle_id"] != canonical_hash({key:value for key,value in bundle.items() if key != "bundle_id"}):
        _fail("HITL_DOCUMENTS_HASH", "bundle_id", "Document bundle content differs from its identifier")
    return copy.deepcopy(bundle)
