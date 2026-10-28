"""Explicit demo driver and recorded human responses, outside the native core."""
import asyncio
import copy
from datetime import timedelta
from uuid import uuid4

from chain.common.validation import timestamp
from demo.support.test_driver import load_driver
from demo.support.recorded_hitl import load_recorded


def done(snapshot):
    return snapshot.get("termination_phase") == "FINALIZED"


def open_question(snapshot, definition_id):
    matches = [item for item in snapshot.get("hitl", {}).get("requests", {}).values()
        if item["decision_definition_id"] == definition_id and item["status"] == "OPEN"]
    if len(matches) > 1:
        raise ValueError("More than one open question for a decision")
    return matches[0] if matches else None


def ready(wait, snapshot, hospital, bindings, *, client_requests=()):
    kind, value = next(iter(wait.items()))
    if kind == "anchor":
        return value in hospital.scheduler.anchors
    if kind == "source_applied":
        row = next((row for row in hospital.publications if row["id"] == value), None)
        return bool(row and snapshot.get("event_receipts", {}).get(row["event"]["event_id"], {}).get("status") in {"APPLIED", "DUPLICATE"})
    if kind == "result_accepted":
        task = bindings["results"][value]
        return any(snapshot["tasks"][row["request"]["task_id"]]["task_definition_id"] == task
            and row["result_use"] == "CURRENT" and row["result"] and row["result"]["status"] == "SUCCESS"
            for row in snapshot.get("executions", {}).values())
    definition_id = bindings["decisions"][value["checkpoint"]]
    if kind == "console_bound":
        return len({row["request_id"] for row in client_requests
                    if row.get("decision_definition_id") == definition_id}) >= value["count"]
    questions = [q for q in snapshot.get("hitl", {}).get("requests", {}).values()
                 if q["decision_definition_id"] == definition_id]
    if kind == "hitl_decided":
        return len([q for q in questions if q["status"] == "DECIDED"]) >= value["count"]
    return len(questions) >= value["count"] and open_question(snapshot, definition_id) is not None


async def drive(plan, hospital, runtime, clock, bindings, *, client_requests=(), write=print):
    for tick in plan["ticks"]:
        while tick["anchor"] not in hospital.scheduler.anchors:
            snapshot = await runtime.progress()
            if done(snapshot):
                return
            await hospital.observe(snapshot)
            await asyncio.sleep(.03)
        due = timestamp(hospital.scheduler.anchors[tick["anchor"]]["at"]) + timedelta(seconds=tick["offset_s"])
        if due < clock.datetime():
            # A clinician can keep a question open beyond a nominal response
            # time. Do not move the synthetic hospital clock backwards.
            due = clock.datetime()
        clock.advance(due.isoformat())
        write("TEST_CLOCK_AT | " + clock.now())
        if "hitl_decided" in tick["wait"]:
            write("DEMO_DECISION_READY | " + tick["wait"]["hitl_decided"]["checkpoint"])
        while True:
            snapshot = await runtime.progress()
            if done(snapshot):
                return
            await hospital.observe(snapshot)
            if ready(tick["wait"], snapshot, hospital, bindings, client_requests=client_requests):
                break
            await asyncio.sleep(.03)


def bind_response(response, snapshot, question, *, now):
    evidence = snapshot["hitl"]["evidence"][question["revision_ids"][-1]]
    manifest = snapshot["run_manifest"]
    return {"contract_schema": "chain-fact-hitl-decision/v1",
        **{key: manifest[key] for key in ("identity", "engine_run_id", "run_pin")},
        "decision_id": "recorded-" + uuid4().hex, "request_id": question["request_id"],
        "evidence_revision": question["current_revision"], "evidence_id": evidence["evidence_id"],
        "evidence_snapshot_id": evidence.get("snapshot_id") or evidence["snapshot"]["snapshot_id"], "decision": response["decision"],
        "actor": copy.deepcopy(response["actor"]), "confirmed_items": copy.deepcopy(response["confirmed_items"]),
        "evidence_viewed": [evidence["evidence_id"]], "comment": response["comment"], "recorded_at": now}


async def consume(source, runtime, hospital, clock, plan, bindings, *, write=print):
    for response in source["responses"]:
        if response["checkpoint"] not in bindings["decisions"]:
            raise ValueError("Recorded response refers to an unknown demo checkpoint")
        decision_id = bindings["decisions"][response["checkpoint"]]
        timing = next((row for row in plan["responses"] if row["checkpoint"] == response["checkpoint"]), None)
        while True:
            snapshot = await runtime.progress()
            if done(snapshot):
                raise ValueError("Episode ended before recorded responses were consumed")
            question = open_question(snapshot, decision_id)
            anchor = hospital.scheduler.anchors.get(timing["anchor"]) if timing else None
            due = timestamp(anchor["at"]) + timedelta(seconds=timing["offset_s"]) if anchor else None
            if question and (not timing or due and clock.datetime() >= due):
                break
            await asyncio.sleep(.03)
        response_wire = bind_response(response, snapshot, question, now=runtime.now())
        await runtime.submit_decision(response_wire)
        write("EXPLICIT_RECORDED_TEST_SUBMITTED | " + question["request_id"])
        while True:
            snapshot = await runtime.progress()
            receipt = next((row["receipt"] for row in snapshot.get("ingress_receipts", [])
                            if row.get("decision_id") == response_wire["decision_id"]), None)
            if receipt:
                if receipt["status"] != "ACCEPTED":
                    raise ValueError("Recorded response rejected: " + receipt.get("code", receipt["status"]))
                break
            await asyncio.sleep(.03)
