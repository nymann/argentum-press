"""Reporter — the pipeline's only window onto its progress.

Reporters receive named events as the pipeline runs (phase entries, per-card
status, phase summaries). The Console implementation prints to stdout in a
shape that's readable while the run is in flight. The Null implementation
is the default for tests and for callers that just want the final
PipelineReport.

Why a Reporter abstraction at all? pipeline.py does the orchestration, but
how progress is presented (TTY-aware, JSON-lines, log-only) is a UI concern.
Pushing print() into the pipeline would couple the two; passing a Reporter
keeps them apart at the cost of one extra constructor argument.
"""

from __future__ import annotations

import sys
import time
from typing import Protocol, TextIO

from .outcome import (
    DeferredEmitterGap,
    DeferredParseFailed,
    Emitted,
)


class Reporter(Protocol):
    """All reporter callbacks are no-ops by default — implementations only
    need to override the events they care about. The pipeline calls them in
    the order they appear here."""

    def phase_triage_start(self, set_code: str) -> None: ...
    def phase_triage_fetched(self, total: int, cache_state: str) -> None: ...
    def phase_triage_end(self, *, already_implemented: int, pending: int) -> None: ...

    def phase_classify_start(self, pending: int) -> None: ...
    def card_parse_failed(
        self,
        outcome: DeferredParseFailed,
        *,
        worker_pid: int | None = None,
        elapsed_s: float | None = None,
    ) -> None: ...
    def card_classified_bucket_1(
        self,
        name: str,
        *,
        worker_pid: int | None = None,
        elapsed_s: float | None = None,
    ) -> None: ...
    def card_classified_bucket_2(
        self,
        outcome: DeferredEmitterGap,
        *,
        worker_pid: int | None = None,
        elapsed_s: float | None = None,
    ) -> None: ...
    def phase_classify_end(self, *, bucket_1: int, bucket_2: int, parse_failed: int) -> None: ...

    def phase_emit_start(self, bucket_1: int) -> None: ...
    def card_emitted(self, outcome: Emitted) -> None: ...
    def phase_emit_end(self, emitted: int) -> None: ...

    def phase_verify_start(self) -> None: ...
    def phase_verify_skipped(self, reason: str) -> None: ...
    def phase_verify_passed(self) -> None: ...
    def phase_verify_failed(self, stderr: str) -> None: ...


class NullReporter:
    """No output. Default for tests."""

    def phase_triage_start(self, set_code: str) -> None: pass
    def phase_triage_fetched(self, total: int, cache_state: str) -> None: pass
    def phase_triage_end(self, *, already_implemented: int, pending: int) -> None: pass
    def phase_classify_start(self, pending: int) -> None: pass
    def card_parse_failed(
        self,
        outcome: DeferredParseFailed,
        *,
        worker_pid: int | None = None,
        elapsed_s: float | None = None,
    ) -> None: pass
    def card_classified_bucket_1(
        self,
        name: str,
        *,
        worker_pid: int | None = None,
        elapsed_s: float | None = None,
    ) -> None: pass
    def card_classified_bucket_2(
        self,
        outcome: DeferredEmitterGap,
        *,
        worker_pid: int | None = None,
        elapsed_s: float | None = None,
    ) -> None: pass
    def phase_classify_end(self, *, bucket_1: int, bucket_2: int, parse_failed: int) -> None: pass
    def phase_emit_start(self, bucket_1: int) -> None: pass
    def card_emitted(self, outcome: Emitted) -> None: pass
    def phase_emit_end(self, emitted: int) -> None: pass
    def phase_verify_start(self) -> None: pass
    def phase_verify_skipped(self, reason: str) -> None: pass
    def phase_verify_passed(self) -> None: pass
    def phase_verify_failed(self, stderr: str) -> None: pass


class ConsoleReporter:
    """Prints phase headers, fetched/cached state, per-card classifications,
    and per-phase summaries to a TextIO sink. Tested with a StringIO sink."""

    def __init__(self, sink: TextIO | None = None) -> None:
        self.sink = sink or sys.stdout
        self._pending: int = 0
        self._classified: int = 0
        self._emitted_seen: int = 0
        # Maps worker pid -> a short index (w0, w1, ...) assigned in order
        # of first appearance. Keeps the per-card prefix compact while still
        # letting the user tell workers apart.
        self._worker_indices: dict[int, int] = {}

    # ---- internal ----

    def _print(self, *parts: str) -> None:
        print(*parts, file=self.sink, flush=True)

    def _header(self, label: str) -> None:
        self._print("")
        self._print(f"── {label} ──")

    # ---- triage ----

    def phase_triage_start(self, set_code: str) -> None:
        self._header(f"phase 1: triage  ({set_code})")

    def phase_triage_fetched(self, total: int, cache_state: str) -> None:
        self._print(f"  fetched {total} cards from Scryfall  (cache: {cache_state})")

    def phase_triage_end(self, *, already_implemented: int, pending: int) -> None:
        self._print(f"  already implemented: {already_implemented}")
        self._print(f"  pending:             {pending}")
        self._pending = pending

    # ---- classify ----

    def phase_classify_start(self, pending: int) -> None:
        self._header(f"phase 2: classify  ({pending} cards)")
        self._classified = 0

    def _tick(self, worker_pid: int | None, elapsed_s: float | None) -> str:
        self._classified += 1
        ts = time.strftime("%H:%M:%S")
        parts = [ts]
        if worker_pid is not None:
            parts.append(self._worker_label(worker_pid))
        if elapsed_s is not None:
            parts.append(f"{elapsed_s:5.1f}s")
        return f"  [{' '.join(parts)}]  [{self._classified:>3}/{self._pending:>3}]"

    def _worker_label(self, pid: int) -> str:
        if pid not in self._worker_indices:
            self._worker_indices[pid] = len(self._worker_indices)
        return f"w{self._worker_indices[pid]}"

    def card_parse_failed(
        self,
        outcome: DeferredParseFailed,
        *,
        worker_pid: int | None = None,
        elapsed_s: float | None = None,
    ) -> None:
        self._print(
            f"{self._tick(worker_pid, elapsed_s)} ⚠  parse  {outcome.name}"
            f"  ({outcome.error})"
        )

    def card_classified_bucket_1(
        self,
        name: str,
        *,
        worker_pid: int | None = None,
        elapsed_s: float | None = None,
    ) -> None:
        self._print(f"{self._tick(worker_pid, elapsed_s)} ✓  b1     {name}")

    def card_classified_bucket_2(
        self,
        outcome: DeferredEmitterGap,
        *,
        worker_pid: int | None = None,
        elapsed_s: float | None = None,
    ) -> None:
        self._print(
            f"{self._tick(worker_pid, elapsed_s)} ✗  b2     {outcome.name}"
            f"  (missing {_short_node(outcome.missing_node)})"
        )

    def phase_classify_end(self, *, bucket_1: int, bucket_2: int, parse_failed: int) -> None:
        self._print(f"  bucket 1 (emit-ready):       {bucket_1}")
        self._print(f"  bucket 2 (needs extension):  {bucket_2}")
        self._print(f"  parse failed:                {parse_failed}")

    # ---- emit ----

    def phase_emit_start(self, bucket_1: int) -> None:
        self._header(f"phase 3: emit  ({bucket_1} cards)")
        self._emitted_seen = 0

    def card_emitted(self, outcome: Emitted) -> None:
        self._emitted_seen += 1
        self._print(f"  [{self._emitted_seen:>3}/{self._pending:>3}] wrote {outcome.path}")

    def phase_emit_end(self, emitted: int) -> None:
        self._print(f"  emitted: {emitted}")

    # ---- verify ----

    def phase_verify_start(self) -> None:
        self._header("phase 4: verify")
        self._print("  running ./gradlew :mtg-sets:compileKotlin …")

    def phase_verify_skipped(self, reason: str) -> None:
        self._header("phase 4: verify  (skipped)")
        self._print(f"  {reason}")

    def phase_verify_passed(self) -> None:
        self._print("  BUILD SUCCESSFUL")

    def phase_verify_failed(self, stderr: str) -> None:
        self._print("  BUILD FAILED")
        for line in stderr.splitlines():
            self._print(f"  | {line}")


def _short_node(qualified: str) -> str:
    """Trim a fully qualified class name to its last segment for readability.
    `dev.nymann.foo.Bar` → `Bar`."""
    return qualified.rsplit(".", 1)[-1]


