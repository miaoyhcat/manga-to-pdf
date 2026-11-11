"""Small CLI to exercise the Venera source runtime without the web UI.

    python -m venera.cli list
    python -m venera.cli search comick "frieren"
    python -m venera.cli info comick "<comic id>"

The runtime needs Node (>= 18) and `node_modules/linkedom` next to host.mjs
(`npm install` in app/venera).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .runtime import VeneraRuntime

HOME = Path(os.environ.get("MANGA_HOME", "/opt/manga"))
PROXY = os.environ.get("MANGA_PROXY") or None


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    rt = VeneraRuntime(HOME / "data", proxy=PROXY, timeout=90)
    try:
        if cmd == "list":
            idx = rt.index()
            print(f"{len(idx)} sources")
            for s in idx:
                print(f"  {s['name']:16} {s['key']:14} v{s.get('version','')}")
            return 0
        if cmd == "search" and len(argv) >= 4:
            key, q = argv[2], argv[3]
            src = rt.source_for(key)
            info = rt.load_source(key, src)
            print(f"source: {info['name']} v{info['version']}")
            res = rt.call(key, "search.load", [q, {}, 1]) or {}
            comics = res.get("comics") or []
            print(f"{len(comics)} results")
            for c in comics[:20]:
                print(f"  {c.get('id')}  {c.get('title')}")
            return 0
        if cmd == "info" and len(argv) >= 4:
            key, cid = argv[2], argv[3]
            rt.load_source(key, rt.source_for(key))
            info = rt.call(key, "comic.loadInfo", [cid]) or {}
            print(json.dumps(info, ensure_ascii=False, indent=2)[:2000])
            return 0
    finally:
        rt.stop()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
