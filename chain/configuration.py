"""Worker/client I/O boundary for the versioned Clinical authoring format.

Importing this module does not load a document or a plugin. Historical v0.3
definitions belong to their preserved artifact and are not dispatched here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import yaml

from .compiler import (DOCUMENT_SCHEMAS, canonical_hash, compile_definition,
                       validate_documents)
from .errors import ConfigError, UnsupportedSchema
from .registry import resolve_bindings
from .shape import check_json

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = Path(__file__).parent / "resources/authoring-v1.schema.json"
MAX_FILE_BYTES = 2 * 1024 * 1024


class _UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        if not isinstance(node, yaml.MappingNode):
            raise ConfigError("YAML_MAPPING_INVALID", "$", "Expected a mapping")
        result = {}
        for key_node, value_node in node.value:
            if key_node.tag != "tag:yaml.org,2002:str":
                raise ConfigError("NON_STRING_KEY", f"line:{key_node.start_mark.line + 1}",
                                  "Keys must be strings; YAML merge keys are unsupported")
            key = self.construct_object(key_node, deep=deep)
            if key in result:
                raise ConfigError("DUPLICATE_KEY", f"line:{key_node.start_mark.line + 1}",
                                  "Duplicate mapping key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("DUPLICATE_KEY", "$", "Duplicate JSON object key")
        result[key] = value
    return result


def _read_document(path, *, json_only=False):
    path = Path(path)
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ConfigError("INPUT_LIMIT", "$", "Configuration file exceeds 2 MiB limit")
        text = data.decode("utf8")
        value = json.loads(text, object_pairs_hook=_unique_json) if json_only or path.suffix == ".json" else yaml.load(text, Loader=_UniqueLoader)
        check_json(value)
        if not isinstance(value, dict):
            raise ConfigError("DOCUMENT_TYPE_INVALID", "$", "Expected a document object")
        evidence = {"file_name": path.name, "byte_hash": "sha256:" + hashlib.sha256(data).hexdigest()}
        return value, evidence
    except ConfigError as error:
        raise ConfigError(error.code, error.path, error.message, source=str(path)) from error
    except (OSError, UnicodeError) as error:
        raise ConfigError("DOCUMENT_READ_FAILED", "$", "File cannot be read as UTF-8", source=str(path)) from None
    except (yaml.YAMLError, json.JSONDecodeError, RecursionError, ValueError) as error:
        # Parser text may quote credentials or patient data. Keep it private.
        raise ConfigError("DOCUMENT_PARSE_FAILED", "$", "Malformed or unsupported document syntax", source=str(path)) from None


def _compiler_pin():
    folder = Path(__file__).parent
    files = {str(path.relative_to(folder)): "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(folder.rglob("*.py"))}
    environment = {"python": ".".join(map(str, sys.version_info[:3])), "pyyaml": yaml.__version__}
    # The shared contracts now belong to this package and are covered above.
    # Installed providers (including demo mocks) retain their own file pins.
    evidence = {"files": files, "environment": environment}
    return canonical_hash(evidence), evidence


def load_configuration(entry_path=None, *, site_path=None, safety_path=None,
                       deployment_path=None, metadata_path=None, registry_path=None,
                       catalog_path=None, installation_root=ROOT, installations=None,
                       validation_only=False):
    """Return a validated native fact-definition compilation package.

    New authoring defaults to strict binding resolution. ``validation_only``
    accepts known future binding gaps as explicit blockers, never corrupt bytes
    or invalid approval. Even native bindings do not make a phase-2 executable.
    ``installations`` is a trusted Worker inventory mapping or JSON file path.
    The public definition/manifest exclude private bindings and byte evidence.
    """
    if type(validation_only) is not bool:
        raise ConfigError("OPTION_INVALID", "validation_only", "Expected a boolean")
    path = Path(entry_path) if entry_path else ROOT / "config/minimum/clinical.yaml"
    entry, entry_evidence = _read_document(path)
    schema_marker = entry.get("schema")
    state_marker = entry.get("state_spec_schema")
    if schema_marker == DOCUMENT_SCHEMAS["clinical"]:
        if state_marker is not None or registry_path is not None or catalog_path is not None:
            raise ConfigError("MIXED_CONFIGURATION", "clinical", "New Clinical cannot contain legacy configuration options", source=str(path))
    else:
        raise UnsupportedSchema("SCHEMA_VERSION_UNSUPPORTED", "schema",
            "Expected chain-clinical/v1; historical Workflow definitions require their preserved artifact",
            source=str(path))
    folder = path.parent
    paths = {"clinical": path, "site": Path(site_path) if site_path else folder / "site.yaml",
             "safety": Path(safety_path) if safety_path else folder / "safety.yaml",
             "deployment": Path(deployment_path) if deployment_path else folder / "deployment.yaml",
             "metadata": Path(metadata_path) if metadata_path else folder / "module_metadata.json"}
    documents, evidence = {"clinical": entry}, {"clinical": entry_evidence}
    for name in ("site", "safety", "deployment", "metadata"):
        documents[name], evidence[name] = _read_document(paths[name], json_only=name == "metadata")
    schema, schema_evidence = _read_document(SCHEMA_PATH, json_only=True)
    try:
        validate_documents(documents, schema)
    except ConfigError as error:
        name = error.path.split(".", 1)[0]
        raise ConfigError(error.code, error.path, error.message, source=str(paths.get(name, path))) from error
    inventory_evidence = None
    if isinstance(installations, (str, Path)):
        installations, inventory_evidence = _read_document(installations, json_only=True)
    if installations is not None:
        check_json(installations, path="installations")
    try:
        bindings = resolve_bindings(documents, installation_root=installation_root,
                                    installations=installations, validation_only=validation_only)
    except ConfigError as error:
        raise ConfigError(error.code, error.path, error.message, source=str(paths["deployment"])) from error
    compiler_pin, compiler_evidence = _compiler_pin()
    result = compile_definition(documents, bindings, schema=schema,
                                compiler_pin=compiler_pin, schema_pin=schema_evidence["byte_hash"])
    result["artifact_evidence"] = {"sources": evidence, "schema": schema_evidence,
                                   "compiler": compiler_evidence}
    if inventory_evidence:
        result["artifact_evidence"]["installation_inventory"] = inventory_evidence
    return result
