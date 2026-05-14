# pyright: basic
"""Anthropic SDK wrapper for the lower-gap playbook.

Each LLM step in the playbook is a *single* tool_use call: the model is
forced into a JSON schema so the orchestrator can validate and act on the
output without re-parsing prose. We use prompt caching aggressively — the
system block and the static context blocks (AST class source, exemplar
handlers) are marked ``cache_control: ephemeral`` so the L4/L5b/L9 calls
within one playbook iteration share their prefix.

Models:

* ``claude-haiku-4-5-20251001`` for the cheap L3 summary.
* ``claude-opus-4-7`` for L4 strategy, L5b code emission, and L9 retry.

Tests inject a fake ``client`` to avoid live calls; the real client is
constructed via :class:`anthropic.Anthropic` when ``client=None``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import jsonschema


# ---------------------------------------------------------------------------
# Tool schemas (each step's structured output)
# ---------------------------------------------------------------------------


SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "mtg_term", "similar_handlers"],
    "properties": {
        "summary": {"type": "string"},
        "mtg_term": {"type": "string"},
        "similar_handlers": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 0,
            "maxItems": 5,
        },
    },
}


STRATEGY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["strategy", "target_dsl_symbol", "justification"],
    "properties": {
        "strategy": {"type": "string", "enum": ["full", "stub", "sub-gap"]},
        "target_dsl_symbol": {"type": "string"},
        "justification": {"type": "string"},
    },
}


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["anchor", "body_python", "new_imports"],
    "properties": {
        "anchor": {
            "type": "object",
            "additionalProperties": False,
            "required": ["pattern"],
            "properties": {
                "pattern": {
                    "type": "string",
                    "enum": ["register-handler", "isinstance-branch"],
                },
                "function": {"type": ["string", "null"]},
                "after_branch": {"type": ["string", "null"]},
                "dispatcher": {"type": ["string", "null"]},
            },
        },
        "body_python": {"type": "string"},
        "new_imports": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 0,
            "maxItems": 20,
        },
    },
}


# Mapping each tool name to its schema. The wrapper uses this to validate the
# response body after Claude returns a tool_use block.
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "emit_ast_summary": SUMMARY_SCHEMA,
    "emit_strategy": STRATEGY_SCHEMA,
    "emit_plan": PLAN_SCHEMA,
}


# ---------------------------------------------------------------------------
# Client protocol (so tests can inject a fake)
# ---------------------------------------------------------------------------


class _MessagesClient(Protocol):
    """Subset of ``anthropic.Anthropic().messages`` we depend on."""

    def create(self, **kwargs: Any) -> Any: ...


class ClientLike(Protocol):
    """Minimal Anthropic-client interface the playbook uses."""

    @property
    def messages(self) -> _MessagesClient: ...


# ---------------------------------------------------------------------------
# Wrapper
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """One LLM tool_use turn, surfaced for the playbook driver."""

    tool_name: str
    arguments: dict[str, Any]
    raw_response: Any  # The full SDK Message object; useful for debugging.


# Cache directives are applied via the messages-API ``cache_control`` field.
# We mark the system block and each static context block as ephemeral so the
# whole static prefix is shared across L4 / L5b / L9 within one iteration.
_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}


def _create_with_backoff(
    client: ClientLike, *, _max_retries: int = 8, _base_delay: float = 4.0, **kwargs: Any
) -> Any:
    """Wrap ``client.messages.create`` with exponential backoff on 429.

    The subscription auth path frequently 429s when another Claude session
    is consuming quota concurrently (e.g. the A/B race runs both freeform
    ``claude -p`` and the playbook SDK calls against the same subscription
    pool). We retry up to ``_max_retries`` times with delays
    ``[4, 8, 16, 32, 64, 90, 90, 90]`` seconds plus random jitter, ~6
    minutes of total grace. If the contending session is freeform's claude
    loop it eventually pauses for pytest, which is when our retry lands.

    Anything else (auth, 500, etc.) propagates immediately.
    """
    import random
    import time
    delay = _base_delay
    for attempt in range(_max_retries):
        try:
            return client.messages.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            is_429 = ("429" in msg or "rate_limit" in msg) and attempt < _max_retries - 1
            if not is_429:
                raise
            # Cap individual sleeps at 90s; small jitter so concurrent retries
            # don't synchronise.
            sleep_for = min(delay, 90.0) + random.uniform(0, 2.0)
            time.sleep(sleep_for)
            delay *= 2
    raise RuntimeError("playbook: backoff loop exhausted")


def _read_oauth_token_from_keychain() -> str | None:
    """Pull the Claude Code OAuth access token from macOS keychain if present.

    The Anthropic SDK refuses to make calls without auth; if the user is a
    Claude Code subscriber (no ANTHROPIC_API_KEY) we fall back to the OAuth
    bearer token Claude Code stashed in the keychain. Returns None on any
    error so the SDK can surface its own clearer "no credentials" message.
    """
    import json
    import subprocess
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    tok = data.get("claudeAiOauth", {}).get("accessToken")
    return tok if isinstance(tok, str) and tok else None


def _read_oauth_token_from_profile(profile_dir: Path) -> str | None:
    """Pull the OAuth bearer token from a Claude Code profile directory.

    ``CLAUDE_CONFIG_DIR`` is Claude Code's standard mechanism for running
    multiple accounts side-by-side: each profile lives at e.g.
    ``~/.claude-A/`` and stores its OAuth credentials in
    ``credentials.json`` (same shape as the keychain JSON). The A/B race
    populates these via ``scripts/setup_claude_profile.py``.
    """
    import json
    creds_path = profile_dir / "credentials.json"
    try:
        data = json.loads(creds_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    tok = data.get("claudeAiOauth", {}).get("accessToken")
    return tok if isinstance(tok, str) and tok else None


def _default_client() -> ClientLike:
    """Construct an Anthropic client lazily so import doesn't require the env.

    Auth precedence:
    1. ``ANTHROPIC_API_KEY`` → API-key mode (separate billing pool).
    2. ``CLAUDE_CONFIG_DIR/credentials.json`` → OAuth bearer from a per-profile
       credentials file. Lets the A/B race split auth across two Claude Code
       subscriptions without keychain rotation; each pane sets
       CLAUDE_CONFIG_DIR to a different directory.
    3. Keychain ``Claude Code-credentials`` entry → OAuth bearer (the default
       when only one account is in use).
    The OAuth paths require the ``oauth-2025-04-20`` beta header.
    """
    import os
    from anthropic import Anthropic

    if os.environ.get("ANTHROPIC_API_KEY"):
        return Anthropic()
    profile_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if profile_dir:
        tok = _read_oauth_token_from_profile(Path(profile_dir))
        if tok:
            return Anthropic(
                auth_token=tok,
                default_headers={"anthropic-beta": "oauth-2025-04-20"},
            )
    token = _read_oauth_token_from_keychain()
    if token:
        return Anthropic(
            auth_token=token,
            default_headers={"anthropic-beta": "oauth-2025-04-20"},
        )
    return Anthropic()


def call_tool(
    *,
    tool_name: str,
    system_prompt: str,
    static_context_blocks: list[dict[str, Any]],
    user_prompt: str,
    model: str,
    client: ClientLike | None = None,
    max_tokens: int = 2048,
    extra_tools: list[dict[str, Any]] | None = None,
) -> ToolCallResult:
    """Call Claude and force a single tool_use turn matching ``tool_name``.

    ``static_context_blocks`` is the list of ``{type: text, text: ..., cache_control}``
    blocks that prefix the user prompt — these are the L3 summary, the AST
    class source, the exemplar handlers, etc. Marking them ``ephemeral`` is
    what makes the second call in the same iteration cheap.

    Returns a :class:`ToolCallResult` after validating the JSON args against
    :data:`TOOL_SCHEMAS`. Raises :class:`ValueError` if the model didn't emit
    a tool_use block or the arguments don't validate.
    """
    schema = TOOL_SCHEMAS[tool_name]
    tools = [
        {
            "name": tool_name,
            "description": f"Emit the {tool_name} JSON for the playbook orchestrator.",
            "input_schema": schema,
        }
    ]
    if extra_tools:
        tools.extend(extra_tools)
    c = client or _default_client()

    # Build the user content as a list so the static prefix can carry
    # cache_control while the per-call tail (dynamic question) stays uncached.
    user_content: list[dict[str, Any]] = list(static_context_blocks)
    user_content.append({"type": "text", "text": user_prompt})

    response = _create_with_backoff(
        c,
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system_prompt, "cache_control": _CACHE_CONTROL}],
        messages=[{"role": "user", "content": user_content}],
        tools=tools,
        tool_choice={"type": "tool", "name": tool_name},
    )

    # The SDK returns a Message with a content list; find the tool_use block.
    args: dict[str, Any] | None = None
    for block in getattr(response, "content", []) or []:
        # Real SDK blocks expose .type / .name / .input; fake blocks in tests
        # may use dict-style. Handle both shapes.
        btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if btype != "tool_use":
            continue
        bname = getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else None)
        if bname != tool_name:
            continue
        binput = getattr(block, "input", None)
        if binput is None and isinstance(block, dict):
            binput = block.get("input")
        if isinstance(binput, str):
            try:
                args = json.loads(binput)
            except json.JSONDecodeError as e:
                raise ValueError(f"playbook: tool_use {tool_name} args not JSON: {e}") from e
        elif isinstance(binput, dict):
            args = binput
        break

    if args is None:
        raise ValueError(
            f"playbook: model returned no tool_use block named {tool_name!r}; "
            f"response content={getattr(response, 'content', '?')!r}"
        )

    try:
        jsonschema.validate(args, schema)
    except jsonschema.ValidationError as e:
        raise ValueError(
            f"playbook: tool_use {tool_name} args failed schema: {e.message}; got {args!r}"
        ) from e

    return ToolCallResult(tool_name=tool_name, arguments=args, raw_response=response)


# ---------------------------------------------------------------------------
# Prompt builders (one per step). Kept here so the schemas + prompts stay
# co-located; the playbook driver only handles control flow.
# ---------------------------------------------------------------------------


def build_summary_blocks(ast_class_source: str, handler_exemplars: str) -> list[dict[str, Any]]:
    """Static blocks for the L3 summary call.

    Both blocks are marked cache_control: ephemeral so re-using the same AST
    class + same exemplar bundle (typical within one iteration) hits the
    prompt cache rather than re-billing the prefix.
    """
    return [
        {
            "type": "text",
            "text": f"AST CLASS SOURCE\n\n{ast_class_source}",
            "cache_control": _CACHE_CONTROL,
        },
        {
            "type": "text",
            "text": f"EXAMPLES OF EXISTING HANDLER PATTERNS\n\n{handler_exemplars}",
            "cache_control": _CACHE_CONTROL,
        },
    ]


def build_strategy_blocks(
    *,
    summary_json: str,
    engine_hints: str,
    ast_class_source: str,
) -> list[dict[str, Any]]:
    # Anthropic accepts at most 4 cache_control blocks across the whole
    # request (1 system + 3 user). The AST class source is the stable
    # prefix shared across L4/L5b/L9; mark it ephemeral so the cache hits.
    # Summary + engine hints are smaller, change less often per AST class,
    # and don't need separate cache breakpoints — they ride the same block.
    return [
        {
            "type": "text",
            "text": f"AST CLASS SOURCE\n\n{ast_class_source}",
            "cache_control": _CACHE_CONTROL,
        },
        {
            "type": "text",
            "text": (
                f"L3 SUMMARY\n\n{summary_json}\n\n"
                f"ENGINE DSL HINTS (ripgrep over argentum-engine)\n\n{engine_hints}"
            ),
        },
    ]


def build_plan_blocks(
    *,
    summary_json: str,
    strategy_json: str,
    pattern: str,
    ast_class_source: str,
    pattern_exemplars: str,
) -> list[dict[str, Any]]:
    # Cache budget: 4 cache_control blocks total (1 system + 3 user).
    # Mark the AST class source + the relevant exemplars as ephemeral — those
    # are stable across L5b retries within one iteration. Summary + strategy
    # ride a single combined block (no separate cache breakpoint).
    return [
        {
            "type": "text",
            "text": f"AST CLASS SOURCE\n\n{ast_class_source}",
            "cache_control": _CACHE_CONTROL,
        },
        {
            "type": "text",
            "text": (
                f"PATTERN (chosen by L5a heuristic): {pattern}\n\n"
                f"RELEVANT EXEMPLARS\n\n{pattern_exemplars}"
            ),
            "cache_control": _CACHE_CONTROL,
        },
        {
            "type": "text",
            "text": (
                f"L3 SUMMARY\n\n{summary_json}\n\n"
                f"L4 STRATEGY\n\n{strategy_json}"
            ),
        },
    ]


def build_retry_blocks(
    *,
    summary_json: str,
    strategy_json: str,
    pattern: str,
    ast_class_source: str,
    pattern_exemplars: str,
    failed_plan_json: str,
    pytest_tail: str,
) -> list[dict[str, Any]]:
    blocks = build_plan_blocks(
        summary_json=summary_json,
        strategy_json=strategy_json,
        pattern=pattern,
        ast_class_source=ast_class_source,
        pattern_exemplars=pattern_exemplars,
    )
    blocks.append(
        {
            "type": "text",
            "text": (
                f"PREVIOUS PLAN (rejected by pytest)\n\n{failed_plan_json}\n\n"
                f"PYTEST OUTPUT (last 1500 chars)\n\n{pytest_tail}"
            ),
            # No cache_control on the retry tail — it's iteration-specific.
        }
    )
    return blocks


SYSTEM_PROMPT = (
    "You are the lower-gap playbook for argentum-press, a Scryfall→Kotlin DSL "
    "compiler. Each call asks you to emit one tool_use payload — never prose. "
    "argentum-press lowers a rich MTG AST (see argentum_press.parser.ast) into "
    "argentum-engine Kotlin DSL strings. When a lowerer handler is missing, "
    "we want to add one: either a top-level @<dispatcher>.register clause, "
    "or an isinstance(stmt, ast.X) branch inside a helper like "
    "_effects_from_statement. Mirror the style of the exemplars exactly. When "
    "argentum-engine has no surface for a concept, emit a stub like "
    "'Effects.X()' so the gap moves past this AST class to whatever's next."
)
