"""Read-only Clinical timer display; never advance time or fire a timer."""
from datetime import datetime

from chain.runtime_contracts import timestamp
from demo.labels import duration, safe_text


def timer_views(snapshot, *, now, verified=True, clock_kind="LOCAL_WALL"):
    """Use the engine's admitted owner/due time and the runner's actual clock.

    A Query can still report ARMED after the displayed countdown reaches zero.
    That means processing is pending, not that a TimerFired event was observed.
    """
    instant = timestamp(now) if isinstance(now, str) else now
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise ValueError("Timer display requires an aware actual clock")
    result = []
    for identifier, record in snapshot.get("timers", {}).items():
        if record.get("status") != "ARMED":
            continue
        payload = record["payload"]
        total = (timestamp(payload["due_at"]) - timestamp(record["anchor_at"])).total_seconds()
        remaining = max(0.0, (timestamp(payload["due_at"]) - instant).total_seconds()) if verified else None
        result.append({"id": identifier, "timer_definition_id": payload["timer_definition_id"],
            "owner": payload["owner"], "label": "의료진 재요청" if payload["purpose"] == "HITL_RETRY" else "지연 알림",
            "seconds": total, "original_seconds": total, "remaining": remaining,
            "clock_kind": clock_kind, "status": "UNVERIFIED" if not verified else "OVERDUE" if remaining == 0 else "WAITING"})
    return result


def format_timer(view, *, width=12):
    width = max(4, min(30, int(width)))
    prefix = "TIMER | " + safe_text(view.get("timer_definition_id", view["id"])) + " | " + safe_text(view["label"])
    prefix += " | " + {"TEMPORAL_DURABLE": "Temporal 실제 시간", "LOCAL_WALL": "Local 실제 시간"}.get(
        view.get("clock_kind"), safe_text(view.get("clock_kind", "시계 미확인")))
    if view.get("remaining") is None or view.get("status") == "UNVERIFIED":
        return prefix + " | 현재 상태 확인 지연"
    total, remaining = view["seconds"], view["remaining"]
    fraction = max(0, min(1, (total - remaining) / total)) if total > 0 else 0
    filled = int(fraction * width)
    bar = "■" * filled + "□" * (width - filled)
    suffix = "만료 처리 대기" if remaining == 0 else "대기 중"
    original = view.get("original_seconds", total)
    original_label = " · 원래 설정 " + duration(original) if original != total else ""
    return prefix + f" | [{bar}] 남은 {duration(remaining)} / 전체 {duration(total)} · {suffix}" + original_label


def format_timer_status(views):
    """Compact all currently observed timers for a separate terminal row."""
    parts = []
    for view in sorted(views, key=lambda value: str(value["id"])):
        remaining = view.get("remaining")
        status = ("현재 상태 확인 지연" if remaining is None or view.get("status") == "UNVERIFIED" else
                  "만료 처리 대기" if remaining == 0 else duration(remaining))
        name = safe_text(view.get("timer_definition_id", view["id"]))
        parts.append(name + " " + status)
    return "TIMER | " + " | ".join(parts) if parts else ""
