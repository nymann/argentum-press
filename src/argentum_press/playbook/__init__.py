"""Structured playbooks that replace freeform fix-loop iterations.

Three playbooks live under this package, one per gap kind:

* :mod:`argentum_press.playbook.lower` for ``kind="lower"`` (handler-missing).
* :mod:`argentum_press.playbook.parse_error` for ``parse-error:`` (Lark
  rejected the preprocessed text).
* :mod:`argentum_press.playbook.unmodeled_rule` for ``unmodeled-rule:`` (Lark
  parsed but the transformer has no method for that rule).

Each playbook is a fixed sequence of orchestrator steps interleaved with a
small number of tool-use-constrained LLM calls; see
``experiments/playbook-design.html`` for the per-step DAG. Drivers return a
common :class:`PlaybookResult` so the strategy layer can treat them
uniformly.

Public surface:

* :class:`PlaybookResult`, :class:`StepLog` — shared per-driver outputs.
* :func:`argentum_press.playbook.lower.run` — lower-gap driver.
* :func:`argentum_press.playbook.parse_error.run` — parse-error driver.
* :func:`argentum_press.playbook.unmodeled_rule.run` — unmodeled-rule driver.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class StepLog:
    """One step's input/output snapshot for the trace TSV / debug dump."""

    name: str
    kind: str  # "orch" / "llm" / "cache" / "heuristic"
    payload: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0


@dataclass(slots=True)
class PlaybookResult:
    """Common result shape across all three playbook drivers.

    ``outcome`` follows the per-driver step ids (``aborted-l4``,
    ``aborted-p5``, ``aborted-u6``, ``applied``, ``applied-after-retry``,
    ``aborted-retry-pytest``). Each driver fills in its own steps + plan.
    """

    label: str
    outcome: str
    steps: list[StepLog] = field(default_factory=list)
    final_plan: dict[str, Any] | None = None
    pytest_first_tail: str = ""
    pytest_retry_tail: str = ""
    edit_path: str | None = None

    def as_json(self) -> str:
        return json.dumps(
            {
                "label": self.label,
                "outcome": self.outcome,
                "final_plan": self.final_plan,
                "pytest_first_tail": self.pytest_first_tail,
                "pytest_retry_tail": self.pytest_retry_tail,
                "edit_path": self.edit_path,
                "steps": [
                    {
                        "name": s.name,
                        "kind": s.kind,
                        "duration_s": round(s.duration_s, 3),
                        "payload": s.payload,
                    }
                    for s in self.steps
                ],
            },
            indent=2,
        )


def log_step(
    steps: list[StepLog], name: str, kind: str, t0: float, **payload: Any
) -> None:
    """Append a :class:`StepLog` to ``steps`` with duration measured from ``t0``."""
    steps.append(
        StepLog(name=name, kind=kind, payload=payload, duration_s=time.monotonic() - t0)
    )


__all__ = ["PlaybookResult", "StepLog", "log_step"]
