"""Closed validator for this package's authoring Schema, not arbitrary JSON Schema.

The packaged document uses only the keywords below. Unknown keywords/external
references fail closed. No format coercion, imports, network or file access.
"""
from __future__ import annotations

import math
import re

from .errors import ConfigError

_KEYWORDS = {
    "$schema", "$ref", "$defs", "title", "description", "oneOf", "anyOf",
    "type", "properties", "required", "additionalProperties", "items",
    "minItems", "maxItems", "uniqueItems", "enum", "const", "pattern",
    "minLength", "minimum", "maximum", "exclusiveMinimum", "propertyNames",
    "minProperties",
}


def check_json(value, *, path="$", max_depth=96, max_nodes=100000):
    """Reject non-JSON, cyclic/overlarge input before recursive compilation."""
    active = set()
    nodes = 0

    def visit(item, location, depth):
        nonlocal nodes
        nodes += 1
        if depth > max_depth or nodes > max_nodes:
            raise ConfigError("INPUT_LIMIT", location, "Configuration exceeds structural limits")
        if type(item) is str:
            try:
                item.encode("utf8")
            except UnicodeError as error:
                raise ConfigError("INVALID_UNICODE", location, "Expected valid UTF-8 text") from error
            return
        if item is None or type(item) in (bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) not in (dict, list):
            raise ConfigError("NON_JSON_VALUE", location, "Expected a finite JSON value")
        if id(item) in active:
            raise ConfigError("CYCLIC_INPUT", location, "Recursive YAML aliases are unsupported")
        active.add(id(item))
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ConfigError("NON_STRING_KEY", location, "Object keys must be strings")
                try:
                    key.encode("utf8")
                except UnicodeError as error:
                    raise ConfigError("INVALID_UNICODE", location, "Expected a valid UTF-8 object key") from error
                visit(child, location + "." + key, depth + 1)
        else:
            for index, child in enumerate(item):
                visit(child, f"{location}[{index}]", depth + 1)
        active.remove(id(item))

    visit(value, path, 0)


def _equal(left, right):
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_equal(v, right[k]) for k, v in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return left == right


class AuthoringValidator:
    def __init__(self, schema):
        check_json(schema)
        self.schema = schema
        self._inspect(schema, "$schema")

    def _inspect(self, node, path):
        if type(node) is bool:
            return
        if not isinstance(node, dict):
            raise ConfigError("SCHEMA_INVALID", path, "Expected a Schema object")
        unknown = set(node) - _KEYWORDS
        if unknown:
            raise ConfigError("SCHEMA_KEYWORD_UNSUPPORTED", path, "Unsupported validator keyword")
        if "$ref" in node:
            self._resolve(node["$ref"], path)
        for keyword in ("$defs", "properties"):
            for key, child in node.get(keyword, {}).items():
                self._inspect(child, path + "." + keyword + "." + key)
        for keyword in ("oneOf", "anyOf"):
            for index, child in enumerate(node.get(keyword, [])):
                self._inspect(child, f"{path}.{keyword}[{index}]")
        for keyword in ("items", "additionalProperties", "propertyNames"):
            if keyword in node:
                self._inspect(node[keyword], path + "." + keyword)

    def _resolve(self, ref, path):
        if not isinstance(ref, str) or not ref.startswith("#/$defs/") or ref.count("/") != 2:
            raise ConfigError("SCHEMA_REF_UNSUPPORTED", path, "Only local named Schema references are supported")
        name = ref.split("/")[-1]
        if name not in self.schema.get("$defs", {}):
            raise ConfigError("SCHEMA_REF_INVALID", path, "Unknown Schema reference")
        return self.schema["$defs"][name]

    def validate(self, value, *, path="$"):
        check_json(value, path=path)
        self._validate(value, self.schema, path, 0)

    def _validate(self, value, node, path, depth):
        def fail(message):
            raise ConfigError("SHAPE_INVALID", path, message)

        if depth > 160:
            raise ConfigError("INPUT_LIMIT", path, "Schema validation exceeds recursion limit")
        if node is True:
            return
        if node is False:
            fail("Unsupported field")
        if "$ref" in node:
            self._validate(value, self._resolve(node["$ref"], path), path, depth + 1)
        for keyword in ("oneOf", "anyOf"):
            if keyword not in node:
                continue
            options = node[keyword]
            # Give a precise field diagnostic for a discriminated document.
            if isinstance(value, dict) and "schema" in value and path in ("$", "clinical", "site", "safety", "deployment", "metadata"):
                selected = []
                for option in options:
                    target = self._resolve(option["$ref"], path) if "$ref" in option else option
                    if target.get("properties", {}).get("schema", {}).get("const") == value["schema"]:
                        selected.append(option)
                if len(selected) == 1:
                    self._validate(value, selected[0], path, depth + 1)
                    continue
            matches = 0
            failures = []
            for option in options:
                try:
                    self._validate(value, option, path, depth + 1)
                    matches += 1
                except ConfigError as error:
                    failures.append(error)
            if matches == 0:
                # Deepest candidate is more useful than the root union message.
                if failures:
                    raise max(failures, key=lambda error: error.path.count(".") + error.path.count("["))
                fail("No supported variant matches")
            if keyword == "oneOf" and matches != 1:
                fail("Ambiguous Schema variants")
        if "type" in node:
            kind = node["type"]
            valid = {
                "object": type(value) is dict, "array": type(value) is list,
                "string": type(value) is str, "boolean": type(value) is bool,
                "integer": type(value) is int or (type(value) is float and value.is_integer()),
                "number": type(value) in (int, float), "null": value is None,
            }
            if not any(valid.get(k, False) for k in (kind if isinstance(kind, list) else [kind])):
                fail("Value has the wrong type")
        if "const" in node and not _equal(value, node["const"]):
            fail("Value differs from the required constant")
        if "enum" in node and not any(_equal(value, entry) for entry in node["enum"]):
            fail("Value is outside the supported set")
        if isinstance(value, dict):
            for key in node.get("required", []):
                if key not in value:
                    raise ConfigError("SHAPE_INVALID", path + "." + key, "Required field is missing")
            if len(value) < node.get("minProperties", 0):
                fail("Too few object properties")
            properties = node.get("properties", {})
            for key, item in value.items():
                if "propertyNames" in node:
                    self._validate(key, node["propertyNames"], path + "." + key, depth + 1)
                if key in properties:
                    self._validate(item, properties[key], path + "." + key, depth + 1)
                elif "additionalProperties" in node:
                    self._validate(item, node["additionalProperties"], path + "." + key, depth + 1)
        if isinstance(value, list):
            if len(value) < node.get("minItems", 0) or len(value) > node.get("maxItems", float("inf")):
                fail("Array size is outside supported bounds")
            if node.get("uniqueItems") and any(_equal(value[i], value[j]) for i in range(len(value)) for j in range(i)):
                fail("Duplicate array item")
            for index, item in enumerate(value):
                if "items" in node:
                    self._validate(item, node["items"], f"{path}[{index}]", depth + 1)
        if isinstance(value, str):
            if len(value) < node.get("minLength", 0):
                fail("String is too short")
            if "pattern" in node and re.search(node["pattern"], value) is None:
                fail("String does not match the required pattern")
        if type(value) in (int, float):
            if value < node.get("minimum", -float("inf")) or value > node.get("maximum", float("inf")):
                fail("Number is outside supported bounds")
            if "exclusiveMinimum" in node and value <= node["exclusiveMinimum"]:
                fail("Number must exceed the lower bound")
