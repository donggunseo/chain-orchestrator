"""Explicit synthetic timestamp rebinding; no prose interpretation or clinical math."""
import copy
import hashlib
import re
from datetime import timedelta

from chain.common.source_contracts import validate_record
from chain.common.validation import canonical_hash, timestamp


def _set_relative(result,original,path,source_time,occurred):
    keys=path.split(".");target=result;old=original
    for key in keys[:-1]:target=target[key];old=old[key]
    key=keys[-1]
    target[key]=(timestamp(occurred)+(timestamp(old[key])-timestamp(source_time))).isoformat()


def rebind_record(original,node,*,occurred,arrival_shift,anchors):
    validate_record(original)
    if timestamp(original["source_time"])!=timestamp(node["reference_source_time"]):
        raise ValueError("Template source time differs from pinned schedule")
    old_source=original["source_time"]
    def shift(value):
        if isinstance(value,dict):return {k:shift(v) for k,v in value.items()}
        if isinstance(value,list):return [shift(v) for v in value]
        if isinstance(value,str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)",value):
            if timestamp(value)==timestamp(old_source):return occurred
            return (timestamp(value)+arrival_shift).isoformat()
        return value
    result=shift(copy.deepcopy(original));result["source_time"]=occurred
    for path in node.get("relative_timestamps",[]):
        _set_relative(result,original,path,old_source,occurred)
    for path in node.get("arrival_timestamps",[]):
        keys=path.split(".");target=result;old=original
        for key in keys[:-1]:target=target[key];old=old[key]
        target[keys[-1]]=(timestamp(old[keys[-1]])+arrival_shift).isoformat()
    if "collection_anchor" in node:
        result["payload"]["specimen_collected_time"]=anchors[node["collection_anchor"]]["at"]
    if "order_anchor" in node:
        data=anchors[node["order_anchor"]]["data"]
        if "order_id" in data:
            result["payload"]["order_id"]=data["order_id"]
            if "ncct_order_id" in result["facts"]:result["facts"]["ncct_order_id"]["value"]=data["order_id"]
        elif result["payload"].get("order_id") not in data.get("order_ids",[]):
            raise ValueError("Result has no matching actually entered order")
    document=result["raw"].get("document")
    if document is not None and arrival_shift:
        reference=timestamp(original["source_time"])
        # Literal tokens from this synthetic document only; no extraction/inference.
        def time_token(match):
            hour,minute=map(int,match.group().split(":"))
            if hour>23 or minute>59:return match.group()
            return (reference.replace(hour=hour,minute=minute)+arrival_shift).strftime("%H:%M")
        document["text"]=re.sub(r"\b\d{2}:\d{2}\b",time_token,document["text"])
        document["text"]=document["text"].replace(reference.strftime("%Y-%m-%d"),(reference+arrival_shift).strftime("%Y-%m-%d"))
        document["text_hash"]="sha256:"+hashlib.sha256(document["text"].encode()).hexdigest()
        document["char_count"]=len(document["text"])
        if "text_hash" in result["payload"]:result["payload"]["text_hash"]=document["text_hash"]
        if "char_count" in result["payload"]:result["payload"]["char_count"]=len(document["text"])
    old_event=result["raw"].get("event")
    if old_event is not None:
        old_event["source_event_time"]=occurred
        old_event["emitted_time"]=occurred
        old_event["payload"]=copy.deepcopy(result["payload"])
        old_event["integrity"]["payload_hash"]=canonical_hash(old_event["payload"])
    result["raw"]["simulation"]={"template_content_hash":original["content_hash"],
        "actual_occurrence":occurred,"arrival_shift_seconds":arrival_shift.total_seconds(),
        "time_binding":"EXPLICIT_SYNTHETIC_LITERAL_REBIND_NO_NLP"}
    result["annotation"]+="; EXPLICIT_STAGE6_SYNTHETIC_TIME_REBIND"
    result["content_hash"]=canonical_hash({k:v for k,v in result.items() if k!="content_hash"})
    validate_record(result)
    return result
