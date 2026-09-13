"""Small, non-blocking code-slimming hints.

The collector deliberately has no lifecycle, policy, waiver, or close semantics.
Callers may display its output, but must never use it to decide Loop status.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class SlimmingAdvice(BaseModel):
    """One informational structural hint."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    path: str
    line: int = 0
    message: str


def collect_slimming_advice(paths: Iterable[Path]) -> list[SlimmingAdvice]:
    """Inspect readable files once and return deterministic advisory hints."""

    advice: list[SlimmingAdvice] = []
    seen: set[Path] = set()
    for supplied_path in paths:
        path = Path(supplied_path)
        key = path.resolve(strict=False)
        if key in seen:
            continue
        seen.add(key)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        lines = text.splitlines()
        if len(lines) > 500:
            advice.append(
                _advice(
                    "file-length",
                    path,
                    1,
                    f"File has {len(lines)} lines; consider a smaller responsibility boundary.",
                )
            )
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for function in functions:
            end_line = function.end_lineno or function.lineno
            length = end_line - function.lineno + 1
            if length > 80:
                advice.append(
                    _advice(
                        "function-length",
                        path,
                        function.lineno,
                        f"Function {function.name!r} has {length} lines; consider extracting one responsibility.",
                    )
                )
        advice.extend(_duplication_advice(path, functions))
        advice.extend(_wrapper_advice(path, tree, functions))
        top_classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        top_functions = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        if (
            len(top_classes) >= 2
            and len(top_functions) >= 2
            and len(top_classes) + len(top_functions) >= 8
        ):
            advice.append(
                _advice(
                    "mixed-responsibility",
                    path,
                    1,
                    "Module mixes several top-level classes and functions; consider separating responsibilities.",
                )
            )
    return sorted(advice, key=lambda item: (item.path, item.line, item.kind))


def _duplication_advice(
    path: Path,
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef],
) -> list[SlimmingAdvice]:
    first_by_body: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    advice: list[SlimmingAdvice] = []
    for function in functions:
        if not function.body:
            continue
        body = ast.dump(
            ast.Module(body=function.body, type_ignores=[]),
            annotate_fields=True,
            include_attributes=False,
        )
        first = first_by_body.setdefault(body, function)
        if first is function:
            continue
        advice.append(
            _advice(
                "same-file-duplication",
                path,
                function.lineno,
                f"Function {function.name!r} duplicates the body of {first.name!r}.",
            )
        )
    return advice


def _wrapper_advice(
    path: Path,
    tree: ast.Module,
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef],
) -> list[SlimmingAdvice]:
    calls = Counter(
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    )
    advice: list[SlimmingAdvice] = []
    for function in functions:
        if calls[function.name] != 1 or len(function.body) != 1:
            continue
        statement = function.body[0]
        value = statement.value if isinstance(statement, (ast.Return, ast.Expr)) else None
        if not isinstance(value, ast.Call):
            continue
        advice.append(
            _advice(
                "single-caller-wrapper",
                path,
                function.lineno,
                f"Function {function.name!r} is a one-call wrapper with one caller.",
            )
        )
    return advice


def _advice(kind: str, path: Path, line: int, message: str) -> SlimmingAdvice:
    return SlimmingAdvice(kind=kind, path=path.as_posix(), line=line, message=message)


__all__ = ["SlimmingAdvice", "collect_slimming_advice"]
