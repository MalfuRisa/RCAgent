"""One metering and paging rule for every local read (PRD §17.5).

Every returned payload is measured on its final serialization - including the
read receipt id, source info, cursors and markers - so a page that fits cannot
grow past ``max_chars`` afterwards. Pages only advance over items or characters
that were actually returned, which keeps the position truthful even when a
single row is larger than the whole budget.

This module has no workspace knowledge: callers pass a ``build`` closure that
turns candidate content into the exact payload they will return.
"""
import json

PAGE_DEFAULT_LIMIT = 5
PAGE_MAX_LIMIT = 100
PAGE_SERIALIZED_LIMIT = 8000
MIN_PAGE_CHARS = 512


def serialized_chars(value):
    return len(json.dumps(value, ensure_ascii=False, default=str))


def fits(value, budget):
    return serialized_chars(value) <= budget


def fit_list(rows, start, ceiling, budget, build):
    """Add whole rows while the final serialization of ``build(items)`` fits.

    ``ceiling`` is the last position this call may consider (offset + limit).
    Returns ``(items, end, too_small)``; ``too_small`` is True only when rows
    exist but not even the first one fits, so the caller answers with an
    explicit read hint instead of repeating the same page.
    """
    items = []
    for index in range(start, ceiling):
        candidate = items + [rows[index]]
        if serialized_chars(build(candidate)) > budget:
            break
        items = candidate
    if not items and start < ceiling:
        return [], start, True
    return items, start + len(items), False


def fit_text(text, start, budget, build):
    """Largest prefix of ``text[start:]`` whose final serialization fits.

    Size grows monotonically with the prefix, so the boundary is found by
    bisection; the caller only has to make ``build`` reflect the exact payload.
    Returns ``(chunk, end, has_more)``; ``chunk`` is None when not even one
    character fits, so the caller answers with an explicit read hint.
    """
    if start >= len(text):
        return '', start, False
    if serialized_chars(build(text[start:start + 1])) > budget:
        return None, start, True  # not even one character fits this budget
    low, high, best = start + 1, len(text), start + 1
    while low <= high:
        middle = (low + high) // 2
        if serialized_chars(build(text[start:middle])) <= budget:
            best, low = middle, middle + 1
        else:
            high = middle - 1
    return text[start:best], best, best < len(text)


def resolve_max_chars(requested):
    """0 keeps the configured default; explicit values are bounded and validated."""
    from config import settings
    if requested is None or requested == 0:
        value = settings.CONTEXT_READ_MAX_CHARS
    else:
        try:
            value = int(requested)
        except (TypeError, ValueError):
            raise ValueError('max_chars must be an integer between %d and %d' % (MIN_PAGE_CHARS, PAGE_SERIALIZED_LIMIT))
        if value < 0:
            raise ValueError('max_chars must be an integer between %d and %d' % (MIN_PAGE_CHARS, PAGE_SERIALIZED_LIMIT))
    if value < MIN_PAGE_CHARS or value > PAGE_SERIALIZED_LIMIT:
        raise ValueError('max_chars must be between %d and %d' % (MIN_PAGE_CHARS, PAGE_SERIALIZED_LIMIT))
    return value