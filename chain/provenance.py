"""Pure, immutable source-field dependency graph and targeted invalidation."""
from __future__ import annotations

import copy
from collections import deque

from .errors import ConfigError


def _fail(code, message):
    raise ConfigError(code, "provenance", message)


def _ref(value):
    if not isinstance(value, dict) or set(value) != {"system", "record_id", "version", "field"}:
        _fail("PROVENANCE_REF_INVALID", "Expected an exact source-field reference")
    if any(not isinstance(value[k], str) or not value[k].strip() for k in ("system", "record_id", "field")):
        _fail("PROVENANCE_REF_INVALID", "Source-field identifiers must be nonempty strings")
    if type(value["version"]) is not int or value["version"] < 1:
        _fail("PROVENANCE_REF_INVALID", "Source version must be a positive integer")
    return {k: value[k] for k in ("system", "record_id", "version", "field")}


def _key(ref):
    return tuple(ref[k] for k in ("system", "record_id", "version", "field"))


def _refs(values):
    if not isinstance(values, (list, tuple)):
        _fail("PROVENANCE_REF_INVALID", "Expected a list or tuple of source-field references")
    refs = [_ref(v) for v in values]
    keys = [_key(v) for v in refs]
    if len(set(keys)) != len(keys):
        _fail("PROVENANCE_REF_INVALID", "Duplicate source-field references")
    return sorted(refs, key=_key)


def _rule(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"rule_id", "version"}
            or any(not isinstance(v, str) or not v.strip() for v in value.values())):
        _fail("PROVENANCE_RULE_INVALID", "Expected a rule ID and version")
    return copy.deepcopy(value)


class ProvenanceIndex:
    """Append-only nodes; invalidation annotates history without rewriting nodes.

    Versions are ordered only within one system/record/field. The graph neither
    chooses current clinical values nor owns historical Agent-result captures.
    """

    def __init__(self):
        self._nodes = {}
        self._dependents = {}
        self._invalidations = []

    def contains(self, ref):
        return _key(_ref(ref)) in self._nodes

    def _known(self, ref):
        ref = _ref(ref)
        if _key(ref) not in self._nodes:
            _fail("DEPENDENCY_PENDING", "Source dependency is not yet known")
        return ref

    def add(self, ref, dependencies=(), *, rule=None):
        ref, dependencies, rule = _ref(ref), _refs(dependencies), _rule(rule)
        key = _key(ref)
        if any(_key(dep) == key for dep in dependencies):
            _fail("DEPENDENCY_CYCLE", "A source field cannot depend on itself")
        existing = self._nodes.get(key)
        if existing is not None:
            if existing["dependencies"] != dependencies or existing["rule"] != rule:
                _fail("PROVENANCE_CONFLICT", "An existing source-field node is immutable")
            return self.node(ref)
        for dependency in dependencies:
            self._known(dependency)
        inherited = []
        for event in self._invalidations:
            corrected_lineage = any(
                all(ref[k] == target[k] for k in ("system", "record_id", "field"))
                and ref["version"] <= target["version"] for target in event["targets"])
            if corrected_lineage or any(event in self._nodes[_key(dep)]["invalidations"] for dep in dependencies):
                inherited.append(copy.deepcopy(event))
        self._nodes[key] = {"ref": ref, "dependencies": dependencies,
                            "rule": rule, "invalidations": inherited}
        self._dependents.setdefault(key, set())
        for dependency in dependencies:
            self._dependents.setdefault(_key(dependency), set()).add(key)
        return self.node(ref)

    def closure(self, refs):
        pending = [_key(self._known(ref)) for ref in _refs(refs)]
        found = set()
        while pending:
            key = pending.pop()
            if key in found:
                continue
            found.add(key)
            pending.extend(_key(dep) for dep in self._nodes[key]["dependencies"])
        return [copy.deepcopy(self._nodes[key]["ref"]) for key in sorted(found)]

    def lineage(self, target):
        target = self._known(target)
        return [copy.deepcopy(node["ref"]) for key, node in sorted(self._nodes.items())
                if all(node["ref"][k] == target[k] for k in ("system", "record_id", "field"))
                and node["ref"]["version"] <= target["version"]]

    def invalidate(self, targets, *, reason, reducer_seq):
        targets = _refs(targets)
        if not targets or not isinstance(reason, str) or not reason.strip():
            _fail("PROVENANCE_INVALIDATION_INVALID", "Targets and an explicit reason are required")
        if type(reducer_seq) is not int or reducer_seq < 1:
            _fail("PROVENANCE_INVALIDATION_INVALID", "Reducer sequence must be a positive integer")
        starts = [_key(ref) for target in targets for ref in self.lineage(target)]
        found, pending = set(), list(starts)
        while pending:
            key = pending.pop()
            if key in found:
                continue
            found.add(key)
            pending.extend(self._dependents.get(key, ()))
        event = {"targets": targets, "reason": reason, "reducer_seq": reducer_seq}
        if event not in self._invalidations:
            self._invalidations.append(copy.deepcopy(event))
        for key in found:
            if event not in self._nodes[key]["invalidations"]:
                self._nodes[key]["invalidations"].append(copy.deepcopy(event))
        return [copy.deepcopy(self._nodes[key]["ref"]) for key in sorted(found)]

    def is_invalid(self, refs):
        refs = [self._known(ref) for ref in _refs(refs)]
        return any(self._nodes[_key(ref)]["invalidations"] for ref in refs)

    def node(self, ref):
        return copy.deepcopy(self._nodes[_key(self._known(ref))])

    def export_state(self):
        return {"provenance_schema": "chain-fact-provenance/v1",
                "nodes": [{key: copy.deepcopy(node[key]) for key in ("ref", "dependencies", "rule")}
                          for _, node in sorted(self._nodes.items())],
                "invalidations": copy.deepcopy(self._invalidations)}

    @classmethod
    def from_state(cls, state):
        if (not isinstance(state, dict) or set(state) != {"provenance_schema", "nodes", "invalidations"}
                or state["provenance_schema"] != "chain-fact-provenance/v1"
                or not isinstance(state["nodes"], list) or not isinstance(state["invalidations"], list)):
            _fail("PROVENANCE_STATE_INVALID", "Unsupported provenance state")
        nodes = {}
        for node in state["nodes"]:
            if not isinstance(node, dict) or set(node) != {"ref", "dependencies", "rule"}:
                _fail("PROVENANCE_STATE_INVALID", "Invalid provenance node")
            ref, dependencies = _ref(node["ref"]), _refs(node["dependencies"])
            key = _key(ref)
            if key in nodes:
                _fail("PROVENANCE_CONFLICT", "Duplicate provenance node")
            nodes[key] = {"ref": ref, "dependencies": dependencies, "rule": _rule(node["rule"])}
        counts, dependents = {}, {}
        for key, node in nodes.items():
            counts[key] = len(node["dependencies"])
            for dep in node["dependencies"]:
                dep_key = _key(dep)
                if dep_key not in nodes:
                    _fail("DEPENDENCY_PENDING", "Restored dependency is not yet known")
                dependents.setdefault(dep_key, []).append(key)
        ready = deque(sorted(key for key, count in counts.items() if not count))
        result = cls()
        while ready:
            key = ready.popleft()
            node = nodes[key]
            result.add(node["ref"], node["dependencies"], rule=node["rule"])
            for dependent in sorted(dependents.get(key, ())):
                counts[dependent] -= 1
                if not counts[dependent]:
                    ready.append(dependent)
        if len(result._nodes) != len(nodes):
            _fail("DEPENDENCY_CYCLE", "Restored dependencies contain a cycle")
        for event in state["invalidations"]:
            if not isinstance(event, dict) or set(event) != {"targets", "reason", "reducer_seq"}:
                _fail("PROVENANCE_STATE_INVALID", "Invalid invalidation history")
            result.invalidate(event["targets"], reason=event["reason"], reducer_seq=event["reducer_seq"])
        return result
