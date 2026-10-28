"""Deterministic fact-based work owner. All source/provider I/O belongs outside.

The Episode owner handles HITL revisions, timers and lifecycle. Local and
Temporal runners must serialize these calls in recorded processing order.
"""
from __future__ import annotations

import copy
from datetime import timedelta

from .common.effect_contracts import validate_effect_receipt

from .compiler import canonical_hash
from .errors import ConfigError
from .projections import StateProjector, _walk, _matches
from .reconciliation import PreflightVerifications
from .runtime_contracts import (canonical_source_refs, timestamp,
                                validate_preflight_result)
from .shape import check_json
from .task_contracts import (input_fingerprint, semantic_input,
                             validate_agent_request, validate_agent_result,
                             validate_work_request, validate_work_result,
                             work_input_fingerprint)


def _fail(code, path, message):
    raise ConfigError(code, path, message)


class TaskEngine:
    """Plan independent work, check sources, then accept immutable completions.

    A command is a request to an external runner, never proof that an Activity
    ran. The supplied ledger, definition and accepted results are the only
    clinical inputs; the display State is not a scheduling key.
    """
    def __init__(self, definition, ledger):
        self._d = copy.deepcopy(definition)
        self._projector = StateProjector(definition)
        if ledger.definition_hash != self._projector.definition_hash:
            _fail("DEFINITION_PIN_MISMATCH", "ledger", "Task owner and ledger definitions differ")
        self.ledger = ledger
        self._tasks, self._executions, self._batches = {}, {}, {}
        self._limited_verifications = {}
        self._closure, self._triggers = None, {}
        self._edges, self._rules, self._seq = {}, {}, 0
        self._clock = ledger.processed_at
        self._lifecycle, self._state_entries = "ACTIVE", {}
        # Keep only one immutable resolver frame. Evaluation sessions must
        # remain fresh: their memoized predicates and node budgets cannot be
        # shared across separate rules/calls, even when source cells agree.
        self._frame_cache_key, self._frame_cache_cells = None, None
        # One fingerprint-only entry per owned task. Issued requests and
        # Snapshots are always constructed/frozen/validated at dispatch.
        self._desired_input_cache = {}
        self._verification = PreflightVerifications(ledger.identity, ledger.engine_run_id,
            ledger.run_pin, event_receipt=ledger.event_receipt)
        self._definitions = copy.deepcopy(self._d["tasks"])
        self._summary_for = {}
        for name, task in list(self._definitions.items()):
            if task["kind"] not in {"agent", "hitl", "notify", "effect"}:
                _fail("TASK_KIND_UNSUPPORTED", name, "Unsupported work primitive")
            context = self._context(task)
            module = self._d["modules"].get(task.get("agent"), {})
            if self._d["contexts"][context]["summary"] or module.get("requires_summary", False):
                summary_name = "summary__" + context
                if summary_name in self._d["tasks"]:
                    _fail("TASK_ID_CONFLICT", summary_name, "Reserved derived Summary task ID")
                service = self._d["context_service"]
                self._definitions[summary_name] = {"kind": "agent", "agent": service["agent"],
                    "mode": service["mode"], "context": context, "after": [],
                    "identity": {"subject": "episode", "repeat": "input_change"},
                    "completion_effect": False, "origin_refs": task["origin_refs"]}
                self._summary_for[name] = summary_name
        for name, task in self._definitions.items():
            self._authorize(task)
            for dependency in task["after"]:
                if dependency not in self._definitions:
                    _fail("UNDEFINED_REFERENCE", name, "Unknown prerequisite task")
            self._scope(task)
        seen, rooted = set(), set()
        for rule in self._d["work_rules"]:
            if set(rule) != {"id", "task", "when", "stop_when", "origin_refs"} or rule["id"] in seen:
                _fail("WORK_RULE_INVALID", "work_rules", "Expected unique closed work rules")
            seen.add(rule["id"])
            if rule["task"] in rooted:
                _fail("AMBIGUOUS_WORK_RULE_TARGET", rule["task"],
                      "v1 result references require a separate task definition for each work rule")
            rooted.add(rule["task"])
            if rule["task"] not in self._definitions:
                _fail("UNDEFINED_REFERENCE", rule["id"], "Unknown work definition")
            self._projector._validate_expression(rule["when"])
            self._projector._validate_expression(rule["stop_when"])
        self._check_after_cycles()

    def _context(self, task):
        return (self._d["decisions"][task["decision"]]["evidence_context"]
                if task["kind"] == "hitl" else task["context"])

    def _scope(self, task):
        context = self._d["contexts"].get(self._context(task))
        if not isinstance(context, dict) or not isinstance(context.get("fields"), list):
            _fail("TASK_SCOPE_INVALID", "context", "A declared fact scope is required")
        names = context["fields"]
        if (not names or len(set(names)) != len(names) or set(names) - set(self._d["facts"])):
            _fail("TASK_SCOPE_INVALID", "context", "Scope must contain unique declared facts")
        return sorted(names)

    def _authorize(self, task):
        policy = self._d["policy"]
        if task["kind"] == "agent":
            name = task["agent"]
            module, approval = self._d["modules"].get(name), policy["agent_approvals"].get(name)
            if (not module or not approval or module["version"] not in approval["versions"]
                    or not set(module["actions"]) <= set(approval["actions"])
                    or not set(module["data_scopes"]) <= set(approval["data_scopes"])
                    or set(module["actions"]) & set(policy["forbidden_actions"])):
                _fail("AGENT_NOT_AUTHORIZED", name, "Agent version/actions/scopes are not approved")
            if (module.get("installation_state") != "VERIFIED"
                    or module.get("request_contract") != "chain-fact-agent-request/v1"
                    or module.get("result_contract") != "chain-fact-agent-result/v1"):
                _fail("CONTRACT_ADAPTER_REQUIRED", name, "A verified native v1 binding is required")
            if task["mode"] not in module["modes"]:
                _fail("MODE_UNSUPPORTED", name, "Agent mode is not declared")
        elif task["kind"] == "notify" and task["template"] not in policy["notification_templates"]:
            _fail("TEMPLATE_NOT_AUTHORIZED", "task", "Notification template is not approved")
        elif task["kind"] == "effect" and task["operation"] not in policy["allowed_effect_operations"]:
            _fail("EFFECT_NOT_AUTHORIZED", "task", "Presentation operation is not approved")
        for source, fields in self._d["source_coverage"].items():
            needed = set(self._scope(task)) & set(fields)
            approval = policy["query_approvals"].get(source, {})
            if needed and (not needed <= set(approval.get("fields", []))
                           or "source:read" not in approval.get("data_scopes", [])):
                _fail("QUERY_NOT_AUTHORIZED", source, "Task source scope is not approved")

    def _check_after_cycles(self):
        done, visiting, order = set(), set(), []
        def result_dependencies(expression, seen):
            dependencies = set()
            for kind, ref in _walk(expression):
                if kind == "requirement":
                    key = "requirement:"+ref
                    nested = self._projector._predicates[ref]
                elif ref["namespace"] == "results":
                    dependencies.add(ref["name"]); continue
                elif ref["namespace"] == "milestones" and "when" in self._d["milestones"][ref["name"]]:
                    key = "milestone:"+ref["name"]
                    nested = self._d["milestones"][ref["name"]]["when"]
                else:
                    continue
                if key not in seen:
                    dependencies.update(result_dependencies(nested,seen|{key}))
            return dependencies
        graph = {name:set(self._after(name)) for name in self._definitions}
        for rule in self._d["work_rules"]:
            for expression in (rule["when"],rule["stop_when"]):
                graph[rule["task"]].update(result_dependencies(expression,set()))
        for timer in self._d.get("timers",{}).values():
            action=timer["on_expire"]
            if action["kind"]=="task" and action["id"] in graph:
                graph[action["id"]].update(result_dependencies(timer["cancel_when"],set()))
        def walk(name):
            if name in visiting:
                _fail("TASK_DEPENDENCY_CYCLE", name, "Prerequisite tasks form a cycle")
            if name in done:
                return
            visiting.add(name)
            for child in sorted(graph[name]):
                walk(child)
            visiting.remove(name); done.add(name); order.append(name)
        for name in self._definitions:
            walk(name)
        self._definition_order = order

    def _after(self, name):
        return sorted(set(self._definitions[name]["after"] +
                          ([self._summary_for[name]] if name in self._summary_for else [])))

    def _at(self, now):
        if timestamp(now) < max(timestamp(self._clock), timestamp(self.ledger.processed_at)):
            _fail("INVALID_TIME", "now", "Processing time must not move backwards")
        self._clock = now

    def _id(self, kind):
        self._seq += 1
        return f"{self.ledger.identity['episode_id']}:{kind}:{self._seq:06d}"

    @property
    def tasks(self):
        return copy.deepcopy(self._tasks)

    @property
    def executions(self):
        return copy.deepcopy(self._executions)

    def task_record(self, task_id):
        """Read one detached task without copying unrelated execution history."""
        return copy.deepcopy(self._tasks[task_id])

    def execution_record(self, execution_id):
        return copy.deepcopy(self._executions[execution_id])

    def rule_evaluation(self, rule_id):
        return copy.deepcopy(self._rules.get(rule_id, {}))

    def reference_results_for(self, task_id):
        """Read old successful prerequisites; current results keep their own path."""
        names = set(self._after(self._tasks[task_id]["task_definition_id"]))
        return {key: copy.deepcopy(entry["result"]) for key, entry in self._executions.items()
                if entry["result"] is not None and entry["result"]["status"] == "SUCCESS"
                and self._tasks[entry["request"]["task_id"]]["task_definition_id"] in names
                and entry["result_use"] != "CURRENT"}

    @property
    def verifications(self):
        return {**copy.deepcopy(self._verification.export_state()["records"]),
                **copy.deepcopy(self._limited_verifications)}

    def _get_verification(self, key):
        return (copy.deepcopy(self._limited_verifications[key]) if key in self._limited_verifications
                else self._verification.get(key))

    def _frame(self, now):
        records = self.results_for_projection()
        key = (self.ledger.reducer_seq, now,
               canonical_hash({"records": records, "state_entries": self._state_entries}))
        if key != self._frame_cache_key:
            self._frame_cache_cells = self._build_frame_cells(now, records)
            self._frame_cache_key = key
        cells = self._frame_cache_cells
        def resolve(ref):
            ns, name, path = ref["namespace"], ref["name"], ref["path"]
            value = cells.get(ns, {}).get(name)
            if path and value is not None and ns!="decisions":
                value = value.get(path[0])
            # Callers and a memoizing Evaluator cannot mutate cached cells.
            return copy.deepcopy(value or {"value":None,"validity":"UNKNOWN","source_refs":[]})
        return self._projector._evaluator(resolve=resolve), resolve

    def _build_frame_cells(self, now, records):
        """Read-only projection; every dependency is part of _frame's key."""
        projection = self._projector.project_ledger(self.ledger, evaluated_at=now,
            results=records, state_entries=self._state_entries,pending_changes=self.ledger.dependency_pending_changes)
        cells = {"facts": self.ledger.snapshot()["facts"],
                 "milestones": {k:v["cell"] for k,v in projection["milestones"].items()},
                 "decisions": {}, "results": {}, "state_entries": copy.deepcopy(self._state_entries)}
        for pending in self.ledger.dependency_pending_changes:
            for name in pending["fields"]:
                cells["facts"][name] = {"value":None,"validity":"PENDING","source_refs":[],
                    "diagnostics":[{"code":"SOURCE_UPDATE_PENDING","reference":"facts."+name,"validity":"PENDING"}]}
        for item in sorted(self.ledger.decisions.values(), key=lambda x:x["accepted_reducer_seq"]):
            record = item["record"]
            evidence = self.ledger.frozen_snapshot(record["evidence_snapshot_id"])
            cells["decisions"][record["decision_definition_id"]] = {
                "value":record["decision"],"validity":"KNOWN",
                "source_refs":canonical_source_refs([ref for fact in evidence["facts"].values() for ref in fact["source_refs"]])}
        for name, record in records.items():
            current = record["status"] == "SUCCESS" and record["usage"] == "CURRENT"
            refs = self._executions[record["execution_id"]]["request"]["dependencies"]
            cells["results"][name] = {field:{"value":copy.deepcopy(value) if current else None,
                "validity":"KNOWN" if current else "INVALIDATED","source_refs":copy.deepcopy(refs)}
                for field,value in record["values"].items()}
        return cells

    def _target(self, task, resolve):
        identity = task["identity"]
        if identity["subject"] == "episode":
            return {"kind":"episode", "id":self.ledger.identity["episode_id"]}
        cell = resolve(identity["subject_ref"])
        if cell["validity"] != "KNOWN" or not isinstance(cell["value"], (str, int)) or type(cell["value"]) is bool:
            return None
        return {"kind":identity["subject"],"id":str(cell["value"])}

    def _sync(self, now):
        # Invalidate old prerequisite results before rules can read them. Work
        # output dependencies are acyclic, so one ordered pass is sufficient.
        self._refresh_inputs()
        evaluator, resolve = self._frame(now)
        active = {}
        rule_order = {name:index for index,name in enumerate(self._definition_order)}
        rules=self._d["work_rules"]+[v["rule"] for v in self._triggers.values() if v["active"]]
        for rule in sorted(rules,key=lambda r:(rule_order[r["task"]],r["id"])):
            # Earlier producers have had their current eligibility evaluated;
            # consumers cannot read a late result from an ineligible producer.
            evaluator, resolve = self._frame(now)
            when, stop = evaluator.evaluate(rule["when"]), evaluator.evaluate(rule["stop_when"])
            target = self._target(self._definitions[rule["task"]], resolve)
            allowed = when["truth"] == "TRUE" and stop["truth"] != "TRUE" and target is not None
            closure_allowed = (self._closure is not None and any(
                t["rule_id"]==rule["id"] and t["clinical_target"]==target
                for key,t in self._tasks.items() if key in self._closure["task_ids"]))
            allowed = allowed and (self._lifecycle == "ACTIVE" or closure_allowed)
            self._rules[rule["id"]] = {"when":when, "stop_when":stop, "allowed":allowed}
            edge_key = canonical_hash({"rule":rule["id"],"target":target})
            edge = self._edges.setdefault(edge_key, {"allowed":False,"cycle":0,"last_definite":None})
            repeat = self._definitions[rule["task"]]["identity"]["repeat"]
            definite = ("FALSE" if when["truth"] == "FALSE" or stop["truth"] == "TRUE" else
                        "TRUE" if when["truth"] == "TRUE" and stop["truth"] == "FALSE" else None)
            if allowed and (edge["cycle"] == 0 or (edge["last_definite"] == "FALSE"
                                                   and definite == "TRUE" and repeat == "condition_reentry")):
                edge["cycle"] += 1
            if definite is not None:
                edge["last_definite"] = definite
            edge["allowed"] = allowed
            for task in self._tasks.values():
                if task["rule_id"] == rule["id"]:
                    task["eligible"] = allowed and task["clinical_target"] == target and task["cause_cycle"] == edge["cycle"]
            self._refresh_use()
            if allowed:
                active[(rule["id"],rule["task"], canonical_hash(target))] = (rule, target, edge["cycle"])
        # Explicit roots share declared prerequisites when their target matches.
        roots = list(active.values())
        def add(name, parent, target, cycle):
            if any(value[0]["task"] == name and value[1] == target for value in active.values()):
                return
            if any(rule["task"] == name for rule in rules):
                return  # A parent's after cannot bypass a declared work guard.
            key = (parent["id"]+"::after::"+name,name,canonical_hash(target))
            active[key] = ({**parent,"id":parent["id"]+"::after::"+name,"task":name},target,cycle)
            for dependency in self._after(name):
                dependency_task = self._definitions[dependency]
                dep_target = self._target(dependency_task, resolve)
                if dep_target is not None:
                    add(dependency, parent, dep_target, cycle)
        for rule, target, cycle in roots:
            for dependency in self._after(rule["task"]):
                dep_target = self._target(self._definitions[dependency], resolve)
                if dep_target is not None:
                    add(dependency, rule, dep_target, cycle)
        eligible_ids = set()
        for rule, target, cycle in active.values():
            key = {"identity":self.ledger.identity,"engine_run_id":self.ledger.engine_run_id,
                   "run_pin":self.ledger.run_pin,"rule_id":rule["id"],"clinical_target":target,"cause_cycle":cycle}
            task_id = canonical_hash(key)
            eligible_ids.add(task_id)
            if task_id not in self._tasks:
                self._tasks[task_id] = {"contract_schema":"chain-fact-task/v1", "identity":copy.deepcopy(self.ledger.identity),
                    "engine_run_id":self.ledger.engine_run_id,"run_pin":self.ledger.run_pin,"task_id":task_id,
                    "task_definition_id":rule["task"],"rule_id":rule["id"],"clinical_target":target,
                    "cause_cycle":cycle,"owner_epoch":1,"status":"CANDIDATE","current_execution_id":None,
                    "desired_input_revision":0,"desired_input_fingerprint":None,"execution_ids":[],
                    "last_reason":None,"eligible":True,"preparation_id":None,"rerun":False}
            task = self._tasks[task_id]
            task["eligible"] = True
            if task["status"] == "NO_LONGER_ELIGIBLE":
                task["owner_epoch"] += 1
                task["status"] = "CANDIDATE" if not task["execution_ids"] else "SUCCEEDED"
            self._desired(task)
        for task_id, task in self._tasks.items():
            if task_id not in eligible_ids:
                task["eligible"] = False
                task["last_reason"] = "WORK_CONDITION_NOT_TRUE"
                if task["status"] != "RUNNING":
                    task["status"] = "NO_LONGER_ELIGIBLE"
        self._refresh_inputs()

    def _refresh_inputs(self):
        for name in self._definition_order:
            for task in self._tasks.values():
                if task["task_definition_id"] == name:
                    self._desired(task)
            self._refresh_use()

    def _prerequisites(self, task):
        ids = []
        for name in self._after(task["task_definition_id"]):
            matches = []
            for other in self._tasks.values():
                if (other["task_definition_id"] != name or not other["execution_ids"]
                        or other["clinical_target"] not in (task["clinical_target"],
                            {"kind":"episode","id":self.ledger.identity["episode_id"]})):
                    continue
                execution = self._executions[other["execution_ids"][-1]]
                if execution.get("result_use") == "CURRENT":
                    matches.append(execution["request"]["execution_id"])
            if len(matches) > 1:
                _fail("AMBIGUOUS_PREREQUISITE", name, "Multiple current results match this target")
            if not matches:
                return None
            ids.extend(matches)
        return sorted(set(ids))

    def _request(self, task, *, now, verification_ids=(), freeze=False):
        spec = self._definitions[task["task_definition_id"]]
        scope = self._scope(spec)
        snapshot = self.ledger.freeze_snapshot(scope) if freeze else self.ledger.snapshot(scope)
        deps = canonical_source_refs([ref for fact in snapshot["facts"].values() for ref in fact["source_refs"]])
        base = {"identity":copy.deepcopy(self.ledger.identity),"engine_run_id":self.ledger.engine_run_id,
            "run_pin":self.ledger.run_pin,"task_id":task["task_id"],"execution_id":"PENDING","request_id":"PENDING",
            "input_revision":max(1,task["desired_input_revision"]),
            "predecessor_execution_id":task["execution_ids"][-1] if task["execution_ids"] else None,
            "snapshot":snapshot,"scope":scope,"dependencies":deps,
            "prerequisite_result_ids":self._prerequisites(task) or [],"evaluated_at":now,
            "verification_ids":sorted(verification_ids)}
        if spec["kind"] == "agent":
            module = self._d["modules"][spec["agent"]]
            base.update(contract_schema="chain-fact-agent-request/v1",agent_id=spec["agent"],
                agent_version=module["version"],implementation_hash=module["implementation_hash"],mode=spec["mode"])
            base["input_fingerprint"] = input_fingerprint(base)
        else:
            base.update(contract_schema="chain-fact-work-request/v1",task_definition_id=task["task_definition_id"],
                        kind=spec["kind"],task=copy.deepcopy(spec))
            if spec["kind"] == "effect":
                base["effect_version"] = self._seq + 1
            base["input_fingerprint"] = work_input_fingerprint(base)
        return base

    def _desired(self, task):
        spec = self._definitions[task["task_definition_id"]]
        scope = self._scope(spec)
        key = (self.ledger.data_epoch, canonical_hash(self.ledger.dependency_pending_changes),
               tuple(self._prerequisites(task) or []))
        cached = self._desired_input_cache.get(task["task_id"])
        if cached is None or cached[0] != key:
            fingerprint = self._request(task, now=self._clock)["input_fingerprint"]
            self._desired_input_cache[task["task_id"]] = (key, fingerprint)
        else:
            fingerprint = cached[1]
        # Pending changes and result eligibility can move without a new data
        # epoch. Their live checks must never be skipped on a cache hit.
        if (task["status"] == "RUNNING" and spec["kind"] == "agent"
                and self._input_pending(scope)):
            task["rerun"] = True
        if task["desired_input_fingerprint"] != fingerprint:
            task["desired_input_revision"] += 1
            task["desired_input_fingerprint"] = fingerprint
            if task["status"] == "RUNNING" and self._definitions[task["task_definition_id"]]["kind"] == "agent":
                task["rerun"] = True

    def _refresh_use(self):
        for execution in self._executions.values():
            result = execution.get("result")
            if result is None:
                continue
            task = self._tasks[execution["request"]["task_id"]]
            if result["status"] == "FAILED":
                use = "FAILED"
            elif not task["eligible"]:
                use = "REFERENCE_NOT_ELIGIBLE"
            elif (execution["request"]["input_fingerprint"] != task["desired_input_fingerprint"]
                    or self._input_pending(execution["request"]["scope"])
                    or task["execution_ids"][-1] != execution["request"]["execution_id"]):
                use = "REFERENCE_OLD_INPUT"
            else:
                use = "CURRENT"
            execution["result_use"] = use

    def _input_pending(self,scope):
        return any(set(item["fields"])&set(scope) for item in self.ledger.dependency_pending_changes)

    def _ready(self, task):
        if task.get("hitl_blocked",False):
            return False
        if self._closure is not None:
            if task["task_id"] not in self._closure["task_ids"] or task["execution_ids"]:
                return False
        if not task["eligible"] or task["status"] in {"RUNNING","PREPARING","WAITING"}:
            return False
        if self._prerequisites(task) is None:
            task["last_reason"] = "PREREQUISITE_NOT_CURRENT"
            return False
        if task.get("preparation_requested",False):
            return True
        if not task["execution_ids"]:
            return True
        prior = self._executions[task["execution_ids"][-1]]
        repeat = self._definitions[task["task_definition_id"]]["identity"]["repeat"]
        return (task["desired_input_fingerprint"] != prior["request"]["input_fingerprint"]
                and (repeat == "input_change" or task["rerun"]))

    def _begin(self, tasks, now, *, round_no=1, deadline=None):
        batch_id = self._id("parallel")
        limits = self._d["execution"]["preparation"]
        deadline = deadline or (timestamp(now)+timedelta(seconds=limits["timeout_s"])).isoformat()
        batch = {"batch_id":batch_id,"task_ids":[t["task_id"] for t in tasks],"requests":{},
                 "round":round_no,"closed":False,"data_epoch":self.ledger.data_epoch,"deadline":deadline}
        self._batches[batch_id] = batch
        groups = {}
        purpose = {"agent":"AGENT_INPUT","hitl":"HITL_EVIDENCE","notify":"DELAY_NOTICE","effect":"EHR_EFFECT"}
        for task in tasks:
            spec = self._definitions[task["task_definition_id"]]
            task["status"], task["preparation_id"] = "PREPARING", self._id("prepare")
            for source, coverage in self._d["source_coverage"].items():
                fields = set(self._scope(spec)) & set(coverage)
                if fields:
                    group = groups.setdefault((source,purpose[spec["kind"]]), {"fields":set(),"consumers":[]})
                    group["fields"].update(fields); group["consumers"].append(task["task_id"])
        commands = []
        for (source, query_purpose), group in sorted(groups.items()):
            scope = sorted(group["fields"])
            consumers = sorted(group["consumers"])
            versions = {canonical_hash(ref):{k:ref[k] for k in ("system","record_id","version")}
                        for fact in self.ledger.snapshot(scope)["facts"].values() for ref in fact["source_refs"]}
            request = {"contract_schema":"chain-fact-preflight-request/v1",
                "identity":copy.deepcopy(self.ledger.identity),"engine_run_id":self.ledger.engine_run_id,
                "run_pin":self.ledger.run_pin,"request_id":self._id("check"),
                "preparation_id":self._tasks[consumers[0]]["preparation_id"],"parallel_batch_id":batch_id,
                "consumer_task_ids":consumers,"purpose":query_purpose,"source":source,"scope":scope,
                "authorization_ref":canonical_hash({"source":source,"approval":self._d["policy"]["query_approvals"][source]}),
                "basis":{"data_epoch":self.ledger.data_epoch,"known_versions":list({canonical_hash(v):v for v in versions.values()}.values()),"source_cursor":None},
                "limits":{"timeout_ms":max(1,int((timestamp(deadline)-timestamp(now)).total_seconds()*1000)),"max_attempts":limits["maximum_attempts"],
                    "max_pages":limits["maximum_pages"],"max_preparation_rounds":limits["maximum_rounds"]}}
            request = self._verification.register(request, now=now)
            batch["requests"][request["request_id"]] = request
            commands.append({"kind":"preflight","request":copy.deepcopy(request)})
        return commands

    def accept_preflight(self, result, *, now):
        request_id = result.get("request_id") if isinstance(result, dict) else None
        requests = {key:value for batch in self._batches.values() for key,value in batch["requests"].items()}
        if request_id not in requests:
            _fail("REQUEST_NOT_REGISTERED", "request_id", "Unknown preparation request")
        result = validate_preflight_result(result, requests[request_id])
        if timestamp(now) < max(timestamp(self._clock), timestamp(self.ledger.processed_at)):
            _fail("INVALID_TIME", "now", "Processing time must not move backwards")
        # Invalid completions must not consume journal order or owner time.
        trial = PreflightVerifications.from_state(self._verification.export_state(),event_receipt=self.ledger.event_receipt)
        trial.accept_result(result,now=now,data_epoch=self.ledger.data_epoch,
            reducer_seq=self.ledger.reducer_seq+1,changes=self.ledger.changes_since(requests[request_id]["basis"]["data_epoch"]),
            pending_changes=self.ledger.pending_changes)
        self._at(now)
        self.ledger.record_observation({"observation_id":canonical_hash(result),"request_id":request_id,
            "kind":"PREFLIGHT_RESULT","content_hash":canonical_hash(result)},at=now)
        return self._verification.accept_result(result,now=now,data_epoch=self.ledger.data_epoch,
            reducer_seq=self.ledger.reducer_seq,changes=self.ledger.changes_since(requests[request_id]["basis"]["data_epoch"]),
            pending_changes=self.ledger.pending_changes)

    def _command(self, execution):
        request = execution["request"]
        spec = self._definitions[self._tasks[request["task_id"]]["task_definition_id"]]
        return {"kind":"hitl_prepared" if spec["kind"] == "hitl" else spec["kind"],
            "request":copy.deepcopy(request),
            "prerequisite_results":{key:copy.deepcopy(self._executions[key]["result"])
                                    for key in request["prerequisite_result_ids"]},
            "verifications":{key:self._get_verification(key) for key in request["verification_ids"]}}

    def _dispatch(self, task, now, verification_ids):
        self._desired(task)
        request = self._request(task,now=now,verification_ids=verification_ids,freeze=True)
        execution_id = self._id("execute")
        request.update(request_id=execution_id,execution_id=execution_id)
        if self._definitions[task["task_definition_id"]]["kind"] == "agent":
            request = validate_agent_request(request,module=self._d["modules"][request["agent_id"]])
        else:
            request = validate_work_request(request)
        self._executions[execution_id] = {"request":request,"attempt_no":1,"result":None,"result_use":None,
            "accepted_at":None,"prepared_at":now,"preparation_id":task["preparation_id"],
            "issued_sequence":self._seq,
            "cause":{"rule_id":task["rule_id"],"clinical_target":copy.deepcopy(task["clinical_target"]),
                     "cause_cycle":task["cause_cycle"],"owner_epoch":task["owner_epoch"],
                     "evaluation":copy.deepcopy(self._rules[task["rule_id"].split("::after::",1)[0]])},
            "deadline":(timestamp(now)+timedelta(seconds=self._d["execution"]["activity"]["timeout_s"])).isoformat()}
        task.update(current_execution_id=execution_id,status="WAITING" if request.get("kind")=="hitl" else "RUNNING",
                    rerun=False,last_reason=None)
        if "preparation_requested" in task: task["preparation_requested"]=False
        task["execution_ids"].append(execution_id)
        if self._closure is not None: self._closure["drain_execution_ids"].append(execution_id)
        return self._command(self._executions[execution_id])

    def refresh(self, *, now, lifecycle=None, state_entries=None):
        """Refresh current eligibility/results without issuing external work."""
        if lifecycle is not None and lifecycle not in {"ACTIVE","DORMANT","CLINICAL_CLOSED","ADMIN_CLOSING","COMPLETED"}:
            _fail("LIFECYCLE_INVALID","lifecycle","Unknown lifecycle")
        if state_entries is not None:
            self._projector.validate_state_entries(state_entries,evaluated_at=now)
        self._at(now)
        if lifecycle is not None: self._lifecycle=lifecycle
        if state_entries is not None: self._state_entries=copy.deepcopy(state_entries)
        self._sync(now)

    def evaluate(self, expression, *, now):
        self._projector._validate_expression(expression)
        return self._frame(now)[0].evaluate(expression)

    def request_hitl_preparation(self, task_id, *, now):
        task=self._tasks.get(task_id)
        if task is None or self._definitions[task["task_definition_id"]]["kind"]!="hitl":
            _fail("HITL_TASK_NOT_FOUND","task_id","Expected an existing HITL task")
        self._at(now)
        if task["status"]=="PREPARING": return
        task.update(status="CANDIDATE",preparation_requested=True,hitl_blocked=False)

    def block_hitl_task(self, task_id, *, now, reason):
        task=self._tasks[task_id]
        if self._definitions[task["task_definition_id"]]["kind"]!="hitl":
            _fail("HITL_TASK_NOT_FOUND","task_id","Expected a HITL task")
        self._at(now)
        task.update(hitl_blocked=True,preparation_requested=False,status="SUCCEEDED",last_reason=reason)

    def trigger_task(self, task_definition_id, *, cause_id, stop_when, now):
        """Internal trusted Timer action; never a user-supplied Tool primitive."""
        if self._closure is not None or self._lifecycle!="ACTIVE": return
        if task_definition_id not in self._definitions or not isinstance(cause_id,str) or not cause_id:
            _fail("TASK_TRIGGER_INVALID","trigger","Unknown task or cause")
        self._projector._validate_expression(stop_when); self._at(now)
        key=canonical_hash({"task":task_definition_id,"cause":cause_id})
        rule={"id":"timer::"+key,"task":task_definition_id,
            "when":{"op":"eq","args":[{"kind":"literal","value":True},{"kind":"literal","value":True}]},
            "stop_when":copy.deepcopy(stop_when),"origin_refs":copy.deepcopy(self._definitions[task_definition_id]["origin_refs"])}
        value={"rule":rule,"active":True,"cause_id":cause_id}
        if key in self._triggers and self._triggers[key]["rule"]!=rule:
            _fail("TASK_TRIGGER_CONFLICT","trigger","Timer cause was reused for different work")
        self._triggers.setdefault(key,value)

    def deactivate_trigger(self, cause_id):
        for value in self._triggers.values():
            if value["cause_id"]==cause_id: value["active"]=False

    def start_closure(self, *, now):
        """Freeze ending effects and already issued work exactly once."""
        if self._closure is not None: return self.closure_set
        self._at(now); self._sync(now)
        task_ids=sorted(key for key,task in self._tasks.items() if task["eligible"] and
            self._definitions[task["task_definition_id"]].get("completion_effect",False))
        execution_ids=sorted(key for key,e in self._executions.items() if e["result"] is None and
            self._definitions[self._tasks[e["request"]["task_id"]]["task_definition_id"]]["kind"]!="hitl")
        self._closure={"task_ids":task_ids,"execution_ids":execution_ids,"drain_execution_ids":list(execution_ids),"captured_at":now}
        for batch in self._batches.values():
            if not batch["closed"] and any(key not in task_ids for key in batch["task_ids"]):
                batch["closed"]=True
                for key in batch["task_ids"]:
                    if self._tasks[key]["status"]=="PREPARING":self._tasks[key]["status"]="CANDIDATE"
        for task in self._tasks.values():
            if task["task_id"] in task_ids and self._prerequisites(task) is None:
                task.update(status="FAILED",last_reason="PREREQUISITE_NOT_CURRENT_AT_CLOSURE",hitl_blocked=True)
        return self.closure_set

    @property
    def closure_set(self):
        return None if self._closure is None else {k:copy.deepcopy(self._closure[k]) for k in ("task_ids","execution_ids")}

    @property
    def closure_complete(self):
        if self._closure is None: return False
        if any(self._executions[key]["result"] is None for key in self._closure["drain_execution_ids"]): return False
        for key in self._closure["task_ids"]:
            task=self._tasks[key]
            if not task["execution_ids"] and task["status"] not in {"FAILED","NO_LONGER_ELIGIBLE"}: return False
            if task["execution_ids"] and self._executions[task["execution_ids"][-1]]["result"] is None: return False
        return True

    def advance(self, *, now, lifecycle=None, state_entries=None):
        if lifecycle is not None:
            if lifecycle not in {"ACTIVE","DORMANT","CLINICAL_CLOSED","ADMIN_CLOSING","COMPLETED"}:
                _fail("LIFECYCLE_INVALID", "lifecycle", "Unknown externally supplied lifecycle")
        if state_entries is not None:
            self._projector.validate_state_entries(state_entries,evaluated_at=now)
        self._at(now)
        if lifecycle is not None:
            self._lifecycle = lifecycle
        if state_entries is not None:
            self._state_entries = copy.deepcopy(state_entries)
        timeout_commands = self._expire_executions(now)
        self._verification.refresh(now=now,data_epoch=self.ledger.data_epoch,reducer_seq=self.ledger.reducer_seq,
            changes=self.ledger.changes_since(0),pending_changes=self.ledger.pending_changes)
        self._sync(now)
        commands = timeout_commands
        for batch in list(self._batches.values()):
            if batch["closed"]:
                continue
            statuses = [self._verification.status(key) for key in batch["requests"]]
            if any(entry["record_id"] is None for entry in statuses):
                continue
            records = [self._verification.get(entry["record_id"]) for entry in statuses]
            eligible = [self._tasks[key] for key in batch["task_ids"] if self._tasks[key]["eligible"]]
            batch["closed"] = True
            stale = any(record["sharing_epoch"] != self.ledger.data_epoch and
                        any(row["verification"] == "VERIFIED" for row in record["coverage"]) for record in records)
            if stale and batch["round"] < self._d["execution"]["preparation"]["maximum_rounds"] and timestamp(now) < timestamp(batch["deadline"]):
                commands.extend(self._begin(eligible,now,round_no=batch["round"]+1,deadline=batch["deadline"]))
                continue
            if stale:
                # The original source observation remains immutable. A bounded
                # preparation that lost freshness gets an explicit unconfirmed
                # consumption record, never a forged VERIFIED current token.
                limited = []
                for record in records:
                    value = copy.deepcopy(record)
                    fields = [row["field"] for row in value["coverage"]]
                    value["coverage"] = [{"field":field,"verification":"UNCONFIRMED"} for field in fields]
                    value["sharing_epoch"] = None
                    value["errors"] = [{"scope":fields,"code":"PREPARATION_LIMIT_REACHED","retryable":False}]
                    value.pop("verification_id")
                    value["verification_id"] = canonical_hash(value)
                    self._limited_verifications[value["verification_id"]] = value
                    limited.append(value)
                records = limited
            for task in eligible:
                if self._prerequisites(task) is None:
                    task.update(status="CANDIDATE",last_reason="PREREQUISITE_NOT_CURRENT")
                    continue
                ids = [r["verification_id"] for r in records if task["task_id"] in r["consumer_task_ids"]]
                task["preparation_unconfirmed"] = stale
                commands.append(self._dispatch(task,now,ids))
        ready = [task for task in self._tasks.values() if self._ready(task)]
        if ready:
            commands.extend(self._begin(ready,now))
            # No source covers this declared scope (e.g. initial identifiers).
            for batch in list(self._batches.values()):
                if not batch["closed"] and not batch["requests"]:
                    batch["closed"] = True
                    for key in batch["task_ids"]:
                        commands.append(self._dispatch(self._tasks[key],now,[]))
        return commands

    def _accept(self, result, *, now, agent):
        execution_id = result.get("execution_id") if isinstance(result,dict) else None
        if execution_id not in self._executions:
            _fail("EXECUTION_NOT_FOUND", "execution_id", "No such issued execution")
        execution = self._executions[execution_id]
        request = execution["request"]
        spec = self._definitions[self._tasks[request["task_id"]]["task_definition_id"]]
        if agent != (spec["kind"] == "agent"):
            _fail("RESULT_CONTRACT_MISMATCH", "result", "Wrong work result kind")
        if agent:
            fields = self._d["modules"][spec["agent"]]["modes"][spec["mode"]]["output_fields"]
            result = validate_agent_result(result,request,output_fields=fields)
            if result["status"] == "SUCCESS":
                for declaration in self._d["milestones"].values():
                    capture = declaration.get("capture_first", {})
                    if capture.get("name") == self._tasks[request["task_id"]]["task_definition_id"]:
                        field = capture["path"][0]
                        if field not in result["result"] or not _matches(result["result"][field],declaration["type"]):
                            _fail("FACT_TYPE_MISMATCH", "result", "Historical capture output has the wrong declared type")
        else:
            result = validate_work_result(result,request)
            if result["status"] == "SUCCESS":
                self._validate_receipt(result["receipt"], request, now)
        if timestamp(result["produced_at"]) > timestamp(now):
            _fail("FUTURE_INPUT", "produced_at", "Result was produced after processing time")
        if execution["result"] is not None:
            if execution["result"] != result:
                _fail("RESULT_ID_CONFLICT", "execution_id", "Execution already has a different completion")
            return copy.deepcopy(execution)
        self._at(now)
        self._sync(now)
        if agent:
            self.ledger.record_task_result({"execution_id":execution_id,"task_id":request["task_id"],
                "request_hash":canonical_hash(request),"result_hash":canonical_hash(result),
                "input_snapshot_id":request["snapshot"]["snapshot_id"]},at=now)
        execution["result"], execution["accepted_at"] = copy.deepcopy(result), now
        task = self._tasks[request["task_id"]]
        task["status"] = "SUCCEEDED" if result["status"] == "SUCCESS" else "FAILED"
        self._refresh_use()
        if agent and execution["result_use"] == "CURRENT":
            for name, declaration in self._d["milestones"].items():
                capture = declaration.get("capture_first", {})
                if capture.get("name") == task["task_definition_id"] and capture.get("path", [None])[0] in result["result"]:
                    field = capture["path"][0]
                    self.ledger.capture_first(name,task_definition_id=task["task_definition_id"],execution_id=execution_id,
                        result_field=field,value=result["result"][field],input_snapshot_id=request["snapshot"]["snapshot_id"],
                        result_status="SUCCESS",at=now,dependency_scoped=True)
        return copy.deepcopy(execution)

    def accept_agent_result(self, result, *, now):
        return self._accept(result,now=now,agent=True)

    def accept_work_result(self, result, *, now):
        return self._accept(result,now=now,agent=False)

    @staticmethod
    def _validate_receipt(receipt, request, now):
        task = request["task"]
        if request["kind"] == "effect":
            wire = {"request_schema":"chain-effect/v0.3","request_id":request["execution_id"],
                "episode_id":request["identity"]["episode_id"],"effect_version":request["effect_version"],
                **{key:task[key] for key in ("operation","resource","target")}}
            try:
                validate_effect_receipt(receipt,wire)
            except ValueError as error:
                _fail("RECEIPT_INVALID", "receipt", str(error))
        else:
            required = {"receipt_schema","request_id","episode_id","template","channel","recipient",
                        "payload_hash","status","recorded_at"}
            if set(receipt) != required:
                _fail("RECEIPT_INVALID", "receipt", "Expected a complete notification receipt")
            wire = {"request_schema":"chain-notification/v0.3",**{key:receipt[key] for key in
                ("request_id","episode_id","template","channel","recipient")}}
            if (receipt["receipt_schema"] != "chain-notification-receipt/v0.3"
                    or receipt["status"] != "MOCK_RECORDED" or receipt["request_id"] != request["execution_id"]
                    or receipt["episode_id"] != request["identity"]["episode_id"] or receipt["template"] != task["template"]
                    or receipt["channel"] not in {"LOG","CONSOLE"} or not isinstance(receipt["recipient"],str)
                    or not receipt["recipient"] or receipt["payload_hash"] != canonical_hash(wire)):
                _fail("RECEIPT_INVALID", "receipt", "Receipt differs from the issued Mock notification")
        if not timestamp(request["evaluated_at"]) <= timestamp(receipt["recorded_at"]) <= timestamp(now):
            _fail("INVALID_TIME", "receipt.recorded_at", "Receipt time is outside this execution")

    def retry(self, execution_id, *, now):
        self._at(now)
        if execution_id not in self._executions:
            _fail("EXECUTION_NOT_FOUND", "execution_id", "No such execution")
        execution = self._executions[execution_id]
        if execution["result"] is not None:
            return None
        if execution["attempt_no"] >= self._d["execution"]["activity"]["maximum_attempts"]:
            _fail("ATTEMPT_LIMIT_REACHED", "execution_id", "Technical retry limit reached")
        if execution["request"].get("kind") == "hitl":
            _fail("HITL_RUNTIME_NOT_IMPLEMENTED", "execution_id", "Prepared HITL is not an Activity completion")
        execution["attempt_no"] += 1
        execution["deadline"] = (timestamp(now)+timedelta(seconds=self._d["execution"]["activity"]["timeout_s"])).isoformat()
        return self._command(execution)

    def _expire_executions(self, now):
        commands = []
        for execution_id, execution in list(self._executions.items()):
            request = execution["request"]
            if execution["result"] is not None or request.get("kind") == "hitl" or timestamp(now) < timestamp(execution["deadline"]):
                continue
            if execution["attempt_no"] < self._d["execution"]["activity"]["maximum_attempts"]:
                commands.append(self.retry(execution_id,now=now))
                continue
            common = ("identity","engine_run_id","run_pin","task_id","execution_id","request_id",
                      "input_revision","predecessor_execution_id","input_fingerprint")
            failure = {key:copy.deepcopy(request[key]) for key in common}
            failure.update(input_snapshot_id=request["snapshot"]["snapshot_id"],status="FAILED",
                           produced_at=now,error_code="ACTIVITY_TIMEOUT")
            if "agent_id" in request:
                failure.update(contract_schema="chain-fact-agent-result/v1",result=None,evidence=[],
                    **{key:request[key] for key in ("agent_id","agent_version","implementation_hash","mode")})
                self._accept(failure,now=now,agent=True)
            else:
                failure.update(contract_schema="chain-fact-work-result/v1",receipt=None)
                self._accept(failure,now=now,agent=False)
        return commands

    @property
    def next_deadline(self):
        """Pending preparation/execution deadlines without copying history."""
        values = [item["deadline"] for item in self._executions.values()
                  if item["result"] is None and item["request"].get("kind") != "hitl"]
        values.extend(item["deadline"] for item in self._batches.values() if not item["closed"])
        return min(values, key=timestamp) if values else None

    def results_for_projection(self):
        results, selected = {}, {}
        for task in self._tasks.values():
            name = task["task_definition_id"]
            if name not in self._d["tasks"] or self._definitions[name]["kind"] != "agent" or not task["execution_ids"]:
                continue
            execution = self._executions[task["execution_ids"][-1]]
            result = execution.get("result")
            if result is None:
                continue
            record = {"execution_id":result["execution_id"],"status":result["status"],
                "usage":"CURRENT" if execution["result_use"] == "CURRENT" else "REFERENCE_OLD_INPUT",
                "input_snapshot_id":result["input_snapshot_id"],"values":copy.deepcopy(result["result"] or {}),
                "known_at":execution["accepted_at"]}
            previous = selected.get(name)
            if previous is not None:
                if record["usage"] == results[name]["usage"] == "CURRENT":
                    _fail("AMBIGUOUS_RESULT_REFERENCE", name, "Result reference requires a unique clinical target")
                if results[name]["usage"] == "CURRENT" or (record["usage"] != "CURRENT" and previous >= execution["issued_sequence"]):
                    continue
            results[name] = record
            selected[name] = execution["issued_sequence"]
        return results

    def export_state(self):
        state = {"contract_schema":"chain-fact-work-owner/v1","definition_hash":self._projector.definition_hash,
            "identity":copy.deepcopy(self.ledger.identity),"engine_run_id":self.ledger.engine_run_id,"run_pin":self.ledger.run_pin,
            "tasks":copy.deepcopy(self._tasks),"executions":copy.deepcopy(self._executions),"batches":copy.deepcopy(self._batches),
            "edges":copy.deepcopy(self._edges),"rules":copy.deepcopy(self._rules),"sequence":self._seq,
            "clock":self._clock,"lifecycle":self._lifecycle,"state_entries":copy.deepcopy(self._state_entries),
            "verifications":self._verification.export_state(),"limited_verifications":copy.deepcopy(self._limited_verifications)}
        if self._closure is not None: state["closure"]=copy.deepcopy(self._closure)
        if self._triggers: state["triggers"]=copy.deepcopy(self._triggers)
        state["state_hash"] = canonical_hash(state)
        return state

    @classmethod
    def from_state(cls, state, *, ledger):
        check_json(state)
        fields = {"contract_schema","definition_hash","identity","engine_run_id","run_pin","tasks","executions",
                  "batches","edges","rules","sequence","clock","lifecycle","state_entries","verifications","limited_verifications","state_hash"}
        if (not isinstance(state,dict) or not fields<=set(state) or set(state)-fields-{"closure","triggers"} or state["contract_schema"] != "chain-fact-work-owner/v1"
                or canonical_hash({k:v for k,v in state.items() if k != "state_hash"}) != state["state_hash"]):
            _fail("STATE_INTEGRITY_MISMATCH", "state", "Saved task owner shape/hash differs")
        definition = ledger.export_state()["constructor"]["definition"]
        owner = cls(definition,ledger)
        if (state["definition_hash"] != ledger.definition_hash or state["identity"] != ledger.identity
                or state["run_pin"] != ledger.run_pin or state["engine_run_id"] != ledger.engine_run_id):
            _fail("IDENTITY_MISMATCH", "state", "Saved work belongs to another run")
        if (type(state["sequence"]) is not int or state["sequence"] < 0
                or state["lifecycle"] not in {"ACTIVE","DORMANT","CLINICAL_CLOSED","ADMIN_CLOSING","COMPLETED"}):
            _fail("STATE_INTEGRITY_MISMATCH", "sequence", "Saved clock/lifecycle/sequence is invalid")
        timestamp(state["clock"])
        task_fields = {"contract_schema","identity","engine_run_id","run_pin","task_id","task_definition_id",
            "rule_id","clinical_target","cause_cycle","owner_epoch","status","current_execution_id",
            "desired_input_revision","desired_input_fingerprint","execution_ids","last_reason","eligible",
            "preparation_id","rerun"}
        statuses = {"CANDIDATE","PREPARING","RUNNING","WAITING","SUCCEEDED","FAILED","NO_LONGER_ELIGIBLE"}
        for task_id, task in state["tasks"].items():
            if (not isinstance(task,dict) or not task_fields <= set(task) or set(task)-task_fields-{"preparation_unconfirmed","hitl_blocked","preparation_requested"}
                    or task["task_definition_id"] not in owner._definitions or task["task_id"] != task_id
                    or task["contract_schema"] != "chain-fact-task/v1" or task["status"] not in statuses
                    or task["identity"] != ledger.identity or task["engine_run_id"] != ledger.engine_run_id
                    or task["run_pin"] != ledger.run_pin or type(task["eligible"]) is not bool or type(task["rerun"]) is not bool
                    or any(type(task[key]) is not int or task[key]<1 for key in ("cause_cycle","owner_epoch","desired_input_revision"))
                    or not isinstance(task["execution_ids"],list) or len(set(task["execution_ids"])) != len(task["execution_ids"])):
                _fail("STATE_INTEGRITY_MISMATCH", "task", "Saved task shape/definition/status differs")
            key = {field:task[field] for field in ("identity","engine_run_id","run_pin","rule_id","clinical_target","cause_cycle")}
            if canonical_hash(key) != task_id or any(eid not in state["executions"] for eid in task["execution_ids"]):
                _fail("STATE_INTEGRITY_MISMATCH", "task_id", "Saved task ID or execution links differ")
            if task["current_execution_id"] != (task["execution_ids"][-1] if task["execution_ids"] else None):
                _fail("STATE_INTEGRITY_MISMATCH", "current_execution_id", "Saved current execution differs")
        for execution_id, execution in state["executions"].items():
            if (not isinstance(execution,dict) or set(execution) != {"request","attempt_no","result","result_use","accepted_at",
                    "prepared_at","preparation_id","issued_sequence","deadline","cause"}
                    or type(execution["attempt_no"]) is not int or not 1 <= execution["attempt_no"] <= owner._d["execution"]["activity"]["maximum_attempts"]
                    or type(execution["issued_sequence"]) is not int or not 1 <= execution["issued_sequence"] <= state["sequence"]):
                _fail("STATE_INTEGRITY_MISMATCH", "execution", "Saved retry/sequence budget differs")
            request = execution["request"]
            if request["execution_id"] != execution_id or request["task_id"] not in state["tasks"]:
                _fail("STATE_INTEGRITY_MISMATCH", "execution", "Saved execution links differ")
            task = state["tasks"][request["task_id"]]
            spec = owner._definitions[task["task_definition_id"]]
            if (execution_id not in task["execution_ids"] or request["scope"] != owner._scope(spec)
                    or (spec["kind"] == "agent" and (request.get("agent_id") != spec["agent"] or request.get("mode") != spec["mode"]))
                    or (spec["kind"] != "agent" and request.get("task") != spec)
                    or any(key not in state["executions"] or state["executions"][key]["result"] is None for key in request["prerequisite_result_ids"])):
                _fail("STATE_INTEGRITY_MISMATCH", "execution", "Saved request differs from its work definition")
            if timestamp(execution["deadline"]) < timestamp(execution["prepared_at"]) or timestamp(execution["prepared_at"]) > timestamp(state["clock"]):
                _fail("STATE_INTEGRITY_MISMATCH", "deadline", "Saved execution times differ")
            if (execution["cause"].get("rule_id") != task["rule_id"] or execution["cause"].get("clinical_target") != task["clinical_target"]
                    or execution["cause"].get("cause_cycle") != task["cause_cycle"]):
                _fail("STATE_INTEGRITY_MISMATCH", "cause", "Saved work cause differs from its task")
            if request["contract_schema"] == "chain-fact-agent-request/v1":
                validate_agent_request(request,module=owner._d["modules"][request["agent_id"]])
                if execution["result"] is not None:
                    fields = owner._d["modules"][request["agent_id"]]["modes"][request["mode"]]["output_fields"]
                    validate_agent_result(execution["result"],request,output_fields=fields)
                    commitment = ledger.task_results.get(execution_id, {}).get("record")
                    if commitment != {"execution_id":execution_id,"task_id":request["task_id"],
                            "request_hash":canonical_hash(request),"result_hash":canonical_hash(execution["result"]),
                            "input_snapshot_id":request["snapshot"]["snapshot_id"]}:
                        _fail("STATE_INTEGRITY_MISMATCH", "result", "Saved result differs from the ledger commitment")
            else:
                validate_work_request(request)
                if execution["result"] is not None:
                    validate_work_result(execution["result"],request)
                    if execution["result"]["status"] == "SUCCESS":
                        owner._validate_receipt(execution["result"]["receipt"],request,state["clock"])
            if (execution["result"] is None) != (execution["accepted_at"] is None) or (execution["result"] is None) != (execution["result_use"] is None):
                _fail("STATE_INTEGRITY_MISMATCH", "result_use", "Saved completion state differs")
            if ledger.frozen_snapshot(request["snapshot"]["snapshot_id"]) != request["snapshot"]:
                _fail("STATE_INTEGRITY_MISMATCH", "snapshot", "Request Snapshot is not in the ledger")
        for task in state["tasks"].values():
            if not task["execution_ids"] and task["status"] not in {"CANDIDATE","PREPARING","NO_LONGER_ELIGIBLE"}:
                _fail("STATE_INTEGRITY_MISMATCH", "status", "Task status has no supporting execution")
            if task["execution_ids"]:
                last = state["executions"][task["execution_ids"][-1]]
                if task["status"] in {"RUNNING","WAITING"} and last["result"] is not None:
                    _fail("STATE_INTEGRITY_MISMATCH", "status", "Completed execution cannot remain running")
        owner._tasks,owner._executions,owner._batches = copy.deepcopy(state["tasks"]),copy.deepcopy(state["executions"]),copy.deepcopy(state["batches"])
        owner._edges,owner._rules,owner._seq = copy.deepcopy(state["edges"]),copy.deepcopy(state["rules"]),state["sequence"]
        owner._clock,owner._lifecycle,owner._state_entries = state["clock"],state["lifecycle"],copy.deepcopy(state["state_entries"])
        owner._closure,owner._triggers=copy.deepcopy(state.get("closure")),copy.deepcopy(state.get("triggers",{}))
        if owner._closure is not None:
            closing=owner._closure
            if not isinstance(closing,dict) or set(closing)!={"task_ids","execution_ids","drain_execution_ids","captured_at"}:
                _fail("STATE_INTEGRITY_MISMATCH","closure","Malformed captured ending work")
            for field,table in (("task_ids",owner._tasks),("execution_ids",owner._executions),("drain_execution_ids",owner._executions)):
                values=closing[field]
                if not isinstance(values,list) or len(set(values))!=len(values) or any(key not in table for key in values):
                    _fail("STATE_INTEGRITY_MISMATCH","closure","Captured ending work references unknown objects")
            if not set(closing["execution_ids"])<=set(closing["drain_execution_ids"]):
                _fail("STATE_INTEGRITY_MISMATCH","closure","Owned executions disappeared from the drain")
            for key in closing["task_ids"]:
                if not owner._definitions[owner._tasks[key]["task_definition_id"]]["completion_effect"]:
                    _fail("STATE_INTEGRITY_MISMATCH","closure","An ordinary task was added to ending work")
            for key,execution in owner._executions.items():
                if execution["request"].get("kind")=="hitl":continue
                if execution["result"] is None and key not in closing["drain_execution_ids"]:
                    _fail("STATE_INTEGRITY_MISMATCH","closure","Pending owned execution was removed from ending work")
                if key in closing["drain_execution_ids"] and key not in closing["execution_ids"] and execution["request"]["task_id"] not in closing["task_ids"]:
                    _fail("STATE_INTEGRITY_MISMATCH","closure","New ordinary work was added after completion capture")
            if timestamp(closing["captured_at"])>timestamp(owner._clock):
                _fail("STATE_INTEGRITY_MISMATCH","closure","Completion capture is in the future")
        owner._verification = PreflightVerifications.from_state(state["verifications"],event_receipt=ledger.event_receipt)
        owner._limited_verifications = copy.deepcopy(state["limited_verifications"])
        for key, value in owner._limited_verifications.items():
            if value["verification_id"] != key or canonical_hash({k:v for k,v in value.items() if k != "verification_id"}) != key:
                _fail("STATE_INTEGRITY_MISMATCH", "verification", "Saved bounded confirmation differs")
        for execution in owner._executions.values():
            if any(key not in owner.verifications for key in execution["request"]["verification_ids"]):
                _fail("STATE_INTEGRITY_MISMATCH", "verification", "Saved issued request lost its preparation records")
        recorded_use = {key:value["result_use"] for key,value in owner._executions.items()}
        owner._refresh_use()
        if any(recorded_use[key] != value["result_use"] for key,value in owner._executions.items()):
            _fail("STATE_INTEGRITY_MISMATCH", "result_use", "Saved result use is inconsistent with its recorded task")
        return owner
