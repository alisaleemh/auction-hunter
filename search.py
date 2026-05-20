from __future__ import annotations

import re
from difflib import SequenceMatcher
from datetime import datetime, timezone
from typing import Iterable, List


PUNCT_RE = re.compile(r"[^a-z0-9]+")
TERM_EXPANSIONS = {
    "carseat": ["car", "seat"],
    "baby": ["infant", "child", "kid"],
    "stroller": ["pram", "pushchair"],
    "gate": ["barrier"],
}


def normalize_text(value: str | None) -> str:
    value = (value or "").lower()
    value = PUNCT_RE.sub(" ", value)
    return " ".join(value.split())


def query_tokens(query: str) -> list[str]:
    normalized = normalize_text(query)
    return normalized.split() if normalized else []


def expanded_query_tokens(query: str) -> list[list[str]]:
    groups: list[list[str]] = []
    for token in query_tokens(query):
        expansions = TERM_EXPANSIONS.get(token, [])
        groups.append([token, *expansions])
    return groups


def build_searchable_text(result: dict) -> str:
    parts = [
        result.get("lot_title"),
        result.get("condition"),
        result.get("description"),
        result.get("details"),
    ]
    return normalize_text(" ".join(part for part in parts if part))


def _ngrams(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text] if text else []
    return [text[i : i + size] for i in range(0, len(text) - size + 1)]


def _token_similarity(token: str, candidates: list[str]) -> float:
    if not candidates:
        return 0.0
    best = 0.0
    for candidate in candidates:
        if token == candidate:
            return 1.0
        if token in candidate or candidate in token:
            best = max(best, 0.92)
            continue
        best = max(best, SequenceMatcher(None, token, candidate).ratio())
    return best


def _token_group_similarity(token_group: list[str], candidates: list[str]) -> float:
    return max(_token_similarity(token, candidates) for token in token_group)


def _phrase_similarity(query: str, text: str) -> float:
    if not query or not text:
        return 0.0
    if query in text:
        return 1.0
    windows = _ngrams(text, max(len(query), 1))
    if not windows:
        return 0.0
    return max(SequenceMatcher(None, query, window).ratio() for window in windows)


def relevance_score(result: dict, query: str) -> float:
    normalized_query = normalize_text(query)
    if not normalized_query:
        return 0.0

    title = normalize_text(result.get("lot_title"))
    condition = normalize_text(result.get("condition"))
    description = normalize_text(result.get("description"))
    details = normalize_text(result.get("details"))
    searchable = build_searchable_text(result)

    query_parts = expanded_query_tokens(query)
    searchable_parts = searchable.split()
    title_parts = title.split()

    token_scores = [_token_group_similarity(token_group, searchable_parts) for token_group in query_parts]
    avg_token_score = sum(token_scores) / len(token_scores) if token_scores else 0.0
    min_token_score = min(token_scores) if token_scores else 0.0

    title_token_scores = [_token_group_similarity(token_group, title_parts) for token_group in query_parts]
    avg_title_token_score = sum(title_token_scores) / len(title_token_scores) if title_token_scores else 0.0
    title_phrase_score = _phrase_similarity(normalized_query, title)
    item_phrase_score = _phrase_similarity(normalized_query, searchable)

    contextual_bonus = 0.0
    base_query_tokens = query_tokens(query)
    if base_query_tokens and any(token in description for token in base_query_tokens):
        contextual_bonus += 0.04
    if base_query_tokens and any(token in condition for token in base_query_tokens):
        contextual_bonus += 0.02
    if base_query_tokens and any(token in details for token in base_query_tokens):
        contextual_bonus += 0.02

    return (
        avg_token_score * 0.45
        + min_token_score * 0.15
        + avg_title_token_score * 0.15
        + title_phrase_score * 0.15
        + item_phrase_score * 0.10
        + contextual_bonus
    )


def matches_query(result: dict, tokens: list[str], query: str) -> bool:
    if not tokens:
        return True
    score = relevance_score(result, query)
    return score >= 0.62 or (len(tokens) == 1 and score >= 0.55)


def match_rank(result: dict, normalized_query: str, tokens: list[str]) -> tuple[int, float]:
    title = normalize_text(result.get("lot_title"))
    score = relevance_score(result, normalized_query)
    if normalized_query and normalized_query in title:
        return (0, -score)
    if tokens and all(token in title for token in tokens):
        return (1, -score)
    if score >= 0.8:
        return (2, -score)
    return (3, -score)


def _end_time_key(value: str | None) -> tuple[int, datetime]:
    if not value:
        return (1, datetime.max.replace(tzinfo=timezone.utc))
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return (1, datetime.max.replace(tzinfo=timezone.utc))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (0, parsed.astimezone(timezone.utc))


def filter_and_sort_results(results: Iterable[dict], query: str) -> List[dict]:
    normalized_query = normalize_text(query)
    tokens = query_tokens(query)
    filtered = [result for result in results if matches_query(result, tokens, normalized_query)]
    filtered.sort(
        key=lambda result: (
            match_rank(result, normalized_query, tokens),
            _end_time_key(result.get("end_time")),
            normalize_text(result.get("source")),
            normalize_text(result.get("lot_title")),
        )
    )
    return filtered
