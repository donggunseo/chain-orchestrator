"""Deterministic Episode data history, independent of clinical State and work.

All time, source resolutions and accepted decision/result records are supplied
by the caller. No file/network access, Agent invocation, condition evaluation,
State transition, HITL acceptance or Temporal command is performed here.
"""
from __future__ import annotations

import copy
from datetime import timedelta
import re

from .common.contracts import validate_event
from .common.source_contracts import validate_revision

from .compiler import canonical_hash
from .errors import ConfigError
from .provenance import ProvenanceIndex
from .runtime_contracts import (canonical_source_refs, normalize_completion, source_ref, source_validity,
                                timestamp, validate_identity, validate_initial,
                                validate_scope, validate_snapshot)
from .shape import check_json

_INITIAL = {"site_id","patient_id","encounter_id","episode_id","age","sex","bed","arrival_time"}
_TYPES = {"string","integer","number","boolean","object"}
_BAD = {"ERROR","RETRACTED","INVALIDATED","UNKNOWN","PENDING","CONFLICT"}
_REV_EVENTS = {"SOURCE_CORRECTED","SOURCE_RETRACTED","SOURCE_ERROR_REPORTED","SOURCE_CONFLICT_DECLARED"}


def _fail(code, path, message):
    raise ConfigError(code, path, message) from None


def _closed(value, fields, path):
    check_json(value, path=path)
    if not isinstance(value, dict) or set(value) != set(fields):
        _fail("CONTRACT_SCHEMA_INVALID", path, "Missing or unsupported fields")


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        _fail("CONTRACT_SCHEMA_INVALID", path, "Expected a nonempty string")


def _hash(value, path):
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        _fail("CONTRACT_HASH_INVALID", path, "Expected a SHA-256 hash")


def _ref_key(ref):
    return ref["system"],ref["record_id"],ref["version"],ref.get("field", "")


def _record_key(ref):
    return _ref_key(ref)[:3]


def _without_time(fact):
    return {k:v for k,v in fact.items() if k != "known_at"}


def _type_matches(value, kind):
    return {"string":type(value) is str,"integer":type(value) is int,
            "number":type(value) in (int,float),"boolean":type(value) is bool,
            "object":type(value) is dict}.get(kind, False)


class FactLedger:
    """Single-writer pure data foundation, not the new orchestration Engine.

    ``definition`` is a phase-2 compiled public definition. ``source_systems``
    is the Worker-approved logical-source/system mapping; optional status maps
    are supplied separately by that trusted normalization layer. Source values
    remain separate from derived records, historical captures and decisions.
    """

    def __init__(self, initial, *, definition, source_systems, engine_run_id,
                 run_pin, known_at, resolution_limits=None, status_mappings=None):
        initial = validate_initial(initial)
        check_json(definition)
        if definition.get("definition_schema") != "chain-fact-definition/v1" or definition.get("contract_version") != "chain-fact-execution/v1":
            _fail("DEFINITION_VERSION_UNSUPPORTED", "definition", "A compiled fact definition v1 is required")
        if definition.get("site_id") != initial["site_id"]:
            _fail("IDENTITY_MISMATCH", "initial.site_id", "Definition and Episode sites differ")
        definitions = definition.get("facts")
        if not isinstance(definitions, dict) or not definitions:
            _fail("DEFINITION_INVALID", "definition.facts", "Fact declarations are required")
        for name, spec in definitions.items():
            _text(name,"definition.facts")
            if (not isinstance(spec,dict) or not isinstance(spec.get("type"),str) or spec["type"] not in _TYPES
                    or not isinstance(spec.get("source"),str) or spec["source"] not in {"initial","external"}):
                _fail("DEFINITION_INVALID", "definition.facts", "Unsupported Fact declaration")
        if {n for n,s in definitions.items() if s["source"] == "initial"} != _INITIAL:
            _fail("INVALID_INITIAL_FACT", "definition.facts", "Exactly the eight creation fields are initial")
        for name,value in initial.items():
            if not _type_matches(value,definitions[name]["type"]):
                _fail("FACT_TYPE_MISMATCH", "initial."+name, "Initial value differs from its declared type")
        coverage = definition.get("source_coverage")
        if not isinstance(coverage,dict) or not isinstance(source_systems,dict) or set(coverage) != set(source_systems):
            _fail("SOURCE_MAPPING_INVALID", "source_systems", "Logical source coverage and system bindings must match")
        for name,system in source_systems.items():
            _text(name,"source_systems"); _text(system,"source_systems."+name)
            if system.startswith("CHAIN_"):
                _fail("SOURCE_MAPPING_INVALID", "source_systems", "External sources cannot claim an internal namespace")
            validate_scope(coverage[name],allowed={n for n,s in definitions.items() if s["source"] == "external"})
        mappings = {} if status_mappings is None else status_mappings
        if not isinstance(mappings,dict) or set(mappings)-set(coverage):
            _fail("SOURCE_MAPPING_INVALID", "status_mappings", "Unknown normalization source")
        for name,mapping in mappings.items():
            source_validity("UNKNOWN",{"system":source_systems[name],"status_mapping":mapping})
        limits = {"timeout_s":120,"maximum_attempts":2} if resolution_limits is None else resolution_limits
        _closed(limits,{"timeout_s","maximum_attempts"},"resolution_limits")
        if (type(limits["timeout_s"]) not in (int,float) or limits["timeout_s"] <= 0
                or type(limits["maximum_attempts"]) is not int or not 1 <= limits["maximum_attempts"] <= 10):
            _fail("INVALID_LIMITS","resolution_limits","Positive timeout and 1..10 attempts required")
        _text(engine_run_id,"engine_run_id"); _hash(run_pin,"run_pin")
        if timestamp(known_at) < timestamp(initial["arrival_time"]):
            _fail("INVALID_TIME","known_at","Creation precedes arrival")
        self.identity = {n:initial[n] for n in ("site_id","patient_id","encounter_id","episode_id")}
        self.engine_run_id,self.run_pin = engine_run_id,run_pin
        self._constructor = copy.deepcopy({"initial":initial,"definition":definition,
            "source_systems":source_systems,"engine_run_id":engine_run_id,"run_pin":run_pin,
            "known_at":known_at,"resolution_limits":limits,"status_mappings":mappings})
        self._definition,self._systems,self._coverage = copy.deepcopy(definition),copy.deepcopy(source_systems),copy.deepcopy(coverage)
        # This owned definition is immutable for the lifetime of this ledger.
        # Reconstructing from state invokes this constructor and recomputes the
        # pin; no cache is serialized or accepted from an external caller.
        self._definition_hash = canonical_hash(self._definition)
        self._types = {name:spec["type"] for name,spec in definitions.items()}
        self._mappings,self._limits = copy.deepcopy(mappings),copy.deepcopy(limits)
        self._clock=self._created_at=known_at
        self._seq=self._epoch=0
        self._journal=[]; self._changes=[]; self._events={}; self._claims={}; self._records={}
        self._waiting={}; self._source_history=[]; self._snapshots={}; self._derived={}; self._decisions={}; self._captures={}; self._observations={}
        self._task_results={}
        self._source_completions={}
        self._graph=ProvenanceIndex()
        ref={"system":"CHAIN_INITIAL","record_id":initial["episode_id"],"version":1}
        self._initial={n:{"value":copy.deepcopy(v),"validity":"KNOWN","source_status":"AVAILABLE",
            "source_refs":[{**ref,"field":n}],"source_time":initial["arrival_time"],"known_at":known_at}
            for n,v in initial.items()}
        for fact in self._initial.values(): self._graph.add(fact["source_refs"][0])
        self._facts=copy.deepcopy(self._initial)
        self._rebuild(cause="CREATION", at=known_at, count_change=False)

    @property
    def data_epoch(self): return self._epoch

    @property
    def reducer_seq(self): return self._seq

    @property
    def definition_hash(self): return self._definition_hash

    @property
    def processed_at(self): return self._clock

    @property
    def journal(self): return copy.deepcopy(self._journal)

    @property
    def source_history(self): return copy.deepcopy(self._source_history)

    @property
    def decisions(self): return copy.deepcopy(self._decisions)

    @property
    def captures(self): return copy.deepcopy(self._captures)

    @property
    def observations(self): return copy.deepcopy(self._observations)

    @property
    def task_results(self): return copy.deepcopy(self._task_results)

    @property
    def source_documents_history(self):
        """Exact admitted source text, never inserted into Agent fact scope."""
        result={}
        for entry in self._source_history:
            ref=entry["record"]["source_ref"]; key=_record_key(ref)
            completion=self._source_completions.get(key)
            if completion is not None:
                name="document:"+ref["record_id"]
                result[key]={"facts":{name:copy.deepcopy(completion["facts"][name])}
                             if name in completion["facts"] else {},"applied_at":entry["applied_at"]}
        return result

    @property
    def pending_changes(self):
        return [{"event_id":eid,"source":item["source_id"],"source_ref":copy.deepcopy(item["event"]["source_ref"]),
                 "fields":self._affected_fields(eid),"status":item["receipt"]["status"]}
                for eid,item in sorted(self._events.items()) if item["receipt"]["status"] in {"PENDING","FAILED"}]

    @property
    def dependency_pending_changes(self):
        """Potential changes to concrete current inputs, narrowed per field.

        Global observations still expire sharing on every accepted change.
        A different known record does not revoke another record's evidence;
        an as-yet unknown field remains conservatively affected by its source.
        """
        result=[]
        for item in self.pending_changes:
            ref=item["source_ref"];fields=[]
            for name in item["fields"]:
                fact=self._facts[name]
                versions=[r["version"] for r in fact["source_refs"]
                    if (r["system"],r["record_id"])==(ref["system"],ref["record_id"])]
                if (fact["validity"] not in {"KNOWN","ABSENT_CONFIRMED"} or
                        versions and ref["version"]>max(versions)):
                    fields.append(name)
            if fields:result.append({**item,"fields":fields})
        return result

    def changes_since(self, epoch):
        if type(epoch) is not int or not 0 <= epoch <= self._epoch:
            _fail("INVALID_COUNTER","data_epoch","Unknown data epoch")
        return copy.deepcopy([change for change in self._changes if change["data_epoch"] > epoch])

    def event_receipt(self, event_id):
        _text(event_id,"event_id")
        item=self._events.get(event_id)
        return copy.deepcopy(item["receipt"]) if item else None

    def _at(self, at):
        now=timestamp(at)
        if now < timestamp(self._clock):
            _fail("INVALID_TIME","at","Reducer time cannot move backwards")
        return now

    def _note(self, op, body, at):
        self._at(at); self._clock=at; self._seq+=1
        row={"reducer_seq":self._seq,"operation":op,"body":copy.deepcopy(body),"at":at,
             "previous_hash":self._journal[-1]["entry_hash"] if self._journal else None}
        row["entry_hash"]=canonical_hash(row)
        self._journal.append(row)

    def _changed(self, cause, kind):
        self._epoch+=1
        self._changes.append({"data_epoch":self._epoch,"event_id":cause,"cause":cause,
                              "kind":kind,"reducer_seq":self._seq})

    def _common(self, value):
        if value["identity"] != self.identity or value["engine_run_id"] != self.engine_run_id or value["run_pin"] != self.run_pin:
            _fail("IDENTITY_MISMATCH","identity","Record belongs to another Episode or pinned run")

    def _affected_fields(self, event_id):
        if event_id in self._waiting:
            record=self._waiting[event_id]
            return sorted(set(record["facts"]) | {r["field"] for r in (record["revision"] or {}).get("targets",[])})
        return sorted(self._coverage[self._events[event_id]["source_id"]])

    def pending_for(self, scope):
        scope=set(validate_scope(scope,allowed=self._types))
        return [item for item in self.pending_changes if scope & set(item["fields"])]

    def resolution_requests(self):
        return [{"event":copy.deepcopy(item["event"]),"source_id":item["source_id"],
                 "attempt_no":item["attempts"],"deadline":item["deadline"]}
                for _,item in sorted(self._events.items())
                if item["receipt"]["status"] == "PENDING" and item["receipt"]["code"] == "RESOLUTION_PENDING"]

    @property
    def next_deadline(self):
        """Next pending resolution expiry; runners supply the clock and wait."""
        values = [item["deadline"] for item in self._events.values()
                  if item["receipt"]["status"] == "PENDING"]
        return min(values, key=timestamp) if values else None

    def receive(self, event, *, source_id, at):
        _text(source_id,"source_id")
        check_json(event)
        try: validate_event(event)
        except (ValueError,TypeError,KeyError): _fail("EVENT_CONTRACT_INVALID","event","Invalid source Event contract")
        if {n:event[n] for n in self.identity} != self.identity:
            _fail("IDENTITY_MISMATCH","event","Source Event belongs to another Episode")
        if source_id not in self._systems or event["source_ref"]["system"] != self._systems[source_id]:
            _fail("SOURCE_MAPPING_INVALID","event.source_ref","Source differs from its approved system")
        payload=event["payload"]
        _closed(payload,{"data_ref","content_hash"},"event.payload")
        _hash(payload["content_hash"],"event.payload.content_hash")
        if payload["data_ref"] != event["source_ref"]:
            _fail("SOURCE_REFERENCE_MISMATCH","event.payload","Event must reference the exact published source version")
        if timestamp(event["source_event_time"]) > timestamp(event["published_at"]) or timestamp(event["received_at"]) > timestamp(at):
            _fail("INVALID_TIME","event","Source publication/receipt/application order is invalid")
        eid=event["event_id"]; fingerprint=canonical_hash(event)
        if eid in self._events:
            if self._events[eid]["event_hash"] != fingerprint or self._events[eid]["source_id"] != source_id:
                _fail("EVENT_ID_CONFLICT","event.event_id","Existing Event ID has different content")
            return self.event_receipt(eid)
        key=_record_key(event["source_ref"])
        if key in self._claims and self._claims[key] != payload["content_hash"]:
            _fail("SOURCE_VERSION_CONFLICT","event.source_ref","Exact source version has different content")
        if key in self._records:
            prior=self._records[key]["record"]
            if any(prior[k] != event[e] for k,e in (("source_time","source_event_time"),("published_at","published_at"),("event_type","event_type"))):
                _fail("SOURCE_VERSION_CONFLICT","event","Publication metadata differs from the known source")
        self._note("receive",{"event":event,"source_id":source_id},at)
        duplicate=key in self._records
        receipt={"event_id":eid,"status":"DUPLICATE" if duplicate else "PENDING",
                 "source_id":source_id,
                 "code":"SOURCE_ALREADY_APPLIED" if duplicate else "RESOLUTION_PENDING",
                 "source_ref":copy.deepcopy(event["source_ref"]),"content_hash":payload["content_hash"],
                 "accepted_reducer_seq":self._seq,"data_epoch":self._epoch}
        if duplicate: receipt["applied_at"]=self._records[key]["applied_at"]
        self._claims[key]=payload["content_hash"]
        self._events[eid]={"event":copy.deepcopy(event),"event_hash":fingerprint,"source_id":source_id,
            "receipt":receipt,"attempts":1,"deadline":(timestamp(at)+timedelta(seconds=self._limits["timeout_s"])).isoformat()}
        return self.event_receipt(eid)

    def resolve(self, completion, *, at):
        """Worker-side pure normalization bridge; exact-source I/O stays outside."""
        eid=completion.get("event_id") if isinstance(completion,dict) else None
        _text(eid,"completion.event_id")
        if eid not in self._events: _fail("EVENT_NOT_RECEIVED","completion.event_id","Receive the Event before its resolution")
        item=self._events[eid]
        sid=item["source_id"]
        source_definition={"system":self._systems[sid]}
        if sid in self._mappings: source_definition["status_mapping"]=copy.deepcopy(self._mappings[sid])
        facts=normalize_completion(completion,fact_types=self._types,source_id=sid,
                                   source_definition=source_definition,applied_at=at)
        if set(facts)-set(self._coverage[sid]):
            _fail("SOURCE_SCOPE_DENIED","completion.facts","Source facts exceed approved coverage")
        event=item["event"]
        pairs={"source_ref":"source_ref","source_time":"source_event_time","published_at":"published_at",
               "received_at":"received_at","event_type":"event_type","sequence_no":"sequence_no"}
        if ({n:completion[n] for n in self.identity} != self.identity
                or any(completion[c] != event[e] for c,e in pairs.items())
                or completion["content_hash"] != event["payload"]["content_hash"]):
            _fail("RESOLUTION_EVENT_MISMATCH","completion","Resolution differs from its exact Event")
        record={"contract_schema":"chain-fact-resolved/v1","identity":copy.deepcopy(self.identity),
            "engine_run_id":self.engine_run_id,"run_pin":self.run_pin,"source_id":sid,"event_id":eid,
            **{k:copy.deepcopy(completion[k]) for k in ("source_ref","source_time","published_at","received_at","event_type","content_hash")},
            "facts":facts,"dependencies":{name:canonical_source_refs([copy.deepcopy(ref) for ref in fact.get("dependencies",[])
                if ref != {**completion["source_ref"],"field":name}]) for name,fact in completion["facts"].items() if name in facts},
            "revision":copy.deepcopy(completion.get("revision"))}
        if record["revision"]:
            record["revision"]["targets"]=canonical_source_refs(record["revision"]["targets"])
        record["resolution_id"]=canonical_hash(record)
        self.record_source_completion(completion,at=at,validate_only=True)
        receipt=self.apply_resolution(record,at=at)
        if receipt["status"] in {"APPLIED","PENDING","DUPLICATE"}:
            self.record_source_completion(completion,at=at)
        return receipt

    def record_source_completion(self, completion, *, at, validate_only=False):
        """Keep validated original completion separately for exact-version text.

        This is metadata retention, not another source Fact or sharing epoch.
        Unadmitted records are invisible to source_documents_history.
        """
        eid=completion.get("event_id") if isinstance(completion,dict) else None
        if eid not in self._events: _fail("EVENT_NOT_RECEIVED","completion","Unknown source Event")
        item=self._events[eid]; event=item["event"]; sid=item["source_id"]
        normalize_completion(completion,fact_types=self._types,source_id=sid,
            source_definition={"system":self._systems[sid],"status_mapping":self._mappings.get(sid,{})},applied_at=at)
        if ({n:completion[n] for n in self.identity} != self.identity or completion["source_ref"] != event["source_ref"]
                or completion["content_hash"] != event["payload"]["content_hash"]):
            _fail("RESOLUTION_EVENT_MISMATCH","source_completion","Source completion differs from its Event")
        document_name="document:"+completion["source_ref"]["record_id"]
        document=completion["facts"].get(document_name)
        if document is not None and document["source_ref"]!={**completion["source_ref"],"field":document_name}:
            _fail("DOCUMENT_SOURCE_MISMATCH","source_completion","Source text belongs to another exact record/version")
        key=_record_key(completion["source_ref"])
        prior=self._source_completions.get(key)
        if prior is not None:
            def semantic(value):
                return {"source_ref":value["source_ref"],"content_hash":value["content_hash"],
                        "facts":{name:{k:canonical_source_refs(v) if k=="dependencies" else v
                            for k,v in fact.items() if k!="known_at"} for name,fact in value["facts"].items()}}
            if semantic(prior) != semantic(completion): _fail("SOURCE_VERSION_CONFLICT","source_completion","Exact source version content differs")
            return
        if validate_only:return
        self._note("source_completion",completion,at)
        self._source_completions[key]=copy.deepcopy(completion)

    def _validate_resolution(self, record, at):
        _closed(record,{"contract_schema","identity","engine_run_id","run_pin","source_id","event_id","source_ref",
                       "source_time","published_at","received_at","event_type","content_hash","facts","dependencies","revision","resolution_id"},"resolution")
        if record["contract_schema"] != "chain-fact-resolved/v1": _fail("CONTRACT_SCHEMA_INVALID","resolution","Unsupported resolution version")
        _text(record["event_id"],"resolution.event_id"); _text(record["source_id"],"resolution.source_id")
        self._common(record)
        if record["resolution_id"] != canonical_hash({k:v for k,v in record.items() if k != "resolution_id"}):
            _fail("CONTRACT_HASH_INVALID","resolution.resolution_id","Resolution content hash differs")
        eid=record["event_id"]
        if eid not in self._events: _fail("EVENT_NOT_RECEIVED","resolution.event_id","Resolution Event is not received")
        item=self._events[eid]; event=item["event"]
        if record["source_id"] != item["source_id"] or any(record[c] != event[e] for c,e in
                (("source_ref","source_ref"),("source_time","source_event_time"),("published_at","published_at"),("received_at","received_at"),("event_type","event_type"))):
            _fail("RESOLUTION_EVENT_MISMATCH","resolution","Resolution publication metadata differs")
        if record["content_hash"] != event["payload"]["content_hash"]:
            _fail("RESOLUTION_EVENT_MISMATCH","resolution.content_hash","Resolution source hash differs")
        source_ref(record["source_ref"])
        if not isinstance(record["facts"],dict) or set(record["facts"])-set(self._coverage[item["source_id"]]):
            _fail("SOURCE_SCOPE_DENIED","resolution.facts","Resolution exceeds source scope")
        if not isinstance(record["dependencies"],dict) or set(record["dependencies"]) != set(record["facts"]):
            _fail("DEPENDENCY_CONTRACT_INVALID","resolution.dependencies","Every normalized field needs a dependency list")
        probe={"contract_schema":"chain-fact-snapshot/v1","identity":copy.deepcopy(self.identity),
               "engine_run_id":self.engine_run_id,"run_pin":self.run_pin,"known_at":at,"facts":record["facts"]}
        probe["snapshot_id"]=canonical_hash(probe); validate_snapshot(probe)
        for name,fact in record["facts"].items():
            own={**record["source_ref"],"field":name}
            dependencies=canonical_source_refs(record["dependencies"][name])
            if record["dependencies"][name] != dependencies:
                _fail("DEPENDENCY_CONTRACT_INVALID","resolution.dependencies","Concrete dependencies must be in canonical order")
            expected=sorted([own,*dependencies],key=_ref_key)
            if fact["source_refs"] != expected or timestamp(fact["source_time"]) > timestamp(record["source_time"]):
                _fail("PROVENANCE_MISMATCH","resolution.facts."+name,"Source provenance differs from normalized dependencies")
            if fact["validity"] == "KNOWN" and not _type_matches(fact["value"],self._types[name]):
                _fail("FACT_TYPE_MISMATCH","resolution.facts."+name,"Normalized value differs from its declared type")
            source_definition={"system":self._systems[item["source_id"]]}
            if item["source_id"] in self._mappings:
                source_definition["status_mapping"]=self._mappings[item["source_id"]]
            if fact["validity"] != source_validity(fact["source_status"],source_definition):
                _fail("SOURCE_STATUS_MISMATCH","resolution.facts."+name,"Validity differs from approved source normalization")
        try:
            validate_revision({"source_ref":record["source_ref"],"event_type":record["event_type"],
                               **({"revision":record["revision"]} if record["revision"] is not None else {})})
        except (ValueError,TypeError,KeyError): _fail("REVISION_CONTRACT_INVALID","resolution.revision","Invalid explicit revision targets")
        for target in (record["revision"] or {}).get("targets",[]):
            replacement=record["facts"].get(target["field"])
            expected={"ERROR":"ERROR","RETRACTION":"RETRACTED","CONFLICT":"CONFLICT"}.get(record["revision"]["kind"])
            if replacement is None or (expected and (replacement["validity"] != expected or replacement["value"] is not None)):
                _fail("REVISION_CONTRACT_INVALID","resolution.revision","Revised fields need a replacement or explicit nonusable marker")

    @staticmethod
    def _source_fingerprint(record):
        return canonical_hash({k:v for k,v in record.items() if k not in
            {"event_id","received_at","resolution_id","identity","engine_run_id","run_pin","facts"}}
            | {"facts":{n:_without_time(f) for n,f in record["facts"].items()}})

    def apply_resolution(self, record, *, at):
        """Apply a compact validated resolution, also the replay input format."""
        self._validate_resolution(record,at)
        eid=record["event_id"]; item=self._events[eid]
        fingerprint=self._source_fingerprint(record)
        if "resolution_fingerprint" in item:
            if item["resolution_fingerprint"] != fingerprint:
                _fail("RESOLUTION_ID_CONFLICT","resolution","Same Event has a different normalized resolution")
            return self.event_receipt(eid)
        key=_record_key(record["source_ref"])
        if key in self._records and self._source_fingerprint(self._records[key]["record"]) != fingerprint:
            _fail("SOURCE_VERSION_CONFLICT","resolution","Same source version has a different normalized body")
        self._at(at)
        # Validate all immediately knowable revision/graph constraints before
        # accepting this resolution. Missing inputs are explicit pending data.
        self._prepare(record)
        self._note("resolution",record,at)
        item["resolution_fingerprint"]=fingerprint
        if key in self._records:
            item["receipt"].update(status="DUPLICATE",code="SOURCE_ALREADY_APPLIED")
            return self.event_receipt(eid)
        self._waiting[eid]=copy.deepcopy(record)
        self._drain(at)
        return self.event_receipt(eid)

    def _prepare(self, record):
        revision=record["revision"]
        targets=(revision or {}).get("targets",[])
        missing=[ref for ref in targets if not self._graph.contains(ref)]
        if missing: return None,"REVISION_TARGET_PENDING"
        if revision:
            prior=[entry["record"] for key,entry in self._records.items()
                   if key[:2] == _record_key(record["source_ref"])[:2] and key[2] < record["source_ref"]["version"]]
            if prior:
                baseline=max(prior,key=lambda value:value["source_ref"]["version"])
                targeted={ref["field"] for ref in targets}
                for name,old in baseline["facts"].items():
                    if name in targeted or name not in record["facts"]: continue
                    def clinical_content(f):
                        return {k:v for k,v in f.items() if k not in {"known_at","source_refs"}}
                    if clinical_content(old) != clinical_content(record["facts"][name]) or baseline["dependencies"][name] != record["dependencies"][name]:
                        _fail("UNTARGETED_REVISION_CHANGE","resolution.facts."+name,"Revision changed a field outside its explicit targets")
        graph=ProvenanceIndex.from_state(self._graph.export_state())
        pending={name:{**record["source_ref"],"field":name} for name in record["facts"]}
        known_new={_ref_key(ref):name for name,ref in pending.items()}
        for dependencies in record["dependencies"].values():
            if any(not graph.contains(ref) and _ref_key(ref) not in known_new for ref in dependencies):
                return None,"DEPENDENCY_PENDING"
        while pending:
            ready=[name for name in sorted(pending) if all(graph.contains(ref) for ref in record["dependencies"][name])]
            if not ready: _fail("DEPENDENCY_CYCLE","resolution.dependencies","Cyclic field dependencies")
            for name in ready:
                graph.add(pending.pop(name),record["dependencies"][name],
                          rule={"rule_id":"source-normalization:"+record["source_id"],"version":"chain-fact-execution/v1"})
        return graph,None

    def _drain(self, at):
        # Every successful iteration removes at least one waiting resolution.
        # Waiting never blocks receipt or resolution from an unrelated source.
        while True:
            progress=False
            for eid in sorted(list(self._waiting),key=lambda e:(_record_key(self._waiting[e]["source_ref"]),e)):
                record=self._waiting[eid]; item=self._events[eid]
                try: graph,reason=self._prepare(record)
                except ConfigError as error:
                    item["receipt"].update(status="REJECTED",code=error.code)
                    del self._waiting[eid]; progress=True; continue
                if reason:
                    if item["receipt"]["status"] != "FAILED":
                        item["receipt"].update(status="PENDING",code=reason)
                    continue
                self._graph=graph
                if record["revision"]:
                    self._graph.invalidate(record["revision"]["targets"],reason=record["revision"]["reason"],reducer_seq=self._seq)
                applied=copy.deepcopy(record)
                for fact in applied["facts"].values(): fact["known_at"]=at
                entry={"record":copy.deepcopy(record),"applied_facts":applied["facts"],
                       "applied_at":at,"applied_reducer_seq":self._seq}
                self._records[_record_key(record["source_ref"])]=entry
                self._source_history.append(copy.deepcopy(entry))
                epoch_before=self._epoch
                self._rebuild(cause=eid,at=at)
                if self._epoch == epoch_before:
                    # A newly verified historical source version is new data
                    # knowledge for U1 even when the current scoped input stays
                    # identical. It must not re-enable an older shared check.
                    self._changed(eid,"SOURCE_VERSION_DISCOVERED")
                item["receipt"].update(status="APPLIED",code="SOURCE_APPLIED",applied_at=at,
                    accepted_reducer_seq=self._seq,data_epoch=self._epoch)
                del self._waiting[eid]; progress=True
            if not progress: break

    def _rebuild(self, *, cause, at, count_change=True):
        latest={}
        for key,entry in sorted(self._records.items()):
            for name,fact in entry["applied_facts"].items():
                item_key=(*key[:2],name)
                if item_key not in latest or latest[item_key][0] < key[2]:
                    latest[item_key]=(key[2],copy.deepcopy(fact))
        candidates={}
        for name,fact in self._initial.items(): candidates.setdefault(name,[]).append(copy.deepcopy(fact))
        for candidate_key,(_,fact) in latest.items():
            name=candidate_key[-1]
            fact["source_refs"]=self._graph.closure(fact["source_refs"])
            if self._graph.is_invalid(fact["source_refs"]):
                sequences=[event["reducer_seq"] for ref in fact["source_refs"]
                           for event in self._graph.node(ref)["invalidations"]]
                invalid_at=max((self._journal[seq-1]["at"] for seq in sequences),key=timestamp)
                fact.update(value=None,validity="INVALIDATED",known_at=max((fact["known_at"],invalid_at),key=timestamp))
            candidates.setdefault(name,[]).append(fact)
        rebuilt={}
        for name in sorted(self._types):
            items=candidates.get(name,[])
            usable=[item for item in items if item["validity"] in {"KNOWN","ABSENT_CONFIRMED"}]
            active=usable or items
            if not active:
                rebuilt[name]={"value":None,"validity":"UNKNOWN","source_status":"NOT_RECEIVED","source_refs":[],
                               "source_time":self._created_at,"known_at":self._created_at}
                continue
            active=sorted(active,key=lambda item:canonical_hash(item))
            signatures={canonical_hash({k:v for k,v in item.items() if k in {"value","validity","unit","confirmation_status"}}) for item in active}
            current=copy.deepcopy(active[0])
            if len(signatures)>1:
                current={"value":None,"validity":"CONFLICT","source_status":"CONFLICT"}
            elif len({item["source_status"] for item in active})>1:
                current["source_status"]="MULTIPLE_SOURCES"
            refs={_ref_key(ref):copy.deepcopy(ref) for item in active for ref in item["source_refs"]}
            current.update(source_refs=[refs[key] for key in sorted(refs)],
                           source_time=max((item["source_time"] for item in active),key=timestamp),
                           known_at=max((item["known_at"] for item in active),key=timestamp))
            rebuilt[name]=current
        changed=rebuilt != self._facts
        self._facts=rebuilt
        if changed and count_change: self._changed(cause,"SOURCE_DATA")

    def snapshot(self, scope=None):
        names=sorted(self._types) if scope is None else validate_scope(scope,allowed=self._types)
        facts={name:copy.deepcopy(self._facts[name]) for name in names}
        known=max((fact["known_at"] for fact in facts.values()),key=timestamp) if facts else self._created_at
        result={"contract_schema":"chain-fact-snapshot/v1","identity":copy.deepcopy(self.identity),
                "engine_run_id":self.engine_run_id,"run_pin":self.run_pin,"known_at":known,"facts":facts}
        result["snapshot_id"]=canonical_hash(result)
        validate_snapshot(result)
        return result

    def freeze_snapshot(self, scope=None):
        result=self.snapshot(scope)
        if result["snapshot_id"] not in self._snapshots:
            self._note("freeze_snapshot",{"scope":sorted(result["facts"])},self._clock)
            self._snapshots[result["snapshot_id"]]=copy.deepcopy(result)
        return result

    def freeze_evidence_snapshot(self, scope, *, at, unconfirmed_fields=()):
        """Freeze an uncertainty view without replacing the admitted raw facts."""
        self._at(at)
        names=validate_scope(scope,allowed=self._types)
        if not isinstance(unconfirmed_fields,(list,tuple)) or set(unconfirmed_fields)-set(names):
            _fail("CONTRACT_SCOPE_INVALID","unconfirmed_fields","Unconfirmed fields must belong to the evidence scope")
        fields=sorted(set(unconfirmed_fields))
        if not fields: return self.freeze_snapshot(names)
        result=self.snapshot(names)
        for name in fields:
            fact=result["facts"][name]
            if fact["validity"] in {"KNOWN","ABSENT_CONFIRMED"}: fact.update(value=None,validity="PENDING")
            known=[fact["known_at"],*[self._events[p["event_id"]]["event"]["received_at"]
                    for p in self.pending_changes if name in p["fields"]]]
            fact["known_at"]=max(known,key=timestamp)
        result["known_at"]=max((f["known_at"] for f in result["facts"].values()),key=timestamp)
        result.pop("snapshot_id"); result["snapshot_id"]=canonical_hash(result); validate_snapshot(result)
        if result["snapshot_id"] not in self._snapshots:
            self._note("evidence_snapshot",{"scope":names,"unconfirmed_fields":fields},at)
            self._snapshots[result["snapshot_id"]]=copy.deepcopy(result)
        return result

    def frozen_snapshot(self, snapshot_id):
        _hash(snapshot_id,"snapshot_id")
        if snapshot_id not in self._snapshots: _fail("SNAPSHOT_NOT_FROZEN","snapshot_id","Snapshot is not in this Episode history")
        return copy.deepcopy(self._snapshots[snapshot_id])

    def input_is_current(self, snapshot_id, *, pending_changes=None):
        prior=self.frozen_snapshot(snapshot_id)
        pending=self.pending_changes if pending_changes is None else pending_changes
        return not any(set(item["fields"])&set(prior["facts"]) for item in pending) and prior["facts"] == self.snapshot(list(prior["facts"]))["facts"]

    def fail_resolution(self, event_id, *, code, at):
        _text(event_id,"event_id")
        _text(code,"code")
        if event_id not in self._events: _fail("EVENT_NOT_RECEIVED","event_id","Unknown Event")
        item=self._events[event_id]
        if item["receipt"]["status"] in {"APPLIED","DUPLICATE","REJECTED"}: return self.event_receipt(event_id)
        self._note("fail_resolution",{"event_id":event_id,"code":code},at)
        item["receipt"].update(status="FAILED",code=code)
        return self.event_receipt(event_id)

    def retry_resolution(self, event_id, *, at):
        _text(event_id,"event_id")
        if event_id not in self._events: _fail("EVENT_NOT_RECEIVED","event_id","Unknown Event")
        item=self._events[event_id]; now=self._at(at)
        if (item["receipt"]["status"] not in {"FAILED","PENDING"} or event_id in self._waiting
                or item["attempts"] >= self._limits["maximum_attempts"] or now >= timestamp(item["deadline"])):
            return None
        self._note("retry_resolution",{"event_id":event_id},at)
        item["attempts"]+=1; item["receipt"].update(status="PENDING",code="RESOLUTION_PENDING")
        return next(request for request in self.resolution_requests() if request["event"]["event_id"] == event_id)

    def expire_resolutions(self, *, at):
        now=self._at(at)
        expired=[eid for eid,item in sorted(self._events.items()) if item["receipt"]["status"] == "PENDING" and now >= timestamp(item["deadline"])]
        if expired:
            self._note("expire_resolutions",{},at)
            for eid in expired:
                self._events[eid]["receipt"].update(status="FAILED",code="RESOLUTION_DEADLINE_EXCEEDED")
        return [self.event_receipt(eid) for eid in expired]

    def record_observation(self, record, *, at):
        """Sequence query/progress metadata without changing clinical data.

        A serialized runner can call this before accepting a query result and
        pass the returned reducer sequence to the observation owner. Keep the
        result in its own owner; this history stores only its immutable hash.
        """
        _closed(record,{"observation_id","request_id","kind","content_hash"},"observation")
        _text(record["observation_id"],"observation_id"); _text(record["request_id"],"request_id")
        _hash(record["content_hash"],"content_hash")
        if record["kind"] not in ("PREFLIGHT_RESULT","RECONCILE_RESULT","QUERY_PROGRESS"):
            _fail("CONTRACT_SCHEMA_INVALID","observation.kind","Unsupported observation metadata kind")
        prior=self._observations.get(record["observation_id"])
        if prior:
            if canonical_hash(prior["record"]) != canonical_hash(record):
                _fail("OBSERVATION_ID_CONFLICT","observation_id","Observation ID has different content")
            return copy.deepcopy(prior)
        self._note("observation",record,at)
        item={"record":copy.deepcopy(record),"accepted_reducer_seq":self._seq,"applied_at":at}
        self._observations[record["observation_id"]]=item
        return copy.deepcopy(item)

    def record_task_result(self, record, *, at):
        """Journal a validated Agent completion commitment, not a source fact.

        The task owner retains the full immutable request/result and performs
        acceptance. A new judgment invalidates preflight sharing even when its
        raw source Snapshot did not change. Query/cache metadata do not.
        """
        _closed(record,{"execution_id","task_id","request_hash","result_hash","input_snapshot_id"},"task_result")
        for key in ("execution_id","task_id"): _text(record[key],"task_result."+key)
        for key in ("request_hash","result_hash","input_snapshot_id"): _hash(record[key],"task_result."+key)
        self.frozen_snapshot(record["input_snapshot_id"])
        prior=self._task_results.get(record["execution_id"])
        if prior:
            if prior["record"] != record: _fail("RESULT_ID_CONFLICT","execution_id","Agent completion commitment differs")
            return copy.deepcopy(prior)
        self._note("task_result",record,at)
        item={"record":copy.deepcopy(record),"accepted_reducer_seq":self._seq,"applied_at":at}
        self._task_results[record["execution_id"]]=item
        self._changed(record["execution_id"],"AGENT_RESULT")
        return copy.deepcopy(item)

    def record_derivation(self, record, *, at):
        """Store a precomputed result and provenance; never evaluate its rule."""
        _closed(record,{"derived_id","name","value","value_type","rule_id","rule_version","snapshot_id","dependencies"},"derivation")
        record=copy.deepcopy(record)
        record["dependencies"]=canonical_source_refs(record["dependencies"])
        for name in ("derived_id","name","rule_id","rule_version"): _text(record[name],"derivation."+name)
        if not isinstance(record["value_type"],str) or record["value_type"] not in _TYPES or not _type_matches(record["value"],record["value_type"]):
            _fail("FACT_TYPE_MISMATCH","derivation.value","Derived value has the wrong declared type")
        prior=self._derived.get(record["derived_id"])
        if prior:
            if canonical_hash(prior["input"]) != canonical_hash(record): _fail("DERIVATION_ID_CONFLICT","derived_id","Derived ID has different content")
            return self.derived_record(record["derived_id"])
        snapshot=self.frozen_snapshot(record["snapshot_id"])
        deps=canonical_source_refs(record["dependencies"])
        if not deps: _fail("DEPENDENCY_CONTRACT_INVALID","dependencies","Derived facts require concrete inputs")
        roots=self._graph.closure(deps)
        snapshot_refs={_ref_key(ref) for fact in snapshot["facts"].values() for ref in fact["source_refs"]}
        leaves=[ref for ref in roots if not self._graph.node(ref)["dependencies"]]
        if any(_ref_key(ref) not in snapshot_refs for ref in leaves):
            _fail("DEPENDENCY_SCOPE_MISMATCH","dependencies","Derived inputs are outside the frozen Snapshot")
        ref={"system":"CHAIN_DERIVED","record_id":record["derived_id"],"version":1,"field":record["name"]}
        graph=ProvenanceIndex.from_state(self._graph.export_state())
        graph.add(ref,deps,rule={"rule_id":record["rule_id"],"version":record["rule_version"]})
        self._note("derivation",record,at); self._graph=graph
        self._derived[record["derived_id"]]={"input":copy.deepcopy(record),"source_ref":ref,
            "known_at":at,"accepted_reducer_seq":self._seq}
        self._changed(record["derived_id"],"DERIVED_DATA")
        return self.derived_record(record["derived_id"])

    def derived_record(self, derived_id):
        _text(derived_id,"derived_id")
        if derived_id not in self._derived: _fail("DERIVATION_NOT_FOUND","derived_id","Unknown derived record")
        item=copy.deepcopy(self._derived[derived_id]); invalid=self._graph.is_invalid([item["source_ref"]])
        item["validity"]="INVALIDATED" if invalid else "KNOWN"
        item["value"]=None if invalid else copy.deepcopy(item["input"]["value"])
        item["dependencies"]=self._graph.closure(item["input"]["dependencies"])
        item["invalidations"]=self._graph.node(item["source_ref"])["invalidations"]
        item["input_current"]=self.input_is_current(item["input"]["snapshot_id"])
        item["usage"]="CURRENT" if not invalid and item["input_current"] else "REFERENCE_OLD_INPUT"
        return item

    def capture_first(self, name, *, task_definition_id, execution_id, result_field, value, input_snapshot_id,
                      result_status, at, dependency_scoped=False):
        """Internal bookkeeping for an upstream validated Agent result only.

        The complete Request/Result/Safety acceptance path is phase 5. This API
        also checks the data guard; a caller's SUCCESS alone cannot revive old
        or still-unresolved input. Captures remain historical after correction.
        """
        _text(name,"capture_first.name"); _text(task_definition_id,"task_definition_id")
        declaration=self._definition.get("milestones",{}).get(name,{})
        source=declaration.get("capture_first")
        if (not source or source.get("namespace") != "results" or source.get("name") != task_definition_id
                or source.get("path") != [result_field]):
            _fail("CAPTURE_CONTRACT_MISMATCH","capture_first","Capture does not match its declared result field")
        _text(execution_id,"execution_id")
        if name in self._captures: return copy.deepcopy(self._captures[name])
        if type(dependency_scoped) is not bool:_fail("CONTRACT_SCHEMA_INVALID","dependency_scoped","Expected a boolean scope mode")
        pending=self.dependency_pending_changes if dependency_scoped else None
        if result_status != "SUCCESS" or not self.input_is_current(input_snapshot_id,pending_changes=pending): return None
        if not _type_matches(value,declaration["type"]): _fail("FACT_TYPE_MISMATCH","capture_first.value","Captured value differs from its declaration")
        snapshot=self.frozen_snapshot(input_snapshot_id)
        body={"name":name,"task_definition_id":task_definition_id,"execution_id":execution_id,"result_field":result_field,"value":copy.deepcopy(value),
              "input_snapshot_id":input_snapshot_id,"result_status":result_status}
        if dependency_scoped:body["dependency_scoped"]=True
        self._note("capture_first",body,at)
        capture={"definition_id":name,"task_definition_id":task_definition_id,"execution_id":execution_id,"input_snapshot_id":input_snapshot_id,
            "value":copy.deepcopy(value),"rule_version":self._definition["version"],"historical":True,
            "source_refs":canonical_source_refs(list({_ref_key(ref):ref for fact in snapshot["facts"].values() for ref in fact["source_refs"]}.values())),
            "accepted_reducer_seq":self._seq,"known_at":at}
        capture["capture_id"]=canonical_hash(capture); self._captures[name]=capture
        self._changed(capture["capture_id"],"HISTORICAL_CAPTURE")
        return copy.deepcopy(capture)

    def record_decision(self, record, *, at):
        """Store an accepted decision record supplied by the trusted HITL layer.

        This is not an API for accepting a screen response. Opening questions,
        authorization, current evidence revisions and acceptance are phase 6.
        Original records are never overwritten or revoked by source correction.
        """
        _closed(record,{"contract_schema","identity","engine_run_id","run_pin","decision_id","decision_definition_id",
            "request_id","decision","actor","evidence_snapshot_id","evidence_id","evidence_revision","policy_rule","recorded_at"},"accepted_decision")
        if record["contract_schema"] != "chain-fact-accepted-decision/v1": _fail("CONTRACT_SCHEMA_INVALID","accepted_decision","Expected an internal accepted-decision record")
        self._common(record)
        for name in ("decision_id","decision_definition_id","request_id","decision"): _text(record[name],name)
        _closed(record["actor"],{"role","staff_id"},"actor")
        for value in record["actor"].values(): _text(value,"actor")
        _closed(record["policy_rule"],{"rule_id","version"},"policy_rule")
        for value in record["policy_rule"].values(): _text(value,"policy_rule")
        _hash(record["evidence_id"],"evidence_id")
        if type(record["evidence_revision"]) is not int or record["evidence_revision"] < 1:
            _fail("CONTRACT_SCHEMA_INVALID","evidence_revision","Expected a positive evidence revision")
        snapshot=self.frozen_snapshot(record["evidence_snapshot_id"])
        if not timestamp(snapshot["known_at"]) <= timestamp(record["recorded_at"]) <= timestamp(at):
            _fail("INVALID_TIME","recorded_at","Decision time is outside its evidence/application interval")
        previous=self._decisions.get(record["decision_id"])
        if previous:
            if previous["record"] != record: _fail("DECISION_ID_CONFLICT","decision_id","Decision ID has different content")
            return copy.deepcopy(previous)
        self._note("decision",record,at)
        item={"record":copy.deepcopy(record),"accepted_reducer_seq":self._seq,"applied_at":at}
        self._decisions[record["decision_id"]]=item
        self._changed(record["decision_id"],"DECISION_DATA")
        return copy.deepcopy(item)

    def export_state(self):
        state={"ledger_schema":"chain-fact-ledger/v1","constructor":copy.deepcopy(self._constructor),"journal":self.journal}
        state["state_hash"]=canonical_hash(state)
        return state

    @classmethod
    def from_state(cls, state):
        try:
            return cls._restore_state(state)
        except ConfigError:
            raise
        except (KeyError,TypeError,AttributeError,ValueError):
            _fail("STATE_INTEGRITY_MISMATCH","ledger_state","Malformed ledger state or replay operation")

    @classmethod
    def _restore_state(cls, state):
        _closed(state,{"ledger_schema","constructor","journal","state_hash"},"ledger_state")
        if state["ledger_schema"] != "chain-fact-ledger/v1" or state["state_hash"] != canonical_hash({k:v for k,v in state.items() if k != "state_hash"}):
            _fail("STATE_INTEGRITY_MISMATCH","ledger_state","Ledger schema or content hash differs")
        _closed(state["constructor"],{"initial","definition","source_systems","engine_run_id","run_pin",
            "known_at","resolution_limits","status_mappings"},"ledger_state.constructor")
        if not isinstance(state["journal"],list): _fail("STATE_INTEGRITY_MISMATCH","journal","Expected a journal list")
        args=copy.deepcopy(state["constructor"]); initial=args.pop("initial")
        ledger=cls(initial,**args)
        for row in state["journal"]:
            _closed(row,{"reducer_seq","operation","body","at","previous_hash","entry_hash"},"journal")
            if row["entry_hash"] != canonical_hash({k:v for k,v in row.items() if k != "entry_hash"}):
                _fail("STATE_INTEGRITY_MISMATCH","journal","Journal entry hash differs")
            body=copy.deepcopy(row["body"]); at=row["at"]; op=row["operation"]
            if op == "receive": ledger.receive(body["event"],source_id=body["source_id"],at=at)
            elif op == "resolution": ledger.apply_resolution(body,at=at)
            elif op == "freeze_snapshot": ledger.freeze_snapshot(body["scope"])
            elif op == "fail_resolution": ledger.fail_resolution(body["event_id"],code=body["code"],at=at)
            elif op == "retry_resolution": ledger.retry_resolution(body["event_id"],at=at)
            elif op == "expire_resolutions": ledger.expire_resolutions(at=at)
            elif op == "derivation": ledger.record_derivation(body,at=at)
            elif op == "capture_first": ledger.capture_first(body.pop("name"),at=at,**body)
            elif op == "decision": ledger.record_decision(body,at=at)
            elif op == "observation": ledger.record_observation(body,at=at)
            elif op == "task_result": ledger.record_task_result(body,at=at)
            elif op == "source_completion": ledger.record_source_completion(body,at=at)
            elif op == "evidence_snapshot": ledger.freeze_evidence_snapshot(body["scope"],unconfirmed_fields=body["unconfirmed_fields"],at=at)
            else: _fail("STATE_INTEGRITY_MISMATCH","journal.operation","Unknown journal operation")
            if not ledger._journal or ledger._journal[-1] != row:
                _fail("STATE_INTEGRITY_MISMATCH","journal","Journal replay sequence or previous hash differs")
        return ledger
