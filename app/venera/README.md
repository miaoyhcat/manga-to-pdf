# Venera source runtime (experimental)

Runs [Venera](https://github.com/venera-app/venera) / [VeneraX](https://github.com/Kyosee/VeneraX)
comic sources from Python, so `manga-to-pdf` can reach the community source catalogue instead of
only the three built-in platforms.

**Status: foundation only.** Searching works and is verified against several real sources; the
chapter/page layer and the web UI are not wired up yet — see *Not done yet* below.

## How it works

Venera sources are plain JS classes extending `ComicSource`. The upstream JS library
(`assets/init.js`) talks to its host through a global `sendMessage({method, ...})`.

- The library calls `sendMessage` **synchronously** for DOM, encoding, cookies and storage
  (`querySelectorAll(...).map(...)`, `el.text`, `Convert.md5(...)`).
- Only network calls are asynchronous.

So `host.mjs` implements the synchronous half in JS — DOM via [linkedom](https://github.com/WebReflection/linkedom),
crypto via `node:crypto`, cookies and a JSON key/value store — and forwards HTTP to Python, which
owns the proxy configuration. That keeps a single process boundary and avoids synchronous IPC
(which is unreliable on Windows pipes).

```
Python  ──line-delimited JSON──>  node host.mjs  ──RPC(http)──>  Python (requests + proxy)
          (load source, call)     (DOM/convert/…)
```

## Requirements

- Node.js >= 18 (`node --version`)
- `npm install` inside this directory (pulls `linkedom`, ~5 MB)
- The JS library is **not** vendored here: it is GPL-3.0 and is downloaded on first use into
  `$MANGA_HOME/data/venera/init.js`. Source files come from the catalogue below.

## Source catalogue

Sources are read from [laincat/vensrc](https://github.com/laincat/vensrc), which inspects and
curates Venera sources (main + extended subscriptions, currently 64 entries). Raw GitHub URLs are
rewritten to jsDelivr because `raw.githubusercontent.com` is unreachable on some networks.

## Usage

```bash
export MANGA_HOME=/opt/manga
export MANGA_PROXY=http://127.0.0.1:7890
export MANGA_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt   # optional, for TLS-intercepting proxies

python -m venera.cli list
python -m venera.cli search dm5 "海贼王"
```

From Python:

```python
from venera.runtime import VeneraRuntime
rt = VeneraRuntime(home, proxy=proxy)
rt.load_source("dm5", rt.source_for("dm5"))
res = rt.call("dm5", "search.load", ["海贼王", {}, 1])
# {"comics": [{"id": "...", "title": "...", "cover": "..."}], "maxPage": N}
```

## Verified

| source | key | search |
|---|---|---|
| comick | `comick` | 7 results for *frieren*, 0.3 s |
| 动漫屋 | `dm5` | 22 results for 海贼王, 4.7 s |
| 热辣漫画 | `hot_manga` | 21 results for 海贼王, 1.3 s |
| 51漫画 | `manga51` | 4 results for 海贼王, 3.1 s |

Some sources fail for reasons outside the runtime: 布卡 requires passing the site's JS challenge,
and a few sites block datacenter/client requests.

## Not done yet

1. **Chapter/page layer.** Sources use two different interfaces and both need support:
   - classic: `comic.loadInfo(id)` → `.chapters`, then `comic.loadPages(id, chapterId)`
   - newer: `comic.loadEp(id, ...)` + `comic.onImageLoad(url, comicId, epId)`
   `dm5`, for example, only has `loadInfo` + `loadEp`, so it returns no chapters today.
2. **Web UI:** a source picker, and `platform=venera` in `/api/search`.
3. **Download pipeline:** feed page URLs into the existing `img2pdf` job runner.
4. **Source health:** per-source timeout, disable-list, and a UI badge for broken sources.

## Licence

Everything in this directory except the downloaded `init.js` is MIT, like the rest of the project.
`init.js` is GPL-3.0 (from VeneraX) and is fetched at runtime, not redistributed here. If you
redistribute a bundle that includes it, that bundle is GPL-3.0.
