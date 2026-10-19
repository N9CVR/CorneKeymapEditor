#!/usr/bin/env python3
"""Independent full DTS syntax validation for generated ZMK Corne keymaps.

This is a verification utility, not part of the GUI. It uses a separate Lark
parser so editor parsing and serialization are not self-validating.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from lark import Lark, UnexpectedInput

GRAMMAR = r"""
start: top_statement*
?top_statement: root | node
root: "/" "{" statement* "}" ";"
?statement: node | property
node: node_head "{" statement* "}" ";"
node_head: AMP_NAME | NAME
property: NAME ";" | NAME "=" values ";"
values: value ("," value)*
?value: STRING | cells | bytes | AMP_NAME | CALL | NAME | NUMBER | PATH
cells: "<" cell_value* ">"
?cell_value: AMP_NAME | CALL | NAME | NUMBER | LPAR | RPAR | PLUS | MINUS
bytes: "[" HEXBYTE* "]"
AMP_NAME: /&[A-Za-z_][A-Za-z0-9_-]*/
CALL: /[A-Za-z_][A-Za-z0-9_]*\([^()<>;]*\)/
NAME: /[#A-Za-z_][#A-Za-z0-9_,.+@-]*/
NUMBER: /0[xX][0-9A-Fa-f]+|[0-9]+/
PATH: /\/[A-Za-z0-9_.,+@\/-]+/
HEXBYTE: /[0-9A-Fa-f]{2}/
LPAR: "("
RPAR: ")"
PLUS: "+"
MINUS: "-"
STRING: /"(?:\\.|[^"\\])*"/
%import common.WS
%ignore WS
"""

PARSER = Lark(GRAMMAR, parser="lalr", propagate_positions=True)


def preprocess(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", lambda match: "\n" * match.group(0).count("\n"), text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    lines = []
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("#"):
            lines.append("\n" if line.endswith("\n") else "")
        else:
            lines.append(line)
    return "".join(lines)


def validate(path: Path, shield: str) -> None:
    source = path.read_text(encoding="utf-8")
    prepared = preprocess(source)
    try:
        PARSER.parse(prepared)
    except UnexpectedInput as error:
        context = error.get_context(prepared, span=60)
        raise ValueError(f"{path}:{error.line}:{error.column}: DTS syntax error\n{context}") from error

    # Independent semantic checks corresponding to zmk,keymap's shape.
    match = re.search(r'\bkeymap\s*\{(?P<body>.*)\n\s*\};\s*\n\s*\};\s*$', source, re.S)
    if not match:
        raise ValueError("zmk keymap node not found")
    layer_nodes = re.findall(r'(?m)^\s{8}([a-z_][a-z0-9_]{0,30})\s*\{', match.group("body"))
    if not layer_nodes:
        raise ValueError("no normalized keymap layers found")
    unsafe = re.findall(r'(?m)^\s{8}([^\s{]+)\s*\{', match.group("body"))
    if layer_nodes != unsafe:
        raise ValueError(f"unsafe layer identifier found: {unsafe}")
    if re.search(r'(?m)^\s*label\s*=', match.group("body")):
        raise ValueError("obsolete label property found in keymap layer")

    print(f"PASS {shield}: full DTS syntax parsed; {len(layer_nodes)} normalized layers")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("keymap", type=Path)
    parser.add_argument("--shield", choices=("corne_left", "corne_right"), required=True)
    args = parser.parse_args()
    try:
        validate(args.keymap, args.shield)
    except (OSError, ValueError) as error:
        print(f"FAIL {args.shield}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
