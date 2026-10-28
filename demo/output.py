"""Format demo notices without changing persisted engine data."""
import json
import math
from threading import RLock
from .labels import safe_text
from .terminal import TerminalStatusLine
from .timers import format_timer, format_timer_status

class DemoOutput:
    """Format display messages outside the engine; keep full data in artifacts."""
    def __init__(self, write, *, terminal_stream=None, input_stream=None):
        self.write, self.lock = write, RLock()
        self.terminal = (TerminalStatusLine(terminal_stream, input_stream=input_stream, lock=self.lock)
                         if terminal_stream is not None else None)
        self._live_timers = False
        self._closed = False
        self._timer_views = {}

    def __enter__(self):
        if self.terminal is not None:
            self.terminal.open()
            self._live_timers = self.terminal.enabled
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            if self.terminal is not None:
                self.terminal.close()

    def timer_status(self, views):
        """Refresh the dedicated TTY row; keep plain logs for captured output."""
        with self.lock:
            if self._closed:
                return
            if self._live_timers:
                # A terminal that becomes too small suspends live rendering.
                # Do not replace it with repeated countdown prints over input.
                self.terminal.update(format_timer_status(views))
                return
            current = set()
            for view in views:
                current.add(view["id"])
                remaining = view.get("remaining")
                step = 5 if view["seconds"] > 120 else 1
                bucket = math.ceil(remaining / step) if remaining is not None else None
                signature = (view.get("status"), bucket)
                if self._timer_views.get(view["id"]) != signature:
                    self._timer_views[view["id"]] = signature
                    self(format_timer(view))
            self._timer_views = {key: value for key, value in self._timer_views.items() if key in current}

    def __call__(self,value):
        value=str(value)
        if value.startswith("MOCK_RECORDED "):
            try:
                receipt=json.loads(value[len("MOCK_RECORDED "):])
                if receipt.get("receipt_schema")=="chain-effect-receipt/v0.3":
                    labels={"SET_FLAG":"EHR 표시 활성화", "CLEAR_FLAG":"EHR 표시 해제",
                            "SET_DASHBOARD":"대시보드 활성화", "CLEAR_DASHBOARD":"대시보드 해제"}
                    operation=labels.get(receipt["operation"],receipt["operation"])
                    result="Mock 상태 반영" if receipt["projection_updated"] else "Mock 상태 유지 · 중복/늦은 요청"
                    value=("MOCK_RECORDED | 모의 표시 기록 · 실제 EHR 변경 없음 | "+safe_text(operation)+
                           " · "+safe_text(receipt["target"])+" / "+safe_text(receipt["resource"])+
                           " | "+result+" | "+safe_text(receipt["request_id"]))
                else:
                    value=("MOCK_RECORDED | 모의 알림 기록 · 실제 발송 없음 | "+
                           safe_text(receipt["recipient"])+" · "+safe_text(receipt["template"])+
                           " | "+safe_text(receipt["request_id"]))
            except (ValueError,KeyError,TypeError):pass
        elif value.startswith("TEST_CLOCK_AT | "):
            value+=" | 가상 에피소드 시계"
        with self.lock:self.write(value)
