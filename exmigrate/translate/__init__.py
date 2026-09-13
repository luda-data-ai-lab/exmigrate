"""Phase 4: translate formula-derived columns into SQL views and a pandas script."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter

from exmigrate.contracts.formulas import FormulaColumn, FormulaInventory
from exmigrate.contracts.ir import ColumnIR, SchemaIR, TableIR
from exmigrate.contracts.translation import DIALECTS, Dialect, TranslatedColumn, Translation
from exmigrate.translate.emitters import PandasEmitter, SqlEmitter, frame_var, quote
from exmigrate.translate.parser import Expr, ParseError, parse
from exmigrate.translate.semantics import Scope

VIEW_SUFFIX = "_v"
CALC_SUFFIX = "_calc"


@dataclass
class _Target:
    table: TableIR
    column: ColumnIR
    fact: FormulaColumn
    ast: Expr | None
    error: str | None = None

    @property
    def key(self) -> str:
        return f"{self.table.name}.{self.column.name}"


def translate(ir: SchemaIR, inventory: FormulaInventory) -> Translation:
    """Translate every formula-derived column of ``ir`` and render the artefacts."""
    targets = _targets(ir, inventory)
    formulas = {t.key: t.ast for t in targets if t.ast is not None and not t.column.include}
    sources = {t.table.name: t.table.name + VIEW_SUFFIX for t in targets if not t.column.include}
    columns: list[TranslatedColumn] = []
    for target in targets:
        columns.append(_translate_one(ir, target, formulas, sources))
    translation = Translation(columns=columns)
    ordered = order_columns(columns)
    translation.views = {dialect: render_views(ir, ordered, dialect) for dialect in DIALECTS}
    translation.script = render_script(ir, ordered)
    return translation


def _targets(ir: SchemaIR, inventory: FormulaInventory) -> list[_Target]:
    tables = {t.name: t for t in ir.tables}
    out: list[_Target] = []
    for fact in inventory.columns:
        table = tables.get(fact.table)
        if table is None or fact.column_index is None or fact.column_index >= len(table.columns):
            continue
        column = table.columns[fact.column_index]
        if not column.derived:
            continue
        books = inventory.books.get(fact.file, [])
        try:
            out.append(_Target(table, column, fact, parse(fact.sample, books)))
        except ParseError as exc:
            out.append(_Target(table, column, fact, None, str(exc)))
    return out


def _translate_one(
    ir: SchemaIR, target: _Target, formulas: dict[str, Expr], sources: dict[str, str]
) -> TranslatedColumn:
    base = TranslatedColumn(
        table=target.table.name,
        column=target.column.name,
        include=target.column.include,
        formula=target.fact.sample,
        status="unsupported",
    )
    if target.ast is None:
        base.notes.append(f"TODO cannot parse formula: {target.error}")
        base.sql = dict.fromkeys(DIALECTS, "NULL")
        base.pandas = "np.nan"
        return base
    notes: list[str] = []
    depends: list[str] = []
    for dialect in DIALECTS:
        scope = Scope(ir, target.table, formulas)
        emitter = SqlEmitter(scope, dialect, sources)
        base.sql[dialect] = emitter.emit(target.ast)
        _merge(notes, scope.notes)
        _merge(depends, scope.depends_on)
        if dialect == "sqlite":
            partial, empty = scope.partial, base.sql[dialect] == emitter.null()
    scope = Scope(ir, target.table, formulas)
    pandas_emitter = PandasEmitter(scope)
    base.pandas = pandas_emitter.emit(target.ast)
    _merge(notes, scope.notes)
    base.notes = notes
    base.depends_on = depends
    base.status = "unsupported" if partial and empty else ("partial" if partial else "ok")
    return base


def _merge(into: list[str], items: Iterable[str]) -> None:
    for item in items:
        if item not in into:
            into.append(item)


def order_columns(columns: Sequence[TranslatedColumn]) -> list[TranslatedColumn]:
    """Columns sorted so that every derived dependency is computed first."""
    by_key = {f"{c.table}.{c.column}": c for c in columns}
    sorter: TopologicalSorter[str] = TopologicalSorter()
    for key, col in by_key.items():
        sorter.add(key, *(d for d in col.depends_on if d in by_key and d != key))
    try:
        return [by_key[k] for k in sorter.static_order()]
    except CycleError:
        return list(columns)


def _output_name(col: TranslatedColumn) -> str:
    return col.column if not col.include else col.column + CALC_SUFFIX


def render_views(ir: SchemaIR, columns: Sequence[TranslatedColumn], dialect: Dialect) -> str:
    """SQL script creating a ``<table>_v`` view per table with formula columns."""
    statements = view_statements(columns, dialect)
    if not statements:
        return ""
    header = (
        f"-- exmigrate recompute views ({dialect}). Excluded derived columns are\n"
        f"-- recreated under their own name; included ones get a '{CALC_SUFFIX}' twin\n"
        "-- so cached Excel values can be compared with the recomputed ones.\n\n"
    )
    return header + ";\n\n".join(statements) + ";\n"


def view_statements(columns: Sequence[TranslatedColumn], dialect: Dialect) -> list[str]:
    """``DROP VIEW`` / ``CREATE VIEW`` statements (no trailing ``;``) in dependency order."""
    per_table: dict[str, list[TranslatedColumn]] = {}
    for col in columns:
        per_table.setdefault(col.table, []).append(col)
    out: list[str] = []
    for name in _table_order(per_table):
        cols = per_table[name]
        view = quote(name + VIEW_SUFFIX)
        out.append(f"DROP VIEW IF EXISTS {view}")
        lines = [f"CREATE VIEW {view} AS", "SELECT t.*,"]
        for i, col in enumerate(cols):
            lines.append(f"  -- {col.column}: {col.formula}")
            for note in col.notes:
                lines.append(f"  -- {note}")
            comma = "," if i < len(cols) - 1 else ""
            lines.append(f"  {col.sql[dialect]} AS {quote(_output_name(col))}{comma}")
        lines.append(f"FROM {quote(name)} t")
        out.append("\n".join(lines))
    return out


def _table_order(per_table: dict[str, list[TranslatedColumn]]) -> list[str]:
    sorter: TopologicalSorter[str] = TopologicalSorter()
    for name, cols in per_table.items():
        deps = {d.split(".", 1)[0] for c in cols for d in c.depends_on}
        sorter.add(name, *(d for d in deps if d in per_table and d != name))
    try:
        return list(sorter.static_order())
    except CycleError:
        return list(per_table)


_HELPERS = '''
def _s(x):
    """Excel-style text coercion."""
    if isinstance(x, pd.Series):
        return x.astype("string").fillna("")
    return "" if x is None or (isinstance(x, float) and np.isnan(x)) else str(x)


def _series(x, index):
    return x if isinstance(x, pd.Series) else pd.Series(x, index=index)


def _if(cond, a, b):
    cond = _series(cond, None).fillna(False).astype(bool)
    return pd.Series(np.where(cond, a, b), index=cond.index)


def _ifs(pairs, index):
    out = pd.Series(np.nan, index=index, dtype=object)
    for cond, value in reversed(pairs):
        out = _if(_series(cond, index), value, out)
    return out


def _lookup(key, table, key_col, ret_col):
    """VLOOKUP/XLOOKUP exact match: first matching row wins."""
    mapping = table.drop_duplicates(key_col).set_index(key_col)[ret_col]
    return _series(key, None).map(mapping)


def _cmp(series, op, value):
    if op == "=":
        return series == value
    if op == "<>":
        return series != value
    if op == "<":
        return series < value
    if op == ">":
        return series > value
    if op == "<=":
        return series <= value
    if op == ">=":
        return series >= value
    pattern = "^" + re.escape(str(value)).replace("%", ".*").replace("_", ".") + "$"
    return _s(series).str.match(pattern)


def _agg(frame, func, value_col):
    if value_col is None:
        return len(frame)
    return frame[value_col].agg({"avg": "mean"}.get(func, func))


def _aggif(func, frame, value_col, criteria, index):
    """SUMIF(S)/COUNTIF(S)/AVERAGEIF(S)/MINIFS/MAXIFS; criteria = [(column, op, value)]."""
    for col, op, value in criteria:
        if not isinstance(value, pd.Series):
            frame = frame[_cmp(frame[col], op, value)]
    keyed = [(c, op, v) for c, op, v in criteria if isinstance(v, pd.Series)]
    if not keyed:
        out = pd.Series(_agg(frame, func, value_col), index=index)
    elif all(op == "=" for _, op, _ in keyed):
        cols = [c for c, _, _ in keyed]
        grouped = frame.groupby(cols, sort=False)
        agg = grouped.size() if value_col is None else grouped[value_col].agg(
            {"avg": "mean"}.get(func, func)
        )
        keys = (
            pd.MultiIndex.from_arrays([v for _, _, v in keyed])
            if len(cols) > 1
            else keyed[0][2]
        )
        out = pd.Series(agg.reindex(keys).to_numpy(), index=index)
    else:
        values = []
        for i in range(len(index)):
            mask = np.logical_and.reduce([_cmp(frame[c], op, v.iloc[i]) for c, op, v in keyed])
            values.append(_agg(frame[mask], func, value_col))
        out = pd.Series(values, index=index)
    if func in ("sum", "count"):
        return pd.to_numeric(out, errors="coerce").fillna(0)
    return out


def _rowagg(func, cells, index):
    frame = pd.concat([_series(c, index) for c in cells], axis=1)
    if func == "count":
        return frame.notna().sum(axis=1)
    return frame.agg({"avg": "mean"}.get(func, func), axis=1)
'''


def render_script(ir: SchemaIR, columns: Sequence[TranslatedColumn]) -> str:
    """Standalone pandas script recomputing all formula columns from migrated tables."""
    tables = sorted(
        {c.table for c in columns} | {d.split(".", 1)[0] for c in columns for d in c.depends_on}
    )
    known = {t.name for t in ir.tables}
    tables = [t for t in tables if t in known]
    lines = [
        '"""Recompute Excel formula columns from the migrated tables (generated by exmigrate).',
        "",
        "Usage: python recompute.py migration.db      # SQLite file produced by exmigrate",
        "Excluded derived columns are recreated under their own name; included ones get a",
        f'"{CALC_SUFFIX}" twin for comparison with the cached Excel values.',
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import re",
        "import sqlite3",
        "import sys",
        "",
        "import numpy as np",
        "import pandas as pd",
        "",
        f"TABLES = {tables!r}",
        _HELPERS,
        "",
        "def recompute(tables: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:",
        '    """Add the recomputed columns in dependency order."""',
    ]
    for name in tables:
        lines.append(f"    {frame_var(name)} = tables[{name!r}]")
    if not columns:
        lines.append("    return tables")
    for col in columns:
        lines.append("")
        lines.append(f"    # {col.table}.{col.column}: {col.formula}")
        for note in col.notes:
            lines.append(f"    # {note}")
        lines.append(f"    {frame_var(col.table)}[{_output_name(col)!r}] = {col.pandas}")
    if columns:
        lines.append("    return tables")
    lines += [
        "",
        "",
        "def load(conn: sqlite3.Connection) -> dict[str, pd.DataFrame]:",
        "    return {n: pd.read_sql_query(f'SELECT * FROM \"{n}\"', conn) for n in TABLES}",
        "",
        "",
        'if __name__ == "__main__":',
        "    with sqlite3.connect(sys.argv[1]) as conn:",
        "        result = recompute(load(conn))",
        "    for name, frame in result.items():",
        '        print(f"== {name} ({len(frame)} rows)")',
        "        print(frame.head().to_string())",
        "",
    ]
    return "\n".join(lines)
