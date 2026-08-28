from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    kind: str
    path: str
    line: int


@dataclass(frozen=True, slots=True)
class Reference:
    name: str
    path: str
    line: int


class PythonCodeIndex:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self._files: dict[
            str, tuple[int, int, tuple[Symbol, ...], tuple[Reference, ...]]
        ] = {}

    def refresh(self) -> None:
        current = {str(path) for path in self.root.rglob("*.py")}
        for stale in set(self._files) - current:
            del self._files[stale]
        for filename in current:
            path = Path(filename)
            stat = path.stat()
            stamp = stat.st_mtime_ns
            size = stat.st_size
            cached = self._files.get(filename)
            if cached and cached[:2] == (stamp, size):
                continue
            symbols, references = self._parse(path)
            self._files[filename] = (stamp, size, symbols, references)

    def symbols(self, name: Optional[str] = None) -> tuple[Symbol, ...]:
        self.refresh()
        values = tuple(
            item for _, _, symbols, _ in self._files.values() for item in symbols
        )
        return tuple(item for item in values if name is None or item.name == name)

    def references(self, name: str) -> tuple[Reference, ...]:
        self.refresh()
        values = tuple(item for _, _, _, refs in self._files.values() for item in refs)
        return tuple(item for item in values if item.name == name)

    def imports(self) -> tuple[tuple[str, str], ...]:
        self.refresh()
        result: list[tuple[str, str]] = []
        for filename in self._files:
            path = Path(filename)
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    result.extend(
                        (str(path.relative_to(self.root)), alias.name)
                        for alias in node.names
                    )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    result.append((str(path.relative_to(self.root)), node.module))
        return tuple(result)

    @staticmethod
    def _parse(path: Path) -> tuple[tuple[Symbol, ...], tuple[Reference, ...]]:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError):
            return (), ()
        symbols: list[Symbol] = []
        references: list[Reference] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.append(
                    Symbol(node.name, type(node).__name__, str(path), node.lineno)
                )
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                references.append(Reference(node.id, str(path), node.lineno))
        return tuple(symbols), tuple(references)
