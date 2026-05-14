# pyright: basic
"""Long-lived ``claude`` CLI driver for the playbook.

Why this exists: the SDK path in :mod:`argentum_press.playbook.llm` hits
``api.anthropic.com`` directly with the OAuth bearer beta, which is a
different rate-limit envelope from the one ``claude -p`` (the freeform
fix-loop's transport) traverses. The same subscription that finishes
freeform iterations next door 429s the SDK path persistently — see the
post-mortem in the 2026-05 A/B race trace. Routing playbook LLM calls
through ``claude -p``'s stream-json interface collapses the asymmetry.

The driver holds **one long-lived** ``claude`` subprocess per model and
talks to it over stdin/stdout in stream-json. Each playbook step writes
one user turn and drains events until ``type=result``. Between gaps the
orchestrator calls :meth:`DriverPool.forget_all`, which terminates and
restarts the underlying processes — equivalent to ``/clear`` (stream-json
mode has no in-band clear in current ``claude`` builds, and a fresh
process is the cheapest way to reset).

Ported from ``hivedrone``'s ``ClaudeCliDriver`` (Java); the shape is
intentionally mirror-image so debugging insights transfer.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


_STREAM_END = "__STREAM_END__"


class DriverError(RuntimeError):
    """Raised when the driver can't get a usable result from ``claude``."""


@dataclass(slots=True)
class AttemptResult:
    """One round-trip's outcome.

    ``assistant_text`` is the concatenated text from all assistant turns
    (free text + the final ``result`` event's ``result`` field if present).
    ``raw_events`` is the full NDJSON event list for debugging.
    """

    assistant_text: str
    raw_events: list[dict]
    wall_s: float


class ClaudeCliDriver:
    """One persistent ``claude --input-format stream-json`` subprocess.

    Construct once per model; reuse across many playbook steps. Call
    :meth:`forget` to wipe conversation state (kill + restart). Call
    :meth:`close` at end of life.

    Tests inject a shim via ``claude_cmd`` (list of argv strings) — the
    shim must speak the same NDJSON stream-json wire format on stdout.
    """

    def __init__(
        self,
        *,
        model: str,
        working_dir: Path | None = None,
        claude_cmd: list[str] | None = None,
        idle_timeout_s: int = 600,
    ) -> None:
        self._model = model
        self._working_dir = working_dir or Path.cwd()
        self._claude_cmd = claude_cmd
        self._idle_timeout_s = idle_timeout_s
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._start()

    @property
    def model(self) -> str:
        return self._model

    def _start(self) -> None:
        cmd = self._claude_cmd or [
            "claude",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--model", self._model,
        ]
        env = os.environ.copy()
        # Match hivedrone's setting; lets us pick up partial stream_event
        # deltas if claude is configured to emit them.
        env.setdefault("CLAUDE_CODE_INCLUDE_PARTIAL_MESSAGES", "1")
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(self._working_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._queue = queue.Queue()
        self._reader = threading.Thread(
            target=_pump_stdout,
            args=(self._proc, self._queue),
            name=f"claude-reader[{self._model}]",
            daemon=True,
        )
        self._reader.start()

    def attempt(self, user_prompt: str) -> AttemptResult:
        """Send one user turn and collect events until ``type=result``."""
        if self._proc is None or self._proc.poll() is not None:
            raise DriverError("claude subprocess is not running")

        # Drain any stale events (e.g. from a previous attempt that ended
        # in an error and left tail bytes in the queue).
        self._drain_stale()

        payload = {
            "type": "user",
            "message": {"role": "user", "content": user_prompt},
        }
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise DriverError(f"writing to claude stdin failed: {e}") from e

        t0 = time.monotonic()
        events: list[dict] = []
        assistant_text_chunks: list[str] = []

        while True:
            try:
                line = self._queue.get(timeout=self._idle_timeout_s)
            except queue.Empty as e:
                self._terminate()
                raise DriverError(
                    f"claude idle timeout after {self._idle_timeout_s}s"
                ) from e

            if line == _STREAM_END:
                # stdout closed before result event — claude died.
                stderr_tail = self._read_stderr_tail()
                raise DriverError(
                    f"claude exited before emitting a result event "
                    f"(model={self._model}). stderr tail: {stderr_tail}"
                )

            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                # Non-JSON output (debug print, etc.) — skip but record.
                events.append({"_raw_non_json": stripped})
                continue
            events.append(event)

            etype = event.get("type", "")
            if etype == "assistant":
                for block in event.get("message", {}).get("content", []) or []:
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        if text:
                            assistant_text_chunks.append(text)
            elif etype == "result":
                # Final event. ``result`` field carries the canonical text
                # for SDKResultSuccess; fall back to accumulated chunks.
                result_text = event.get("result")
                if isinstance(result_text, str) and result_text:
                    final_text = result_text
                else:
                    final_text = "".join(assistant_text_chunks)
                wall_s = time.monotonic() - t0
                return AttemptResult(
                    assistant_text=final_text,
                    raw_events=events,
                    wall_s=wall_s,
                )

    def forget(self) -> None:
        """Drop conversation history by killing + restarting the process."""
        self._terminate()
        self._start()

    def close(self) -> None:
        self._terminate()

    # ----- internals -----------------------------------------------------

    def _drain_stale(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _terminate(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin is not None:
                try:
                    self._proc.stdin.close()
                except OSError:
                    pass
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
        finally:
            self._proc = None
            # Reader thread will exit on EOF; it's daemon so we don't join.

    def _read_stderr_tail(self) -> str:
        if self._proc is None or self._proc.stderr is None:
            return "(no stderr)"
        try:
            return self._proc.stderr.read()[-1000:]
        except OSError:
            return "(stderr unreadable)"


def _pump_stdout(proc: subprocess.Popen, q: queue.Queue[str]) -> None:
    """Reader thread: copies stdout lines onto the queue."""
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            q.put(line.rstrip("\n"))
    except (OSError, ValueError):
        pass
    finally:
        q.put(_STREAM_END)


class DriverPool:
    """Lazy per-model driver cache.

    The playbook uses up to two models per gap (Haiku for L3, Opus for
    L4+). The pool spawns each on first use and reuses them for the
    lifetime of the fix-loop run. :meth:`forget_all` is the
    between-gaps reset hook; :meth:`close` is end-of-run cleanup.
    """

    def __init__(
        self,
        *,
        working_dir: Path | None = None,
        claude_cmd: list[str] | None = None,
        idle_timeout_s: int = 600,
    ) -> None:
        self._working_dir = working_dir
        self._claude_cmd = claude_cmd
        self._idle_timeout_s = idle_timeout_s
        self._drivers: dict[str, ClaudeCliDriver] = {}

    def get(self, model: str) -> ClaudeCliDriver:
        drv = self._drivers.get(model)
        if drv is None:
            drv = ClaudeCliDriver(
                model=model,
                working_dir=self._working_dir,
                claude_cmd=self._claude_cmd,
                idle_timeout_s=self._idle_timeout_s,
            )
            self._drivers[model] = drv
        return drv

    def forget_all(self) -> None:
        for drv in self._drivers.values():
            drv.forget()

    def close(self) -> None:
        for drv in self._drivers.values():
            drv.close()
        self._drivers.clear()


__all__ = [
    "AttemptResult",
    "ClaudeCliDriver",
    "DriverError",
    "DriverPool",
]
