#!/usr/bin/env bash
# Drive the diagnose+fix loop for one Scryfall set.
#
# Each iteration:
#   1. `argentum-press diagnose` surfaces the first parse/lower gap.
#   2. `claude -p` (fresh context per iteration -> self-cleaning) edits
#      transformer.py or lowerer.py and runs pytest.
#   3. Loop. Bails out cleanly when no gap remains, or with exit 2 if the
#      same label appears twice in a row (no progress).
#
# Usage:
#   scripts/fix-parser-gaps.sh <set-code> <project-dir>
#
# Example:
#   scripts/fix-parser-gaps.sh spm ../argentum-engine

set -euo pipefail

set_code="${1:?set code required (e.g. spm)}"
project_dir="${2:?project dir required (path to argentum-engine)}"

# ANSI colors. Off when stdout isn't a TTY (e.g. piped to a log file) or
# when NO_COLOR=1 is set. Honor https://no-color.org for the env var.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_GRAY=$'\033[90m'
  C_GREEN=$'\033[32m'
  C_RED=$'\033[31m'
  C_CYAN=$'\033[36m'
  C_YELLOW=$'\033[33m'
  C_MAGENTA=$'\033[35m'
  C_DIM=$'\033[2m'
  C_BOLD=$'\033[1m'
  C_RESET=$'\033[0m'
else
  C_GRAY=''; C_GREEN=''; C_RED=''; C_CYAN=''; C_YELLOW=''
  C_MAGENTA=''; C_DIM=''; C_BOLD=''; C_RESET=''
fi

# Timestamp every line of output (including claude -p's) so an overnight run
# is readable when you come back. Wrapping main() and piping its combined
# output through `stamp` (rather than `exec > >(stamp)`) avoids the
# trailing-line loss you get when the script exits before the background
# stamper drains.
stamp() {
  while IFS= read -r line; do
    printf '%s[%s]%s %s\n' "$C_GRAY" "$(date '+%H:%M:%S')" "$C_RESET" "$line"
  done
}

# Render the per-iteration prompt to stdout. Defined as a function (not
# captured via `prompt=$(cat <<EOF ... EOF)`) because bash tracks quote
# state across heredoc bodies when matching the closing `)` of a command
# substitution — any line in the prompt with an odd number of literal
# `"` would then be a parse error. With the heredoc redirected straight
# into a pipeline, no such tracking happens and the body can contain
# arbitrary punctuation. Reads $card, $oracle, $kind, $label, $card_q,
# $kind_guidance, $ast_dump, $gap_class_def, $handler_map, $engine_hints,
# $set_code, $PWD from the caller (bash dynamic scoping).
render_prompt() {
  cat <<EOF
Fix one parser gap in argentum-press. Do NOT browse the repo — every path you need is below.

CARD
  name: $card
  oracle text (raw, pre-preprocessing):
$oracle

GAP
  kind: $kind
  label: $label
  $kind_guidance

AST (parsed shape of the failing card — for kind=lower gaps, find the label
class in here; the surrounding fields tell you what data the missing handler
will receive)
$ast_dump

GAP CLASS (the dataclass definition of the label; tells you exactly what
fields the new handler will see)
$gap_class_def

HANDLER MAP (every @<dispatcher>.register line in lowerer.py with its line
number; the dispatcher prefix tells you which singledispatchmethod the new
handler belongs to. Pick one structurally similar to the GAP CLASS shape and
mirror its body — do NOT bulk-read the file)
$handler_map

ENGINE DSL HINTS (grep results from argentum-engine for a keyword derived
from GAP.label; shows the Kotlin DSL classes / imports that already exist
for this kind of effect. Mirror the existing surface — do NOT invent new
DSL functions)
$engine_hints

PROJECT LAYOUT (cwd: $PWD)
  src/argentum_press/parser/transformer.py
      Lark Tree -> AST. One method per grammar rule. Unmodeled rules raise
      LoweringIncomplete via __default__, which surfaces as the label above.
  src/argentum_press/parser/ast/*.py
      Frozen-dataclass AST nodes, grouped by category (abilities.py,
      expressions.py, statements.py, references.py, keywords.py,
      card_types.py, colormana.py, card.py).
  src/argentum_press/parser/grammar/grammar.py
      Reed's Lark grammar (~1k lines), returned as a string from getGrammar().
      Only edit this if the failing label is 'parse-error:...' AND no
      existing rule covers the shape.
  src/argentum_press/lowerer.py
      AST -> Kotlin DSL. @register-keyed dispatch on AST node class.

PREPROCESSING (already applied to the oracle text before the grammar sees it; see _preprocess in transformer.py)
  - Card name is substituted with '~'. For "Foo, Bar" both the full name and "Foo" are replaced.
  - Contractions expanded: "it's"->"it is", "don't"->"do not", "his or her"->"their", "each get"->"get", etc.
  - Sentinel: '."' becomes '."."'.

PATTERNS TO MIRROR (copy structure from these; do not invent new shapes)
  - Simple-keyword transformer rule: kwflying / kwhaste in transformer.py.
  - Parametric keyword: kwequip, kwward (operand is items[0]).
  - New AST node: pick the matching ast/<file>.py and mirror the
    @dataclass(frozen=True, slots=True) shape of its neighbors.
  - New lowerer handler: grep '@register' in lowerer.py and copy a
    structurally similar one.

READING DISCIPLINE
  - The CARD, GAP, and AST sections above are the FULL pre-edit context.
    The outer loop already ran diagnose and dumped the parsed AST. Do NOT
    re-run diagnose, write Python to call parse() / KotlinLowerer(), or
    introspect anything from Python — the answer is already in the prompt.
    Only re-run diagnose AFTER your edit, via VERIFY YOUR FIX below.
  - Do NOT bulk-read test files (test_lowerer.py, test_diagnose.py, etc.).
    Run them via pytest; grep them only if you need to understand a specific
    regression after pytest goes red.
  - For large source files (lowerer.py, transformer.py): grep first. These
    are 1k+ lines; reading end-to-end wastes context. Grep for @register
    (lowerer) or the specific grammar rule method (transformer), then Read
    only the surrounding region.

VERIFY YOUR FIX  (post-edit only — do not run before editing)
  uv run argentum-press diagnose $set_code --card $card_q

  Output is JSON. Look at .gap.label — if it differs from GAP.label above,
  your fix moved the gap. If it's null, the card is now bucket-1 (clean).
  If it's the same as GAP.label, your fix didn't take.

WORKFLOW
  1. From GAP + CARD above, decide which file to edit (lowerer.py for kind=lower;
     transformer.py / ast/*.py / grammar.py for kind=parse).
  2. Grep that file for the specific symbol (the label class for lower gaps; the
     rule name for parse gaps). Read only the surrounding region.
  3. Make the minimum edit needed for THIS card's shape. No refactors, no
     drive-by cleanup, no unrelated rule changes.
  4. Run: uv run pytest tests/test_diagnose.py tests/test_pipeline.py tests/test_lowerer.py tests/test_classify.py -x -q -n auto
  5. If pytest red, fix the regression and re-run before continuing.
  6. Run the VERIFY YOUR FIX snippet. Confirm .gap.label DIFFERS from GAP.label
     above. Same label = your fix didn't move the gap; the outer loop will
     abort on the next iteration. Re-investigate and re-edit.
  7. Do NOT commit; the outer loop owns commits.
EOF
}

main() {
  local prev_label=""
  local i=0

  while true; do
    i=$((i + 1))
    echo
    printf '%s=== iteration %d ===%s\n' "$C_BOLD" "$i" "$C_RESET"

    local report label scanned kind card oracle card_q kind_guidance ast_dump
    local bare_label ast_file gap_class_def handler_map engine_hints keyword found
    report=$(uv run argentum-press diagnose "$set_code" --project-dir "$project_dir")
    label=$(printf '%s' "$report" | jq -r '.gap.label // empty')

    if [ -z "$label" ]; then
      scanned=$(printf '%s' "$report" | jq -r '.scanned')
      printf '%sno gaps remaining%s (scanned=%s). Done.\n' "$C_GREEN" "$C_RESET" "$scanned"
      return 0
    fi

    if [ "$label" = "$prev_label" ]; then
      printf "%sno progress%s: label '%s' surfaced twice in a row. Aborting.\n" \
        "$C_RED" "$C_RESET" "$label"
      return 2
    fi

    kind=$(printf '%s' "$report" | jq -r '.gap.kind')
    card=$(printf '%s' "$report" | jq -r '.gap.card_name')
    oracle=$(printf '%s' "$report" | jq -r '.gap.oracle_text')
    # Shell-escaped card name for the prompt's VERIFY YOUR FIX snippet, so
    # the agent can paste-run the command even when the name contains
    # apostrophes ("Urza's Tower"), commas, etc.
    card_q=$(printf '%q' "$card")
    # Fetch the parsed AST for this card and bake it into the prompt's AST
    # section. The agent never has to call diagnose pre-edit — the data is
    # already here. Skip the call for kind=parse since there's no AST yet
    # (Lark failed before the transformer ran).
    if [ "$kind" = "lower" ]; then
      ast_dump=$(uv run argentum-press diagnose "$set_code" --card "$card" --ast 2>/dev/null \
                 | jq -r '.ast // "(failed to fetch AST)"')
    else
      ast_dump="(parse failed at the Lark layer — no AST yet. Your job is to make Lark accept the text; see the GAP label and grammar.py.)"
    fi

    # Bare label name (e.g. "PreventDamageExpression" from the dotted form
    # "argentum_press.parser.ast.expressions.PreventDamageExpression"). Used
    # to look up the AST class definition and to derive an engine-search
    # keyword. For parse-kind gaps the label is a rule name like
    # "unmodeled-rule:X" so bare_label may not match a Python class.
    bare_label="${label##*.}"

    # AST class definition — grepped from src/argentum_press/parser/ast/*.py.
    # Only meaningful for kind=lower (parse-kind labels aren't Python classes).
    gap_class_def="(N/A — parse-kind gap; the label is a grammar rule, not a Python class.)"
    if [ "$kind" = "lower" ]; then
      ast_file=$(grep -l "^class ${bare_label}[(:]" src/argentum_press/parser/ast/*.py 2>/dev/null | head -1)
      if [ -n "$ast_file" ]; then
        gap_class_def="from ${ast_file}:

$(grep -B 2 -A 15 "^class ${bare_label}[(:]" "$ast_file" 2>/dev/null)"
      else
        gap_class_def="(no class definition found for ${bare_label} in parser/ast/*.py; the AST node may live elsewhere or be a base-class generic)"
      fi
    fi

    # Handler map — every singledispatchmethod register decorator in
    # lowerer.py with its line number. Gives the agent a map of existing
    # handlers across all dispatchers so it can jump to a structurally
    # similar one instead of bulk-reading the whole file.
    handler_map=$(grep -nE '^[[:space:]]*@[A-Za-z_]+\.register\b' src/argentum_press/lowerer.py 2>/dev/null | head -80)
    if [ -z "$handler_map" ]; then
      handler_map="(no @<dispatcher>.register decorators found — check lowerer.py manually)"
    fi

    # Engine DSL hints — grep argentum-engine for a keyword derived from
    # the bare label, stripping common AST suffixes so e.g.
    # PreventDamageExpression -> PreventDamage. Surfaces existing Kotlin
    # DSL classes the agent should mirror in its output.
    engine_hints="(N/A — parse-kind gap; engine DSL is irrelevant until the AST builds.)"
    if [ "$kind" = "lower" ] && [ -d "$project_dir" ]; then
      keyword="${bare_label%Expression}"
      keyword="${keyword%Statement}"
      keyword="${keyword%Ability}"
      if [ -n "$keyword" ]; then
        found=$(grep -rn --include='*.kt' -B 1 -A 3 "$keyword" "$project_dir" 2>/dev/null | head -40)
        if [ -n "$found" ]; then
          engine_hints="grep '${keyword}' in ${project_dir} (top 40 lines):

${found}"
        else
          engine_hints="(no occurrences of '${keyword}' in ${project_dir}/**/*.kt; either the engine lacks DSL support for this effect, or the keyword needs adjustment.)"
        fi
      fi
    fi

    printf '%sgap%s kind=%s  card=%s  label=%s\n' \
      "$C_YELLOW" "$C_RESET" "$kind" "$card" "$label"

    if [ "$kind" = "parse" ]; then
      kind_guidance="A 'parse' gap means Lark or the transformer can't handle this text. Most common: the transformer is missing a method for an unmodeled rule (label starts 'unmodeled-rule:<rulename>'). Less common: the grammar itself doesn't accept the text (label starts 'parse-error:'). Even rarer: a new AST dataclass is needed because no existing node fits."
    else
      kind_guidance="A 'lower' gap means the AST parsed fine but lowerer.py has no @register handler for the node class in the label. Add one handler — do NOT change the AST or transformer."
    fi

    # Stream claude's tool calls + assistant text as they happen so an
    # overnight run is auditable. Each event is one JSON object; the jq
    # filter flattens it to a single colored, human-readable line which
    # the outer `stamp` pipe then prefixes with a timestamp.
    render_prompt \
      | claude -p --dangerously-skip-permissions \
               --output-format stream-json --verbose \
      | jq -r --unbuffered \
          --arg green   "$C_GREEN" \
          --arg red     "$C_RED" \
          --arg cyan    "$C_CYAN" \
          --arg yellow  "$C_YELLOW" \
          --arg magenta "$C_MAGENTA" \
          --arg dim     "$C_DIM" \
          --arg reset   "$C_RESET" '
          if .type == "system" and .subtype == "init" then
            $magenta + "[claude init]" + $reset
            + " model=\(.model // "?")  cwd=\(.cwd // "?")"
          elif .type == "assistant" then
            (.message.content[]? |
              if .type == "text" then
                $cyan + "» " + (.text | gsub("\n"; " ⏎ ")) + $reset
              elif .type == "tool_use" then
                $green + "→ \(.name)" + $reset + " "
                + ((.input // {}) | tostring | .[0:240])
              else empty end)
          elif .type == "user" then
            (.message.content[]? |
              if .type == "tool_result" then
                (if (.is_error // false)
                  then $red + "← ERROR "
                  else $dim + "← " end)
                + ((.content // "") | tostring | gsub("\n"; " ⏎ ") | .[0:240])
                + $reset
              else empty end)
          elif .type == "result" then
            $magenta + "[claude done]" + $reset
            + " subtype=\(.subtype // "?")"
            + "  turns=\(.num_turns // 0)"
            + "  cost=$\(.total_cost_usd // 0)"
          else empty end
        '

    prev_label="$label"
  done
}

main 2>&1 | stamp
exit "${PIPESTATUS[0]}"
