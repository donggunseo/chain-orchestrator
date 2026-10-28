"""Pinned native-v1 installation boundary for Workers, never Workflow code.

An approved ``module:function`` is a synchronous factory ``(binding, services)``.
It returns an Agent provider with explicit Schema callbacks, a source exposing
``query_latest/resolve_exact/reconcile_changes``, or a Mock receipt ``record``
provider. ``binding`` is the selected private binding plus kind, logical_id and
implementation_hash. Services are trusted, private Worker dependencies.

Imports, fixture reads and source I/O belong here or to the installed module.
An approval inventory is a trusted deployment input, not a Python sandbox. The
listed files and the core artifact are rechecked before every Activity. Native
providers may depend on installed SDKs; transitive imports are not discovered or
automatically approved. Legacy v0.3 Agent contracts require an explicit adapter
and remain rejected by this path.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
from pathlib import Path
import sys
import types

from .common.contracts import validate_event
from .common.source_contracts import identity, validate_completion

from . import task_contracts
from .compiler import canonical_hash
from .configuration import _compiler_pin
from .errors import ConfigError
from .registry import _hash as descriptor_hash, _inspect_descriptor
from .runtime_contracts import (normalize_completion, validate_preflight_request,
    validate_preflight_result, validate_reconcile_request, validate_reconcile_result,
    _hash as validate_hash)
from .shape import check_json
from .task_runtime import LocalTaskRuntime


def _fail(code, path, message):
    raise ConfigError(code, path, message) from None


class FactWorkerRuntime:
    """One immutable configuration, shared by independently identified Episodes.

    ``provider_factories`` can reuse already imported *pinned* entrypoints; it
    cannot replace them with arbitrary injected callables. Input/output Schema
    callbacks and provider metadata are checked even when such a cache is used.
    Providers are synchronous and must provide their own safe I/O concurrency.
    No lock around an external call serializes otherwise independent Activities.
    """

    def __init__(self, package, installation_root, services=None, provider_factories=None):
        check_json(package, path="configuration")
        self.package = copy.deepcopy(package)
        self.installation_root = Path(installation_root).resolve()
        if services is not None and not isinstance(services, dict):
            _fail("WORKER_SERVICES_INVALID", "services", "Expected a trusted Worker service mapping")
        self.services = {} if services is None else services
        self._factory_cache = {} if provider_factories is None else dict(provider_factories)
        self._providers = {}
        self._verify_configuration()
        for kind, logical_id, binding in self._selected():
            descriptor = binding["descriptor"]
            factory = self._factory(descriptor)
            private = {**copy.deepcopy(binding), "kind": kind, "logical_id": logical_id,
                       "implementation_hash": descriptor_hash(descriptor)}
            try:
                provider = factory(private, self.services)
            except ConfigError:
                raise
            except Exception:
                _fail("MODULE_FACTORY_FAILED", kind + "." + logical_id, "Installed provider factory failed; inspect its private Worker dependencies")
            self._verify_provider(kind, logical_id, binding, provider)
            self._providers[kind, logical_id] = provider
        bindings = self.package["worker"]["bindings"]
        self._runtime = LocalTaskRuntime(
            {name: self._providers["agent", name] for name in bindings["agents"]},
            notifier=self._providers.get(("notifier", "notifier")),
            recipients=(bindings["notifier"] or {}).get("recipients", {}))

    def _selected(self):
        bindings = self.package["worker"]["bindings"]
        for kind, field in (("agent", "agents"), ("source", "sources"), ("effect", "effects")):
            for logical_id, binding in sorted(bindings[field].items()):
                yield kind, logical_id, binding
        if bindings["notifier"] is not None:
            yield "notifier", "notifier", bindings["notifier"]

    def _verify_configuration(self):
        package = self.package
        if not isinstance(package, dict) or not {"manifest", "worker", "public_definition"} <= set(package):
            _fail("WORKER_CONFIGURATION_INVALID", "configuration", "Expected a compiled configuration package")
        from .execution import _validate_configuration
        _validate_configuration(package)
        manifest = package["manifest"]
        if (manifest.get("manifest_schema") != "chain-fact-configuration/v1"
                or manifest.get("configuration_hash") != canonical_hash({k: v for k, v in manifest.items() if k != "configuration_hash"})
                or manifest.get("definition_hash") != canonical_hash(package["public_definition"])
                or manifest.get("worker_binding_hash") != canonical_hash(package["worker"])
                or manifest.get("installation_hash") != canonical_hash(package["worker"]["installation_evidence"])):
            _fail("WORKER_ARTIFACT_MISMATCH", "configuration", "Compiled artifacts differ from their Manifest")
        if manifest.get("binding_blockers") or not manifest.get("bindings_validated"):
            _fail("WORKER_BINDINGS_UNAVAILABLE", "configuration", "Selected native implementations must all be installed")
        if manifest.get("compiler", {}).get("implementation_hash") != _compiler_pin()[0]:
            _fail("WORKER_IMPLEMENTATION_MISMATCH", "configuration.compiler", "Worker code differs from the pinned compilation artifact")
        evidence = package["worker"]["installation_evidence"]
        for kind, logical_id, binding in self._selected():
            descriptor = binding.get("descriptor")
            if descriptor is None:
                _fail("WORKER_BINDINGS_UNAVAILABLE", kind + "." + logical_id, "Selected module has no installed descriptor")
            actual = _inspect_descriptor(descriptor, self.installation_root, kind, kind + "." + logical_id)
            item = evidence.get(kind + ":" + logical_id, {})
            if (descriptor_hash(actual) != item.get("descriptor_hash")
                    or descriptor_hash(actual["metadata"]) != item.get("metadata_hash")
                    or actual["files"] != item.get("files")
                    or binding["implementation_ref"] != item.get("implementation_ref")):
                _fail("WORKER_INSTALLATION_MISMATCH", kind + "." + logical_id, "Installed descriptor differs from recorded approval evidence")

    def _factory(self, descriptor):
        entrypoint = descriptor["entrypoint"]
        module_name, function_name = entrypoint.split(":")
        filename = module_name.replace(".", "/") + ".py"
        path = self.installation_root / filename
        data = path.read_bytes()
        if "sha256:" + hashlib.sha256(data).hexdigest() != descriptor["files"][filename]:
            _fail("INSTALLATION_HASH_MISMATCH", "entrypoint", "Installed entrypoint changed before import")
        cached = self._factory_cache.get(entrypoint)
        if cached is not None:
            code = getattr(cached, "__code__", None)
            if (not callable(cached) or code is None or Path(code.co_filename).resolve() != path.resolve()
                    or getattr(cached, "__name__", None) != function_name):
                _fail("UNPINNED_FACTORY_OVERRIDE", "entrypoint", "Factory cache must reference the pinned installed entrypoint")
            factory = cached
        else:
            # Read and execute the verified bytes, avoiding stale .pyc and a
            # similarly named provider from another installation directory.
            unique = "_chain_installed_" + hashlib.sha256(str(path).encode("utf8") + data).hexdigest()
            module = types.ModuleType(unique)
            module.__file__ = str(path)
            module.__package__ = module_name.rpartition(".")[0]
            sys.modules[unique] = module
            try:
                exec(compile(data, str(path), "exec"), module.__dict__)
                factory = module.__dict__.get(function_name)
            except Exception:
                sys.modules.pop(unique, None)
                _fail("MODULE_IMPORT_FAILED", "entrypoint", "Installed provider module could not be imported")
        if not callable(factory) or inspect.iscoroutinefunction(factory):
            _fail("MODULE_FACTORY_INVALID", "entrypoint", "A synchronous installed provider factory is required")
        self._factory_cache[entrypoint] = factory
        return factory

    def _verify_provider(self, kind, logical_id, binding, provider):
        if kind == "agent":
            module = self.package["public_definition"]["modules"][logical_id]
            if (module["request_contract"] != task_contracts.REQUEST_SCHEMA
                    or module["result_contract"] != task_contracts.RESULT_SCHEMA):
                _fail("CONTRACT_ADAPTER_REQUIRED", logical_id, "Legacy Agent needs an explicitly installed native adapter")
            expected = {"agent_id": logical_id, "version": module["version"],
                        "implementation_hash": module["implementation_hash"],
                        "request_contract": task_contracts.REQUEST_SCHEMA,
                        "result_contract": task_contracts.RESULT_SCHEMA}
            if any(getattr(provider, key, None) != value for key, value in expected.items()):
                _fail("AGENT_IMPLEMENTATION_MISMATCH", logical_id, "Provider identity, contract or implementation differs from its installed metadata")
            if set(getattr(provider, "modes", ())) != set(module["modes"]):
                _fail("AGENT_MODE_MISMATCH", logical_id, "Provider modes differ from installed metadata")
            names = ("invoke", "validate_input", "validate_output")
        elif kind == "source":
            names = ("query_latest", "resolve_exact", "reconcile_changes")
        else:
            # Native v1 execution explicitly converts to the existing durable
            # MOCK_RECORDED receipt contract in LocalTaskRuntime.
            names = ("record",)
        if any(not callable(getattr(provider, name, None))
               or inspect.iscoroutinefunction(getattr(provider, name, None)) for name in names):
            _fail("MODULE_PROVIDER_INVALID", kind + "." + logical_id, "Installed provider lacks synchronous contract/Schema callbacks")

    def _envelope(self, envelope, required, optional=()):
        check_json(envelope, path="activity")
        if not isinstance(envelope, dict) or set(envelope) != set(required) | (set(envelope) & set(optional)):
            _fail("ACTIVITY_ENVELOPE_INVALID", "activity", "Missing or unsupported Activity fields")
        self._verify_configuration()
        from .execution import validate_worker_manifest
        manifest = validate_worker_manifest(envelope["run_manifest"], self.package)
        return manifest

    @staticmethod
    def _bound(request, manifest):
        if any(request.get(key) != manifest[key] for key in ("identity", "engine_run_id", "run_pin")):
            _fail("ACTIVITY_RUN_MISMATCH", "request", "Request belongs to another Episode or pinned execution")

    def _source(self, source_id):
        provider = self._providers.get(("source", source_id))
        if provider is None:
            _fail("SOURCE_BINDING_MISSING", "source", "Logical source is not installed in this Worker")
        return provider

    def _source_request(self, request, manifest):
        self._bound(request, manifest)
        source_id = request["source"]
        binding = self.package["worker"]["bindings"]["sources"].get(source_id)
        approval = self.package["public_definition"]["policy"]["query_approvals"].get(source_id)
        if (binding is None or approval is None or not set(request["scope"]) <= set(binding["fields"])
                or not set(request["scope"]) <= set(approval["fields"])
                or request["authorization_ref"] != canonical_hash({"source": source_id, "approval": approval})):
            _fail("SOURCE_SCOPE_DENIED", "scope", "Query scope or authorization differs from the pinned approval")
        return self._source(source_id)

    def _discovered(self, value, request, validator):
        check_json(value, path="source_result")
        if not isinstance(value, dict) or set(value) != {"result", "events"} or not isinstance(value["events"], list):
            _fail("SOURCE_RESULT_INVALID", "source_result", "Expected a result and standard Ingress Events")
        result = validator(value["result"], request)
        observed = []
        ids = set()
        for item in value["events"]:
            if not isinstance(item, dict) or set(item) != {"source_id", "event"} or item["source_id"] != request["source"]:
                _fail("SOURCE_EVENT_MISMATCH", "events", "Discovered Event belongs to another logical source")
            event = item["event"]
            self._event(event, request["source"], request["identity"])
            if event["event_id"] in ids:
                _fail("SOURCE_EVENT_MISMATCH", "events", "A discovery response repeated an Event")
            ids.add(event["event_id"])
            observed.append({"event_id": event["event_id"], "source_ref": event["source_ref"],
                             "content_hash": event["payload"]["content_hash"]})
        if canonical_hash(sorted(observed, key=canonical_hash)) != canonical_hash(sorted(result["event_refs"], key=canonical_hash)):
            _fail("SOURCE_EVENT_MISMATCH", "event_refs", "Discovered Event references differ from returned standard Events")
        return {"result": result, "events": copy.deepcopy(value["events"])}

    def _event(self, event, source_id, expected_identity):
        try:
            validate_event(event)
        except (ValueError, TypeError, KeyError):
            _fail("EVENT_INVALID", "event", "Invalid standard source Event")
        binding = self.package["worker"]["bindings"]["sources"].get(source_id)
        if binding is None or identity(event) != expected_identity or event["source_ref"]["system"] != binding["system"]:
            _fail("SOURCE_IDENTITY_MISMATCH", "event", "Event identity or system differs from its source binding")
        if (set(event["payload"]) != {"data_ref", "content_hash"}
                or event["payload"]["data_ref"] != event["source_ref"]):
            _fail("EVENT_INVALID", "event.payload", "Exact-version source reference and content hash are required")
        validate_hash(event["payload"]["content_hash"], "event.payload.content_hash")

    def resolve_event(self, envelope):
        manifest = self._envelope(envelope, {"run_manifest", "source_id", "event"})
        event, source_id = envelope["event"], envelope["source_id"]
        self._event(event, source_id, manifest["identity"])
        result = self._source(source_id).resolve_exact(copy.deepcopy(event))
        try:
            validate_completion(result)
        except (ValueError, TypeError, KeyError):
            _fail("COMPLETION_INVALID", "completion", "Source returned an invalid exact-version completion")
        for key in ("event_id", "event_type", "sequence_no", "source_ref", "published_at", "received_at"):
            if result[key] != event[key]:
                _fail("COMPLETION_EVENT_MISMATCH", "completion", "Resolved source completion differs from the accepted Event")
        if (identity(result) != manifest["identity"] or result["content_hash"] != event["payload"]["content_hash"]
                or result["source_time"] != event["source_event_time"]):
            _fail("COMPLETION_EVENT_MISMATCH", "completion", "Completion identity, hash or source time differs from Event")
        system = self.package["worker"]["bindings"]["sources"][source_id]["system"]
        source_definition = {"system": system}
        mapping = manifest.get("status_mappings", {}).get(source_id)
        if mapping is not None:
            source_definition["status_mapping"] = mapping
        normalize_completion(result, fact_types={name: self.package["public_definition"]["facts"][name]["type"]
                             for name in self.package["public_definition"]["source_coverage"][source_id]},
                             source_id=source_id, source_definition=source_definition, applied_at=event["received_at"])
        return copy.deepcopy(result)

    def preflight(self, envelope):
        manifest = self._envelope(envelope, {"run_manifest", "request"})
        request = validate_preflight_request(envelope["request"])
        provider = self._source_request(request, manifest)
        return self._discovered(provider.query_latest(copy.deepcopy(request)), request, validate_preflight_result)

    def reconcile(self, envelope):
        manifest = self._envelope(envelope, {"run_manifest", "request"})
        request = validate_reconcile_request(envelope["request"])
        provider = self._source_request(request, manifest)
        return self._discovered(provider.reconcile_changes(copy.deepcopy(request)), request, validate_reconcile_result)

    def run_agent(self, envelope):
        manifest = self._envelope(envelope, {"run_manifest", "request"}, {"prerequisite_results", "verifications"})
        request = envelope["request"]
        self._bound(request, manifest)
        module = self.package["public_definition"]["modules"].get(request.get("agent_id"))
        if module is None:
            _fail("AGENT_BINDING_MISSING", "agent_id", "Agent is not selected by the pinned definition")
        request = task_contracts.validate_agent_request(request, module=module)
        return self._runtime.invoke_agent(request, output_fields=module["modes"][request["mode"]]["output_fields"],
            prerequisite_results=envelope.get("prerequisite_results"), verifications=envelope.get("verifications"))

    def agent_timeout_s(self, envelope):
        """Declared provider deadline; the Activity separately bounds waiting.

        Timing out a Python thread does not stop blocking SDK code. The same
        execution/request ID is retained for any permitted technical retry.
        """
        agent_id = envelope.get("request", {}).get("agent_id") if isinstance(envelope, dict) else None
        binding = self.package["worker"]["deployment"]["agents"].get(agent_id)
        if binding is None:
            _fail("AGENT_BINDING_MISSING", "agent_id", "Agent is not selected by the pinned Worker")
        return binding["timeout_s"]

    def run_work(self, envelope):
        manifest = self._envelope(envelope, {"run_manifest", "request"})
        request = task_contracts.validate_work_request(envelope["request"])
        self._bound(request, manifest)
        definition = self.package["public_definition"]
        task = definition["tasks"].get(request["task_definition_id"])
        if task != request["task"] or task["kind"] != request["kind"]:
            _fail("WORK_DEFINITION_MISMATCH", "task", "Work differs from the pinned task definition")
        if request["kind"] == "notify":
            if task["template"] not in definition["policy"]["notification_templates"]:
                _fail("NOTIFICATION_TEMPLATE_DENIED", "task", "Notification template is not approved")
            return self._runtime.invoke_work(request)
        if request["kind"] != "effect" or task["operation"] not in definition["policy"]["allowed_effect_operations"]:
            _fail("EFFECT_OPERATION_DENIED", "task", "Work operation is not approved")
        provider = self._providers.get(("effect", task["target"]))
        if provider is None:
            _fail("EFFECT_BINDING_MISSING", "task.target", "Effect target has no installed provider")
        runtime = LocalTaskRuntime({}, effects=provider)
        return runtime.invoke_work(request)
