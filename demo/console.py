"""Native HITL console. Key presses become the same commands a backend sends.

This is demo I/O only: Query evidence is copied, never amended or approved by
the display. A newer revision discards the in-progress form and its old input.
"""
import asyncio
import copy
from datetime import datetime, timezone
import json
from uuid import uuid4

from demo.input import _InputClosed, _InputLines
from demo.labels import CONFIRMATION_LABELS, OPTION_LABELS, ROLE_LABELS, label, safe_text

from .presentation import question_view, show_documents, show_evidence


def _clock():
    return datetime.now(timezone.utc).isoformat()


def _actor_form(roles, write):
    while True:
        write("역할: " + ", ".join(str(index + 1) + "=" + label(role, ROLE_LABELS)
                                    for index, role in enumerate(roles)))
        role = (yield "모의 의료진 역할 번호/코드: ").strip().upper()
        if role.isdigit() and 1 <= int(role) <= len(roles):
            role = roles[int(role) - 1]
        if role in roles:
            break
        write("설정에서 허용한 역할을 입력해 주세요.")
    staff = (yield "모의 의료진 ID [DEMO-CLINICIAN]: ").strip() or "DEMO-CLINICIAN"
    return {"role": role, "staff_id": staff}


def decision_form(view, *, write=print, clock=_clock):
    question, evidence = view["question"], view["evidence"]
    if question["status"] != "OPEN" or evidence["revision"] != question["current_revision"]:
        raise ValueError("현재 열린 HITL의 최신 근거로만 답변할 수 있습니다.")
    show_evidence(view, write)
    options = list(view["decision_spec"]["options"])
    while True:
        for index, option in enumerate(options):
            write(str(index + 1) + ". " + label(option, OPTION_LABELS))
        choice = (yield "선택 번호/코드 (docs/json=근거 열람, q=입력 종료): ").strip().upper()
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            choice = options[int(choice) - 1]
        aliases = {"YES": "THROMBOLYSIS_YES", "NO": "THROMBOLYSIS_NO"}
        if choice not in options and aliases.get(choice) in options:
            choice = aliases[choice]
        if choice not in options:
            write("설정에 있는 선택지를 입력해 주세요.")
            continue
        confirmations = view["requirement"].get("confirmations", {}).get(choice, [])
        confirmed = []
        for name in confirmations:
            answer = (yield label(name, CONFIRMATION_LABELS) + "? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                break
            confirmed.append(name)
        if len(confirmed) != len(confirmations):
            write("필수 확인사항이 완료되지 않아 전송하지 않았습니다.")
            continue
        break
    actor = yield from _actor_form(view["requirement"]["roles"], write)
    while True:
        comment = (yield "결정/보류 사유 (필수): ").strip()
        if comment:
            break
        write("사유를 입력해 주세요.")
    return {"contract_schema": "chain-fact-hitl-decision/v1",
        **{key: copy.deepcopy(question[key]) for key in ("identity", "engine_run_id", "run_pin", "request_id")},
        "decision_id": "DEMO-DECISION-" + uuid4().hex, "evidence_revision": evidence["revision"],
        "evidence_id": evidence["evidence_id"], "evidence_snapshot_id": evidence["snapshot"]["snapshot_id"],
        "decision": choice, "actor": actor, "confirmed_items": confirmed,
        "evidence_viewed": [evidence["evidence_id"]], "comment": comment, "recorded_at": clock()}


def resume_form(question, requirement, *, failed=False, write=print, clock=_clock):
    write("HITL_WAITING | " + safe_text(question["request_id"]) + " | 현재 상태를 유지합니다.")
    if failed:
        write("새 HITL 근거 준비에 실패했습니다. 준비 상태를 확인한 뒤 ready로 다시 요청할 수 있습니다.")
    write("타이머 만료 전에도 ready로 재요청할 수 있습니다. 독립 Event와 Agent 응답은 계속 처리됩니다.")
    while True:
        answer = (yield "보류 중: ready=지금 다시 결정하기, docs/json=보류 근거, q=입력 종료: ").strip().lower()
        if answer == "ready":
            break
        write("ready 또는 q를 입력해 주세요.")
    actor = yield from _actor_form(requirement["roles"], write)
    return {"contract_schema": "chain-fact-hitl-resume/v1",
        **{key: copy.deepcopy(question[key]) for key in ("identity", "engine_run_id", "run_pin")},
        "command_id": "DEMO-RESUME-" + uuid4().hex, "prior_request_id": question["request_id"],
        "actor": actor, "requested_at": clock()}


def waiting_question(snapshot):
    """Find deferred ownership from actual retry timers, never State names."""
    questions = snapshot.get("hitl", {}).get("requests", {})
    latest = {}
    for question in questions.values():
        name = question["decision_definition_id"]
        if name not in latest or question["cycle_no"] > latest[name]["cycle_no"]:
            latest[name] = question
    candidates = []
    for record in snapshot.get("timers", {}).values():
        payload = record["payload"]
        if payload["purpose"] != "HITL_RETRY" or record["status"] not in {"ARMED", "FIRED"}:
            continue
        prior = questions.get(payload["owner"]["id"])
        if not prior or prior["status"] != "DECIDED":
            continue
        candidates.append((prior, record))
    for prior, record in sorted(candidates, key=lambda pair: pair[0]["cycle_no"], reverse=True):
        current = latest[prior["decision_definition_id"]]
        failed = current["status"] == "PREPARATION_FAILED" and current["cycle_no"] == prior["cycle_no"] + 1
        if (current["request_id"] == prior["request_id"] and record["status"] == "ARMED") or failed:
            return {"question": copy.deepcopy(prior), "failed": failed}
    return None


async def _discard_line(lines, write):
    if lines.task is None:
        return
    if getattr(lines.input_fn, "read_async", None):
        lines.task.cancel()
        await asyncio.gather(lines.task, return_exceptions=True)
        lines.task = None
        lines.prompt = None
    else:
        lines.discard_next = True
        write("근거 변경 전 입력은 사용하지 않습니다. 입력 중인 한 줄을 끝내면 최신 자료의 선택으로 돌아갑니다.")


def _closed(write):
    write("INPUT_CLOSED_NO_DECISION | 콘솔 입력을 종료했습니다. 이 종료 입력으로 새 응답·재요청·Workflow 취소를 전송하지 않았습니다.")


async def serve_console(query, submit, *, definition, resume=None, input_fn=input, write=print,
                        clock=_clock, poll_interval=.1, on_request=None, detail=None):
    if poll_interval <= 0:
        raise ValueError("Positive console poll interval required")
    lines = _InputLines(input_fn, write)
    form = None
    binding = None
    view = None
    prompt = None
    awaiting = {}
    observed_receipts = set()
    try:
        while True:
            snapshot = await query()
            for row in snapshot.get("ingress_receipts", []):
                key = row.get("decision_id") if row.get("kind") == "decision" else row.get("command_id")
                if key not in awaiting or key in observed_receipts:
                    continue
                observed_receipts.add(key)
                kind, _, choice = awaiting.pop(key)
                receipt = row["receipt"]
                status = receipt["status"]
                write(("DECISION_" if kind == "decision" else "RESUME_") + safe_text(status) + " | " +
                      safe_text(key) + " | " + safe_text(receipt.get("code", "")))
                if kind == "decision" and status == "ACCEPTED" and choice and choice["effect"] == "defer":
                    write("결정을 보류했습니다. 설정된 타이머 또는 ready 재요청을 기다립니다.")
                if status == "REJECTED" and receipt.get("code") == "EVIDENCE_REVISION_MISMATCH":
                    write("근거가 갱신되어 응답이 거절되었습니다. 최신 자료를 확인한 뒤 다시 답변해 주세요.")
            if snapshot.get("termination_phase") == "FINALIZED":
                return
            requests = snapshot.get("hitl", {}).get("requests", {})
            blocked = {item[1] for item in awaiting.values()}
            selected = next((q for q in requests.values() if q["status"] == "OPEN" and q["request_id"] not in blocked), None)
            wait = waiting_question(snapshot) if resume else None
            if wait and wait["question"]["request_id"] in blocked:
                wait = None
            wanted = ("decision", selected["request_id"], selected["current_revision"]) if selected else (
                ("resume", wait["question"]["request_id"], wait["question"]["current_revision"], wait["failed"]) if wait else None)
            if wanted != binding:
                previous = binding
                if form:
                    form.close()
                    await _discard_line(lines, write)
                form = None
                binding = wanted
                view = None
                if previous and previous[0] == "decision":
                    write("HITL_EVIDENCE_UPDATED | 이전 입력을 전송하지 않았습니다. 최신 근거를 확인하고 처음부터 답변해 주세요.")
                if wanted:
                    q = selected if selected else wait["question"]
                    exact = await detail(q["request_id"], q["current_revision"]) if detail else snapshot
                    exact_question = exact["hitl"]["requests"][q["request_id"]]
                    if (exact_question["current_revision"] != q["current_revision"] or
                            exact_question["status"] != q["status"]):
                        # A detail Query can observe a newer revision than the
                        # progress poll. Never bind old inputs to that revision.
                        binding = None
                        await asyncio.sleep(poll_interval)
                        continue
                    view = question_view(exact, q["request_id"], definition, revision=q["current_revision"])
                    if wanted[0] == "decision":
                        if on_request:
                            on_request(copy.deepcopy(q))
                        form = decision_form(view, write=write, clock=clock)
                    else:
                        form = resume_form(q, view["requirement"], failed=wait["failed"], write=write, clock=clock)
                    prompt = next(form)
                else:
                    prompt = "근거 준비/처리 중 (q=콘솔 입력 종료): "
            if prompt is None:
                prompt = "외부 Event 또는 HITL 대기 중 (q=콘솔 입력 종료): "
            lines.start(prompt)
            if lines.task is not None and lines.task.done():
                try:
                    answer = lines.take()
                except (EOFError, KeyboardInterrupt, _InputClosed):
                    _closed(write)
                    return
                if not isinstance(answer, str):
                    write("텍스트 한 줄을 입력해 주세요.")
                    continue
                if answer.strip().lower() == "q":
                    _closed(write)
                    return
                if lines.discard_next:
                    lines.discard_next = False
                    await asyncio.sleep(poll_interval)
                    continue
                tokens = answer.strip().lower().split()
                if view and tokens and tokens[0] in {"docs", "json"}:
                    try:
                        revision = int(tokens[1]) if len(tokens) == 2 else view["selected_revision"]
                        if len(tokens) > 2:
                            raise ValueError("docs/json 뒤에는 개정 번호 하나만 입력해 주세요.")
                        request_id = view["question"]["request_id"]
                        available = snapshot["hitl"]["requests"][request_id]["revision_ids"]
                        if not 1 <= revision <= len(available):
                            raise ValueError("해당 근거 개정이 없습니다.")
                        exact = await detail(request_id, revision) if detail else snapshot
                        inspected = question_view(exact, request_id, definition, revision=revision)
                        if tokens[0] == "docs":
                            show_documents(inspected, write)
                        else:
                            write("[개정별 원문] evidence.snapshot=판단 근거 / evidence.source_documents=별도 열람 원문")
                            write(json.dumps(inspected, ensure_ascii=False, indent=2, sort_keys=True))
                    except ValueError as error:
                        write(safe_text(error))
                    continue
                if form is None:
                    write("아직 열린 결정 요청이 없습니다. 최신 자료를 준비하고 있습니다.")
                    continue
                try:
                    prompt = form.send(answer)
                except StopIteration as result:
                    command = result.value
                    if binding[0] == "decision":
                        await submit(command)
                        key = command["decision_id"]
                        awaiting[key] = ("decision", command["request_id"], view["decision_spec"]["options"][command["decision"]])
                        write("DECISION_SUBMITTED_AWAITING_ENGINE | " + safe_text(key) + " | 전송됨 · 엔진 수락 확인 대기")
                    else:
                        await resume(command)
                        key = command["command_id"]
                        awaiting[key] = ("resume", command["prior_request_id"], None)
                        write("RESUME_SUBMITTED_AWAITING_ENGINE | " + safe_text(key) + " | 전송됨 · 엔진 수락 확인 대기")
                    form = None
                    binding = None
                    view = None
                    prompt = "응답/재요청 처리 중 (q=콘솔 입력 종료): "
            await asyncio.sleep(poll_interval)
    finally:
        if form:
            form.close()
        await lines.close()
