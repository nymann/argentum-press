# pyright: basic
"""LLM transports for the three playbooks (lower / parse-error / unmodeled-rule).

Each LLM step is a *single* tool_use-style call: the model is forced into
a JSON schema so the orchestrator can validate and act on the output
without re-parsing prose. We use prompt caching aggressively — the
system block and the static context blocks (AST class source, exemplar
handlers) are marked ``cache_control: ephemeral`` so the L4/L5b/L9 calls
within one playbook iteration share their prefix.

Three transports, picked by ``lower._call`` based on the model name +
which arguments are supplied:

* :func:`call_tool` — Anthropic SDK. Used when ``client`` is provided
  (production SDK path; tests pass a FakeClient with SDK shape).
* :func:`call_tool_via_cli` — long-lived ``claude -p`` subprocess
  pool. Used in production when ``pool`` is provided.
* :func:`call_tool_via_local_openai` — POST to an OpenAI-compatible
  local server on ``http://localhost:8080``. Routed to automatically
  when the model name contains ``/`` (e.g.
  ``mlx-community/Qwen3-Coder-Next-4bit``).

Model assignment (see ``lower.py``, ``parse_error.py``, ``unmodeled_rule.py``):

* L3 cached-summary  → ``mlx-community/Qwen3-Coder-Next-4bit`` (local).
* L4 / L5 / P3 / U3 / U4 (picker + non-grammar code emission) → sonnet 4.6.
* P4 (new grammar emission) → opus 4.7.
* L9 / P8 / U8 (retry-after-pytest-red diagnosis) → opus 4.7.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import jsonschema

from . import driver as _driver_mod


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


# --- parse-error schemas --------------------------------------------------


PARSE_PARENT_CHOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["parent_rule", "missing_phrase", "rationale"],
    "properties": {
        "parent_rule": {"type": "string"},
        "missing_phrase": {"type": "string"},
        "rationale": {"type": "string"},
    },
}


PARSE_ALTERNATIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["parent_rule", "alternative_text", "label"],
    "properties": {
        "parent_rule": {"type": "string"},
        "alternative_text": {"type": "string"},
        "label": {"type": ["string", "null"]},
    },
}


# --- unmodeled-rule schemas -----------------------------------------------


AST_FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "type"],
    "properties": {
        "name": {"type": "string"},
        "type": {"type": "string"},
        "default": {"type": ["string", "null"]},
    },
}


AST_CLASS_DESIGN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["classname", "parent_class", "parent_module", "fields", "docstring"],
    "properties": {
        "classname": {"type": "string"},
        "parent_class": {"type": "string"},
        "parent_module": {"type": "string"},
        "fields": {
            "type": "array",
            "items": AST_FIELD_SCHEMA,
            "minItems": 0,
            "maxItems": 12,
        },
        "docstring": {"type": "string"},
    },
}


TRANSFORMER_METHOD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["method_source", "extra_imports"],
    "properties": {
        "method_source": {"type": "string"},
        "extra_imports": {
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
    "emit_parse_parent_choice": PARSE_PARENT_CHOICE_SCHEMA,
    "emit_parse_alternative": PARSE_ALTERNATIVE_SCHEMA,
    "emit_ast_class_design": AST_CLASS_DESIGN_SCHEMA,
    "emit_transformer_method": TRANSFORMER_METHOD_SCHEMA,
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
# CLI-backed variant: routes through ``claude -p`` instead of the SDK.
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _extract_json_object(text: str) -> dict[str, Any]:
    """Pull a JSON object out of ``claude``'s free-text response.

    Preference order: (1) the last ```json fenced block, (2) the whole
    response parsed as JSON, (3) the largest balanced ``{...}`` substring.
    Raises :class:`ValueError` if nothing parses.
    """
    matches = _FENCE_RE.findall(text)
    candidates: list[str] = []
    if matches:
        candidates.append(matches[-1].strip())
    candidates.append(text.strip())
    # Last-resort balanced-braces extraction; greedy from first `{`.
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first : last + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError(
        f"playbook: could not extract JSON object from claude response; "
        f"tail={text[-400:]!r}"
    )


# ---------------------------------------------------------------------------
# Local OpenAI-compatible route (MLX server on localhost:8080)
# ---------------------------------------------------------------------------


# Model names that route to the local OpenAI-compatible server instead of the
# Anthropic API. The MLX server exposes its models with a `mlx-community/`
# prefix, so the namespace is naturally non-Anthropic.
LOCAL_OPENAI_URL = "http://localhost:8080/v1/chat/completions"
LOCAL_MODEL_TIMEOUT_S = 60.0


def _is_local_model(model: str) -> bool:
    """True for model names handled by the local OpenAI-compatible server.

    The heuristic is `contains "/"` — Anthropic model IDs are flat
    (`claude-opus-4-7`), while OpenAI-compatible servers use a
    `vendor/model-name` namespace. Cheap to check and unambiguous against
    every Claude model we use.
    """
    return "/" in model


def call_tool_via_local_openai(
    *,
    tool_name: str,
    system_prompt: str,
    static_context_blocks: list[dict[str, Any]],
    user_prompt: str,
    model: str,
    max_tokens: int = 2048,
    url: str = LOCAL_OPENAI_URL,
    timeout_s: float = LOCAL_MODEL_TIMEOUT_S,
) -> ToolCallResult:
    """OpenAI-compatible route for a local MLX server.

    Designed for the L3 cached-summary call where the output is small,
    bounded, and the API cost saving (running on-box) is the explicit
    point. Builds one chat completion request, asks for JSON output,
    validates against the same :data:`TOOL_SCHEMAS` schema as the
    Anthropic routes, returns the same :class:`ToolCallResult` shape so
    the playbook driver doesn't branch on transport.

    Loses two things relative to the Anthropic SDK path:

    * **No ``tool_use`` forcing.** OpenAI-compatible function calling
      varies by server; we use ``response_format={"type": "json_object"}``
      where supported and lean on the prompt to enforce the schema. The
      jsonschema validation step is unchanged.
    * **No ``cache_control`` markers.** Local inference has its own
      key-value cache reuse semantics; the explicit Anthropic markers are
      a no-op here. For L3 (always a cache-miss caller, since it's the
      first call in the playbook iteration) this costs nothing.
    """
    import urllib.error
    import urllib.request

    schema = TOOL_SCHEMAS[tool_name]
    static_text_parts: list[str] = []
    for block in static_context_blocks:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str) and text:
            static_text_parts.append(text)

    schema_str = json.dumps(schema, indent=2)
    user_message = (
        ("\n\n---\n\n".join(static_text_parts) + "\n\n---\n\n" if static_text_parts else "")
        + f"{user_prompt}\n\n"
        f"Respond with EXACTLY one JSON object matching this schema. "
        f"Do not include any prose outside the JSON. Do not wrap in code fences.\n\n"
        f"SCHEMA (tool_name={tool_name}):\n\n{schema_str}\n"
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "temperature": 0.0,  # deterministic for the structured-output use case
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"playbook: local OpenAI route failed ({url}): {e}; "
            f"is the MLX server running on {url}?"
        ) from e

    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(
            f"playbook: local response missing choices[0].message.content: {body!r}"
        ) from e

    args = _extract_json_object(text)
    try:
        jsonschema.validate(args, schema)
    except jsonschema.ValidationError as e:
        raise ValueError(
            f"playbook: local {tool_name} args failed schema: {e.message}; got {args!r}"
        ) from e

    return ToolCallResult(tool_name=tool_name, arguments=args, raw_response=body)


# ---------------------------------------------------------------------------
# CLI-backed variant: routes through ``claude -p`` instead of the SDK.
# ---------------------------------------------------------------------------


def call_tool_via_cli(
    *,
    tool_name: str,
    system_prompt: str,
    static_context_blocks: list[dict[str, Any]],
    user_prompt: str,
    pool: _driver_mod.DriverPool,
    model: str,
) -> ToolCallResult:
    """CLI-backed equivalent of :func:`call_tool`.

    Concatenates the system prompt + static context blocks + user prompt
    into one user turn, adds a schema-emission instruction, sends it to
    the per-model long-lived ``claude`` subprocess via :class:`DriverPool`,
    parses JSON from the response, and validates against the tool's
    schema. Returns the same :class:`ToolCallResult` shape so callers
    don't branch on transport.

    Loses two things relative to the SDK path:

    * Forced ``tool_use`` JSON — replaced by prompt instruction +
      :func:`_extract_json_object`. Schema validation still runs.
    * Per-block ``cache_control`` markers — ``claude -p`` does its own
      prompt caching at the edge, but we can't pin specific blocks.
    """
    schema = TOOL_SCHEMAS[tool_name]
    static_text_parts: list[str] = []
    for block in static_context_blocks:
        text = block.get("text") if isinstance(block, dict) else None
        if isinstance(text, str) and text:
            static_text_parts.append(text)

    schema_str = json.dumps(schema, indent=2)
    prompt = (
        f"{system_prompt}\n\n"
        + "\n\n---\n\n".join(static_text_parts)
        + ("\n\n---\n\n" if static_text_parts else "")
        + f"{user_prompt}\n\n"
        f"Respond with EXACTLY one JSON object matching this schema, "
        f"wrapped in a single ```json fenced code block. Do not include "
        f"any prose outside the fence. Do not call any tools.\n\n"
        f"SCHEMA (tool_name={tool_name}):\n\n{schema_str}\n"
    )

    driver = pool.get(model)
    attempt = driver.attempt(prompt)
    args = _extract_json_object(attempt.assistant_text)

    try:
        jsonschema.validate(args, schema)
    except jsonschema.ValidationError as e:
        raise ValueError(
            f"playbook: cli {tool_name} args failed schema: {e.message}; got {args!r}"
        ) from e

    return ToolCallResult(tool_name=tool_name, arguments=args, raw_response=attempt)


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


# ---------------------------------------------------------------------------
# Parse-error playbook prompts
# ---------------------------------------------------------------------------


PARSE_ERROR_SYSTEM_PROMPT = (
    "You are the parse-error playbook for argentum-press. Lark rejected a "
    "card's preprocessed oracle text — that means an existing grammar rule "
    "needs a new alternative. Each call emits one tool_use payload. Grammar "
    "literals are double-quoted lowercase tokens; alternatives attach to the "
    "rule with `| <alt> -> <label>` (the label is optional but conventional). "
    "Keep alternatives short and mirror the style of the surrounding rule. "
    "Never invent new terminal names — only literals already used elsewhere "
    "in the grammar or new double-quoted strings. Do NOT include the leading "
    "`|` in alternative_text; the orchestrator adds it. Labels are lowercase "
    "rule-name shape (e.g. `redirectalldamageexpression`)."
)


def build_parse_parent_choice_blocks(
    *,
    pe_block: str,
    candidates_dump: str,
    oracle_text: str,
) -> list[dict[str, Any]]:
    """Static prefix for the P3 parent-rule choice call.

    The orchestrator already ranked three rules and dumped their definitions.
    The LLM only needs to pick one + identify the missing phrase. Both
    blocks are static within an iteration so we mark them ephemeral.
    """
    return [
        {
            "type": "text",
            "text": (
                f"FAILING ORACLE TEXT\n\n{oracle_text}\n\n"
                f"LARK ERROR\n\n{pe_block}"
            ),
            "cache_control": _CACHE_CONTROL,
        },
        {
            "type": "text",
            "text": (
                f"TOP-3 CANDIDATE PARENT RULES (ranked by literal overlap)\n\n"
                f"{candidates_dump}"
            ),
            "cache_control": _CACHE_CONTROL,
        },
    ]


def build_parse_alternative_blocks(
    *,
    pe_block: str,
    oracle_text: str,
    parent_rule_def: str,
    parent_choice_json: str,
) -> list[dict[str, Any]]:
    """Static prefix for the P4 alternative-emission call."""
    return [
        {
            "type": "text",
            "text": (
                f"FAILING ORACLE TEXT\n\n{oracle_text}\n\n"
                f"LARK ERROR\n\n{pe_block}"
            ),
            "cache_control": _CACHE_CONTROL,
        },
        {
            "type": "text",
            "text": f"PARENT RULE DEFINITION\n\n{parent_rule_def}",
            "cache_control": _CACHE_CONTROL,
        },
        {
            "type": "text",
            "text": f"P3 PARENT CHOICE\n\n{parent_choice_json}",
        },
    ]


def build_parse_retry_blocks(
    *,
    pe_block: str,
    oracle_text: str,
    parent_rule_def: str,
    parent_choice_json: str,
    failed_plan_json: str,
    pytest_tail: str,
) -> list[dict[str, Any]]:
    blocks = build_parse_alternative_blocks(
        pe_block=pe_block,
        oracle_text=oracle_text,
        parent_rule_def=parent_rule_def,
        parent_choice_json=parent_choice_json,
    )
    blocks.append(
        {
            "type": "text",
            "text": (
                f"PREVIOUS PLAN (rejected by pytest)\n\n{failed_plan_json}\n\n"
                f"PYTEST OUTPUT (last 1500 chars)\n\n{pytest_tail}"
            ),
        }
    )
    return blocks


# ---------------------------------------------------------------------------
# Unmodeled-rule playbook prompts
# ---------------------------------------------------------------------------


UNMODELED_RULE_SYSTEM_PROMPT = (
    "You are the unmodeled-rule playbook for argentum-press. Lark parsed a "
    "card but the CardTransformer has no method for one of the rules it "
    "produced — so we need to (a) design a frozen-slots @dataclass AST node "
    "and (b) write the transformer method that builds it from lark `items`. "
    "Each call emits one tool_use payload. Mirror the exemplar diffs: every "
    "dataclass is `@dataclass(frozen=True, slots=True)`, extends a base from "
    "the same file (Statement/Expression/Ability), uses `tuple[…, …]` for "
    "collections, and `None` for optional fields. Transformer methods are "
    "named after the rule, take `(self, items)`, and inspect `items` types "
    "to pick fields out. Don't import lark — items are already transformed."
)


def build_ast_design_blocks(
    *,
    rule_def: str,
    parent_module_summary: str,
    exemplar_diffs: str,
) -> list[dict[str, Any]]:
    return [
        {
            "type": "text",
            "text": f"GRAMMAR RULE DEFINITION\n\n{rule_def}",
            "cache_control": _CACHE_CONTROL,
        },
        {
            "type": "text",
            "text": (
                f"PARENT AST MODULE (existing class names)\n\n"
                f"{parent_module_summary}"
            ),
            "cache_control": _CACHE_CONTROL,
        },
        {
            "type": "text",
            "text": f"EXEMPLAR `parser: handle <rule>` DIFFS\n\n{exemplar_diffs}",
            "cache_control": _CACHE_CONTROL,
        },
    ]


def build_transformer_method_blocks(
    *,
    rule_def: str,
    ast_design_json: str,
    exemplar_diffs: str,
) -> list[dict[str, Any]]:
    return [
        {
            "type": "text",
            "text": f"GRAMMAR RULE DEFINITION\n\n{rule_def}",
            "cache_control": _CACHE_CONTROL,
        },
        {
            "type": "text",
            "text": f"EXEMPLAR `parser: handle <rule>` DIFFS\n\n{exemplar_diffs}",
            "cache_control": _CACHE_CONTROL,
        },
        {
            "type": "text",
            "text": f"AST CLASS DESIGN (from previous step)\n\n{ast_design_json}",
        },
    ]


def build_unmodeled_retry_blocks(
    *,
    rule_def: str,
    ast_design_json: str,
    method_source: str,
    exemplar_diffs: str,
    pytest_tail: str,
) -> list[dict[str, Any]]:
    return [
        {
            "type": "text",
            "text": f"GRAMMAR RULE DEFINITION\n\n{rule_def}",
            "cache_control": _CACHE_CONTROL,
        },
        {
            "type": "text",
            "text": f"EXEMPLAR `parser: handle <rule>` DIFFS\n\n{exemplar_diffs}",
            "cache_control": _CACHE_CONTROL,
        },
        {
            "type": "text",
            "text": (
                f"PREVIOUS AST DESIGN\n\n{ast_design_json}\n\n"
                f"PREVIOUS TRANSFORMER METHOD\n\n{method_source}\n\n"
                f"PYTEST OUTPUT (last 1500 chars)\n\n{pytest_tail}"
            ),
        }
    ]
