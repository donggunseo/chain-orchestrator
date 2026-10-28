"""Validate an explicit synthetic clock driver; no engine execution."""
import copy
from pathlib import Path
from demo.io import read_yaml
from chain.common.validation import fields, positive, text

def load_driver(path,*,test_mode=False):
    if test_mode is not True:raise ValueError("Synthetic driver requires explicit test mode")
    plan=read_yaml(Path(path))
    fields(plan,{"driver_schema","synthetic","ticks","responses","notes"},where="Local test driver")
    if plan["driver_schema"]!="chain-local-test/v0.3" or plan["synthetic"] is not True:
        raise ValueError("Unsupported synthetic test driver")
    if not isinstance(plan["ticks"],list) or not plan["ticks"]:raise ValueError("Driver ticks required")
    for tick in plan["ticks"]:
        fields(tick,{"anchor","offset_s","wait"},where="Test tick")
        text(tick["anchor"])
        if type(tick["offset_s"]) not in (int,float) or not 0<=tick["offset_s"]<31536000:raise ValueError("Invalid test offset")
        if not isinstance(tick["wait"],dict) or len(tick["wait"])!=1:raise ValueError("One explicit test wait required")
        kind,value=next(iter(tick["wait"].items()))
        if kind in {"source_applied","anchor","result_accepted"}:text(value)
        elif kind in {"hitl_open","hitl_decided","console_bound"}:
            fields(value,{"checkpoint","count"},where="HITL test wait");text(value["checkpoint"]);positive(value["count"],integer=True)
        else:raise ValueError("Unsupported test wait: "+kind)
    if not isinstance(plan["responses"],list):raise ValueError("Response timings must be a list")
    for timing in plan["responses"]:
        fields(timing,{"checkpoint","anchor","offset_s"},where="Synthetic response timing")
        text(timing["checkpoint"]);text(timing["anchor"])
        if type(timing["offset_s"]) not in (int,float) or not 0<=timing["offset_s"]<31536000:raise ValueError("Invalid response offset")
    if not isinstance(plan["notes"],list) or not all(isinstance(n,str) for n in plan["notes"]):raise ValueError("Driver notes required")
    return copy.deepcopy(plan)
