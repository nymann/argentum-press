# pyright: basic
"""Lower-gap playbook driver: orchestrate L0-L10.

Public entry point: :func:`run`. The driver knows which steps are
orchestrator (free) vs LLM (paid) and threads context between them. Pytest
is the gate; on red we get one retry via L9/L10.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import PlaybookResult, StepLog, cache, context, driver as _driver_mod, edits, heuristics, llm, log_step


# Pytest invocation reused across the success path and the retry. We mirror
# the targets the freeform fix-loop uses so a green run here means a green
# run there.
PYTEST_TARGETS = (
    "tests/test_diagnose.py",
    "tests/test_pipeline.py",
    "tests/test_lowerer.py",
    "tests/test_classify.py",
)


# L3 runs on a local MLX-hosted Qwen3-Coder-Next-4bit (non-thinking
# variant — the thinking flavours break structured JSON output). The
# summary is small + bounded; local is free and avoids the rate-limit
# envelope. The MLX server is the OpenAI-compatible llm router on
# http://localhost:8080. Model names containing "/" route there
# automatically (see llm._is_local_model).
L3_MODEL = "mlx-community/Qwen3-Coder-Next-4bit"

# Sonnet 4.6 for the structured-code-emission and picker steps (L4 / L5)
# and their cross-playbook aliases (P3 picker, U3/U4 code emission). Opus
# is reserved for "new grammar" (P4) and retries-on-pytest-red (L9 / P8 /
# U8) where the reasoning lift earns the cost.
L4_MODEL = "claude-sonnet-4-6"
L5_MODEL = "claude-sonnet-4-6"
L9_MODEL = "claude-opus-4-7"


def _resolve_models(override: str | None) -> tuple[str, str, str, str]:
    """Resolve (L3, L4, L5, L9) model ids.

    ``override`` (when set) replaces L4 / L5 / L9; L3 stays on haiku because
    it's the cached cheap-summary step. Useful when opus is rate-limited on
    the subscription and the demo needs to keep moving.
    """
    if override:
        return L3_MODEL, override, override, override
    return L3_MODEL, L4_MODEL, L5_MODEL, L9_MODEL


# ---------------------------------------------------------------------------
# Pytest runner (mirror of scripts/fix_parser_gaps.py:run_pytest)
# ---------------------------------------------------------------------------


def run_pytest(repo: Path) -> tuple[int, str]:
    proc = subprocess.run(
        ["uv", "run", "pytest", *PYTEST_TARGETS, "-x", "-q", "-n", "auto"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# Live-card classify gate (L7b / L10b)
# ---------------------------------------------------------------------------
#
# pytest accepts dead-code additions to lowerer.py (a new `if isinstance(...):
# return "..."` after an unreachable `return` line, for instance — exactly
# the misfire that produced three commits in a row for Common Crook's
# DiesExpression). The live-card classify gate runs *after* pytest in a
# fresh subprocess (singledispatch's caching makes in-process re-import
# unreliable for the lowerer) and checks whether the originating card
# still produces the same lower:<ast-class> gap. If yes, the edit was a
# no-op for live input — revert and surface aborted-classify-unchanged so
# the strategy chain can fall back to freeform.


def _live_card_still_failing_lower(
    *, card_name: str, oracle_text: str, label: str
) -> bool:
    """True iff a fresh classify of ``card_name`` still produces ``label``.

    Returns False on parse_error / clean classify / unexpected
    classifications — anything *other* than "still the same lower gap"
    counts as progress (different gap kind = the loop will pick it up
    next iteration; clean classify = fully fixed).

    Returns True on subprocess crashes too: a crash post-edit means the
    edit is structurally wrong, and we'd rather revert + escalate than
    commit and let the next iteration's gap scan die.
    """
    if not card_name or not oracle_text:
        return False
    probe = (
        "import json, sys\n"
        "from argentum_press.parser import parse\n"
        "from argentum_press.lowerer import KotlinLowerer\n"
        "from argentum_press.classify import classify, Bucket1, Bucket2\n"
        f"r = parse({oracle_text!r}, name={card_name!r})\n"
        "if r.error is not None:\n"
        "    print(json.dumps({'result': 'parse-error', 'label': r.error.message}))\n"
        "    sys.exit(0)\n"
        "try:\n"
        "    c = classify(r.ast, KotlinLowerer())\n"
        "except BaseException as exc:\n"
        "    print(json.dumps({'result': 'crash', 'exception': type(exc).__name__, 'message': str(exc)}))\n"
        "    sys.exit(0)\n"
        "if isinstance(c, Bucket1):\n"
        "    print(json.dumps({'result': 'ok'}))\n"
        "else:\n"
        "    print(json.dumps({'result': 'bucket2', 'missing_node': c.missing_node}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=context.REPO,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ},  # preserve ARGENTUM_PARSE_CACHE etc.
    )
    if proc.returncode != 0:
        return True
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return True
    if out["result"] == "bucket2" and out.get("missing_node") == label:
        return True
    if out["result"] == "crash":
        return True
    return False


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


_log_step = log_step  # legacy alias retained for in-module call sites


def _short(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[: n - 3] + "..."


def _call(
    *,
    tool_name: str,
    system_prompt: str,
    static_context_blocks: list,
    user_prompt: str,
    model: str,
    max_tokens: int,
    pool: _driver_mod.DriverPool | None,
    client: llm.ClientLike | None,
) -> llm.ToolCallResult:
    """Route to CLI driver, SDK, or local OpenAI server based on transport.

    Routing precedence:

    * ``client`` is set → SDK path with that client (test mode; the
      scripted client owns every LLM turn regardless of model name).
    * ``model`` is a local namespace (contains ``/``) → local OpenAI-
      compatible POST to ``http://localhost:8080``. Used in production
      for L3's cheap summary; can be overridden by injecting a client
      from tests.
    * ``pool`` is set → CLI-backed claude subprocess.
    * Otherwise → fresh Anthropic SDK client.
    """
    if client is not None:
        return llm.call_tool(
            tool_name=tool_name,
            system_prompt=system_prompt,
            static_context_blocks=static_context_blocks,
            user_prompt=user_prompt,
            model=model,
            client=client,
            max_tokens=max_tokens,
        )
    if llm._is_local_model(model):
        return llm.call_tool_via_local_openai(
            tool_name=tool_name,
            system_prompt=system_prompt,
            static_context_blocks=static_context_blocks,
            user_prompt=user_prompt,
            model=model,
            max_tokens=max_tokens,
        )
    if pool is not None:
        return llm.call_tool_via_cli(
            tool_name=tool_name,
            system_prompt=system_prompt,
            static_context_blocks=static_context_blocks,
            user_prompt=user_prompt,
            pool=pool,
            model=model,
        )
    return llm.call_tool(
        tool_name=tool_name,
        system_prompt=system_prompt,
        static_context_blocks=static_context_blocks,
        user_prompt=user_prompt,
        model=model,
        client=None,
        max_tokens=max_tokens,
    )


def run(
    *,
    label: str,
    project_dir: Path,
    repo: Path | None = None,
    client: llm.ClientLike | None = None,
    pool: _driver_mod.DriverPool | None = None,
    pytest_runner: Callable[[Path], tuple[int, str]] | None = None,
    cache_root: Path | None = None,
    card_name: str = "",
    oracle_text: str = "",
    ast_text: str | None = None,
    verbose: bool = True,
    model_override: str | None = None,
) -> PlaybookResult:
    """End-to-end lower-gap playbook.

    Returns a :class:`PlaybookResult` with the step trace; never raises on a
    test failure or libcst rejection — outcomes encode the failure mode so the
    caller can decide whether to escalate.
    """
    repo = repo or context.REPO
    pytest_runner = pytest_runner or run_pytest
    l3_model, l4_model, l5_model, l9_model = _resolve_models(model_override)
    steps: list[StepLog] = []
    result = PlaybookResult(label=label, outcome="pending")

    def _say(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    # ---- L0: extract AST class ------------------------------------------
    t0 = time.monotonic()
    try:
        ctx = context.gather(
            label=label,
            project_dir=project_dir,
            card_name=card_name,
            oracle_text=oracle_text,
            ast_text=ast_text,
        )
    except ValueError as e:
        result.outcome = "aborted-l0"
        _log_step(steps, "L0", "orch", t0, error=str(e))
        result.steps = steps
        return result
    _log_step(
        steps, "L0", "orch", t0,
        ast_class=ctx.ast_class.classname,
        path=str(ctx.ast_class.path.relative_to(repo)),
        fields=list(ctx.ast_class.fields),
    )
    _say(f"L0: located {ctx.ast_class.classname} in {ctx.ast_class.path.name}")

    # ---- L1: exemplars (already collected by context.gather, just trace) ---
    _log_step(
        steps, "L1", "orch", time.monotonic(),
        register_handlers=len(ctx.exemplars.register_handlers),
        isinstance_branches=len(ctx.exemplars.isinstance_branches),
    )
    _say(
        f"L1: {len(ctx.exemplars.register_handlers)} @register handlers, "
        f"{len(ctx.exemplars.isinstance_branches)} isinstance branches"
    )

    # ---- L2: engine hints (also already in ctx) -------------------------
    _log_step(
        steps, "L2", "orch", time.monotonic(),
        engine_hints_chars=len(ctx.engine_hints or ""),
    )

    # ---- L3: summary (cacheable) ----------------------------------------
    t0 = time.monotonic()
    summary = cache.get(ctx.ast_class.source, root=cache_root)
    if summary is None:
        exemplar_text = _exemplars_for_summary(ctx)
        blocks = llm.build_summary_blocks(ctx.ast_class.source, exemplar_text)
        user_prompt = (
            f"Summarise the AST class {ctx.ast_class.classname} for the playbook. "
            f"Return a one-sentence summary, the MTG term it represents, and "
            f"2-3 existing handler names from the exemplars that look most "
            f"similar."
        )
        try:
            call = _call(
                tool_name="emit_ast_summary",
                system_prompt=llm.SYSTEM_PROMPT,
                static_context_blocks=blocks,
                user_prompt=user_prompt,
                model=l3_model,
                max_tokens=512,
                pool=pool,
                client=client,
            )
            summary = call.arguments
            cache.put(ctx.ast_class.source, summary, root=cache_root)
            _log_step(steps, "L3", "llm", t0, cache="miss", summary=summary)
            _say(f"L3 (LLM): {_short(summary['summary'], 100)}")
        except Exception as e:  # noqa: BLE001
            result.outcome = "aborted-l3"
            _log_step(steps, "L3", "llm", t0, error=str(e))
            result.steps = steps
            return result
    else:
        _log_step(steps, "L3", "cache", t0, cache="hit", summary=summary)
        _say(f"L3 (cache hit): {_short(summary['summary'], 100)}")

    summary_json = json.dumps(summary, indent=2)

    # ---- L4: strategy ---------------------------------------------------
    t0 = time.monotonic()
    strategy_blocks = llm.build_strategy_blocks(
        summary_json=summary_json,
        engine_hints=ctx.engine_hints or "(no engine hints found)",
        ast_class_source=ctx.ast_class.source,
    )
    strategy_user = (
        f"Decide the strategy for handling {ctx.ast_class.classname}. Pick "
        f"'full' if argentum-engine has a clear DSL surface (cite the engine "
        f"hits in target_dsl_symbol), 'stub' if the AST shape is too thin to "
        f"emit a real call (e.g. fields are optional/None in practice), or "
        f"'sub-gap' if the handler should delegate to an inner emitter that "
        f"will surface its own next gap."
    )
    try:
        strat_call = _call(
            tool_name="emit_strategy",
            system_prompt=llm.SYSTEM_PROMPT,
            static_context_blocks=strategy_blocks,
            user_prompt=strategy_user,
            model=l4_model,
            max_tokens=512,
            pool=pool,
            client=client,
        )
    except Exception as e:  # noqa: BLE001
        result.outcome = "aborted-l4"
        _log_step(steps, "L4", "llm", t0, error=str(e))
        result.steps = steps
        return result
    strategy = strat_call.arguments
    strategy_json = json.dumps(strategy, indent=2)
    _log_step(steps, "L4", "llm", t0, strategy=strategy)
    _say(
        f"L4 (LLM): strategy={strategy['strategy']} target={strategy['target_dsl_symbol']}"
    )

    # ---- L5a: pattern heuristic -----------------------------------------
    t0 = time.monotonic()
    pattern_choice = heuristics.pick_pattern(ctx.ast_class.classname, ctx.exemplars)
    _log_step(
        steps, "L5a", "heuristic", t0,
        pattern=pattern_choice.pattern,
        confidence=pattern_choice.confidence,
        rationale=pattern_choice.rationale,
    )
    _say(
        f"L5a (heuristic): pattern={pattern_choice.pattern} "
        f"(confidence={pattern_choice.confidence:.2f})"
    )

    # ---- L5b: emit handler body + anchor --------------------------------
    pattern_exemplars = "\n\n---\n\n".join(
        list(context.filter_exemplars_for_pattern(ctx.exemplars, pattern_choice.pattern))[:8]
    )
    plan_blocks = llm.build_plan_blocks(
        summary_json=summary_json,
        strategy_json=strategy_json,
        pattern=pattern_choice.pattern,
        ast_class_source=ctx.ast_class.source,
        pattern_exemplars=pattern_exemplars,
    )
    plan_user = _plan_user_prompt(ctx, strategy, pattern_choice.pattern)

    t0 = time.monotonic()
    try:
        plan_call = _call(
            tool_name="emit_plan",
            system_prompt=llm.SYSTEM_PROMPT,
            static_context_blocks=plan_blocks,
            user_prompt=plan_user,
            model=l5_model,
            max_tokens=2048,
            pool=pool,
            client=client,
        )
    except Exception as e:  # noqa: BLE001
        result.outcome = "aborted-l5"
        _log_step(steps, "L5b", "llm", t0, error=str(e))
        result.steps = steps
        return result
    plan = plan_call.arguments
    _log_step(steps, "L5b", "llm", t0, plan=plan)
    _say(f"L5b (LLM): anchor.pattern={plan['anchor']['pattern']}")

    # ---- L6: apply edit -------------------------------------------------
    t0 = time.monotonic()
    try:
        edit = edits.apply_plan(plan, context.LOWERER)
    except edits.AnchorNotFoundError as e:
        result.outcome = "aborted-l6"
        result.final_plan = plan
        _log_step(steps, "L6", "orch", t0, error=str(e))
        result.steps = steps
        return result
    _log_step(steps, "L6", "orch", t0, path=str(edit.path.relative_to(repo)))
    _say(f"L6: applied edit to {edit.path.name}")
    result.edit_path = str(edit.path.relative_to(repo))

    # ---- L7: pytest -----------------------------------------------------
    t0 = time.monotonic()
    rc, out = pytest_runner(repo)
    tail = out[-1500:]
    result.pytest_first_tail = tail
    _log_step(steps, "L7", "orch", t0, returncode=rc, tail=_short(tail, 200))
    _say(f"L7: pytest rc={rc}")

    if rc == 0:
        # ---- L7b: live-card classify gate ------------------------------
        # pytest is the unit-test gate; it doesn't actually classify the
        # card that produced the gap. A lowerer edit that's syntactically
        # fine but semantically a no-op (dead branch, wrong AST class
        # pattern, etc.) passes pytest and dead-ends the live classifier
        # next iteration — that's how we ended up with four redundant
        # DiesExpression commits before the no_progress check fired.
        # Re-classify the originating card here so we catch this in-
        # playbook and revert before committing.
        t0 = time.monotonic()
        live_failed = _live_card_still_failing_lower(
            card_name=card_name, oracle_text=oracle_text, label=label,
        )
        _log_step(
            steps, "L7b", "orch", t0,
            card_name=card_name, label=label, still_failing=live_failed,
        )
        _say(f"L7b: live-card classify still_failing={live_failed}")
        if live_failed:
            edits.revert(edit)
            result.outcome = "aborted-classify-unchanged"
            result.final_plan = plan
            result.steps = steps
            return result
        result.outcome = "applied"
        result.final_plan = plan
        result.steps = steps
        return result

    # ---- L9: diagnose + revise -----------------------------------------
    t0 = time.monotonic()
    retry_blocks = llm.build_retry_blocks(
        summary_json=summary_json,
        strategy_json=strategy_json,
        pattern=pattern_choice.pattern,
        ast_class_source=ctx.ast_class.source,
        pattern_exemplars=pattern_exemplars,
        failed_plan_json=json.dumps(plan, indent=2),
        pytest_tail=tail,
    )
    retry_user = (
        "The previous plan failed pytest. Diagnose the failure from the "
        "tail output and emit a revised plan in the same emit_plan schema. "
        "Keep the same anchor pattern unless the failure clearly indicates "
        "the pattern itself was wrong."
    )
    try:
        retry_call = _call(
            tool_name="emit_plan",
            system_prompt=llm.SYSTEM_PROMPT,
            static_context_blocks=retry_blocks,
            user_prompt=retry_user,
            model=l9_model,
            max_tokens=2048,
            pool=pool,
            client=client,
        )
    except Exception as e:  # noqa: BLE001
        edits.revert(edit)
        result.outcome = "aborted-l9"
        result.final_plan = plan
        _log_step(steps, "L9", "llm", t0, error=str(e))
        result.steps = steps
        return result
    revised = retry_call.arguments
    _log_step(steps, "L9", "llm", t0, plan=revised)
    _say(f"L9 (LLM): revised anchor.pattern={revised['anchor']['pattern']}")

    # ---- L10: revert + reapply -----------------------------------------
    t0 = time.monotonic()
    edits.revert(edit)
    try:
        edit = edits.apply_plan(revised, context.LOWERER)
    except edits.AnchorNotFoundError as e:
        result.outcome = "aborted-l10"
        result.final_plan = revised
        _log_step(steps, "L10", "orch", t0, error=str(e))
        result.steps = steps
        return result
    _log_step(steps, "L10", "orch", t0)
    _say("L10: reapplied revised plan")

    # ---- second pytest -------------------------------------------------
    t0 = time.monotonic()
    rc2, out2 = pytest_runner(repo)
    tail2 = out2[-1500:]
    result.pytest_retry_tail = tail2
    _log_step(steps, "L7-retry", "orch", t0, returncode=rc2, tail=_short(tail2, 200))
    _say(f"L7-retry: pytest rc={rc2}")
    if rc2 == 0:
        t0 = time.monotonic()
        live_failed2 = _live_card_still_failing_lower(
            card_name=card_name, oracle_text=oracle_text, label=label,
        )
        _log_step(
            steps, "L7b-retry", "orch", t0,
            card_name=card_name, label=label, still_failing=live_failed2,
        )
        _say(f"L7b-retry: live-card classify still_failing={live_failed2}")
        if live_failed2:
            edits.revert(edit)
            result.outcome = "aborted-classify-unchanged"
            result.final_plan = revised
            result.steps = steps
            return result
        result.outcome = "applied-after-retry"
        result.final_plan = revised
        result.steps = steps
        return result
    edits.revert(edit)
    result.outcome = "aborted-retry-pytest"
    result.final_plan = revised
    result.steps = steps
    return result


# ---------------------------------------------------------------------------
# Prompt-builder helpers (kept here so the driver owns control flow)
# ---------------------------------------------------------------------------


def _exemplars_for_summary(ctx: context.LowerContext) -> str:
    """Pick a small mixed sample for L3.

    Five register handlers + five isinstance branches keeps the prompt within
    the cacheable static prefix while showing both shapes. The first ones in
    the file tend to be the canonical full/stub examples.
    """
    register = ctx.exemplars.register_handlers[:5]
    branches = ctx.exemplars.isinstance_branches[:5]
    parts: list[str] = []
    for h in register:
        parts.append(f"@{h.dispatcher}.register  # ast={h.ast_class}\n{h.body}")
    for b in branches:
        parts.append(f"# isinstance branch in {b.function}\n{b.branch_source}")
    return "\n\n---\n\n".join(parts)


def _plan_user_prompt(
    ctx: context.LowerContext, strategy: dict[str, Any], pattern: str
) -> str:
    """Tailor the L5b user prompt to the chosen pattern."""
    base = (
        f"Emit the implementation plan for {ctx.ast_class.classname} "
        f"(strategy={strategy['strategy']}, target={strategy['target_dsl_symbol']}). "
    )
    if pattern == "register-handler":
        return base + (
            "Set anchor.pattern='register-handler' and anchor.dispatcher to "
            "the relevant @<dispatcher>.register dispatcher (e.g. 'ability', "
            "'effect'). body_python MUST be the full decorated def, "
            "including the @<dispatcher>.register line and a complete "
            "function body. Use the exemplars verbatim as your style "
            "template. Do NOT include indentation appropriate to class scope "
            "— body_python will be reparsed and reindented automatically."
        )
    return base + (
        "Set anchor.pattern='isinstance-branch' and anchor.function to the "
        "name of the helper to extend (e.g. '_effects_from_statement'). "
        "body_python MUST be the full `if isinstance(stmt, ast.X): return ...` "
        "block. The orchestrator inserts it before the trailing "
        "`raise EmitterGap(stmt)` line, so do not include that raise. Use "
        "the exemplars verbatim as your style template."
    )


# ---------------------------------------------------------------------------
# CLI entrypoint helper (so the script + tests share one path)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Run the lower-gap playbook for one label.")
    p.add_argument("--gap-class", required=True,
                   help="Qualified AST class name, e.g. argentum_press.parser.ast.statements.AtStatement")
    p.add_argument("--project-dir", required=True, type=Path,
                   help="Path to the argentum-engine repo for L2 ripgrep")
    p.add_argument("--card-name", default="", help="Optional: card name for trace context")
    p.add_argument("--oracle-text", default="", help="Optional: oracle text for trace context")
    p.add_argument("--trace-out", type=Path, default=None,
                   help="Write the playbook trace JSON to this path")
    p.add_argument("--model-override", default=None,
                   help="Force every LLM step (except L3 summary) to use this "
                        "model. Useful when opus is rate-limited; e.g. "
                        "--model-override=claude-haiku-4-5-20251001.")
    args = p.parse_args(argv)

    # Make sure the parse cache stays segregated; the spec wants the env var
    # ARGENTUM_PARSE_CACHE_DIR pointing at the worktree-local dir.
    os.environ.setdefault(
        "ARGENTUM_PARSE_CACHE_DIR",
        str(context.REPO / ".parse-cache"),
    )

    result = run(
        label=args.gap_class,
        project_dir=args.project_dir,
        card_name=args.card_name,
        oracle_text=args.oracle_text,
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
