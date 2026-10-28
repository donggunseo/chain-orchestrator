"""Original causal hospital schedule, observed through the native Query.

The hospital owns its historical synthetic clock. A real observed State or
decision anchors a new synthetic relative interval at the hospital clock's
current position; the actual engine timestamp is retained as separate metadata.
No future source document is opened before its causal release occurs.
"""
import asyncio
import copy
from datetime import timedelta

from chain.common.source_contracts import identity
from chain.common.validation import timestamp
from demo.simulator.hospital import HospitalSimulator


class FactHospital(HospitalSimulator):
    def __init__(self, episode, staging, store, clock, *, decision_bindings, plan_path=None):
        # Publications model the hospital/backend becoming queryable. CHAIN
        # discovers them through its own periodic or work-preparation query.
        super().__init__(episode, staging, store, clock, None, plan_path=plan_path)
        self.decision_bindings = copy.deepcopy(decision_bindings)
        self.manifest.update(simulation_version="fact-demo/v1", source_delivery="POLLING_ONLY",
            clock_binding="OBSERVED_ENGINE_EVENT_TO_CURRENT_SYNTHETIC_CLOCK")

    async def start(self, initial):
        if self.started or identity(initial) != self.store.identity:
            raise ValueError("Hospital already started or Episode identity differs")
        self.arrival_shift = timestamp(initial["arrival_time"]) - timestamp(self.reference_arrival)
        if self.arrival_shift:
            raise ValueError("Packaged exact fixtures require the original synthetic arrival")
        self.started = True
        await self.scheduler.mark("ARRIVAL", initial["arrival_time"])

    async def observe(self, snapshot):
        if not snapshot.get("ready"):
            return
        if snapshot["run_manifest"]["identity"] != self.store.identity:
            raise ValueError("Observed a different Episode")
        for change in snapshot.get("changes", []):
            if change["kind"] != "STATE_ENTERED":
                continue
            anchor = self.plan["observations"]["states"].get(change["data"]["id"])
            if anchor and anchor not in self.scheduler.anchors:
                await self.scheduler.mark(anchor, self.clock.now(), {
                    "state": change["data"]["id"], "engine_observed_at": change["at"],
                    "engine_view_seq": change["view_seq"]})
        questions = snapshot.get("hitl", {}).get("requests", {})
        decisions = snapshot.get("decisions", {})
        for row in snapshot.get("ingress_receipts", []):
            receipt = row["receipt"]
            if row["kind"] != "decision" or receipt.get("status") != "ACCEPTED":
                continue
            question = questions.get(receipt.get("request_id"))
            accepted = decisions.get(row.get("decision_id"), {}).get("record")
            if not question or not accepted:
                continue
            for rule in self.plan["observations"]["decisions"]:
                if (question["decision_definition_id"] == self.decision_bindings[rule["checkpoint"]]
                        and accepted["decision"] == rule["decision"]
                        and rule["anchor"] not in self.scheduler.anchors):
                    await self.scheduler.mark(rule["anchor"], self.clock.now(), {
                        "request_id": question["request_id"], "engine_observed_at": row["at"],
                        "decision_id": row.get("decision_id")})

    async def _publish(self, node, record, occurred):
        try:
            due = (timestamp(occurred) + timedelta(seconds=self.plan["publication_delay"]["offset_s"])).isoformat()
            await self.clock.sleep_until(due)
            systems = self.store.site["sources"]
            matches = [name for name, spec in systems.items() if spec["system"] == record["source_ref"]["system"]]
            if len(matches) != 1:
                raise ValueError("A publication needs one declared logical source")
            source = matches[0]
            event = self.store.publish(record, source_id=source,
                event_id=self.store.identity["episode_id"] + ":FACT-SIM:" + node["id"], at=self.clock.now())
            self.publications.append({"id": node["id"], "actor": node.get("actor", "MOCK_SOURCE"),
                "source_id": source, "event": copy.deepcopy(event)})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failures.append({"id": node["id"], "error": f"{type(error).__name__}: {error}"})
