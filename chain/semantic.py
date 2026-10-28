"""Pure semantic checks for shape-validated fact-authoring documents.

This module resolves declared IDs and statically visible types. It never reads
source files, imports providers, evaluates clinical conditions or creates work.
Installation inspection and executable Bundle generation are separate layers.
"""
from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from .errors import ConfigError


_INITIAL_TYPES = {"site_id": "string", "patient_id": "string", "encounter_id": "string",
                  "episode_id": "string", "age": "integer", "sex": "string",
                  "bed": "string", "arrival_time": "string"}
_NUMERIC = {"number", "integer"}
_OPERATORS = {"all", "any", "not", "eq", "ne", "gte", "in", "is_known",
              "has_status", "requirement"}


def _fail(code, path, message):
    raise ConfigError(code, path, message)


def _require(condition, code, path, message):
    if not condition:
        _fail(code, path, message)


def _has(mapping, key, path):
    _require(key in mapping, "UNDEFINED_REFERENCE", path, f"Undefined reference: {key}")
    return mapping[key]


def _acyclic(graph, path):
    """Check a declared graph without making an execution order out of key order."""
    marks = {}
    for root in sorted(graph):
        if marks.get(root) == 2:
            continue
        marks[root] = 1
        stack = [(root, iter(sorted(graph[root])))]
        while stack:
            name, children = stack[-1]
            target = next(children, None)
            if target is None:
                marks[name] = 2
                stack.pop()
                continue
            _has(graph, target, path)
            if marks.get(target) == 1:
                _fail("DEPENDENCY_CYCLE", path, f"Cyclic dependency involving {target}")
            if marks.get(target) != 2:
                marks[target] = 1
                stack.append((target, iter(sorted(graph[target]))))


def _literal_type(value):
    if value is None:
        return "null"
    return {bool: "boolean", int: "integer", float: "number", str: "string",
            list: "array", dict: "object"}.get(type(value), "unknown")


def _compatible(left, right):
    if "null" in (left, right):
        return False
    return "unknown" in (left, right) or left == right or {left, right} <= _NUMERIC


class _Validator:
    def __init__(self, documents):
        self.c = documents["clinical"]
        self.site = documents["site"]
        self.safety = documents["safety"]
        self.deployment = documents["deployment"]
        self.metadata = documents["metadata"]
        self.facts = self.c["facts"]
        self.tasks = self.c["tasks"]
        self.requirements = self.safety["requirements"]
        self.contexts = self.c["contexts"]
        self.decisions = self.c["decisions"]

    def origins(self, refs, path):
        for index, name in enumerate(refs):
            _has(self.c["origin_refs"], name, f"{path}[{index}]")

    def agent(self, agent_id, mode, path, *, selected):
        metadata = self.metadata["agents"].get(agent_id)
        if not selected and metadata is None:
            return None
        metadata = _has(self.metadata["agents"], agent_id, path + ".agent")
        _has(metadata["modes"], mode, path + ".mode")
        if not selected:
            return metadata
        binding = _has(self.deployment["agents"], agent_id, path + ".agent")
        approval = _has(self.safety["agent_approvals"], agent_id, path + ".agent")
        _require(binding["version"] == metadata["version"]
                 and metadata["version"] in approval["versions"],
                 "AGENT_VERSION_NOT_APPROVED", path + ".agent", "Agent version is not approved")
        _require(set(metadata["actions"]) <= set(approval["actions"])
                 and not set(metadata["actions"]) & set(self.safety["forbidden_actions"]),
                 "ACTION_NOT_APPROVED", path + ".agent", "Agent actions exceed the independent approval")
        _require(set(metadata["data_scopes"]) <= set(approval["data_scopes"]),
                 "SCOPE_NOT_APPROVED", path + ".agent", "Agent data scopes exceed the independent approval")
        return metadata

    def reference(self, reference, path):
        """Return a declared static type; result field types remain unknown."""
        parts = reference.split(".")
        _require(len(parts) >= 2, "UNSUPPORTED_REFERENCE", path, "Expected a namespaced reference")
        namespace, name = parts[:2]
        if namespace == "facts":
            fact = _has(self.facts, name, path)
            _require(len(parts) == 2, "UNSUPPORTED_REFERENCE", path,
                     "Nested Fact fields need a declared field contract")
            return fact["type"]
        if namespace == "milestones":
            milestone = _has(self.c["milestones"], name, path)
            _require(len(parts) == 2, "UNSUPPORTED_REFERENCE", path, "Expected a milestone ID")
            return milestone.get("type", "boolean")
        if namespace == "state_entries":
            _has(self.c["states"], name, path)
            _require(len(parts) == 3 and parts[2] in {"entered", "generation"},
                     "UNSUPPORTED_REFERENCE", path, "Expected a declared State entry field")
            return "boolean" if parts[2] == "entered" else "integer"
        if namespace == "decisions":
            _has(self.decisions, name, path)
            _require(len(parts) == 3 and parts[2] == "value", "UNSUPPORTED_REFERENCE", path,
                     "Only the declared decision value is supported")
            return "string"
        if namespace == "results":
            task = _has(self.tasks, name, path)
            _require(task["kind"] == "agent", "RESULT_CONTRACT_MISMATCH", path,
                     "Result reference must identify an Agent task")
            _require(len(parts) == 3, "UNSUPPORTED_REFERENCE", path,
                     "Nested results need a declared result field contract")
            metadata = self.agent(task["agent"], task["mode"], path, selected=False)
            _require(metadata is not None, "RESULT_CONTRACT_MISMATCH", path,
                     "A referenced result requires module metadata")
            _require(parts[2] in metadata["modes"][task["mode"]]["output_fields"],
                     "RESULT_CONTRACT_MISMATCH", path, "Unknown Agent result field")
            return "unknown"
        _fail("UNSUPPORTED_REFERENCE", path, "Unsupported reference namespace")

    def operand(self, operand, path):
        if "ref" in operand:
            return self.reference(operand["ref"], path + ".ref")
        return _literal_type(operand["literal"])

    def expression(self, expression, path):
        op, value = next(iter(expression.items()))
        _require(op in _OPERATORS, "UNSUPPORTED_OPERATOR", path, "Unsupported condition operator")
        if op in {"all", "any"}:
            for index, child in enumerate(value):
                self.expression(child, f"{path}.{op}[{index}]")
        elif op == "not":
            self.expression(value, path + ".not")
        elif op == "requirement":
            requirement = _has(self.requirements, value, path + ".requirement")
            _require(requirement["kind"] == "predicate", "REQUIREMENT_KIND_MISMATCH", path,
                     "A condition requires a predicate safety requirement")
        elif op in {"is_known", "has_status"}:
            self.reference(value["ref"], path + "." + op + ".ref")
        else:
            left = self.operand(value[0], f"{path}.{op}[0]")
            right = self.operand(value[1], f"{path}.{op}[1]")
            if op == "gte":
                _require(left in _NUMERIC | {"unknown"} and right in _NUMERIC | {"unknown"},
                         "CONDITION_TYPE_MISMATCH", path, "gte requires numeric operands")
            elif op == "in":
                _require("literal" in value[1] and isinstance(value[1]["literal"], list),
                         "CONDITION_TYPE_MISMATCH", path, "in requires an explicit literal list")
                _require(left != "null" and all(_compatible(left, _literal_type(item))
                                               for item in value[1]["literal"]),
                         "CONDITION_TYPE_MISMATCH", path,
                         "Membership items have incompatible declared types; use explicit validity conditions for absence")
            else:
                _require(_compatible(left, right), "CONDITION_TYPE_MISMATCH", path,
                         "Comparison operands have incompatible declared types")

    def projection(self, projection, states, path):
        _has(states, projection["fallback"], path + ".fallback")
        priorities = set()
        for name, rule in projection["rules"].items():
            location = path + ".rules." + name
            priority = rule["priority"]
            _require(type(priority) is int and priority >= 0, "INVALID_PRIORITY", location + ".priority",
                     "Priority must be a nonnegative integer")
            _require(priority not in priorities, "DUPLICATE_PRIORITY", location + ".priority",
                     "Projection priority must be unique")
            priorities.add(priority)
            _has(states, rule["state"], location + ".state")
            self.expression(rule["when"], location + ".when")
            self.origins(rule["origin_refs"], location + ".origin_refs")
            for index, reference in enumerate(rule["requirement_refs"]):
                requirement = _has(self.requirements, reference, f"{location}.requirement_refs[{index}]")
                _require(requirement["kind"] == "predicate", "REQUIREMENT_KIND_MISMATCH", location,
                         "Projection requires predicate safety requirements")

    def selected_tasks(self):
        roots = [(r["task"], "clinical.work_rules." + name + ".task")
                 for name, r in self.c["work_rules"].items()]
        roots += [(t["on_expire"]["id"], "clinical.timers." + name + ".on_expire.id")
                  for name, t in self.c["timers"].items() if t["on_expire"]["kind"] == "task"]
        selected = set()
        pending = list(roots)
        while pending:
            name, path = pending.pop()
            task = _has(self.tasks, name, path)
            if name in selected:
                continue
            selected.add(name)
            for index, predecessor in enumerate(task["after"]):
                pending.append((predecessor, f"clinical.tasks.{name}.after[{index}]"))
        return selected

    def validate(self):
        # Native responses already carry canonical Safety roles. External
        # identity/group conversion belongs to the authenticated backend; no
        # Workflow/Worker currently executes a Site alias mapping.
        for role, aliases in self.site["role_bindings"].items():
            _require(aliases == [role], "ROLE_BINDING_UNSUPPORTED", "site.role_bindings." + role,
                     "Only identity canonical-role declarations are supported; map authenticated roles in the backend")
        for name, definition in self.c["origin_refs"].items():
            path = definition["path"]
            _require(not PurePosixPath(path).is_absolute() and not PureWindowsPath(path).is_absolute()
                     and ".." not in PurePosixPath(path).parts and "\\" not in path,
                     "INVALID_PROVENANCE_PATH", "clinical.origin_refs." + name + ".path",
                     "Origin must be a relative logical source path")
        _require(self.site["site_id"] == self.deployment["site_id"], "SITE_MISMATCH",
                 "deployment.site_id", "Site and Deployment identities differ")
        if self.c["profile"] == "extension-review":
            _require(self.safety["purpose"] == "review-only", "PROFILE_NOT_APPROVED",
                     "safety.purpose", "Extension review is not an executable clinical approval")
        for name, fact in self.facts.items():
            path = "clinical.facts." + name
            self.origins(fact["origin_refs"], path + ".origin_refs")
            if fact["source"] == "initial":
                _require(name in _INITIAL_TYPES, "INVALID_INITIAL_FACT", path,
                         "Initial facts are limited to the eight creation fields")
                _require(fact["type"] == _INITIAL_TYPES[name], "INVALID_INITIAL_FACT", path + ".type",
                         "Initial fact type does not match the creation contract")
            else:
                _has(self.site["fact_bindings"], name, path + ".source")
        for name, bindings in self.site["fact_bindings"].items():
            path = "site.fact_bindings." + name
            fact = _has(self.facts, name, path)
            _require(fact["source"] == "external", "SOURCE_BINDING_MISMATCH", path,
                     "Only external facts have Site source bindings")
            for index, binding in enumerate(bindings):
                location = f"{path}[{index}]"
                _has(self.site["sources"], binding["source"], location + ".source")
                approval = _has(self.safety["query_approvals"], binding["source"], location + ".source")
                _require(name in approval["fields"] and bool(approval["data_scopes"]),
                         "QUERY_SCOPE_NOT_APPROVED", location, "Source field is not approved for query")
        for name, source in self.site["sources"].items():
            _has(self.deployment["source_bindings"], source["binding"], "site.sources." + name + ".binding")
        for name, context in self.contexts.items():
            path = "clinical.contexts." + name
            for index, field in enumerate(context["fields"]):
                _has(self.facts, field, f"{path}.fields[{index}]")
            self.origins(context["origin_refs"], path + ".origin_refs")
        for name, milestone in self.c["milestones"].items():
            path = "clinical.milestones." + name
            self.origins(milestone["origin_refs"], path + ".origin_refs")
            if "when" in milestone:
                self.expression(milestone["when"], path + ".when")
            else:
                reference = milestone["capture_first"]["ref"]
                _require(reference.startswith("results."), "CAPTURE_CONTRACT_MISMATCH", path + ".capture_first",
                         "capture_first records an accepted Agent result")
                kind = self.reference(reference, path + ".capture_first.ref")
                _require(_compatible(kind, milestone["type"]), "CAPTURE_CONTRACT_MISMATCH", path,
                         "Captured value has an incompatible declared type")
        self.projection(self.c["state_projection"], self.c["states"], "clinical.state_projection")
        self.states()
        selected = self.selected_tasks()
        self.validate_tasks(selected)
        self.validate_decisions()
        self.validate_timers()
        for name, requirement in self.requirements.items():
            path = "safety.requirements." + name
            self.origins(requirement["origin_refs"], path + ".origin_refs")
            if requirement["kind"] == "predicate":
                self.expression(requirement["when"], path + ".when")
        life = self.c["lifecycle"]
        lifecycle_states = dict.fromkeys(life["states"])
        _has(lifecycle_states, life["initial"], "clinical.lifecycle.initial")
        _has(lifecycle_states, life["completion"]["state"], "clinical.lifecycle.completion.state")
        self.projection(life["projection"], lifecycle_states, "clinical.lifecycle.projection")
        self.origins(life["origin_refs"], "clinical.lifecycle.origin_refs")
        self.validate_derived_cycles()

    def states(self):
        for name, state in self.c["states"].items():
            path = "clinical.states." + name
            groups = ((state["Entry Trigger"]["projection_rule_refs"], self.c["state_projection"]["rules"], "Entry Trigger.projection_rule_refs"),
                      (state["Incoming Data / Evidence"]["context_refs"], self.contexts, "Incoming Data / Evidence.context_refs"),
                      (state["Required Capability"]["task_refs"], self.tasks, "Required Capability.task_refs"),
                      (state["Timer / Exception / Fail-safe"]["timer_refs"], self.c["timers"], "Timer / Exception / Fail-safe.timer_refs"),
                      (state["Safety / Audit"]["requirement_refs"], self.requirements, "Safety / Audit.requirement_refs"))
            for references, table, suffix in groups:
                for index, reference in enumerate(references):
                    _has(table, reference, f"{path}.{suffix}[{index}]")
            for index, reference in enumerate(state["Required Capability"]["task_refs"]):
                task = self.tasks[reference]
                if task["kind"] == "agent":
                    location = f"{path}.Required Capability.task_refs[{index}]"
                    metadata = self.agent(task["agent"], task["mode"], location, selected=False)
                    _require(metadata is not None, "RESULT_CONTRACT_MISMATCH", location,
                             "A displayed Agent requires module metadata for derived input/output views")
            for index, item in enumerate(state["Data requirement"]):
                _has(self.facts, item["fact"], f"{path}.Data requirement[{index}].fact")
            self.origins(state["Safety / Audit"]["origin_refs"], path + ".Safety / Audit.origin_refs")

    def validate_tasks(self, selected):
        summary_needed = False
        for name, task in self.tasks.items():
            path = "clinical.tasks." + name
            self.origins(task["origin_refs"], path + ".origin_refs")
            if task["identity"]["subject"] == "cause":
                self.reference(task["identity"]["subject_ref"], path + ".identity.subject_ref")
            for index, predecessor in enumerate(task["after"]):
                previous = _has(self.tasks, predecessor, f"{path}.after[{index}]")
                _require(previous["kind"] == "agent", "PREDECESSOR_CONTRACT_MISMATCH",
                         f"{path}.after[{index}]", "after currently requires an accepted Agent result")
            if task["kind"] == "agent":
                metadata = self.agent(task["agent"], task["mode"], path, selected=name in selected)
                if name in selected and metadata is not None:
                    summary_needed |= metadata["requires_summary"]
            if task["kind"] in {"agent", "notify", "effect"}:
                context = _has(self.contexts, task["context"], path + ".context")
                if name in selected:
                    summary_needed |= context["summary"]
            elif task["kind"] == "hitl":
                decision = _has(self.decisions, task["decision"], path + ".decision")
                if name in selected:
                    context = _has(self.contexts, decision["evidence_context"], path + ".decision")
                    summary_needed |= context["summary"]
            if task["kind"] == "notify":
                _has(self.site["recipients"], task["recipient"], path + ".recipient")
                _require(task["template"] in self.safety["notification_templates"], "NOTIFICATION_NOT_APPROVED",
                         path + ".template", "Notification template is not approved")
            elif task["kind"] == "effect":
                _has(self.site["effect_targets"], task["target"], path + ".target")
                _require(task["operation"] in self.safety["allowed_effect_operations"], "EFFECT_NOT_APPROVED",
                         path + ".operation", "Effect operation is not approved")
        _acyclic({name: set(task["after"]) for name, task in self.tasks.items()}, "clinical.tasks")
        for name, rule in self.c["work_rules"].items():
            path = "clinical.work_rules." + name
            _has(self.tasks, rule["task"], path + ".task")
            self.expression(rule["when"], path + ".when")
            self.expression(rule["stop_when"], path + ".stop_when")
            self.origins(rule["origin_refs"], path + ".origin_refs")
        service = self.c["context_service"]
        metadata = self.agent(service["agent"], service["mode"], "clinical.context_service", selected=summary_needed)
        if metadata is not None:
            _require(not metadata["requires_summary"], "RECURSIVE_SUMMARY", "clinical.context_service",
                     "Context service cannot require itself")
            _require("structured_context" in metadata["modes"][service["mode"]]["output_fields"],
                     "SUMMARY_CONTRACT_MISMATCH", "clinical.context_service", "Context service must return structured_context")

    def validate_decisions(self):
        for name, decision in self.decisions.items():
            path = "clinical.decisions." + name
            _has(self.contexts, decision["evidence_context"], path + ".evidence_context")
            requirement = _has(self.requirements, decision["requirement_ref"], path + ".requirement_ref")
            _require(requirement["kind"] == "hitl", "REQUIREMENT_KIND_MISMATCH", path + ".requirement_ref",
                     "Decision requires a HITL safety requirement")
            for option in requirement["confirmations"]:
                _has(decision["options"], option, path + ".requirement_ref")
            for option_name, option in decision["options"].items():
                if option["effect"] != "defer":
                    continue
                location = path + ".options." + option_name + ".resume_timer"
                timer = _has(self.c["timers"], option["resume_timer"], location)
                _require(timer["owner"] == {"kind": "decision", "id": name}
                         and timer["anchor"] == "decision_deferred"
                         and timer["on_expire"] == {"kind": "resume_decision", "id": name},
                         "TIMER_OWNER_MISMATCH", location, "Resume timer must belong to this decision")
            self.origins(decision["origin_refs"], path + ".origin_refs")

    def validate_timers(self):
        for name, timer in self.c["timers"].items():
            path = "clinical.timers." + name
            owner = timer["owner"]
            table = self.c["states"] if owner["kind"] == "state" else self.decisions
            _has(table, owner["id"], path + ".owner.id")
            _require((owner["kind"] == "state" and timer["anchor"] == "state_entered")
                     or (owner["kind"] == "decision" and timer["anchor"] in {"decision_opened", "decision_deferred"}),
                     "TIMER_OWNER_MISMATCH", path + ".anchor", "Timer owner and anchor do not match")
            expiry = timer["on_expire"]
            if expiry["kind"] == "task":
                _has(self.tasks, expiry["id"], path + ".on_expire.id")
            else:
                _has(self.decisions, expiry["id"], path + ".on_expire.id")
                _require(owner["kind"] == "decision" and owner["id"] == expiry["id"],
                         "TIMER_OWNER_MISMATCH", path + ".on_expire.id", "Timer cannot resume another decision")
            self.expression(timer["cancel_when"], path + ".cancel_when")
            self.origins(timer["origin_refs"], path + ".origin_refs")

    def validate_derived_cycles(self):
        def dependencies(expression):
            op, value = next(iter(expression.items()))
            if op in {"all", "any"}:
                return set().union(*(dependencies(child) for child in value))
            if op == "not":
                return dependencies(value)
            if op == "requirement":
                return {"requirement:" + value}
            operands = value if isinstance(value, list) else [value]
            return {"milestone:" + operand["ref"].split(".")[1]
                    for operand in operands if isinstance(operand, dict)
                    and operand.get("ref", "").startswith("milestones.")}

        graph = {"milestone:" + name: dependencies(definition["when"])
                 if "when" in definition else set() for name, definition in self.c["milestones"].items()}
        graph.update({"requirement:" + name: dependencies(definition["when"])
                      for name, definition in self.requirements.items() if definition["kind"] == "predicate"})
        _acyclic(graph, "clinical.milestones")


def validate_semantics(documents):
    """Validate five shape-checked documents, returning None without mutation.

    ``documents`` has clinical/site/safety/deployment/metadata mappings. Failures
    are ConfigError(code, path, message), with a document-qualified path. This
    function checks declarations, not actual clinical values or installation.
    """
    _Validator(documents).validate()
