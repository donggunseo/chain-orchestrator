"""Pure, versioned start and Worker-binding contracts for the Fact Workflow.

Configuration compilation remains an inspection artifact with runtime_ready
False. A start envelope admits only complete native bindings and pins the
Episode, definition, private Worker configuration and implementation together.
Hash checks detect changes; they do not authenticate a caller or issue module
approval. File inspection/import belongs to the Worker, never this module.

engine_run_id is also the Temporal Workflow ID. The Workflow checks that ID
against Temporal execution metadata. Client/Worker binding enforces the exact
configuration-specific Task Queue; SDK Replay uses its own synthetic queue.
"""
from __future__ import annotations

import copy
import re

from .compiler import canonical_hash
from .errors import ConfigError
from .runtime_contracts import IDENTITY_FIELDS, validate_identity, validate_initial
from .shape import check_json


WORKFLOW_TYPE = "ChainFactEpisodeV1"
START_SCHEMA = "chain-fact-start/v1"
RUN_SCHEMA = "chain-fact-run/v1"
CONTRACT_VERSION = "chain-fact-execution/v1"
SIGNALS = ("submit_event", "submit_decision", "request_hitl_resume", "source_reconnected")
QUERIES = ("snapshot", "changes_since", "progress", "hitl_view")
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MANIFEST_FIELDS = {"manifest_schema", "compiler", "normalization_version", "authoring_schema_hash",
    "documents", "definition_hash", "worker_binding_hash", "installation_hash", "installed_modules",
    "binding_blockers", "bindings_validated", "runtime_ready", "runtime_blockers", "configuration_hash"}
_PUBLIC_FIELDS = {"definition_schema", "contract_version", "spec_id", "version", "profile", "site_id",
    "context_service", "origin_refs", "facts", "contexts", "milestones", "states", "state_projection",
    "tasks", "work_rules", "decisions", "timers", "lifecycle", "policy", "modules", "source_coverage",
    "execution", "runtime_ready"}
_RUN_FIELDS = {"manifest_schema", "contract_version", "workflow_type", "task_queue", "engine_run_id",
    "identity", "initial_hash", "configuration_hash", "definition_hash", "worker_binding_hash",
    "installation_hash", "implementation_hash", "source_systems", "run_pin"}


def _fail(code, path, message):
    raise ConfigError(code, path, message) from None


def _closed(value, fields, path, *, optional=()):
    if not isinstance(value, dict) or set(value) - set(fields) - set(optional) or set(fields) - set(value):
        _fail("EXECUTION_CONTRACT_INVALID", path, "Missing or unsupported execution fields")


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        _fail("EXECUTION_CONTRACT_INVALID", path, "Expected a nonempty string")


def _hash(value, path):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _fail("EXECUTION_PIN_INVALID", path, "Expected a SHA-256 content hash")


def _configuration_manifest(manifest):
    _closed(manifest, _MANIFEST_FIELDS, "configuration_manifest")
    if (manifest["manifest_schema"] != "chain-fact-configuration/v1"
            or manifest["normalization_version"] != "chain-json-map-order/v1"):
        _fail("EXECUTION_VERSION_UNSUPPORTED", "configuration_manifest", "Unsupported configuration contract")
    _closed(manifest["compiler"], {"version", "implementation_hash"}, "configuration_manifest.compiler")
    if manifest["compiler"]["version"] != "chain-fact-compiler/v1.0.0":
        _fail("EXECUTION_VERSION_UNSUPPORTED", "configuration_manifest.compiler", "Unsupported compiler contract")
    for field in ("authoring_schema_hash", "definition_hash", "worker_binding_hash", "installation_hash", "configuration_hash"):
        _hash(manifest[field], "configuration_manifest." + field)
    _hash(manifest["compiler"]["implementation_hash"], "configuration_manifest.compiler.implementation_hash")
    if manifest["configuration_hash"] != canonical_hash({k:v for k,v in manifest.items() if k != "configuration_hash"}):
        _fail("CONFIGURATION_HASH_MISMATCH", "configuration_manifest.configuration_hash", "Configuration content differs from its pin")
    if manifest["bindings_validated"] is not True or manifest["binding_blockers"] != []:
        _fail("EXECUTION_BINDINGS_NOT_READY", "configuration_manifest.binding_blockers", "Native bindings must be complete and independently approved")
    if manifest["runtime_ready"] is not False or manifest["runtime_blockers"] != ["FACT_REDUCER_AND_EXECUTION_NOT_IMPLEMENTED"]:
        _fail("EXECUTION_CONTRACT_INVALID", "configuration_manifest.runtime_ready", "Expected an unchanged configuration-inspection artifact")
    schemas = {"clinical":"chain-clinical/v1", "site":"chain-site/v1", "safety":"chain-safety/v1",
               "deployment":"chain-deployment/v1", "metadata":"chain-module-metadata/v1"}
    _closed(manifest["documents"], schemas, "configuration_manifest.documents")
    for name, schema in schemas.items():
        entry = manifest["documents"][name]
        _closed(entry, {"schema", "semantic_hash"}, "configuration_manifest.documents."+name, optional={"version"})
        if entry["schema"] != schema:
            _fail("EXECUTION_VERSION_UNSUPPORTED", "configuration_manifest.documents."+name, "Unsupported authoring contract")
        _hash(entry["semantic_hash"], "configuration_manifest.documents."+name)
        if "version" in entry:
            _text(entry["version"], "configuration_manifest.documents."+name+".version")
    if not isinstance(manifest["installed_modules"], dict):
        _fail("EXECUTION_CONTRACT_INVALID", "configuration_manifest.installed_modules", "Expected pinned module evidence")
    for name, entry in manifest["installed_modules"].items():
        _text(name, "configuration_manifest.installed_modules")
        _closed(entry, {"version", "descriptor_hash", "metadata_hash"}, "configuration_manifest.installed_modules."+name)
        _text(entry["version"], "configuration_manifest.installed_modules."+name+".version")
        for key in ("descriptor_hash", "metadata_hash"):
            _hash(entry[key], "configuration_manifest.installed_modules."+name+"."+key)


def _public_definition(definition, manifest):
    _closed(definition, _PUBLIC_FIELDS, "public_definition")
    if definition["definition_schema"] != "chain-fact-definition/v1" or definition["contract_version"] != CONTRACT_VERSION:
        _fail("EXECUTION_VERSION_UNSUPPORTED", "public_definition", "Unsupported native Fact definition")
    if definition["runtime_ready"] is not False:
        _fail("EXECUTION_CONTRACT_INVALID", "public_definition.runtime_ready", "Do not rewrite configuration inspection status")
    if canonical_hash(definition) != manifest["definition_hash"]:
        _fail("DEFINITION_HASH_MISMATCH", "public_definition", "Definition differs from its configuration pin")
    _text(definition["site_id"], "public_definition.site_id")
    if not isinstance(definition["modules"], dict) or not isinstance(definition["source_coverage"], dict):
        _fail("EXECUTION_CONTRACT_INVALID", "public_definition", "Expected explicit modules and source coverage")
    for name, spec in definition["modules"].items():
        if (not isinstance(spec, dict) or spec.get("installation_state") != "VERIFIED"
                or spec.get("request_contract") != "chain-fact-agent-request/v1"
                or spec.get("result_contract") != "chain-fact-agent-result/v1"):
            _fail("EXECUTION_BINDINGS_NOT_READY", "public_definition.modules."+name, "An approved native Agent contract is required")
        evidence = manifest["installed_modules"].get("agent:"+name)
        if evidence is None or spec.get("implementation_hash") != evidence["descriptor_hash"]:
            _fail("EXECUTION_MODULE_MISMATCH", "public_definition.modules."+name, "Agent metadata differs from installed evidence")


def queue_for_configuration(configuration):
    """Return the dedicated native queue for this exact configuration hash."""
    manifest = configuration.get("manifest") if isinstance(configuration, dict) else None
    if not isinstance(manifest, dict):
        _fail("EXECUTION_CONTRACT_INVALID", "configuration", "A compiled native configuration is required")
    checksum = manifest.get("configuration_hash")
    _hash(checksum, "configuration.manifest.configuration_hash")
    return "chain-fact-v1-" + checksum[len("sha256:"):][:16]


def _run_manifest(manifest):
    _closed(manifest, _RUN_FIELDS, "run_manifest")
    if (manifest["manifest_schema"] != RUN_SCHEMA or manifest["contract_version"] != CONTRACT_VERSION
            or manifest["workflow_type"] != WORKFLOW_TYPE):
        _fail("EXECUTION_VERSION_UNSUPPORTED", "run_manifest", "Unsupported Workflow execution contract")
    for key in ("initial_hash", "configuration_hash", "definition_hash", "worker_binding_hash",
                "installation_hash", "implementation_hash", "run_pin"):
        _hash(manifest[key], "run_manifest."+key)
    _text(manifest["engine_run_id"], "run_manifest.engine_run_id")
    validate_identity(manifest["identity"])
    expected_queue = "chain-fact-v1-" + manifest["configuration_hash"][len("sha256:"):][:16]
    if manifest["task_queue"] != expected_queue:
        _fail("EXECUTION_QUEUE_MISMATCH", "run_manifest.task_queue", "The configuration-specific native Task Queue is required")
    if not isinstance(manifest["source_systems"], dict):
        _fail("EXECUTION_CONTRACT_INVALID", "run_manifest.source_systems", "Expected logical source/system bindings")
    for name, system in manifest["source_systems"].items():
        _text(name, "run_manifest.source_systems")
        _text(system, "run_manifest.source_systems."+name)
        if system.startswith("CHAIN_"):
            _fail("SOURCE_MAPPING_INVALID", "run_manifest.source_systems", "External sources cannot claim an internal namespace")
    if manifest["run_pin"] != canonical_hash({k:v for k,v in manifest.items() if k != "run_pin"}):
        _fail("EXECUTION_RUN_PIN_MISMATCH", "run_manifest.run_pin", "Run content differs from its pin")


def _validate_configuration(configuration):
    check_json(configuration, path="configuration")
    _closed(configuration, {"public_definition", "manifest", "worker"}, "configuration", optional={"artifact_evidence"})
    manifest = configuration["manifest"]
    _configuration_manifest(manifest)
    _public_definition(configuration["public_definition"], manifest)
    worker = configuration["worker"]
    _closed(worker, {"site", "deployment", "bindings", "installation_evidence"}, "configuration.worker")
    _closed(worker["bindings"], {"agents", "sources", "notifier", "effects"}, "configuration.worker.bindings")
    if not isinstance(worker["installation_evidence"], dict):
        _fail("EXECUTION_CONTRACT_INVALID", "configuration.worker.installation_evidence", "Expected installed module evidence")
    if canonical_hash(worker) != manifest["worker_binding_hash"]:
        _fail("WORKER_BINDING_HASH_MISMATCH", "configuration.worker", "Worker bindings differ from their configuration pin")
    if canonical_hash(worker["installation_evidence"]) != manifest["installation_hash"]:
        _fail("INSTALLATION_HASH_MISMATCH", "configuration.worker.installation_evidence", "Installation evidence differs from its pin")
    modules = {}
    for name, value in worker["installation_evidence"].items():
        _closed(value, {"implementation_ref", "descriptor_hash", "metadata_hash", "version", "status", "approval_ref", "files"},
                "configuration.worker.installation_evidence."+name)
        modules[name] = {key:value[key] for key in ("version", "descriptor_hash", "metadata_hash")}
    if modules != manifest["installed_modules"]:
        _fail("EXECUTION_MODULE_MISMATCH", "configuration.worker.installation_evidence", "Public and private installation evidence differ")
    bindings = worker["bindings"]
    for kind, table in (("agent", bindings["agents"]), ("source", bindings["sources"]), ("effect", bindings["effects"])):
        if not isinstance(table, dict):
            _fail("EXECUTION_CONTRACT_INVALID", "configuration.worker.bindings", "Expected logical module tables")
        for name, binding in table.items():
            _binding_evidence(kind, name, binding, worker["installation_evidence"])
    if bindings["notifier"] is not None:
        _binding_evidence("notifier", "notifier", bindings["notifier"], worker["installation_evidence"])
    if set(bindings["agents"]) != set(configuration["public_definition"]["modules"]):
        _fail("EXECUTION_MODULE_MISMATCH", "configuration.worker.bindings.agents", "Public and private Agent sets differ")
    sources = bindings["sources"]
    if set(sources) != set(configuration["public_definition"]["source_coverage"]):
        _fail("SOURCE_MAPPING_INVALID", "configuration.worker.bindings.sources", "Source coverage and approved bindings must match")
    return {name:entry["system"] for name,entry in sources.items()}


def _binding_evidence(kind, name, binding, evidence):
    path = "configuration.worker.bindings."+kind+":"+name
    fields = {"agent":{"backend"}, "source":{"system", "fields"}, "notifier":{"recipients"}, "effect":set()}[kind]
    _closed(binding, {"implementation_ref", "descriptor"} | fields, path)
    descriptor = binding["descriptor"]
    _closed(descriptor, {"kind", "version", "entrypoint", "files", "metadata", "status", "approval_ref", "capabilities"}, path+".descriptor")
    if descriptor["kind"] != kind or descriptor["status"] != "APPROVED":
        _fail("EXECUTION_BINDINGS_NOT_READY", path, "An independently approved native implementation is required")
    _text(descriptor["approval_ref"], path+".approval_ref")
    expected = {"implementation_ref":binding["implementation_ref"], "descriptor_hash":canonical_hash(descriptor),
        "metadata_hash":canonical_hash(descriptor["metadata"]), "version":descriptor["version"],
        "status":descriptor["status"], "approval_ref":descriptor["approval_ref"], "files":descriptor["files"]}
    if evidence.get(kind+":"+name) != expected:
        _fail("EXECUTION_MODULE_MISMATCH", path, "Private descriptor differs from approved installation evidence")
    metadata = descriptor["metadata"]
    if not isinstance(metadata, dict):
        _fail("EXECUTION_CONTRACT_INVALID", path+".metadata", "Expected provider metadata")
    if kind == "agent":
        contract = metadata.get("contract")
        if (metadata.get("agent_id") != name or metadata.get("backend") != binding["backend"]
                or not isinstance(contract, dict) or contract.get("request_contract") != "chain-fact-agent-request/v1"
                or contract.get("result_contract") != "chain-fact-agent-result/v1"):
            _fail("EXECUTION_BINDINGS_NOT_READY", path, "A matching native Agent contract is required")
    elif metadata.get("contract_version") != CONTRACT_VERSION:
        _fail("EXECUTION_BINDINGS_NOT_READY", path, "A matching native module contract is required")


def build_start(configuration, initial, engine_run_id, task_queue=None):
    """Bind a strict native configuration and initial input without any I/O."""
    sources = _validate_configuration(configuration)
    initial = validate_initial(initial)
    manifest = configuration["manifest"]
    expected_queue = queue_for_configuration(configuration)
    if task_queue is not None and task_queue != expected_queue:
        _fail("EXECUTION_QUEUE_MISMATCH", "task_queue", "The configuration-specific native Task Queue is required")
    run = {"manifest_schema":RUN_SCHEMA, "contract_version":CONTRACT_VERSION,
        "workflow_type":WORKFLOW_TYPE, "task_queue":expected_queue, "engine_run_id":engine_run_id,
        "identity":{key:initial[key] for key in sorted(IDENTITY_FIELDS)}, "initial_hash":canonical_hash(initial),
        "configuration_hash":manifest["configuration_hash"], "definition_hash":manifest["definition_hash"],
        "worker_binding_hash":manifest["worker_binding_hash"], "installation_hash":manifest["installation_hash"],
        "implementation_hash":manifest["compiler"]["implementation_hash"], "source_systems":sources}
    run["run_pin"] = canonical_hash(run)
    return validate_start({"contract_schema":START_SCHEMA, "initial":initial,
        "public_definition":copy.deepcopy(configuration["public_definition"]),
        "configuration_manifest":copy.deepcopy(manifest), "run_manifest":run})


def validate_start(value):
    """Validate pinned public inputs. Worker file/binding verification is separate."""
    check_json(value, path="start")
    _closed(value, {"contract_schema", "initial", "public_definition", "configuration_manifest", "run_manifest"}, "start")
    if value["contract_schema"] != START_SCHEMA:
        _fail("EXECUTION_VERSION_UNSUPPORTED", "start.contract_schema", "Unsupported start contract")
    initial = validate_initial(value["initial"])
    config, run, definition = value["configuration_manifest"], value["run_manifest"], value["public_definition"]
    _configuration_manifest(config)
    _public_definition(definition, config)
    _run_manifest(run)
    if run["identity"] != {key:initial[key] for key in IDENTITY_FIELDS} or definition["site_id"] != initial["site_id"]:
        _fail("IDENTITY_MISMATCH", "start", "Initial, definition and run identities differ")
    if run["initial_hash"] != canonical_hash(initial):
        _fail("EXECUTION_INITIAL_PIN_MISMATCH", "start.initial", "Initial data differs from the run pin")
    for key in ("configuration_hash", "definition_hash", "worker_binding_hash", "installation_hash"):
        if run[key] != config[key]:
            _fail("EXECUTION_CONFIGURATION_MISMATCH", "run_manifest."+key, "Run and configuration pins differ")
    if run["implementation_hash"] != config["compiler"]["implementation_hash"]:
        _fail("EXECUTION_CONFIGURATION_MISMATCH", "run_manifest.implementation_hash", "Run implementation differs from the compiler pin")
    if set(run["source_systems"]) != set(definition["source_coverage"]):
        _fail("SOURCE_MAPPING_INVALID", "run_manifest.source_systems", "Run sources differ from declared source coverage")
    return copy.deepcopy(value)


def validate_worker_manifest(run_manifest, configuration):
    """Match an Activity's run to the Worker's frozen trusted configuration.

    The Worker separately verifies current files, factory metadata and provider
    contracts before invoking a provider. This helper performs no imports/I/O.
    """
    check_json(run_manifest, path="run_manifest")
    _run_manifest(run_manifest)
    sources = _validate_configuration(configuration)
    config = configuration["manifest"]
    for key in ("configuration_hash", "definition_hash", "worker_binding_hash", "installation_hash"):
        if run_manifest[key] != config[key]:
            _fail("EXECUTION_CONFIGURATION_MISMATCH", "run_manifest."+key, "Worker cannot execute another configuration's run")
    if (run_manifest["implementation_hash"] != config["compiler"]["implementation_hash"]
            or run_manifest["source_systems"] != sources
            or run_manifest["identity"]["site_id"] != configuration["public_definition"]["site_id"]):
        _fail("EXECUTION_CONFIGURATION_MISMATCH", "run_manifest", "Worker implementation, source or site binding differs")
    return copy.deepcopy(run_manifest)


def validate_worker_binding(start, configuration):
    """Validate a full start and its matching trusted Worker configuration."""
    start = validate_start(start)
    validate_worker_manifest(start["run_manifest"], configuration)
    if (start["configuration_manifest"] != configuration["manifest"]
            or start["public_definition"] != configuration["public_definition"]):
        _fail("EXECUTION_CONFIGURATION_MISMATCH", "start", "Worker and Workflow public configuration differ")
    return start
