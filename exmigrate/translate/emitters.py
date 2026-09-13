"""Render a parsed formula as a SQL expression (SQLite / PostgreSQL) or pandas code.

Both emitters walk the same AST. Anything they cannot express raises
:class:`Unsupported`; :meth:`Emitter.emit` catches that per sub-expression,
records a ``TODO`` note and substitutes a null so the rest of the formula
still translates (``status == "partial"``).
"""

from __future__ import annotations

import keyword
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence

from exmigrate.contracts.ir import ColumnType
from exmigrate.contracts.translation import Dialect
from exmigrate.translate.parser import (
    Binary,
    Bool,
    Call,
    Empty,
    Expr,
    Num,
    Ref,
    Str,
    Unary,
)
from exmigrate.translate.semantics import (
    Aggregate,
    Criterion,
    Located,
    Lookup,
    Scope,
    Unsupported,
    aggregate_spec,
    inline_target,
    lookup_spec,
)

LOOKUP_FUNCS = frozenset({"VLOOKUP", "XLOOKUP", "INDEX"})
AGGREGATE_FUNCS = frozenset(
    {"SUMIF", "SUMIFS", "COUNTIF", "COUNTIFS", "AVERAGEIF", "AVERAGEIFS", "MINIFS", "MAXIFS"}
)
RANGE_AGGREGATES = {"SUM": "sum", "AVERAGE": "avg", "MIN": "min", "MAX": "max", "COUNT": "count"}
DYNAMIC_FUNCS = frozenset({"INDIRECT", "OFFSET"})


def frame_var(table: str) -> str:
    """Python variable name used for ``table`` in generated pandas code."""
    name = re.sub(r"\W", "_", table)
    if not name or name[0].isdigit() or keyword.iskeyword(name) or name.startswith("_"):
        name = f"t_{name}"
    return name


class Emitter(ABC):
    """AST → target-language expression text."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope

    def emit(self, expr: Expr, depth: int = 0) -> str:
        """Render ``expr``; unsupported parts become nulls with a ``TODO`` note."""
        try:
            return self.render(expr, depth)
        except Unsupported as exc:
            self.scope.partial = True
            self.scope.note(f"TODO {exc}")
            return self.null()

    def render(self, expr: Expr, depth: int) -> str:
        if isinstance(expr, Num):
            return self.number(expr.value)
        if isinstance(expr, Str):
            return self.string(expr.value)
        if isinstance(expr, Bool):
            return self.boolean(expr.value)
        if isinstance(expr, Empty):
            return self.null()
        if isinstance(expr, Ref):
            return self.reference(expr, depth)
        if isinstance(expr, Unary):
            return self.unary(expr, depth)
        if isinstance(expr, Binary):
            return self.binary(expr, depth)
        return self.call(expr, depth)

    def reference(self, ref: Ref, depth: int) -> str:
        if not ref.is_cell:
            raise Unsupported(
                f"{ref.text}: ranges are only supported inside lookup/aggregate functions"
            )
        located = self.scope.locate(ref)
        if not self.scope.is_current(located):
            raise Unsupported(f"{ref.text}: cell in another table without a lookup key")
        inlined = inline_target(ref, located, self.scope, depth)
        if inlined is not None:
            return self.group(self.emit(inlined, depth + 1))
        return self.column(located.column.name)

    def unary(self, expr: Unary, depth: int) -> str:
        operand = self.emit(expr.operand, depth)
        if expr.op == "-":
            return f"-{self.group(operand)}"
        if expr.op == "+":
            return operand
        if expr.op == "%":
            return self.group(f"{operand} / 100.0")
        raise Unsupported(f"operator {expr.op}")

    def binary(self, expr: Binary, depth: int) -> str:
        left, right = self.emit(expr.left, depth), self.emit(expr.right, depth)
        if expr.op == "&":
            return self.concat([self.text(left, expr.left), self.text(right, expr.right)])
        if expr.op == "^":
            return self.power(left, right)
        if expr.op == "/":
            return self.divide(left, right)
        if expr.op in {"=", "<>", "<", ">", "<=", ">="}:
            return self.group(f"{left} {self.compare_op(expr.op)} {right}")
        return self.group(f"{left} {expr.op} {right}")

    def call(self, call: Call, depth: int) -> str:
        name = call.name
        if name in DYNAMIC_FUNCS:
            raise Unsupported(f"{name}() resolves its target at runtime; rewrite by hand")
        if name in LOOKUP_FUNCS:
            return self.lookup(lookup_spec(call, self.scope), depth)
        if name in AGGREGATE_FUNCS:
            return self.aggregate(aggregate_spec(call, self.scope), depth)
        if name in RANGE_AGGREGATES:
            return self.range_aggregate(name, call.args, depth)
        args = [self.emit(a, depth) for a in call.args]
        return self.function(name, call.args, args, depth)

    def range_aggregate(self, name: str, args: Sequence[Expr], depth: int) -> str:
        """``SUM(B2,C2)`` / ``SUM(B2:D2)`` row-wise, or ``SUM(Sheet!B:B)`` over a whole table."""
        cells: list[str] = []
        for arg in args:
            if isinstance(arg, Ref) and arg.is_column:
                if len(args) != 1:
                    raise Unsupported(f"{name}: mixing a column range with other arguments")
                located = self.scope.locate(arg)
                return self.aggregate(
                    Aggregate(RANGE_AGGREGATES[name], located, (), located.table), depth
                )
            if isinstance(arg, Ref) and arg.row1 == arg.row2 and arg.row1 is not None:
                for col in range(arg.col1, arg.col2 + 1):
                    cell = Ref(arg.sheet, arg.book, col, arg.row1, col, arg.row1, arg.text)
                    cells.append(self.emit(cell, depth))
            elif isinstance(arg, Empty):
                continue
            else:
                cells.append(self.emit(arg, depth))
        if not cells:
            raise Unsupported(f"{name}() without arguments")
        return self.row_aggregate(RANGE_AGGREGATES[name], cells)

    def group(self, text: str) -> str:
        return f"({text})"

    def compare_op(self, op: str) -> str:
        return op

    # --- target specific -------------------------------------------------

    @abstractmethod
    def null(self) -> str: ...

    @abstractmethod
    def number(self, value: float) -> str: ...

    @abstractmethod
    def string(self, value: str) -> str: ...

    @abstractmethod
    def boolean(self, value: bool) -> str: ...

    @abstractmethod
    def column(self, name: str) -> str: ...

    @abstractmethod
    def text(self, rendered: str, expr: Expr) -> str: ...

    @abstractmethod
    def concat(self, parts: Sequence[str]) -> str: ...

    @abstractmethod
    def power(self, left: str, right: str) -> str: ...

    @abstractmethod
    def divide(self, left: str, right: str) -> str: ...

    @abstractmethod
    def lookup(self, spec: Lookup, depth: int) -> str: ...

    @abstractmethod
    def aggregate(self, spec: Aggregate, depth: int) -> str: ...

    @abstractmethod
    def row_aggregate(self, func: str, cells: Sequence[str]) -> str: ...

    @abstractmethod
    def function(self, name: str, raw: Sequence[Expr], args: Sequence[str], depth: int) -> str: ...


def _n(value: float) -> str:
    return str(int(value)) if value.is_integer() else repr(value)


class SqlEmitter(Emitter):
    """Correlated-subquery style SQL; ``t`` is the current row's table alias."""

    def __init__(self, scope: Scope, dialect: Dialect, sources: dict[str, str]) -> None:
        super().__init__(scope)
        self.dialect = dialect
        self.sources = sources
        self._alias = 0

    def source(self, table: str, columns: Sequence[Located]) -> str:
        """Relation to read from: the recompute view when an excluded column is needed."""
        if any(not c.column.include for c in columns):
            return quote(self.sources.get(table, table))
        return quote(table)

    def next_alias(self) -> str:
        self._alias += 1
        return f"r{self._alias}"

    def null(self) -> str:
        return "NULL"

    def number(self, value: float) -> str:
        return _n(value)

    def string(self, value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def boolean(self, value: bool) -> str:
        return "TRUE" if value else "FALSE"

    def column(self, name: str) -> str:
        return f"t.{quote(name)}"

    def text(self, rendered: str, expr: Expr) -> str:
        return rendered if isinstance(expr, Str) else f"CAST({rendered} AS TEXT)"

    def concat(self, parts: Sequence[str]) -> str:
        return self.group(" || ".join(parts))

    def power(self, left: str, right: str) -> str:
        if self.dialect == "sqlite":
            self.scope.note("POWER() needs SQLite built with math functions (3.35+)")
        return f"POWER({left}, {right})"

    def divide(self, left: str, right: str) -> str:
        return self.group(f"{left} * 1.0 / {right}")

    def lookup(self, spec: Lookup, depth: int) -> str:
        alias = self.next_alias()
        key = self.emit(spec.key, depth)
        sub = (
            f"(SELECT {alias}.{quote(spec.returned.column.name)} "
            f"FROM {self.source(spec.lookup.table.name, [spec.lookup, spec.returned])} {alias} "
            f"WHERE {alias}.{quote(spec.lookup.column.name)} = {key} LIMIT 1)"
        )
        if spec.default is not None:
            return f"COALESCE({sub}, {self.emit(spec.default, depth)})"
        return sub

    def aggregate(self, spec: Aggregate, depth: int) -> str:
        alias = self.next_alias()
        if spec.value is None:
            agg = "COUNT(*)"
        else:
            func = {"sum": "SUM", "count": "COUNT", "avg": "AVG", "min": "MIN", "max": "MAX"}
            value = f"{alias}.{quote(spec.value.column.name)}"
            numeric = spec.value.column.type in (ColumnType.INTEGER, ColumnType.FLOAT)
            if spec.func in {"sum", "avg"} and not numeric and self.dialect == "postgres":
                self.scope.note(f"{spec.value.key} is not numeric; cast to NUMERIC")
                value = f"CAST({value} AS NUMERIC)"
            agg = f"{func[spec.func]}({value})"
        if spec.func in {"sum", "count"}:
            agg = f"COALESCE({agg}, 0)"
        where = " AND ".join(self.criterion(c, alias, depth) for c in spec.criteria)
        used = [c.column for c in spec.criteria] + ([spec.value] if spec.value else [])
        sql = f"(SELECT {agg} FROM {self.source(spec.table.name, used)} {alias}"
        return sql + (f" WHERE {where})" if where else ")")

    def criterion(self, crit: Criterion, alias: str, depth: int) -> str:
        column = f"{alias}.{quote(crit.column.column.name)}"
        value = self.emit(crit.value, depth)
        if crit.op == "like":
            return f"{column} LIKE {value} ESCAPE '\\'"
        return f"{column} {crit.op} {value}"

    def row_aggregate(self, func: str, cells: Sequence[str]) -> str:
        if func == "sum":
            return self.group(" + ".join(f"COALESCE({c}, 0)" for c in cells))
        if func == "count":
            return self.group(
                " + ".join(f"(CASE WHEN {c} IS NULL THEN 0 ELSE 1 END)" for c in cells)
            )
        if func == "avg":
            total = " + ".join(f"COALESCE({c}, 0)" for c in cells)
            return self.group(f"({total}) * 1.0 / {len(cells)}")
        return self.least_greatest(func, cells)

    def least_greatest(self, func: str, cells: Sequence[str]) -> str:
        if len(cells) == 1:
            return cells[0]
        if self.dialect == "sqlite":
            return f"{func.upper()}({', '.join(cells)})"
        return f"{'LEAST' if func == 'min' else 'GREATEST'}({', '.join(cells)})"

    def function(self, name: str, raw: Sequence[Expr], args: Sequence[str], depth: int) -> str:
        pg = self.dialect == "postgres"
        a = list(args)
        n = len([r for r in raw if not isinstance(r, Empty)])
        if name == "IF":
            if n < 2:
                raise Unsupported("IF needs a condition and a value")
            else_ = a[2] if len(a) > 2 else "NULL"
            return f"CASE WHEN {a[0]} THEN {a[1]} ELSE {else_} END"
        if name == "IFS":
            pairs = [f"WHEN {a[i]} THEN {a[i + 1]}" for i in range(0, len(a) - 1, 2)]
            return f"CASE {' '.join(pairs)} END"
        if name in {"IFERROR", "IFNA"}:
            self.scope.note(f"{name} translated as COALESCE (only NULL, not errors, is caught)")
            return f"COALESCE({', '.join(a)})"
        if name == "AND":
            return self.group(" AND ".join(a))
        if name == "OR":
            return self.group(" OR ".join(a))
        if name == "NOT":
            return f"NOT {self.group(a[0])}"
        if name == "ROUND":
            digits = a[1] if len(a) > 1 else "0"
            return f"ROUND(CAST({a[0]} AS NUMERIC), {digits})" if pg else f"ROUND({a[0]}, {digits})"
        if name in {"ROUNDUP", "ROUNDDOWN"}:
            fn = "CEIL" if name == "ROUNDUP" else "FLOOR"
            if len(a) > 1 and a[1] != "0":
                scale = f"POWER(10, {a[1]})"
                return self.group(f"{fn}({a[0]} * {scale}) / {scale}")
            return f"{fn}({a[0]})"
        if name == "INT":
            return f"FLOOR({a[0]})"
        if name == "TRUNC":
            return f"TRUNC({a[0]})" if pg else f"CAST({a[0]} AS INTEGER)"
        if name == "ABS":
            return f"ABS({a[0]})"
        if name == "MOD":
            return f"MOD({a[0]}, {a[1]})" if pg else self.group(f"{a[0]} % {a[1]}")
        if name == "SQRT":
            return f"SQRT({a[0]})"
        if name == "LEN":
            return f"LENGTH({a[0]})"
        if name in {"UPPER", "LOWER", "TRIM"}:
            return f"{name}({a[0]})"
        if name == "LEFT":
            return f"SUBSTR({a[0]}, 1, {a[1] if len(a) > 1 else '1'})"
        if name == "RIGHT":
            count = a[1] if len(a) > 1 else "1"
            return f"RIGHT({a[0]}, {count})" if pg else f"SUBSTR({a[0]}, -({count}))"
        if name == "MID":
            return f"SUBSTR({a[0]}, {a[1]}, {a[2]})"
        if name in {"CONCATENATE", "CONCAT"}:
            return self.concat([self.text(x, r) for x, r in zip(a, raw, strict=True)])
        if name == "TEXT":
            self.scope.note("TEXT() format string dropped; value cast to text")
            return f"CAST({a[0]} AS TEXT)"
        if name == "VALUE":
            return f"CAST({a[0]} AS NUMERIC)" if pg else f"CAST({a[0]} AS REAL)"
        if name == "SUBSTITUTE":
            return f"REPLACE({a[0]}, {a[1]}, {a[2]})"
        if name in {"FIND", "SEARCH"}:
            return f"POSITION({a[0]} IN {a[1]})" if pg else f"INSTR({a[1]}, {a[0]})"
        if name == "TODAY":
            return "CURRENT_DATE"
        if name == "NOW":
            return "CURRENT_TIMESTAMP"
        if name in {"YEAR", "MONTH", "DAY"}:
            if pg:
                return f"EXTRACT({name} FROM {a[0]})"
            fmt = {"YEAR": "%Y", "MONTH": "%m", "DAY": "%d"}[name]
            return f"CAST(strftime('{fmt}', {a[0]}) AS INTEGER)"
        if name == "ISBLANK":
            return self.group(f"{a[0]} IS NULL")
        if name in {"ROW", "COLUMN"}:
            raise Unsupported(f"{name}() has no meaning for a table row")
        raise Unsupported(f"{name}() has no SQL translation")


class PandasEmitter(Emitter):
    """Vectorised pandas over one DataFrame per table; relies on helpers in the script header."""

    def __init__(self, scope: Scope) -> None:
        super().__init__(scope)
        self.frame = frame_var(scope.table.name)

    def null(self) -> str:
        return "np.nan"

    def number(self, value: float) -> str:
        return _n(value)

    def string(self, value: str) -> str:
        return repr(value)

    def boolean(self, value: bool) -> str:
        return "True" if value else "False"

    def column(self, name: str) -> str:
        return f"{self.frame}[{name!r}]"

    def text(self, rendered: str, expr: Expr) -> str:
        return rendered if isinstance(expr, Str) else f"_s({rendered})"

    def concat(self, parts: Sequence[str]) -> str:
        return self.group(" + ".join(parts))

    def power(self, left: str, right: str) -> str:
        return self.group(f"{left} ** {right}")

    def divide(self, left: str, right: str) -> str:
        return self.group(f"{left} / {right}")

    def compare_op(self, op: str) -> str:
        return {"=": "==", "<>": "!="}.get(op, op)

    def lookup(self, spec: Lookup, depth: int) -> str:
        key = self.emit(spec.key, depth)
        text = (
            f"_lookup({key}, {frame_var(spec.lookup.table.name)}, "
            f"{spec.lookup.column.name!r}, {spec.returned.column.name!r})"
        )
        if spec.default is not None:
            return f"{text}.fillna({self.emit(spec.default, depth)})"
        return text

    def aggregate(self, spec: Aggregate, depth: int) -> str:
        value = "None" if spec.value is None else repr(spec.value.column.name)
        crits = ", ".join(
            f"({c.column.column.name!r}, {c.op!r}, {self.emit(c.value, depth)})"
            for c in spec.criteria
        )
        return (
            f"_aggif({spec.func!r}, {frame_var(spec.table.name)}, {value}, "
            f"[{crits}], {self.frame}.index)"
        )

    def row_aggregate(self, func: str, cells: Sequence[str]) -> str:
        return f"_rowagg({func!r}, [{', '.join(cells)}], {self.frame}.index)"

    def function(self, name: str, raw: Sequence[Expr], args: Sequence[str], depth: int) -> str:
        a = list(args)
        n = len([r for r in raw if not isinstance(r, Empty)])
        if name == "IF":
            if n < 2:
                raise Unsupported("IF needs a condition and a value")
            return f"_if({a[0]}, {a[1]}, {a[2] if len(a) > 2 else 'np.nan'})"
        if name == "IFS":
            pairs = ", ".join(f"({a[i]}, {a[i + 1]})" for i in range(0, len(a) - 1, 2))
            return f"_ifs([{pairs}], {self.frame}.index)"
        if name in {"IFERROR", "IFNA"}:
            self.scope.note(f"{name} translated as fillna (only missing values are caught)")
            return f"_series({a[0]}, {self.frame}.index).fillna({a[1]})"
        if name == "AND":
            return self.group(" & ".join(self.group(x) for x in a))
        if name == "OR":
            return self.group(" | ".join(self.group(x) for x in a))
        if name == "NOT":
            return f"~{self.group(a[0])}"
        if name == "ROUND":
            return f"np.round({a[0]}, {a[1] if len(a) > 1 else '0'})"
        if name in {"ROUNDUP", "ROUNDDOWN"}:
            fn = "np.ceil" if name == "ROUNDUP" else "np.floor"
            if len(a) > 1 and a[1] != "0":
                return self.group(f"{fn}({a[0]} * 10 ** {a[1]}) / 10 ** {a[1]}")
            return f"{fn}({a[0]})"
        if name == "INT":
            return f"np.floor({a[0]})"
        if name == "TRUNC":
            return f"np.trunc({a[0]})"
        if name == "ABS":
            return f"np.abs({a[0]})"
        if name == "MOD":
            return f"np.mod({a[0]}, {a[1]})"
        if name == "SQRT":
            return f"np.sqrt({a[0]})"
        if name == "LEN":
            return f"_s({a[0]}).str.len()"
        if name == "UPPER":
            return f"_s({a[0]}).str.upper()"
        if name == "LOWER":
            return f"_s({a[0]}).str.lower()"
        if name == "TRIM":
            return f"_s({a[0]}).str.strip()"
        if name == "LEFT":
            return f"_s({a[0]}).str[:{a[1] if len(a) > 1 else '1'}]"
        if name == "RIGHT":
            return f"_s({a[0]}).str[-({a[1] if len(a) > 1 else '1'}):]"
        if name == "MID":
            return f"_s({a[0]}).str[{a[1]} - 1:{a[1]} - 1 + {a[2]}]"
        if name in {"CONCATENATE", "CONCAT"}:
            return self.concat([self.text(x, r) for x, r in zip(a, raw, strict=True)])
        if name == "TEXT":
            self.scope.note("TEXT() format string dropped; value cast to text")
            return f"_s({a[0]})"
        if name == "VALUE":
            return f"pd.to_numeric({a[0]}, errors='coerce')"
        if name == "SUBSTITUTE":
            return f"_s({a[0]}).str.replace({a[1]}, {a[2]}, regex=False)"
        if name in {"FIND", "SEARCH"}:
            return f"(_s({a[1]}).str.find({a[0]}) + 1)"
        if name == "TODAY":
            return "pd.Timestamp.today().normalize()"
        if name == "NOW":
            return "pd.Timestamp.now()"
        if name in {"YEAR", "MONTH", "DAY"}:
            return f"pd.to_datetime({a[0]}).dt.{name.lower()}"
        if name == "ISBLANK":
            return f"_series({a[0]}, {self.frame}.index).isna()"
        if name in {"ROW", "COLUMN"}:
            raise Unsupported(f"{name}() has no meaning for a table row")
        raise Unsupported(f"{name}() has no pandas translation")


def quote(ident: str) -> str:
    """Double-quote a SQL identifier."""
    return '"' + ident.replace('"', '""') + '"'
