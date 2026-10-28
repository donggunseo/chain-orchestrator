"""Pure, bounded source-reconciliation and verification bookkeeping.

All time and receipt information is supplied by the caller. Requests are data:
this module does not call an API, run a Timer, prepare a clinical task, or alter
facts. Checkpoints are opaque source-owned values. Only a complete scan whose
discovered Events were all applied may advance its durable checkpoint.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
import hashlib
import json

from .errors import ConfigError
from .runtime_contracts import (validate_preflight_request, validate_preflight_result,
                                validate_reconcile_request, validate_reconcile_result)


def _fail(code, message):
    raise ConfigError(code, "reconciliation", message)


def _hash(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf8")).hexdigest()


def _time(value):
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            raise ValueError()
    except (ValueError, TypeError):
        _fail("INVALID_TIME", "An explicit timezone-aware timestamp is required")
    return result


def _integer(value, *, minimum=0):
    if type(value) is not int or value < minimum:
        _fail("INVALID_COUNTER", "A bounded nonnegative integer is required")
    return value


def _limits(value):
    expected = {"timeout_ms", "max_attempts", "max_pages", "max_preparation_rounds"}
    if not isinstance(value, dict) or set(value) != expected:
        _fail("INVALID_LIMITS", "Four explicit technical limits are required")
    for item in value.values():
        _integer(item, minimum=1)
    return copy.deepcopy(value)


def _same_identity(value, identity, run_id, run_pin):
    if value["identity"] != identity or value["engine_run_id"] != run_id or value["run_pin"] != run_pin:
        _fail("IDENTITY_MISMATCH", "The response belongs to another Episode or pinned run")


def _events(refs, lookup, expected_source):
    applied, pending, failed, epochs, sequences = [], [], [], set(), []
    for ref in refs:
        receipt = lookup(ref["event_id"])
        if receipt is None:
            pending.append(ref["event_id"])
            continue
        if receipt.get("source_id") != expected_source:
            failed.append(ref["event_id"])
            continue
        if receipt.get("status") == "PENDING":
            pending.append(ref["event_id"])
            continue
        exact = (receipt.get("event_id") == ref["event_id"]
                 and receipt.get("source_ref") == ref["source_ref"]
                 and receipt.get("content_hash") == ref["content_hash"])
        if receipt.get("status") not in {"APPLIED", "DUPLICATE"} or not exact:
            failed.append(ref["event_id"])
            continue
        applied.append(ref["event_id"])
        if type(receipt.get("data_epoch")) is int:
            epochs.add(receipt["data_epoch"])
        if type(receipt.get("accepted_reducer_seq")) is int:
            sequences.append(receipt["accepted_reducer_seq"])
    return applied, pending, failed, epochs, sequences


def _export(schema, value):
    result = {"schema": schema, **copy.deepcopy(value)}
    result["state_hash"] = _hash(result)
    return result


def _restore(state, schema):
    value = copy.deepcopy(state)
    checksum = value.pop("state_hash", None)
    if value.get("schema") != schema or _hash(value) != checksum:
        _fail("STATE_INTEGRITY_MISMATCH", "Saved owner state has a different schema or content hash")
    return value


class ReconciliationOwner:
    """One independent, finite scan per source; driven only by explicit calls.

    ``sources`` maps a logical source to scope/authorization_ref. A PARTIAL
    page without errors may continue at its tentative cursor, but never commits
    that cursor. A partial/error round restarts at the durable cursor; FAILED
    requests retry with the same request ID. Attempts, pages, rounds and the
    original deadline are retained across reconnects. A later scheduled poll is
    a new scan, not an unbounded retry of a failed scan.

    Disabling startup suppresses only that trigger. The first explicit poll is
    immediately due; later polls follow the configured interval. Reconnects do
    not replace an active scan or reset its deadline, page or retry budgets.
    After an incomplete/limited scan, reconnects also respect its next poll
    boundary instead of turning repeated reconnect signals into fresh retries.

    Retry deliveries carry ``attempt_no`` as trusted internal runner metadata,
    outside the source wire contract. After a retry has been issued, omitting
    that metadata is an error; an old attempt cannot complete the current one.
    """
    def __init__(self, identity, engine_run_id, run_pin, *, sources, limits,
                 reconciliation, event_receipt, checkpoints=None):
        self.identity, self.engine_run_id, self.run_pin = copy.deepcopy(identity), engine_run_id, run_pin
        self.sources, self.limits = copy.deepcopy(sources), _limits(limits)
        self.reconciliation = copy.deepcopy(reconciliation)
        if (set(reconciliation) != {"on_startup", "on_reconnect", "poll_interval_s"}
                or type(reconciliation["on_startup"]) is not bool
                or type(reconciliation["on_reconnect"]) is not bool
                or type(reconciliation["poll_interval_s"]) not in (int, float)
                or reconciliation["poll_interval_s"] <= 0):
            _fail("INVALID_RECONCILIATION", "Explicit triggers and a positive poll interval are required")
        _hash(reconciliation)
        self._lookup, self._sequence, self._started = event_receipt, 0, False
        self._requests = {}
        self._states = {source: {"checkpoint":copy.deepcopy((checkpoints or {}).get(source)),
            "status":"IDLE", "cycle":None, "next_poll_at":None, "errors":[], "attempts":0}
            for source in sorted(sources)}
        for source, spec in sources.items():
            if set(spec) != {"scope", "authorization_ref"}:
                _fail("INVALID_SOURCE", "Each source needs scope and authorization_ref")
            probe = self._request(source, self._states[source]["checkpoint"], identifier="validation-only")
            validate_reconcile_request(probe)

    def _request(self, source, checkpoint, *, identifier=None):
        if identifier is None:
            self._sequence += 1
            identifier = f"{self.identity['episode_id']}:reconcile:{self._sequence:06d}"
        return validate_reconcile_request({"contract_schema":"chain-fact-reconcile-request/v1",
            "request_id":identifier,"identity":copy.deepcopy(self.identity),"engine_run_id":self.engine_run_id,
            "run_pin":self.run_pin,"source":source,"scope":copy.deepcopy(self.sources[source]["scope"]),
            "authorization_ref":self.sources[source]["authorization_ref"],"checkpoint":copy.deepcopy(checkpoint),
            "limits":copy.deepcopy(self.limits)})

    def _send(self, source, now, *, retry=False, checkpoint=None):
        state = self._states[source]
        cycle = state["cycle"]
        if retry:
            cycle["attempts"] += 1
            request = self._requests[cycle["request_id"]]["request"]
            self._requests[cycle["request_id"]]["issued_attempts"] = cycle["attempts"]
        else:
            request = self._request(source, checkpoint)
            cycle["request_id"] = request["request_id"]
            cycle["pages"] += 1
            cycle["attempts"] = 1
            self._requests[request["request_id"]] = {"request":copy.deepcopy(request),"responses":[],"issued_attempts":1}
        state["attempts"] = cycle["attempts"]
        cycle["phase"] = "RESPONSE"
        state["status"] = "QUERYING"
        return copy.deepcopy(request)

    def _start(self, source, now):
        state = self._states[source]
        state["cycle"] = {"deadline":(_time(now) + timedelta(milliseconds=self.limits["timeout_ms"])).isoformat(),
            "started_at":now,"pages":0,"rounds":1,"attempts":0,"refs":[],"phase":"START",
            "working_checkpoint":copy.deepcopy(state["checkpoint"]),"candidate_checkpoint":None}
        state["errors"] = []
        return self._send(source, now, checkpoint=state["checkpoint"])

    def _end(self, source, status, now, *, error=None):
        state = self._states[source]
        state["status"] = status
        if error and error not in state["errors"]:
            state["errors"].append(error)
        state["cycle"] = None
        state["next_poll_at"] = (_time(now) + timedelta(seconds=self.reconciliation["poll_interval_s"])).isoformat()

    def on_startup(self, now):
        _time(now)
        if self._started:
            return []
        self._started = True
        return [self._start(source, now) for source in sorted(self.sources)] if self.reconciliation["on_startup"] else []

    def on_reconnect(self, now):
        if not self.reconciliation["on_reconnect"]:
            self.refresh(now)
            return []
        return self._drive(now, force=True)

    def poll(self, now):
        return self._drive(now, force=False)

    def _drive(self, now, *, force):
        self.refresh(now)
        requests = []
        for source in sorted(self.sources):
            state = self._states[source]
            cycle = state["cycle"]
            if cycle is None:
                due = state["next_poll_at"] is None or _time(now) >= _time(state["next_poll_at"])
                failed = state["status"] in {"INCOMPLETE", "LIMIT_REACHED"}
                if due or (force and not failed):
                    requests.append(self._start(source, now))
                continue
            if cycle["phase"] == "RETRY":
                if cycle["attempts"] >= self.limits["max_attempts"]:
                    self._end(source, "LIMIT_REACHED", now, error="ATTEMPT_LIMIT")
                else:
                    requests.append(self._send(source, now, retry=True))
            elif cycle["phase"] in {"PAGE", "ROUND"}:
                if cycle["pages"] >= self.limits["max_pages"] or cycle["rounds"] > self.limits["max_preparation_rounds"]:
                    self._end(source, "LIMIT_REACHED", now, error="SCAN_LIMIT")
                else:
                    requests.append(self._send(source, now, checkpoint=cycle["working_checkpoint"]))
        return requests

    def accept_result(self, result, *, now, attempt_no=None):
        _time(now)
        entry = self._requests.get(result.get("request_id"))
        if entry is None:
            _fail("REQUEST_NOT_REGISTERED", "Unknown reconciliation request")
        result = validate_reconcile_result(result, entry["request"])
        _same_identity(result, self.identity, self.engine_run_id, self.run_pin)
        source = result["source"]
        state, fingerprint = self._states[source], _hash(result)
        cycle = state["cycle"]
        attempt = cycle["attempts"] if cycle and cycle.get("request_id") == result["request_id"] else None
        previous = entry["responses"]
        issued = entry.get("issued_attempts",max((row["attempt"] for row in previous),default=1))
        if attempt_no is None:
            if issued > 1:
                _fail("DELIVERY_ATTEMPT_REQUIRED", "Retry delivery requires internal attempt metadata")
            attempt_no = 1
        if type(attempt_no) is not int or not 1 <= attempt_no <= issued:
            _fail("INVALID_DELIVERY_ATTEMPT", "The response identifies an attempt that was not issued")
        for response in previous:
            if response["attempt"] == attempt_no:
                if response["hash"] != fingerprint:
                    _fail("RESULT_ID_CONFLICT", "A completed query attempt response was replaced")
                return copy.deepcopy(response["receipt"])
        if cycle is None or cycle.get("request_id") != result["request_id"]:
            _fail("STALE_QUERY_RESULT", "The source no longer owns this request")
        if attempt_no != attempt or cycle["phase"] != "RESPONSE":
            _fail("STALE_QUERY_RESULT", "The delivery does not identify the current pending query attempt")
        if _time(now) < _time(cycle["started_at"]):
            _fail("INVALID_TIME", "Result precedes its scan")
        if _time(now) >= _time(cycle["deadline"]):
            self._end(source, "LIMIT_REACHED", now, error="DEADLINE")
        else:
            cycle["refs"].extend(ref for ref in copy.deepcopy(result["event_refs"])
                                 if ref not in cycle["refs"])
            state["errors"].extend(e for e in copy.deepcopy(result["errors"]) if e not in state["errors"])
            if result["status"] == "COMPLETE":
                cycle["candidate_checkpoint"] = copy.deepcopy(result["candidate_checkpoint"])
                cycle["phase"] = "EVENTS"
                state["status"] = "WAITING_EVENTS"
            elif result["status"] == "FAILED":
                if any(error["retryable"] for error in result["errors"]):
                    cycle["phase"] = "RETRY"
                    state["status"] = "RETRY_PENDING"
                else:
                    self._end(source, "INCOMPLETE", now, error="NON_RETRYABLE_QUERY_FAILURE")
            elif result["errors"] or _hash(result["candidate_checkpoint"]) == _hash(entry["request"]["checkpoint"]):
                cycle["rounds"] += 1
                cycle["working_checkpoint"] = copy.deepcopy(state["checkpoint"])
                cycle["phase"] = "ROUND"
                state["status"] = "PARTIAL"
            else:
                cycle["working_checkpoint"] = copy.deepcopy(result["candidate_checkpoint"])
                cycle["phase"] = "PAGE"
                state["status"] = "PARTIAL"
            self.refresh(now)
        receipt = self.status(source)
        previous.append({"attempt":attempt,"hash":fingerprint,"result":result,"receipt":receipt})
        return copy.deepcopy(receipt)

    def refresh(self, now):
        instant = _time(now)
        for source, state in self._states.items():
            cycle = state["cycle"]
            if cycle is None:
                continue
            if instant >= _time(cycle["deadline"]):
                self._end(source, "LIMIT_REACHED", now, error="DEADLINE")
                continue
            _, pending, failed, _, _ = _events(cycle["refs"], self._lookup, source)
            if failed:
                self._end(source, "INCOMPLETE", now, error="DISCOVERED_EVENT_REJECTED")
            elif cycle["phase"] == "EVENTS" and not pending:
                state["checkpoint"] = copy.deepcopy(cycle["candidate_checkpoint"])
                self._end(source, "COMPLETE", now)
        return {source:self.status(source) for source in sorted(self.sources)}

    def checkpoint(self, source):
        return copy.deepcopy(self._states[source]["checkpoint"])

    def status(self, source):
        return copy.deepcopy(self._states[source])

    def export_state(self):
        return _export("chain-reconciliation-owner/v1", {"identity":self.identity,"engine_run_id":self.engine_run_id,
            "run_pin":self.run_pin,"sources":self.sources,"limits":self.limits,"reconciliation":self.reconciliation,
            "sequence":self._sequence,"started":self._started,"states":self._states,"requests":self._requests})

    @classmethod
    def from_state(cls, state, *, event_receipt):
        state = _restore(state, "chain-reconciliation-owner/v1")
        result = cls(state["identity"],state["engine_run_id"],state["run_pin"],sources=state["sources"],
            limits=state["limits"],reconciliation=state["reconciliation"],event_receipt=event_receipt)
        for entry in state["requests"].values():
            validate_reconcile_request(entry["request"])
            for response in entry["responses"]:
                validate_reconcile_result(response["result"],entry["request"])
        result._sequence = _integer(state["sequence"])
        result._started, result._states, result._requests = state["started"],state["states"],state["requests"]
        return result


class PreflightVerifications:
    """Track observations, exact discovered Events and immutable confirmations.

    Pending observations have a deadline and do not block other requests. A
    confirmation is reusable only in its original batch and unchanged epoch.
    ``changes`` must describe actual ledger changes since the request basis;
    receipt epochs justify only this observation's own discovered Events.
    """
    def __init__(self, identity, engine_run_id, run_pin, *, event_receipt):
        self.identity,self.engine_run_id,self.run_pin = copy.deepcopy(identity),engine_run_id,run_pin
        self._lookup,self._requests,self._records,self._success = event_receipt,{},{},{}

    def register(self, request, *, now):
        request = validate_preflight_request(request)
        _same_identity(request,self.identity,self.engine_run_id,self.run_pin)
        existing = self._requests.get(request["request_id"])
        if existing:
            if existing["request"] != request:
                _fail("REQUEST_ID_CONFLICT", "A query request ID was reused")
            return copy.deepcopy(existing["request"])
        self._requests[request["request_id"]] = {"request":request,"registered_at":now,
            "deadline":(_time(now)+timedelta(milliseconds=request["limits"]["timeout_ms"])).isoformat(),
            "result":None,"record_id":None,"status":"QUERYING","changes":{},"pending_changes":[]}
        return copy.deepcopy(request)

    def accept_result(self, result, *, now, data_epoch, reducer_seq, changes=(), pending_changes=()):
        entry = self._requests.get(result.get("request_id"))
        if entry is None:
            _fail("REQUEST_NOT_REGISTERED", "Unknown preflight request")
        result = validate_preflight_result(result,entry["request"])
        _same_identity(result,self.identity,self.engine_run_id,self.run_pin)
        if not _time(entry["registered_at"]) <= _time(result["checked_at"]) <= _time(now):
            _fail("INVALID_TIME", "Observation time is outside the supplied processing order")
        if entry["result"] is not None and _hash(entry["result"]) != _hash(result):
            _fail("RESULT_ID_CONFLICT", "A preflight result was replaced")
        entry["result"] = result
        self._finish(entry,now,data_epoch,reducer_seq,changes,pending_changes)
        return self.get(entry["record_id"]) if entry["record_id"] else None

    def _finish(self, entry, now, data_epoch, reducer_seq, changes, pending_changes):
        _integer(data_epoch);_integer(reducer_seq)
        request,result = entry["request"],entry["result"]
        if data_epoch < request["basis"]["data_epoch"]:
            _fail("STALE_DATA_EPOCH", "Current epoch precedes the query basis")
        if entry["record_id"]:
            return
        if result is None:
            if _time(now) < _time(entry["deadline"]):
                return
            result = validate_preflight_result({"contract_schema":"chain-fact-preflight-result/v1",
                **{key:copy.deepcopy(request[key]) for key in ("request_id","identity","engine_run_id","run_pin")},
                "status":"FAILED","coverage":[{"field":field,"verification":"UNCONFIRMED"} for field in request["scope"]],
                "event_refs":[],"source_basis":{"kind":"OBSERVATION","token":None,"observed_at":now},
                "checked_at":now,"errors":[{"scope":copy.deepcopy(request["scope"]),
                    "code":"SOURCE_TIMEOUT","retryable":False}]},request)
            entry["result"] = result
        for change in changes:
            epoch = _integer(change["data_epoch"])
            if epoch > request["basis"]["data_epoch"]:
                entry["changes"][str(epoch)] = copy.deepcopy(change)
        entry["pending_changes"] = copy.deepcopy(list(pending_changes))
        applied,pending,failed,own_epochs,sequences = _events(result["event_refs"],self._lookup,request["source"])
        expired = _time(now) >= _time(entry["deadline"])
        if pending and not expired and not failed:
            entry["status"] = "WAITING_EVENTS"
            return
        refs = {ref["event_id"] for ref in result["event_refs"]}
        basis = request["basis"]["data_epoch"]
        relevant_epochs = {epoch for epoch in own_epochs if basis < epoch <= data_epoch}
        unrelated = False
        for key, change in entry["changes"].items():
            epoch = int(key)
            if epoch > data_epoch:
                _fail("INVALID_DATA_EPOCH", "A supplied change is newer than the current epoch")
            cause = change.get("event_id",change.get("cause"))
            if cause in refs:
                relevant_epochs.add(epoch)
            else:
                unrelated = True
        sharing = (not unrelated and not pending_changes and not pending and not failed and not expired
                   and len(relevant_epochs) == data_epoch-basis and all(seq <= reducer_seq for seq in sequences))
        coverage,errors = copy.deepcopy(result["coverage"]),copy.deepcopy(result["errors"])
        if pending or failed or expired:
            coverage = [{"field":field,"verification":"UNCONFIRMED"} for field in request["scope"]]
            errors = [{"scope":copy.deepcopy(request["scope"]),"code":"DISCOVERED_EVENT_UNAPPLIED" if pending or failed else "PREPARATION_LIMIT_REACHED","retryable":False}]
            sharing = False
        last = self._success.setdefault(request["source"],{})
        for item in coverage:
            if item["verification"] == "VERIFIED":
                previous = last.get(item["field"])
                if previous is None or _time(previous) <= _time(result["checked_at"]):
                    last[item["field"]] = result["checked_at"]
        versions = {(ref["source_ref"]["system"],ref["source_ref"]["record_id"],ref["source_ref"]["version"]):
                    copy.deepcopy(ref["source_ref"]) for ref in result["event_refs"] if ref["event_id"] in applied}
        record = {"contract_schema":"chain-fact-verification/v1","identity":copy.deepcopy(self.identity),
            "engine_run_id":self.engine_run_id,"run_pin":self.run_pin,"request_id":request["request_id"],
            "preparation_id":request["preparation_id"],"parallel_batch_id":request["parallel_batch_id"],
            "consumer_task_ids":copy.deepcopy(request["consumer_task_ids"]),"source":request["source"],
            "authorization_ref":request["authorization_ref"],"coverage":coverage,"source_basis":copy.deepcopy(result["source_basis"]),
            "observed_source_versions":[versions[key] for key in sorted(versions)],
            "applied_event_ids":sorted(applied),"accepted_reducer_seq":reducer_seq,
            "sharing_epoch":data_epoch if sharing else None,"checked_at":result["checked_at"],
            "last_success_by_scope":{field:last[field] for field in request["scope"] if field in last},"errors":errors}
        record["verification_id"] = _hash(record)
        self._records[record["verification_id"]] = record
        entry["record_id"],entry["status"] = record["verification_id"],"RECORDED"

    def refresh(self, *, now, data_epoch, reducer_seq, changes=(), pending_changes=()):
        completed = []
        for entry in self._requests.values():
            previous = entry["record_id"]
            self._finish(entry,now,data_epoch,reducer_seq,changes,pending_changes)
            if previous is None and entry["record_id"]:
                completed.append(self.get(entry["record_id"]))
        return completed

    def get(self, verification_id):
        return copy.deepcopy(self._records[verification_id])

    def status(self, request_id):
        return copy.deepcopy(self._requests[request_id])

    def reusable(self, verification_id, request, *, data_epoch, pending_changes=()):
        request = validate_preflight_request(request)
        _same_identity(request,self.identity,self.engine_run_id,self.run_pin)
        record = self._records[verification_id]
        original = self._requests[record["request_id"]]["request"]
        return (record["sharing_epoch"] is not None and record["sharing_epoch"] == data_epoch
            and not pending_changes and request["parallel_batch_id"] == original["parallel_batch_id"]
            and request["source"] == original["source"] and request["authorization_ref"] == original["authorization_ref"]
            and set(request["scope"]) <= {item["field"] for item in record["coverage"]}
            and set(request["consumer_task_ids"]) <= set(original["consumer_task_ids"]))

    def export_state(self):
        return _export("chain-preflight-observations/v1", {"identity":self.identity,"engine_run_id":self.engine_run_id,
            "run_pin":self.run_pin,"requests":self._requests,"records":self._records,"last_success":self._success})

    @classmethod
    def from_state(cls, state, *, event_receipt):
        state = _restore(state,"chain-preflight-observations/v1")
        result = cls(state["identity"],state["engine_run_id"],state["run_pin"],event_receipt=event_receipt)
        for entry in state["requests"].values():
            validate_preflight_request(entry["request"])
            if entry["result"] is not None:
                validate_preflight_result(entry["result"],entry["request"])
        for key, record in state["records"].items():
            if record["verification_id"] != key or _hash({k:v for k,v in record.items() if k!="verification_id"}) != key:
                _fail("STATE_INTEGRITY_MISMATCH", "Saved immutable verification changed")
        result._requests,result._records,result._success = state["requests"],state["records"],state["last_success"]
        return result
