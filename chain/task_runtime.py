"""Injected native-v1 providers at the Worker boundary; no Workflow I/O.

This Local harness does not load Deployment descriptors or register a Worker.
The owner issues pinned, scoped requests after preparation/permission checks;
the trusted Worker supplies providers with explicit input/output validation.
External receipt providers are used only through the conversions below.
"""
from __future__ import annotations

import copy

from .common.services import AgentServices, FixtureError, SummaryCache
from .common.effect_contracts import validate_effect_receipt

from .compiler import canonical_hash
from .errors import ConfigError
from .runtime_contracts import _hash, _text, timestamp, validate_scope
from .shape import check_json
from . import task_contracts as contracts


def _fail(code, path, message):
    raise ConfigError(code, path, message) from None


def fixture_commitment(request):
    """Exact synthetic input selector, deliberately independent of run/code pins.

    A fixture file can be part of an implementation pin, so including that pin
    in its own selector would create a content-hash cycle. Execution contracts
    still validate the separate implementation/input_fingerprint binding.
    """
    return canonical_hash({"facts": contracts.semantic_input(request["snapshot"]),
        "scope": validate_scope(request["scope"]),
        **{field: request[field] for field in ("agent_id", "agent_version", "mode")}})


class NativeAgentProvider:
    """Trusted Worker registration, not automatic installation or approval.

    Callbacks validate an Agent's own Schema beyond the shared wire contract.
    They receive (request, snapshot) and (payload, request, snapshot).
    """

    request_contract = contracts.REQUEST_SCHEMA
    result_contract = contracts.RESULT_SCHEMA

    def __init__(self, *, agent_id, version, implementation_hash, modes,
                 invoke, input_validator, output_validator):
        self.agent_id = _text(agent_id, "provider.agent_id")
        self.version = _text(version, "provider.version")
        self.implementation_hash = _hash(implementation_hash, "provider.implementation_hash")
        if not isinstance(modes, (list, tuple)):
            _fail("AGENT_PROVIDER_INVALID", "provider.modes", "Expected an explicit mode list")
        self.modes = validate_scope(list(modes), path="provider.modes")
        for name, callback in (("invoke", invoke), ("input_validator", input_validator),
                               ("output_validator", output_validator)):
            if not callable(callback):
                _fail("AGENT_PROVIDER_INVALID", "provider." + name, "An explicit callable is required")
        self.invoke = invoke
        self.validate_input = input_validator
        self.validate_output = output_validator


class V1FixtureProvider(NativeAgentProvider):
    """Already-loaded synthetic records; no fallback or future source lookup.

    records are exactly {input_hash, output}. input_hash=fixture_commitment of
    the actual request. The loader belongs to the Worker, outside this object.
    Multiple matches are an explicit test failure even if their outputs agree.
    """

    def __init__(self, records, *, agent_id, version, implementation_hash, modes,
                 input_validator, output_validator):
        check_json(records, path="fixtures")
        if not isinstance(records, list):
            _fail("FIXTURE_INVALID", "fixtures", "Expected explicit synthetic fixture records")
        self.records = copy.deepcopy(records)
        for record in self.records:
            if not isinstance(record, dict) or set(record) != {"input_hash", "output"}:
                _fail("FIXTURE_INVALID", "fixtures", "Expected a closed input commitment and output")
            _hash(record["input_hash"], "fixture.input_hash")
            if not isinstance(record["output"], dict):
                _fail("FIXTURE_INVALID", "fixture.output", "Expected a result object")
        super().__init__(agent_id=agent_id, version=version,
            implementation_hash=implementation_hash, modes=modes, invoke=self._invoke,
            input_validator=input_validator, output_validator=output_validator)

    def _invoke(self, request, snapshot, services):
        selected = [record for record in self.records if record["input_hash"] == fixture_commitment(request)]
        if not selected:
            raise FixtureError("FIXTURE_NOT_FOUND")
        if len(selected) != 1:
            raise FixtureError("FIXTURE_AMBIGUOUS")
        return copy.deepcopy(selected[0]["output"])


class StructuredSummaryProvider(NativeAgentProvider):
    """Requested structured cells only. This is composition, not NLP."""

    def __init__(self, *, agent_id, version, implementation_hash, mode="context",
                 return_cache_metadata=False):
        if type(return_cache_metadata) is not bool:
            _fail("AGENT_PROVIDER_INVALID", "return_cache_metadata", "Expected a boolean")
        self.return_cache_metadata = return_cache_metadata
        self.build_count = 0
        self.cache_hits = 0
        super().__init__(agent_id=agent_id, version=version,
            implementation_hash=implementation_hash, modes=[mode], invoke=self._invoke,
            input_validator=self._validate_input, output_validator=self._validate_output)

    def _validate_input(self, request, snapshot):
        if set(snapshot["facts"]) != set(request["scope"]):
            raise ValueError("Summary scope differs from input")

    def _validate_output(self, payload, request, snapshot):
        expected = {"structured_context", "missing_information"}
        if self.return_cache_metadata:
            expected.add("cache")
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("Summary output differs from declared composition")
        if payload["structured_context"] != snapshot["facts"]:
            raise ValueError("Summary changed source cells")
        missing = sorted(name for name, fact in snapshot["facts"].items() if fact["validity"] != "KNOWN")
        if payload["missing_information"] != missing:
            raise ValueError("Summary availability differs from input")

    def _invoke(self, request, snapshot, services):
        key = canonical_hash({"contract": "chain-fact-structured-context/v1",
            "input": contracts.semantic_input(snapshot), "scope": request["scope"],
            **{field: request[field] for field in
               ("agent_id", "agent_version", "implementation_hash", "mode")}})

        def build():
            self.build_count += 1
            return {"missing_information": sorted(name for name, fact in snapshot["facts"].items()
                                                  if fact["validity"] != "KNOWN")}

        # Cache the computed semantic result, then bind current provenance/time
        # cells. Different requests never receive an old envelope or Snapshot.
        payload, hit = services.cache.get_or_create(key, build)
        self.cache_hits += int(hit)
        payload["structured_context"] = copy.deepcopy(snapshot["facts"])
        if self.return_cache_metadata:
            # A retry of one execution must not return a different result just
            # because this process now has a cache hit. Operational hit counts
            # remain Worker-local observations, outside the clinical payload.
            payload["cache"] = {"key": key}
        return payload


class LocalTaskRuntime:
    """Synchronous native provider harness for Local/Activity callers only."""

    def __init__(self, agents, *, summary_cache=None, notifier=None, effects=None,
                 recipients=None):
        if not isinstance(agents, dict) or any(not isinstance(name, str) or not name for name in agents):
            _fail("AGENT_PROVIDER_INVALID", "agents", "Expected explicit logical provider registrations")
        self.agents = dict(agents)
        self.cache = summary_cache if summary_cache is not None else SummaryCache()
        self.notifier = notifier
        self.effects = effects
        self.recipients = copy.deepcopy(recipients or {})

    def invoke_agent(self, request, *, output_fields, request_validator=None,
                     result_validator=None, prerequisite_results=None, verifications=None):
        request = contracts.validate_agent_request(request)
        provider = self.agents.get(request["agent_id"])
        if provider is None:
            _fail("AGENT_NOT_REGISTERED", "agent_id", "No native provider is registered")
        if (getattr(provider, "request_contract", None) != contracts.REQUEST_SCHEMA
                or getattr(provider, "result_contract", None) != contracts.RESULT_SCHEMA):
            _fail("CONTRACT_ADAPTER_REQUIRED", "provider", "Legacy providers require an explicit v1 adapter")
        if (getattr(provider, "agent_id", None) != request["agent_id"]
                or getattr(provider, "version", None) != request["agent_version"]
                or getattr(provider, "implementation_hash", None) != request["implementation_hash"]
                or request["mode"] not in getattr(provider, "modes", ())):
            _fail("AGENT_IMPLEMENTATION_MISMATCH", "provider", "Request differs from Worker registration")
        if not all(callable(getattr(provider, name, None)) for name in ("invoke", "validate_input", "validate_output")):
            _fail("AGENT_PROVIDER_INVALID", "provider", "Provider needs explicit invocation and Schema callbacks")
        if request_validator is not None:
            request_validator(copy.deepcopy(request))
        prerequisites, checks = self._sidecars(request, prerequisite_results, verifications)
        fields = ("identity", "engine_run_id", "run_pin", "task_id", "execution_id", "request_id",
                  "input_revision", "predecessor_execution_id", "agent_id", "agent_version",
                  "implementation_hash", "mode", "input_fingerprint")
        result = {"contract_schema": contracts.RESULT_SCHEMA,
            **{field: copy.deepcopy(request[field]) for field in fields},
            "input_snapshot_id": request["snapshot"]["snapshot_id"], "status": "SUCCESS",
            "result": None, "evidence": copy.deepcopy(request["dependencies"]),
            "produced_at": request["evaluated_at"]}
        try:
            snapshot = copy.deepcopy(request["snapshot"])
            provider.validate_input(copy.deepcopy(request), copy.deepcopy(snapshot))
            services = AgentServices(self.cache, None)
            # These are detached owner-approved inputs, never merged into raw
            # source Context. The runtime does not itself approve a decision.
            services.source_snapshot = copy.deepcopy(snapshot)
            services.prerequisite_results = copy.deepcopy(prerequisites)
            services.verifications = copy.deepcopy(checks)
            services.structured_contexts = {name: copy.deepcopy(value["result"]["structured_context"])
                for name, value in prerequisites.items() if "structured_context" in value["result"]}
            payload = provider.invoke(copy.deepcopy(request), copy.deepcopy(snapshot), services)
            provider.validate_output(copy.deepcopy(payload), copy.deepcopy(request), copy.deepcopy(snapshot))
            result["result"] = copy.deepcopy(payload)
            contracts.validate_agent_result(result, request, output_fields=output_fields)
        except (FixtureError, ValueError, TypeError, KeyError) as exc:
            result.update(status="FAILED", result=None, evidence=[],
                          error_code=exc.code if isinstance(exc, FixtureError) else "AGENT_INVALID_OUTPUT")
        result = contracts.validate_agent_result(result, request, output_fields=output_fields)
        if result_validator is not None:
            result_validator(copy.deepcopy(result), copy.deepcopy(request))
        return result

    @staticmethod
    def _sidecars(request, prerequisite_results, verifications):
        prerequisites = {} if prerequisite_results is None else copy.deepcopy(prerequisite_results)
        checks = {} if verifications is None else copy.deepcopy(verifications)
        check_json(prerequisites, path="prerequisite_results")
        check_json(checks, path="verifications")
        if not isinstance(prerequisites, dict) or set(prerequisites) != set(request["prerequisite_result_ids"]):
            _fail("PREREQUISITE_RESULT_MISSING", "prerequisite_results", "Provide exactly the accepted prerequisite results")
        if not isinstance(checks, dict) or set(checks) != set(request["verification_ids"]):
            _fail("VERIFICATION_RECORD_MISSING", "verifications", "Provide exactly the issued verification records")
        for identifier, value in prerequisites.items():
            if (not isinstance(value, dict) or value.get("contract_schema") != contracts.RESULT_SCHEMA
                    or value.get("execution_id") != identifier or value.get("request_id") != identifier
                    or value.get("status") != "SUCCESS" or not isinstance(value.get("result"), dict)):
                _fail("PREREQUISITE_RESULT_INVALID", "prerequisite_results", "A prerequisite needs a recorded successful native result")
            _hash(value.get("input_snapshot_id"), "prerequisite_results.input_snapshot_id")
            if timestamp(value.get("produced_at"), path="prerequisite_results.produced_at") > timestamp(request["evaluated_at"]):
                _fail("FUTURE_INPUT", "prerequisite_results", "A prerequisite was not yet available at evaluation time")
            for field in ("identity", "engine_run_id", "run_pin"):
                if value.get(field) != request[field]:
                    _fail("IDENTITY_MISMATCH", "prerequisite_results", "A prerequisite belongs to another run")
        for identifier, value in checks.items():
            if (not isinstance(value, dict) or value.get("contract_schema") != "chain-fact-verification/v1"
                    or value.get("verification_id") != identifier
                    or canonical_hash({k: v for k, v in value.items() if k != "verification_id"}) != identifier
                    or request["task_id"] not in value.get("consumer_task_ids", [])):
                _fail("VERIFICATION_RECORD_INVALID", "verifications", "Verification does not bind the issued task")
            for field in ("identity", "engine_run_id", "run_pin"):
                if value.get(field) != request[field]:
                    _fail("IDENTITY_MISMATCH", "verifications", "Verification belongs to another run")
        return prerequisites, checks

    def invoke_work(self, request):
        request = contracts.validate_work_request(request)
        if request["kind"] == "hitl":
            _fail("HITL_EXECUTION_NOT_IMPLEMENTED", "kind", "HITL lifecycle execution belongs to phase 6")
        fields = ("identity", "engine_run_id", "run_pin", "task_id", "execution_id", "request_id",
                  "input_revision", "predecessor_execution_id", "input_fingerprint")
        result = {"contract_schema": "chain-fact-work-result/v1",
            **{field: copy.deepcopy(request[field]) for field in fields},
            "input_snapshot_id": request["snapshot"]["snapshot_id"], "status": "SUCCESS",
            "receipt": None, "produced_at": request["evaluated_at"]}
        task = request["task"]
        if request["kind"] == "notify":
            if self.notifier is None:
                result.update(status="FAILED", error_code="NOTIFIER_NOT_CONFIGURED")
            else:
                recipient = self.recipients.get(task["recipient"],
                    {"channel": "LOG", "address_ref": task["recipient"]})
                if set(recipient) != {"channel", "address_ref"} or recipient["channel"] not in {"LOG", "CONSOLE"}:
                    _fail("NOTIFIER_BINDING_INVALID", "recipient", "Only explicit console/log mock bindings are supported")
                wire = {"request_schema": "chain-notification/v0.3", "request_id": request["execution_id"],
                    "episode_id": request["identity"]["episode_id"], "template": task["template"],
                    "channel": recipient["channel"], "recipient": recipient["address_ref"]}
                result["receipt"] = self.notifier.record(wire, recorded_at=request["evaluated_at"])
                receipt = result["receipt"]
                expected = {"receipt_schema", "request_id", "episode_id", "payload_hash", "template",
                            "channel", "recipient", "status", "recorded_at"}
                if (not isinstance(receipt, dict) or set(receipt) != expected
                        or receipt["receipt_schema"] != "chain-notification-receipt/v0.3"
                        or receipt["payload_hash"] != canonical_hash(wire)
                        or receipt["status"] != "MOCK_RECORDED"
                        or any(receipt[field] != wire[field] for field in
                               ("request_id", "episode_id", "template", "channel", "recipient"))):
                    _fail("WORK_RECEIPT_INVALID", "receipt", "Mock notification receipt differs from the issued request")
                timestamp(receipt["recorded_at"], path="receipt.recorded_at")
        else:
            if self.effects is None:
                result.update(status="FAILED", error_code="EFFECTS_NOT_CONFIGURED")
            else:
                wire = {"request_schema": "chain-effect/v0.3", "request_id": request["execution_id"],
                    "episode_id": request["identity"]["episode_id"], "effect_version": request["effect_version"],
                    **{field: task[field] for field in ("operation", "resource", "target")}}
                result["receipt"] = self.effects.record(wire, recorded_at=request["evaluated_at"])
                validate_effect_receipt(result["receipt"], wire)
        return contracts.validate_work_result(result, request)
