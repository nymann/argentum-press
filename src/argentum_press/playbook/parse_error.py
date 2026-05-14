# pyright: basic
"""Parse-error playbook driver: orchestrate P0-P9.

Public entry point: :func:`run`. The driver runs:

* P0 — extract failing oracle text + lark error context (orchestrator).
* P1 — keyword-rank candidate parent rules in ``grammar.py``.
* P2 — dump top-3 rule definitions with line numbers.
* P3 — LLM picks which top-3 rule to attach to.
* P4 — LLM writes the Lark alternative.
* P5 — orchestrator splices the new line into ``grammar.py``'s triple-quoted
  string and validates that Lark can still compile the grammar.
* P6 — pytest gate.
* P8/P9 — diagnose + revise + reapply on a red retry.

See ``experiments/playbook-design.html`` section 2b for the full DAG.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import PlaybookResult, context, driver as _driver_mod, edits, llm, log_step
from .lower import L4_MODEL, L5_MODEL, L9_MODEL, _call, _short, run_pytest


P3_MODEL = L4_MODEL
P4_MODEL = L5_MODEL
P8_MODEL = L9_MODEL


def _resolve_models(override: str | None) -> tuple[str, str, str]:
    if override:
        return override, override, override
    return P3_MODEL, P4_MODEL, P8_MODEL


def run(
    *,
    label: str,
    project_dir: Path,
    repo: Path | None = None,
    client: llm.ClientLike | None = None,
    pool: _driver_mod.DriverPool | None = None,
    pytest_runner: Callable[[Path], tuple[int, str]] | None = None,
    card_name: str = "",
    oracle_text: str = "",
    pe_block: str | None = None,
    verbose: bool = True,
    model_override: str | None = None,
) -> PlaybookResult:
    """End-to-end parse-error playbook.

    Returns a :class:`PlaybookResult` with the step trace; never raises on a
    test failure or libcst rejection — outcomes encode the failure mode
    (``aborted-p3``, ``aborted-p4``, ``aborted-p5``, ``aborted-retry-pytest``,
    etc.) so the caller can decide whether to escalate.
    """
    repo = repo or context.REPO
    pytest_runner = pytest_runner or run_pytest
    p3_model, p4_model, p8_model = _resolve_models(model_override)
    steps: list[Any] = []
    result = PlaybookResult(label=label, outcome="pending")

    def _say(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    # ---- P0/P1/P2: context gather ---------------------------------------
    t0 = time.monotonic()
    ctx = context.gather_parse_error(
        label=label,
        project_dir=project_dir,
        oracle_text=oracle_text,
        pe_block=pe_block,
        card_name=card_name,
    )
    log_step(
        steps, "P0+P1+P2", "orch", t0,
        candidates=[
            {"rule": rc.rule.name, "score": rc.score, "overlap": list(rc.overlap)}
            for rc in ctx.candidates
        ],
        candidates_count=len(ctx.candidates),
    )
    _say(
        f"P0+P1+P2: {len(ctx.candidates)} candidate parent rules "
        f"({', '.join(rc.rule.name for rc in ctx.candidates)})"
    )
    if not ctx.candidates:
        result.outcome = "aborted-p1"
        result.steps = steps
        return result

    pe_for_prompt = pe_block or "(no parse-error block)"

    # ---- P3: parent-rule choice (LLM, could-move) -----------------------
    t0 = time.monotonic()
    choice_blocks = llm.build_parse_parent_choice_blocks(
        pe_block=pe_for_prompt,
        candidates_dump=ctx.candidates_dump,
        oracle_text=oracle_text,
    )
    choice_user = (
        "Of the top-3 ranked rules above, which one should the new alternative "
        "attach to? Identify the phrase in the failing oracle text that the "
        "current grammar can't accept (the 'missing phrase'). Return parent_rule "
        "(must be exactly one of the candidate names) + missing_phrase + a "
        "one-sentence rationale."
    )
    try:
        choice_call = _call(
            tool_name="emit_parse_parent_choice",
            system_prompt=llm.PARSE_ERROR_SYSTEM_PROMPT,
            static_context_blocks=choice_blocks,
            user_prompt=choice_user,
            model=p3_model,
            max_tokens=512,
            pool=pool,
            client=client,
        )
    except Exception as e:  # noqa: BLE001
        result.outcome = "aborted-p3"
        log_step(steps, "P3", "llm", t0, error=str(e))
        result.steps = steps
        return result
    choice = choice_call.arguments
    log_step(steps, "P3", "llm", t0, choice=choice)
    _say(f"P3 (LLM): parent_rule={choice['parent_rule']}")

    candidate_names = {rc.rule.name for rc in ctx.candidates}
    if choice["parent_rule"] not in candidate_names:
        result.outcome = "aborted-p3"
        log_step(
            steps, "P3-validate", "orch", time.monotonic(),
            error=f"parent_rule {choice['parent_rule']!r} not in candidates {sorted(candidate_names)!r}",
        )
        result.steps = steps
        return result

    parent_rule_def = next(
        rc.rule for rc in ctx.candidates if rc.rule.name == choice["parent_rule"]
    )
    parent_rule_source = context.dump_rule_definitions([parent_rule_def])
    choice_json = json.dumps(choice, indent=2)

    # ---- P4: write the Lark alternative ---------------------------------
    t0 = time.monotonic()
    alt_blocks = llm.build_parse_alternative_blocks(
        pe_block=pe_for_prompt,
        oracle_text=oracle_text,
        parent_rule_def=parent_rule_source,
        parent_choice_json=choice_json,
    )
    alt_user = (
        f"Emit the new Lark alternative for the {choice['parent_rule']!r} rule "
        f"so the missing phrase parses. Use only literals (double-quoted "
        f"lowercase tokens) and rule names that already appear in the grammar. "
        f"Do NOT include the leading `|` — the orchestrator adds it. Provide a "
        f"label in lowercase rule-name style; null is acceptable if the existing "
        f"alternative naming convention doesn't use one."
    )
    try:
        alt_call = _call(
            tool_name="emit_parse_alternative",
            system_prompt=llm.PARSE_ERROR_SYSTEM_PROMPT,
            static_context_blocks=alt_blocks,
            user_prompt=alt_user,
            model=p4_model,
            max_tokens=512,
            pool=pool,
            client=client,
        )
    except Exception as e:  # noqa: BLE001
        result.outcome = "aborted-p4"
        log_step(steps, "P4", "llm", t0, error=str(e))
        result.steps = steps
        return result
    plan = alt_call.arguments
    log_step(steps, "P4", "llm", t0, plan=plan)
    _say(
        f"P4 (LLM): parent_rule={plan['parent_rule']} "
        f"alternative={_short(plan['alternative_text'], 80)}"
    )

    # ---- P5: splice into grammar.py + validate --------------------------
    t0 = time.monotonic()
    try:
        edit = edits.apply_grammar_alternative(
            grammar_path=context.GRAMMAR,
            parent_rule=plan["parent_rule"],
            alternative_text=plan["alternative_text"],
            label=plan.get("label") or None,
        )
    except edits.AnchorNotFoundError as e:
        result.outcome = "aborted-p5"
        result.final_plan = plan
        log_step(steps, "P5", "orch", t0, error=str(e))
        result.steps = steps
        return result
    log_step(
        steps, "P5", "orch", t0,
        path=str(edit.path.relative_to(repo)),
        inserted_line=edit.inserted_line,
    )
    _say(f"P5: applied grammar alternative at line {edit.inserted_line}")
    result.edit_path = str(edit.path.relative_to(repo))

    # ---- P6: pytest -----------------------------------------------------
    t0 = time.monotonic()
    rc, out = pytest_runner(repo)
    tail = out[-1500:]
    result.pytest_first_tail = tail
    log_step(steps, "P6", "orch", t0, returncode=rc, tail=_short(tail, 200))
    _say(f"P6: pytest rc={rc}")
    if rc == 0:
        result.outcome = "applied"
        result.final_plan = plan
        result.steps = steps
        return result

    # ---- P8: diagnose + revise -----------------------------------------
    t0 = time.monotonic()
    retry_blocks = llm.build_parse_retry_blocks(
        pe_block=pe_for_prompt,
        oracle_text=oracle_text,
        parent_rule_def=parent_rule_source,
        parent_choice_json=choice_json,
        failed_plan_json=json.dumps(plan, indent=2),
        pytest_tail=tail,
    )
    retry_user = (
        "The previous grammar alternative caused pytest to fail. Diagnose the "
        "failure and emit a revised alternative in the same emit_parse_alternative "
        "schema. If the parent rule was wrong, switch to a different candidate "
        "(but keep the rule name from the original P3 candidate set)."
    )
    try:
        retry_call = _call(
            tool_name="emit_parse_alternative",
            system_prompt=llm.PARSE_ERROR_SYSTEM_PROMPT,
            static_context_blocks=retry_blocks,
            user_prompt=retry_user,
            model=p8_model,
            max_tokens=512,
            pool=pool,
            client=client,
        )
    except Exception as e:  # noqa: BLE001
        edits.revert_grammar(edit)
        result.outcome = "aborted-p8"
        result.final_plan = plan
        log_step(steps, "P8", "llm", t0, error=str(e))
        result.steps = steps
        return result
    revised = retry_call.arguments
    log_step(steps, "P8", "llm", t0, plan=revised)
    _say(f"P8 (LLM): revised parent_rule={revised['parent_rule']}")

    # ---- P9: revert + reapply ------------------------------------------
    t0 = time.monotonic()
    edits.revert_grammar(edit)
    try:
        edit = edits.apply_grammar_alternative(
            grammar_path=context.GRAMMAR,
            parent_rule=revised["parent_rule"],
            alternative_text=revised["alternative_text"],
            label=revised.get("label") or None,
        )
    except edits.AnchorNotFoundError as e:
        result.outcome = "aborted-p9"
        result.final_plan = revised
        log_step(steps, "P9", "orch", t0, error=str(e))
        result.steps = steps
        return result
    log_step(steps, "P9", "orch", t0)
    _say("P9: reapplied revised alternative")

    # ---- second pytest -------------------------------------------------
    t0 = time.monotonic()
    rc2, out2 = pytest_runner(repo)
    tail2 = out2[-1500:]
    result.pytest_retry_tail = tail2
    log_step(steps, "P6-retry", "orch", t0, returncode=rc2, tail=_short(tail2, 200))
    _say(f"P6-retry: pytest rc={rc2}")
    if rc2 == 0:
        result.outcome = "applied-after-retry"
        result.final_plan = revised
        result.steps = steps
        return result
    edits.revert_grammar(edit)
    result.outcome = "aborted-retry-pytest"
    result.final_plan = revised
    result.steps = steps
    return result


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Run the parse-error playbook for one label.")
    p.add_argument("--label", required=True, help="e.g. parse-error:<EOF>@t...")
    p.add_argument("--project-dir", required=True, type=Path)
    p.add_argument("--card-name", default="")
    p.add_argument("--oracle-text", default="")
    p.add_argument(
        "--pe-block", default=None,
        help="Pre-formatted ParseErrorDetails block (one of the freeform "
             "prompt inputs). Optional: the playbook degrades gracefully "
             "when only oracle_text is available.",
    )
    p.add_argument("--trace-out", type=Path, default=None)
    p.add_argument("--model-override", default=None)
    args = p.parse_args(argv)

    os.environ.setdefault(
        "ARGENTUM_PARSE_CACHE_DIR", str(context.REPO / ".parse-cache"),
    )

    pe_block = args.pe_block
    if pe_block and Path(pe_block).is_file():
        pe_block = Path(pe_block).read_text(encoding="utf-8")

    result = run(
        label=args.label,
        project_dir=args.project_dir,
        card_name=args.card_name,
        oracle_text=args.oracle_text,
        pe_block=pe_block,
        model_override=args.model_override,
    )
    print()
    print(f"=== outcome: {result.outcome} ===")
    if result.final_plan is not None:
        print()
        print("FINAL PLAN:")
        print(json.dumps(result.final_plan, indent=2))
    if args.trace_out is not None:
        args.trace_out.parent.mkdir(parents=True, exist_ok=True)
        args.trace_out.write_text(result.as_json(), encoding="utf-8")
        print(f"trace written to {args.trace_out}")
    return 0 if result.outcome.startswith("applied") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
