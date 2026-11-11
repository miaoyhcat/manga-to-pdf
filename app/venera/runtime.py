"""Host side of the Venera source runtime.

Venera sources are JavaScript classes (the same ones the VeneraX app runs). This module starts a
small Node process (``host.mjs``), loads the upstream JS library plus a source file into it, and
answers the RPC calls that library makes back to its host: DOM queries, HTTP, encoding/crypto,
cookies and key/value storage.

The JS library itself is *not* vendored here: it is GPL-3.0 and downloaded on demand into the
data directory, so this repository stays MIT while the runtime keeps upstream's licence.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from lxml import etree
    from lxml.cssselect import CSSSelector
    HAVE_LXML = True
except Exception:  # pragma: no cover
    HAVE_LXML = False

import requests

INIT_JS_URL = "https://cdn.jsdelivr.net/gh/Kyosee/VeneraX@master/assets/init.js"
INDEX_URL = "https://github.com/laincat/vensrc/releases/latest/download/index.json"
EXTENDED_URL = "https://github.com/laincat/vensrc/releases/latest/download/extended.json"
# optional CA bundle for proxy setups that re-sign TLS
CA_BUNDLE = os.environ.get("MANGA_CA_BUNDLE") or None
NODE = os.environ.get("VENERA_NODE", "node")


def _b64(b: bytes) -> Dict[str, str]:
    return {"__b64": base64.b64encode(b).decode()}


def _unb64(v: Any) -> bytes:
    if isinstance(v, dict) and "__b64" in v:
        return base64.b64decode(v["__b64"])
    if isinstance(v, str):
        return v.encode()
    return b""


class VeneraRuntime:
    """One Node process holding the JS library and any number of loaded sources."""

    def __init__(self, home: Path, proxy: Optional[str] = None, timeout: float = 60.0):
        self.home = Path(home)
        self.cache = self.home / "venera"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.proxy = proxy
        self.timeout = timeout
        self.proc: Optional[subprocess.Popen] = None
        self._cid = 0
        self._lock = threading.Lock()
        self._loaded: Dict[str, Dict[str, Any]] = {}
        self._lib_loaded = False
        self._docs: Dict[int, Dict[str, Any]] = {}
        self._cookies = requests.cookies.RequestsCookieJar()
        self._store_path = self.cache / "storage.json"
        try:
            self._store = json.loads(self._store_path.read_text("utf-8"))
        except Exception:
            self._store = {}
        self.session = requests.Session()

    # ---------------------------------------------------------------- process
    def _ensure(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        host = Path(__file__).with_name("host.mjs")
        if not host.exists():
            raise RuntimeError("host.mjs is missing")
        env = dict(os.environ)
        self.proc = subprocess.Popen(
            [NODE, str(host), str(self.cache)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, env=env,
        )
        line = self.proc.stdout.readline()
        if '"ready"' not in line:
            raise RuntimeError(f"node host failed to start: {line[:200]}")
        # a fresh process has lost everything the old one had
        self._lib_loaded = False
        self._loaded.clear()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write('{"cmd":"shutdown"}\n')
                self.proc.stdin.flush()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()
        self.proc = None

    @property
    def init_js(self) -> Path:
        p = self.cache / "init.js"
        if not p.exists() or p.stat().st_size < 10000:
            r = self._get(INIT_JS_URL, timeout=60)
            r.raise_for_status()
            p.write_bytes(r.content)
        return p

    def _get(self, url: str, timeout: int = 60) -> requests.Response:
        return requests.get(url, proxies=self._proxies(), timeout=timeout,
                            verify=CA_BUNDLE or True)

    def index(self) -> List[Dict[str, Any]]:
        """Source catalogue: main + extended subscriptions from the vensrc project."""
        p = self.cache / "index.json"
        if not p.exists():
            main = []
            for url in (INDEX_URL, EXTENDED_URL):
                try:
                    r = self._get(url, timeout=60)
                    r.raise_for_status()
                    main.extend(r.json())
                except Exception as e:
                    print(f"[venera] index {url}: {e}")
            p.write_text(json.dumps(main, ensure_ascii=False), "utf-8")
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            return []

    def source_for(self, key: str) -> Path:
        """Download (once) the JS file for a catalogue key."""
        for s in self.index():
            if s.get("key") == key:
                return self.source_file(key, s["url"])
        raise KeyError(f"unknown source: {key}")

    def _proxies(self):
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def load_library(self) -> None:
        self._ensure()
        if self._lib_loaded:
            return
        self._request({"cmd": "load_init", "path": str(self.init_js)})
        self._lib_loaded = True

    def load_source(self, sid: str, js_path: Path) -> Dict[str, Any]:
        self.load_library()
        if sid in self._loaded:
            return self._loaded[sid]["info"]
        info = self._request({"cmd": "load_source", "sid": sid, "path": str(js_path)})
        self._loaded[sid] = {"info": info, "path": str(js_path)}
        return info

    def call(self, sid: str, path: str, args: Optional[List[Any]] = None) -> Any:
        self._ensure()
        return self._request({"cmd": "call", "sid": sid, "path": path, "args": args or []})

    # ---------------------------------------------------------------- request loop
    def _request(self, msg: Dict[str, Any]) -> Any:
        with self._lock:
            self._cid += 1
            msg["cid"] = self._cid
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
            deadline = time.time() + self.timeout
            while True:
                if time.time() > deadline:
                    raise TimeoutError("venera call timed out")
                line = self.proc.stdout.readline()
                if not line:
                    err = self.proc.stderr.read()[:400] if self.proc.stderr else ""
                    raise RuntimeError(f"node host exited: {err}")
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("t") == "rpc":
                    value = self._handle_rpc(ev.get("payload") or {})
                    if os.environ.get("VENERA_DEBUG"):
                        print(f"[rpc->] {type(value).__name__} {str(value)[:120]}",
                              file=__import__("sys").stderr)
                    self.proc.stdin.write(json.dumps(
                        {"cmd": "rpc_result", "rid": ev["rid"], "value": value},
                        ensure_ascii=False) + "\n")
                    self.proc.stdin.flush()
                    continue
                if ev.get("t") == "ok" and ev.get("cid") == msg.get("cid"):
                    return ev.get("value")
                if ev.get("t") == "err" and ev.get("cid") == msg.get("cid"):
                    raise RuntimeError(ev.get("error") or "unknown error")

    # ---------------------------------------------------------------- RPC dispatch
    def _handle_rpc(self, p: Dict[str, Any]) -> Any:
        """Only network (and a few odds and ends) reach Python; DOM/convert/storage live in JS."""
        method = p.get("method")
        try:
            if method == "http":
                return self._http(p)
            if method == "random":
                import random
                if p.get("type") == "double":
                    return random.uniform(p.get("min", 0), p.get("max", 1))
                return random.randint(int(p.get("min", 0)), int(p.get("max", 100)))
            if method == "delay":
                time.sleep(min(5.0, float(p.get("time", 0)) / 1000.0))
                return None
            return None
        except Exception as e:
            print(f"[venera] rpc {method} failed: {e}")
            return {"error": f"{type(e).__name__}: {str(e)[:200]}"}

    # ---------------------------------------------------------------- html
    def _doc(self, key: int) -> Dict[str, Any]:
        d = self._docs.get(key)
        if d is None:
            d = {"root": None, "ids": {}, "next": 1}
            self._docs[key] = d
        return d

    def _reg(self, key: int, el) -> Optional[int]:
        d = self._doc(key)
        if el is None:
            return None
        i = d["next"]
        d["next"] += 1
        d["ids"][i] = el
        return i

    def _el(self, doc: int, key: int):
        return self._doc(doc)["ids"].get(key)

    def _html(self, p: Dict[str, Any]) -> Any:
        fn = p.get("function")
        key = int(p.get("key") or 0)
        if fn == "parse":
            if not HAVE_LXML:
                return None
            data = p.get("data") or ""
            root = etree.HTML(data)
            self._docs[key] = {"root": root, "ids": {}, "next": 1}
            return None
        if fn == "dispose":
            self._docs.pop(key, None)
            return None
        if fn in ("querySelector", "getElementById"):
            root = self._doc(key)["root"]
            if root is None:
                return None
            if fn == "getElementById":
                target = p.get("id") or ""
                hits = root.xpath(f'//*[@id="{target}"]') if target else []
                found = hits[0] if hits else None
            else:
                found = self._css(root, p.get("query") or "", single=True)
            return self._reg(key, found)
        if fn == "querySelectorAll":
            root = self._doc(key)["root"]
            if root is None:
                return []
            return [self._reg(key, e) for e in self._css(root, p.get("query") or "", single=False)]
        if fn in ("dom_querySelector", "dom_querySelectorAll"):
            el = self._el(int(p.get("doc") or 0), key)
            if el is None:
                return None if fn == "dom_querySelector" else []
            if fn == "dom_querySelector":
                return self._reg(int(p.get("doc") or 0), self._css(el, p.get("query") or "", True))
            return [self._reg(int(p.get("doc") or 0), e)
                    for e in self._css(el, p.get("query") or "", False)]
        # element getters
        doc = int(p.get("doc") or 0)
        el = self._el(doc, key)
        if el is None:
            return None
        if fn == "getText":
            return "".join(el.itertext())
        if fn == "getAttributes":
            return dict(el.attrib)
        if fn == "getChildren":
            return [self._reg(doc, c) for c in list(el)]
        if fn == "getClassNames":
            return (el.get("class") or "").split()
        if fn == "getId":
            return el.get("id")
        if fn == "getInnerHTML":
            return (el.text or "") + "".join(
                etree.tostring(c, encoding="unicode") for c in el)
        if fn == "getLocalName":
            return el.tag if isinstance(el.tag, str) else None
        if fn == "getParent":
            return self._reg(doc, el.getparent())
        if fn == "getPreviousSibling":
            prev = el.getprevious()
            return self._reg(doc, prev)
        if fn == "getNextSibling":
            nxt = el.getnext()
            return self._reg(doc, nxt)
        if fn == "getNodes":
            return [self._reg(doc, c) for c in list(el)]
        if fn == "node_text":
            return el.text or ""
        if fn == "node_type":
            return "text" if not isinstance(el.tag, str) else "element"
        if fn == "node_toElement":
            return key
        return None

    @staticmethod
    def _css(root, query: str, single: bool):
        try:
            sel = CSSSelector(query)
            return sel(root)[0] if single else sel(root)
        except Exception:
            return None if single else []

    # ---------------------------------------------------------------- convert
    def _convert(self, p: Dict[str, Any]) -> Any:
        typ = p.get("type")
        is_encode = p.get("isEncode", True)
        val = p.get("value")
        raw = _unb64(val) if not isinstance(val, str) else val.encode()
        if typ == "utf8":
            return raw.decode("utf-8", "replace") if not is_encode else _b64(raw)
        if typ == "gbk":
            return raw.decode("gbk", "replace") if not is_encode else _b64(raw)
        if typ == "base64":
            if is_encode:
                return base64.b64encode(raw).decode()
            return _b64(base64.b64decode(val if isinstance(val, str) else raw))
        if typ in ("md5", "sha1", "sha256", "sha512"):
            h = hashlib.new(typ, raw).digest()
            return _b64(h) if is_encode else h.hex()
        if typ == "hmac":
            algo = p.get("hash") or "sha256"
            digest = hmac.new(_unb64(p.get("key")), raw, algo).hexdigest()
            return digest if p.get("isString") else _b64(bytes.fromhex(digest))
        if typ and typ.startswith("aes"):
            return self._aes(typ, p, raw, is_encode)
        if typ == "rsa":
            return None
        return None

    def _aes(self, mode: str, p: Dict[str, Any], raw: bytes, is_encode: bool) -> Any:
        try:
            from Crypto.Cipher import AES
            from Crypto.Util.Padding import pad, unpad
        except Exception:
            return None
        key = _unb64(p.get("key"))
        iv = _unb64(p.get("iv")) if p.get("iv") else None
        if mode == "aes-ecb":
            cipher = AES.new(key, AES.MODE_ECB)
        elif mode == "aes-cbc":
            cipher = AES.new(key, AES.MODE_CBC, iv)
        elif mode == "aes-cfb":
            cipher = AES.new(key, AES.MODE_CFB, iv)
        elif mode == "aes-ofb":
            cipher = AES.new(key, AES.MODE_OFB, iv)
        else:
            return None
        if is_encode:
            return _b64(cipher.encrypt(pad(raw, AES.block_size)))
        return _b64(unpad(cipher.decrypt(raw), AES.block_size))

    # ---------------------------------------------------------------- network
    def _http(self, p: Dict[str, Any]) -> Any:
        url = p.get("url") or ""
        method = (p.get("http_method") or "GET").upper()
        headers = p.get("headers") or {}
        body = p.get("data")
        data = _unb64(body) if isinstance(body, dict) else (body.encode() if isinstance(body, str) else None)
        try:
            r = self.session.request(method, url, headers=headers, data=data,
                                     proxies=self._proxies(), timeout=self.timeout,
                                     cookies=self._cookies)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        body = _b64(r.content) if p.get("bytes") else r.text
        return {"status": r.status_code,
                "headers": {k: v for k, v in r.headers.items()},
                "body": body}

    def _cookie(self, p: Dict[str, Any]) -> Any:
        fn = p.get("function")
        url = p.get("url") or ""
        if fn == "get":
            return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                    for c in self._cookies if c.domain and c.domain in url]
        if fn == "set":
            for c in p.get("cookies") or []:
                if isinstance(c, dict) and c.get("name"):
                    self._cookies.set(c["name"], c.get("value", ""), domain=c.get("domain"),
                                      path=c.get("path", "/"))
            return None
        if fn == "delete":
            return None
        return None

    # ---------------------------------------------------------------- storage
    def _storage(self, method: str, p: Dict[str, Any]) -> Any:
        key = str(p.get("data_key") or p.get("setting_key") or p.get("key") or "")
        if method == "load_data":
            return p.get("default") if key not in self._store else self._store[key]
        if method == "save_data":
            self._store[key] = p.get("data")
            self._save_store()
            return None
        if method == "delete_data":
            self._store.pop(key, None)
            self._save_store()
            return None
        if method == "load_setting":
            return p.get("default")
        return None

    def _save_store(self) -> None:
        try:
            self._store_path.write_text(json.dumps(self._store, ensure_ascii=False), "utf-8")
        except Exception:
            pass

    # ---------------------------------------------------------------- discovery
    @staticmethod
    def _mirror(url: str) -> List[str]:
        """raw.githubusercontent.com is unreachable in some networks; try jsDelivr first."""
        m = re.match(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)", url)
        if not m:
            return [url]
        user, repo, ref, path = m.groups()
        return [f"https://cdn.jsdelivr.net/gh/{user}/{repo}@{ref}/{path}", url]

    def source_file(self, sid: str, url: str) -> Path:
        """Download a source .js once and keep it in the cache."""
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in sid)[:60]
        p = self.cache / "sources" / f"{safe}.js"
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists() or p.stat().st_size < 500:
            last: Optional[Exception] = None
            for candidate in self._mirror(url):
                try:
                    r = self._get(candidate, timeout=60)
                    r.raise_for_status()
                    p.write_bytes(r.content)
                    last = None
                    break
                except Exception as e:
                    last = e
            if last is not None:
                raise last
        return p


_RUNTIME_LOCK = threading.Lock()
_RUNTIME: Optional[VeneraRuntime] = None


def runtime(home: Path, proxy: Optional[str] = None) -> VeneraRuntime:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = VeneraRuntime(home, proxy=proxy)
        return _RUNTIME
