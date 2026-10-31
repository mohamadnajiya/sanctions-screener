"""Self-contained HTML audit report (no external resources, prints cleanly to PDF). Pure function of its inputs."""

from __future__ import annotations

import datetime as dt
import html
from typing import Any

from .matcher import LIST_TITLES

_CSS = """
:root{--ink:#1b1f24;--muted:#5b6470;--line:#d9dee5;--ok:#1b7f3b;--warn:#b26a00;--bad:#b3261e;--bg:#f6f8fa}
*{box-sizing:border-box}body{font:13px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--ink);margin:0;padding:28px 32px;max-width:1100px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:26px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.meta{color:var(--muted);font-size:12px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;margin:10px 0}
.card{border:1px solid var(--line);border-radius:6px;padding:8px 10px;background:var(--bg)}.card b{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
table{border-collapse:collapse;width:100%;margin:6px 0 12px}th,td{border:1px solid var(--line);padding:5px 7px;text-align:left;vertical-align:top;font-size:12px}th{background:var(--bg)}
.badge{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:600;color:#fff}.clear{background:var(--ok)}.review{background:var(--warn)}.likely_match{background:var(--bad)}
.subject{border:1px solid var(--line);border-radius:6px;padding:10px 12px;margin:10px 0;page-break-inside:avoid}.subject h3{margin:0 0 6px;font-size:14px}
.small{font-size:11px;color:var(--muted)}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;word-break:break-all}
.disc{border-left:3px solid var(--warn);padding:6px 10px;background:#fff8e6;margin-top:18px;font-size:12px}
@media print{body{padding:0}.subject{break-inside:avoid}}
"""


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def render(
    screened: list[dict[str, Any]],
    *,
    lists: dict[str, dict[str, Any]],
    data_version: str,
    generated_at: dt.datetime,
    prepared_by: str | None,
    case_reference: str | None,
    disclaimer: str,
    min_score: float,
) -> str:
    counts = {"clear": 0, "review": 0, "likely_match": 0}
    for r in screened:
        counts[r["decision_hint"]] += 1
    parts: list[str] = []
    parts.append(
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Sanctions screening report {_e(generated_at.date())}</title>"
        f"<style>{_CSS}</style></head><body>"
    )
    parts.append("<h1>Sanctions &amp; terror-list screening report</h1>")
    parts.append(
        f"<div class='meta'>Generated {_e(generated_at.isoformat().replace('+00:00', 'Z'))} · data_version {_e(data_version)} · "
        f"threshold min_score {_e(min_score)} · {len(screened)} subject(s)"
        + (f" · case reference <b>{_e(case_reference)}</b>" if case_reference else "")
        + (f" · prepared by {_e(prepared_by)}" if prepared_by else "")
        + "</div>"
    )
    parts.append("<div class='grid'>")
    for k, label in (("likely_match", "Likely matches"), ("review", "To review"), ("clear", "Clear")):
        parts.append(f"<div class='card'><b>{label}</b><span style='font-size:22px'>{counts[k]}</span></div>")
    parts.append("</div>")

    parts.append(
        "<h2>Lists screened</h2><table><tr><th>List</th><th>Authority</th><th>Entries</th><th>Fetched (UTC)</th><th>Source</th><th>SHA-256</th></tr>"
    )
    for k, m in lists.items():
        c = (m.get("counts") or {}).get("entries", "")
        parts.append(
            f"<tr><td>{_e(LIST_TITLES.get(k, k))}</td><td>{_e(m.get('authority'))}</td><td>{_e(c)}</td><td>{_e(m.get('fetched_at'))}</td>"
            f"<td class='mono'>{_e(m.get('source_url'))}</td><td class='mono'>{_e((m.get('sha256') or '')[:16])}…</td></tr>"
        )
    parts.append("</table>")

    parts.append("<h2>Subjects</h2>")
    for i, r in enumerate(screened, 1):
        q = r["query"]
        hint = r["decision_hint"]
        parts.append(
            f"<div class='subject'><h3>{i}. {_e(q['name'])} <span class='badge {hint}'>{_e(hint.replace('_', ' '))}</span></h3>"
        )
        details = [
            f"DOB: {_e(q['dob'])}" if q.get("dob") else None,
            f"Nationality: {_e(q['nationality'])}" if q.get("nationality") else None,
            f"Type: {_e(q['entity_type'])}" if q.get("entity_type") and q["entity_type"] != "any" else None,
            "ID number supplied" if q.get("id_number") else None,
            f"Reference: {_e(r['reference'])}" if r.get("reference") else None,
        ]
        parts.append("<div class='small'>" + " · ".join(d for d in details if d) + "</div>")
        parts.append(
            f"<div class='small'>Normalised query: <span class='mono'>{_e(r['normalised']['text'])}</span> ({_e(r['normalised']['script'])})</div>"
        )
        if not r["hits"]:
            parts.append(
                f"<p><b>No match.</b> The name (with the identifiers above) produced no hit with score ≥ {_e(min_score)} on "
                f"{', '.join(_e(LIST_TITLES.get(k, k)) for k in r['lists_screened'])} as bundled in data_version {_e(data_version)}.</p>"
            )
        else:
            parts.append(
                "<table><tr><th>#</th><th>Score</th><th>Strength</th><th>List</th><th>Listed name</th><th>Type</th><th>Matched on</th><th>Programs / regime</th><th>Listed DOB · nationality</th><th>Source</th></tr>"
            )
            for j, h in enumerate(r["hits"], 1):
                parts.append(
                    f"<tr><td>{j}</td><td>{_e(h['score'])}</td><td>{_e(h['match_strength'])}</td><td>{_e(h['list_title'])}<br><span class='small'>{_e(h['list_entry_id'])}"
                    + (f" · {_e(h['reference'])}" if h.get("reference") else "")
                    + f"</span></td><td>{_e(h['listed_name'])}"
                    + (
                        f"<br><span class='small'>alias: {_e('; '.join(h['aliases_matched']))}</span>"
                        if h.get("aliases_matched")
                        else ""
                    )
                    + f"</td><td>{_e(h['entity_type'])}</td><td class='small'>{'<br>'.join(_e(x) for x in h['matched_on'])}</td>"
                    f"<td class='small'>{_e(', '.join(h['programs'][:4]))}</td><td class='small'>{_e(', '.join(h['dobs'][:3]))}"
                    + (" · " if h["dobs"] and h["nationalities"] else "")
                    + f"{_e(', '.join(h['nationalities'][:3]))}</td><td class='mono'>{_e(h['source_url'])}</td></tr>"
                )
            parts.append("</table>")
        parts.append("</div>")

    parts.append(f"<div class='disc'><b>Disclaimer.</b> {_e(disclaimer)}</div>")
    parts.append(
        "<p class='small'>Matching: deterministic normalisation (Unicode NFKC, Arabic letter folding, honorific/particle removal, "
        "romanisation-variant folding) then token-sort / token-set / Jaro-Winkler similarity on canonical tokens; cross-script hits use "
        "a consonant-skeleton heuristic capped at 90. Produced by the Noah's Arch GCC Sanctions &amp; Terror-List Screener; no names are logged.</p>"
    )
    parts.append("</body></html>")
    return "".join(parts)
