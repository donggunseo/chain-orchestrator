"""Pure authoring-to-definition compiler. Does not execute rules or patient work."""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .errors import ConfigError
from .semantic import validate_semantics
from .shape import AuthoringValidator, check_json

COMPILER_VERSION = "chain-fact-compiler/v1.0.0"
NORMALIZATION_VERSION = "chain-json-map-order/v1"
DEFINITION_SCHEMA = "chain-fact-definition/v1"
MANIFEST_SCHEMA = "chain-fact-configuration/v1"
DOCUMENT_SCHEMAS = {
    "clinical": "chain-clinical/v1", "site": "chain-site/v1",
    "safety": "chain-safety/v1", "deployment": "chain-deployment/v1",
    "metadata": "chain-module-metadata/v1",
}


def canonical_bytes(value):
    check_json(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf8")


def canonical_hash(value):
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def normalized(value):
    """Mappings sort by key; list order is preserved, never assigned priority."""
    return json.loads(canonical_bytes(value))


def compile_operand(value):
    if "ref" in value:
        parts = value["ref"].split(".")
        return {"kind": "ref", "namespace": parts[0], "name": parts[1], "path": parts[2:]}
    return {"kind": "literal", "value": copy.deepcopy(value["literal"])}


def compile_expression(expression):
    op, value = next(iter(expression.items()))
    if op in {"all", "any"}:
        return {"op": op, "args": [compile_expression(v) for v in value]}
    if op == "not":
        return {"op": op, "args": [compile_expression(value)]}
    if op == "requirement":
        return {"op": op, "requirement_id": value}
    if op in {"eq", "ne", "gte", "in"}:
        return {"op": op, "args": [compile_operand(v) for v in value]}
    if op == "is_known":
        return {"op": op, "args": [compile_operand(value)]}
    if op == "has_status":
        return {"op": op, "args": [compile_operand({"ref": value["ref"]})],
                "status": value["status"]}
    raise ConfigError("OPERATOR_UNSUPPORTED", "expression", "Unsupported condition operator")


def _projection(source):
    return {"fallback": source["fallback"], "selection": "highest_true_priority",
            "rules": [{"id": name, **copy.deepcopy(rule), "when": compile_expression(rule["when"])}
                      for name, rule in sorted(source["rules"].items(),
                          key=lambda pair: (-pair[1]["priority"], pair[0]))]}


def _states(clinical, metadata):
    result = copy.deepcopy(clinical["states"])
    for state in result.values():
        task_ids = state["Required Capability"]["task_refs"]
        selected = [(tid, clinical["tasks"][tid]) for tid in task_ids]
        state["Agent Input"] = {"derived": [
            {"task": tid, "agent": task["agent"], "context": task["context"],
             "fields": copy.deepcopy(clinical["contexts"][task["context"]]["fields"])}
            for tid, task in selected if task["kind"] == "agent"]}
        state["Expected Agent Output"] = {"derived": [
            {"task": tid, "mode": task["mode"], "output_fields": copy.deepcopy(
                metadata["agents"][task["agent"]]["modes"][task["mode"]]["output_fields"])}
            for tid, task in selected if task["kind"] == "agent"]}
        state["HITL"] = {"decision_refs": [task["decision"] for _, task in selected if task["kind"] == "hitl"]}
        state["Guard / Transition condition"] = {
            "projection_rule_refs": copy.deepcopy(state["Entry Trigger"]["projection_rule_refs"]),
            "requirements_are_independent": True}
        state["Next State / Branch"] = {"model": "global_projection", "projection_ref": "state_projection"}
    return result


def validate_documents(documents, schema):
    check_json(documents)
    if set(documents) != set(DOCUMENT_SCHEMAS):
        raise ConfigError("DOCUMENT_SET_INVALID", "$", "Five authoring documents are required")
    validator = AuthoringValidator(schema)
    for name, expected in DOCUMENT_SCHEMAS.items():
        doc = documents[name]
        if not isinstance(doc, dict) or doc.get("schema") != expected:
            raise ConfigError("DOCUMENT_VERSION_UNSUPPORTED", name + ".schema", "Wrong document kind or version")
        validator.validate(doc, path=name)
    validate_semantics(documents)


def _validate_bindings(documents, bindings, schema):
    """Check the pure compiler's input boundary, not installed code execution."""
    check_json(bindings)
    required = {"document_hash", "public_agents", "worker_bindings", "installation_evidence", "blockers", "runtime_ready"}
    if not isinstance(bindings, dict) or set(bindings) != required:
        raise ConfigError("BINDING_RESULT_INVALID", "bindings", "Expected a closed binding resolution result")
    if bindings["document_hash"] != canonical_hash(documents):
        raise ConfigError("BINDING_DOCUMENT_MISMATCH", "bindings.document_hash", "Bindings were resolved against different authoring documents")
    if bindings["runtime_ready"] is not False or not isinstance(bindings["public_agents"], dict):
        raise ConfigError("BINDING_RESULT_INVALID", "bindings", "Configuration inspection cannot claim runtime readiness")
    if not isinstance(bindings["worker_bindings"], dict) or not isinstance(bindings["installation_evidence"], dict):
        raise ConfigError("BINDING_RESULT_INVALID", "bindings", "Expected private binding objects")
    worker = bindings["worker_bindings"]
    if set(worker) != {"agents", "sources", "notifier", "effects"} or not isinstance(worker["agents"], dict):
        raise ConfigError("BINDING_RESULT_INVALID", "bindings.worker_bindings", "Expected closed Worker bindings")
    for name, evidence in bindings["installation_evidence"].items():
        fields = {"implementation_ref", "descriptor_hash", "metadata_hash", "version", "status", "approval_ref", "files"}
        if (not isinstance(evidence, dict) or set(evidence) != fields
                or not isinstance(evidence["version"], str) or not evidence["version"]
                or evidence["status"] != "APPROVED" or not isinstance(evidence["approval_ref"], str)
                or not evidence["approval_ref"] or not isinstance(evidence["files"], dict)
                or not all(isinstance(evidence[key], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", evidence[key])
                           for key in ("descriptor_hash", "metadata_hash"))):
            raise ConfigError("BINDING_RESULT_INVALID", "bindings.installation_evidence", "Expected verified installation evidence")
        parts = name.split(":", 1)
        tables = {"agent": documents["metadata"]["agents"], "source": documents["site"]["sources"],
                  "notifier": {"notifier":None}, "effect": documents["site"]["effect_targets"]}
        if len(parts) != 2 or parts[0] not in tables or parts[1] not in tables[parts[0]]:
            raise ConfigError("BINDING_RESULT_INVALID", "bindings.installation_evidence", "Expected a logical installed module ID")
    clinical = documents["clinical"]
    pending = [rule["task"] for rule in clinical["work_rules"].values()]
    pending += [timer["on_expire"]["id"] for timer in clinical["timers"].values() if timer["on_expire"]["kind"] == "task"]
    selected = set()
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        selected.add(name)
        pending.extend(clinical["tasks"][name]["after"])
    expected_agents = {clinical["tasks"][name]["agent"] for name in selected if clinical["tasks"][name]["kind"] == "agent"}
    contexts = {task["context"] if "context" in task else clinical["decisions"][task["decision"]]["evidence_context"]
                for name in selected for task in [clinical["tasks"][name]]}
    if any(clinical["contexts"][name]["summary"] for name in contexts) or any(
            documents["metadata"]["agents"][name]["requires_summary"] for name in expected_agents):
        expected_agents.add(clinical["context_service"]["agent"])
    if set(bindings["public_agents"]) != expected_agents or set(worker["agents"]) != expected_agents:
        raise ConfigError("BINDING_AGENT_MISMATCH", "bindings.public_agents", "Bindings do not cover selected Agent dependency closure")
    contracts = {}
    extra_fields = {"installation_state", "implementation_hash"}
    spec_fields = {"version", "request_contract", "result_contract", "modes", "requires_summary", "actions", "data_scopes"}
    for name, entry in bindings["public_agents"].items():
        path = "bindings.public_agents." + name
        if not isinstance(entry, dict) or set(entry) != spec_fields | extra_fields:
            raise ConfigError("PUBLIC_BINDING_FIELD_INVALID", path, "Public module contracts cannot contain private binding fields")
        contracts[name] = {key: copy.deepcopy(entry[key]) for key in spec_fields}
        if name not in documents["metadata"]["agents"]:
            raise ConfigError("BINDING_AGENT_MISMATCH", path, "Unknown public Agent")
        def contract_normalized(contract):
            value = copy.deepcopy(contract)
            for key in ("actions", "data_scopes"):
                value[key] = sorted(value[key])
            for mode in value["modes"].values():
                mode["output_fields"] = sorted(mode["output_fields"])
            return value
        # Shape checking precedes normalization so malformed values fail clearly.
        AuthoringValidator(schema).validate({"schema": DOCUMENT_SCHEMAS["metadata"], "agents": {name: contracts[name]}}, path="metadata")
        if contract_normalized(contracts[name]) != contract_normalized(documents["metadata"]["agents"][name]):
            raise ConfigError("BINDING_AGENT_MISMATCH", path, "Public contract differs from current provider metadata")
        installed, digest = entry["installation_state"], entry["implementation_hash"]
        if not ((installed == "VERIFIED" and isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest))
                or (installed == "UNAVAILABLE" and digest is None)):
            raise ConfigError("BINDING_RESULT_INVALID", path, "Installation state and hash disagree")
        private = worker["agents"][name]
        if not isinstance(private, dict) or set(private) != {"implementation_ref", "backend", "descriptor"}:
            raise ConfigError("BINDING_RESULT_INVALID", path, "Expected matching private Agent binding")
        setting = documents["deployment"]["agents"][name]
        if private["implementation_ref"] != setting["implementation_ref"] or private["backend"] != setting["backend"]:
            raise ConfigError("BINDING_AGENT_MISMATCH", path, "Private Agent binding differs from Deployment")
        evidence = bindings["installation_evidence"].get("agent:" + name)
        if installed == "VERIFIED":
            if (not isinstance(evidence, dict) or evidence.get("descriptor_hash") != digest
                    or canonical_hash(private["descriptor"]) != digest):
                raise ConfigError("BINDING_AGENT_MISMATCH", path, "Installed Agent hash differs from supplied evidence")
        elif private["descriptor"] is not None or evidence is not None:
            raise ConfigError("BINDING_AGENT_MISMATCH", path, "Unavailable Agent has conflicting installation evidence")
    messages = {
        "CONTRACT_ADAPTER_REQUIRED": "Verified legacy Agent requires an unimplemented v1 contract adapter",
        "IMPLEMENTATION_NOT_AVAILABLE": "Declared contract has no installed implementation at this stage",
    }
    if not isinstance(bindings["blockers"], list):
        raise ConfigError("BINDING_RESULT_INVALID", "bindings.blockers", "Expected blocker records")
    for blocker in bindings["blockers"]:
        if (not isinstance(blocker, dict) or set(blocker) != {"code", "kind", "logical_id", "path", "message"}
                or blocker.get("code") not in messages or blocker.get("message") != messages.get(blocker.get("code"))
                or blocker.get("kind") not in {"agent", "source", "notifier", "effect"}
                or not all(isinstance(blocker.get(key), str) for key in ("logical_id", "path"))):
            raise ConfigError("BINDING_RESULT_INVALID", "bindings.blockers", "Unsupported binding blocker record")
        kind, name = blocker["kind"], blocker["logical_id"]
        allowed = {"agent": documents["metadata"]["agents"], "source": documents["site"]["sources"],
                   "notifier": {"notifier": None}, "effect": documents["site"]["effect_targets"]}
        prefix = {"agent": "deployment.agents.", "source": "site.sources.",
                  "notifier": "deployment.notifier_binding", "effect": "site.effect_targets."}[kind]
        expected_path = prefix if kind == "notifier" else prefix + name
        if name not in allowed[kind] or blocker["path"] != expected_path:
            raise ConfigError("BINDING_RESULT_INVALID", "bindings.blockers", "Invalid logical blocker location")


def compile_definition(documents, bindings, *, schema, compiler_pin, schema_pin):
    """Compile JSON inputs only; caller owns all file/installation inspection.

    Configuration hashes are semantic, not source-byte hashes. Source evidence
    is returned by the loader separately. This is not an Episode Run Manifest.
    """
    validate_documents(documents, schema)
    _validate_bindings(documents, bindings, schema)
    for key, value in (("compiler_pin", compiler_pin), ("schema_pin", schema_pin)):
        if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ConfigError("PIN_INVALID", key, "Expected a SHA-256 implementation pin")
    c, site, safety, dep, meta = (documents[n] for n in DOCUMENT_SCHEMAS)
    milestones = {}
    for name, value in c["milestones"].items():
        milestones[name] = copy.deepcopy(value)
        if "when" in value:
            milestones[name]["when"] = compile_expression(value["when"])
        else:
            milestones[name]["capture_first"] = compile_operand(value["capture_first"])
    requirements = copy.deepcopy(safety["requirements"])
    for value in requirements.values():
        if value["kind"] == "predicate":
            value["when"] = compile_expression(value["when"])
    work_rules = [{"id": name, **copy.deepcopy(rule),
                   "when": compile_expression(rule["when"]),
                   "stop_when": compile_expression(rule["stop_when"])}
                  for name, rule in sorted(c["work_rules"].items())]
    timers = copy.deepcopy(c["timers"])
    for value in timers.values():
        value["cancel_when"] = compile_expression(value["cancel_when"])
    lifecycle = copy.deepcopy(c["lifecycle"])
    lifecycle["projection"] = _projection(c["lifecycle"]["projection"])
    tasks = copy.deepcopy(c["tasks"])
    for value in tasks.values():
        if "subject_ref" in value["identity"]:
            value["identity"]["subject_ref"] = compile_operand({"ref": value["identity"]["subject_ref"]})
    public = {
        "definition_schema": DEFINITION_SCHEMA,
        "contract_version": c["contract_version"],
        "spec_id": c["spec_id"], "version": c["version"], "profile": c["profile"],
        "site_id": site["site_id"], "context_service": copy.deepcopy(c["context_service"]),
        "origin_refs": copy.deepcopy(c["origin_refs"]),
        "facts": copy.deepcopy(c["facts"]), "contexts": copy.deepcopy(c["contexts"]),
        "milestones": milestones, "states": _states(c, meta),
        "state_projection": _projection(c["state_projection"]),
        "tasks": tasks, "work_rules": work_rules, "decisions": copy.deepcopy(c["decisions"]),
        "timers": timers, "lifecycle": lifecycle,
        "policy": {"policy_id": safety["policy_id"], "version": safety["version"],
                   "purpose": safety["purpose"], "requirements": requirements,
                   **{key: copy.deepcopy(safety[key]) for key in
                      ("agent_approvals", "query_approvals", "forbidden_actions",
                       "notification_templates", "allowed_effect_operations")}},
        "modules": copy.deepcopy(bindings["public_agents"]),
        "source_coverage": {source: sorted(fact for fact, refs in site["fact_bindings"].items()
                            if any(ref["source"] == source for ref in refs))
                            for source in sorted(site["sources"])},
        "execution": {key: copy.deepcopy(dep[key]) for key in ("activity", "preparation", "reconciliation")},
        "runtime_ready": False,
    }
    worker = {"site": copy.deepcopy(site), "deployment": copy.deepcopy(dep),
              "bindings": copy.deepcopy(bindings["worker_bindings"]),
              "installation_evidence": copy.deepcopy(bindings["installation_evidence"])}
    blockers = sorted(copy.deepcopy(bindings["blockers"]),
                      key=lambda value: canonical_bytes(value))
    manifest = {
        "manifest_schema": MANIFEST_SCHEMA,
        "compiler": {"version": COMPILER_VERSION, "implementation_hash": compiler_pin},
        "normalization_version": NORMALIZATION_VERSION,
        "authoring_schema_hash": schema_pin,
        "documents": {name: {"schema": doc["schema"], **({"version": doc["version"]} if "version" in doc else {}),
                             "semantic_hash": canonical_hash(doc)}
                      for name, doc in documents.items()},
        "definition_hash": canonical_hash(public), "worker_binding_hash": canonical_hash(worker),
        "installation_hash": canonical_hash(bindings["installation_evidence"]),
        "installed_modules": {name: {key: copy.deepcopy(value[key]) for key in
                              ("version", "descriptor_hash", "metadata_hash")}
                              for name, value in bindings["installation_evidence"].items()},
        "binding_blockers": blockers,
        "bindings_validated": not blockers, "runtime_ready": False,
        "runtime_blockers": ["FACT_REDUCER_AND_EXECUTION_NOT_IMPLEMENTED"],
    }
    manifest["configuration_hash"] = canonical_hash(manifest)
    return normalized({"public_definition": public, "manifest": manifest, "worker": worker})
