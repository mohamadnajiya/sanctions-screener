"""Tool handlers: wrap the matcher in the shared Envelope (confidence, sources, data version, warnings, disclaimer).

Nothing here logs the names it screens: only aggregate counters are kept by agentkit.metrics. No network.
"""

from __future__ import annotations

import datetime as dt
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agentkit import Envelope, ToolError

from . import matcher, report
from .matcher import LIST_KEYS, LIST_TITLES, index
from .normalize import compare_steps, normalise, tables

DISCLAIMER = (
    "Screening result, not a legal determination. A hit means a listed name is similar to the name you gave; verify every "
    "positive match against the source list entry (source_url) and your customer's identity documents before acting. "
    "A 'clear' result means no match above your threshold in the bundled list snapshot (see data_version / list_status), "
    "not that the person is unsanctioned everywhere. Follow your own AML/CFT obligations (UAE Federal Decree-Law 10/2025 "
    "and Cabinet Resolution 74/2020 for UAE DNFBPs and FIs, or the law that applies to you), including freezing and "
    "reporting duties on confirmed matches."
)
STALE_DAYS = 7
MAX_BATCH = 500
MAX_AUDIT = 50
WORKERS = int(os.environ.get("SCREEN_WORKERS", "4"))
HEURISTICS = [
    "romanisation-variant folding (data/name_variants.yaml) is a heuristic table, not an authority",
    "cross-script (Arabic ↔ Latin) hits use a consonant-skeleton comparison and are capped at score 90",
]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def _list_meta() -> dict[str, dict[str, Any]]:
    return index().version.get("lists", {})


def _age_days(meta: dict[str, Any]) -> int | None:
    fa = meta.get("fetched_at")
    if not fa:
        return None
    try:
        t = dt.datetime.fromisoformat(str(fa).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, (_now() - t).days)


def _stale_warnings(lists: list[str]) -> list[str]:
    out = []
    meta = _list_meta()
    for k in lists:
        m = meta.get(k)
        if not m:
            out.append(f"list '{k}' is not bundled in this build")
            continue
        age = _age_days(m)
        if age is not None and age > STALE_DAYS:
            out.append(
                f"{LIST_TITLES.get(k, k)} snapshot is {age} days old (fetched {m.get('fetched_at')}); run `screen refresh`"
            )
    return out


def _sources(lists: list[str]) -> list[str]:
    meta = _list_meta()
    out = []
    for k in lists:
        m = meta.get(k)
        if m:
            out.append(m.get("page_url") or m.get("source_url"))
    return out


def health_extra() -> dict[str, Any]:
    ix = index()
    meta = _list_meta()
    return {
        "lists": {
            k: {"entries": ix.list_counts.get(k, {}).get("entries", 0), "age_days": _age_days(meta.get(k, {}))}
            for k in LIST_KEYS
        },
        "entries": len(ix.entries),
        "aliases": len(ix.aliases),
        "variant_groups": tables().n_groups,
        "stale": bool(_stale_warnings(list(LIST_KEYS))),
    }


def _validate_lists(lists: list[str] | None) -> list[str]:
    if not lists:
        return list(LIST_KEYS)
    bad = [x for x in lists if x not in LIST_KEYS]
    if bad:
        raise ToolError(f"unknown list(s) {bad}; choose from {list(LIST_KEYS)}")
    return sorted(set(lists), key=LIST_KEYS.index)


def _envelope(result: dict[str, Any], lists: list[str], *, cross: bool, extra_warnings: list[str] = ()) -> Envelope:
    warnings = _stale_warnings(lists) + list(extra_warnings)
    est: list[str] = []
    conf = "high"
    if cross:
        conf = "medium"
        est.append(HEURISTICS[1])
    if any("days old" in w for w in warnings):
        conf = "medium"
        est.append("list snapshot older than 7 days; new designations since then are not included")
    est.append(HEURISTICS[0])
    return Envelope(
        result=result,
        data_confidence=conf,  # type: ignore[arg-type]
        estimated_items=est,
        sources=_sources(lists),
        data_version=index().data_version,
        warnings=warnings,
        disclaimer=DISCLAIMER,
    )


# ---------------------------------------------------------------- tools


def screen_name(
    name: str,
    dob: str | None,
    nationality: str | None,
    entity_type: str,
    id_number: str | None,
    lists: list[str] | None,
    min_score: float,
) -> Envelope:
    ls = _validate_lists(lists)
    r = matcher.screen(
        name,
        dob=dob,
        nationality=nationality,
        entity_type=entity_type,
        id_number=id_number,
        lists=ls,
        min_score=min_score,
    )
    if not r["normalised"]["tokens"]:
        raise ToolError("name contains no letters after normalisation")
    r["billable_names"] = 1
    return _envelope(r, ls, cross=r["cross_script_used"])


def _row_key(row: dict[str, Any]) -> tuple:
    return (
        normalise(row["name"]).text,
        row.get("dob") or "",
        (row.get("nationality") or "").upper(),
        row.get("entity_type") or "any",
        row.get("id_number") or "",
    )


def screen_batch(
    rows: list[dict[str, Any]], lists: list[str] | None, min_score: float, default_nationality: str | None
) -> Envelope:
    ls = _validate_lists(lists)
    uniq: dict[tuple, dict[str, Any]] = {}
    refs: dict[tuple, list[str | None]] = {}
    for row in rows:
        row = dict(row)
        if not row.get("nationality") and default_nationality:
            row["nationality"] = default_nationality
        k = _row_key(row)
        uniq.setdefault(k, row)
        refs.setdefault(k, []).append(row.get("reference"))
    keys = list(uniq)

    def one(k: tuple) -> dict[str, Any]:
        row = uniq[k]
        r = matcher.screen(
            row["name"],
            dob=row.get("dob"),
            nationality=row.get("nationality"),
            entity_type=row.get("entity_type") or "any",
            id_number=row.get("id_number"),
            lists=ls,
            min_score=min_score,
        )
        hits = r["hits"][:5]
        return {
            "references": [x for x in refs[k] if x] or None,
            "name": row["name"],
            "dob": row.get("dob"),
            "nationality": row.get("nationality"),
            "entity_type": row.get("entity_type") or "any",
            "decision_hint": r["decision_hint"],
            "hit_count": r["hit_count"],
            "top_score": hits[0]["score"] if hits else None,
            "hits": [
                {
                    k2: h[k2]
                    for k2 in (
                        "list",
                        "list_entry_id",
                        "listed_name",
                        "score",
                        "match_strength",
                        "matched_on",
                        "programs",
                        "source_url",
                    )
                }
                for h in hits
            ],
            "cross_script_used": r["cross_script_used"],
        }

    with ThreadPoolExecutor(max_workers=max(1, min(WORKERS, len(keys)))) as ex:
        results = list(ex.map(one, keys))
    summary = {"clear": 0, "review": 0, "likely_match": 0}
    for r in results:
        summary[r["decision_hint"]] += 1
    out = {
        "count": len(results),
        "duplicates_removed": len(rows) - len(results),
        "billable_names": len(results),
        "min_score": min_score,
        "lists_screened": ls,
        "summary": summary,
        "results": results,
    }
    return _envelope(out, ls, cross=any(r["cross_script_used"] for r in results))


def audit_report(
    subjects: list[dict[str, Any]],
    lists: list[str] | None,
    min_score: float,
    prepared_by: str | None,
    case_reference: str | None,
) -> Envelope:
    ls = _validate_lists(lists)
    screened = []
    cross = False
    for s in subjects:
        r = matcher.screen(
            s["name"],
            dob=s.get("dob"),
            nationality=s.get("nationality"),
            entity_type=s.get("entity_type") or "any",
            id_number=s.get("id_number"),
            lists=ls,
            min_score=min_score,
        )
        r["reference"] = s.get("reference")
        cross = cross or r["cross_script_used"]
        screened.append(r)
    meta = _list_meta()
    now = _now()
    html = report.render(
        screened,
        lists={k: meta[k] for k in ls if k in meta},
        data_version=index().data_version,
        generated_at=now,
        prepared_by=prepared_by,
        case_reference=case_reference,
        disclaimer=DISCLAIMER,
        min_score=min_score,
    )
    summary = {"clear": 0, "review": 0, "likely_match": 0}
    for r in screened:
        summary[r["decision_hint"]] += 1
    out = {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "subjects": len(screened),
        "summary": summary,
        "lists": ls,
        "html": html,
        "html_bytes": len(html.encode("utf-8")),
        "note": "Self-contained HTML (no external resources). Open in a browser and print to PDF for the AML file.",
    }
    return _envelope(out, ls, cross=cross)


def list_status() -> Envelope:
    ix = index()
    meta = _list_meta()
    out_lists = []
    for k in LIST_KEYS:
        m = meta.get(k, {})
        c = ix.list_counts.get(k, {})
        age = _age_days(m)
        out_lists.append(
            {
                "list": k,
                "title": LIST_TITLES.get(k, k),
                "authority": m.get("authority"),
                "entries": c.get("entries", 0),
                "individuals": c.get("individual", 0),
                "entities": c.get("entity", 0),
                "vessels": c.get("vessel", 0),
                "aircraft": c.get("aircraft", 0),
                "aliases": c.get("aliases", 0),
                "fetched_at": m.get("fetched_at"),
                "days_since_fetch": age,
                "stale": bool(age is not None and age > STALE_DAYS),
                "source_url": m.get("source_url"),
                "page_url": m.get("page_url"),
                "sha256": m.get("sha256"),
                "bytes": m.get("bytes"),
                "terms": m.get("terms"),
            }
        )
    failed = index().version.get("failed") or {}
    out = {
        "data_version": ix.data_version,
        "generated_at": index().version.get("generated_at"),
        "total_entries": len(ix.entries),
        "total_aliases": len(ix.aliases),
        "stale_threshold_days": STALE_DAYS,
        "lists": out_lists,
        "last_refresh_failures": failed,
        "refresh_command": "screen refresh   (weekly cron on the server; never runs on the request path)",
    }
    return _envelope(
        out,
        list(LIST_KEYS),
        cross=False,
        extra_warnings=[f"last refresh failed for {k}: {v}" for k, v in failed.items()],
    )


def explain_match(name_a: str, name_b: str) -> Envelope:
    a, b = normalise(name_a), normalise(name_b)
    if not a.tokens or not b.tokens:
        raise ToolError("both names must contain letters")
    comp = matcher.compare(a, b)
    out = {
        "a": compare_steps(a),
        "b": compare_steps(b),
        "components": comp,
        "score": comp["score"],
        "match_strength": matcher.strength(
            comp["score"],
            identical=bool(comp.get("identical")),
            weak=False,
            negative=False,
            cross=bool(comp.get("cross_script")),
        ),
        "would_hit_at_default_threshold": comp["score"] >= 85,
        "notes": [
            "score is the name-only component; DOB, nationality, ID and entity type adjust it in screen_name",
            HEURISTICS[1]
            if comp.get("cross_script")
            else "same script: token_sort / token_set / Jaro-Winkler on canonical tokens",
        ],
    }
    return _envelope(out, [], cross=bool(comp.get("cross_script")))
