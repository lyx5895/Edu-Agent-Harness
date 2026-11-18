"""Text helpers: sentence spans and verbatim-quote grounding (migrated from harness.core.evidence)."""

import hashlib
import json
import re
import unicodedata


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def sha(value) -> str:
    return hashlib.sha256((value if isinstance(value, str) else dump(value)).encode()).hexdigest()


def sentences(text: str) -> list[str]:
    """Exact, non-empty spans of text split at line breaks and sentence ends; decimals stay intact."""
    spans = re.findall(r'[^\n。！？；!?]+(?:[。！？；!?]+|(?<=\S)\.(?=\s|$))?', text or '')
    out = []
    for span in spans:
        out.extend(p.strip() for p in re.split(r'(?<=[^\d\s]\.)\s+|(?<=\d\.)\s+(?=[A-Z])', span))
    return [s[:300] for s in out if s][:300]


def _loose(text: str):
    # NFKC folds full-width forms; whitespace and quote-mark variants are ignored. Keeps an index map back to text.
    chars, index = [], []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize('NFKC', ch).lower():
            if c.isspace() or c in '"\'“”‘’「」『』`':
                continue
            chars.append(c)
            index.append(i)
    return ''.join(chars), index


def loose_match(quote: str, text: str) -> str | None:
    q, _ = _loose(quote)
    t, index = _loose(text)
    at = t.find(q) if len(q) >= 4 else -1
    return text[index[at] : index[at + len(q) - 1] + 1] if at >= 0 else None


def verbatim_quote(quote: str, text: str) -> str | None:
    """Return an exact substring of ``text`` that ``quote`` refers to, or None if it cannot be found.

    Exact match first, then a loose match (full/half width, whitespace, quote marks), then the longest
    verbatim prefix when a long quote's tail was garbled.
    """
    if not quote or not text:
        return None
    if quote in text:
        return quote
    loose = loose_match(quote, text)
    if loose:
        return loose
    trimmed = quote.rstrip().rstrip('.…').rstrip()
    low, high = 0, len(trimmed)
    while low < high:
        mid = (low + high + 1) // 2
        if trimmed[:mid] in text:
            low = mid
        else:
            high = mid - 1
    return trimmed[:low] if low >= max(12, int(0.8 * len(trimmed))) else None


def normalize_answer(text: str) -> str:
    """Canonical form for short-answer comparison: NFKC, lower case, no whitespace or trailing punctuation."""
    text = unicodedata.normalize('NFKC', text or '').strip().lower()
    text = re.sub(r'\s+', '', text)
    return text.strip('。.，,；;：:!！?？')
