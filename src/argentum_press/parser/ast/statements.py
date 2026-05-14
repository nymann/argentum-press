# pyright: basic
"""Statement dataclasses for the parser AST.

Statements organise expressions into the instruction sequences that make up
an ability's body. Conditional statements (``if``, ``when``, ``whenever``,
``at``, ``during``, ``until``, etc.) preserve their distinct kinds since each
has subtly different semantics for triggers vs conditions.

The base classes (``Statement``, ``ConditionalStatement``) are plain Python
classes used only for ``isinstance``/``match`` dispatch.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argentum_press.parser.ast.expressions import Expression


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class Statement:
    """Base for the closed statement hierarchy."""


class ConditionalStatement(Statement):
    """Base for if/when/whenever/at/until/... statements."""


# ---------------------------------------------------------------------------
# Composite statements
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatementBlock(Statement):
    """A sequence of statements that make up a single ability body."""

    statements: tuple[Statement, ...] = ()


class CompoundTerminator(Enum):
    THEN = "then"
    AND = "and"
    INSTEAD = "instead"


@dataclass(frozen=True, slots=True)
class CompoundStatement(Statement):
    """Comma-separated clauses terminated with ``then`` or ``and``."""

    statements: tuple[Statement, ...] = ()
    terminator: CompoundTerminator = CompoundTerminator.THEN


@dataclass(frozen=True, slots=True)
class ExpressionStatement(Statement):
    """A statement whose body is a single expression/term."""

    root: Expression


@dataclass(frozen=True, slots=True)
class MayStatement(Statement):
    """``<player> may <statement>``."""

    player: Expression
    statement: Statement


@dataclass(frozen=True, slots=True)
class BeingStatement(Statement):
    """Statement of being/status (``~ is an Elf``, ``~ has flying``, ``~ can't block``).

    ``lhs`` is None when implied (e.g. in a compound predicate where the LHS
    is bound to the previous clause).
    """

    rhs: Expression
    lhs: Expression | None = None

    @property
    def implied_lhs(self) -> bool:
        return self.lhs is None


@dataclass(frozen=True, slots=True)
class CostIncreaseStatement(Statement):
    """``<spells> cost {X} more to cast/activate``."""

    subject: Expression
    amount: Expression


@dataclass(frozen=True, slots=True)
class IsStatement(Statement):
    """Reed left this stubbed; carried forward as a marker."""


@dataclass(frozen=True, slots=True)
class ThenStatement(Statement):
    """``Then <body>``."""

    body: Statement


@dataclass(frozen=True, slots=True)
class KeywordAbilityListStatement(Statement):
    """Comma-separated sequence of keyword abilities (``flying, haste, first strike``)."""

    abilities: tuple[object, ...] = ()  # tuple[KeywordAbility, ...] avoiding circular import


# ---------------------------------------------------------------------------
# Conditional statements
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IfStatement(ConditionalStatement):
    conditional: Statement | Expression
    consequence: Statement
    inverted: bool = False


@dataclass(frozen=True, slots=True)
class WheneverStatement(ConditionalStatement):
    conditional: Statement | Expression
    consequence: Statement
    inverted: bool = False


@dataclass(frozen=True, slots=True)
class WhenStatement(ConditionalStatement):
    conditional: Statement | Expression
    consequence: Statement
    inverted: bool = False


@dataclass(frozen=True, slots=True)
class AtStatement(ConditionalStatement):
    conditional: Statement | Expression
    consequence: Statement
    inverted: bool = False


@dataclass(frozen=True, slots=True)
class AsLongAsStatement(ConditionalStatement):
    conditional: Statement | Expression
    consequence: Statement
    inverted: bool = False


@dataclass(frozen=True, slots=True)
class AsStatement(ConditionalStatement):
    conditional: Statement | Expression
    consequence: Statement
    inverted: bool = False


@dataclass(frozen=True, slots=True)
class UntilStatement(ConditionalStatement):
    conditional: Statement | Expression
    consequence: Statement
    inverted: bool = False


@dataclass(frozen=True, slots=True)
class OtherwiseStatement(ConditionalStatement):
    conditional: Statement | Expression
    consequence: Statement


@dataclass(frozen=True, slots=True)
class DuringStatement(ConditionalStatement):
    conditional: Statement | Expression
    consequence: Statement
    exclusive: bool = False  # "only during"


@dataclass(frozen=True, slots=True)
class UnlessStatement(ConditionalStatement):
    conditional: Statement | Expression
    consequence: Statement


@dataclass(frozen=True, slots=True)
class ExceptStatement(ConditionalStatement):
    conditional: Statement | Expression
    consequence: Statement | Expression


@dataclass(frozen=True, slots=True)
class ForStatement(ConditionalStatement):
    """``for each X, …``. Reed left this stubbed."""

    conditional: Statement | Expression | None = None
    consequence: Statement | None = None


@dataclass(frozen=True, slots=True)
class WhileStatement(ConditionalStatement):
    conditional: Statement | Expression
    consequence: Statement
    inverted: bool = False


# ---------------------------------------------------------------------------
# Activation, ability-sequence, quoted-ability statements
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActivationStatement(Statement):
    """``<cost>: <instructions>`` — the body of an activated ability."""

    cost: Expression
    instructions: Statement


@dataclass(frozen=True, slots=True)
class ActivationRestrictionStatement(Statement):
    """``Activate only as a sorcery`` — sole form the grammar admits today."""


@dataclass(frozen=True, slots=True)
class AbilitySequenceStatement(Statement):
    """``flying and haste``-style sequence used in token descriptions."""

    abilities: tuple[object, ...] = ()  # tuple[Ability, ...]


@dataclass(frozen=True, slots=True)
class QuotedAbilityStatement(Statement):
    """A statement block in double quotes (granted abilities, emblems)."""

    block: StatementBlock
