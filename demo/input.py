"""Cancellable terminal line transport, independent of the orchestrator."""
import asyncio
import os
import select
import sys
from threading import Event

class StdinReader:
    """One buffered pipe/TTY consumer with selectable, cancellable async reads."""
    def __init__(self, stop=None, write=print):
        self.stop = stop if stop is not None else Event()
        self.write = write;self.buffer = b"";self.eof = False

    def _line(self):
        if b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            return line.decode(sys.stdin.encoding or "utf8").rstrip("\r")
        if self.eof:
            if self.buffer:
                line, self.buffer = self.buffer, b""
                return line.decode(sys.stdin.encoding or "utf8")
            raise EOFError
        return None

    def _available(self, timeout):
        if select.select([sys.stdin.fileno()], [], [], timeout)[0]:
            data = os.read(sys.stdin.fileno(), 4096)
            if data:self.buffer += data
            else:self.eof = True

    def __call__(self, prompt):
        self.write(prompt)
        while not self.stop.is_set():
            line = self._line()
            if line is not None:return line
            self._available(.1)
        raise EOFError

    async def read_async(self, prompt):
        self.write(prompt)
        while not self.stop.is_set():
            line = self._line()
            if line is not None:return line
            self._available(0)
            await asyncio.sleep(.01)
        raise EOFError

class _InputLines:
    """Own exactly one line task, including while a wait form is replaced."""
    def __init__(self, input_fn, write):
        self.input_fn = StdinReader(write=write) if input_fn is input else input_fn
        self.write = write;self.task = None;self.prompt = None;self.discard_next = False

    def start(self, prompt):
        if self.task is None:
            self.prompt = prompt
            reader = getattr(self.input_fn, "read_async", None)
            self.task = asyncio.create_task(reader(prompt) if reader else asyncio.to_thread(self._read_blocking, prompt))
        elif prompt != self.prompt:
            self.prompt = prompt
            if not self.discard_next:self.write(prompt)

    def _read_blocking(self, prompt):
        try:return self.input_fn(prompt)
        except (EOFError, KeyboardInterrupt):raise _InputClosed from None

    def take(self):
        task, self.task = self.task, None
        self.prompt = None
        return task.result()

    async def discard_actor_input(self):
        if self.task is None:return
        if getattr(self.input_fn, "read_async", None):
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None;self.prompt = None
        else:
            self.discard_next = True
            self.write("이전 재요청의 역할/ID 입력은 전송하지 않습니다. 해당 입력이 끝나면 새 선택 안내를 표시합니다.")

    async def close(self):
        if self.task is not None:
            # Cancels selectable stdin immediately. An injected blocking callback
            # may finish later; no replacement read is started after shutdown.
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

class _InputClosed(Exception):
    """Carry injected input interruption without interrupting the event loop."""
