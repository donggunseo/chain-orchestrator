"""Read saved human responses only in explicitly selected test mode."""
import copy
from pathlib import Path
from demo.io import read_json
from chain.common.validation import canonical_hash, fields, strings, text

def load_recorded(path,*,test_mode=False):
    if test_mode is not True:raise ValueError("Recorded responses require explicit test mode")
    source=read_json(path);responses=[]
    if not isinstance(source,dict):raise ValueError("Saved responses require a JSON object")
    if source.get("test_mode_only") is True and set(source)=={"test_mode_only","events"}:
        if not isinstance(source["events"],list):raise ValueError("Saved events must be a list")
        for event in source["events"]:
            if not isinstance(event,dict) or event.get("event_type")!="HITL_DECISION":raise ValueError("Saved event is not a human decision")
            try:
                payload=event["payload"]
                responses.append({"checkpoint":payload["checkpoint"],"decision":payload["decision"],
                    "actor":{k:payload["actor"][k] for k in ("role","staff_id")},
                    "confirmed_items":copy.deepcopy(payload.get("confirmed_items",[])),"comment":payload["comment"]})
            except (KeyError,TypeError) as exc:raise ValueError("Incomplete saved human response") from exc
    else:
        fields(source,{"recorded_schema","test_mode_only","responses"},where="Recorded test responses")
        if source["recorded_schema"]!="chain-recorded/v0.3" or source["test_mode_only"] is not True:
            raise ValueError("Only explicit synthetic recorded responses supported")
        responses=copy.deepcopy(source["responses"])
    if not isinstance(responses,list) or not responses:raise ValueError("Recorded responses must be a nonempty list")
    for response in responses:
        fields(response,{"checkpoint","decision","actor","confirmed_items","comment"},where="Saved response")
        for key in ("checkpoint","decision","comment"):text(response[key])
        fields(response["actor"],{"role","staff_id"},where="Saved actor")
        for value in response["actor"].values():text(value)
        strings(response["confirmed_items"])
    return {"responses":responses,"source_path":str(Path(path).resolve()),"source_hash":canonical_hash(source),
            "binding":"EXPLICIT_RECORDED_TEST; current request/snapshot/time; no regimen/order/old IDs copied"}
