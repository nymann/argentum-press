# Parser-layer small fixes carried over from Reed

Three Reed-era bugs that the absorption preserved verbatim because
"verbatim lift" was the explicit instruction during the move. None
are blocking the live path, but they're each a small fix away from
being clean.

## `preprocessor.prelex` argument order is wrong  [done 2026-05-14]

`argentum_press/parser/grammar/preprocessor.py` defines:

```python
def prelex(self, inputobj, flags, name):
    ...
```

but the corresponding call inside `grammar/compiler.py` is:

```python
self._preprocessor.prelex(textInput, name, flags)
```

The positional args don't line up — `flags` lands where `name` is
expected and vice versa. This was sidestepped during the absorption
by **inlining `_preprocess()` directly inside `transformer.py`**
(`transformer.py:1654`), so the broken `prelex()` is no longer on
the live path. But the grammar-layer file still has the bug.

Fix: swap the call site (or the signature). Once fixed, the inlined
`_preprocess` in `transformer.py` could be deleted and replaced with
`MtgJsonPreprocessor().prelex(text, {}, name)` — though doing so
would also surrender the A-prefix workaround we added in `921a501`.
Probably easier to leave the inlined version and just fix the
grammar-layer signature for anyone who reaches for it later.

**Resolution (2026-05-14):** swapped the signature in
`preprocessor.py` to `prelex(self, inputobj, name, flags)` so it
matches the existing `compiler.py:113` call site. Inlined
`_preprocess` in `transformer.py` is unchanged and still owns the
live path with the A-prefix workaround.

## `SyntaxWarning` in grammar.py:11  [deferred — live-path file, parser-gaps loop running]

```
SyntaxWarning: invalid escape sequence '\('
  remindertext : /\(.*?\)/
```

Non-raw regex literal. Python 3.12+ raises this as a warning;
future Python versions will make it a `SyntaxError`. One-character
fix: prefix the regex literal with `r`. Verify the grammar still
builds afterwards.

## `grammarian.py` has hardcoded `mtgcompiler/...` path defaults  [done 2026-05-14 — file later deleted]

`argentum_press/parser/grammar/grammarian.py` has default arguments
like `"mtgcompiler/frontend/grammarian/grmgrammar.lark"` and a
`magicspec` directory that no longer exist after the absorption.
None of this is on the live path — `compiler.py` only calls
`grammar.getGrammar()`, never `grammarian` — so these only break
if someone explicitly reaches for grammarian for grammar
regeneration.

Fix: update the defaults to relative paths under
`argentum_press/parser/grammar/`, or delete the module if we're
sure we won't regenerate the grammar from the grammarian DSL.

**Resolution (2026-05-14):** defaults now resolve via
`Path(__file__).parent` so the module is loadable. `grmgrammar.lark`
sits next to it; `magicspec/` doesn't exist anywhere in the repo, so
`requestGrammar` still won't *do* anything useful until spec files
get re-added — but it's no longer a hard reference to a path that
can never resolve.

**Follow-up (2026-05-14):** briefly imported `magicspec/` from
upstream `rmmilewi/mtgcompiler` to test whether it could serve as
source-of-truth. Findings: (1) `requestGrammar()` blows up in
`GrammarAssembler.alias` on a `None` item with current Lark, so the
compile pipeline is broken; (2) upstream `abilities.grm` is 0 bytes
yet `grammar.py` clearly contains ability rules — magicspec is
demonstrably incomplete, not a faithful source. Decision: remove
the directory and keep `grammar.py` as the single live source. The
partition aid is human-ergonomic and doesn't help the LLM agent
that drives `fix-parser-gaps.sh`. Reversible: the .grm files still
exist intact at `~/code/github.com/rmmilewi/mtgcompiler/src/mtgcompiler/frontend/grammarian/magicspec/`.
`grammarian.py` and `grmgrammar.lark` are now strictly dead code —
candidates for deletion if we ever do a sweep.

**Cleanup (2026-05-14):** deleted `grammarian.py` (597 lines) and
`grmgrammar.lark` (67 lines). Also removed the stale `from .
import grammarian` and the dead `requestGrammar(...)` commented-out
block from `compiler.py`. Compiler still instantiates cleanly.

## `hasParser` / `getParser` defined twice in `base.py`  [done 2026-05-14]

The four `BaseImplementation/Base*.py` files merged into
`grammar/base.py` during the lift. `BaseCompiler` originally
defined `hasParser` / `getParser` twice in the same class body;
Python takes the second definition, so this is functionally
harmless, just untidy.

Fix: delete the first definition; keep the one that matches
current usage.

**Resolution (2026-05-14):** deleted the first pair. No behavior
change (Python had been taking the second def anyway).
