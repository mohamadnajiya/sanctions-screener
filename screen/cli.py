"""`screen` CLI: query tools locally, refresh the bundled lists, validate the data, print the version."""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="screen")
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("query", help='call a tool: screen query screen_name \'{"name": "Ali Kony"}\'')
    q.add_argument("tool")
    q.add_argument("params", nargs="?", default="{}")
    sub.add_parser("version")
    sub.add_parser("validate", help="load every list, build the index and print the health summary")
    sub.add_parser("tools")
    r = sub.add_parser(
        "refresh", help="download the official lists into the data dir and rewrite version.yaml (network!)"
    )
    r.add_argument("--only", nargs="*", help="refresh only these lists")
    r.add_argument("--data-dir", default=None)
    r.add_argument("--keep-raw", default=None)
    ns = ap.parse_args(argv)
    if ns.cmd == "refresh":
        from pathlib import Path

        from .normalize import data_dir
        from .refresh import refresh

        doc = refresh(
            Path(ns.data_dir) if ns.data_dir else data_dir(),
            ns.only or None,
            keep_raw=Path(ns.keep_raw) if ns.keep_raw else None,
        )
        print(json.dumps({"data_version": doc["data_version"], "failed": doc["failed"]}, ensure_ascii=False))
        sys.exit(1 if doc["failed"] else 0)
    from .tools import CATALOGUE

    if ns.cmd == "version":
        print(json.dumps({"code": CATALOGUE.info.version, "data_version": CATALOGUE.data_version()}))
    elif ns.cmd == "tools":
        for t in CATALOGUE.tools:
            print(f"{t.name:16} {t.event or 'free':16} {t.title}")
    elif ns.cmd == "validate":
        from .engine import health_extra

        print(json.dumps(health_extra(), indent=1))
    else:
        if ns.tool not in CATALOGUE.by_name:
            sys.exit(f"unknown tool {ns.tool}")
        env = CATALOGUE.call(ns.tool, json.loads(ns.params))
        print(env.model_dump_json(indent=1))


if __name__ == "__main__":
    main()
