"""Serialized Local Episode reducer joining facts, work, HITL and timers.

No console, network, plugin loading or actual clock lives here. A runner feeds
validated source Events, completions, human responses and timer firings.
Temporal registration lives in the separate temporal_workflow module.
"""
from __future__ import annotations

import copy

from .compiler import canonical_hash
from .errors import ConfigError
from .fact_ledger import FactLedger
from .hitl import HITLOwner
from .projections import StateProjector, validate_projection
from .runtime_contracts import canonical_source_refs, timestamp
from .shape import check_json
from .task_contracts import semantic_input, work_input_fingerprint
from .tasks import TaskEngine
from .timers import TimerOwner


def _fail(code,path,message):
    raise ConfigError(code,path,message)


class EpisodeEngine:
    """Local coordinator; mutation APIs must run in one recorded order."""
    def __init__(self,definition,ledger):
        self._d=copy.deepcopy(definition)
        self.ledger=ledger
        self.tasks=TaskEngine(definition,ledger)
        self.hitl=HITLOwner(definition,ledger)
        self.timers=TimerOwner(definition,identity=ledger.identity,engine_run_id=ledger.engine_run_id,run_pin=ledger.run_pin)
        self._projector=StateProjector(definition)
        life=definition.get("lifecycle",{})
        if (set(life.get("states",[]))!={"ACTIVE","COMPLETED"} or life.get("initial")!="ACTIVE"
                or life.get("completion")!={"state":"COMPLETED","wait_for":"owned_work","after_completion":"reject"}):
            _fail("LIFECYCLE_APPROVAL_REQUIRED","lifecycle","This Local profile activates only approved ACTIVE/COMPLETED semantics")
        projection=life.get("projection",{})
        if projection.get("selection")!="highest_true_priority" or projection.get("fallback") not in life["states"]:
            _fail("LIFECYCLE_INVALID","lifecycle.projection","Expected explicit selection and fallback")
        seen=set()
        for rule in projection.get("rules",[]):
            if (rule["state"] not in life["states"] or type(rule["priority"]) is not int or rule["priority"] in seen):
                _fail("LIFECYCLE_INVALID","lifecycle.projection","Expected declared lifecycle and unique priorities")
            seen.add(rule["priority"]);self._projector._validate_expression(rule["when"])
            for name in rule["requirement_refs"]:
                if name not in self._projector._predicates:
                    _fail("LIFECYCLE_INVALID","requirement_refs","Lifecycle conditions require declared predicates")
        self._clock=ledger.processed_at
        self._projection=None;self._lifecycle="ACTIVE";self._termination_phase="RUNNING"
        self._entries={};self._entry_history=[];self._changes=[];self._view_seq=0;self._outbox=[]
        self._prepared={};self._question_by_task={};self._refresh_keys={};self._next_question={}
        self._hitl_view_seq=0;self._timer_status={}
        self._published_questions=set()

    @property
    def projection(self):return copy.deepcopy(self._projection)

    @property
    def lifecycle(self):return self._lifecycle

    @property
    def termination_phase(self):return self._termination_phase

    @property
    def closure_set(self):return self.tasks.closure_set

    @property
    def next_deadline(self):
        values = [item for item in (self.ledger.next_deadline, self.tasks.next_deadline) if item]
        return min(values, key=timestamp) if values else None

    def _at(self,now):
        if timestamp(now)<max(timestamp(self._clock),timestamp(self.ledger.processed_at)):
            _fail("INVALID_TIME","now","Episode processing time cannot move backwards")
        self._clock=now

    def _change(self,kind,data,now):
        self._view_seq+=1
        self._changes.append({"view_seq":self._view_seq,"kind":kind,"data":copy.deepcopy(data),"at":now})

    def _state_cells(self):
        return {name:{"entered":{"value":True,"validity":"KNOWN","source_refs":[]},
            "generation":{"value":value["epoch"],"validity":"KNOWN","source_refs":[]}}
            for name,value in self._entries.items()}

    def _projection_and_lifecycle(self,now):
        self.tasks.refresh(now=now,lifecycle=self._lifecycle,state_entries=self._state_cells())
        projection=self._projector.project_ledger(self.ledger,evaluated_at=now,
            results=self.tasks.results_for_projection(),state_entries=self._state_cells(),
            pending_changes=self.ledger.dependency_pending_changes)
        if self._projection is None or self._projection["state"]!=projection["state"]:
            name=projection["state"];epoch=self._entries.get(name,{}).get("epoch",0)+1
            entry={"id":name,"epoch":epoch,"entered_at":now,"active":True}
            for value in self._entries.values():value["active"]=False
            self._entries[name]=entry
            self._entry_history.append({**copy.deepcopy(entry),"projection_id":projection["projection_id"]})
            self._change("STATE_ENTERED",entry,now)
        self._projection=projection
        if self._termination_phase!="RUNNING":return
        rules=sorted(self._d["lifecycle"]["projection"]["rules"],key=lambda r:(-r["priority"],r["id"]))
        selected=None
        for rule in rules:
            proofs=[self.tasks.evaluate(rule["when"],now=now),*[self.tasks.evaluate(
                {"op":"requirement","requirement_id":name},now=now) for name in rule["requirement_refs"]]]
            if all(proof["truth"]=="TRUE" for proof in proofs):selected=rule["state"];break
        selected=selected or self._d["lifecycle"]["projection"]["fallback"]
        if selected=="COMPLETED":
            # Capture allowed ending work before disabling ordinary new work.
            self.tasks.start_closure(now=now)
            self._lifecycle="COMPLETED";self._termination_phase="DRAINING"
            tasks=self.tasks.tasks
            for request_id,q in self.hitl.requests.items():
                if q["status"] in {"OPEN","PREPARING","PREPARATION_FAILED"}:
                    self.hitl.close(request_id,now=now,reason="EPISODE_CLOSING")
                if q["task_id"] in tasks:
                    self.tasks.block_hitl_task(q["task_id"],now=now,reason="EPISODE_CLOSING")
            self._change("EPISODE_DRAINING",self.closure_set,now)

    def _related_pending(self,evidence):
        scope=set(evidence["snapshot"]["facts"])
        refs={(r["system"],r["record_id"]) for r in evidence["dependencies"]}
        unknown={name for name,fact in evidence["snapshot"]["facts"].items() if fact["validity"] not in {"KNOWN","ABSENT_CONFIRMED"}}
        return [item for item in self.ledger.dependency_pending_changes if scope & set(item["fields"]) and (
            (item["source_ref"]["system"],item["source_ref"]["record_id"]) in refs or unknown & set(item["fields"]))]

    def _reference_results(self,task_id):
        return self.tasks.reference_results_for(task_id)

    def _display_command(self,request_id,now,*,unconfirmed_fields=()):
        command=copy.deepcopy(self._prepared[request_id]);request=command["request"]
        request["snapshot"]=self.ledger.freeze_evidence_snapshot(request["scope"],at=now,unconfirmed_fields=list(unconfirmed_fields))
        request["dependencies"]=canonical_source_refs([r for f in request["snapshot"]["facts"].values() for r in f["source_refs"]])
        task=self.tasks.task_record(request["task_id"])
        ids=self.tasks._prerequisites(task) or []
        request["prerequisite_result_ids"]=ids
        command["prerequisite_results"]={key:self.tasks.execution_record(key)["result"] for key in ids}
        command["reference_results"]=self._reference_results(request["task_id"])
        checks={}
        for value in command.get("verifications",{}).values():
            value=copy.deepcopy(value)
            fields=sorted({row["field"] for row in value["coverage"]}&set(request["scope"]))
            if not fields:continue
            value["coverage"]=[{"field":field,"verification":"UNCONFIRMED"} for field in fields]
            value["sharing_epoch"]=None
            value["errors"]=[{"scope":fields,"code":"EVIDENCE_UPDATE_PENDING","retryable":False}]
            value.pop("verification_id");value["verification_id"]=canonical_hash(value)
            checks[value["verification_id"]]=value
        command["verifications"]=checks
        request["verification_ids"]=sorted(checks);request["evaluated_at"]=now
        request["input_fingerprint"]=work_input_fingerprint(request)
        return command

    def _refresh_open(self,now):
        if self._lifecycle!="ACTIVE":return
        for request_id,q in self.hitl.requests.items():
            if q["status"] not in {"OPEN","PREPARING","PREPARATION_FAILED"} or not q["revision_ids"]:continue
            task=self.tasks.task_record(q["task_id"])
            evidence=self.hitl.current(request_id)
            pending=self._related_pending(evidence)
            rule=self.tasks.rule_evaluation(task["rule_id"].split("::after::",1)[0])
            if rule.get("stop_when",{}).get("truth")=="TRUE" or rule.get("when",{}).get("truth")=="FALSE":
                self.hitl.close(request_id,now=now,reason="WORK_NOT_ELIGIBLE")
                self.tasks.block_hitl_task(q["task_id"],now=now,reason="WORK_NOT_ELIGIBLE")
                continue
            raw=self.ledger.snapshot(list(evidence["snapshot"]["facts"]))
            current_ids=self.tasks._prerequisites(task)
            key=canonical_hash({"input":semantic_input(raw),"prerequisites":current_ids,
                "pending":[{k:v for k,v in item.items() if k in {"event_id","status"}} for item in pending]})
            if request_id not in self._refresh_keys:
                self._refresh_keys[request_id]=key
            if self._refresh_keys[request_id]==key:continue
            self._refresh_keys[request_id]=key
            affected=sorted({name for item in pending for name in item["fields"]}&set(raw["facts"]))
            command=self._display_command(request_id,now,unconfirmed_fields=affected)
            self.hitl.revise(request_id,command,now=now,ready=False,
                source_records=self.ledger.source_documents_history,reason="EVIDENCE_UPDATE_PENDING")
            self.tasks.request_hitl_preparation(q["task_id"],now=now)

    def _consume(self,commands,now):
        for command in commands:
            if command["kind"]!="hitl_prepared":
                self._outbox.append(copy.deepcopy(command));continue
            task_id=command["request"]["task_id"]
            existing=self._question_by_task.get(task_id)
            target=self._next_question.get(task_id,{})
            if target:
                logical_id=target.get("request_id")
                prior=target.get("prior_request_id")
            elif existing and self.hitl.requests[existing]["status"] in {"OPEN","PREPARING","PREPARATION_FAILED"}:
                logical_id,prior=existing,None
            elif existing:
                self.tasks.block_hitl_task(task_id,now=now,reason="QUESTION_CYCLE_COMPLETE");continue
            else:logical_id,prior=None,None
            command=copy.deepcopy(command)
            pending=[p for p in self.ledger.dependency_pending_changes if set(p["fields"])&set(command["request"]["scope"])]
            failed_fields=sorted({name for item in pending if item["status"]=="FAILED" for name in item["fields"]}
                                 &set(command["request"]["scope"]))
            if failed_fields:
                request=command["request"]
                request["snapshot"]=self.ledger.freeze_evidence_snapshot(request["scope"],at=now,unconfirmed_fields=failed_fields)
                request["dependencies"]=canonical_source_refs([r for f in request["snapshot"]["facts"].values() for r in f["source_refs"]])
                request["input_fingerprint"]=work_input_fingerprint(request)
            command["reference_results"]=self._reference_results(task_id)
            question=self.hitl.prepare(command,now=now,logical_request_id=logical_id,prior_request_id=prior,
                replaces_decision_id=target.get("replaces_decision_id"),source_records=self.ledger.source_documents_history)
            identifier=question["request_id"]
            self._prepared[identifier]=command;self._question_by_task[task_id]=identifier
            self._next_question.pop(task_id,None)
            evidence=self.hitl.current(identifier)
            self._refresh_keys[identifier]=canonical_hash({"input":semantic_input(self.ledger.snapshot(command["request"]["scope"])),
                "prerequisites":self.tasks._prerequisites(self.tasks.task_record(task_id)),
                "pending":[{k:v for k,v in item.items() if k in {"event_id","status"}} for item in self._related_pending(evidence)]})

    def _preparation_failures(self,now):
        if self._lifecycle!="ACTIVE":return
        # No task owner mutation occurs in this read pass. Keep one detached
        # view instead of copying every task again for each dependency.
        tasks=self.tasks.tasks
        for task_id,task in tasks.items():
            spec=self.tasks._definitions[task["task_definition_id"]]
            if spec["kind"]!="hitl" or not task["eligible"]:continue
            failures=[]
            for dependency in self.tasks._after(task["task_definition_id"]):
                for candidate in tasks.values():
                    if candidate["task_definition_id"]==dependency and candidate["eligible"] and candidate["status"]=="FAILED":
                        failures.append(dependency)
            if not failures:continue
            request_id=self._next_question.get(task_id,{}).get("request_id") or self._question_by_task.get(task_id)
            if request_id is None:
                q=self.hitl.reserve_preparation(task_id,spec["decision"],now=now)
                request_id=q["request_id"];self._question_by_task[task_id]=request_id
            if self.hitl.requests[request_id]["status"] in {"OPEN","PREPARING","PREPARATION_FAILED"}:
                self.hitl.preparation_failed(request_id,now=now,code="PREREQUISITE_FAILED")

    def _feeds(self,now):
        changes=self.hitl.changes_since(self._hitl_view_seq)
        questions=self.hitl.requests if changes else {}
        for change in changes:
            self._hitl_view_seq=change["view_seq"]
            request_id=change["request_id"]
            q=questions[request_id]
            kind="hitl_opened" if change["kind"]=="HITL_OPENED" else "hitl_updated" if change["kind"]=="HITL_EVIDENCE_REVISED" else "hitl_status"
            if q["status"]=="OPEN" and request_id not in self._published_questions:
                kind="hitl_opened";self._published_questions.add(request_id)
            data={"question":q,"evidence":self.hitl.current(request_id)}
            self._change(kind,data,now)
            self._outbox.append({"kind":kind,"request":copy.deepcopy(data),"view_seq":self._view_seq})
        for key,record in self.timers.records.items():
            old=self._timer_status.get(key)
            if record["status"]!=old:
                self._timer_status[key]=record["status"]
                if record["status"] in {"ARMED","CANCELLED"}:
                    kind="timer_start" if record["status"]=="ARMED" else "timer_cancel"
                    self._outbox.append({"kind":kind,"request":copy.deepcopy(record["payload"])})
                self._change("TIMER_STATUS",{"timer_id":key,"status":record["status"]},now)

    def _process(self,now):
        self._at(now)
        if self._termination_phase=="FINALIZED":return
        self.ledger.expire_resolutions(at=now)
        self._projection_and_lifecycle(now)
        self._refresh_open(now)
        # A fired Timer's work still has to own its current residency/cycle
        # before a completed or timed-out preparation can dispatch anything.
        timer_records=self.timers.records if self.tasks._triggers else {}
        for trigger in self.tasks._triggers.values():
            record=timer_records.get(trigger["cause_id"])
            if not record:continue
            owner=record["payload"]["owner"]
            if owner["kind"]=="STATE_RESIDENCY":
                current=self._entries.get(owner["id"],{})
                valid=current.get("active") and current.get("epoch")==owner["epoch"]
            else:
                spec=self._d["timers"][record["payload"]["timer_definition_id"]]
                current=self.hitl.decision_cycles.get(spec["owner"]["id"],{})
                status="OPEN" if spec["anchor"]=="decision_opened" else "DEFERRED"
                valid=current.get("id")==owner["id"] and current.get("epoch")==owner["epoch"] and current.get("status")==status
            if not valid:self.tasks.deactivate_trigger(trigger["cause_id"])
        commands=self.tasks.advance(now=now,lifecycle=self._lifecycle,state_entries=self._state_cells())
        self._consume(commands,now)
        self._preparation_failures(now)
        self.timers.sync(now=now,state_entries=self._entries,decision_cycles=self.hitl.decision_cycles,
            evaluate=lambda expr:self.tasks.evaluate(expr,now=now),lifecycle=self._lifecycle)
        self._feeds(now)
        if self._termination_phase=="DRAINING" and self.tasks.closure_complete:
            self._termination_phase="FINALIZED"
            self._change("EPISODE_FINALIZED",self.closure_set,now)

    def advance(self,*,now):
        self._process(now)
        commands=copy.deepcopy(self._outbox);self._outbox=[]
        return commands

    def receive(self,event,*,source_id,now):
        if self._termination_phase=="FINALIZED":
            _fail("EPISODE_COMPLETED","event","Completed Episodes reject later Events")
        result=self.ledger.receive(event,source_id=source_id,at=now);self._process(now);return result

    def resolve(self,completion,*,now):
        if self._termination_phase=="FINALIZED":_fail("EPISODE_COMPLETED","completion","Episode is completed")
        result=self.ledger.resolve(completion,at=now);self._process(now);return result

    def accept_preflight(self,result,*,now):
        value=self.tasks.accept_preflight(result,now=now);self._process(now);return value

    def accept_agent_result(self,result,*,now):
        value=self.tasks.accept_agent_result(result,now=now);self._process(now);return value

    def accept_work_result(self,result,*,now):
        value=self.tasks.accept_work_result(result,now=now);self._process(now);return value

    def submit_hitl(self,decision,*,now):
        self._process(now)
        q=self.hitl.requests.get(decision.get("request_id")) if isinstance(decision,dict) else None
        evidence=self.hitl.current(q["request_id"]) if q else None
        pending=bool(evidence and any(item["status"]=="PENDING" for item in self._related_pending(evidence)))
        eligible=self._lifecycle=="ACTIVE" and bool(q and self.tasks.task_record(q["task_id"])["eligible"])
        result=self.hitl.submit(decision,now=now,eligible=eligible,pending=pending)
        if result["status"]=="ACCEPTED" and q:
            self.tasks.block_hitl_task(q["task_id"],now=now,reason="HITL_DECIDED")
        self._process(now)
        return result

    def _resume(self,receipt,now):
        if receipt["status"] not in {"PREPARING","FAILED"}:return
        q=self.hitl.requests[receipt["request_id"]]
        self._next_question[q["task_id"]]={"request_id":q["request_id"],"prior_request_id":receipt["prior_request_id"]}
        self.tasks.request_hitl_preparation(q["task_id"],now=now)

    def resume_hitl(self,command,*,now):
        self._process(now)
        if self._lifecycle!="ACTIVE":_fail("EPISODE_COMPLETED","resume","Episode is not active")
        result=self.hitl.request_resume(command,now=now);self._resume(result,now);self._process(now);return result

    def fire_timer(self,payload,*,now):
        self._process(now)
        result=self.timers.fire(payload,now=now)
        action=result.get("on_expire")
        if action is not None and not result.get("duplicate",False):
            if action["kind"]=="resume_decision":
                receipt=self.hitl.resume_from_timer(payload["owner"]["id"],now=now,timer_id=payload["timer_id"])
                self._resume(receipt,now)
            else:
                self.tasks.trigger_task(action["id"],cause_id=payload["timer_id"],
                    stop_when=self._d["timers"][payload["timer_definition_id"]]["cancel_when"],now=now)
        self._process(now);return result

    def changes_since(self,view_seq=0):
        if type(view_seq) is not int or view_seq<0:_fail("VIEW_SEQUENCE_INVALID","view_seq","Expected a nonnegative sequence")
        return copy.deepcopy([item for item in self._changes if item["view_seq"]>view_seq])

    def query(self):
        return {"state":self._projection["state"] if self._projection else None,"projection":self.projection,
            "decisions":self.ledger.decisions,
            "lifecycle":self._lifecycle,"termination_phase":self._termination_phase,"closure_set":self.closure_set,
            "hitl":{"requests":self.hitl.requests,"evidence":self.hitl.evidence,"view_seq":self.hitl.view_seq},
            "tasks":self.tasks.tasks,"executions":self.tasks.executions,"timers":self.timers.records,
            "view_seq":self._view_seq,"changes":self.changes_since()}

    def export_state(self):
        state={"contract_schema":"chain-fact-episode/v1","ledger":self.ledger.export_state(),
            "tasks":self.tasks.export_state(),"hitl":self.hitl.export_state(),"timers":self.timers.export_state(),
            "clock":self._clock,"projection":self.projection,"lifecycle":self._lifecycle,"termination_phase":self._termination_phase,
            "entries":copy.deepcopy(self._entries),"entry_history":copy.deepcopy(self._entry_history),
            "changes":self.changes_since(),"view_seq":self._view_seq,"outbox":copy.deepcopy(self._outbox),
            "prepared":copy.deepcopy(self._prepared),"question_by_task":copy.deepcopy(self._question_by_task),
            "refresh_keys":copy.deepcopy(self._refresh_keys),"next_question":copy.deepcopy(self._next_question),
            "hitl_view_seq":self._hitl_view_seq,"timer_status":copy.deepcopy(self._timer_status)}
        state["published_questions"]=sorted(self._published_questions)
        state["state_hash"]=canonical_hash(state);return state

    @classmethod
    def from_state(cls,state):
        try:
            return cls._restore_state(state)
        except ConfigError:raise
        except (KeyError,TypeError,ValueError,AttributeError):
            _fail("STATE_INTEGRITY_MISMATCH","episode","Malformed saved Episode links or state")

    @classmethod
    def _restore_state(cls,state):
        check_json(state)
        fields={"contract_schema","ledger","tasks","hitl","timers","clock","projection","lifecycle","termination_phase",
            "entries","entry_history","changes","view_seq","outbox","prepared","question_by_task","refresh_keys",
            "next_question","hitl_view_seq","timer_status","published_questions","state_hash"}
        if not isinstance(state,dict) or set(state)!=fields or state.get("contract_schema")!="chain-fact-episode/v1" or state.get("state_hash")!=canonical_hash({k:v for k,v in state.items() if k!="state_hash"}):
            _fail("STATE_INTEGRITY_MISMATCH","episode","Saved Episode schema/hash differs")
        ledger=FactLedger.from_state(state["ledger"])
        definition=ledger.export_state()["constructor"]["definition"]
        owner=cls(definition,ledger)
        owner.tasks=TaskEngine.from_state(state["tasks"],ledger=ledger)
        owner.hitl=HITLOwner.from_state(state["hitl"],ledger=ledger)
        owner.timers=TimerOwner.from_state(state["timers"],definition=definition)
        for field in ("clock","projection","lifecycle","termination_phase","entries","entry_history","changes",
            "view_seq","outbox","prepared","question_by_task","refresh_keys","next_question","hitl_view_seq","timer_status"):
            setattr(owner,"_"+field,copy.deepcopy(state[field]))
        owner._published_questions=set(state["published_questions"])
        if owner._view_seq!=len(owner._changes) or any(item["view_seq"]!=index+1 for index,item in enumerate(owner._changes)):
            _fail("STATE_INTEGRITY_MISMATCH","view_seq","Episode change ordering differs")
        if owner._termination_phase not in {"RUNNING","DRAINING","FINALIZED"} or owner._lifecycle not in {"ACTIVE","COMPLETED"}:
            _fail("STATE_INTEGRITY_MISMATCH","lifecycle","Saved Episode lifecycle differs")
        if ((owner._termination_phase=="RUNNING")!=(owner._lifecycle=="ACTIVE") or
                (owner._termination_phase=="RUNNING")!=(owner.tasks.closure_set is None) or
                owner._termination_phase=="FINALIZED" and not owner.tasks.closure_complete):
            _fail("STATE_INTEGRITY_MISMATCH","termination_phase","Lifecycle, closure and finalization disagree")
        if owner._projection is not None:
            validate_projection(owner._projection)
            if owner._projection["definition_hash"]!=ledger.definition_hash or owner._projection["identity"]!=ledger.identity:
                _fail("STATE_INTEGRITY_MISMATCH","projection","Projection belongs to another Episode")
            active=[name for name,item in owner._entries.items() if item["active"]]
            if active!=[owner._projection["state"]]:
                _fail("STATE_INTEGRITY_MISMATCH","state_entries","Projection and active residency differ")
        generations={};prior_time=None
        for item in owner._entry_history:
            name=item["id"];instant=timestamp(item["entered_at"])
            if name not in definition["states"] or item["epoch"]!=generations.get(name,0)+1 or instant>timestamp(owner._clock) or prior_time is not None and instant<prior_time:
                _fail("STATE_INTEGRITY_MISMATCH","state_entries","State entry order or generation differs")
            generations[name]=item["epoch"];prior_time=instant
        if generations!={name:value["epoch"] for name,value in owner._entries.items()}:
            _fail("STATE_INTEGRITY_MISMATCH","state_entries","State entry history differs from current generations")
        if owner.timers.identity!=ledger.identity or owner.timers.engine_run_id!=ledger.engine_run_id or owner.timers.run_pin!=ledger.run_pin:
            _fail("STATE_INTEGRITY_MISMATCH","timers","Timers belong to another Episode")
        for task_id,request_id in owner._question_by_task.items():
            if owner.hitl.requests[request_id]["task_id"]!=task_id or task_id not in owner.tasks.tasks:
                _fail("STATE_INTEGRITY_MISMATCH","question","Saved question task link differs")
        for value in owner.tasks._triggers.values():
            record=owner.timers.records.get(value["cause_id"])
            if not record or record["status"]!="FIRED" or not record.get("receipt") or record["receipt"]["status"]!="ACCEPTED":
                _fail("STATE_INTEGRITY_MISMATCH","trigger","Task trigger has no accepted timer firing")
            spec=definition["timers"][record["payload"]["timer_definition_id"]]
            if spec["on_expire"]!={"kind":"task","id":value["rule"]["task"]} or value["rule"]["stop_when"]!=spec["cancel_when"]:
                _fail("STATE_INTEGRITY_MISMATCH","trigger","Task trigger differs from the declared timer action")
        return owner
