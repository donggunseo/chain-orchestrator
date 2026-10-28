"""Post-run semantic checks; never imported by the native Workflow or engine."""
import json
from pathlib import Path


def compare(snapshot, publications, simulation, expected_path):
    expected = json.loads(Path(expected_path).read_text(encoding="utf8"))
    if (not isinstance(expected, dict) or set(expected) != {"expected_schema", "state_path", "terminal_state"}
            or expected["expected_schema"] != "chain-fact-demo-expected/v1"
            or not isinstance(expected["state_path"], list) or not expected["state_path"]
            or any(not isinstance(item, str) or not item for item in expected["state_path"])):
        raise ValueError("Unsupported fact demo expectation")
    states = [row["data"]["id"] for row in snapshot.get("changes", []) if row["kind"] == "STATE_ENTERED"]
    anchors = simulation.get("anchors", {})
    ct = next((row for row in publications if row["id"] == "ct_order"), None)
    ncct = next((row for row in publications if row["id"] == "ncct"), None)
    causal = ct is None and ncct is None
    if ct:
        causal = all(name in anchors for name in ("HITL1_PROCEED_ACCEPTED", "S2_ENTERED", "ct_order"))
        causal = causal and ct["actor"] == "SIMULATED_CLINICIAN"
        if ncct:
            causal = causal and "ncct" in anchors and snapshot["facts"]["facts"]["ct_order_id"]["value"] == snapshot["facts"]["facts"]["ncct_order_id"]["value"]
    checks = {"state_path": states == expected["state_path"],
        "terminal_state": snapshot.get("state") == expected["terminal_state"],
        "finalized": snapshot.get("termination_phase") == "FINALIZED", "ct_causality": bool(causal),
        "simulation_errors_absent": not simulation.get("failures"),
        "no_agent_failures": not any(row.get("result", {}).get("status") == "FAILED"
            for row in snapshot.get("executions", {}).values() if row.get("result"))}
    return {"comparison_schema": "chain-fact-demo-comparison/v1", "checks": checks,
            "checks_passed": all(checks.values()), "observed_state_path": states}
