"""Console entry points (see pyproject [project.scripts])."""

from __future__ import annotations

from typing import Any


def mcp_main() -> None:
    from agentkit.mcp_server import run_cli

    from .tools import CATALOGUE

    run_cli(CATALOGUE, default_port=8785)


def api_main() -> None:
    from agentkit.rest import run_cli

    from .tools import CATALOGUE

    run_cli(CATALOGUE, default_port=8786)


async def _batch(actor: Any, inp: dict[str, Any]) -> None:
    """Ordinary Apify run: input {names: [...]} or {text}, optional nationality / entity_type / min_score / lists
    → one dataset row per unique name with decision_hint, hit_count, top_score and the top hits; charged per name."""
    import re

    from . import engine
    from .tools import NAME_SCREENED

    names: list[str] = [str(n).strip() for n in (inp.get("names") or []) if n and str(n).strip()]
    if inp.get("text"):
        names += [ln.strip() for ln in re.split(r"[\r\n;]+", str(inp["text"])) if ln.strip()]
    if not names:
        await actor.fail(status_message="No names given. Provide `names` (list) or `text` (one name per line).")
        return
    min_score = float(inp.get("min_score", 85) or 85)
    nat = (inp.get("nationality") or None) or None
    typ = inp.get("entity_type") or "any"
    lists = inp.get("lists") or None
    total = 0
    summary = {"clear": 0, "review": 0, "likely_match": 0}
    for i in range(0, len(names), engine.MAX_BATCH):
        chunk = [{"name": n, "entity_type": typ} for n in names[i : i + engine.MAX_BATCH]]
        env = engine.screen_batch(chunk, lists, min_score, nat)
        rows = []
        for r in env.result["results"]:
            row = dict(r)
            row["data_version"] = env.data_version
            row["disclaimer"] = env.disclaimer
            rows.append(row)
        await actor.push_data(rows)
        n = env.result["billable_names"]
        total += n
        for k in summary:
            summary[k] += env.result["summary"][k]
        if n:
            await actor.charge(event_name=NAME_SCREENED, count=n)
        actor.log.info("screened %d name(s) (running total %d)", n, total)
    await actor.set_value("SUMMARY", {"names_screened": total, **summary})
    await actor.set_status_message(
        f"{total} name(s) screened: {summary['likely_match']} likely match, {summary['review']} to review, {summary['clear']} clear."
    )


def apify_main() -> None:
    from agentkit.apify import run_actor

    from .tools import CATALOGUE

    run_actor(CATALOGUE, _batch)
