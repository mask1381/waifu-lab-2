from __future__ import annotations

import re

JP_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
GENERIC = {
    "unknown", "illust", "illustration", "original", "girl", "girls", "character", "characters",
    "anime", "manga", "art", "artwork", "fanart", "general", "waifu", "woman", "lady", "person",
    "solo", "female", "boy", "boys", "model", "cute", "beautiful",
}
MAX_LEN = 25
WORD_OK = re.compile(r"^[A-Za-z][A-Za-z'’.\-]*$")
CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
CUT = re.compile(r"\s*(?::|\?|!|\s[-–—]\s|\()")


def _tidy(s) -> str:
    """'MamakoOsuki?' -> 'Mamako Osuki'; underscores, (..), ?! removed."""
    t = re.sub(r"\([^)]*\)", " ", str(s or "").replace("_", " "))
    t = re.sub(r"[?!]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -.,:;~")
    if t and " " not in t:
        parts = CAMEL.split(t)
        if 2 <= len(parts) <= 3 and all(len(p) >= 3 for p in parts):
            t = " ".join(parts)  # 'JoJo' stays as is (parts shorter than 3)
    return t


def _valid(t: str) -> bool:
    if not t or len(t) > MAX_LEN or JP_RE.search(t):
        return False
    words = t.split()
    if not 1 <= len(words) <= 3 or not all(WORD_OK.match(w) for w in words):
        return False
    letters = re.sub(r"[^a-z]", "", t.lower())
    return len(letters) >= 3 and letters not in GENERIC


def person_name(s) -> str | None:
    """A character name that is safe to print inside a sentence, else None."""
    t = _tidy(s)
    return t if _valid(t) else None


def franchise_short(s) -> str | None:
    """First part of a long franchise name ('Re:Zero Kara ...' -> 'Re Zero'), else None."""
    raw = str(s or "").strip()
    if not raw:
        return None
    m = CUT.search(raw)
    head, tail = (raw[: m.start()], raw[m.end():]) if m else (raw, "")
    if len(re.sub(r"[^A-Za-z0-9]", "", head)) < 4 and tail.strip():
        head = f"{head} {_tidy(tail).split(' ')[0]}"
    t = _tidy(head)
    return t if _valid(t) else None


def _key(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def relation(cover: dict | None, other: dict | None) -> tuple[str, str | None]:
    """How two posts relate: ('char', name) | ('franchise', name) | ('none', None)."""
    cover, other = cover or {}, other or {}
    mine = {}
    for c in cover.get("characters") or []:
        n = person_name(c)
        if n:
            mine[_key(n)] = n
    for c in other.get("characters") or []:
        n = person_name(c)
        if n and _key(n) in mine:
            return "char", mine[_key(n)]
    fa, fb = cover.get("franchise"), other.get("franchise")
    if fa and fb and _key(fa) == _key(fb):
        name = franchise_short(fa)
        if name:
            return "franchise", name
    return "none", None