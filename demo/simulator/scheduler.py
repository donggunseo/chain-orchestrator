"""Causal anchors + offsets. A deadline never fabricates a missing prerequisite."""
import asyncio
import copy
from datetime import timedelta
from pathlib import Path
import re
from datetime import date

from demo.io import read_yaml as _read_yaml
from chain.common.validation import canonical_hash, fields, json_value, strings, text, timestamp


def _seconds(value):
    if type(value) not in (int,float) or not 0<=value<86400*365:
        raise ValueError("Offset must be finite nonnegative seconds below one year")


def validate_plan(plan):
    fields(plan,{"simulation_schema","synthetic","runtime_status","reference_date","releases",
                 "synthetic_anchors","publication_delay","observations","notes"},where="Simulator plan")
    if (plan["simulation_schema"]!="chain-simulation/v0.3" or plan["synthetic"] is not True
            or plan["runtime_status"]!="EXECUTABLE_STAGE6"):
        raise ValueError("Only executable synthetic stage-6 plans supported")
    date.fromisoformat(text(plan["reference_date"]));strings(plan["notes"])
    obs=fields(plan["observations"],{"states","decisions"},where="Simulator observations")
    if not isinstance(obs["states"],dict) or not isinstance(obs["decisions"],list):raise ValueError("Invalid observations")
    external={"ARRIVAL"}
    for state,anchor in obs["states"].items():
        text(state);text(anchor)
        if anchor in external:raise ValueError("Duplicate observation anchor")
        external.add(anchor)
    for rule in obs["decisions"]:
        fields(rule,{"checkpoint","decision","anchor"},where="Decision observation")
        for value in rule.values():text(value)
        if rule["anchor"] in external:raise ValueError("Duplicate observation anchor")
        external.add(rule["anchor"])
    delay=fields(plan["publication_delay"],{"offset_s","timing_origin"},where="Publication delay")
    _seconds(delay["offset_s"]);text(delay["timing_origin"])
    nodes={}
    for collection in ("releases","synthetic_anchors"):
        if not isinstance(plan[collection],list):raise ValueError("Schedule nodes must be lists")
        for node in plan[collection]:
            required={"id","anchor","offset_s","timing_origin"}
            optional={"requires"}
            if collection=="synthetic_anchors":optional.add("data")
            if collection=="releases":
                required|={"source","reference_source_time"}
                optional|={"actor","order_anchor","collection_anchor","relative_timestamps","arrival_timestamps"}
            fields(node,required,optional,"Simulator node")
            for key in ("id","anchor","timing_origin"):text(node[key])
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*",node["id"]):raise ValueError("Unsafe schedule node ID")
            _seconds(node["offset_s"])
            strings(node.get("requires",[]));json_value(node.get("data",{}))
            if not isinstance(node.get("data",{}),dict):raise ValueError("Anchor data must be an object")
            if node["id"] in nodes or node["id"] in external:raise ValueError("Duplicate node/anchor")
            if collection=="releases":
                source=Path(text(node["source"]));timestamp(node["reference_source_time"])
                if source.is_absolute() or ".." in source.parts or source.suffix!=".json":raise ValueError("Invalid source path")
                if "actor" in node and node["actor"]!="SIMULATED_CLINICIAN":raise ValueError("Unsupported hospital actor")
                strings(node.get("relative_timestamps",[]))
                strings(node.get("arrival_timestamps",[]))
                if set(node.get("relative_timestamps",[]))&set(node.get("arrival_timestamps",[])):
                    raise ValueError("Conflicting timestamp bindings")
            nodes[node["id"]]=node
    def dependencies(node):
        return {node["anchor"],*node.get("requires",[]),
                *[node[k] for k in ("order_anchor","collection_anchor") if k in node]}
    active=set();visited=set()
    def visit(name):
        if name in active:raise ValueError("Causal cycle")
        if name in visited or name in external:return
        if name not in nodes:raise ValueError("Unknown causal anchor: "+str(name))
        active.add(name)
        for dependency in dependencies(nodes[name]):text(dependency);visit(dependency)
        active.remove(name);visited.add(name)
    for name in nodes:visit(name)
    return copy.deepcopy(plan)


def load_plan(path):
    return validate_plan(_read_yaml(Path(path)))


class CausalScheduler:
    def __init__(self,plan,clock,execute):
        # validate once, pin a copy; caller mutations cannot change a running schedule.
        self.plan=validate_plan(plan);self.clock=clock;self.execute=execute
        self.anchors={};self.audit=[];self.tasks=[];self.failures=[]
        self._changed=asyncio.Condition()
        self._external={"ARRIVAL",*self.plan["observations"]["states"].values(),
                       *[r["anchor"] for r in self.plan["observations"]["decisions"]]}

    async def mark(self,name,at,data=None):
        if name not in self._external:raise ValueError("Cannot inject a scheduled or unknown anchor")
        return await self._record(name,at,data or {})

    async def _record(self,name,at,data):
        if timestamp(at)>self.clock.datetime():raise ValueError("Anchor has not occurred")
        json_value(data);entry={"at":at,"data":copy.deepcopy(data)}
        async with self._changed:
            if name in self.anchors:
                if self.anchors[name]!=entry:raise ValueError("Conflicting actual anchor: "+name)
                return
            self.anchors[name]=entry
            self.audit.append({"kind":"ANCHOR_OCCURRED","id":name,**entry})
            self._changed.notify_all()

    async def __aenter__(self):
        if self.tasks:raise RuntimeError("Scheduler already started")
        self.tasks=[asyncio.create_task(self._run(node)) for node in self.plan["synthetic_anchors"]+self.plan["releases"]]
        return self

    async def __aexit__(self,*_):
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        self.tasks=[]

    async def _run(self,node):
        try:
            deps={node["anchor"],*node.get("requires",[]),
                  *[node[k] for k in ("order_anchor","collection_anchor") if k in node]}
            async with self._changed:
                await self._changed.wait_for(lambda:deps<=self.anchors.keys())
                due=max(timestamp(self.anchors[node["anchor"]]["at"])+timedelta(seconds=node["offset_s"]),
                        *[timestamp(self.anchors[d]["at"]) for d in deps])
            await self.clock.sleep_until(due.isoformat())
            occurred=self.clock.now()
            data=await self.execute(copy.deepcopy(node),occurred) if "source" in node else copy.deepcopy(node.get("data",{}))
            await self._record(node["id"],occurred,data)
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.failures.append({"id":node["id"],"error":f"{type(exc).__name__}: {exc}"})
            self.audit.append({"kind":"SIMULATION_FAILED",**self.failures[-1]})
