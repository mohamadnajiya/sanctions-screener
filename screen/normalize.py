"""Deterministic name normalisation for Latin-script and Arabic-script names.

Pipeline (every step is recorded so `explain_match` can show it):
  Latin:  NFKC → casefold → strip diacritics → apostrophes/hyphens/punctuation → tokens → split merged "Abdul…" forms →
          drop honorifics / particles / legal suffixes → fold romanisation variants (data/name_variants.yaml) → canonical tokens
  Arabic: NFKC → remove tashkeel + tatweel → letter folding (أإآ→ا, ة→ه, ى→ي, ؤ→و, ئ→ي, Persian ک/ی/گ/پ/چ) → Arabic-Indic digits →
          punctuation → tokens → strip ال / وال prefixes, split عبد… → drop particles / honorifics → canonical tokens
Both produce a consonant "skeleton" per token (shared alphabet for the two scripts) so a Latin query can be compared with an
Arabic alias and vice versa. The skeleton is lossy by design and is only ever reported as a heuristic.
No network, no ML, no state: pure functions over the bundled YAML tables.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_TASHKEEL = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_APOS = re.compile(r"[‘’ʻʼʾʿ`´'ʼ‘’ʻ]")
_WS = re.compile(r"\s+")
_ABD_LAT = re.compile(r"^abd(?:ul|el|al|ol|ur|us|u|o|e|a)?(.{3,})$")
_ABD_AR = re.compile(r"^عبد(.{2,})$")
_AL_LAT = re.compile(r"^(?:al|el)([bcdfghjklmnpqrstvwxz][a-z]{3,})$")
_AR_LETTER_MAP = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ة": "ه",
        "ى": "ي",
        "ؤ": "و",
        "ئ": "ي",
        "ء": "",
        "ک": "ك",
        "ی": "ي",
        "ے": "ي",
        "ۀ": "ه",
        "ہ": "ه",
        "ھ": "ه",
        "ۃ": "ه",
        "گ": "ج",
        "پ": "ب",
        "چ": "ش",
        "ژ": "ز",
        "ڤ": "ف",
        "ڨ": "ق",
        "٠": "0",
        "١": "1",
        "٢": "2",
        "٣": "3",
        "٤": "4",
        "٥": "5",
        "٦": "6",
        "٧": "7",
        "٨": "8",
        "٩": "9",
        "۰": "0",
        "۱": "1",
        "۲": "2",
        "۳": "3",
        "۴": "4",
        "۵": "5",
        "۶": "6",
        "۷": "7",
        "۸": "8",
        "۹": "9",
    }
)
# consonant skeleton: shared alphabet for Latin and Arabic (see module docstring)
_SKEL_LAT_DIGRAPHS = (
    ("kh", "x"),
    ("sh", "S"),
    ("ch", "S"),
    ("th", "t"),
    ("dh", "d"),
    ("ph", "f"),
    ("gh", "j"),
    ("ck", "k"),
    ("ss", "s"),
)
_SKEL_LAT = str.maketrans(
    {
        "q": "k",
        "c": "k",
        "z": "s",
        "v": "f",
        "p": "b",
        "g": "j",
        "a": "",
        "e": "",
        "i": "",
        "o": "",
        "u": "",
        "y": "",
        "w": "",
        "'": "",
    }
)
_SKEL_AR = str.maketrans(
    {
        "ا": "",
        "ب": "b",
        "ت": "t",
        "ث": "t",
        "ج": "j",
        "ح": "h",
        "خ": "x",
        "د": "d",
        "ذ": "d",
        "ر": "r",
        "ز": "s",
        "س": "s",
        "ش": "S",
        "ص": "s",
        "ض": "d",
        "ط": "t",
        "ظ": "s",
        "ع": "",
        "غ": "j",
        "ف": "f",
        "ق": "k",
        "ك": "k",
        "ل": "l",
        "م": "m",
        "ن": "n",
        "ه": "h",
        "و": "",
        "ي": "",
        "ء": "",
    }
)
_DOUBLES = re.compile(r"(.)\1+")


def data_dir() -> Path:
    env = os.environ.get("SCREEN_DATA_DIR")
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parents[1] / "data"
    return repo if repo.exists() else Path(__file__).resolve().parent / "_data"


class Tables:
    def __init__(self) -> None:
        d = yaml.safe_load((data_dir() / "name_variants.yaml").read_text(encoding="utf-8"))
        self.version: str = str(d.get("version", ""))
        self.canon: dict[str, str] = {}
        self.group_of: dict[str, tuple[str, ...]] = {}
        for g in d["groups"]:
            g = [str(x).casefold() for x in g]
            for t in g:
                self.canon.setdefault(t, g[0])
                self.group_of.setdefault(t, tuple(g))
        self.honorifics = frozenset(str(x).casefold() for x in d["honorifics"])
        self.particles = frozenset(str(x).casefold() for x in d["particles"])
        self.legal = frozenset(str(x).casefold().strip(".") for x in d["legal_suffixes"])
        self.n_groups = len(d["groups"])


@lru_cache(maxsize=1)
def tables() -> Tables:
    return Tables()


@dataclass(frozen=True)
class Normalised:
    original: str
    script: str  # "latin" | "arabic" | "mixed"
    tokens: tuple[str, ...]  # canonical tokens, original order
    skeleton: tuple[str, ...]  # consonant skeleton per token (same order)
    steps: tuple[str, ...] = field(default=(), compare=False)
    variants_applied: tuple[str, ...] = field(default=(), compare=False)

    @property
    def text(self) -> str:
        return " ".join(self.tokens)

    @property
    def sorted_text(self) -> str:
        return " ".join(sorted(self.tokens))

    @property
    def skeleton_text(self) -> str:
        return " ".join(self.skeleton)

    @property
    def skeleton_compact(self) -> str:
        return "".join(sorted(self.skeleton))


def is_arabic(s: str) -> bool:
    return bool(ARABIC_RE.search(s))


def script_of(s: str) -> str:
    ar = bool(ARABIC_RE.search(s))
    lat = bool(re.search(r"[A-Za-zÀ-ɏ]", s))
    return "mixed" if ar and lat else ("arabic" if ar else "latin")


def strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def skeleton_latin(tok: str) -> str:
    s = tok
    for a, b in _SKEL_LAT_DIGRAPHS:
        s = s.replace(a, b)
    s = s.translate(_SKEL_LAT)
    s = _DOUBLES.sub(r"\1", s)
    if len(s) > 1 and s.endswith("h"):
        s = s[:-1]
    return s or tok[:1]


def skeleton_arabic(tok: str) -> str:
    s = tok.translate(_SKEL_AR)
    s = _DOUBLES.sub(r"\1", s)
    if len(s) > 1 and s.endswith("h"):
        s = s[:-1]
    return s or tok[:1]


def _skeleton(tok: str) -> str:
    return skeleton_arabic(tok) if is_arabic(tok) else skeleton_latin(tok)


def _latin_tokens(text: str, steps: list[str]) -> list[str]:
    s = unicodedata.normalize("NFKC", text).casefold()
    s2 = strip_diacritics(s)
    if s2 != s:
        steps.append("stripped diacritics")
    s = _APOS.sub("", s2)
    s = s.replace("-", " ").replace("_", " ").replace("/", " ")
    s = _PUNCT.sub(" ", s)
    toks = [t for t in _WS.split(s) if t]
    return toks


def _arabic_tokens(text: str, steps: list[str]) -> list[str]:
    s = unicodedata.normalize("NFKC", text)
    s2 = _TASHKEEL.sub("", s)
    if s2 != s:
        steps.append("removed tashkeel/tatweel")
    s = s2.translate(_AR_LETTER_MAP)
    if s != s2:
        steps.append("folded Arabic letters (أإآ→ا, ة→ه, ى→ي, ؤ→و, ئ→ي, Persian/Urdu forms)")
    s = _APOS.sub("", s)
    s = s.replace("-", " ").replace("_", " ").replace("/", " ").replace("،", " ").replace("؛", " ")
    s = _PUNCT.sub(" ", s).casefold()
    return [t for t in _WS.split(s) if t]


def normalise(text: str, *, explain: bool = True) -> Normalised:
    """Normalise a personal or entity name. Deterministic; safe on any string (max useful length ~300 chars).
    explain=False skips the step log (used when indexing 100k aliases)."""
    T = tables()
    steps: list[str] = ["NFKC + casefold"]
    original = text
    text = text.strip()[:400]
    script = script_of(text)
    raw: list[str] = []
    if script == "arabic":
        raw = _arabic_tokens(text, steps)
    elif script == "latin":
        raw = _latin_tokens(text, steps)
    else:  # mixed: handle each run with its own rules
        for piece in re.split(r"(" + ARABIC_RE.pattern + r"+(?:\s+" + ARABIC_RE.pattern + r"+)*)", text):
            if not piece.strip():
                continue
            raw += _arabic_tokens(piece, steps) if is_arabic(piece) else _latin_tokens(piece, steps)
        steps.append("mixed scripts: normalised each run separately")

    # 1. split merged particle forms
    toks: list[str] = []
    for t in raw:
        if is_arabic(t):
            if t.startswith("وال") and len(t) > 5:
                t = t[3:]
                steps.append("stripped وال prefix")
            if t.startswith("ال") and len(t) > 4 and t not in T.particles:
                t = t[2:]
                steps.append("stripped ال prefix")
            m = _ABD_AR.match(t)
            if m and t != "عبدالله":
                rest = m.group(1)
                rest = rest[2:] if rest.startswith("ال") and len(rest) > 3 else rest
                toks += ["عبد", rest]
                steps.append(f"split {t} → عبد {rest}")
                continue
        else:
            m = _ABD_LAT.match(t)
            if m and t not in T.canon:
                toks += ["abd", m.group(1)]
                steps.append(f"split merged '{t}' → abd {m.group(1)}")
                continue
            m = _AL_LAT.match(t)
            if m and t not in T.canon and t not in T.honorifics:
                steps.append(f"stripped merged '{t[:2]}' prefix → {m.group(1)}")
                t = m.group(1)
        toks.append(t)

    # 2. merge عبد الله / abd allah
    merged: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if t in ("abd",) and nxt in ("allah", "alah", "ullah"):
            merged.append("abdullah")
            steps.append("merged 'abd allah' → abdullah")
            i += 2
            continue
        if t == "عبد" and nxt in ("الله", "لله"):
            merged.append("عبدالله")
            steps.append("merged عبد الله → عبدالله")
            i += 2
            continue
        merged.append(t)
        i += 1
    toks = merged

    # 3. drop honorifics, particles, legal suffixes (only when something remains)
    kept: list[str] = []
    dropped: list[str] = []
    for t in toks:
        key = t.strip(".")
        if key in T.honorifics or key in T.particles or key in T.legal:
            dropped.append(t)
        else:
            kept.append(t)
    if dropped and kept:
        steps.append("dropped " + ", ".join(dropped))
        toks = kept
    elif dropped and not kept:
        steps.append("kept particles (nothing else left)")

    # 4. romanisation variants → canonical
    out: list[str] = []
    variants: list[str] = []
    for t in toks:
        c = T.canon.get(t)
        if c and c != t:
            variants.append(f"{t}→{c}")
            out.append(c)
        else:
            out.append(t)
    if variants:
        steps.append("romanisation variants: " + ", ".join(variants))
    skel = tuple(_skeleton(t) for t in out)
    return Normalised(original, script, tuple(out), skel, tuple(steps) if explain else (), tuple(variants))


def canonical_id(s: str) -> str:
    """ID numbers: keep alphanumerics only, uppercase (passport 'P 0458-1926' == 'P04581926')."""
    return re.sub(r"[^A-Z0-9]", "", unicodedata.normalize("NFKC", s).upper().translate(_AR_LETTER_MAP))


def compare_steps(n: Normalised) -> dict[str, object]:
    """Explain one normalisation (for explain_match and screen_name.normalised)."""
    return {
        "input": n.original,
        "script": n.script,
        "canonical": n.text,
        "tokens": list(n.tokens),
        "skeleton": n.skeleton_text,
        "steps": list(n.steps),
        "variants_applied": list(n.variants_applied),
    }
