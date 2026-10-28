"""Pure failure envelopes for exhausted technical work; never clinical defaults."""
import copy

from .runtime_contracts import validate_preflight_result, validate_reconcile_result
from .task_contracts import validate_agent_result, validate_work_result


def query_failure(request, *, code, now, retryable=False):
    result = {key: copy.deepcopy(request[key]) for key in
              ("identity", "engine_run_id", "run_pin", "request_id")}
    errors = [{"scope": request["scope"], "code": code, "retryable": retryable}]
    if request["contract_schema"] == "chain-fact-preflight-request/v1":
        result.update(contract_schema="chain-fact-preflight-result/v1", status="FAILED",
            coverage=[{"field": field, "verification": "UNCONFIRMED"} for field in request["scope"]],
            event_refs=[], source_basis={"kind": "OBSERVATION", "token": None, "observed_at": now},
            checked_at=now, errors=errors)
        return validate_preflight_result(result, request)
    result.update(contract_schema="chain-fact-reconcile-result/v1", source=request["source"],
        status="FAILED", from_checkpoint=request["checkpoint"],
        candidate_checkpoint=request["checkpoint"], event_refs=[], errors=errors)
    return validate_reconcile_result(result, request)


def work_failure(request, *, code, now):
    fields = ("identity", "engine_run_id", "run_pin", "task_id", "execution_id", "request_id",
              "input_revision", "predecessor_execution_id", "input_fingerprint")
    result = {key: copy.deepcopy(request[key]) for key in fields}
    result.update(input_snapshot_id=request["snapshot"]["snapshot_id"], status="FAILED",
                  produced_at=now, error_code=code)
    if "agent_id" in request:
        result.update(contract_schema="chain-fact-agent-result/v1", result=None, evidence=[],
            **{key: request[key] for key in ("agent_id", "agent_version", "implementation_hash", "mode")})
        return validate_agent_result(result, request)
    result.update(contract_schema="chain-fact-work-result/v1", receipt=None)
    return validate_work_result(result, request)
