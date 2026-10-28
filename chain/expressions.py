"""Closed, pure three-valued evaluation of compiled fact-expression ASTs.

This does not reinterpret the v0.3 DSL, resolve a path into an arbitrary Python
object, read files, evaluate prose or perform work. The caller supplies a pure
resolver for declared references and id -> compiled predicate requirements.

The function ``evaluate`` creates a fresh session. ``Evaluator`` may be reused
only within ONE immutable input frame to memoize shared references/requirements.
It must not be retained across new source data or different pinned definitions.
Return values are detached JSON (plain truth strings, not Enum instances).
"""
from __future__ import annotations

import copy
from contextvars import ContextVar
from enum import Enum
import json
import re

from .errors import ConfigError
from .runtime_contracts import VALIDITIES, canonical_source_refs, timestamp
from .shape import check_json


class Truth(str, Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"

    def __bool__(self):
        raise TypeError("Compare Truth explicitly; UNKNOWN is not a Python boolean")


_NAMESPACES = {"facts", "milestones", "results", "decisions", "state_entries"}
_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
_VALUE_TYPES = {"string", "integer", "number", "boolean", "object"}
_NUMERIC = {"integer", "number"}
_CELL_FIELDS = {"value", "validity", "source_refs"}
_CELL_OPTIONS = {"value_type", "dependencies", "diagnostics", "source_status", "source_time", "known_at", "unit", "confirmation_status"}
_ACTIVE_CONTEXT = ContextVar("chain_expression_context", default=None)


def _fail(code, path, message):
    raise ConfigError(code, path, message) from None


def _closed(value, required, path, optional=()):
    if not isinstance(value, dict) or set(required) - set(value) or set(value) - set(required) - set(optional):
        _fail("EXPRESSION_SCHEMA_INVALID", path, "Missing or unsupported expression fields")


def _text(value, path):
    if not isinstance(value, str) or not value.strip():
        _fail("EXPRESSION_SCHEMA_INVALID", path, "Expected a nonempty string")
    return value


def _name(value, path):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail("REFERENCE_INVALID", path, "Expected a declared logical identifier")
    return value


def _logical_ref(operand, path):
    _closed(operand, {"kind", "namespace", "name", "path"}, path)
    if operand["kind"] != "ref" or not isinstance(operand["namespace"], str) or operand["namespace"] not in _NAMESPACES:
        _fail("REFERENCE_INVALID", path, "Unsupported reference namespace")
    name = _name(operand["name"], path + ".name")
    parts = operand["path"]
    if not isinstance(parts, list):
        _fail("REFERENCE_INVALID", path + ".path", "Expected a declared field path")
    for part in parts:
        _name(part, path + ".path")
    namespace = operand["namespace"]
    if ((namespace in {"facts", "milestones"} and parts)
            or namespace == "state_entries" and parts not in (["entered"], ["generation"])
            or namespace == "decisions" and parts != ["value"]
            or namespace == "results" and len(parts) != 1):
        _fail("REFERENCE_INVALID", path, "Unsupported nested reference path")
    return ".".join([namespace, name, *parts])


def _dependency(value, path):
    _text(value, path)
    parts = value.split(".")
    if len(parts) == 2 and parts[0] == "requirements":
        _name(parts[1], path)
        return value
    if len(parts) < 2:
        _fail("REFERENCE_INVALID", path, "Expected a namespaced logical dependency")
    _logical_ref({"kind": "ref", "namespace": parts[0], "name": parts[1], "path": parts[2:]}, path)
    return value


def _diagnostics(value, path):
    if not isinstance(value, list):
        _fail("VALUE_CELL_INVALID", path, "Expected diagnostic records")
    for item in value:
        _closed(item, {"code"}, path, {"path", "reference", "validity"})
        _text(item["code"], path + ".code")
        if "path" in item:
            _text(item["path"], path + ".path")
        if "reference" in item:
            _dependency(item["reference"], path + ".reference")
        if "validity" in item and (not isinstance(item["validity"], str) or item["validity"] not in VALIDITIES):
            _fail("VALUE_CELL_INVALID", path + ".validity", "Unsupported diagnostic validity")
    return copy.deepcopy(value)


def _kind(value):
    return {str: "string", bool: "boolean", int: "integer", float: "number", dict: "object", list: "array", type(None): "null"}.get(type(value))


def _compatible(left, right):
    return "unknown" in (left, right) or left == right or {left, right} <= _NUMERIC


def _deduplicate_diagnostics(items):
    result = {json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":")): item for item in items}
    return [copy.deepcopy(result[key]) for key in sorted(result)]


class _Context:
    def __init__(self, max_depth, max_nodes):
        self.max_depth, self.max_nodes = max_depth, max_nodes
        self.depth = self.nodes = 0
        self.active_refs = set()
        self.active_requirements = set()

    def consume(self, path, amount=1):
        self.nodes += amount
        if self.nodes > self.max_nodes:
            _fail("EVALUATION_LIMIT", path, "Expression evaluation exceeds its node budget")

    def enter(self, path):
        self.consume(path)
        self.depth += 1
        if self.depth > self.max_depth:
            self.depth -= 1
            _fail("EVALUATION_LIMIT", path, "Expression evaluation exceeds its depth limit")


def _proof(truth, *items, dependencies=()):
    refs = {}
    names = set(dependencies)
    diagnostics = []
    for item in items:
        for ref in item["source_refs"]:
            key = (ref["system"], ref["record_id"], ref["version"], ref["field"])
            refs[key] = ref
        names.update(item["dependencies"])
        diagnostics.extend(item["diagnostics"])
    return {"truth": truth, "source_refs": [copy.deepcopy(refs[key]) for key in sorted(refs)],
            "dependencies": sorted(names), "diagnostics": _deduplicate_diagnostics(diagnostics)}


class Evaluator:
    """Memoized pure evaluation session for one immutable resolver input frame."""

    def __init__(self, *, resolve, requirements=None, max_depth=96, max_nodes=10000):
        self._initialize(resolve, max_depth, max_nodes)
        requirements = {} if requirements is None else requirements
        check_json(requirements, path="requirements")
        if not isinstance(requirements, dict):
            _fail("EXPRESSION_SCHEMA_INVALID", "requirements", "Expected declared predicate requirements")
        for name in requirements:
            _name(name, "requirements")
        self._requirements = copy.deepcopy(requirements)

    def _initialize(self, resolve, max_depth, max_nodes):
        if not callable(resolve):
            _fail("RESOLVER_INVALID", "resolve", "A declared-reference resolver is required")
        if (type(max_depth) is not int or not 1 <= max_depth <= 96
                or type(max_nodes) is not int or not 1 <= max_nodes <= 100000):
            _fail("EVALUATION_LIMIT_INVALID", "limits", "Expected bounded positive evaluation limits")
        self._resolve = resolve
        self._context = _Context(max_depth, max_nodes)
        self._refs = {}
        self._predicates = {}

    @classmethod
    def _from_owned_requirements(cls, *, resolve, requirements, max_depth=96, max_nodes=10000):
        """Internal session over a projector-owned, validated immutable table.

        The public constructor still copies and validates caller input. Only a
        StateProjector which owns its detached definition uses this factory;
        references, predicate memoization and budgets are fresh on every call.
        """
        owner = cls.__new__(cls)
        owner._initialize(resolve, max_depth, max_nodes)
        owner._requirements = requirements
        return owner

    def evaluate(self, expression):
        check_json(expression, path="expression")
        current = _ACTIVE_CONTEXT.get()
        context = current if current is not None else self._context
        token = _ACTIVE_CONTEXT.set(context) if current is None else None
        try:
            result = self._node(expression, "expression", context)
            return copy.deepcopy(result)
        except RecursionError:
            _fail("EVALUATION_LIMIT", "expression", "Recursive callback evaluation exceeds supported limits")
        finally:
            if token is not None:
                _ACTIVE_CONTEXT.reset(token)

    def _cell(self, operand, path, context):
        reference = _logical_ref(operand, path)
        if reference in context.active_refs:
            _fail("REFERENCE_CYCLE", path, "Resolver references form a cycle")
        if reference in self._refs:
            cached = self._refs[reference]
            context.consume(path, len(cached["source_refs"]) + len(cached["dependencies"]) + len(cached["diagnostics"]))
            return copy.deepcopy(self._refs[reference])
        context.active_refs.add(reference)
        try:
            try:
                supplied = self._resolve(copy.deepcopy(operand))
            except ConfigError:
                raise
            except (KeyError, IndexError, AttributeError, TypeError, ValueError):
                _fail("UNDEFINED_REFERENCE", path, "Reference resolution failed its declared contract")
            check_json(supplied, path=path)
            _closed(supplied, _CELL_FIELDS, path, _CELL_OPTIONS)
            validity = supplied["validity"]
            if not isinstance(validity, str) or validity not in VALIDITIES:
                _fail("VALUE_CELL_INVALID", path + ".validity", "Unsupported value validity")
            if (validity == "KNOWN") == (supplied["value"] is None):
                _fail("VALUE_CELL_INVALID", path, "Known cells need a value; unavailable cells must be null")
            declared = supplied.get("value_type")
            if declared is not None and (not isinstance(declared, str) or declared not in _VALUE_TYPES):
                _fail("VALUE_CELL_INVALID", path + ".value_type", "Unsupported declared cell type")
            actual = _kind(supplied["value"])
            if operand["namespace"] == "state_entries":
                expected = "boolean" if operand["path"] == ["entered"] else "integer"
                if declared is not None and declared != expected or validity == "KNOWN" and actual != expected:
                    _fail("CONDITION_TYPE_MISMATCH", path, "State entry field differs from its boolean/integer contract")
                declared = expected
            if validity == "KNOWN" and declared is not None and not (actual == declared or declared == "number" and actual == "integer"):
                _fail("CONDITION_TYPE_MISMATCH", path, "Known cell value differs from its declared type")
            refs = canonical_source_refs(supplied["source_refs"])
            dependencies = supplied.get("dependencies", [])
            if not isinstance(dependencies, list):
                _fail("VALUE_CELL_INVALID", path + ".dependencies", "Expected logical dependency identifiers")
            names = {_dependency(name, path + ".dependencies") for name in dependencies}
            if len(names) != len(dependencies):
                _fail("VALUE_CELL_INVALID", path + ".dependencies", "Duplicate logical dependency")
            names.add(reference)
            diagnostics = _diagnostics(supplied.get("diagnostics", []), path + ".diagnostics")
            if validity != "KNOWN":
                diagnostics.append({"code": "INPUT_" + validity, "reference": reference, "validity": validity, "path": path})
            for key in ("source_status", "unit", "confirmation_status"):
                if key in supplied:
                    _text(supplied[key], path + "." + key)
            for key in ("source_time", "known_at"):
                if key in supplied:
                    timestamp(supplied[key], path=path + "." + key)
            if "source_time" in supplied and "known_at" in supplied and timestamp(supplied["source_time"]) > timestamp(supplied["known_at"]):
                _fail("VALUE_CELL_INVALID", path, "Source time follows known time")
            context.consume(path, len(refs) + len(names) + len(diagnostics))
            result = {"value": copy.deepcopy(supplied["value"]), "validity": validity,
                      "value_type": declared or (actual if validity == "KNOWN" else "unknown"),
                      "source_refs": refs, "dependencies": sorted(names), "diagnostics": diagnostics}
            self._refs[reference] = copy.deepcopy(result)
            return result
        finally:
            context.active_refs.remove(reference)

    def _operand(self, value, path, context, *, ref_only=False):
        context.consume(path)
        if not isinstance(value, dict):
            _fail("EXPRESSION_SCHEMA_INVALID", path, "Expected an explicit ref or literal operand")
        if value.get("kind") == "ref":
            return self._cell(value, path, context)
        _closed(value, {"kind", "value"}, path)
        if ref_only or value["kind"] != "literal":
            _fail("EXPRESSION_SCHEMA_INVALID", path, "Expected a declared reference operand")
        return {"value": copy.deepcopy(value["value"]), "validity": "KNOWN", "value_type": _kind(value["value"]),
                "source_refs": [], "dependencies": [], "diagnostics": []}

    def _equal(self, left, right, path, context):
        context.consume(path)
        a, b = _kind(left), _kind(right)
        if a in _NUMERIC and b in _NUMERIC:
            return left == right
        if a != b:
            return False
        if isinstance(left, dict):
            return left.keys() == right.keys() and all(self._equal(left[key], right[key], path, context) for key in sorted(left))
        if isinstance(left, list):
            return len(left) == len(right) and all(self._equal(a, b, path, context) for a, b in zip(left, right))
        return left == right

    def _node(self, expression, path, context):
        context.enter(path)
        try:
            if not isinstance(expression, dict):
                _fail("EXPRESSION_SCHEMA_INVALID", path, "Expected a compiled expression object")
            op = expression.get("op")
            if not isinstance(op, str):
                _fail("OPERATOR_UNSUPPORTED", path, "Expected a supported compiled operator")
            if op == "requirement":
                _closed(expression, {"op", "requirement_id"}, path)
                name = _name(expression["requirement_id"], path + ".requirement_id")
                if name not in self._requirements:
                    _fail("UNDEFINED_REQUIREMENT", path, "Predicate requirement is not declared")
                guard = (id(self), name)
                if guard in context.active_requirements:
                    _fail("REQUIREMENT_CYCLE", path, "Predicate requirements form a cycle")
                if name not in self._predicates:
                    context.active_requirements.add(guard)
                    try:
                        self._predicates[name] = self._node(self._requirements[name], "requirements." + name, context)
                    finally:
                        context.active_requirements.remove(guard)
                else:
                    cached = self._predicates[name]
                    context.consume(path, len(cached["source_refs"]) + len(cached["dependencies"]) + len(cached["diagnostics"]))
                return _proof(self._predicates[name]["truth"], self._predicates[name], dependencies=["requirements." + name])
            operators = {"all", "any", "not", "eq", "ne", "gte", "in", "is_known", "has_status"}
            if op not in operators:
                _fail("OPERATOR_UNSUPPORTED", path, "Unsupported compiled condition operator")
            _closed(expression, {"op", "args"} | ({"status"} if op == "has_status" else set()), path)
            args = expression["args"]
            if not isinstance(args, list):
                _fail("EXPRESSION_SCHEMA_INVALID", path + ".args", "Expected operator arguments")
            arity = 1 if op in {"not", "is_known", "has_status"} else 2
            if (op in {"all", "any"} and not args) or (op not in {"all", "any"} and len(args) != arity):
                _fail("EXPRESSION_SCHEMA_INVALID", path + ".args", "Operator arity differs from its contract")
            if op in {"all", "any", "not"}:
                results = [self._node(arg, f"{path}.args[{index}]", context) for index, arg in enumerate(args)]
                truths = [result["truth"] for result in results]
                if op == "all":
                    truth = "FALSE" if "FALSE" in truths else "TRUE" if all(t == "TRUE" for t in truths) else "UNKNOWN"
                elif op == "any":
                    truth = "TRUE" if "TRUE" in truths else "FALSE" if all(t == "FALSE" for t in truths) else "UNKNOWN"
                else:
                    truth = {"TRUE": "FALSE", "FALSE": "TRUE", "UNKNOWN": "UNKNOWN"}[truths[0]]
                return _proof(truth, *results)
            cells = [self._operand(arg, f"{path}.args[{index}]", context, ref_only=op in {"is_known", "has_status"})
                     for index, arg in enumerate(args)]
            if op == "is_known":
                truth = {"KNOWN": "TRUE", "ABSENT_CONFIRMED": "FALSE"}.get(cells[0]["validity"], "UNKNOWN")
                return _proof(truth, *cells)
            if op == "has_status":
                status = expression["status"]
                if not isinstance(status, str) or status not in VALIDITIES:
                    _fail("EXPRESSION_SCHEMA_INVALID", path + ".status", "Unsupported validity status")
                return _proof("TRUE" if cells[0]["validity"] == status else "FALSE", *cells)
            left, right = cells
            a, b = left["value_type"], right["value_type"]
            if op == "in":
                if args[1].get("kind") != "literal" or type(right["value"]) is not list:
                    _fail("CONDITION_TYPE_MISMATCH", path, "Membership requires an explicit literal list")
                members = [_kind(value) for value in right["value"]]
                if a == "null" or any(kind == "null" or not _compatible(a, kind) for kind in members):
                    _fail("CONDITION_TYPE_MISMATCH", path, "Membership values have incompatible types")
            elif op == "gte":
                if a not in _NUMERIC | {"unknown"} or b not in _NUMERIC | {"unknown"}:
                    _fail("CONDITION_TYPE_MISMATCH", path, "Numeric comparison requires numeric operands")
            elif "null" in (a, b) or not _compatible(a, b):
                _fail("CONDITION_TYPE_MISMATCH", path, "Equality operands have incompatible types")
            if left["validity"] != "KNOWN" or right["validity"] != "KNOWN":
                return _proof("UNKNOWN", *cells)
            if op == "gte":
                matches = left["value"] >= right["value"]
            elif op == "in":
                matches = any(self._equal(left["value"], member, path, context) for member in right["value"])
            else:
                matches = self._equal(left["value"], right["value"], path, context)
                if op == "ne":
                    matches = not matches
            return _proof("TRUE" if matches else "FALSE", *cells)
        finally:
            context.depth -= 1


def _validate_value_cell(operand, supplied):
    """Run the same cell checks as a one-ref ``is_known`` without its proof.

    Projectors construct the reference themselves and need validation, not a
    truth/proof for each supplied cell. Keep the same two AST-node charges,
    active recursion context and cell contract as that expression evaluation.
    """
    session = Evaluator(resolve=lambda _: supplied)
    current = _ACTIVE_CONTEXT.get()
    context = current if current is not None else session._context
    token = _ACTIVE_CONTEXT.set(context) if current is None else None
    try:
        context.enter("expression")
        try:
            context.consume("expression.args[0]")
            session._cell(operand, "expression.args[0]", context)
        finally:
            context.depth -= 1
    except RecursionError:
        _fail("EVALUATION_LIMIT", "expression", "Recursive callback evaluation exceeds supported limits")
    finally:
        if token is not None:
            _ACTIVE_CONTEXT.reset(token)


def evaluate(expression, *, resolve, requirements=None, max_depth=96, max_nodes=10000):
    """Evaluate a compiled AST against a fresh immutable caller-supplied frame."""
    return Evaluator(resolve=resolve, requirements=requirements, max_depth=max_depth, max_nodes=max_nodes).evaluate(expression)


def validate_expression(expression, *, resolve=None, requirements=None, max_depth=96, max_nodes=10000):
    """Validate compiled syntax/constants, optionally against declared typed refs.

    Without ``resolve`` this is syntactic validation only: it cannot establish
    reference existence or a Fact's declared type. The supplied static resolver
    should return explicitly declared UNKNOWN cells with value_type metadata.
    This helper returns the detached AST, never a fabricated runtime truth.
    """
    if resolve is None:
        resolve = lambda operand: {"value": None, "validity": "UNKNOWN", "source_refs": []}
    evaluate(expression, resolve=resolve, requirements=requirements, max_depth=max_depth, max_nodes=max_nodes)
    return copy.deepcopy(expression)
