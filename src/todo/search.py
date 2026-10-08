"""Fuzzy matching utility for search within Filemeta items and tasks."""

import difflib
import re
from typing import Callable, List, Optional, Sequence, Tuple, TypeVar

T = TypeVar("T")


def fuzzy_score(query: str, target: str) -> Optional[float]:
    """
    Calculate a fuzzy match score between a query and a target string.
    Returns None if no match. Higher score indicates a better match.
    Features:
    - Case-insensitive
    - Space/token tolerant (e.g. 'head psy' matching 'Psychology of Headlines')
    - Substring matches score highest, with prefix and word-boundary bonuses
    - Subsequence and partial-word tolerance
    """
    q = query.lower().strip()
    t = target.lower().strip()

    if not q:
        return 1.0

    # 1. Exact string match
    if q == t:
        return 2000.0 - len(t)

    # 2. Exact substring match
    pos = t.find(q)
    if pos != -1:
        score = 1000.0 - pos * 2.0 - len(t)
        # Bonus for start of string or word boundary
        if pos == 0 or (pos > 0 and t[pos - 1] in " _-./()[]"):
            score += 200.0
        return score

    # 3. Multi-token match (e.g. 'head psy' matching 'Psychology of Headlines')
    tokens = q.split()
    target_words = [w for w in re.split(r"[\s_\-./()\[\]]+", t) if w]

    if len(tokens) > 1:
        total_token_score = 0.0
        for tok in tokens:
            tok_pos = t.find(tok)
            if tok_pos != -1:
                bonus = 100.0
                if any(w.startswith(tok) for w in target_words):
                    bonus += 50.0
                total_token_score += bonus - tok_pos
            else:
                best_ratio = max(
                    (difflib.SequenceMatcher(None, tok, w).ratio() for w in target_words),
                    default=0.0,
                )
                if best_ratio >= 0.65:
                    total_token_score += 50.0 * best_ratio
                else:
                    return None
        return 500.0 + total_token_score - len(t)

    # 4. Single token: check prefix of any word in target
    if any(w.startswith(q) for w in target_words):
        return 800.0 - len(t)

    # 5. Subsequence match (characters appear in order)
    t_idx = 0
    consecutive = 0
    subseq_score = 100.0
    all_chars_found = True
    for ch in q:
        found = False
        while t_idx < len(t):
            if t[t_idx] == ch:
                found = True
                if t_idx == 0 or t[t_idx - 1] in " _-./()[]":
                    subseq_score += 20.0
                if consecutive > 0:
                    subseq_score += 10.0 * consecutive
                consecutive += 1
                t_idx += 1
                break
            else:
                consecutive = 0
                t_idx += 1
        if not found:
            all_chars_found = False
            break

    if all_chars_found:
        return subseq_score - len(t)

    # 6. Fallback to difflib SequenceMatcher similarity for small typos/variations
    ratio = difflib.SequenceMatcher(None, q, t).ratio()
    if ratio >= 0.55:
        return ratio * 80.0 - len(t)

    return None


def fuzzy_search(
    items: Sequence[T],
    query: str,
    key: Callable[[T], str] = lambda x: getattr(x, "title", str(x)),
) -> List[T]:
    """
    Filter and rank items using fuzzy matching against key(item).
    If query is empty, returns items in their original order.
    Preserves original objects and ranks best matches first.
    """
    if not query or not query.strip():
        return list(items)

    scored: List[Tuple[float, int, T]] = []
    for idx, item in enumerate(items):
        text = key(item)
        score = fuzzy_score(query, text)
        if score is not None:
            scored.append((score, -idx, item))

    # Sort descending by score, and ascending by original order (using -idx)
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [item for _, _, item in scored]
