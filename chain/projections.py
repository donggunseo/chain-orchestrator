"""Pure current-milestone and single main-State projection, without effects.

No workflow transition, entry fact, clinical task, lifecycle completion, Agent,
HITL, notification, effect or Timer is created. A fresh evaluator is used for
each immutable input frame. Historical first-result captures are read only.
"""
from __future__ import annotations

import copy
from collections import deque
import re

from .compiler import canonical_hash
from .errors import ConfigError
from .expressions import Evaluator, evaluate, _validate_value_cell
from .runtime_contracts import (VALIDITIES, canonical_source_refs, source_ref,
                                timestamp, validate_snapshot)
from .shape import check_json

PROJECTION_SCHEMA = "chain-fact-projection/v1"
_TYPES = {"string","integer","number","boolean","object"}
_TRUTHS = {"TRUE","FALSE","UNKNOWN"}
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")


def _fail(code, path, message):
    raise ConfigError(code, path, message) from None


def _closed(value, required, path, optional=()):
    check_json(value,path=path)
    if not isinstance(value,dict) or set(required)-set(value) or set(value)-set(required)-set(optional):
        _fail("PROJECTION_CONTRACT_INVALID",path,"Missing or unsupported fields")


def _text(value, path):
    if not isinstance(value,str) or not value.strip():
        _fail("PROJECTION_CONTRACT_INVALID",path,"Expected a nonempty string")


def _name(value, path):
    if not isinstance(value,str) or not _NAME.fullmatch(value):
        _fail("REFERENCE_INVALID",path,"Expected a declared identifier")


def _hash(value, path):
    if not isinstance(value,str) or not re.fullmatch(r"sha256:[0-9a-f]{64}",value):
        _fail("PROJECTION_HASH_INVALID",path,"Expected a SHA-256 hash")


def _matches(value, kind):
    return {"string":type(value) is str,"integer":type(value) is int,
            "number":type(value) in (int,float),"boolean":type(value) is bool,
            "object":type(value) is dict}.get(kind,False)


def _ref_name(ref):
    return ".".join([ref["namespace"],ref["name"],*ref["path"]])


def _unique_refs(refs):
    values={(r["system"],r["record_id"],r["version"],r["field"]):r for r in refs}
    return [copy.deepcopy(values[key]) for key in sorted(values)]


def _diagnostics(items):
    values={canonical_hash(item):item for item in items}
    return [copy.deepcopy(values[key]) for key in sorted(values)]


def _proof_union(items, truth):
    return {"truth":truth,"source_refs":_unique_refs([ref for item in items for ref in item["source_refs"]]),
            "dependencies":sorted({name for item in items for name in item["dependencies"]}),
            "diagnostics":_diagnostics([d for item in items for d in item["diagnostics"]])}


def _and(truths):
    return "FALSE" if "FALSE" in truths else "UNKNOWN" if "UNKNOWN" in truths else "TRUE"


def _logical_reference(value, path):
    _text(value,path)
    parts=value.split(".")
    if len(parts)<2: _fail("PROJECTION_CONTRACT_INVALID",path,"Expected a logical reference")
    for part in parts[1:]: _name(part,path)
    valid=(len(parts)==2 and parts[0] in {"facts","milestones","requirements"}
           or len(parts)==3 and parts[0]=="results"
           or len(parts)==3 and parts[0]=="decisions" and parts[2]=="value"
           or len(parts)==3 and parts[0]=="state_entries" and parts[2] in {"entered","generation"})
    if not valid: _fail("PROJECTION_CONTRACT_INVALID",path,"Unsupported logical reference")


def _check_names(values, path, *, logical=False, sorted_required=False):
    if not isinstance(values,list) or any(not isinstance(value,str) or not value for value in values):
        _fail("PROJECTION_CONTRACT_INVALID",path,"Expected unique string references")
    if len(set(values))!=len(values) or sorted_required and values!=sorted(values):
        _fail("PROJECTION_CONTRACT_INVALID",path,"References must be unique and canonical")
    for value in values:
        if logical: _logical_reference(value,path)
        else: _name(value,path)


def _check_diagnostics(items, path):
    if not isinstance(items,list): _fail("PROJECTION_CONTRACT_INVALID",path,"Expected diagnostic records")
    for item in items:
        _closed(item,{"code"},path,{"path","reference","validity"})
        _text(item["code"],path)
        if "path" in item: _text(item["path"],path)
        if "reference" in item: _logical_reference(item["reference"],path)
        if "validity" in item and (not isinstance(item["validity"],str) or item["validity"] not in VALIDITIES):
            _fail("PROJECTION_CONTRACT_INVALID",path,"Unsupported diagnostic validity")


def _check_proof(proof):
    _closed(proof,{"truth","source_refs","dependencies","diagnostics"},"evaluation")
    if not isinstance(proof["truth"],str) or proof["truth"] not in _TRUTHS:
        _fail("PROJECTION_CONTRACT_INVALID","evaluation.truth","Unsupported truth value")
    if proof["source_refs"]!=canonical_source_refs(proof["source_refs"]):
        _fail("PROJECTION_CONTRACT_INVALID","evaluation.source_refs","Evidence sources must be canonical")
    _check_names(proof["dependencies"],"evaluation.dependencies",logical=True,sorted_required=True)
    _check_diagnostics(proof["diagnostics"],"evaluation.diagnostics")


def _walk(expression):
    """Iterative walk, after closed-expression validation by the evaluator."""
    pending=[expression]
    while pending:
        item=pending.pop()
        if item.get("op") == "requirement":
            yield "requirement",item["requirement_id"]
        for arg in reversed(item.get("args",[])):
            if "op" in arg: pending.append(arg)
            elif arg.get("kind") == "ref": yield "ref",arg


def _topological(graph):
    incoming={name:len(deps) for name,deps in graph.items()}
    downstream={name:[] for name in graph}
    for name,deps in graph.items():
        for dependency in deps:
            if dependency not in graph: _fail("UNDEFINED_REFERENCE",name,"Derived dependency is not declared")
            downstream[dependency].append(name)
    ready=deque(sorted(name for name,count in incoming.items() if count==0))
    order=[]
    while ready:
        name=ready.popleft(); order.append(name)
        for target in sorted(downstream[name]):
            incoming[target]-=1
            if incoming[target]==0: ready.append(target)
    if len(order)!=len(graph): _fail("DEPENDENCY_CYCLE","definition.milestones","Cyclic milestone or predicate requirements")
    return order


class StateProjector:
    """Read a phase-2 definition; calculate without advancing either Engine.

    ``project`` takes normalized, already vetted value cells for decisions,
    results and entry history. ``project_ledger`` assembles them from the data
    ledger and internal accepted records. Full Agent/HITL acceptance is later
    work and is not performed by this calculator.
    """
    def __init__(self, definition):
        check_json(definition,path="definition")
        if not isinstance(definition,dict) or definition.get("definition_schema") != "chain-fact-definition/v1" or definition.get("contract_version") != "chain-fact-execution/v1":
            _fail("DEFINITION_VERSION_UNSUPPORTED","definition","A compiled fact definition v1 is required")
        required={"facts","states","state_projection","milestones","policy","tasks","decisions","modules","contexts","version","site_id","source_coverage"}
        if not required <= set(definition): _fail("DEFINITION_INVALID","definition","Missing projection definition fields")
        self._d=copy.deepcopy(definition)
        self.definition_hash=canonical_hash(definition)
        _text(self._d["version"],"definition.version"); _text(self._d["site_id"],"definition.site_id")
        for field in ("facts","states","milestones","tasks","decisions","modules","contexts","source_coverage"):
            if not isinstance(self._d[field],dict): _fail("DEFINITION_INVALID","definition."+field,"Expected named declarations")
            for name in self._d[field]: _name(name,"definition."+field)
        if not self._d["facts"] or not self._d["states"]: _fail("DEFINITION_INVALID","definition","Facts and States must be declared")
        for name,spec in self._d["decisions"].items():
            if not isinstance(spec,dict) or not isinstance(spec.get("options"),dict) or not spec["options"]:
                _fail("DEFINITION_INVALID","definition.decisions."+name,"Decision options must be declared")
        for name,spec in self._d["facts"].items():
            if not isinstance(spec,dict) or not isinstance(spec.get("type"),str) or spec["type"] not in _TYPES:
                _fail("DEFINITION_INVALID","definition.facts."+name,"Unsupported declared Fact type")
        for name,coverage in self._d["source_coverage"].items():
            if not isinstance(coverage,list) or any(not isinstance(field,str) for field in coverage) or len(set(coverage))!=len(coverage) or set(coverage)-set(self._d["facts"]):
                _fail("DEFINITION_INVALID","definition.source_coverage."+name,"Expected unique declared Fact fields")
        _closed(self._d["state_projection"],{"fallback","selection","rules"},"state_projection")
        projection=self._d["state_projection"]
        _name(projection["fallback"],"state_projection.fallback")
        if projection["selection"] != "highest_true_priority" or projection["fallback"] not in self._d["states"]:
            _fail("DEFINITION_INVALID","state_projection","Unsupported selection or undefined fallback")
        if not isinstance(projection["rules"],list): _fail("DEFINITION_INVALID","state_projection.rules","Expected projection rules")
        policy=self._d["policy"]
        if not isinstance(policy,dict) or not isinstance(policy.get("requirements"),dict):
            _fail("DEFINITION_INVALID","policy.requirements","Predicate requirements must be declared")
        self._predicates={}
        for name,spec in policy["requirements"].items():
            _name(name,"policy.requirements")
            if not isinstance(spec,dict) or spec.get("kind") not in ("predicate","hitl"):
                _fail("DEFINITION_INVALID","policy.requirements."+name,"Unsupported requirement kind")
            if spec["kind"]=="predicate":
                _closed(spec,{"kind","when","origin_refs"},"policy.requirements."+name)
                self._predicates[name]=spec["when"]
        ids=set(); priorities=set()
        for rule in projection["rules"]:
            _closed(rule,{"id","state","priority","when","requirement_refs","origin_refs"},"state_projection.rule")
            _name(rule["id"],"state_projection.rule.id")
            _name(rule["state"],"state_projection.rule.state")
            if rule["id"] in ids: _fail("DUPLICATE_RULE","state_projection.rules","Duplicate rule ID")
            ids.add(rule["id"])
            if type(rule["priority"]) is not int or rule["priority"] < 0:
                _fail("INVALID_PRIORITY","state_projection.rules","Priority must be a nonnegative integer")
            if rule["priority"] in priorities: _fail("DUPLICATE_PRIORITY","state_projection.rules","Priority ties are unsupported")
            priorities.add(rule["priority"])
            if rule["state"] not in self._d["states"]: _fail("UNDEFINED_REFERENCE","state_projection.rule.state","State is not declared")
            if (not isinstance(rule["requirement_refs"],list) or any(not isinstance(name,str) for name in rule["requirement_refs"])
                    or len(set(rule["requirement_refs"]))!=len(rule["requirement_refs"])):
                _fail("DEFINITION_INVALID","state_projection.rule.requirement_refs","Expected unique predicate IDs")
            for name in rule["requirement_refs"]:
                if name not in self._predicates: _fail("REQUIREMENT_KIND_MISMATCH","state_projection.rule.requirement_refs","A State condition requires a declared predicate")
        self._rules=sorted(copy.deepcopy(projection["rules"]),key=lambda row:(-row["priority"],row["id"]))
        graph={"requirements."+name:set() for name in self._predicates}
        for name,spec in self._d["milestones"].items():
            if not isinstance(spec,dict): _fail("DEFINITION_INVALID","milestones."+name,"Expected a milestone declaration")
            if "when" in spec:
                _closed(spec,{"when","origin_refs"},"milestones."+name)
            else:
                _closed(spec,{"capture_first","type","origin_refs"},"milestones."+name)
                if not isinstance(spec["type"],str) or spec["type"] not in _TYPES:
                    _fail("DEFINITION_INVALID","milestones."+name,"Unsupported capture type")
                self._declared_type(spec["capture_first"])
                if spec["capture_first"]["namespace"] != "results":
                    _fail("CAPTURE_CONTRACT_MISMATCH","milestones."+name,"Historical capture must refer to an Agent result")
            graph["milestones."+name]=set()
        expressions={"requirements."+name:expr for name,expr in self._predicates.items()}
        expressions.update({"milestones."+name:spec["when"] for name,spec in self._d["milestones"].items() if "when" in spec})
        for name,expr in expressions.items():
            self._validate_expression(expr)
            for kind,ref in _walk(expr):
                if kind=="requirement": graph[name].add("requirements."+ref)
                elif ref["namespace"]=="milestones": graph[name].add("milestones."+ref["name"])
        self._order=_topological(graph)
        for rule in self._rules: self._validate_expression(rule["when"])
        # One derived calculation only; neither cached input nor session state
        # is exported/restored or shared between pinned projector instances.
        self._projection_cache = None

    def _evaluator(self, *, resolve, max_depth=96, max_nodes=10000):
        return Evaluator._from_owned_requirements(resolve=resolve, requirements=self._predicates,
            max_depth=max_depth, max_nodes=max_nodes)

    def _declared_type(self, ref):
        _closed(ref,{"kind","namespace","name","path"},"reference")
        if ref["kind"] != "ref" or not isinstance(ref["path"],list): _fail("REFERENCE_INVALID","reference","Expected compiled reference")
        namespace,name,path=ref["namespace"],ref["name"],ref["path"]
        _name(name,"reference.name")
        if namespace=="facts" and not path and name in self._d["facts"]: return self._d["facts"][name]["type"]
        if namespace=="milestones" and not path and name in self._d["milestones"]: return self._d["milestones"][name].get("type","boolean")
        if namespace=="decisions" and path==["value"] and name in self._d["decisions"]: return "string"
        if namespace=="state_entries" and name in self._d["states"]:
            if path==["entered"]: return "boolean"
            if path==["generation"]: return "integer"
        if namespace=="results" and len(path)==1 and name in self._d["tasks"]:
            if path[0] in self._result_fields(name): return None
        _fail("UNDEFINED_REFERENCE","reference","Reference or supported field is not declared")

    def _result_fields(self, name):
        task=self._d["tasks"].get(name)
        if not isinstance(task,dict) or task.get("kind")!="agent" or not all(isinstance(task.get(key),str) for key in ("agent","mode","context")):
            _fail("RESULT_CONTRACT_MISMATCH","reference","Result must identify an Agent task")
        spec=self._d["modules"].get(task["agent"])
        if not spec: _fail("RESULT_CONTRACT_MISSING","reference","Referenced result needs public module metadata")
        if not isinstance(spec,dict) or not isinstance(spec.get("modes"),dict):
            _fail("RESULT_CONTRACT_MISMATCH","reference","Invalid public result metadata")
        mode=spec["modes"].get(task["mode"])
        if not isinstance(mode,dict) or set(mode)!={"output_fields"}:
            _fail("RESULT_CONTRACT_MISMATCH","reference","Mode output fields are not declared")
        fields=mode["output_fields"]
        if not isinstance(fields,list) or not fields or any(not isinstance(field,str) for field in fields) or len(set(fields))!=len(fields):
            _fail("RESULT_CONTRACT_MISMATCH","reference","Expected unique output field names")
        context=self._d["contexts"].get(task["context"])
        scope=context.get("fields") if isinstance(context,dict) else None
        if not isinstance(scope,list) or any(not isinstance(field,str) for field in scope) or len(set(scope))!=len(scope) or set(scope)-set(self._d["facts"]):
            _fail("RESULT_SCOPE_MISMATCH","reference","Task input scope is not declared")
        return fields

    def _validate_expression(self, expr):
        def unknown(ref):
            kind=self._declared_type(ref)
            return {"value":None,"validity":"UNKNOWN","source_refs":[],**({"value_type":kind} if kind else {})}
        self._evaluator(resolve=unknown).evaluate(expr)

    def _cell(self, value, *, kind, reference, evaluated_at):
        check_json(value,path=reference)
        if not isinstance(value,dict): _fail("VALUE_CELL_INVALID",reference,"Expected a normalized value cell")
        # The evaluator validates closed cells and their typed/null/provenance
        # contract; doing it here also checks supplied but unreferenced cells.
        cell=copy.deepcopy(value)
        if kind:
            if "value_type" in cell and cell["value_type"] != kind: _fail("FACT_TYPE_MISMATCH",reference,"Value type differs from the declaration")
            cell["value_type"]=kind
        if any(field in cell and timestamp(cell[field]) > timestamp(evaluated_at) for field in ("known_at","source_time")):
            _fail("FUTURE_INPUT",reference,"Input was not known at the evaluation time")
        if reference.startswith("state_entries.") and reference.endswith(".generation") and cell.get("validity")=="KNOWN" and (type(cell.get("value")) is not int or cell["value"]<0):
            _fail("VALUE_CELL_INVALID",reference,"Recorded entry generation must be a nonnegative integer")
        _validate_value_cell({"kind":"ref","namespace":reference.split(".")[0],
            "name":reference.split(".")[1],"path":reference.split(".")[2:]}, cell)
        return cell

    def _state_entry_cells(self, state_entries, evaluated_at):
        cells = {}
        for name,fields in state_entries.items():
            if name not in self._d["states"]: _fail("UNDEFINED_REFERENCE","state_entries","Entry State is not declared")
            if not isinstance(fields,dict): _fail("VALUE_CELL_INVALID","state_entries","Expected recorded entry fields")
            cells[name]={}
            for field,cell in fields.items():
                ref={"kind":"ref","namespace":"state_entries","name":name,"path":[field]}
                kind=self._declared_type(ref)
                cells[name][field]=self._cell(cell,kind=kind,reference=_ref_name(ref),evaluated_at=evaluated_at)
        return cells

    def validate_state_entries(self, state_entries, *, evaluated_at):
        """Validate detached entry cells without calculating unrelated State rules."""
        timestamp(evaluated_at)
        check_json(state_entries, path="state_entries")
        if not isinstance(state_entries, dict):
            _fail("PROJECTION_CONTRACT_INVALID", "state_entries", "Expected named normalized inputs")
        return self._state_entry_cells(copy.deepcopy(state_entries), evaluated_at)

    @staticmethod
    def _unknown(kind, reference, code="NOT_RECEIVED"):
        return {"value":None,"validity":"UNKNOWN","source_refs":[],
                **({"value_type":kind} if kind else {}),"dependencies":[reference],
                "diagnostics":[{"code":code,"reference":reference,"validity":"UNKNOWN"}]}

    def project(self, snapshot, *, evaluated_at, captures=None, decisions=None, results=None,
                state_entries=None, pending_changes=()):
        """Calculate using immutable, normalized inputs; never mutate the ledger."""
        snapshot=validate_snapshot(snapshot)
        now=timestamp(evaluated_at)
        if snapshot["identity"]["site_id"] != self._d["site_id"]:
            _fail("IDENTITY_MISMATCH","snapshot.identity","Snapshot site differs from the definition")
        if timestamp(snapshot["known_at"]) > now: _fail("FUTURE_INPUT","snapshot.known_at","Snapshot was not known at this time")
        for name in snapshot["facts"]:
            if name not in self._d["facts"]: _fail("UNDEFINED_REFERENCE","snapshot.facts","Snapshot contains undeclared facts")
        captures={} if captures is None else copy.deepcopy(captures)
        decisions={} if decisions is None else copy.deepcopy(decisions)
        results={} if results is None else copy.deepcopy(results)
        state_entries={} if state_entries is None else copy.deepcopy(state_entries)
        for name,value in (("captures",captures),("decisions",decisions),("results",results),("state_entries",state_entries)):
            check_json(value,path=name)
            if not isinstance(value,dict): _fail("PROJECTION_CONTRACT_INVALID",name,"Expected named normalized inputs")
        if not isinstance(pending_changes,(list,tuple)):
            _fail("PROJECTION_CONTRACT_INVALID","pending_changes","Expected an explicit unresolved-change list")
        pending=copy.deepcopy(list(pending_changes)); check_json(pending,path="pending_changes")
        # Snapshot integrity and the complete JSON inputs are checked before a
        # hit. Only byte-equivalent normalized inputs at the same time reuse a
        # prior fully validated result. New/unreferenced cells still miss and
        # run all cell/provenance/type/time checks below.
        input_hash=canonical_hash({"snapshot":snapshot,"captures":captures,"decisions":decisions,"results":results,
                                  "state_entries":state_entries,"pending_changes":sorted(pending,key=canonical_hash)})
        cache_key=(evaluated_at,input_hash)
        cached = self._projection_cache
        if cached is not None and cache_key == cached[0]:
            return copy.deepcopy(cached[1])
        affected={}; pending_ids={}
        for item in pending:
            _closed(item,{"event_id","source","source_ref","fields","status"},"pending_changes")
            _text(item["event_id"],"pending_changes.event_id"); source_ref(item["source_ref"])
            if not isinstance(item["source"],str) or item["source"] not in self._d["source_coverage"] or item["status"] not in ("PENDING","FAILED"):
                _fail("PROJECTION_CONTRACT_INVALID","pending_changes","Unknown source or unresolved status")
            if (not isinstance(item["fields"],list) or any(not isinstance(field,str) for field in item["fields"]) or len(set(item["fields"]))!=len(item["fields"])
                    or not set(item["fields"]) <= set(self._d["source_coverage"][item["source"]])):
                _fail("PROJECTION_CONTRACT_INVALID","pending_changes.fields","Pending fields exceed the source coverage")
            for field in item["fields"]:
                affected[field]=True; pending_ids.setdefault("facts."+field,[]).append(item["event_id"])
        cells={"facts":{},"milestones":{},"decisions":{},"results":{},"state_entries":{}}
        for name,spec in self._d["facts"].items():
            reference="facts."+name
            raw=snapshot["facts"].get(name)
            cell=self._unknown(spec["type"],reference,"OUTSIDE_INPUT_SCOPE") if raw is None else self._cell(raw,kind=spec["type"],reference=reference,evaluated_at=evaluated_at)
            if name in affected:
                cell.update(value=None,validity="PENDING")
                cell["diagnostics"]=[{"code":"SOURCE_UPDATE_PENDING","reference":reference,"validity":"PENDING"}]
            cells["facts"][name]=cell
        for name,cell in decisions.items():
            ref={"kind":"ref","namespace":"decisions","name":name,"path":["value"]}
            kind=self._declared_type(ref); reference=_ref_name(ref)
            normalized=self._cell(cell,kind=kind,reference=reference,evaluated_at=evaluated_at)
            if normalized["validity"]=="KNOWN" and normalized["value"] not in self._d["decisions"][name]["options"]:
                _fail("DECISION_VALUE_INVALID",reference,"Decision value is not a declared option")
            cells["decisions"][name]=normalized
        for name,fields in results.items():
            self._result_fields(name)
            if not isinstance(fields,dict): _fail("VALUE_CELL_INVALID","results","Expected normalized result fields")
            cells["results"][name]={}
            for field,cell in fields.items():
                ref={"kind":"ref","namespace":"results","name":name,"path":[field]}
                kind=self._declared_type(ref)
                cells["results"][name][field]=self._cell(cell,kind=kind,reference=_ref_name(ref),evaluated_at=evaluated_at)
        cells["state_entries"]=self._state_entry_cells(state_entries, evaluated_at)
        milestone_results={}
        for name,capture in captures.items():
            if name not in self._d["milestones"] or "capture_first" not in self._d["milestones"][name]:
                _fail("CAPTURE_CONTRACT_MISMATCH","captures","Historical capture definition is not declared")
            _closed(capture,{"definition_id","task_definition_id","execution_id","input_snapshot_id","value","rule_version","historical",
                            "source_refs","accepted_reducer_seq","known_at","capture_id"},"capture")
            spec=self._d["milestones"][name]
            if (capture["definition_id"]!=name or capture["task_definition_id"]!=spec["capture_first"]["name"] or capture["historical"] is not True
                    or capture["rule_version"]!=self._d["version"] or not _matches(capture["value"],spec["type"])):
                _fail("CAPTURE_CONTRACT_MISMATCH","capture","Capture does not match its declared historical rule")
            if capture["capture_id"] != canonical_hash({k:v for k,v in capture.items() if k!="capture_id"}):
                _fail("PROJECTION_HASH_INVALID","capture.capture_id","Capture content hash differs")
            _hash(capture["input_snapshot_id"],"capture.input_snapshot_id"); _text(capture["execution_id"],"capture.execution_id")
            if type(capture["accepted_reducer_seq"]) is not int or capture["accepted_reducer_seq"]<1:
                _fail("CAPTURE_CONTRACT_MISMATCH","capture.accepted_reducer_seq","Expected a recorded positive reducer sequence")
            if timestamp(capture["known_at"]) > now: _fail("FUTURE_INPUT","capture.known_at","Historical capture was not known yet")
            cell={"value":copy.deepcopy(capture["value"]),"validity":"KNOWN","source_refs":canonical_source_refs(capture["source_refs"]),
                  "value_type":spec["type"],"dependencies":["milestones."+name],"diagnostics":[]}
            cells["milestones"][name]=cell
            milestone_results[name]={"cell":cell,"kind":"HISTORICAL_CAPTURE","rule_id":name,
                "rule_version":self._d["version"],"evaluation":None,"capture_id":capture["capture_id"]}
        for name,spec in self._d["milestones"].items():
            if "capture_first" in spec and name not in cells["milestones"]:
                cell=self._unknown(spec["type"],"milestones."+name,"HISTORICAL_CAPTURE_NOT_RECORDED")
                cells["milestones"][name]=cell
                milestone_results[name]={"cell":cell,"kind":"HISTORICAL_CAPTURE","rule_id":name,
                    "rule_version":self._d["version"],"evaluation":None,"capture_id":None}
        resolved={}
        def resolve(ref):
            kind=self._declared_type(ref); reference=_ref_name(ref); ns,name,path=ref["namespace"],ref["name"],ref["path"]
            if ns in {"facts","milestones"}: cell=cells[ns][name]
            elif ns=="decisions": cell=cells[ns].get(name,self._unknown(kind,reference))
            else: cell=cells[ns].get(name,{}).get(path[0],self._unknown(kind,reference))
            resolved[reference]=cell
            return copy.deepcopy(cell)
        evaluator=self._evaluator(resolve=resolve)
        for node in self._order:
            namespace,name=node.split(".",1)
            if namespace=="requirements":
                evaluator.evaluate({"op":"requirement","requirement_id":name})
            elif "when" in self._d["milestones"][name]:
                proof=evaluator.evaluate(self._d["milestones"][name]["when"])
                cell={"value":proof["truth"]=="TRUE" if proof["truth"]!="UNKNOWN" else None,
                      "validity":"KNOWN" if proof["truth"]!="UNKNOWN" else "UNKNOWN","value_type":"boolean",
                      "source_refs":copy.deepcopy(proof["source_refs"]),"dependencies":copy.deepcopy(proof["dependencies"]),
                      "diagnostics":copy.deepcopy(proof["diagnostics"])}
                cells["milestones"][name]=cell
                milestone_results[name]={"cell":cell,"kind":"PREDICATE","rule_id":name,
                    "rule_version":self._d["version"],"evaluation":proof,"capture_id":None}
        rules=[]; proofs=[]
        for rule in self._rules:
            when=evaluator.evaluate(rule["when"])
            requirements={name:evaluator.evaluate({"op":"requirement","requirement_id":name}) for name in sorted(rule["requirement_refs"])}
            truth=_and([when["truth"],*[item["truth"] for item in requirements.values()]])
            proofs.append(_proof_union([when,*requirements.values()],truth))
            rules.append({"rule_id":rule["id"],"state":rule["state"],"priority":rule["priority"],
                          "when":when,"requirements":requirements,"truth":truth})
        selected=next((index for index,row in enumerate(rules) if row["truth"]=="TRUE"),None)
        selection=rules[selected] if selected is not None else None
        evidence=proofs[selected] if selected is not None else _proof_union(proofs,"UNKNOWN" if any(p["truth"]=="UNKNOWN" for p in proofs) else "FALSE")
        unknown_ids=[row["rule_id"] for row in rules if row["truth"]=="UNKNOWN"]
        diag=_diagnostics([item for proof in proofs for item in proof["diagnostics"]])
        unresolved=[]
        for item in diag:
            reference=item.get("reference")
            if reference is None: continue
            cell=resolved.get(reference)
            if cell is not None and cell["validity"]!="KNOWN":
                unresolved.append({"reference":reference,"validity":cell["validity"],"code":item["code"],
                                   "event_ids":sorted(set(pending_ids.get(reference,[])))})
        result={"projection_schema":PROJECTION_SCHEMA,"identity":copy.deepcopy(snapshot["identity"]),
            "engine_run_id":snapshot["engine_run_id"],"run_pin":snapshot["run_pin"],"definition_hash":self.definition_hash,
            "input_hash":input_hash,"snapshot_id":snapshot["snapshot_id"],"evaluated_at":evaluated_at,
            "state":selection["state"] if selection else self._d["state_projection"]["fallback"],
            "selected_rule_id":selection["rule_id"] if selection else None,"selected_priority":selection["priority"] if selection else None,
            "selection_kind":"RULE" if selection else "FALLBACK","reason":"HIGHEST_TRUE_PRIORITY" if selection else "NO_RULE_TRUE",
            "source_refs":evidence["source_refs"],"dependencies":evidence["dependencies"],"unknown":bool(unknown_ids),
            "unresolved_rule_ids":unknown_ids,"higher_priority_unknown":[row["rule_id"] for row in rules
                if row["truth"]=="UNKNOWN" and (selection is None or row["priority"]>selection["priority"])],
            "unresolved_inputs":_diagnostics(unresolved),"milestones":milestone_results,"rules":rules}
        result["projection_id"]=canonical_hash(result)
        validated=validate_projection(result)
        self._projection_cache = (cache_key, copy.deepcopy(validated))
        return validated

    def project_ledger(self, ledger, *, evaluated_at, results=None, state_entries=None, pending_changes=None):
        """Read the ledger only; never freeze/append/accept or invoke anything."""
        if ledger.definition_hash != self.definition_hash:
            _fail("DEFINITION_PIN_MISMATCH","ledger","Ledger and projector definitions differ")
        if timestamp(evaluated_at) < timestamp(ledger.processed_at):
            _fail("FUTURE_INPUT","ledger","Cannot calculate before the last processed input")
        decisions={}; latest={}
        for item in ledger.decisions.values():
            record=item["record"]; name=record["decision_definition_id"]
            if name not in self._d["decisions"]: _fail("UNDEFINED_REFERENCE","decision_definition_id","Accepted decision is outside this definition")
            if name not in latest or item["accepted_reducer_seq"]>latest[name]["accepted_reducer_seq"]: latest[name]=item
        for name,item in latest.items():
            record=item["record"]; evidence=ledger.frozen_snapshot(record["evidence_snapshot_id"])
            decisions[name]={"value":record["decision"],"validity":"KNOWN","value_type":"string",
                "source_refs":_unique_refs([ref for fact in evidence["facts"].values() for ref in fact["source_refs"]]),
                "known_at":item["applied_at"],"dependencies":["decisions."+name+".value"],"diagnostics":[]}
        normalized_results={}
        if results is not None:
            check_json(results,path="results")
            if not isinstance(results,dict): _fail("PROJECTION_CONTRACT_INVALID","results","Expected vetted result records by task")
        for task_id,record in ({} if results is None else results).items():
            _closed(record,{"execution_id","status","usage","input_snapshot_id","values","known_at"},"results."+task_id)
            _text(record["execution_id"],"execution_id")
            if record["status"] not in ("SUCCESS","FAILED") or record["usage"] not in ("CURRENT","REFERENCE_OLD_INPUT"):
                _fail("RESULT_CONTRACT_MISMATCH","results","Expected an already vetted result status and usage")
            if not isinstance(record["values"],dict) or (record["status"]=="FAILED" and record["values"]):
                _fail("RESULT_CONTRACT_MISMATCH","results.values","Failed results cannot carry successful values")
            task=self._d["tasks"].get(task_id)
            fields=self._result_fields(task_id)
            snapshot=ledger.frozen_snapshot(record["input_snapshot_id"])
            if set(snapshot["facts"]) != set(self._d["contexts"][task["context"]]["fields"]):
                _fail("RESULT_SCOPE_MISMATCH","results.input_snapshot_id","Result Snapshot differs from the task's declared input scope")
            if not timestamp(snapshot["known_at"]) <= timestamp(record["known_at"]) <= timestamp(evaluated_at):
                _fail("FUTURE_INPUT","results.known_at","Result is outside its input/evaluation time interval")
            current=record["status"]=="SUCCESS" and record["usage"]=="CURRENT" and ledger.input_is_current(record["input_snapshot_id"],pending_changes=pending_changes)
            validity="KNOWN" if current else "ERROR" if record["status"]=="FAILED" else "INVALIDATED"
            normalized_results[task_id]={}
            if set(record["values"])-set(fields): _fail("RESULT_CONTRACT_MISMATCH","results.values","Result fields exceed the public module contract")
            for field in fields:
                cell_validity=validity if field in record["values"] or validity!="KNOWN" else "UNKNOWN"
                reference="results."+task_id+"."+field
                normalized_results[task_id][field]={"value":copy.deepcopy(record["values"][field]) if cell_validity=="KNOWN" else None,
                    "validity":cell_validity,"source_refs":_unique_refs([ref for fact in snapshot["facts"].values() for ref in fact["source_refs"]]),
                    "known_at":record["known_at"],"dependencies":[reference],"diagnostics":[] if cell_validity=="KNOWN" else
                    [{"code":"RESULT_NOT_CURRENT","reference":reference,"validity":cell_validity}]}
        return self.project(ledger.snapshot(),evaluated_at=evaluated_at,captures=ledger.captures,decisions=decisions,
                            results=normalized_results,state_entries=state_entries,
                            pending_changes=ledger.pending_changes if pending_changes is None else pending_changes)


def validate_projection(value):
    """Validate detached calculation output, including its selection and hash."""
    required={"projection_schema","identity","engine_run_id","run_pin","definition_hash","input_hash","snapshot_id","evaluated_at",
        "state","selected_rule_id","selected_priority","selection_kind","reason","source_refs","dependencies","unknown",
        "unresolved_rule_ids","higher_priority_unknown","unresolved_inputs","milestones","rules","projection_id"}
    _closed(value,required,"projection")
    if value["projection_schema"]!=PROJECTION_SCHEMA: _fail("PROJECTION_CONTRACT_INVALID","projection_schema","Unsupported projection version")
    for key in ("projection_id","run_pin","definition_hash","input_hash","snapshot_id"): _hash(value[key],key)
    if value["projection_id"]!=canonical_hash({k:v for k,v in value.items() if k!="projection_id"}):
        _fail("PROJECTION_HASH_INVALID","projection_id","Projection content hash differs")
    _closed(value["identity"],{"site_id","patient_id","encounter_id","episode_id"},"identity")
    for item in value["identity"].values(): _text(item,"identity")
    _text(value["engine_run_id"],"engine_run_id"); _name(value["state"],"state"); timestamp(value["evaluated_at"])
    if type(value["unknown"]) is not bool: _fail("PROJECTION_CONTRACT_INVALID","unknown","Expected an explicit uncertainty flag")
    if value["source_refs"]!=canonical_source_refs(value["source_refs"]): _fail("PROJECTION_CONTRACT_INVALID","source_refs","Sources must be canonical")
    _check_names(value["dependencies"],"dependencies",logical=True,sorted_required=True)
    for key in ("unresolved_rule_ids","higher_priority_unknown"): _check_names(value[key],key)
    if not isinstance(value["rules"],list) or not isinstance(value["milestones"],dict) or not isinstance(value["unresolved_inputs"],list):
        _fail("PROJECTION_CONTRACT_INVALID","projection","Expected rule, milestone and uncertainty records")
    ids=set(); priorities=set()
    for row in value["rules"]:
        _closed(row,{"rule_id","state","priority","when","requirements","truth"},"rule")
        _name(row["rule_id"],"rule.rule_id"); _name(row["state"],"rule.state")
        if row["rule_id"] in ids or type(row["priority"]) is not int or row["priority"]<0 or row["priority"] in priorities:
            _fail("PROJECTION_CONTRACT_INVALID","rules","Duplicate rule or priority")
        ids.add(row["rule_id"]); priorities.add(row["priority"])
        if not isinstance(row["requirements"],dict): _fail("PROJECTION_CONTRACT_INVALID","rule.requirements","Expected predicate evaluations")
        for name in row["requirements"]: _name(name,"rule.requirements")
        for proof in [row["when"],*row["requirements"].values()]: _check_proof(proof)
        if not isinstance(row["truth"],str) or row["truth"] not in _TRUTHS: _fail("PROJECTION_CONTRACT_INVALID","rule.truth","Unsupported combined truth")
        if row["truth"]!=_and([row["when"]["truth"],*[p["truth"] for p in row["requirements"].values()]]):
            _fail("PROJECTION_CONTRACT_INVALID","rule.truth","Combined condition differs from its predicates")
    ordered=sorted(value["rules"],key=lambda row:(-row["priority"],row["rule_id"]))
    if value["rules"]!=ordered: _fail("PROJECTION_CONTRACT_INVALID","rules","Rules must use explicit priority order")
    chosen=next((row for row in ordered if row["truth"]=="TRUE"),None)
    if chosen:
        if (type(value["selected_priority"]) is not int or value["selected_priority"]<0
                or value["selection_kind"]!="RULE" or value["reason"]!="HIGHEST_TRUE_PRIORITY" or value["state"]!=chosen["state"]
                or value["selected_rule_id"]!=chosen["rule_id"] or value["selected_priority"]!=chosen["priority"]):
            _fail("PROJECTION_CONTRACT_INVALID","selection","Selected State is not the highest TRUE candidate")
    elif value["selection_kind"]!="FALLBACK" or value["reason"]!="NO_RULE_TRUE" or value["selected_rule_id"] is not None or value["selected_priority"] is not None:
        _fail("PROJECTION_CONTRACT_INVALID","selection","A fallback cannot claim a successful rule")
    unknown=[row["rule_id"] for row in ordered if row["truth"]=="UNKNOWN"]
    higher=[row["rule_id"] for row in ordered if row["truth"]=="UNKNOWN" and (chosen is None or row["priority"]>chosen["priority"])]
    if value["unknown"]!=bool(unknown) or value["unresolved_rule_ids"]!=unknown or value["higher_priority_unknown"]!=higher:
        _fail("PROJECTION_CONTRACT_INVALID","unknown","Uncertainty differs from the evaluated candidates")
    selected_rows=[chosen] if chosen is not None else ordered
    proof=_proof_union([proof for row in selected_rows for proof in [row["when"],*row["requirements"].values()]],"TRUE")
    if value["source_refs"]!=proof["source_refs"] or value["dependencies"]!=proof["dependencies"]:
        _fail("PROJECTION_CONTRACT_INVALID","selection.evidence","Selection evidence differs from its evaluated rules")
    for name,milestone in value["milestones"].items():
        _name(name,"milestones")
        _closed(milestone,{"cell","kind","rule_id","rule_version","evaluation","capture_id"},"milestone")
        _name(milestone["rule_id"],"milestone.rule_id"); _text(milestone["rule_version"],"milestone.rule_version")
        if milestone["rule_id"]!=name: _fail("PROJECTION_CONTRACT_INVALID","milestone.rule_id","Milestone name differs from its rule ID")
        evaluate({"op":"is_known","args":[{"kind":"ref","namespace":"milestones","name":name,"path":[]}]},resolve=lambda _:milestone["cell"])
        cell=milestone["cell"]
        if milestone["kind"]=="PREDICATE":
            _check_proof(milestone["evaluation"])
            expected={"TRUE":True,"FALSE":False,"UNKNOWN":None}[milestone["evaluation"]["truth"]]
            if (milestone["capture_id"] is not None or cell.get("value_type")!="boolean" or type(cell["value"]) is not type(expected)
                    or cell["value"]!=expected or cell["validity"]!=("UNKNOWN" if expected is None else "KNOWN")
                    or cell["source_refs"]!=milestone["evaluation"]["source_refs"]
                    or cell.get("dependencies")!=milestone["evaluation"]["dependencies"]
                    or cell.get("diagnostics")!=milestone["evaluation"]["diagnostics"]):
                _fail("PROJECTION_CONTRACT_INVALID","milestone","Predicate value differs from its evaluation")
        elif milestone["kind"]=="HISTORICAL_CAPTURE":
            if milestone["evaluation"] is not None: _fail("PROJECTION_CONTRACT_INVALID","milestone","Historical capture is not a current predicate")
            if milestone["capture_id"] is None:
                if cell["validity"]!="UNKNOWN": _fail("PROJECTION_CONTRACT_INVALID","milestone","Unrecorded capture must be unknown")
            else:
                _hash(milestone["capture_id"],"capture_id")
                if cell["validity"]!="KNOWN": _fail("PROJECTION_CONTRACT_INVALID","milestone","Recorded historical capture must retain its historical value")
        else: _fail("PROJECTION_CONTRACT_INVALID","milestone.kind","Unsupported milestone kind")
    for item in value["unresolved_inputs"]:
        _closed(item,{"reference","validity","code","event_ids"},"unresolved_input")
        _logical_reference(item["reference"],"unresolved_input.reference"); _text(item["code"],"unresolved_input.code")
        if not isinstance(item["validity"],str) or item["validity"] not in VALIDITIES or item["validity"]=="KNOWN":
            _fail("PROJECTION_CONTRACT_INVALID","unresolved_input.validity","Expected non-known input validity")
        if not isinstance(item["event_ids"],list) or any(not isinstance(e,str) or not e for e in item["event_ids"]) or len(set(item["event_ids"]))!=len(item["event_ids"]):
            _fail("PROJECTION_CONTRACT_INVALID","unresolved_input.event_ids","Expected unique pending Event IDs")
    return copy.deepcopy(value)
