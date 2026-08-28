from __future__ import annotations

import re
from dataclasses import dataclass

_TERM = re.compile(r"[A-Za-z0-9_]+")


@dataclass(frozen=True, slots=True)
class ContextItem:
    source: str
    key: str
    content: str
    relevance: float = 0.5
    freshness: float = 1.0

    @property
    def estimated_tokens(self) -> int:
        return max(1, (len(self.content) + 3) // 4)


@dataclass(frozen=True, slots=True)
class ContextSelection:
    items: tuple[ContextItem, ...]
    used_tokens: int
    dropped_count: int

    def render(self) -> str:
        return "\n\n".join(
            f"[{item.source}:{item.key}]\n{item.content}" for item in self.items
        )


@dataclass(frozen=True, slots=True)
class InvalidContextBudgetError(ValueError):
    token_budget: int

    def __str__(self) -> str:
        return f"context token budget must be positive, got {self.token_budget}"


class ContextEngine:
    def select(
        self,
        query: str,
        items: tuple[ContextItem, ...],
        token_budget: int,
    ) -> ContextSelection:
        if token_budget <= 0:
            raise InvalidContextBudgetError(token_budget)
        query_terms = _terms(query)
        best_by_key: dict[str, tuple[float, ContextItem]] = {}
        for item in items:
            score = self._score(query_terms, item)
            current = best_by_key.get(item.key)
            if current is None or score > current[0]:
                best_by_key[item.key] = (score, item)

        ranked = sorted(
            best_by_key.values(),
            key=lambda pair: (-pair[0], pair[1].source, pair[1].key),
        )
        selected: list[ContextItem] = []
        used_tokens = 0
        for _score, item in ranked:
            item_tokens = item.estimated_tokens
            if used_tokens + item_tokens > token_budget:
                continue
            selected.append(item)
            used_tokens += item_tokens
        return ContextSelection(
            tuple(selected),
            used_tokens,
            len(items) - len(selected),
        )

    @staticmethod
    def _score(query_terms: frozenset[str], item: ContextItem) -> float:
        content_terms = _terms(f"{item.key} {item.content}")
        lexical = (
            len(query_terms.intersection(content_terms)) / len(query_terms)
            if query_terms
            else 0.0
        )
        relevance = max(0.0, min(1.0, item.relevance))
        freshness = max(0.0, min(1.0, item.freshness))
        return (0.6 * relevance) + (0.25 * lexical) + (0.15 * freshness)


def _terms(value: str) -> frozenset[str]:
    return frozenset(match.group(0).lower() for match in _TERM.finditer(value))
