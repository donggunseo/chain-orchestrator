"""Human-readable views of native Episode Query data; no clinical inference."""
import copy
import json
import math

from chain.compiler import canonical_hash
from chain.hitl_documents import validate_source_documents
from chain.runtime_contracts import validate_snapshot
from demo.labels import (CONFIRMATION_LABELS, OPTION_LABELS, ROLE_LABELS,
    FIELD_LABELS, STATUS_LABELS, label, safe_text, source_text)

from .timers import format_timer


FACT_LABELS = {**FIELD_LABELS, "initial_note": "초진 기록", "ncct_completed": "NCCT 완료",
    "lkw": "마지막 정상 확인 시각", "anticoagulant": "항응고제 정보"}
VALIDITY_LABELS = {**STATUS_LABELS, "KNOWN": "확보", "ABSENT_CONFIRMED": "원천에서 부재 확인"}
CHANGE_LABELS = {"ADDED": "새 자료", "UPDATED": "같은 항목 갱신", "CORRECTED": "정정",
    "RETRACTED": "철회/오류", "CONFLICT": "충돌", "UNCONFIRMED": "미확인"}


def compact(value, limit=180):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
    text = safe_text(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def question_view(snapshot, request_id, definition, *, revision=None):
    """Detach one exact evidence revision and its referenced result sidecars.

    Historical inspection does not make that revision eligible for a response.
    The response form separately requires the latest OPEN question.
    """
    question = copy.deepcopy(snapshot["hitl"]["requests"][request_id])
    revision = question["current_revision"] if revision is None else revision
    if type(revision) is not int or not 1 <= revision <= len(question["revision_ids"]):
        raise ValueError("해당 근거 개정이 없습니다.")
    evidence = copy.deepcopy(snapshot["hitl"]["evidence"][question["revision_ids"][revision - 1]])
    if (evidence["request_id"] != request_id or evidence["revision"] != revision or
            evidence["evidence_id"] != canonical_hash({k: v for k, v in evidence.items() if k != "evidence_id"})):
        raise ValueError("HITL 근거의 식별자·개정·본문이 일치하지 않습니다.")
    validate_snapshot(evidence["snapshot"])
    for key in ("identity", "engine_run_id", "run_pin"):
        if question[key] != evidence[key] or evidence[key] != evidence["snapshot"][key]:
            raise ValueError("다른 Episode/실행의 근거입니다.")
    document_request = {"request_id": request_id, "evidence_revision": revision,
        "evidence_snapshot_id": evidence["snapshot"]["snapshot_id"], "dependencies": evidence["dependencies"]}
    if "source_documents" in evidence:
        validate_source_documents(evidence["source_documents"], document_request, evidence["snapshot"])
    spec = definition["decisions"][question["decision_definition_id"]]
    policy = definition["policy"]["requirements"][spec["requirement_ref"]]
    executions = snapshot.get("executions", {})
    def results(ids):
        return {identifier: copy.deepcopy(executions[identifier]["result"])
                for identifier in ids if identifier in executions and executions[identifier].get("result") is not None}
    return {"question": question, "evidence": evidence, "selected_revision": revision,
        "decision_spec": copy.deepcopy(spec), "requirement": copy.deepcopy(policy),
        "required_results": results(evidence["required_result_ids"]),
        "reference_results": results(evidence["reference_result_ids"]),
        "verifications": {identifier: copy.deepcopy(snapshot.get("verifications", {})[identifier])
                          for identifier in evidence["verification_ids"] if identifier in snapshot.get("verifications", {})}}


def show_facts(facts, write):
    for name, fact in sorted(facts.items()):
        value = compact(fact.get("value")) if fact.get("validity") in {"KNOWN", "ABSENT_CONFIRMED"} else "—"
        write("  " + label(name, FACT_LABELS) + " | " + label(fact["validity"], VALIDITY_LABELS) + " | " + value)
        refs = fact.get("source_refs", [])
        if refs:
            write("    출처: " + "; ".join(source_text(ref) for ref in refs))
        write("    CHAIN 인지: " + safe_text(fact.get("known_at", "미확보")))


def show_evidence(view, write=print):
    q, e = view["question"], view["evidence"]
    write("HITL | " + safe_text(q["decision_definition_id"]) + " | " + safe_text(q["request_id"]) +
          " | 근거 개정 " + str(e["revision"]) + " · " + safe_text(q["status"]))
    write(safe_text(view["decision_spec"]["question"], multiline=True))
    write("판단 자료 | 확보/미확보 상태와 출처를 확인해 주세요.")
    show_facts(e["snapshot"]["facts"], write)
    if e.get("change_set"):
        write("개정 내역 | " + ", ".join(label(row["field"], FACT_LABELS) + ": " +
              CHANGE_LABELS.get(row["kind"], row["kind"]) for row in e["change_set"]))
    unconfirmed = sorted({row["field"] for record in view["verifications"].values()
                          for row in record["coverage"] if row["verification"] == "UNCONFIRMED"})
    if unconfirmed:
        write("최신 원천 확인 미완료 | " + ", ".join(label(name, FACT_LABELS) for name in unconfirmed))
    for identifier, result in view["required_results"].items():
        write("현재 판단 결과 | " + safe_text(result.get("agent_id", identifier)) + " | " + compact(result.get("result")))
    missing_results = sorted(set(e["required_result_ids"]) - set(view["required_results"]))
    if missing_results:
        write("판단 결과 본문 미제공 | " + ", ".join(safe_text(identifier) for identifier in missing_results))
    for identifier, result in view["reference_results"].items():
        write("이전 입력 결과 · 참고용 | " + safe_text(result.get("agent_id", identifier)) + " | " + compact(result.get("result")))
    write("docs: 이 개정의 출처 원문 · json: 판단 Snapshot/결과/원문 전체 · docs/json 번호: 이전 개정 열람")


def show_documents(view, write=print):
    evidence = view["evidence"]
    write("출처 원문 | 근거 개정 " + str(evidence["revision"]) + " | " + safe_text(evidence["request_id"]))
    documents = evidence.get("source_documents")
    if documents is None:
        write("이 개정은 출처 원문 열람자료를 제공하지 않는 이전 형식입니다.")
        return
    if not documents["entries"]:
        write("이 개정에서 참조한 원천 문서가 없습니다. 최신 Context의 문서를 대신 붙이지 않습니다.")
    for entry in documents["entries"]:
        write("  " + source_text(entry["source_ref"]) + " | " + safe_text(entry["status"]))
        if entry["status"] == "AVAILABLE":
            write(safe_text(entry["document"]["value"], multiline=True))
        else:
            write("    사유: " + safe_text(entry.get("reason", entry["status"])))


class FactDemoPresenter:
    """Emit meaningful changed fields once; raw Query remains in artifacts."""
    def __init__(self, definition, write=print):
        self.definition, self.write = copy.deepcopy(definition), write
        self._state = None
        self._facts = {}
        self._milestones = {}
        self._executions = {}
        self._timers = {}
        self._hitl = {}
        self._timer_views = {}

    def observe(self, snapshot, timers=None):
        if not snapshot.get("ready", True):
            return
        state = snapshot.get("state")
        if state != self._state:
            self._state = state
            spec = self.definition["states"].get(state, {})
            self.write("STATE | " + safe_text(state) + " | " + safe_text(spec.get("State", {}).get("name", "")))
        for name, fact in snapshot.get("facts", {}).get("facts", {}).items():
            fingerprint = canonical_hash(fact)
            if self._facts.get(name) != fingerprint:
                self._facts[name] = fingerprint
                if fact["validity"] != "UNKNOWN":
                    value = "구조화 자료 · HITL에서 json으로 열람" if fact.get("value_omitted") else compact(fact.get("value"))
                    self.write("자료 갱신 | " + label(name, FACT_LABELS) + " | " +
                        label(fact["validity"], VALIDITY_LABELS) + " | " + value)
        for name, milestone in (snapshot.get("projection") or {}).get("milestones", {}).items():
            cell = milestone["cell"]
            fingerprint = canonical_hash(cell)
            if self._milestones.get(name) != fingerprint:
                self._milestones[name] = fingerprint
                self.write("MILESTONE | " + safe_text(name) + " | " + label(cell["validity"], VALIDITY_LABELS) +
                           " | " + compact(cell["value"]))
        for identifier, execution in snapshot.get("executions", {}).items():
            signature = (execution.get("attempt_no"), execution.get("result_use"), bool(execution.get("result")))
            if self._executions.get(identifier) == signature:
                continue
            self._executions[identifier] = signature
            request = execution["request"]
            result = execution.get("result")
            name = request.get("agent_id", request.get("kind", "작업"))
            usage = execution.get("result_use")
            status = "실행 중" if result is None else "실패" if result["status"] == "FAILED" else (
                "이전 입력 결과 · 참고용" if usage and usage.startswith("REFERENCE_") else "현재 결과 수락")
            if request.get("kind") == "hitl" and result is None:
                status = "의료진 요청 준비/응답 대기"
            self.write("TASK | " + safe_text(name) + " | " + status + " | " + safe_text(identifier))
            if result and result["status"] == "FAILED":
                self.write("  오류: " + safe_text(result.get("error_code", "명시적 실행 실패")))
            elif result and isinstance(result.get("receipt"), dict):
                self.write("  MOCK_RECORDED | " + safe_text(result["receipt"].get("status")) + " · 실제 외부 전달 없음")
        for identifier, record in snapshot.get("timers", {}).items():
            status = record["status"]
            if status != self._timers.get(identifier):
                self._timers[identifier] = status
                if status != "ARMED":
                    self.write("TIMER_" + safe_text(status) + " | " + safe_text(record["payload"]["timer_definition_id"]))
        for identifier, question in snapshot.get("hitl", {}).get("requests", {}).items():
            signature = (question["status"], question["current_revision"])
            if self._hitl.get(identifier) == signature:
                continue
            previous = self._hitl.get(identifier)
            self._hitl[identifier] = signature
            self.write("HITL_STATUS | " + safe_text(question["decision_definition_id"]) + " | " +
                       safe_text(question["status"]) + " | 근거 개정 " + str(question["current_revision"]))
            if (snapshot.get("query_schema") != "chain-fact-progress/v1" and question["current_revision"]
                    and (previous is None or previous[1] != question["current_revision"])):
                show_evidence(question_view(snapshot, identifier, self.definition), self.write)
        status_writer = getattr(self.write, "timer_status", None)
        if timers is not None and callable(status_writer):
            # Send the complete set, including [] when the last timer ends.
            # The output owns TTY rendering; the presenter never edits input.
            status_writer(timers)
            return
        for timer in timers or []:
            remaining = timer.get("remaining")
            step = 5 if timer["seconds"] > 120 else 1
            bucket = math.ceil(remaining / step) if remaining is not None else None
            signature = (timer.get("status"), bucket)
            if self._timer_views.get(timer["id"]) != signature:
                self._timer_views[timer["id"]] = signature
                self.write(format_timer(timer))
