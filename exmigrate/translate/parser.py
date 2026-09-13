"""Excel formula → small expression AST, built on openpyxl's tokenizer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from openpyxl.formula.tokenizer import Token, Tokenizer, TokenizerError
from openpyxl.utils.cell import column_index_from_string


class ParseError(ValueError):
    """The formula uses syntax the translator does not understand."""


@dataclass(frozen=True)
class Num:
    value: float


@dataclass(frozen=True)
class Str:
    value: str


@dataclass(frozen=True)
class Bool:
    value: bool


@dataclass(frozen=True)
class Ref:
    """A cell or rectangular range; columns 0-based, rows 1-based (``None`` = whole column)."""

    sheet: str | None
    book: str | None
    col1: int
    row1: int | None
    col2: int
    row2: int | None
    text: str

    @property
    def is_cell(self) -> bool:
        return self.col1 == self.col2 and self.row1 == self.row2 and self.row1 is not None

    @property
    def is_column(self) -> bool:
        """A single column, whole or a vertical run of rows."""
        return self.col1 == self.col2 and not self.is_cell


@dataclass(frozen=True)
class Unary:
    op: str
    operand: Expr


@dataclass(frozen=True)
class Binary:
    op: str
    left: Expr
    right: Expr


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple[Expr, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Empty:
    """An omitted argument, e.g. the middle of ``IF(a,,b)``."""


Expr = Num | Str | Bool | Ref | Unary | Binary | Call | Empty

_PRECEDENCE = {
    "=": 1,
    "<>": 1,
    "<": 1,
    ">": 1,
    "<=": 1,
    ">=": 1,
    "&": 2,
    "+": 3,
    "-": 3,
    "*": 4,
    "/": 4,
    "^": 5,
}
_BOOK_RE = re.compile(r"^\[(\d+)\](.*)$")
_CELL_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)$")
_COL_RE = re.compile(r"^\$?([A-Za-z]{1,3})$")
_ROW_RE = re.compile(r"^\$?(\d+)$")


def parse(formula: str, books: list[str] | None = None) -> Expr:
    """Parse ``formula`` (with or without the leading ``=``)."""
    text = formula if formula.startswith("=") else "=" + formula
    try:
        tokens = [t for t in Tokenizer(text).items if t.type != Token.WSPACE]
    except (TokenizerError, IndexError) as exc:
        raise ParseError(f"cannot tokenize: {exc}") from exc
    parser = _Parser(tokens, books or [])
    expr = parser.expression()
    if parser.pos != len(tokens):
        raise ParseError(f"unexpected token {tokens[parser.pos].value!r}")
    return expr


class _Parser:
    def __init__(self, tokens: list[Token], books: list[str]) -> None:
        self.tokens = tokens
        self.books = books
        self.pos = 0

    def _peek(self) -> Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> Token:
        tok = self._peek()
        if tok is None:
            raise ParseError("unexpected end of formula")
        self.pos += 1
        return tok

    def expression(self, min_prec: int = 0) -> Expr:
        left = self.unary()
        while True:
            tok = self._peek()
            if tok is None or tok.type != Token.OP_IN:
                return left
            prec = _PRECEDENCE.get(tok.value)
            if prec is None:
                raise ParseError(f"unknown operator {tok.value!r}")
            if prec <= min_prec and not (tok.value == "^" and prec == min_prec):
                return left
            self._next()
            right = self.expression(prec)
            left = Binary(tok.value, left, right)

    def unary(self) -> Expr:
        tok = self._peek()
        if tok is not None and tok.type == Token.OP_PRE:
            self._next()
            return Unary(tok.value, self.unary())
        expr = self.primary()
        tok = self._peek()
        while tok is not None and tok.type == Token.OP_POST:
            self._next()
            expr = Unary(tok.value, expr)
            tok = self._peek()
        return expr

    def primary(self) -> Expr:
        tok = self._next()
        if tok.type == Token.OPERAND:
            return self.operand(tok)
        if tok.type == Token.FUNC and tok.subtype == Token.OPEN:
            return self.call(tok.value[:-1])
        if tok.type == Token.PAREN and tok.subtype == Token.OPEN:
            inner = self.expression()
            close = self._next()
            if close.type != Token.PAREN or close.subtype != Token.CLOSE:
                raise ParseError("missing ')'")
            return inner
        raise ParseError(f"unexpected token {tok.value!r}")

    def call(self, name: str) -> Expr:
        name = name.upper()
        if name.startswith("_XLFN."):
            name = name[6:]
        args: list[Expr] = []
        tok = self._peek()
        if tok is not None and tok.type == Token.FUNC and tok.subtype == Token.CLOSE:
            self._next()
            return Call(name, ())
        while True:
            tok = self._peek()
            if tok is not None and tok.type == Token.SEP and tok.subtype == Token.ARG:
                self._next()
                args.append(Empty())
                continue
            if tok is not None and tok.type == Token.FUNC and tok.subtype == Token.CLOSE:
                self._next()
                args.append(Empty())
                return Call(name, tuple(args))
            args.append(self.expression())
            tok = self._next()
            if tok.type == Token.FUNC and tok.subtype == Token.CLOSE:
                return Call(name, tuple(args))
            if not (tok.type == Token.SEP and tok.subtype == Token.ARG):
                raise ParseError(f"unexpected token {tok.value!r} in {name}()")

    def operand(self, tok: Token) -> Expr:
        if tok.subtype == Token.NUMBER:
            return Num(float(tok.value))
        if tok.subtype == Token.TEXT:
            return Str(tok.value[1:-1].replace('""', '"'))
        if tok.subtype == Token.LOGICAL:
            return Bool(tok.value.upper() == "TRUE")
        if tok.subtype == Token.RANGE:
            return self.reference(tok.value)
        if tok.subtype == Token.ERROR:
            raise ParseError(f"error literal {tok.value}")
        raise ParseError(f"unsupported operand {tok.value!r}")

    def reference(self, text: str) -> Ref:
        sheet: str | None = None
        book: str | None = None
        rest = text
        if "!" in text:
            prefix, _, rest = text.rpartition("!")
            prefix = prefix.strip()
            if prefix.startswith("'") and prefix.endswith("'"):
                prefix = prefix[1:-1].replace("''", "'")
            m = _BOOK_RE.match(prefix)
            if m:
                index = int(m.group(1))
                if not 1 <= index <= len(self.books):
                    raise ParseError(f"unknown external workbook [{index}] in {text}")
                book, prefix = self.books[index - 1], m.group(2)
            sheet = prefix
        first, _, second = rest.partition(":")
        c1, r1 = _cell_parts(first)
        if second:
            c2, r2 = _cell_parts(second)
        else:
            c2, r2 = c1, r1
        if c1 is None or c2 is None:
            raise ParseError(f"row-only references are not supported: {text}")
        return Ref(sheet, book, c1, r1, c2, r2, text)


def _cell_parts(part: str) -> tuple[int | None, int | None]:
    part = part.strip()
    m = _CELL_RE.match(part)
    if m:
        return column_index_from_string(m.group(1).upper()) - 1, int(m.group(2))
    m = _COL_RE.match(part)
    if m:
        return column_index_from_string(m.group(1).upper()) - 1, None
    m = _ROW_RE.match(part)
    if m:
        return None, int(m.group(1))
    raise ParseError(f"bad reference part {part!r}")
