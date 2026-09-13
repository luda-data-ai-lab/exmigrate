"""Resolve formula references against the Schema IR.

The parser knows only sheets and column letters; this module maps them onto
IR tables/columns and recognises the lookup and conditional-aggregate call
shapes both emitters render (as correlated subqueries or pandas helpers).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from exmigrate.analyzer.keys import table_by_sheet
from exmigrate.contracts.ir import ColumnIR, SchemaIR, TableIR
from exmigrate.translate.parser import Binary, Bool, Call, Empty, Expr, Num, Ref, Str

_CRITERIA_RE = re.compile(r"^(<>|<=|>=|<|>|=)(.*)$", re.DOTALL)
COMPARISONS = frozenset({"=", "<>", "<", ">", "<=", ">="})
INLINE_DEPTH = 5


class Unsupported(Exception):
    """A sub-expression that has no SQL/pandas equivalent; carries the reason."""


@dataclass(frozen=True)
class Located:
    """An IR column a reference points at."""

    table: TableIR
    column: ColumnIR

    @property
    def key(self) -> str:
        return f"{self.table.name}.{self.column.name}"


@dataclass(frozen=True)
class Criterion:
    """``column <op> value`` on the aggregated table; ``like`` uses ``%``/``_`` wildcards."""

    column: Located
    op: str
    value: Expr


@dataclass(frozen=True)
class Lookup:
    key: Expr
    lookup: Located
    returned: Located
    default: Expr | None = None


@dataclass(frozen=True)
class Aggregate:
    func: str
    value: Located | None
    criteria: tuple[Criterion, ...]
    table: TableIR


@dataclass
class Scope:
    """Where a formula lives and what it may refer to."""

    ir: SchemaIR
    table: TableIR
    formulas: dict[str, Expr] = field(default_factory=dict)
    """``table.column`` → parsed formula of excluded derived columns (for inlining)."""
    notes: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    partial: bool = False

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def locate(self, ref: Ref, column: int | None = None) -> Located:
        """IR column for ``ref`` (or for ``column`` within ``ref``'s sheet)."""
        file = ref.book or self.table.source_file
        sheet = ref.sheet or self.table.source_sheet
        table = table_by_sheet(self.ir, file, sheet)
        if table is None:
            where = f"[{ref.book}]{sheet}" if ref.book else sheet
            raise Unsupported(f"{ref.text}: sheet '{where}' is not an uploaded table")
        index = ref.col1 if column is None else column
        if index >= len(table.columns):
            raise Unsupported(f"{ref.text}: column {index + 1} is outside table '{table.name}'")
        located = Located(table, table.columns[index])
        if located.table is not self.table or located.column.derived:
            self._depend(located)
        return located

    def _depend(self, located: Located) -> None:
        if located.key not in self.depends_on:
            self.depends_on.append(located.key)

    def is_current(self, located: Located) -> bool:
        return located.table is self.table


def lookup_spec(call: Call, scope: Scope) -> Lookup:
    """``VLOOKUP`` / ``XLOOKUP`` / ``INDEX(…, MATCH(…))`` → :class:`Lookup`."""
    name, args = call.name, [a for a in call.args if not isinstance(a, Empty)]
    if name == "VLOOKUP":
        if len(call.args) < 3:
            raise Unsupported("VLOOKUP needs at least 3 arguments")
        rng, idx = call.args[1], call.args[2]
        if not isinstance(rng, Ref) or not isinstance(idx, Num):
            raise Unsupported("VLOOKUP: table array must be a range and index a literal")
        lookup = scope.locate(rng)
        returned = scope.locate(rng, rng.col1 + int(idx.value) - 1)
        if len(args) < 4 or _truthy(args[3]):
            scope.note("VLOOKUP approximate match (range_lookup omitted/TRUE) translated as exact")
        return Lookup(call.args[0], lookup, returned)
    if name == "XLOOKUP":
        if len(args) < 3 or not isinstance(args[1], Ref) or not isinstance(args[2], Ref):
            raise Unsupported("XLOOKUP: lookup_array and return_array must be ranges")
        lookup, returned = scope.locate(args[1]), scope.locate(args[2])
        if lookup.table is not returned.table:
            raise Unsupported("XLOOKUP: lookup and return arrays are in different tables")
        raw = call.args[3] if len(call.args) > 3 else Empty()
        default = None if isinstance(raw, Empty) else raw
        if len(call.args) > 4:
            scope.note("XLOOKUP match_mode/search_mode ignored (exact match)")
        return Lookup(args[0], lookup, returned, default)
    if name == "INDEX":
        if len(args) != 2 or not isinstance(args[0], Ref) or not isinstance(args[1], Call):
            raise Unsupported(
                "INDEX is only supported as INDEX(return_range, MATCH(key, range, 0))"
            )
        match = args[1]
        margs = [a for a in match.args if not isinstance(a, Empty)]
        if match.name != "MATCH" or len(margs) < 2 or not isinstance(margs[1], Ref):
            raise Unsupported(
                "INDEX is only supported as INDEX(return_range, MATCH(key, range, 0))"
            )
        if len(margs) < 3 or not (isinstance(margs[2], Num) and margs[2].value == 0):
            scope.note("MATCH approximate match translated as exact")
        returned, lookup = scope.locate(args[0]), scope.locate(margs[1])
        if lookup.table is not returned.table:
            raise Unsupported("INDEX/MATCH: ranges are in different tables")
        return Lookup(margs[0], lookup, returned)
    raise Unsupported(f"{name} is not a supported lookup")


def aggregate_spec(call: Call, scope: Scope) -> Aggregate:
    """``SUMIF(S)``/``COUNTIF(S)``/``AVERAGEIF(S)``/``MINIFS``/``MAXIFS`` → :class:`Aggregate`."""
    name, args = call.name, [a for a in call.args if not isinstance(a, Empty)]
    func = {"SUM": "sum", "COUNT": "count", "AVERAGE": "avg", "MIN": "min", "MAX": "max"}[
        name.removesuffix("IFS").removesuffix("IF")
    ]
    value: Located | None = None
    pairs: list[tuple[Expr, Expr]]
    if name in {"SUMIF", "COUNTIF", "AVERAGEIF"}:
        if len(args) < 2:
            raise Unsupported(f"{name} needs at least 2 arguments")
        pairs = [(args[0], args[1])]
        if name != "COUNTIF":
            value_ref = args[2] if len(args) > 2 else args[0]
            value = _column(value_ref, scope)
    else:
        start = 0
        if name != "COUNTIFS":
            if not args:
                raise Unsupported(f"{name} needs a value range")
            value = _column(args[0], scope)
            start = 1
        pairs = [(args[i], args[i + 1]) for i in range(start, len(args) - 1, 2)]
        if not pairs:
            raise Unsupported(f"{name} needs criteria range/criteria pairs")
    criteria = tuple(criterion(rng, crit, scope) for rng, crit in pairs)
    table = criteria[0].column.table
    for crit in criteria:
        if crit.column.table is not table:
            raise Unsupported(f"{name}: criteria ranges span several tables")
    if value is not None and value.table is not table:
        raise Unsupported(f"{name}: value range and criteria range are in different tables")
    return Aggregate(func, value, criteria, table)


def criterion(rng: Expr, crit: Expr, scope: Scope) -> Criterion:
    """Excel criteria (``">10"``, ``"<>"&B2``, ``"a*"``, ``B2``) → :class:`Criterion`."""
    column = _column(rng, scope)
    if isinstance(crit, Str):
        m = _CRITERIA_RE.match(crit.value)
        if m:
            return Criterion(column, m.group(1), _literal(m.group(2)))
        if "*" in crit.value or "?" in crit.value:
            pattern = crit.value.replace("%", "\\%").replace("_", "\\_")
            return Criterion(column, "like", Str(pattern.replace("*", "%").replace("?", "_")))
        return Criterion(column, "=", crit)
    if isinstance(crit, Binary) and crit.op == "&" and isinstance(crit.left, Str):
        op = crit.left.value.strip()
        if op in COMPARISONS:
            return Criterion(column, op, crit.right)
    return Criterion(column, "=", crit)


def _column(expr: Expr, scope: Scope) -> Located:
    if not isinstance(expr, Ref) or not expr.is_column:
        raise Unsupported("expected a single-column range")
    return scope.locate(expr)


def _literal(text: str) -> Expr:
    try:
        return Num(float(text))
    except ValueError:
        return Str(text)


def _truthy(expr: Expr) -> bool:
    if isinstance(expr, Bool):
        return expr.value
    if isinstance(expr, Num):
        return expr.value != 0
    return True


def inline_target(ref: Ref, located: Located, scope: Scope, depth: int) -> Expr | None:
    """Formula to substitute for a same-table excluded derived column, if any."""
    if not scope.is_current(located) or located.column.include:
        return None
    formula = scope.formulas.get(located.key)
    if formula is None:
        return None
    if depth >= INLINE_DEPTH:
        raise Unsupported(f"{ref.text}: derived columns nest too deeply to inline")
    return formula
