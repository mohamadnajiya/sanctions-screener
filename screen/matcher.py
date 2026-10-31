"""Deterministic sanctions-list matcher: bundled JSONL lists → in-memory index → scored, explainable hits.

Retrieval (so a query never scans 100k aliases fuzzily):
  * inverted index canonical token → alias ids, weighted by IDF
  * inverted index consonant-skeleton token → alias ids (catches spelling noise and cross-script hits)
  * the top CANDIDATES by accumulated weight are scored with rapidfuzz; everything else is ignored
Scoring (0–100), same script:
  base = max(token_sort_ratio, subset score) blended 80/20 with Jaro-Winkler on the token-sorted strings;
  subset score = token_set_ratio − 6 per extra list-side token (−10 more for one-word queries), capped at 90.
Cross-script (Arabic ↔ Latin): skeleton similarity, capped at 90, flagged as heuristic.
Adjustments on the best alias per entry: DOB (+5 exact / +3 year / +1 ±1 year / −15 mismatch when the entry has a DOB),
nationality (+3 / −5), entity-type disagreement −20, weak alias −3, ID number exact match → 100 "exact".
match_strength: exact (identical normalised name, strong alias, no negative adjustment) · strong ≥ 92 · possible ≥ 80 · weak.
"""

from __future__ import annotations

import json
import math
import re
from array import array
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import yaml
from rapidfuzz import fuzz, process
from rapidfuzz.distance import JaroWinkler

from .normalize import Normalised, canonical_id, data_dir, normalise

LIST_KEYS = ("ofac_sdn", "ofac_cons", "un", "eu", "uk", "uae")
LIST_TITLES = {
    "ofac_sdn": "OFAC SDN List (US Treasury)",
    "ofac_cons": "OFAC Consolidated non-SDN List (US Treasury)",
    "un": "UN Security Council Consolidated List",
    "eu": "EU Consolidated Financial Sanctions List",
    "uk": "UK Sanctions List (FCDO)",
    "uae": "UAE Local Terrorist List (Cabinet / EOCN)",
}
CANDIDATES = 1500
MAX_HITS = 20
STRONG, POSSIBLE = 92, 80
_YEAR = re.compile(r"\d{4}")


@dataclass(slots=True)
class Entry:
    id: str
    list: str
    type: str
    name: str
    aliases: tuple[tuple[str, int], ...]  # (text, weak)
    dobs: tuple[str, ...]
    nats: tuple[str, ...]
    ids: tuple[tuple[str, str], ...]
    programs: tuple[str, ...]
    listed: str | None
    url: str
    ref: str | None
    years: frozenset[int]
    id_keys: frozenset[str]


@dataclass(slots=True)
class Alias:
    entry: int  # index into Index.entries
    norm: Normalised  # norm.original is the display text as listed
    weak: bool
    is_primary: bool

    @property
    def text(self) -> str:
        return self.norm.original


def _years(dobs: tuple[str, ...]) -> frozenset[int]:
    ys: set[int] = set()
    for d in dobs:
        m = _YEAR.findall(d)
        if "/" in d and len(m) >= 2:
            a, b = int(m[0]), int(m[1])
            if 0 <= b - a <= 15:
                ys.update(range(a, b + 1))
            else:
                ys.update((a, b))
        elif m:
            ys.add(int(m[0]))
    return frozenset(ys)


class Index:
    """All bundled lists loaded and indexed. Build once per process (see `index()`)."""

    def __init__(self) -> None:
        d = data_dir()
        self.version: dict[str, Any] = yaml.safe_load((d / "version.yaml").read_text(encoding="utf-8"))
        self.data_version: str = self.version["data_version"]
        self.entries: list[Entry] = []
        self.aliases: list[Alias] = []
        self.by_id: dict[str, int] = {}
        self.list_counts: dict[str, dict[str, int]] = {}
        tok_post: dict[str, list[int]] = defaultdict(list)
        skel_post: dict[str, list[int]] = defaultdict(list)
        for key in LIST_KEYS:
            path = d / "lists" / f"{key}.jsonl"
            if not path.exists():
                continue
            counts: dict[str, int] = defaultdict(int)
            with path.open(encoding="utf-8") as f:
                for line in f:
                    r = json.loads(line)
                    eidx = len(self.entries)
                    aliases = tuple((a[0], int(a[1])) for a in r.get("aliases", []))
                    ids = tuple((i[0], i[1]) for i in r.get("ids", []))
                    e = Entry(
                        r["id"],
                        key,
                        r.get("type", "unknown"),
                        r["name"],
                        aliases,
                        tuple(r.get("dobs", [])),
                        tuple(r.get("nats", [])),
                        ids,
                        tuple(r.get("programs", [])),
                        r.get("listed"),
                        r.get("url", ""),
                        r.get("ref"),
                        _years(tuple(r.get("dobs", []))),
                        frozenset(canonical_id(i[1]) for i in ids if canonical_id(i[1])),
                    )
                    self.entries.append(e)
                    self.by_id[e.id] = eidx
                    counts["entries"] += 1
                    counts[e.type] += 1
                    self._add_alias(eidx, r["name"], False, True, tok_post, skel_post)
                    for text, weak in aliases:
                        self._add_alias(eidx, text, bool(weak), False, tok_post, skel_post)
            counts["aliases"] = sum(1 for a in self.aliases if self.entries[a.entry].list == key)
            self.list_counts[key] = dict(counts)
        n = len(self.aliases)
        self.tok_index: dict[str, array] = {t: array("I", sorted(set(v))) for t, v in tok_post.items()}
        self.skel_index: dict[str, array] = {t: array("I", sorted(set(v))) for t, v in skel_post.items()}
        self.idf: dict[str, float] = {t: math.log(1 + n / len(v)) for t, v in self.tok_index.items()}
        self.skel_idf: dict[str, float] = {t: math.log(1 + n / len(v)) for t, v in self.skel_index.items()}
        self.texts: list[str] = [a.norm.text for a in self.aliases]
        self.skel_texts: list[str] = [a.norm.skeleton_text for a in self.aliases]

    def _add_alias(self, eidx: int, text: str, weak: bool, primary: bool, tok_post, skel_post) -> None:
        norm = normalise(text, explain=False)
        if not norm.tokens:
            return
        aidx = len(self.aliases)
        self.aliases.append(Alias(eidx, norm, weak, primary))
        for t in set(norm.tokens):
            tok_post[t].append(aidx)
        for s in set(norm.skeleton):
            if len(s) >= 2:
                skel_post[s].append(aidx)

    # ------------------------------------------------------------ retrieval

    def candidates(self, q: Normalised, lists: frozenset[str] | None, limit: int = CANDIDATES) -> list[int]:
        acc: dict[int, float] = defaultdict(float)
        for t in set(q.tokens):
            post = self.tok_index.get(t)
            if post is not None:
                w = self.idf[t]
                for a in post:
                    acc[a] += w
        for s in set(q.skeleton):
            post = self.skel_index.get(s)
            if post is not None and len(s) >= 2:
                w = 0.6 * self.skel_idf[s]
                for a in post:
                    acc[a] += w
        if lists is not None:
            ents = self.entries
            als = self.aliases
            acc = {a: w for a, w in acc.items() if ents[als[a].entry].list in lists}
        if len(acc) <= limit:
            return list(acc)
        return [a for a, _ in sorted(acc.items(), key=lambda kv: -kv[1])[:limit]]


@lru_cache(maxsize=1)
def index() -> Index:
    return Index()


# ---------------------------------------------------------------- scoring


def compare(q: Normalised, a: Normalised) -> dict[str, Any]:
    """Score two normalised names. Returns components + `score` (0–100) + `cross_script` flag + explanations."""
    same_script = q.script == a.script or "mixed" in (q.script, a.script)
    out: dict[str, Any] = {"cross_script": not same_script}
    if same_script and q.tokens and a.tokens:
        ts = fuzz.token_sort_ratio(q.text, a.text)
        tset = fuzz.token_set_ratio(q.text, a.text)
        jw = JaroWinkler.similarity(q.sorted_text, a.sorted_text) * 100
        extra = max(0, len(a.tokens) - len(q.tokens))
        subset = min(90.0, tset - 6 * extra - (10 if len(q.tokens) == 1 and extra else 0)) if extra else 0.0
        base = max(ts, subset)
        score = 100.0 if base >= 100 else 0.8 * base + 0.2 * jw
        out.update(
            token_sort_ratio=round(ts, 1),
            token_set_ratio=round(tset, 1),
            jaro_winkler=round(jw, 1),
            subset_score=round(subset, 1),
            identical=q.tokens == a.tokens,
            reordered=q.tokens != a.tokens and sorted(q.tokens) == sorted(a.tokens),
            subset=subset > ts,
        )
    else:
        k1 = fuzz.token_sort_ratio(q.skeleton_text, a.skeleton_text)
        k2 = fuzz.ratio("".join(q.skeleton), "".join(a.skeleton))
        common = len(set(q.skeleton) & set(a.skeleton))
        coverage = 100.0 * common / max(1, max(len(set(q.skeleton)), len(set(a.skeleton))))
        score = min(90.0, 0.5 * max(k1, k2) + 0.5 * coverage)
        out.update(
            skeleton_token_sort=round(k1, 1),
            skeleton_ratio=round(k2, 1),
            skeleton_tokens_shared=common,
            skeleton_coverage=round(coverage, 1),
        )
    out["score"] = round(max(0.0, min(100.0, score)), 1)
    return out


def _dob_adjust(entry: Entry, dob: str | None) -> tuple[float, str | None]:
    if not dob or not entry.years:
        return 0.0, None if not dob else "dob: entry has no date of birth"
    qy = int(dob[:4])
    if len(dob) >= 10 and dob in entry.dobs:
        return 5.0, f"dob exact match {dob}"
    if qy in entry.years:
        return 3.0, f"dob year match {qy}"
    if any(abs(qy - y) == 1 for y in entry.years):
        return 1.0, f"dob year within ±1 ({qy} vs {sorted(entry.years)})"
    return -15.0, f"dob mismatch ({qy} vs listed {', '.join(entry.dobs[:4])})"


def _nat_adjust(entry: Entry, nat: str | None) -> tuple[float, str | None]:
    if not nat or not entry.nats:
        return 0.0, None if not nat else "nationality: entry has none listed"
    if nat.upper() in entry.nats:
        return 3.0, f"nationality match {nat.upper()}"
    return -5.0, f"nationality mismatch ({nat.upper()} vs {', '.join(entry.nats[:4])})"


def _type_adjust(entry: Entry, typ: str) -> tuple[float, str | None]:
    if typ == "any" or entry.type == "unknown":
        return 0.0, None
    if typ == entry.type:
        return 0.0, f"type match ({typ})"
    return -20.0, f"type mismatch (asked {typ}, listed {entry.type})"


def strength(score: float, *, identical: bool, weak: bool, negative: bool, cross: bool) -> str:
    if identical and not weak and not negative and not cross and score >= 97:
        return "exact"
    if score >= STRONG:
        return "strong"
    if score >= POSSIBLE:
        return "possible"
    return "weak"


def screen(
    name: str,
    *,
    dob: str | None = None,
    nationality: str | None = None,
    entity_type: str = "any",
    id_number: str | None = None,
    lists: list[str] | None = None,
    min_score: float = 85.0,
    max_hits: int = MAX_HITS,
) -> dict[str, Any]:
    """Screen one name. Returns {query, normalised, hits[], decision_hint, ...}. Pure function of the loaded index."""
    ix = index()
    q = normalise(name)
    lset = frozenset(lists) if lists else None
    hits_by_entry: dict[int, dict[str, Any]] = {}
    cross_used = False
    if q.tokens:
        cands = ix.candidates(q, lset)
        same: list[int] = []
        cross: list[int] = []
        for a in cands:
            al = ix.aliases[a]
            (same if (al.norm.script == q.script or "mixed" in (q.script, al.norm.script)) else cross).append(a)
        scored: list[tuple[int, float]] = []
        if same:
            texts = {a: ix.texts[a] for a in same}
            floor = max(0.0, min_score - 25)
            for _t, sc, a in process.extract(
                q.text, texts, scorer=fuzz.token_sort_ratio, limit=None, score_cutoff=floor
            ):
                scored.append((a, sc))
            seen = {a for a, _ in scored}
            for _t, sc, a in process.extract(
                q.text, texts, scorer=fuzz.token_set_ratio, limit=None, score_cutoff=max(floor, 70)
            ):
                if a not in seen:
                    scored.append((a, sc))
                    seen.add(a)
        for a, _pre in scored:
            al = ix.aliases[a]
            comp = compare(q, al.norm)
            if comp["score"] < min_score - 25:
                continue
            cur = hits_by_entry.get(al.entry)
            if cur is None or comp["score"] > cur["_score"]:
                hits_by_entry[al.entry] = {"_score": comp["score"], "_alias": a, "_comp": comp}
        if cross:
            texts = {a: ix.skel_texts[a] for a in cross}
            for _t, _sc, a in process.extract(
                q.skeleton_text, texts, scorer=fuzz.token_sort_ratio, limit=None, score_cutoff=max(60.0, min_score - 15)
            ):
                al = ix.aliases[a]
                comp = compare(q, al.norm)
                cur = hits_by_entry.get(al.entry)
                if comp["score"] >= min_score - 15 and (cur is None or comp["score"] > cur["_score"]):
                    hits_by_entry[al.entry] = {"_score": comp["score"], "_alias": a, "_comp": comp}

    # ID exact match can surface entries the name never reached
    qid = canonical_id(id_number) if id_number else ""
    if qid and len(qid) >= 4:
        for eidx, e in enumerate(ix.entries):
            if qid in e.id_keys and (lset is None or e.list in lset) and eidx not in hits_by_entry:
                comp = {"score": 0.0, "cross_script": False, "identical": False}
                hits_by_entry[eidx] = {"_score": 0.0, "_alias": None, "_comp": comp}

    hits: list[dict[str, Any]] = []
    for eidx, h in hits_by_entry.items():
        e = ix.entries[eidx]
        comp = h["_comp"]
        score = float(h["_score"])
        matched_on: list[str] = []
        al = ix.aliases[h["_alias"]] if h["_alias"] is not None else None
        if al is not None:
            if comp.get("cross_script"):
                matched_on.append(
                    f"cross-script consonant skeleton (heuristic): '{q.skeleton_text}' vs '{al.norm.skeleton_text}'"
                )
                cross_used = True
            elif comp.get("identical"):
                matched_on.append(
                    "identical after normalisation" + (" (primary name)" if al.is_primary else " (alias)")
                )
            elif comp.get("reordered"):
                matched_on.append("same name parts in a different order")
            elif comp.get("subset"):
                matched_on.append("query name parts all present in a longer listed name")
            else:
                matched_on.append(
                    f"fuzzy similarity (token_sort {comp.get('token_sort_ratio')}, jaro-winkler {comp.get('jaro_winkler')})"
                )
            if al.norm.variants_applied or q.variants_applied:
                matched_on.append(
                    "romanisation variants folded: "
                    + ", ".join(sorted(set(al.norm.variants_applied + q.variants_applied)))[:200]
                )
            if al.weak:
                matched_on.append("matched a low-quality / weak alias")
                score -= 3
        negative = False
        for fn, arg in ((_dob_adjust, dob), (_nat_adjust, nationality)):
            adj, why = fn(e, arg)
            if why:
                matched_on.append(why)
            if adj < 0:
                negative = True
            score += adj
        adj, why = _type_adjust(e, entity_type)
        if why:
            matched_on.append(why)
        if adj < 0:
            negative = True
        score += adj
        id_hit = bool(qid) and qid in e.id_keys
        if id_hit:
            matched_on.append(f"ID number exact match ({qid[:3]}…)" if len(qid) > 6 else "ID number exact match")
            score = 100.0
        score = round(max(0.0, min(100.0, score)), 1)
        if score < min_score and not id_hit:
            continue
        st = (
            "exact"
            if id_hit
            else strength(
                score,
                identical=bool(comp.get("identical")),
                weak=bool(al and al.weak),
                negative=negative,
                cross=bool(comp.get("cross_script")),
            )
        )
        hits.append(
            {
                "list": e.list,
                "list_title": LIST_TITLES.get(e.list, e.list),
                "list_entry_id": e.id.split(":", 1)[1],
                "listed_name": e.name,
                "entity_type": e.type,
                "aliases_matched": [al.text] if al is not None and not al.is_primary else [],
                "score": score,
                "match_strength": st,
                "matched_on": matched_on,
                "programs": list(e.programs),
                "listed_on": e.listed,
                "dobs": list(e.dobs),
                "nationalities": list(e.nats),
                "id_numbers": [{"type": t, "number": n} for t, n in e.ids[:10]],
                "reference": e.ref,
                "source_url": e.url,
                "all_aliases": [t for t, _w in e.aliases[:15]],
            }
        )
    order = {"exact": 0, "strong": 1, "possible": 2, "weak": 3}
    hits.sort(key=lambda h: (-h["score"], order[h["match_strength"]], h["list"], h["listed_name"]))
    hits = hits[:max_hits]
    if any(h["match_strength"] in ("exact", "strong") for h in hits):
        hint = "likely_match"
    elif hits:
        hint = "review"
    else:
        hint = "clear"
    return {
        "query": {
            "name": name,
            "dob": dob,
            "nationality": nationality,
            "entity_type": entity_type,
            "id_number": bool(id_number),
        },
        "normalised": {"text": q.text, "script": q.script, "tokens": list(q.tokens), "steps": list(q.steps)},
        "lists_screened": sorted(lset) if lset else list(LIST_KEYS),
        "min_score": min_score,
        "hit_count": len(hits),
        "hits": hits,
        "decision_hint": hint,
        "cross_script_used": cross_used,
    }
