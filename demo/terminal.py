"""One ANSI footer below ordinary logs and canonical terminal input.

The last row is reserved before input starts; lines 1..height-1 keep scrolling.
Keeping the scroll region at row 1 preserves ordinary terminal scrollback.
No stdin bytes or terminal input modes are changed. Pipes and terminals without
usable dimensions remain untouched. Any resize ends live rendering for this
session rather than clearing a row that might now contain reflowed input.
"""
from __future__ import annotations

import os
import sys
from threading import RLock
import unicodedata


_SAVE = "\x1b7"
_RESTORE = "\x1b8"
_FULL_REGION = "\x1b[r"


def _status_row(rows):
    return f"\x1b[{rows};1H\x1b[2K"


def _clusters(text):
    """Produce safe, conservative display cells without splitting combining marks."""
    clusters = []
    for char in str(text):
        category = unicodedata.category(char)
        if category in {"Cc", "Cs"}:
            char, category = " ", "Zs"
        if category in {"Mn", "Me", "Cf"} or unicodedata.combining(char):
            if clusters:
                clusters[-1] += char
            continue
        clusters.append(char)
    return clusters


def _cluster_width(cluster):
    # Ambiguous-width glyphs use two cells deliberately: an overestimate only
    # clips earlier, while underestimating can wrap into the input area.
    width = 2 if unicodedata.east_asian_width(cluster[0]) in {"W", "F", "A"} else 1
    if "\ufe0f" in cluster or "\U0001f1e6" <= cluster[0] <= "\U0001f1ff":
        width = max(width, 2)
    return width


def display_width(text):
    """Return a conservative terminal-cell width, treating control bytes as spaces."""
    return sum(_cluster_width(cluster) for cluster in _clusters(text))


def clip_status_text(text, width):
    """Make one safe line of at most width cells, with a clipped-content marker."""
    width = max(0, int(width))
    clusters = _clusters(text)
    if sum(_cluster_width(cluster) for cluster in clusters) <= width:
        return "".join(clusters)
    marker = "…" if width >= 2 else "." if width else ""
    available = width - display_width(marker)
    result, used = [], 0
    for cluster in clusters:
        cells = _cluster_width(cluster)
        if used + cells > available:
            break
        result.append(cluster)
        used += cells
    return "".join(result) + marker


class TerminalStatusLine:
    """TTY-only display lifecycle; never read or replace the user's input buffer."""
    def __init__(self, stream, input_stream=None, lock=None):
        self.stream = stream
        self.input_stream = sys.stdin if input_stream is None else input_stream
        self.lock = RLock() if lock is None else lock
        self._enabled = False
        self._closed = False
        self._dimensions = None
        self._last_rendered = None

    def _size(self):
        try:
            size = os.get_terminal_size(self.stream.fileno())
            return size.columns, size.lines
        except (AttributeError, OSError, ValueError):
            return None

    @staticmethod
    def _tty(stream):
        try:
            return bool(stream.isatty())
        except (AttributeError, OSError, ValueError):
            return False

    @property
    def supported(self):
        term = os.environ.get("TERM", "").strip().lower()
        size = self._size()
        return (term not in {"", "dumb", "unknown"}
                and self._tty(self.stream) and self._tty(self.input_stream)
                and size is not None and size[0] >= 2 and size[1] >= 3)

    @property
    def enabled(self):
        return self._enabled

    def _write(self, value):
        try:
            self.stream.write(value)
            self.stream.flush()
            return True
        except (OSError, ValueError, UnicodeError):
            # A flush can fail after bytes changed the scroll region. Make one
            # best-effort restoration without clearing any possible input row.
            try:
                self.stream.write(_SAVE + _FULL_REGION + _RESTORE)
                self.stream.flush()
            except (OSError, ValueError, UnicodeError):
                pass
            self._enabled = False
            self._closed = True
            return False

    def open(self):
        with self.lock:
            if self._closed or self._enabled or not self.supported:
                return self
            size = self._size()
            if size is None or size[0] < 2 or size[1] < 3:
                return self
            self._dimensions = size
            # Input has not started yet. Put its future cursor on the final
            # body row; the dedicated footer must stay outside normal output.
            # The body starts at physical row 1 so scrolling retains history.
            frame = ("\n\n" + f"\x1b[1;{size[1] - 1}r\x1b[{size[1] - 1};1H"
                     + _SAVE + _status_row(size[1]) + _RESTORE)
            self._enabled = self._write(frame)
            return self

    def _finish(self, *, clear):
        frame = _SAVE + _FULL_REGION
        if clear:
            frame += _status_row(self._dimensions[1])
        self._write(frame + _RESTORE)
        self._enabled = False
        self._closed = True

    def update(self, text):
        with self.lock:
            if not self._enabled or self._closed:
                return False
            size = self._size()
            if size != self._dimensions:
                # Reflow can put partial canonical input on the former footer,
                # even after a width-only resize. Do not clear or reposition it.
                # Restore scrolling and do not restart this live session.
                self._finish(clear=False)
                return False
            text = clip_status_text(text, size[0] - 1)
            signature = size, text
            if signature == self._last_rendered:
                return False
            frame = _SAVE
            frame += _status_row(size[1]) + text + _RESTORE
            written = self._write(frame)
            if written:
                self._last_rendered = signature
            return written

    def close(self):
        with self.lock:
            if self._closed:
                return
            if self._enabled:
                size = self._size()
                self._finish(clear=size == self._dimensions)
            else:
                self._closed = True

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()


TimerStatusLine = TerminalStatusLine
