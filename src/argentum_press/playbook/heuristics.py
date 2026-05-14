# pyright: basic
"""L5a — pick insertion pattern (isinstance-branch vs register-handler).

Two patterns coexist in ``lowerer.py``:

* ``@<dispatcher>.register`` clauses for top-level dispatch (e.g. one per
  Ability subclass, one per effect Expression).
* ``isinstance(stmt, ast.X)`` branches inside helper methods like
  ``_effects_from_statement`` for sub-statement dispatch.

We pick deterministically based on the class name and the exemplar set
collected in L1. The orchestrator can fall back to an LLM call if confidence
is low — currently we always commit to a decision and surface the rationale.
"""
from __future__ import annotations

from dataclasses import dataclass

from .context import LowererExemplars


@dataclass(frozen=True, slots=True)
class PatternChoice:
    """L5a result."""

    pattern: str  # "register-handler" or "isinstance-branch"
    confidence: float  # 0.0 .. 1.0
    rationale: str


_STATEMENT_HELPER_FNS = {"_effects_from_statement"}


def pick_pattern(classname: str, exemplars: LowererExemplars) -> PatternChoice:
    """Decide which insertion pattern to use for a new handler.

    Rules (in priority order):

    1. If ``classname`` already appears as ``ast.X`` in any captured
       isinstance branch → isinstance-branch (high confidence; clearly the
       sibling pattern).
    2. If ``classname`` ends in ``Statement`` AND there is at least one
       isinstance branch inside ``_effects_from_statement`` → isinstance-branch
       (medium-high confidence; the helper exists and the suffix matches).
    3. If ``classname`` ends in ``Ability`` or ``Expression`` → register-handler
       (high confidence; that's the established split).
    4. Otherwise default to register-handler at low confidence — the playbook
       driver may decide to ask the LLM in that case.
    """
    isinstance_ast_classes = {b.ast_class.replace("ast.", "") for b in exemplars.isinstance_branches}
    if classname in isinstance_ast_classes:
        return PatternChoice(
            pattern="isinstance-branch",
            confidence=0.95,
            rationale=f"{classname} already appears as an isinstance branch (sibling pattern)",
        )

    has_effects_helper = any(
        b.function in _STATEMENT_HELPER_FNS for b in exemplars.isinstance_branches
    )

    if classname.endswith("Statement") and has_effects_helper:
        return PatternChoice(
            pattern="isinstance-branch",
            confidence=0.85,
            rationale=(
                f"{classname} ends in Statement and "
                f"_effects_from_statement has existing isinstance branches"
            ),
        )

    if classname.endswith(("Ability", "Expression")):
        return PatternChoice(
            pattern="register-handler",
            confidence=0.9,
            rationale=f"{classname} suffix matches the @register dispatch pattern",
        )

    return PatternChoice(
        pattern="register-handler",
        confidence=0.4,
        rationale=f"{classname} doesn't match a confident pattern; defaulting to @register",
    )
