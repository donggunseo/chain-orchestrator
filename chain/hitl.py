"""Revisioned HITL questions and receipts for the fact execution path.

Calls are serialized by the Episode owner. This module has no console, API,
plugin import, authentication service, clock or Timer. Policy string checks do
not prove authentication or actual reading of evidence.
"""
from __future__ import annotations

import copy

from .compiler import canonical_hash
from .errors import ConfigError
from .hitl_documents import freeze_source_documents, validate_source_documents
from .runtime_contracts import canonical_source_refs, timestamp, validate_snapshot, _hash
from .shape import check_json
from .task_contracts import semantic_input, validate_work_request

_COMMON = {"identity", "engine_run_id", "run_pin"}
_QUESTION = {"contract_schema", *_COMMON, "task_id", "request_id", "decision_definition_id",
             "cycle_no", "status", "current_revision", "revision_ids", "requested_at"}
_STATUSES = {"PREPARING", "OPEN", "DECIDED", "CLOSED", "PREPARATION_FAILED"}


def _fail(code, path, message):
    raise ConfigError(code, path, message)


def _closed(value, fields, path):
    if not isinstance(value, dict) or set(value) != fields:
        _fail("HITL_CONTRACT_INVALID", path, "Expected a closed HITL contract")


def _text(value, path):
    if not isinstance(value, str) or not value:
        _fail("HITL_CONTRACT_INVALID", path, "Expected a nonempty string")


def _strings(value, path):
    if not isinstance(value, list) or any(not isinstance(v,str) or not v for v in value) or len(set(value)) != len(value):
        _fail("HITL_CONTRACT_INVALID", path, "Expected unique string entries")


def _union(refs):
    return canonical_source_refs(list({canonical_hash(ref):ref for ref in refs}.values()))


class HITLOwner:
    def __init__(self, definition, ledger):
        check_json(definition)
        if canonical_hash(definition) != ledger.definition_hash:
            _fail("DEFINITION_PIN_MISMATCH", "definition", "HITL and ledger definitions differ")
        self._d, self.ledger = copy.deepcopy(definition), ledger
        self._questions, self._evidence, self._metadata, self._commands = {}, {}, {}, {}
        self._submissions, self._resumes, self._resume_targets, self._cycles = {}, {}, {}, {}
        self._result_store, self._execution_questions, self._meaning_hashes = {}, {}, {}
        self._latest, self._feed, self._sequence, self._view_seq = {}, [], 0, 0
        self._clock = ledger.processed_at

    def _common(self):
        return {"identity":copy.deepcopy(self.ledger.identity),"engine_run_id":self.ledger.engine_run_id,"run_pin":self.ledger.run_pin}

    def _identity(self, value):
        if not isinstance(value,dict) or any(value.get(key) != expected for key,expected in self._common().items()):
            _fail("IDENTITY_MISMATCH", "identity", "HITL message belongs to another pinned run")

    def _check_time(self, now):
        if timestamp(now) < max(timestamp(self._clock), timestamp(self.ledger.processed_at)):
            _fail("INVALID_TIME", "now", "HITL processing time cannot move backwards")

    def _event(self, kind, request_id, now):
        self._clock = now
        self._view_seq += 1
        question = self._questions[request_id]
        self._feed.append({"view_seq":self._view_seq,"kind":kind,"request_id":request_id,
            "evidence_revision":question["current_revision"],"status":question["status"],"at":now})

    @property
    def requests(self): return copy.deepcopy(self._questions)

    @property
    def evidence(self): return copy.deepcopy(self._evidence)

    @property
    def receipts(self): return {key:copy.deepcopy(value["receipt"]) for key,value in self._submissions.items()}

    @property
    def view_seq(self): return self._view_seq

    def metadata(self, request_id): return copy.deepcopy(self._metadata[request_id])

    def current(self, request_id):
        ids = self._questions[request_id]["revision_ids"]
        return copy.deepcopy(self._evidence[ids[-1]]) if ids else None

    def changes_since(self, view_seq=0):
        if type(view_seq) is not int or not 0 <= view_seq <= self._view_seq:
            _fail("HITL_CONTRACT_INVALID", "view_seq", "Unknown display sequence")
        return copy.deepcopy([entry for entry in self._feed if entry["view_seq"] > view_seq])

    @property
    def decision_cycles(self):
        result = {}
        for name, request_id in self._latest.items():
            question, meta = self._questions[request_id], self._metadata[request_id]
            status = "DEFERRED" if question["status"] == "DECIDED" and meta["effect"] == "defer" else question["status"]
            if status == "PREPARATION_FAILED": status = "PREPARING"
            result[name] = {"id":request_id,"epoch":question["cycle_no"],"opened_at":meta["opened_at"] or question["requested_at"],
                "deferred_at":meta["deferred_at"],"status":status}
        return copy.deepcopy(result)

    def _command(self, command, now, *, ready=True):
        check_json(command)
        if not isinstance(command,dict) or command.get("kind") != "hitl_prepared" or "request" not in command:
            _fail("HITL_CONTRACT_INVALID", "command", "Expected prepared HITL work")
        request = validate_work_request(command["request"])
        self._identity(request)
        if request["kind"] != "hitl" or request["task_definition_id"] not in self._d["tasks"]:
            _fail("HITL_CONTRACT_INVALID", "task", "Prepared work must identify a declared HITL task")
        spec = self._d["tasks"][request["task_definition_id"]]
        if spec != request["task"]:
            _fail("HITL_CONTRACT_INVALID", "task", "Prepared work declaration differs from the pinned definition")
        decision = self._d["decisions"][spec["decision"]]
        scope = sorted(self._d["contexts"][decision["evidence_context"]]["fields"])
        if request["scope"] != scope or timestamp(request["evaluated_at"]) > timestamp(now):
            _fail("HITL_CONTRACT_INVALID", "scope", "Prepared evidence scope/time differs from its declaration")
        if self.ledger.frozen_snapshot(request["snapshot"]["snapshot_id"]) != request["snapshot"]:
            _fail("HITL_CONTRACT_INVALID", "snapshot", "Evidence must already be frozen in this ledger")
        results, references = command.get("prerequisite_results",{}), command.get("reference_results",{})
        checks = command.get("verifications",{})
        if (not isinstance(results,dict) or set(results) != set(request["prerequisite_result_ids"])
                or not isinstance(references,dict) or not isinstance(checks,dict)
                or set(checks) != set(request["verification_ids"])):
            _fail("HITL_CONTRACT_INVALID", "command", "Prepared evidence needs its exact result/verification sidecars")
        required, old, structured = [], [], []
        for is_required, collection in ((True,results),(False,references)):
            for identifier, result in collection.items():
                self._identity(result)
                if (not {"execution_id","request_id","status","result","produced_at","input_snapshot_id"} <= set(result)
                        or result.get("execution_id") != identifier or result.get("request_id") != identifier
                        or result.get("status") != "SUCCESS" or not isinstance(result.get("result"),dict)
                        or timestamp(result.get("produced_at")) > timestamp(now)):
                    _fail("HITL_RESULT_INVALID", "results", "Evidence requires a recorded successful Agent result")
                commitment = self.ledger.task_results.get(identifier,{}).get("record")
                if not commitment or commitment["result_hash"] != canonical_hash(result):
                    _fail("HITL_RESULT_INVALID", "results", "Result is not a validated ledger completion")
                current = self.ledger.input_is_current(result["input_snapshot_id"],pending_changes=self.ledger.dependency_pending_changes)
                if is_required and current:
                    required.append(identifier)
                    if "structured_context" in result["result"]: structured.append(identifier)
                else:
                    old.append(identifier)
                if is_required and ready and not current:
                    _fail("HITL_RESULT_NOT_CURRENT", "results", "An old input cannot satisfy a current prerequisite")
        if ready and len(required) < len(spec["after"]):
            _fail("HITL_PREREQUISITE_MISSING", "results", "Required Agent results are not ready")
        if ready and self._d["contexts"][decision["evidence_context"]]["summary"] and not structured:
            _fail("HITL_SUMMARY_MISSING", "results", "Declared Summary must succeed before opening HITL")
        for identifier, record in checks.items():
            self._identity(record)
            if (record.get("contract_schema") != "chain-fact-verification/v1" or record.get("verification_id") != identifier
                    or canonical_hash({k:v for k,v in record.items() if k != "verification_id"}) != identifier
                    or request["task_id"] not in record.get("consumer_task_ids",[])):
                _fail("HITL_VERIFICATION_INVALID", "verifications", "Verification does not bind this prepared task")
        return copy.deepcopy(request), sorted(required), sorted(old), sorted(structured), copy.deepcopy(checks)

    def _reserve(self, name, task_id, now, *, request_id=None, prior_request_id=None, replaces_decision_id=None):
        self._sequence += 1
        request_id = request_id or f"{self.ledger.identity['episode_id']}:hitl:{self._sequence:06d}"
        _text(request_id,"request_id")
        if request_id in self._questions:
            _fail("HITL_REQUEST_CONFLICT", "request_id", "Question ID already exists")
        cycle = self._cycles.get(name,0)+1
        self._cycles[name] = cycle
        question = {"contract_schema":"chain-fact-hitl/v1",**self._common(),"task_id":task_id,"request_id":request_id,
            "decision_definition_id":name,"cycle_no":cycle,"status":"PREPARING","current_revision":0,
            "revision_ids":[],"requested_at":now}
        self._questions[request_id] = question
        self._metadata[request_id] = {"prior_request_id":prior_request_id,"replaces_decision_id":replaces_decision_id,
            "decision_id":None,"effect":None,"opened_at":None,"deferred_at":None,"reason":None,
            "ready":False,"resumed_by":None}
        return question

    def prepare(self, command, *, now, logical_request_id=None, prior_request_id=None,
                replaces_decision_id=None, source_records=None):
        self._check_time(now)
        request, _, _, _, _ = self._command(command,now)
        name = request["task"]["decision"]
        if logical_request_id is None and replaces_decision_id is None and prior_request_id is None and request["execution_id"] in self._execution_questions:
            return copy.deepcopy(self._questions[self._execution_questions[request["execution_id"]]])
        if replaces_decision_id is not None:
            previous = self.ledger.decisions.get(replaces_decision_id,{}).get("record")
            if not previous or previous["decision_definition_id"] != name or self._d["decisions"][name]["options"][previous["decision"]]["effect"] != "commit":
                _fail("HITL_REDECISION_INVALID", "replaces_decision_id", "Reconsideration requires an explicit completed decision")
        if logical_request_id in self._questions:
            question = self._questions[logical_request_id]
            if (question["status"] not in {"PREPARING","PREPARATION_FAILED","OPEN"}
                    or question["decision_definition_id"] != name or question["task_id"] != request["task_id"]):
                _fail("HITL_REQUEST_CONFLICT", "logical_request_id", "Reserved question does not match prepared work")
            metadata = self._metadata[logical_request_id]
            if ((prior_request_id is not None and metadata["prior_request_id"] != prior_request_id)
                    or (replaces_decision_id is not None and metadata["replaces_decision_id"] != replaces_decision_id)):
                _fail("HITL_REQUEST_CONFLICT", "logical_request_id", "Reserved question belongs to another clinical cycle")
        else:
            question = self._reserve(name,request["task_id"],now,request_id=logical_request_id,
                prior_request_id=prior_request_id,replaces_decision_id=replaces_decision_id)
        identifier = question["request_id"]
        self.revise(identifier,command,now=now,source_records=source_records,ready=True)
        self._execution_questions[request["execution_id"]] = identifier
        return copy.deepcopy(question)

    def reserve_preparation(self, task_id, decision_definition_id, *, now):
        """Expose a failed/pending preparation without opening a question."""
        self._check_time(now);_text(task_id,"task_id")
        if decision_definition_id not in self._d["decisions"]:
            _fail("HITL_CONTRACT_INVALID","decision_definition_id","Unknown decision definition")
        for question in self._questions.values():
            if question["task_id"]==task_id and question["status"] in {"PREPARING","PREPARATION_FAILED"}:
                return copy.deepcopy(question)
        question=self._reserve(decision_definition_id,task_id,now)
        self._event("HITL_PREPARING",question["request_id"],now)
        return copy.deepcopy(question)

    @staticmethod
    def _verification_meaning(checks, scope):
        return sorted([{"source":record["source"],"coverage":[row for row in record["coverage"] if row["field"] in scope],
            "errors":[{"scope":sorted(set(error["scope"]) & set(scope)),"code":error["code"]}
                      for error in record["errors"] if set(error["scope"]) & set(scope)]}
            for record in checks.values()],key=canonical_hash)

    def _change_set(self, prior, snapshot, checks):
        before = {} if prior is None else semantic_input(prior["snapshot"])
        current = semantic_input(snapshot)
        result = []
        for name, fact in current.items():
            old = before.get(name)
            if old == fact: continue
            validity = fact["validity"]
            if validity == "CONFLICT": kind = "CONFLICT"
            elif validity in {"RETRACTED","ERROR","INVALIDATED"}: kind = "RETRACTED"
            elif validity in {"UNKNOWN","PENDING","UNCONFIRMED"}: kind = "UNCONFIRMED"
            elif old is None or old["validity"] != "KNOWN": kind = "ADDED"
            else:
                kind = "UPDATED"
                keys={(r["system"],r["record_id"],r["version"]) for r in fact["source_refs"]}
                for item in self.ledger.source_history:
                    ref=item["record"]["source_ref"]
                    if ((ref["system"],ref["record_id"],ref["version"]) in keys
                            and (item["record"].get("revision") or {}).get("kind") == "CORRECTION"):
                        kind = "CORRECTED"
            result.append({"field":name,"kind":kind,"prior_source_refs":copy.deepcopy(old["source_refs"] if old else []),
                           "current_source_refs":copy.deepcopy(fact["source_refs"])})
        if not result and prior is not None:
            unconfirmed = {row["field"] for record in checks.values() for row in record["coverage"]
                           if row["verification"] == "UNCONFIRMED" and row["field"] in current}
            result = [{"field":name,"kind":"UNCONFIRMED","prior_source_refs":current[name]["source_refs"],
                       "current_source_refs":current[name]["source_refs"]} for name in sorted(unconfirmed)]
        return result

    def revise(self, request_id, command, *, now, source_records=None, ready=True, reason=None):
        self._check_time(now)
        if type(ready) is not bool: _fail("HITL_CONTRACT_INVALID","ready","Expected a readiness flag")
        question = self._questions.get(request_id)
        if question is None or question["status"] in {"DECIDED","CLOSED"}:
            _fail("HITL_NOT_OPEN", "request_id", "Completed evidence cannot be revised")
        request, required, references, structured, checks = self._command(command,now,ready=ready)
        if question["task_id"] != request["task_id"] or question["decision_definition_id"] != request["task"]["decision"]:
            _fail("HITL_REQUEST_CONFLICT", "request_id", "Prepared work belongs to another question")
        prior = self.current(request_id)
        if prior:
            references = sorted(set(references) | (set(prior["required_result_ids"])-set(required)) |
                                (set(prior["reference_result_ids"])-set(required)))
        revision = question["current_revision"]+1
        document_request={"request_id":request_id,"evidence_snapshot_id":request["snapshot"]["snapshot_id"],
            "evidence_revision":revision,"dependencies":request["dependencies"]}
        records = getattr(self.ledger,"source_documents_history",{}) if source_records is None else source_records
        documents = freeze_source_documents(document_request,request["snapshot"],records,frozen_at=now)
        meaning = {"snapshot":semantic_input(request["snapshot"]),"required":required,"reference":references,
            "structured":structured,"checks":self._verification_meaning(checks,request["scope"]),"documents":documents["entries"],"ready":ready}
        meaning_hash = canonical_hash(meaning)
        self._commands[request_id] = copy.deepcopy(command)
        self._metadata[request_id].update(ready=ready,reason=reason)
        if self._meaning_hashes.get(request_id) == meaning_hash:
            if ready and question["status"] != "OPEN":
                question["status"]="OPEN"; self._event("HITL_OPENED",request_id,now)
            return self.current(request_id)
        evidence={"contract_schema":"chain-fact-evidence/v1",**self._common(),"request_id":request_id,
            "revision":revision,"snapshot":copy.deepcopy(request["snapshot"]),"structured_context_result_ids":structured,
            "required_result_ids":required,"reference_result_ids":references,"dependencies":copy.deepcopy(request["dependencies"]),
            "source_documents":documents,"verification_ids":copy.deepcopy(request["verification_ids"]),
            "change_set":self._change_set(prior,request["snapshot"],checks),"created_at":now}
        evidence["evidence_id"]=canonical_hash(evidence)
        self._evidence[evidence["evidence_id"]]=evidence
        self._meaning_hashes[request_id]=meaning_hash
        question["current_revision"]=revision;question["revision_ids"].append(evidence["evidence_id"])
        question["status"]="OPEN" if ready else "PREPARING"
        if ready:
            self._metadata[request_id]["opened_at"] = self._metadata[request_id]["opened_at"] or now
            self._latest[question["decision_definition_id"]]=request_id
            prior_id=self._metadata[request_id]["prior_request_id"]
            if prior_id:
                self._metadata[prior_id]["resumed_by"]=request_id
        for result in [*command.get("prerequisite_results",{}).values(),*command.get("reference_results",{}).values()]:
            self._result_store[result["execution_id"]]=copy.deepcopy(result)
        self._event("HITL_EVIDENCE_REVISED",request_id,now)
        return copy.deepcopy(evidence)

    def mark_preparing(self, request_id, *, now, code="EVIDENCE_UPDATE_PENDING"):
        self._check_time(now)
        question=self._questions[request_id]
        if question["status"] in {"DECIDED","CLOSED"}: return copy.deepcopy(question)
        if question["status"] != "PREPARING" or self._metadata[request_id]["reason"] != code:
            question["status"]="PREPARING";self._metadata[request_id].update(ready=False,reason=code)
            self._event("HITL_PREPARING",request_id,now)
        return copy.deepcopy(question)

    def preparation_failed(self, request_id, *, now, code="HITL_PREPARATION_FAILED"):
        self._check_time(now)
        question=self._questions[request_id]
        if question["status"] in {"DECIDED","CLOSED"}: return copy.deepcopy(question)
        if question["status"] != "PREPARATION_FAILED" or self._metadata[request_id]["reason"] != code:
            question["status"]="PREPARATION_FAILED";self._metadata[request_id].update(ready=False,reason=code)
            self._event("HITL_PREPARATION_FAILED",request_id,now)
        return copy.deepcopy(question)

    def close(self, request_id, *, now, reason):
        self._check_time(now);_text(reason,"reason")
        question=self._questions[request_id]
        if question["status"] in {"DECIDED","CLOSED"}:return copy.deepcopy(question)
        question["status"]="CLOSED";self._metadata[request_id].update(ready=False,reason=reason)
        self._event("HITL_CLOSED",request_id,now)
        return copy.deepcopy(question)

    def _receipt(self, decision, question, *, status, code, seq=None):
        return {"contract_schema":"chain-fact-hitl-receipt/v1",**self._common(),
            "decision_id":decision["decision_id"],"request_id":decision["request_id"],"status":status,"code":code,
            "current_revision":question["current_revision"] if question else None,"accepted_reducer_seq":seq}

    def submit(self, decision, *, now, eligible=True, pending=False):
        self._check_time(now)
        check_json(decision)
        _closed(decision,{"contract_schema",*_COMMON,"decision_id","request_id","evidence_revision","evidence_id",
            "evidence_snapshot_id","decision","actor","confirmed_items","evidence_viewed","comment","recorded_at"},"decision")
        self._identity(decision)
        if decision["contract_schema"] != "chain-fact-hitl-decision/v1":
            _fail("HITL_CONTRACT_INVALID","contract_schema","Unsupported HITL decision contract")
        for key in ("decision_id","request_id","decision","evidence_id","evidence_snapshot_id"):_text(decision[key],key)
        for key in ("evidence_id","evidence_snapshot_id"):_hash(decision[key],key)
        if type(decision["evidence_revision"]) is not int or decision["evidence_revision"]<1:
            _fail("HITL_CONTRACT_INVALID","evidence_revision","Expected a positive evidence revision")
        _closed(decision["actor"],{"role","staff_id"},"actor")
        for key,value in decision["actor"].items():_text(value,"actor."+key)
        _strings(decision["confirmed_items"],"confirmed_items");_strings(decision["evidence_viewed"],"evidence_viewed")
        if not isinstance(decision["comment"],str) or timestamp(decision["recorded_at"])>timestamp(now):
            _fail("HITL_CONTRACT_INVALID","recorded_at","Invalid response reason/time")
        if type(eligible) is not bool or type(pending) is not bool:
            _fail("HITL_CONTRACT_INVALID","eligibility","Expected trusted boolean processing flags")
        prior=self._submissions.get(decision["decision_id"])
        if prior:
            if prior["decision"] != decision:_fail("DECISION_ID_CONFLICT","decision_id","Response ID has another body")
            return copy.deepcopy(prior["receipt"])
        question=self._questions.get(decision["request_id"])
        evidence=self.current(question["request_id"]) if question else None
        code,status="HITL_ACCEPTED","ACCEPTED"
        if question is None:code,status="HITL_REQUEST_NOT_FOUND","REJECTED"
        elif question["status"] in {"DECIDED","CLOSED"}:code,status="HITL_NOT_OPEN","REJECTED"
        elif pending:code,status="EVIDENCE_UPDATE_PENDING","PREPARING"
        elif not eligible:code,status="WORK_NOT_ELIGIBLE","REJECTED"
        elif question["status"] in {"PREPARING","PREPARATION_FAILED"}:code,status="HITL_PREPARATION_FAILED" if question["status"]=="PREPARATION_FAILED" else "EVIDENCE_UPDATE_PENDING","PREPARING"
        elif question["status"] != "OPEN":code,status="HITL_NOT_OPEN","REJECTED"
        elif (decision["evidence_revision"] != question["current_revision"] or decision["evidence_id"] != evidence["evidence_id"]
              or decision["evidence_snapshot_id"] != evidence["snapshot"]["snapshot_id"]):code,status="EVIDENCE_REVISION_MISMATCH","REJECTED"
        else:
            definition=self._d["decisions"][question["decision_definition_id"]]
            policy=self._d["policy"]["requirements"][definition["requirement_ref"]]
            option=definition["options"].get(decision["decision"])
            allowed_viewed={evidence["evidence_id"],*evidence["required_result_ids"],*evidence["structured_context_result_ids"]}
            if policy["kind"] != "hitl" or decision["actor"]["role"] not in policy["roles"]:code,status="ROLE_NOT_AUTHORIZED","REJECTED"
            elif option is None:code,status="DECISION_NOT_ALLOWED","REJECTED"
            elif self._metadata[question["request_id"]]["replaces_decision_id"] and option["effect"] == "defer":code,status="REDECISION_DEFER_NOT_ALLOWED","REJECTED"
            elif not set(policy["confirmations"].get(decision["decision"],[])) <= set(decision["confirmed_items"]):code,status="CONFIRMATION_MISSING","REJECTED"
            elif evidence["evidence_id"] not in decision["evidence_viewed"] or not set(decision["evidence_viewed"]) <= allowed_viewed:code,status="EVIDENCE_NOT_VIEWED","REJECTED"
            elif timestamp(decision["recorded_at"]) < timestamp(evidence["created_at"]):code,status="RESPONSE_PRECEDES_EVIDENCE","REJECTED"
        sequence=None
        if status == "ACCEPTED":
            name=question["decision_definition_id"]
            accepted={"contract_schema":"chain-fact-accepted-decision/v1",**self._common(),
                **{key:copy.deepcopy(decision[key]) for key in ("decision_id","request_id","decision","actor","evidence_snapshot_id","evidence_id","evidence_revision","recorded_at")},
                "decision_definition_id":name,"policy_rule":{"rule_id":self._d["decisions"][name]["requirement_ref"],"version":self._d["policy"]["version"]}}
            stored=self.ledger.record_decision(accepted,at=now);sequence=stored["accepted_reducer_seq"]
            effect=self._d["decisions"][name]["options"][decision["decision"]]["effect"]
            question["status"]="DECIDED"
            self._metadata[question["request_id"]].update(decision_id=decision["decision_id"],effect=effect,
                deferred_at=now if effect == "defer" else None,reason=decision["comment"],ready=False)
            self._event("HITL_DECISION_ACCEPTED",question["request_id"],now)
        receipt=self._receipt(decision,question,status=status,code=code,seq=sequence)
        self._submissions[decision["decision_id"]]={"decision":copy.deepcopy(decision),"receipt":receipt}
        self._clock=now
        return copy.deepcopy(receipt)

    def _resume(self, command, *, now, trusted_timer=False):
        self._check_time(now);check_json(command)
        _closed(command,{"contract_schema",*_COMMON,"command_id","prior_request_id","actor","requested_at"},"resume")
        self._identity(command)
        if command["contract_schema"] != "chain-fact-hitl-resume/v1":_fail("HITL_CONTRACT_INVALID","contract_schema","Unsupported resume contract")
        for key in ("command_id","prior_request_id"):_text(command[key],key)
        _closed(command["actor"],{"role","staff_id"},"actor")
        for key,value in command["actor"].items():_text(value,"actor."+key)
        if timestamp(command["requested_at"])>timestamp(now):_fail("INVALID_TIME","requested_at","Resume is in the future")
        existing=self._resumes.get(command["command_id"])
        if existing:
            if existing["command"] != command:_fail("COMMAND_ID_CONFLICT","command_id","Resume ID has another body")
            result=copy.deepcopy(existing["receipt"]);result["duplicate"]=True
            if result["request_id"] in self._questions:
                current=self._questions[result["request_id"]]["status"]
                result["status"]="FAILED" if current=="PREPARATION_FAILED" else current
            return result
        prior_id=command["prior_request_id"];prior=self._questions.get(prior_id)
        if not prior or prior["status"] != "DECIDED" or self._metadata[prior_id]["effect"] != "defer":
            result={"status":"REJECTED","code":"HITL_NOT_DEFERRED","prior_request_id":prior_id,"request_id":None,"cycle_no":None,"duplicate":False}
        else:
            policy=self._d["policy"]["requirements"][self._d["decisions"][prior["decision_definition_id"]]["requirement_ref"]]
            if not trusted_timer and command["actor"]["role"] not in policy["roles"]:
                result={"status":"REJECTED","code":"ROLE_NOT_AUTHORIZED","prior_request_id":prior_id,"request_id":None,"cycle_no":None,"duplicate":False}
            else:
                target=self._resume_targets.get(prior_id)
                if target is None:
                    question=self._reserve(prior["decision_definition_id"],prior["task_id"],now,prior_request_id=prior_id)
                    target=question["request_id"];self._resume_targets[prior_id]=target
                    self._event("HITL_RESUME_PREPARING",target,now)
                question=self._questions[target]
                result={"status":"FAILED" if question["status"]=="PREPARATION_FAILED" else question["status"],
                    "code":"HITL_RESUME_RESERVED","prior_request_id":prior_id,"request_id":target,
                    "cycle_no":question["cycle_no"],"duplicate":False}
        self._resumes[command["command_id"]]={"command":copy.deepcopy(command),"receipt":copy.deepcopy(result)}
        self._clock=now
        return result

    def request_resume(self, command, *, now):
        return self._resume(command,now=now)

    def resume_from_timer(self, prior_request_id, *, now, timer_id):
        _text(timer_id,"timer_id")
        command={"contract_schema":"chain-fact-hitl-resume/v1",**self._common(),
            "command_id":"timer:"+timer_id,"prior_request_id":prior_request_id,
            "actor":{"role":"INTERNAL_TIMER","staff_id":timer_id},"requested_at":now}
        existing=self._resumes.get(command["command_id"])
        if existing:command=existing["command"]
        return self._resume(command,now=now,trusted_timer=True)

    def export_state(self):
        result={"contract_schema":"chain-fact-hitl-owner/v1",**self._common(),"definition_hash":self.ledger.definition_hash,
            "questions":self.requests,"evidence":self.evidence,"metadata":copy.deepcopy(self._metadata),
            "commands":copy.deepcopy(self._commands),"submissions":copy.deepcopy(self._submissions),
            "resumes":copy.deepcopy(self._resumes),"resume_targets":copy.deepcopy(self._resume_targets),
            "cycles":copy.deepcopy(self._cycles),"result_store":copy.deepcopy(self._result_store),
            "execution_questions":copy.deepcopy(self._execution_questions),"meaning_hashes":copy.deepcopy(self._meaning_hashes),
            "latest":copy.deepcopy(self._latest),"feed":copy.deepcopy(self._feed),"sequence":self._sequence,
            "view_seq":self._view_seq,"clock":self._clock}
        result["state_hash"]=canonical_hash(result)
        return result

    @classmethod
    def from_state(cls, state, *, ledger):
        try:return cls._restore(state,ledger=ledger)
        except ConfigError:raise
        except (KeyError,TypeError,ValueError,AttributeError):_fail("STATE_INTEGRITY_MISMATCH","hitl","Malformed saved HITL state")

    @classmethod
    def _restore(cls,state,*,ledger):
        check_json(state)
        fields={"contract_schema",*_COMMON,"definition_hash","questions","evidence","metadata","commands","submissions","resumes",
            "resume_targets","cycles","result_store","execution_questions","meaning_hashes","latest","feed","sequence","view_seq","clock","state_hash"}
        _closed(state,fields,"hitl_state")
        if (state["contract_schema"] != "chain-fact-hitl-owner/v1" or state["definition_hash"] != ledger.definition_hash
                or state["state_hash"] != canonical_hash({k:v for k,v in state.items() if k != "state_hash"})):
            _fail("STATE_INTEGRITY_MISMATCH","hitl","Saved HITL definition/hash differs")
        owner=cls(ledger.export_state()["constructor"]["definition"],ledger);owner._identity(state)
        if set(state["metadata"]) != set(state["questions"]):
            _fail("STATE_INTEGRITY_MISMATCH","metadata","Saved question metadata is incomplete")
        for key,question in state["questions"].items():
            _closed(question,_QUESTION,"question");owner._identity(question)
            if (question["request_id"] != key or question["status"] not in _STATUSES
                    or question["decision_definition_id"] not in owner._d["decisions"]
                    or type(question["cycle_no"]) is not int or question["cycle_no"]<1
                    or type(question["current_revision"]) is not int or question["current_revision"] != len(question["revision_ids"])):
                _fail("STATE_INTEGRITY_MISMATCH","question","Saved question identity/revision differs")
            for revision,identifier in enumerate(question["revision_ids"],1):
                evidence=state["evidence"][identifier]
                _closed(evidence,{"contract_schema",*_COMMON,"request_id","revision","evidence_id","snapshot",
                    "structured_context_result_ids","required_result_ids","reference_result_ids","dependencies",
                    "source_documents","verification_ids","change_set","created_at"},"evidence")
                owner._identity(evidence)
                if (evidence["evidence_id"] != identifier or evidence["request_id"] != key or evidence["revision"] != revision
                        or evidence["contract_schema"] != "chain-fact-evidence/v1"
                        or canonical_hash({k:v for k,v in evidence.items() if k != "evidence_id"}) != identifier):
                    _fail("STATE_INTEGRITY_MISMATCH","evidence","Saved evidence identity/hash differs")
                snapshot=validate_snapshot(evidence["snapshot"])
                if ledger.frozen_snapshot(snapshot["snapshot_id"]) != snapshot:
                    _fail("STATE_INTEGRITY_MISMATCH","snapshot","Saved evidence is not ledger frozen data")
                docreq={"request_id":key,"evidence_snapshot_id":snapshot["snapshot_id"],"evidence_revision":revision,"dependencies":evidence["dependencies"]}
                validate_source_documents(evidence["source_documents"],docreq,snapshot)
                if evidence["dependencies"] != _union([ref for fact in snapshot["facts"].values() for ref in fact["source_refs"]]):
                    _fail("STATE_INTEGRITY_MISMATCH","dependencies","Evidence changed its fixed source scope")
                if not timestamp(question["requested_at"]) <= timestamp(evidence["created_at"]) <= timestamp(state["clock"]):
                    _fail("STATE_INTEGRITY_MISMATCH","created_at","Evidence time differs from its question")
                for field in ("required_result_ids","reference_result_ids","structured_context_result_ids","verification_ids"):
                    _strings(evidence[field],field)
                if (set(evidence["required_result_ids"]) & set(evidence["reference_result_ids"])
                        or not set(evidence["structured_context_result_ids"]) <= set(evidence["required_result_ids"])):
                    _fail("STATE_INTEGRITY_MISMATCH","result_ids","Evidence result roles overlap")
            meta=state["metadata"][key]
            _closed(meta,{"prior_request_id","replaces_decision_id","decision_id","effect","opened_at","deferred_at","reason","ready","resumed_by"},"metadata")
            if type(meta["ready"]) is not bool or (question["status"]=="OPEN") != meta["ready"]:
                _fail("STATE_INTEGRITY_MISMATCH","ready","Question readiness differs from its status")
            if question["status"]=="DECIDED":
                accepted=ledger.decisions.get(meta["decision_id"],{}).get("record")
                if (not accepted or accepted["request_id"] != key or accepted["decision_definition_id"] != question["decision_definition_id"]
                        or meta["effect"] != owner._d["decisions"][question["decision_definition_id"]]["options"][accepted["decision"]]["effect"]
                        or (meta["effect"]=="defer") != (meta["deferred_at"] is not None)):
                    _fail("STATE_INTEGRITY_MISMATCH","metadata","Question decision differs from the ledger")
        if type(state["sequence"]) is not int or state["sequence"] < len(state["questions"]) or state["view_seq"] != len(state["feed"]):
            _fail("STATE_INTEGRITY_MISMATCH","sequence","Saved HITL sequence differs")
        for decision_id,item in state["submissions"].items():
            if item["decision"]["decision_id"] != decision_id or item["receipt"]["decision_id"] != decision_id:
                _fail("STATE_INTEGRITY_MISMATCH","decision","Saved response links differ")
            if item["receipt"]["status"] == "ACCEPTED":
                accepted=ledger.decisions.get(decision_id)
                if not accepted or accepted["accepted_reducer_seq"] != item["receipt"]["accepted_reducer_seq"]:
                    _fail("STATE_INTEGRITY_MISMATCH","decision","Accepted response is missing from the ledger")
                for field in ("request_id","decision","actor","evidence_snapshot_id","evidence_id","evidence_revision","recorded_at"):
                    if item["decision"][field] != accepted["record"][field]:
                        _fail("STATE_INTEGRITY_MISMATCH","decision","Saved response differs from its accepted ledger record")
            elif item["receipt"]["status"] not in {"PREPARING","REJECTED"} or item["receipt"]["accepted_reducer_seq"] is not None:
                _fail("STATE_INTEGRITY_MISMATCH","receipt","Saved response status/sequence differs")
        if [event.get("view_seq") for event in state["feed"]] != list(range(1,state["view_seq"]+1)):
            _fail("STATE_INTEGRITY_MISMATCH","view_seq","Saved display sequence is not contiguous")
        for event in state["feed"]:
            if event["request_id"] not in state["questions"] or timestamp(event["at"]) > timestamp(state["clock"]):
                _fail("STATE_INTEGRITY_MISMATCH","feed","Saved display event has invalid identity/time")
        for name,key in state["latest"].items():
            if key not in state["questions"] or state["questions"][key]["decision_definition_id"] != name:
                _fail("STATE_INTEGRITY_MISMATCH","latest","Saved current decision cycle differs")
        for prior_id,target in state["resume_targets"].items():
            if (prior_id not in state["questions"] or target not in state["questions"]
                    or state["metadata"][target]["prior_request_id"] != prior_id
                    or state["questions"][target]["cycle_no"] <= state["questions"][prior_id]["cycle_no"]):
                _fail("STATE_INTEGRITY_MISMATCH","resume_targets","Saved resume points to another cycle")
        for attribute,key in (("_questions","questions"),("_evidence","evidence"),("_metadata","metadata"),("_commands","commands"),
                ("_submissions","submissions"),("_resumes","resumes"),("_resume_targets","resume_targets"),("_cycles","cycles"),
                ("_result_store","result_store"),("_execution_questions","execution_questions"),("_meaning_hashes","meaning_hashes"),
                ("_latest","latest"),("_feed","feed"),("_sequence","sequence"),("_view_seq","view_seq"),("_clock","clock")):
            setattr(owner,attribute,copy.deepcopy(state[key]))
        return owner
