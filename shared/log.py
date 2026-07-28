"""
Timestamped log output.

sync.log used to be bare lines like `[local_sync] nothing to do` with no
time attached. When a real question came up — "did the sync actually run
overnight while the Mac was closed?" — the log couldn't answer it, and
diagnosing it needed `pmset -g log` and file mtimes instead. A log that
can't tell you *when* something happened is barely a log.

Rather than touch every print() in the codebase, this wraps stdout and
stderr once per process so every line picks up a timestamp — including
lines printed by libraries we don't control.

Timestamps are in Peter's timezone, not UTC, so a line in the log lines
up with what his phone showed him.
"""

import atexit
import sys

from . import timeutil

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class _TimestampedStream:
    """
    Line-buffering proxy that prefixes each completed line with the time.

    Buffers partial writes because print() emits the text and the newline
    as separate calls — prefixing on every write would put a timestamp in
    the middle of a line.
    """

    def __init__(self, stream):
        self._stream = stream
        self._partial = ""

    def write(self, text: str) -> int:
        self._partial += text
        while "\n" in self._partial:
            line, _, self._partial = self._partial.partition("\n")
            stamp = timeutil.now().strftime(TIMESTAMP_FORMAT)
            self._stream.write(f"{stamp}  {line}\n")
        return len(text)

    def flush(self):
        # Flush any trailing text that never got its newline, so a crash
        # mid-line doesn't swallow the last thing written.
        if self._partial:
            stamp = timeutil.now().strftime(TIMESTAMP_FORMAT)
            self._stream.write(f"{stamp}  {self._partial}\n")
            self._partial = ""
        self._stream.flush()

    def isatty(self) -> bool:
        return self._stream.isatty()

    def fileno(self) -> int:
        return self._stream.fileno()


def install():
    """Wrap stdout and stderr. Safe to call more than once."""
    if not isinstance(sys.stdout, _TimestampedStream):
        sys.stdout = _TimestampedStream(sys.stdout)
    if not isinstance(sys.stderr, _TimestampedStream):
        sys.stderr = _TimestampedStream(sys.stderr)
    # The interpreter flushes the original streams at shutdown, not
    # these wrappers, so a trailing partial line would be lost.
    atexit.register(_flush_all)


def _flush_all():
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, _TimestampedStream):
            stream.flush()
