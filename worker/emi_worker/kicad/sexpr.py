"""A tolerant s-expression reader for KiCad board files.

KiCad's ``.kicad_pcb`` is one big s-expression. The format is stable in shape but not in
detail -- keys come and go between major versions, and the same value is quoted in one
release and bare in the next -- so this reader is deliberately permissive: it builds a
generic tree and lets the extraction layer ask for what it needs and shrug at what it does
not recognise. A strict schema here would break on every KiCad release.

Files run to tens of megabytes, so the tokeniser is a single compiled regex rather than a
character loop; the difference is roughly two orders of magnitude.
"""

from __future__ import annotations

import re
from typing import Iterator, Union

Atom = Union[str, float]
Node = list  # [symbol, *children]; children are Atom or Node

#: One token: an open paren, a close paren, a double-quoted string (with escapes), or a
#: bare atom running up to whitespace or a paren.
_TOKEN = re.compile(
    r"""
      (?P<open>\()
    | (?P<close>\))
    | "(?P<string>(?:[^"\\]|\\.)*)"
    | (?P<atom>[^\s()"]+)
    """,
    re.VERBOSE,
)

_ESCAPES = re.compile(r"\\(.)")

#: Bare tokens that look like numbers. KiCad writes plain decimals and the occasional
#: exponent; anything else stays a string.
_NUMBER = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


class SexprError(ValueError):
    """The file is not a well-formed s-expression."""


def _tokens(text: str) -> Iterator[tuple[str, str]]:
    pos = 0
    end = len(text)
    while pos < end:
        ch = text[pos]
        if ch in " \t\r\n":
            pos += 1
            continue
        m = _TOKEN.match(text, pos)
        if m is None:
            raise SexprError(f"unexpected character {text[pos]!r} at offset {pos}")
        pos = m.end()
        kind = m.lastgroup
        yield kind, m.group(kind)  # type: ignore[arg-type]


def parse(text: str) -> Node:
    """Parse one top-level s-expression.

    Returns a nested list whose first element is the node's symbol, e.g.
    ``["kicad_pcb", ["version", 20240108.0], ...]``.
    """
    stack: list[Node] = []
    root: Node | None = None

    for kind, value in _tokens(text):
        if kind == "open":
            node: Node = []
            if stack:
                stack[-1].append(node)
            stack.append(node)
        elif kind == "close":
            if not stack:
                raise SexprError("unbalanced closing parenthesis")
            node = stack.pop()
            if not stack:
                if root is not None:
                    raise SexprError("more than one top-level expression")
                root = node
        elif kind == "string":
            if not stack:
                raise SexprError("string outside any expression")
            # Quoted values stay strings even when they look numeric, which is what keeps
            # a net literally named "5" from becoming the number 5.
            stack[-1].append(_ESCAPES.sub(r"\1", value))
        else:  # atom
            if not stack:
                raise SexprError("atom outside any expression")
            stack[-1].append(float(value) if _NUMBER.match(value) else value)

    if stack:
        raise SexprError("unbalanced opening parenthesis")
    if root is None:
        raise SexprError("file contains no s-expression")
    return root


# ---- navigation helpers -------------------------------------------------------------
#
# Written as free functions rather than a wrapper class so the tree stays plain lists:
# a 40 MB board becomes millions of nodes, and one Python object per node is memory we
# do not need to spend.


def sym(node) -> str:
    """The node's leading symbol, or "" if it has none."""
    if isinstance(node, list) and node and isinstance(node[0], str):
        return node[0]
    return ""


def children(node: Node, name: str) -> Iterator[Node]:
    """Every direct child list with the given symbol."""
    for c in node:
        if isinstance(c, list) and sym(c) == name:
            yield c


def child(node: Node, name: str) -> Node | None:
    """The first direct child list with the given symbol."""
    for c in children(node, name):
        return c
    return None


def values(node: Node, name: str) -> list[Atom]:
    """The atoms of the first child with the given symbol, e.g. ``(at 1 2 90)`` -> [1,2,90]."""
    c = child(node, name)
    if c is None:
        return []
    return [v for v in c[1:] if not isinstance(v, list)]


def value(node: Node, name: str, index: int = 0, default=None):
    """One atom from a named child."""
    vs = values(node, name)
    if index < len(vs):
        return vs[index]
    return default


def number(node: Node, name: str, index: int = 0, default: float = 0.0) -> float:
    v = value(node, name, index, None)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return default
    return default


def text(node: Node, name: str, index: int = 0, default: str = "") -> str:
    v = value(node, name, index, None)
    if v is None:
        return default
    if isinstance(v, float):
        # Reverse the tokeniser's number coercion for values that are conceptually names.
        return str(int(v)) if v.is_integer() else str(v)
    return str(v)


def flag(node: Node, name: str) -> bool:
    """Whether a bare marker child is present, e.g. ``(locked)`` or ``(hide)``.

    KiCad 8 also writes these as ``(locked yes)``, so a present-but-"no" value counts as
    absent.
    """
    c = child(node, name)
    if c is None:
        return name in node  # bare symbol form, e.g. a lone `locked`
    vs = [v for v in c[1:] if not isinstance(v, list)]
    if not vs:
        return True
    return str(vs[0]).lower() not in ("no", "false", "0")


def points(node: Node) -> list[tuple[float, float]]:
    """Read a ``(pts (xy x y) ...)`` child into a coordinate list.

    KiCad 7+ can also emit ``(arc (start ...) (mid ...) (end ...))`` inside ``pts`` for
    curved zone outlines. Those are flattened to their endpoints here: a chord instead of
    an arc slightly under-fills a curve, which for meshing is the safe direction to err.
    """
    pts_node = child(node, "pts")
    if pts_node is None:
        return []
    out: list[tuple[float, float]] = []
    for c in pts_node[1:]:
        if not isinstance(c, list):
            continue
        kind = sym(c)
        if kind == "xy" and len(c) >= 3:
            out.append((float(c[1]), float(c[2])))
        elif kind == "arc":
            for part in ("start", "mid", "end"):
                vs = values(c, part)
                if len(vs) >= 2:
                    out.append((float(vs[0]), float(vs[1])))
    return out
