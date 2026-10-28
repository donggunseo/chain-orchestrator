"""Display labels and value formatting; no clinical inference."""
import math

FIELD_LABELS = {
    "patient_id": "환자 ID", "encounter_id": "내원 ID", "episode_id": "에피소드 ID",
    "site_id": "기관 ID", "age": "나이", "sex": "성별", "bed": "병상",
    "arrival_time": "도착 시각", "lkw": "마지막 정상 확인 시각",
    "glucose": "혈당", "sbp": "수축기 혈압", "dbp": "이완기 혈압",
    "weight_kg": "체중", "platelet_count": "혈소판 수", "inr": "INR",
    "nihss": "NIHSS", "anticoagulant": "항응고제 정보",
    "ct_order_id": "CT 오더 ID", "ncct_order_id": "NCCT 결과의 오더 ID",
    "ncct_completed": "NCCT 완료 정보", "screening_result": "Screening 결과",
    "assessment": "의사결정지원 결과", "mode": "실행 모드", "mock_only": "Mock 출력",
    "basis": "결과 근거", "missing_information": "미확보 항목",
}

STATUS_LABELS = {
    "AVAILABLE": "확보", "COMPLETED": "자료 완료", "UNKNOWN": "미확보",
    "PENDING": "결과 대기", "RETRACTED": "철회", "ERROR": "오류",
    "CONFLICT": "출처 간 충돌", "INVALIDATED": "무효화", "NO_EVIDENCE": "근거 없음",
    "UNCONFIRMED": "미확인", "CONFIRMED": "확인됨",
    "PHYSICIAN_READ_REQUIRED": "의료진 직접 판독 필요",
}

OPTION_LABELS = {
    "PROCEED_TO_CT": "CT 경로 진행 승인", "NOT_STROKE_PATHWAY": "해당 경로 종료",
    "DEFER": "결정 보류 · 현재 상태 유지",
    "THROMBOLYSIS_YES": "YES · 시행 계획 승인",
    "THROMBOLYSIS_NO": "NO · 시행 계획 없이 종료",
    "HOLD": "결정 보류 · 현재 상태 유지",
}

ROLE_LABELS = {"NEUROLOGIST": "신경과 의료진", "EM_PHYSICIAN": "응급의학과 의료진"}

CONFIRMATION_LABELS = {
    "NCCT_NO_HEMORRHAGE_PHYSICIAN_READ": "NCCT를 직접 판독하고 출혈 없음 확인",
    "LKW_RECONFIRMED_WITH_FAMILY": "보호자와 마지막 정상 확인 시각 재확인",
    "BP_RECHECK_BELOW_185_110": "혈압 재측정 및 185/110 미만 확인",
    "NO_ANTICOAGULANT_RECONFIRMED": "항응고제 복용 없음 재확인",
}

CLOCK_LABELS = {
    "LOCAL_SYNTHETIC": "Local 가상 시계", "LOCAL_WALL": "Local 실제 시계",
    "TEMPORAL_DURABLE": "실제 Temporal 타이머",
}

def safe_text(value, *, multiline=False):
    """Escape terminal control characters without altering the stored value."""
    text = str(value)
    return "".join(char if (ord(char) >= 32 and ord(char) != 127) or (multiline and char == "\n")
                   else "\\n" if char == "\n" else f"\\x{ord(char):02x}" for char in text)

def label(code, labels=FIELD_LABELS):
    code = safe_text(code)
    return f"{labels[code]} ({code})" if code in labels else code

def value_text(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return safe_text(value)

def source_text(ref):
    parts = [safe_text(ref.get("system", "?")), safe_text(ref.get("record_id", "?")),
             "v" + safe_text(ref.get("version", "?"))]
    if "field" in ref:
        parts.append(safe_text(ref["field"]))
    return " · ".join(parts)

def duration(seconds):
    total = max(0, math.ceil(float(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
