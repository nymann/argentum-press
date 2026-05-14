# pyright: basic
"""Deterministic L3: TF-IDF retrieval over @register handlers + textual
heuristics for ``summary`` and ``mtg_term``.

Replaces the LLM call that previously generated the L3 AST-class summary.
The output shape matches :data:`llm.SUMMARY_SCHEMA` exactly so the L4 /
L5 / L9 prompts (which consume the JSON as static context) need no
changes.

Why not an LLM:

* The corpus is small (~400 ``@register`` handlers in ``lowerer.py``).
* The query is code, not human prose — handlers share vocabulary
  (``@effect.register``, ``ast.X``, common field names like ``subject``
  / ``target`` / ``player``), so TF-IDF's "literal token overlap"
  similarity is the *exact* signal we want.
* Brute-force cosine over 400 vectors takes well under a millisecond;
  no ANN index, no embedding model, no remote service.

Why not Combfind / sentence-transformers / chroma:

* Combfind labels every symbol with an LLM at index time — the
  initialisation cost is what we're trying to avoid.
* Sentence-transformers brings ~1.5 GB of torch/transformers and
  embeds via a model that takes longer than the LLM call we're
  replacing on a slow box.
* Chroma / LanceDB are vector databases optimised for collections
  with metadata filters and durable persistence; the corpus here is
  rebuilt from ``lowerer.py`` on every playbook iteration anyway.

Result: L3 is microseconds, deterministic, and the ``similar_handlers``
field now lists AST class names that *actually exist* in our codebase
rather than the LLM's best guess from general MTG knowledge.
"""
from __future__ import annotations

import re
from typing import Any

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .context import AstClassInfo, LowererExemplars


# Suffixes stripped from class names when deriving ``mtg_term``. Order
# matters — longest match first so e.g. "InAdditionToTypesExpression"
# yields "inadditiontotypes" rather than a partial match against the
# "Expression" suffix mid-word. (None of our suffixes are prefixes of
# another, but ordering by length is the safe convention.)
_NAME_SUFFIXES: tuple[str, ...] = (
    "Declaration",
    "Expression",
    "Specifier",
    "Reference",
    "Statement",
    "Modifier",
    "Ability",
)


def _derive_mtg_term(classname: str) -> str:
    """Strip the longest matching suffix and lowercase the remainder.

    Examples:
        DiesExpression   → "dies"
        AwakenAbility    → "awaken"
        WhenStatement    → "when"
        NameReference    → "name"
    """
    for suffix in _NAME_SUFFIXES:
        if classname.endswith(suffix):
            return classname[: -len(suffix)].lower()
    return classname.lower()


_DOCSTRING_RE = re.compile(r'"""(.*?)"""', re.DOTALL)


def _extract_docstring_first_line(source: str) -> str | None:
    """First non-empty line of the class docstring, if present.

    Returns None when the class has no docstring or it's empty.
    """
    match = _DOCSTRING_RE.search(source)
    if not match:
        return None
    body = match.group(1).strip()
    if not body:
        return None
    return body.splitlines()[0].strip()


def _derive_summary(ast_class: AstClassInfo) -> str:
    """One-line description of the AST class.

    Preference order: (1) the dataclass's docstring first line, (2) a
    generic shape line built from name + module + fields. Both feed
    L4's strategy prompt the same way the LLM-generated summary did.
    """
    doc = _extract_docstring_first_line(ast_class.source)
    if doc:
        return doc
    field_list = ", ".join(f"{name}: {anno}" for name, anno in ast_class.fields)
    parts = [f"{ast_class.classname} (in {ast_class.parent_module}.py)"]
    if field_list:
        parts.append(f"fields: {field_list}")
    return "; ".join(parts)


def _top_k_similar_handlers(
    *,
    query_source: str,
    exemplars: LowererExemplars,
    k: int = 5,
) -> list[str]:
    """TF-IDF + cosine over register-handler source text.

    Each row in the corpus is one ``RegisterHandler``, represented as
    its dispatcher + AST class + body text — so the vectoriser sees the
    same identifiers the playbook's L4/L5 prompts care about. Returns
    the bare AST class names (e.g. ``"AwakenAbility"``) of the top-k
    matches, dedup-preserved, capped at ``k``.

    Returns an empty list when there are no handlers (e.g. a fresh
    install where ``lowerer.py`` hasn't been initialised) so callers
    can still produce a schema-valid L3 dict.
    """
    handlers = list(exemplars.register_handlers)
    if not handlers:
        return []
    corpus = [
        f"{h.dispatcher} {h.ast_class}\n{h.body}"
        for h in handlers
    ]
    vec = TfidfVectorizer(
        ngram_range=(1, 2),
        token_pattern=r"[A-Za-z_][A-Za-z_0-9]*",
    )
    matrix = vec.fit_transform(corpus)
    query_vec = vec.transform([query_source])
    sims = cosine_similarity(query_vec, matrix).ravel()
    order = sims.argsort()[::-1]
    out: list[str] = []
    seen: set[str] = set()
    for idx in order:
        # `RegisterHandler.ast_class` is verbatim source like "ast.X" or
        # bare "X" depending on how the handler was written; strip the
        # `ast.` namespace prefix so the L4 prompt sees the bare name.
        name = handlers[int(idx)].ast_class.removeprefix("ast.")
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= k:
            break
    return out


def deterministic_l3(
    ast_class: AstClassInfo,
    exemplars: LowererExemplars,
    *,
    k: int = 5,
) -> dict[str, Any]:
    """Compute the L3 summary dict without any LLM call.

    Output schema matches :data:`llm.SUMMARY_SCHEMA` exactly so callers
    can route the result through the same JSON-string pipeline.
    """
    return {
        "summary": _derive_summary(ast_class),
        "mtg_term": _derive_mtg_term(ast_class.classname),
        "similar_handlers": _top_k_similar_handlers(
            query_source=ast_class.source,
            exemplars=exemplars,
            k=k,
        ),
    }


__all__ = ["deterministic_l3"]
