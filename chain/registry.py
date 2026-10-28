"""Worker/client-only installation inspection; never imports or invokes plugins.

``resolve_bindings`` accepts the five validated authoring documents (clinical,
site, safety, deployment, metadata). Its optional trusted installation inventory
is closed: {schema: 'chain-installed/v1', modules: {implementation_ref: descriptor}}.
Only selected descriptors are inspected; unused extensions need not be installed.

A descriptor has exactly kind, version, entrypoint, files, metadata, status,
approval_ref, capabilities. kind is agent/source/notifier/effect; files maps
root-relative paths to SHA-256 byte hashes; entrypoint is module:function.
status must be explicitly APPROVED and approval_ref must identify supplied
approval information. Hash generation never issues an approval or verifies a
cryptographic signature. The inventory is a trusted Worker deployment input,
not a Clinical document or arbitrary module discovery mechanism.

Kind-specific metadata is also closed:
* agent: {agent_id, contract, backend}; contract has the seven fields of
  authoring metadata.agents[agent_id]; backend matches deployment backend.
* source: {contract_version, systems, fields, data_scopes}.
* notifier: {contract_version, templates, channels}.
* effect: {contract_version, operations, resources, targets}.
Each non-agent contract_version is chain-fact-execution/v1. Required capabilities
are invoke_agent, query_latest/resolve_exact/reconcile_changes, notify, and
apply_effect respectively. Capabilities are declarations, not execution tests.

Historical legacy-registry: references are explicitly unsupported. They belong
to their preserved v0.3 configuration and never trigger an old loader here.
Contract placeholders remain explicitly unimplemented. Validation-only
permits these known gaps as blockers, but never accepts bad registered bytes,
metadata, scope, version or approval. Strict resolution rejects such gaps.

Public output contains logical IDs, contracts and hashes only. Entry points,
backend paths, inventory references and full installation evidence stay private.
Even complete native bindings do not create a Worker: phase-2 runtime_ready is
always False. No source/API/Activity or provider code is executed here.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
import re

from .errors import ConfigError


CONTRACT_VERSION = "chain-fact-execution/v1"
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REF = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]*\Z")
_ENTRY = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*\Z")
_SPEC_FIELDS = {"version", "request_contract", "result_contract", "modes",
                "requires_summary", "actions", "data_scopes"}
_SET_FIELDS = {"capabilities", "output_fields", "actions", "data_scopes", "systems",
               "fields", "templates", "channels", "operations", "resources", "targets"}


def _fail(code, path, message):
    # Parser/SyntaxError contexts may contain credentials or source text.
    raise ConfigError(code, path, message) from None


def _shape(value, required, path, optional=()):
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        _fail("INSTALLATION_SCHEMA_INVALID", path, "Expected an object with string keys")
    if set(value) != set(required) | (set(value) & set(optional)):
        _fail("INSTALLATION_SCHEMA_INVALID", path, "Missing or unsupported installation fields")
    return value


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        _fail("INSTALLATION_SCHEMA_INVALID", path, "Expected a nonempty string")
    return value


def _strings(value, path, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        _fail("INSTALLATION_SCHEMA_INVALID", path, "Expected a string list")
    for item in value:
        _text(item, path)
    if len(value) != len(set(value)):
        _fail("INSTALLATION_SCHEMA_INVALID", path, "Duplicate set member")
    return set(value)


def _normalized(value, key=None):
    if isinstance(value, dict):
        return {k: _normalized(v, k) for k, v in value.items()}
    if isinstance(value, list):
        result = [_normalized(v) for v in value]
        return sorted(result) if key in _SET_FIELDS else result
    return value


def _hash(value):
    raw = json.dumps(_normalized(value), sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf8")).hexdigest()


def _document_hash(documents):
    """Bind this resolution to the exact JSON documents, preserving list order."""
    raw = json.dumps(documents, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf8")).hexdigest()


def _agent_spec(spec, path):
    _shape(spec, _SPEC_FIELDS, path)
    for key in ("version", "request_contract", "result_contract"):
        _text(spec[key], path + "." + key)
    if type(spec["requires_summary"]) is not bool:
        _fail("INSTALLATION_SCHEMA_INVALID", path + ".requires_summary", "Expected a boolean")
    _strings(spec["actions"], path + ".actions")
    _strings(spec["data_scopes"], path + ".data_scopes")
    if not isinstance(spec["modes"], dict) or not spec["modes"]:
        _fail("INSTALLATION_SCHEMA_INVALID", path + ".modes", "Expected declared modes")
    for mode, setting in spec["modes"].items():
        _text(mode, path + ".modes")
        _shape(setting, {"output_fields"}, path + ".modes." + mode)
        _strings(setting["output_fields"], path + ".modes." + mode, nonempty=True)
    return _normalized(spec)


def _backend(value, path):
    _shape(value, {"kind"}, path, {"config_ref"})
    if value["kind"] not in {"fixture", "structured", "api"}:
        _fail("BACKEND_MISMATCH", path, "Unsupported Agent backend")
    if "config_ref" in value:
        _text(value["config_ref"], path + ".config_ref")
    if value["kind"] == "fixture" and "config_ref" not in value:
        _fail("BACKEND_MISMATCH", path, "Fixture backend requires an installed configuration")
    if value["kind"] == "structured" and "config_ref" in value:
        _fail("BACKEND_MISMATCH", path, "Structured backend does not read fixture configuration")
    return copy.deepcopy(value)


def _inspect_descriptor(descriptor, root, kind, path):
    _shape(descriptor, {"kind", "version", "entrypoint", "files", "metadata", "status",
                        "approval_ref", "capabilities"}, path)
    if descriptor["kind"] != kind:
        _fail("IMPLEMENTATION_KIND_MISMATCH", path, "Installed module has a different kind")
    _text(descriptor["version"], path + ".version")
    if descriptor["status"] != "APPROVED":
        _fail("IMPLEMENTATION_NOT_APPROVED", path, "Selected installation is not approved")
    _text(descriptor["approval_ref"], path + ".approval_ref")
    caps = _strings(descriptor["capabilities"], path + ".capabilities")
    expected = {"agent": {"invoke_agent"},
                "source": {"query_latest", "resolve_exact", "reconcile_changes"},
                "notifier": {"notify"}, "effect": {"apply_effect"}}[kind]
    if not expected <= caps:
        _fail("CAPABILITY_MISMATCH", path, "Selected module lacks a required capability")
    entrypoint = _text(descriptor["entrypoint"], path + ".entrypoint")
    if not _ENTRY.fullmatch(entrypoint):
        _fail("ENTRYPOINT_INVALID", path, "Entrypoint must be module:function")
    files = descriptor["files"]
    if not isinstance(files, dict) or not files:
        _fail("INSTALLATION_SCHEMA_INVALID", path + ".files", "Installed file hashes are required")
    for name, checksum in files.items():
        _text(name, path + ".files")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            _fail("INSTALLATION_PATH_INVALID", path, "Installed file escapes its root")
        if not isinstance(checksum, str) or not _HASH.fullmatch(checksum):
            _fail("INSTALLATION_SCHEMA_INVALID", path, "Expected an installed SHA-256 byte hash")
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            _fail("INSTALLATION_FILE_MISSING", path, "Installed file is missing or outside its root")
        try:
            actual = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            _fail("INSTALLATION_FILE_UNREADABLE", path, "Cannot read a selected installed file")
        if actual != checksum:
            _fail("INSTALLATION_HASH_MISMATCH", path, "Installed bytes differ from the supplied approval inventory")
    module, function = entrypoint.split(":")
    filename = module.replace(".", "/") + ".py"
    if filename not in files:
        _fail("ENTRYPOINT_NOT_PINNED", path, "Entrypoint file is absent from the installation manifest")
    try:
        tree = ast.parse((root / filename).read_text(encoding="utf8"))
    except (OSError, UnicodeError, SyntaxError):
        _fail("ENTRYPOINT_INVALID", path, "Entrypoint source cannot be inspected")
    if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function
               for node in tree.body):
        _fail("ENTRYPOINT_NOT_DECLARED", path, "Entrypoint function is not declared")
    metadata = descriptor["metadata"]
    if kind == "agent":
        _shape(metadata, {"agent_id", "contract", "backend"}, path + ".metadata")
        _text(metadata["agent_id"], path + ".metadata.agent_id")
        _agent_spec(metadata["contract"], path + ".metadata.contract")
        _backend(metadata["backend"], path + ".metadata.backend")
        if metadata["backend"]["kind"] == "fixture" and metadata["backend"]["config_ref"] not in files:
            _fail("BACKEND_NOT_PINNED", path, "Fixture configuration is absent from the installed files")
    else:
        keys = {"source": {"systems", "fields", "data_scopes"},
                "notifier": {"templates", "channels"},
                "effect": {"operations", "resources", "targets"}}[kind]
        _shape(metadata, keys | {"contract_version"}, path + ".metadata")
        if metadata["contract_version"] != CONTRACT_VERSION:
            _fail("MODULE_CONTRACT_MISMATCH", path, "Unsupported installed contract version")
        for key in keys:
            _strings(metadata[key], path + ".metadata." + key, nonempty=True)
    return _normalized(descriptor)


def _selected_tasks(clinical):
    tasks = clinical["tasks"]
    selected = {r["task"] for r in clinical["work_rules"].values()}
    selected.update(t["on_expire"]["id"] for t in clinical["timers"].values()
                    if t["on_expire"]["kind"] == "task")
    pending = list(selected)
    while pending:
        name = pending.pop()
        if name not in tasks:
            _fail("BINDING_TASK_UNDEFINED", "clinical.tasks", "Selected task is undefined")
        for predecessor in tasks[name]["after"]:
            if predecessor not in selected:
                selected.add(predecessor)
                pending.append(predecessor)
    return sorted(selected)


def resolve_bindings(documents, *, installation_root, installations=None, validation_only=True):
    """Validate selected installations and return public/private configuration.

    The caller validates authoring Schema and general references first. This
    boundary independently checks selected provider metadata, capabilities and
    approval intersections. ``installations`` is trusted deployment inventory;
    omitted inventory contains no installed modules. Historical bridge
    references are rejected without reading archived files.
    """
    if type(validation_only) is not bool:
        _fail("BINDING_MODE_INVALID", "validation_only", "Expected an explicit boolean")
    root = Path(installation_root).resolve()
    inventory = installations if installations is not None else {"schema": "chain-installed/v1", "modules": {}}
    _shape(inventory, {"schema", "modules"}, "installations")
    if inventory["schema"] != "chain-installed/v1" or not isinstance(inventory["modules"], dict):
        _fail("INSTALLATION_SCHEMA_INVALID", "installations", "Unsupported installation inventory")
    c, site, safety, deployment, metadata = (documents[n] for n in ("clinical", "site", "safety", "deployment", "metadata"))
    public = {}
    worker = {"agents": {}, "sources": {}, "notifier": None, "effects": {}}
    evidence = {}
    blockers = []
    inspected = {}

    def gap(code, kind, logical_id, path, message):
        if not validation_only:
            _fail(code, path, message)
        blockers.append({"code": code, "kind": kind, "logical_id": logical_id,
                         "path": path, "message": message})

    def installed(reference, kind, logical_id, path):
        if not isinstance(reference, str) or not _REF.fullmatch(reference):
            _fail("IMPLEMENTATION_REF_INVALID", path, "Expected a registered logical implementation reference")
        cache_key = (reference, kind)
        if cache_key in inspected:
            return inspected[cache_key]
        if reference.startswith("legacy-registry:"):
            _fail("LEGACY_IMPLEMENTATION_UNSUPPORTED", path,
                "Historical registry references require their preserved v0.3 artifact")
        if reference in inventory["modules"]:
            descriptor = _inspect_descriptor(inventory["modules"][reference], root, kind, path)
        elif (reference.startswith(("contract-only:", "published-source-contract:"))
              or reference in {"mock-notifier-contract", "mock-effects-contract"}):
            gap("IMPLEMENTATION_NOT_AVAILABLE", kind, logical_id, path,
                "Declared contract has no installed implementation at this stage")
            return None
        else:
            _fail("IMPLEMENTATION_NOT_INSTALLED", path, "Selected implementation is not registered")
        inspected[cache_key] = descriptor
        return descriptor

    def record(kind, logical_id, reference, descriptor):
        key = kind + ":" + logical_id
        evidence[key] = {"implementation_ref": reference, "descriptor_hash": _hash(descriptor),
                         "metadata_hash": _hash(descriptor["metadata"]), "version": descriptor["version"],
                         "status": descriptor["status"], "approval_ref": descriptor["approval_ref"],
                         "files": copy.deepcopy(descriptor["files"])}

    task_names = _selected_tasks(c)
    agent_ids = {c["tasks"][t]["agent"] for t in task_names if c["tasks"][t]["kind"] == "agent"}
    contexts = set()
    for name in task_names:
        task = c["tasks"][name]
        if "context" in task:
            contexts.add(task["context"])
        elif task["kind"] == "hitl":
            contexts.add(c["decisions"][task["decision"]]["evidence_context"])
    needs_summary = (any(c["contexts"][name]["summary"] for name in contexts)
                     or any(metadata["agents"].get(a, {}).get("requires_summary", False) for a in agent_ids))
    if needs_summary:
        agent_ids.add(c["context_service"]["agent"])

    for agent_id in sorted(agent_ids):
        path = "deployment.agents." + agent_id
        if agent_id not in deployment["agents"] or agent_id not in metadata["agents"]:
            _fail("AGENT_METADATA_MISSING", path, "Selected Agent metadata or deployment is missing")
        requested = _agent_spec(metadata["agents"][agent_id], "metadata.agents." + agent_id)
        pair = (requested["request_contract"], requested["result_contract"])
        if pair not in {("chain-agent-request/v0.3", "chain-agent-result/v0.3"),
                        ("chain-fact-agent-request/v1", "chain-fact-agent-result/v1")}:
            _fail("MODULE_CONTRACT_MISMATCH", path, "Unsupported Agent Request/Result contract pair")
        if needs_summary and agent_id == c["context_service"]["agent"]:
            if requested["requires_summary"]:
                _fail("SUMMARY_CONTRACT_RECURSIVE", path, "Context service cannot require its own Summary")
            if c["context_service"]["mode"] not in requested["modes"]:
                _fail("AGENT_MODE_MISMATCH", path, "Context service mode is not provided")
        setting = deployment["agents"][agent_id]
        approval = safety["agent_approvals"].get(agent_id)
        if (setting["version"] != requested["version"] or not approval
                or requested["version"] not in approval["versions"]):
            _fail("AGENT_VERSION_DENIED", path, "Selected Agent version is not approved")
        if (not set(requested["actions"]) <= set(approval["actions"])
                or set(requested["actions"]) & set(safety["forbidden_actions"])):
            _fail("AGENT_ACTION_DENIED", path, "Agent actions exceed approval or include a forbidden action")
        if not set(requested["data_scopes"]) <= set(approval["data_scopes"]):
            _fail("AGENT_SCOPE_DENIED", path, "Agent data scopes exceed independent approval")
        for name in task_names:
            task = c["tasks"][name]
            if task["kind"] == "agent" and task["agent"] == agent_id and task["mode"] not in requested["modes"]:
                _fail("AGENT_MODE_MISMATCH", path, "Selected task mode is not provided")
        backend = _backend(setting["backend"], path + ".backend")
        reference = setting["implementation_ref"]
        descriptor = installed(reference, "agent", agent_id, path)
        public[agent_id] = {**copy.deepcopy(requested), "installation_state": "UNAVAILABLE",
                            "implementation_hash": None}
        worker["agents"][agent_id] = {"implementation_ref": reference, "backend": backend, "descriptor": descriptor}
        if descriptor is None:
            continue
        provider = descriptor["metadata"]
        if provider["agent_id"] != agent_id or _hash(provider["contract"]) != _hash(requested):
            _fail("AGENT_METADATA_MISMATCH", path, "Declared Agent contract differs from installed provider metadata")
        if provider["backend"] != backend:
            _fail("BACKEND_MISMATCH", path, "Requested backend differs from the installed provider binding")
        record("agent", agent_id, reference, descriptor)
        public[agent_id].update(installation_state="VERIFIED", implementation_hash=_hash(descriptor))
        if pair == ("chain-agent-request/v0.3", "chain-agent-result/v0.3"):
            gap("CONTRACT_ADAPTER_REQUIRED", "agent", agent_id, path,
                "Verified legacy Agent requires an unimplemented v1 contract adapter")

    selected_sources = {}
    for fact, spec in c["facts"].items():
        if spec["source"] != "external":
            continue
        for binding in site["fact_bindings"].get(fact, []):
            selected_sources.setdefault(binding["source"], set()).add(fact)
    for source_id, fields in sorted(selected_sources.items()):
        path = "site.sources." + source_id
        source = site["sources"][source_id]
        approval = safety["query_approvals"].get(source_id)
        if not approval or not fields <= set(approval["fields"]):
            _fail("SOURCE_SCOPE_DENIED", path, "Source fields exceed independent query approval")
        if source["binding"] not in deployment["source_bindings"]:
            _fail("SOURCE_BINDING_MISSING", path, "Selected source binding is not deployed")
        reference = deployment["source_bindings"][source["binding"]]
        descriptor = installed(reference, "source", source_id, path)
        worker["sources"][source_id] = {"implementation_ref": reference, "system": source["system"],
                                        "fields": sorted(fields), "descriptor": descriptor}
        if descriptor is None:
            continue
        provider = descriptor["metadata"]
        if (source["system"] not in provider["systems"] or not fields <= set(provider["fields"])
                or not set(approval["data_scopes"]) <= set(provider["data_scopes"])):
            _fail("SOURCE_METADATA_MISMATCH", path, "Source system, fields or scopes exceed installed capabilities")
        record("source", source_id, reference, descriptor)

    notices = [c["tasks"][n] for n in task_names if c["tasks"][n]["kind"] == "notify"]
    if notices:
        path = "deployment.notifier_binding"
        templates = {t["template"] for t in notices}
        if not templates <= set(safety["notification_templates"]):
            _fail("NOTIFICATION_TEMPLATE_DENIED", path, "A selected notification template is not approved")
        recipients = {t["recipient"]: copy.deepcopy(site["recipients"][t["recipient"]]) for t in notices}
        channels = {r["channel"] for r in recipients.values()}
        reference = deployment["notifier_binding"]
        descriptor = installed(reference, "notifier", "notifier", path)
        worker["notifier"] = {"implementation_ref": reference, "recipients": recipients, "descriptor": descriptor}
        if descriptor is not None:
            provider = descriptor["metadata"]
            if not templates <= set(provider["templates"]) or not channels <= set(provider["channels"]):
                _fail("NOTIFIER_METADATA_MISMATCH", path, "Selected templates or channels are not installed")
            record("notifier", "notifier", reference, descriptor)

    effects = [c["tasks"][n] for n in task_names if c["tasks"][n]["kind"] == "effect"]
    for target in sorted({t["target"] for t in effects}):
        path = "site.effect_targets." + target
        operations = {t["operation"] for t in effects if t["target"] == target}
        resources = {t["resource"] for t in effects if t["target"] == target}
        if not operations <= set(safety["allowed_effect_operations"]):
            _fail("EFFECT_OPERATION_DENIED", path, "Selected effect operation is not approved")
        reference = deployment["effect_binding"]
        if site["effect_targets"][target]["binding"] != reference:
            _fail("EFFECT_BINDING_MISMATCH", path, "Site target and deployed effect binding differ")
        descriptor = installed(reference, "effect", target, path)
        worker["effects"][target] = {"implementation_ref": reference, "descriptor": descriptor}
        if descriptor is not None:
            provider = descriptor["metadata"]
            if (not operations <= set(provider["operations"]) or not resources <= set(provider["resources"])
                    or target not in provider["targets"]):
                _fail("EFFECT_METADATA_MISMATCH", path, "Selected operation, resource or target is not installed")
            record("effect", target, reference, descriptor)
    return {"document_hash": _document_hash(documents),
            "public_agents": _normalized(public), "worker_bindings": _normalized(worker),
            "installation_evidence": _normalized(evidence), "blockers": blockers, "runtime_ready": False}
