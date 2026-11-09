"""E-Hentai / JM (18comic) / Pixiv -> PDF + in-browser reader.

- Paste a gallery link or an ID -> fetch images -> build a PDF
- Search by title or author on every supported source
- Downloaded images are kept for a few days so they can be read in-browser
"""
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path
from urllib.parse import quote

import img2pdf
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

BASE = Path(os.environ.get("MANGA_HOME", "/opt/manga"))
DATA = BASE / "data"
JOBS_DIR = DATA / "jobs"
OUT_DIR = DATA / "output"
CONF_DIR = BASE / "config"
STATE_FILE = DATA / "jobs.json"
COOKIE_FILE = CONF_DIR / "cookies.txt"
GDL = os.environ.get("GALLERY_DL", str(BASE / "venv" / "bin" / "gallery-dl"))
GDL_CONF = str(CONF_DIR / "gallery-dl.conf")
PROXY = os.environ.get("MANGA_PROXY", "http://127.0.0.1:7890")
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else {}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
KEEP_DAYS = int(os.environ.get("MANGA_KEEP_DAYS", "5"))
MAX_PAGES = 3000
# Storage caps: when exceeded, delete oldest downloads first
MAX_TOTAL_BYTES = int(float(os.environ.get("MANGA_MAX_GB", "12")) * 1024 ** 3)
MAX_JOBS = int(os.environ.get("MANGA_MAX_JOBS", "150"))
DISK_WARN_PCT = int(os.environ.get("MANGA_DISK_WARN", "85"))
DISK_CRIT_PCT = int(os.environ.get("MANGA_DISK_CRIT", "93"))

FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
           b'<rect width="64" height="64" rx="14" fill="#4f7cff"/>'
           b'<path d="M18 16h20a8 8 0 0 1 8 8v24H26a8 8 0 0 1-8-8z" fill="#fff" opacity=".92"/>'
           b'<path d="M24 24h14M24 32h14M24 40h9" stroke="#4f7cff" stroke-width="3" '
           b'stroke-linecap="round"/></svg>')

for d in (JOBS_DIR, OUT_DIR, CONF_DIR):
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.WARNING)

try:
    import jmcomic
    for _a in ("LOGGER", "LOG"):
        if hasattr(jmcomic.JmModuleConfig, _a):
            getattr(jmcomic.JmModuleConfig, _a).setLevel(logging.WARNING)
    JM_OK = True
except Exception:
    jmcomic = None
    JM_OK = False

_lock = threading.RLock()
_jobs: dict = {}
_jm_lock = threading.Lock()
_jm_client = None
_jm_client_at = 0.0


# ---------------------------------------------------------------- State
def load_state():
    global _jobs
    if STATE_FILE.exists():
        try:
            _jobs = json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            _jobs = {}
    for j in _jobs.values():
        j.setdefault("platform", "eh")
        if j.get("status") in ("queued", "running"):
            j["status"] = "queued"
            j["message"] = "Server restarted, re-queued"


def save_state():
    with _lock:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_jobs, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- Utilities
def parse_input(raw: str):
    """Return (platform, normalized input)"""
    s = (raw or "").strip()
    if not s:
        raise ValueError("Please enter a link or a JM id")

    m = re.search(r"(e-hentai\.org|exhentai\.org)/g/(\d+)/([0-9a-zA-Z]+)", s)
    if m:
        return "eh", f"https://{m.group(1)}/g/{m.group(2)}/{m.group(3)}/"

    m = re.fullmatch(r"(\d+)/([0-9a-zA-Z]{6,})", s)
    if m:
        return "eh", f"https://e-hentai.org/g/{m.group(1)}/{m.group(2)}/"

    # Pixiv
    m = re.search(r"pixiv\.net/(?:[a-z]{2}/)?artworks/(\d+)", s)
    if m:
        return "pixiv", m.group(1)
    m = re.search(r"pixiv\.net/(?:[a-z]{2}/)?users/(\d+)", s)
    if m:
        return "pixiv_user", m.group(1)
    m = re.search(r"pixiv\.net/(?:[a-z]{2}/)?member_illust\.php\?.*illust_id=(\d+)", s)
    if m:
        return "pixiv", m.group(1)
    m = re.fullmatch(r"(?:pixiv|pix|px)[\s_:-]*(\d{4,12})", s, re.I)
    if m:
        return "pixiv", m.group(1)

    # JM (18comic)
    m = re.search(r"(?:18comic|jmcomic|jm)[^\d]{0,6}(\d{4,8})", s, re.I)
    if m:
        return "jm", m.group(1)
    m = re.search(r"/(?:album|photo)/(\d{4,8})", s)
    if m:
        return "jm", m.group(1)
    if re.fullmatch(r"\d{4,8}", s):
        return "jm", s

    raise ValueError(
        "Unrecognized input. Supported formats:\n"
        "· E-Hentai gallery: https://e-hentai.org/g/123456/abcdef12/\n"
        "· JM id: jm1454202 / 1454202\n"
        "· Pixiv artwork: pixiv151885064 or https://www.pixiv.net/artworks/151885064")


def read_cookie_jar():
    jar = {}
    if not COOKIE_FILE.exists():
        return jar
    for line in COOKIE_FILE.read_text("utf-8", "replace").splitlines():
        if not line or line.startswith("#"):
            continue
        p = line.split("\t")
        if len(p) >= 7:
            jar[p[5]] = p[6]
    return jar


def http_get(url: str, timeout=45):
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    return requests.get(url, headers=headers, proxies=PROXIES, timeout=timeout,
                        cookies=read_cookie_jar())


IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".jxl"}
MIHOMO_API = os.environ.get("MIHOMO_API", "http://127.0.0.1:9090")
EH_GROUP = os.environ.get("EH_GROUP", "EH")


def rotate_eh_node():
    try:
        r = requests.get(f"{MIHOMO_API}/proxies/{EH_GROUP}", timeout=8).json()
        members = r.get("all") or []
        now = r.get("now")
        if len(members) < 2:
            return None
        idx = members.index(now) if now in members else -1
        nxt = members[(idx + 1) % len(members)]
        requests.put(f"{MIHOMO_API}/proxies/{EH_GROUP}", json={"name": nxt}, timeout=8)
        time.sleep(2.5)
        return nxt
    except Exception:
        return None


def count_images(root: Path) -> int:
    n = 0
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in IMG_EXT:
            n += 1
    return n


def natural_key(p: Path):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", p.name)]


def collect_images(img_dir: Path):
    return sorted([p for p in img_dir.rglob("*")
                   if p.is_file() and p.suffix.lower() in IMG_EXT], key=natural_key)


_img_cache: dict = {}
_img_cache_lock = threading.Lock()


def job_images(job_id: str, ttl: float = 1.0):
    """Short-TTL cache for the image list: the reader requests dozens or hundreds
    of images in a row, and re-globbing the directory each time pegs the CPU."""
    now = time.time()
    with _img_cache_lock:
        hit = _img_cache.get(job_id)
        if hit and now - hit[0] < ttl:
            return hit[1]
    d = JOBS_DIR / job_id / "img"
    imgs = collect_images(d) if d.exists() else []
    with _img_cache_lock:
        _img_cache[job_id] = (now, imgs)
    return imgs


def drop_img_cache(job_id: str):
    with _img_cache_lock:
        _img_cache.pop(job_id, None)


def build_pdf(images, out_path: Path):
    supported = {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".jp2"}
    conv_dir = out_path.parent / "_conv"
    final = []
    try:
        for i, src in enumerate(images):
            if src.suffix.lower() in supported:
                final.append(str(src))
                continue
            from PIL import Image
            conv_dir.mkdir(exist_ok=True)
            dst = conv_dir / f"{i:05d}.jpg"
            im = Image.open(src)
            if im.mode in ("RGBA", "P", "LA"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                im = im.convert("RGBA")
                bg.paste(im, mask=im.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
            im.save(dst, "JPEG", quality=90, optimize=True)
            final.append(str(dst))
        if not final:
            raise RuntimeError("No usable images")
        with open(out_path, "wb") as f:
            img2pdf.convert(final, outputstream=f)
    finally:
        shutil.rmtree(conv_dir, ignore_errors=True)


# ---------------------------------------------------------------- JM
def jm_client():
    global _jm_client, _jm_client_at
    if not JM_OK:
        raise RuntimeError("jmcomic is not installed on the server")
    with _jm_lock:
        if _jm_client is None or time.time() - _jm_client_at > 600:
            opt = jmcomic.JmOption.default()
            _jm_client = opt.new_jm_client()
            _jm_client_at = time.time()
        return _jm_client


def probe_jm(album_id: str):
    detail = jm_client().get_album_detail(album_id)
    tags = []
    try:
        tags = list(detail.tags)[:6]
    except Exception:
        pass
    return {"title": str(detail.name), "pages": int(detail.page_count),
            "author": str(getattr(detail, "author", "") or ""), "tags": tags}


_jm_search_lock = threading.Lock()
_search_cache: dict = {}
_search_cache_lock = threading.Lock()
SEARCH_TTL = 120


def jm_search(q: str, by: str, page: int):
    key = ("jm", by, q, page)
    now = time.time()
    with _search_cache_lock:
        hit = _search_cache.get(key)
        if hit and now - hit[0] < SEARCH_TTL:
            return hit[1]
    with _jm_search_lock:
        cli = jm_client()
        fn = cli.search_author if by == "author" else cli.search_work
        sp = fn(q, page=page)
        out = []
        for aid, title in list(sp.iter_id_title()):
            out.append({"platform": "jm", "id": str(aid), "title": title.strip(),
                        "url": f"https://18comic.vip/album/{aid}/"})
    with _search_cache_lock:
        _search_cache[key] = (now, out)
        if len(_search_cache) > 200:
            for k in list(_search_cache)[:100]:
                _search_cache.pop(k, None)
    return out


# ---------------------------------------------------------------- EH
def probe_eh(url: str):
    info = {"title": None, "pages": None}
    r = http_get(url)
    t = r.text
    if "temporarily banned" in t or "excessive request rate" in t:
        raise RuntimeError("E-Hentai temporarily banned this exit IP, try again later")
    if "Sad Panda" in t or "This page is not available" in t:
        raise RuntimeError("Login cookie required, or the gallery does not exist (Sad Panda)")
    m = re.search(r'<h1 id="gn">(.*?)</h1>', t, re.S)
    if m:
        info["title"] = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    m = re.search(r'class="gdt2">(\d+)\s*pages', t)
    if m:
        info["pages"] = int(m.group(1))
    return info


_eh_net_lock = threading.Lock()


def eh_search(q: str, by: str, page: int):
    key = ("eh", by, q, page)
    now = time.time()
    with _search_cache_lock:
        hit = _search_cache.get(key)
        if hit and now - hit[0] < SEARCH_TTL:
            return hit[1]
    query = f'artist:"{q}"' if by == "author" else q
    with _eh_net_lock:
        out = _eh_search_once(query, page)
        if not out and by == "author":
            out = _eh_search_once(q, page)
    with _search_cache_lock:
        _search_cache[key] = (now, out)
    return out


def _eh_search_once(query: str, page: int):
    params = {"f_search": query}
    if page > 1:
        params["page"] = page - 1
    url = "https://e-hentai.org/?" + "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    r = http_get(url)
    if "temporarily banned" in r.text or "excessive request rate" in r.text:
        raise RuntimeError("E-Hentai is rate-limiting this exit IP, try again later")
    rows = re.findall(
        r'<td class="gl3c glname"[^>]*>\s*<a href="(https://e-hentai\.org/g/(\d+)/([0-9a-f]+)/)">'
        r'<div class="glink">(.*?)</div>', r.text, re.S)
    results = []
    for link, gid, token, title in rows:
        results.append({"platform": "eh", "id": gid,
                        "title": re.sub(r"<[^>]+>", "", title).strip(),
                        "url": link})
    return results


# ---------------------------------------------------------------- Pixiv
PIXIV_AJAX = "https://www.pixiv.net/ajax/"
PIXIV_COOKIE_FILE = CONF_DIR / "pixiv_cookies.txt"


def pixiv_cookies():
    jar = {}
    if PIXIV_COOKIE_FILE.exists():
        for line in PIXIV_COOKIE_FILE.read_text("utf-8", "replace").splitlines():
            if not line or line.startswith("#"):
                continue
            p = line.split("\t")
            if len(p) >= 7:
                jar[p[5]] = p[6]
    return jar


def pixiv_json(path, timeout=45):
    headers = {"User-Agent": UA, "Referer": "https://www.pixiv.net/",
               "Accept": "application/json", "Accept-Language": "zh-CN,zh;q=0.9"}
    r = requests.get(PIXIV_AJAX + path, headers=headers, proxies=PROXIES,
                     cookies=pixiv_cookies(), timeout=timeout)
    try:
        d = r.json()
    except Exception:
        raise RuntimeError(f"Pixiv returned non-JSON (HTTP {r.status_code})")
    if d.get("error"):
        raise RuntimeError(f"Pixiv API error: {str(d.get('message') or d.get('error'))[:120]}")
    return d


def probe_pixiv(art_id):
    b = pixiv_json(f"illust/{art_id}?lang=zh").get("body") or {}
    return {"title": (b.get("title") or f"pixiv {art_id}").strip(),
            "pages": int(b.get("pageCount") or 1),
            "author": b.get("userName") or ""}


def _pixiv_urls(art_id, original=False):
    """When original=False, use the 1200px 'regular' image; PDF size drops by more than half."""
    pages = pixiv_json(f"illust/{art_id}/pages?lang=zh").get("body") or []
    key = "original" if original else "regular"
    urls = []
    for pg in pages:
        u = (pg.get("urls") or {}).get(key) or (pg.get("urls") or {}).get("regular")
        if u:
            urls.append(u)
    return urls


def probe_pixiv_user(uid):
    d = pixiv_json(f"user/{uid}/profile/all?lang=zh")
    ill = (d.get("body") or {}).get("illusts") or {}
    ids = sorted(ill.keys(), key=lambda x: -int(x))[:10]
    return {"title": f"Pixiv user {uid} (latest {len(ids)} works)", "pages": None}


def run_pixiv_user(job, img_dir, log_path):
    uid = job["input"]
    d = pixiv_json(f"user/{uid}/profile/all?lang=zh")
    b = d.get("body") or {}
    ill = b.get("illusts") or {}
    manga = b.get("manga") or {}
    ids = sorted(list(ill.keys()) + list(manga.keys()), key=lambda x: -int(x))[:10]
    if not ids:
        raise RuntimeError("This Pixiv user has no public works")
    urls = []
    for aid in ids:
        urls.extend(_pixiv_urls(aid, bool(job.get("original"))))
    if not urls:
        raise RuntimeError("Pixiv returned no image URLs (login cookie may be required; PHPSESSID for R-18)")
    return _pixiv_download(urls, img_dir, job)


def run_pixiv(job, img_dir, log_path):
    urls = _pixiv_urls(job["input"], bool(job.get("original")))
    if not urls:
        raise RuntimeError("Pixiv returned no image URLs; this work may need a login cookie (PHPSESSID for R-18)")
    return _pixiv_download(urls, img_dir, job)


def _pixiv_download(urls, img_dir, job):
    total = len(urls)
    with _lock:
        job["pages"] = total
        job["message"] = f"{total} pages, starting download…"
    headers = {"User-Agent": UA, "Referer": "https://www.pixiv.net/"}
    for i, u in enumerate(urls, 1):
        if job.get("cancel"):
            return []
        ext = os.path.splitext(u.split("?")[0])[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            ext = ".jpg"
        dst = img_dir / f"{i:04d}{ext}"
        if not dst.exists():
            r = requests.get(u, headers=headers, proxies=PROXIES, timeout=120,
                             cookies=pixiv_cookies())
            if r.status_code != 200 or not r.content:
                raise RuntimeError(f"Pixiv image download failed (HTTP {r.status_code}, page {i}/{total})")
            dst.write_bytes(r.content)
        with _lock:
            job["files"] = i
            job["progress"] = min(97, int(i * 100 / total))
            job["message"] = f"Downloaded {i} / {total} pages"
    return collect_images(img_dir)


def pixiv_search(q, by, page):
    from urllib.parse import quote as _q
    path = (f"search/artworks/{_q(q)}?word={_q(q)}&order=date_d&mode=all"
            f"&p={page}&s_mode=s_tag&type=all&lang=zh")
    body = (pixiv_json(path).get("body") or {}).get("illustManga") or {}
    out = []
    for it in body.get("data") or []:
        out.append({"platform": "pixiv", "id": str(it.get("id")),
                    "title": (it.get("title") or "").strip(),
                    "author": it.get("userName") or "",
                    "url": f"https://www.pixiv.net/artworks/{it.get('id')}"})
    if by == "author":
        # Pixiv user search requires login, so filter artwork results by author name instead
        hit = [x for x in out if q.lower() in (x["author"] or "").lower()]
        return hit or out
    return out


# ---------------------------------------------------------------- Job execution
def run_eh(job, img_dir, log_path):
    url = job["input"]
    cmd = [GDL, "--proxy", PROXY, "--config", GDL_CONF,
           "-d", str(img_dir), "--no-mtime",
           "-o", "extractor.exhentai.original=" +
                 ("true" if job.get("original") else "false")]
    if COOKIE_FILE.exists():
        cmd += ["--cookies", str(COOKIE_FILE)]
    cmd.append(url)
    images = []
    rc = None
    for attempt in range(2):
        if attempt:
            newnode = rotate_eh_node()
            with _lock:
                job["message"] = f"Exit blocked, switched to {newnode or 'backup node'}, retrying…"
                job["progress"] = 0
            if newnode is None:
                raise RuntimeError("E-Hentai rate-limited this exit IP and no backup node is available")
            shutil.rmtree(img_dir, ignore_errors=True)
            img_dir.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as logf:
            logf.write(("\n=== attempt %d: %s\n" % (attempt + 1, " ".join(cmd))).encode())
            logf.flush()
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=str(BASE))
            job["pid"] = proc.pid
            while True:
                if proc.poll() is not None:
                    rc = proc.returncode
                    break
                if job.get("cancel"):
                    proc.kill()
                    return []
                n = count_images(img_dir)
                total = job.get("pages") or 0
                with _lock:
                    job["files"] = n
                    if total:
                        job["progress"] = min(97, int(n * 100 / total))
                    job["message"] = f"Downloaded {n}" + (f" / {total} pages" if total else " images")
                time.sleep(1.2)
        if job.get("cancel"):
            return []
        images = collect_images(img_dir)
        if images:
            break
        tail = log_path.read_text("utf-8", "replace")[-1500:] if log_path.exists() else ""
        if "temporarily banned" in tail or "excessive request rate" in tail:
            continue
        if "Sad Panda" in tail or "not available" in tail:
            raise RuntimeError("This gallery needs login cookies (ExHentai / sad panda); configure them at the bottom of the page")
        raise RuntimeError(f"No images downloaded (gallery-dl exit {rc})\n{tail[-500:]}")
    if not images:
        raise RuntimeError("E-Hentai rate-limited this exit IP; retry in 1-3 hours")
    return images


def run_jm(job, img_dir, log_path):
    album_id = job["input"]
    opt = jmcomic.JmOption.default()
    opt.dir_rule.base_dir = str(img_dir)
    stop = threading.Event()

    def ticker():
        while not stop.is_set():
            n = count_images(img_dir)
            total = job.get("pages") or 0
            with _lock:
                job["files"] = n
                if total:
                    job["progress"] = min(97, int(n * 100 / total))
                job["message"] = f"Downloaded {n}" + (f" / {total} pages" if total else " images")
            time.sleep(1.0)

    t = threading.Thread(target=ticker, daemon=True)
    t.start()
    try:
        opt.download_album(album_id)
    finally:
        stop.set()
    if job.get("cancel"):
        return []
    return collect_images(img_dir)


PROBES = {"eh": probe_eh, "jm": probe_jm, "pixiv": probe_pixiv,
          "pixiv_user": probe_pixiv_user}
RUNNERS = {"eh": run_eh, "jm": run_jm, "pixiv": run_pixiv,
           "pixiv_user": run_pixiv_user}


def run_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return
    dest = JOBS_DIR / job_id
    img_dir = dest / "img"
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    img_dir.mkdir(parents=True, exist_ok=True)
    log_path = dest / "run.log"
    pdf_path = OUT_DIR / f"{job_id}.pdf"

    with _lock:
        job.update(status="running", started=time.time(), message="Fetching metadata…",
                   progress=0, files=0, cancel=False)

    try:
        # Probe inside the worker thread so HTTP requests are not blocked
        try:
            info = PROBES[job["platform"]](job["input"])
            with _lock:
                if info.get("title"):
                    job["title"] = info["title"]
                if info.get("pages"):
                    job["pages"] = info["pages"]
                job["message"] = "Fetching images…"
        except Exception as pe:
            with _lock:
                job["message"] = f"Metadata fetch failed (still trying to download): {str(pe)[:120]}"

        if job.get("pages") and job["pages"] > MAX_PAGES:
            raise RuntimeError(f"Too many pages ({job['pages']}), limit is {MAX_PAGES}")

        images = RUNNERS[job["platform"]](job, img_dir, log_path)

        if job.get("cancel"):
            with _lock:
                job.update(status="canceled", message="Canceled", finished=time.time())
            save_state()
            return

        if not images:
            raise RuntimeError("No images were downloaded")

        with _lock:
            job["pages"] = len(images)
            job["images"] = len(images)
            job["readable"] = True
            job["progress"] = 98
            job["message"] = f"Building PDF ({len(images)} pages)…"

        build_pdf(images, pdf_path)
        size = pdf_path.stat().st_size
        with _lock:
            job.update(status="done", finished=time.time(), progress=100,
                       message=f"Done, {len(images)} pages / {size/1048576:.1f} MB",
                       pdf=pdf_path.name, size=size, files=len(images))
    except Exception as e:
        with _lock:
            job.update(status="error", finished=time.time(),
                       message=str(e)[:800], trace=traceback.format_exc()[-1200:])
    finally:
        drop_img_cache(job_id)
        job.pop("pid", None)
        save_state()
        try:
            cleanup_storage()
        except Exception:
            pass


_queue: list = []
_cv = threading.Condition()


_last_cleanup = [0.0]


def worker_loop():
    while True:
        with _cv:
            while not _queue:
                _cv.wait(timeout=1800)   # wake up every 30 min to run cleanup
            job_id = _queue.pop(0) if _queue else None
        if job_id is None:
            try:
                cleanup_storage()
            except Exception:
                pass
            continue
        try:
            run_job(job_id)
        except Exception:
            traceback.print_exc()


def enqueue(job_id: str):
    with _cv:
        if job_id not in _queue:
            _queue.append(job_id)
        _cv.notify()


def dir_size(root: Path) -> int:
    total = 0
    if not root.exists():
        return 0
    for base, _dirs, files in os.walk(root):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def data_size() -> int:
    return dir_size(JOBS_DIR) + dir_size(OUT_DIR)


def purge_job(jid: str):
    """Delete one job's images and its PDF"""
    shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)
    pdf = OUT_DIR / f"{jid}.pdf"
    try:
        if pdf.exists():
            pdf.unlink()
    except OSError:
        pass
    drop_img_cache(jid)
    _jobs.pop(jid, None)


def cleanup_storage(log=None):
    """Clean up oldest downloads by TTL -> job count -> total size"""
    now = time.time()
    finished = ("done", "error", "canceled")
    removed = []

    # 1) older than the retention window
    for jid, j in list(_jobs.items()):
        if j.get("status") not in finished:
            continue
        fin = j.get("finished") or j.get("created") or now
        if now - fin > KEEP_DAYS * 86400:
            purge_job(jid)
            removed.append(jid)

    # 2) over the job-count cap (finished jobs only, oldest first)
    done_jobs = sorted([j for j in _jobs.values() if j.get("status") in finished],
                       key=lambda x: x.get("created", 0))
    per_job = {}
    total = None
    while done_jobs and len(_jobs) > MAX_JOBS:
        purge_job(done_jobs.pop(0)["id"])

    # 3) over the total size cap
    total = data_size()
    if total > MAX_TOTAL_BYTES:
        done_jobs = sorted([j for j in _jobs.values() if j.get("status") in finished],
                           key=lambda x: x.get("created", 0))
        for j in done_jobs:
            if total <= MAX_TOTAL_BYTES * 0.85:
                break
            jid = j["id"]
            sz = dir_size(JOBS_DIR / jid)
            pdf = OUT_DIR / f"{jid}.pdf"
            if pdf.exists():
                try:
                    sz += pdf.stat().st_size
                except OSError:
                    pass
            purge_job(jid)
            total -= sz
            removed.append(jid)

    # 4) orphan files (PDFs whose job record is gone)
    for pdf in OUT_DIR.glob("*.pdf"):
        if pdf.stem not in _jobs:
            try:
                pdf.unlink()
            except OSError:
                pass

    if removed:
        save_state()
        if log:
            log.info("cleanup removed %d jobs, data=%d MB", len(removed), data_size() // 1048576)
    return removed


# ---------------------------------------------------------------- Web
app = FastAPI(title="EH/JM -> PDF")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title data-i18n="app.title">Manga → PDF & Reader</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0f1115;color:#e6e8ee;font:15px/1.6 -apple-system,"Segoe UI","Noto Sans","Microsoft YaHei",sans-serif}
.wrap{max-width:880px;margin:0 auto;padding:28px 18px 80px}
.hd{display:flex;align-items:flex-start;gap:12px}
.hd .grow{flex:1;min-width:0}
h1{font-size:21px;margin:0 0 3px}
.sub{color:#8b93a7;font-size:13px;margin-bottom:18px}
select.lang{background:#171a21;border:1px solid #242938;color:#9aa3b8;border-radius:8px;
 padding:6px 8px;font-size:12.5px;outline:none;cursor:pointer;width:auto;max-width:130px;
 flex:0 0 auto;margin-top:2px}
.tabs{display:flex;gap:8px;margin-bottom:14px}
.tab{padding:8px 16px;border-radius:9px;background:#171a21;border:1px solid #242938;
 color:#9aa3b8;cursor:pointer;font-size:14px}
.tab.on{background:#1d2436;border-color:#3a4a75;color:#dbe3f5}
.pane{display:none}.pane.on{display:block}
.card{background:#171a21;border:1px solid #242938;border-radius:12px;padding:16px;margin-bottom:14px}
label{display:block;font-size:13px;color:#9aa3b8;margin-bottom:6px}
input[type=text],select{width:100%;background:#0f1115;border:1px solid #2b3145;color:#e6e8ee;
 border-radius:9px;padding:11px 12px;font-size:14px;outline:none}
input[type=text]:focus,select:focus{border-color:#4f7cff}
.row{display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap}
button{background:#4f7cff;color:#fff;border:0;border-radius:9px;padding:10px 18px;font-size:14px;cursor:pointer}
button:hover{background:#3f6ae8}
button.ghost{background:#232936;color:#c9d1e3}
button.ghost:hover{background:#2b3245}
button.mini{padding:6px 12px;font-size:12.5px}
.job{background:#171a21;border:1px solid #242938;border-radius:12px;padding:13px 15px;margin-bottom:9px}
.job h3{margin:0 0 5px;font-size:14px;font-weight:600;word-break:break-all}
.meta{font-size:12px;color:#8b93a7}
.bar{height:6px;background:#242938;border-radius:3px;overflow:hidden;margin:8px 0}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#4f7cff,#7aa2ff);width:0;transition:width .4s}
.tag{font-size:11px;padding:1px 7px;border-radius:20px;background:#232936;color:#9aa3b8;margin-left:6px}
.tag.done{background:#123526;color:#4ade80}
.tag.error{background:#3a1620;color:#f87171}
.tag.running{background:#1a2547;color:#7aa2ff}
.tag.jm{background:#3a2a12;color:#fbbf24}
.tag.eh{background:#1a2b3f;color:#60a5fa}
.tag.px{background:#1f2b45;color:#93c5fd}
a.dl{display:inline-block;margin-top:7px;background:#123526;color:#4ade80;text-decoration:none;
 padding:6px 13px;border-radius:8px;font-size:13px}
a.rd{display:inline-block;margin-top:7px;background:#25203c;color:#c4b5fd;text-decoration:none;
 padding:6px 13px;border-radius:8px;font-size:13px}
.err{color:#f87171;font-size:12px;white-space:pre-wrap;margin-top:6px;max-height:120px;overflow:auto}
.res{display:flex;justify-content:space-between;gap:10px;align-items:center;
 border-bottom:1px solid #1e222d;padding:10px 2px}
.res:last-child{border-bottom:0}
.res .t{font-size:13.5px;line-height:1.45;word-break:break-all}
.res .a{display:flex;gap:6px;flex-shrink:0}
details summary{cursor:pointer;color:#9aa3b8;font-size:13px}
.hint{font-size:12px;color:#6b7488;margin-top:8px}
#banner{background:#3a1620;border:1px solid #7f1d1d;color:#fca5a5;border-radius:10px;
 padding:10px 14px;margin-bottom:14px;font-size:13px}
#stats{font-size:12px;color:#5f6779;margin-top:10px;text-align:center}
.pagebar{display:flex;gap:10px;justify-content:center;align-items:center;margin-top:12px;font-size:13px;color:#8b93a7}
.pagebar button{padding:6px 14px;font-size:13px}
.pagebar button:disabled{opacity:.4;cursor:default}
</style></head><body><div class="wrap">
<div class="hd">
  <div class="grow">
    <h1 data-i18n="app.title">Manga → PDF &amp; Reader</h1>
    <div class="sub" data-i18n="app.sub"></div>
  </div>
  <select class="lang" id="lang" aria-label="language">
    <option value="en">English</option>
    <option value="zh">中文</option>
    <option value="ja">日本語</option>
  </select>
</div>
<div id="banner" style="display:none"></div>

<div class="tabs">
  <div class="tab on" data-pane="dl" data-i18n="tab.download"></div>
  <div class="tab" data-pane="search" data-i18n="tab.search"></div>
</div>

<div class="pane on" id="pane-dl">
  <div class="card">
    <label data-i18n="field.input"></label>
    <input type="text" id="url" data-i18n-ph="ph.input" autocomplete="off">
    <div class="row">
      <label style="margin:0"><input type="checkbox" id="orig"> <span data-i18n="opt.original"></span></label>
      <button id="go" data-i18n="btn.generate"></button>
      <button class="ghost" id="refresh" data-i18n="btn.refresh"></button>
    </div>
    <div class="hint" id="msg"></div>
  </div>
</div>

<div class="pane" id="pane-search">
  <div class="card">
    <div class="row" style="margin-top:0">
      <select id="pf" style="flex:1;min-width:130px">
        <option value="jm" data-i18n="pf.jm"></option>
        <option value="eh" data-i18n="pf.eh"></option>
        <option value="pixiv" data-i18n="pf.pixiv"></option>
      </select>
      <select id="by" style="flex:1;min-width:130px">
        <option value="title" data-i18n="by.title"></option>
        <option value="author" data-i18n="by.author"></option>
      </select>
    </div>
    <div class="row">
      <input type="text" id="q" style="flex:3;min-width:200px" data-i18n-ph="ph.search" autocomplete="off">
      <button id="do-search" data-i18n="btn.search"></button>
    </div>
    <div class="hint" id="smsg"></div>
  </div>
  <div class="card" id="results" style="display:none"></div>
</div>

<div id="list"></div>
<div id="stats"></div>

<div class="card">
  <details>
    <summary data-i18n="cookie.title"></summary>
    <div style="margin-top:12px">
      <label>ipb_member_id</label><input type="text" id="c1" autocomplete="off">
      <label style="margin-top:8px">ipb_pass_hash</label><input type="text" id="c2" autocomplete="off">
      <label style="margin-top:8px" data-i18n="cookie.igneous"></label><input type="text" id="c3" autocomplete="off">
      <div class="row"><button class="ghost" id="saveck" data-i18n="btn.saveCookie"></button>
      <span class="meta" id="ckstate"></span></div>
      <div class="hint" data-i18n="cookie.hint"></div>
    </div>
  </details>
</div>

<script>
const I18N = {
  en: {
    "app.title": "Manga → PDF & Reader",
    "app.sub": "Paste a link or an ID. Sources: E-Hentai, JM (18comic), Pixiv.",
    "tab.download": "Download / PDF",
    "tab.search": "Search",
    "field.input": "Link or ID",
    "ph.input": "e-hentai link / jm1454202 / pixiv151885064",
    "ph.search": "Title or author, e.g. frieren",
    "opt.original": "Download original images (slower, larger)",
    "btn.generate": "Generate PDF",
    "btn.refresh": "Refresh",
    "btn.search": "Search",
    "btn.saveCookie": "Save cookies",
    "pf.jm": "JM (18comic)",
    "pf.eh": "E-Hentai",
    "pf.pixiv": "Pixiv",
    "by.title": "By title",
    "by.author": "By author",
    "job.queued": "queued", "job.running": "downloading", "job.done": "done",
    "job.error": "failed", "job.canceled": "canceled",
    "btn.pdf": "Download PDF", "btn.read": "Read online", "btn.cancel": "Cancel",
    "search.searching": "Searching…", "search.page": "Page {page} · {n} results",
    "search.empty": "No results on this page", "search.failed": "Search failed: {e}",
    "search.prev": "Prev", "search.next": "Next", "search.pageN": "Page {n}",
    "msg.submitting": "Submitting…", "msg.queued": "Queued",
    "msg.submitFail": "Submit failed: {e}",
    "msg.offline": "Backend unreachable: {e}",
    "msg.empty": "No jobs yet",
    "cookie.title": "Login cookies (optional — ExHentai / higher quota)",
    "cookie.igneous": "igneous (ExHentai only)",
    "cookie.hint": "Open e-hentai in a browser → DevTools → Application → Cookies and copy these three values.",
    "cookie.saved": "Saved", "cookie.configured": "Configured",
    "cookie.notConfigured": "Not configured (anonymous)", "cookie.fail": "Failed: {e}",
    "stats": "Disk {disk}% · Cache {mb}/{cacheLimit} MB · Jobs {n}/{jobLimit} · Keep {days} d"
  },
  zh: {
    "app.title": "本子 → PDF / 在线看",
    "app.sub": "支持 E-Hentai / 禁漫 JM / Pixiv，贴链接或编号即可",
    "tab.download": "下载 / 转 PDF",
    "tab.search": "搜索",
    "field.input": "链接 / 编号",
    "ph.input": "e-hentai 链接 / jm1454202 / pixiv151885064",
    "ph.search": "输入作品名或作者名，例如 frieren",
    "opt.original": "下载原图（更清晰，更慢）",
    "btn.generate": "开始生成",
    "btn.refresh": "刷新列表",
    "btn.search": "搜索",
    "btn.saveCookie": "保存 Cookie",
    "pf.jm": "禁漫 JM",
    "pf.eh": "E-Hentai",
    "pf.pixiv": "Pixiv",
    "by.title": "按作品名",
    "by.author": "按作者名",
    "job.queued": "Queued", "job.running": "下载中", "job.done": "已完成",
    "job.error": "失败", "job.canceled": "Canceled",
    "btn.pdf": "下载 PDF", "btn.read": "在线阅读", "btn.cancel": "取消",
    "search.searching": "搜索中…", "search.page": "第 {page} 页 · {n} 条",
    "search.empty": "这一页没有结果", "search.failed": "搜索失败：{e}",
    "search.prev": "上一页", "search.next": "下一页", "search.pageN": "第 {n} 页",
    "msg.submitting": "提交中…", "msg.queued": "已加入队列",
    "msg.submitFail": "提交失败：{e}",
    "msg.offline": "连不上后端：{e}",
    "msg.empty": "还没有任务",
    "cookie.title": "登录 Cookie 设置（可选，exhentai / 高清配额需要）",
    "cookie.igneous": "igneous（exhentai 需要，e-hentai 可留空）",
    "cookie.hint": "浏览器登录 e-hentai 后，F12 → Application → Cookies 里可复制这三项。",
    "cookie.saved": "已保存", "cookie.configured": "已配置",
    "cookie.notConfigured": "未配置（匿名）", "cookie.fail": "失败：{e}",
    "stats": "磁盘 {disk}% · 缓存 {mb}/{cacheLimit} MB · 任务 {n}/{jobLimit} · 保留 {days} 天"
  },
  ja: {
    "app.title": "漫画 → PDF・オンライン閲覧",
    "app.sub": "E-Hentai / JM（禁漫）/ Pixiv に対応。リンクか ID を貼るだけ。",
    "tab.download": "ダウンロード / PDF",
    "tab.search": "検索",
    "field.input": "リンクまたは ID",
    "ph.input": "e-hentai リンク / jm1454202 / pixiv151885064",
    "ph.search": "作品名または作者名（例: frieren）",
    "opt.original": "原寸画像を取得（遅い・大きい）",
    "btn.generate": "PDF を生成",
    "btn.refresh": "更新",
    "btn.search": "検索",
    "btn.saveCookie": "Cookie を保存",
    "pf.jm": "JM（禁漫）",
    "pf.eh": "E-Hentai",
    "pf.pixiv": "Pixiv",
    "by.title": "作品名で",
    "by.author": "作者名で",
    "job.queued": "待機中", "job.running": "ダウンロード中", "job.done": "完了",
    "job.error": "失敗", "job.canceled": "キャンセル",
    "btn.pdf": "PDF をダウンロード", "btn.read": "オンラインで読む", "btn.cancel": "中止",
    "search.searching": "検索中…", "search.page": "{page} ページ · {n} 件",
    "search.empty": "このページに結果はありません", "search.failed": "検索失敗: {e}",
    "search.prev": "前へ", "search.next": "次へ", "search.pageN": "{n} ページ",
    "msg.submitting": "送信中…", "msg.queued": "キューに追加しました",
    "msg.submitFail": "送信失敗: {e}",
    "msg.offline": "バックエンドに接続できません: {e}",
    "msg.empty": "まだタスクはありません",
    "cookie.title": "ログイン Cookie（任意 — ExHentai / 高画質枠）",
    "cookie.igneous": "igneous（ExHentai のみ）",
    "cookie.hint": "ブラウザで e-hentai にログイン → F12 → Application → Cookies から 3 つコピー。",
    "cookie.saved": "保存しました", "cookie.configured": "設定済み",
    "cookie.notConfigured": "未設定（匿名）", "cookie.fail": "失敗: {e}",
    "stats": "ディスク {disk}% · キャッシュ {mb}/{cacheLimit} MB · タスク {n}/{jobLimit} · 保持 {days} 日"
  }
};

const SUPPORTED = ["en", "zh", "ja"];
let LANG = "en";
try {
  const saved = localStorage.getItem("lang");
  const nav = (navigator.language || "en").toLowerCase();
  const auto = nav.startsWith("zh") ? "zh" : (nav.startsWith("ja") ? "ja" : "en");
  // ?lang=en in the URL wins, so links can pin a language
  let q = "";
  try { q = new URLSearchParams(location.search).get("lang") || ""; } catch (e) {}
  LANG = SUPPORTED.includes(q) ? q : (SUPPORTED.includes(saved) ? saved : auto);
} catch (e) {}
if (!SUPPORTED.includes(LANG)) LANG = "en";

function t(key, vars){
  let s = (I18N[LANG] && I18N[LANG][key]) || (I18N.en[key] || key);
  if (vars) Object.keys(vars).forEach(k => { s = s.split("{" + k + "}").join(vars[k]); });
  return s;
}

function applyLang(){
  document.documentElement.lang = LANG;
  document.querySelectorAll("[data-i18n]").forEach(el => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-ph]").forEach(el => { el.placeholder = t(el.dataset.i18nPh); });
  const sel = document.querySelector("#lang");
  if (sel) sel.value = LANG;
}

const $ = s => document.querySelector(s);
const BASE = (window.BACKEND || "").replace(/\/$/, "");
// The app may be mounted on a sub-path (e.g. /manga/): derive the mount prefix
// from location. The reader lives at /manga/read/{id}, so /read/ is stripped first.
let PREFIX = (window.BACKEND || "");
while (PREFIX.endsWith("/")) PREFIX = PREFIX.slice(0, -1);
const APP = (() => {
  let q = location.pathname;
  const i = q.indexOf("/read/");
  if (i >= 0) q = q.slice(0, i);
  while (q.endsWith("/")) q = q.slice(0, -1);
  return q;
})();
const u = p => {
  p = String(p);
  while (p.startsWith("/")) p = p.slice(1);
  return PREFIX ? PREFIX + "/" + p : APP + "/" + p;
};
const api = (p, opt) => fetch(p, opt).then(r => r.json());
const esc = s => (s || "").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt = t2 => t2 ? new Date(t2*1000).toLocaleString(LANG === "zh" ? "zh-CN" : (LANG === "ja" ? "ja-JP" : "en-US"), {hour12:false}) : "";

document.querySelectorAll('.tab').forEach(el => el.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('on'));
  document.querySelectorAll('.pane').forEach(x => x.classList.remove('on'));
  el.classList.add('on');
  $('#pane-' + el.dataset.pane).classList.add('on');
});

function jobCard(j){
  const tag = j.status==='done'?'done':(j.status==='error'?'error':(j.status==='running'?'running':''));
  const p = j.status==='done'?100:(j.progress||0);
  const PF = {jm:'<span class="tag jm">JM</span>', eh:'<span class="tag eh">EH</span>',
              pixiv:'<span class="tag px">PIXIV</span>',
              pixiv_user:'<span class="tag px">PIXIV</span>'};
  return `<div class="job">
    <h3>${esc(j.title || j.input)}${PF[j.platform] || ''}<span class="tag ${tag}">${t('job.' + j.status)}</span></h3>
    <div class="meta">${esc(j.input)} · ${fmt(j.created)}</div>
    <div class="bar"><i style="width:${p}%"></i></div>
    <div class="meta">${esc(j.message||'')}</div>
    ${j.status==='done' && j.pdf ? `<a class="dl" href="${u('dl/' + j.id)}">${t('btn.pdf')}（${(j.size/1048576).toFixed(1)} MB）</a>` : ''}
    ${j.readable ? `<a class="rd" href="${u('read/' + j.id)}" target="_blank">${t('btn.read')}</a>` : ''}
    ${j.status==='error' ? `<div class="err">${esc(j.message||'')}</div>` : ''}
    ${j.status==='queued'||j.status==='running' ? `<div class="row"><button class="mini ghost" onclick="cancelJob('${j.id}')">${t('btn.cancel')}</button></div>` : ''}
  </div>`;
}

let lastSig = "";
let hasActive = false;

async function load(){
  let d;
  try { d = await api(u('api/jobs')); }
  catch (e) { $('#list').innerHTML = '<div class="meta" style="padding:8px 2px">' + esc(t('msg.offline', {e:String(e)})) + '</div>'; return; }
  const jobs = d.jobs || [];
  hasActive = jobs.some(j => j.status === 'running' || j.status === 'queued');
  const sig = LANG + JSON.stringify(jobs);
  if (sig === lastSig) return;
  lastSig = sig;
  $('#list').innerHTML = jobs.length
    ? jobs.map(jobCard).join('')
    : '<div class="meta" style="padding:8px 2px">' + t('msg.empty') + '</div>';
}

async function loadStatus(){
  try {
    const st = await api(u('api/status'));
    const b = $('#banner');
    if (st.warnings && st.warnings.length){
      b.textContent = '⚠ ' + st.warnings.join('　·　');
      b.style.display = 'block';
    } else { b.style.display = 'none'; }
    $('#stats').textContent = t('stats', {
      disk: st.disk.used_pct, mb: st.data.mb, cacheLimit: st.data.limit_mb,
      n: st.jobs.total, jobLimit: st.jobs.limit, days: st.limits.keep_days
    });
  } catch (e) {}
}

function scheduleNext(){
  setTimeout(async () => { await load(); scheduleNext(); }, hasActive ? 2000 : 6000);
}

async function cancelJob(id){
  await fetch(u('api/jobs/' + id + '/cancel'), {method:'POST'});
  load();
}

async function submit(raw){
  return await fetch(u('api/jobs'), {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({url: raw, original: $('#orig').checked})
  }).then(x => x.json());
}

$('#go').onclick = async () => {
  const v = $('#url').value.trim();
  if (!v) return;
  $('#msg').textContent = t('msg.submitting');
  try {
    const d = await submit(v);
    if (d.error){ $('#msg').textContent = d.error; return; }
    $('#msg').textContent = t('msg.queued') + (d.title ? ': ' + d.title : '');
    $('#url').value = '';
    load();
  } catch(e){ $('#msg').textContent = t('msg.submitFail', {e: String(e)}); }
};
$('#url').addEventListener('keydown', e => { if (e.key === 'Enter') $('#go').click(); });
$('#refresh').onclick = load;

let searchPage = 1;

async function runSearch(page){
  const q = $('#q').value.trim();
  if (!q) return;
  searchPage = page;
  $('#smsg').textContent = t('search.searching');
  $('#results').style.display = 'none';
  try {
    const d = await api(u(`api/search?platform=${$('#pf').value}&by=${$('#by').value}&q=${encodeURIComponent(q)}&page=${page}`));
    if (d.error){ $('#smsg').textContent = d.error; return; }
    window.__res = d.results;
    $('#smsg').textContent = d.results.length
      ? t('search.page', {page: page, n: d.results.length}) : t('search.empty');
    if (!d.results.length){
      $('#results').innerHTML = '<div class="meta" style="padding:8px 2px">' + t('search.empty') + '</div>';
      $('#results').style.display = 'block';
      return;
    }
    $('#results').innerHTML = d.results.map((r, i) => `
      <div class="res">
        <div class="t">${esc(r.title)}<div class="meta">${esc(r.author || r.id)}</div></div>
        <div class="a"><button class="mini" onclick="pick(${i})">${t('btn.pdf')}</button></div>
      </div>`).join('') + `
      <div class="pagebar">
        <button class="ghost" onclick="runSearch(${page - 1})" ${page <= 1 ? 'disabled' : ''}>${t('search.prev')}</button>
        <span>${t('search.pageN', {n: page})}</span>
        <button class="ghost" onclick="runSearch(${page + 1})" ${d.results.length < 10 ? 'disabled' : ''}>${t('search.next')}</button>
      </div>`;
    $('#results').style.display = 'block';
  } catch(e){ $('#smsg').textContent = t('search.failed', {e: String(e)}); }
}

$('#do-search').onclick = () => runSearch(1);
$('#q').addEventListener('keydown', e => { if (e.key === 'Enter') $('#do-search').click(); });

async function pick(i){
  const r = window.__res[i];
  const map = {jm: 'jm' + r.id, pixiv: 'pixiv' + r.id};
  const d = await submit(map[r.platform] || r.url);
  if (d.error){ alert(d.error); return; }
  document.querySelector('.tab[data-pane="dl"]').click();
  load();
}

$('#saveck').onclick = async () => {
  try {
    const d = await api(u('api/cookies'), {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ipb_member_id:$('#c1').value, ipb_pass_hash:$('#c2').value, igneous:$('#c3').value})
    });
    $('#ckstate').textContent = d.ok ? t('cookie.saved') : t('cookie.fail', {e: d.error || ''});
  } catch(e){ $('#ckstate').textContent = t('cookie.fail', {e: String(e)}); }
};

document.querySelector('#lang').onchange = (e) => {
  LANG = e.target.value;
  try { localStorage.setItem("lang", LANG); } catch (err) {}
  applyLang();
  lastSig = "";
  load();
  loadStatus();
};

(async () => {
  applyLang();
  try {
    const s = await api(u('api/cookies'));
    $('#ckstate').textContent = s.configured ? t('cookie.configured') : t('cookie.notConfigured');
  } catch(e){}
  load();
  loadStatus();
  scheduleNext();
  setInterval(loadStatus, 30000);
})();
</script></div></body></html>"""

READER = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reader</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0a0b0e;color:#c9d1e3;font:14px/1.5 -apple-system,"Segoe UI","Noto Sans","Microsoft YaHei",sans-serif}
#bar{position:fixed;top:0;left:0;right:0;height:44px;background:rgba(15,17,21,.94);
 border-bottom:1px solid #1e222d;display:flex;align-items:center;
 gap:12px;padding:0 14px;z-index:9;font-size:13px}
#bar a{color:#9aa3b8;text-decoration:none}
#bar .sp{flex:1}
#bar b{color:#e6e8ee;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:55vw}
#wrap{padding-top:44px}
#wrap img{display:block;width:100%;max-width:900px;margin:0 auto;background:#15171c}
#status{text-align:center;color:#6b7488;padding:20px}
</style></head><body>
<div id="bar">
  <a href="javascript:history.back()">←</a>
  <b id="title">…</b>
  <div class="sp"></div>
  <span id="cnt"></span>
</div>
<div id="wrap"></div>
<div id="status"></div>
<script>
const $ = s => document.querySelector(s);
const ID = location.pathname.split('/').filter(Boolean).pop();
let PREFIX = (window.BACKEND || "");
while (PREFIX.endsWith("/")) PREFIX = PREFIX.slice(0, -1);
const APP = (() => {
  let q = location.pathname;
  const i = q.indexOf("/read/");
  if (i >= 0) q = q.slice(0, i);
  while (q.endsWith("/")) q = q.slice(0, -1);
  return q;
})();
const u = p => {
  p = String(p);
  while (p.startsWith("/")) p = p.slice(1);
  return PREFIX ? PREFIX + "/" + p : APP + "/" + p;
};
let rendered = 0, total = 0, done = false;

async function meta(){
  try{ return await (await fetch(u('api/jobs/' + ID))).json(); } catch(e){ return null; }
}
async function count(){
  const d = await (await fetch(u('api/jobs/' + ID + '/pages'))).json();
  return d.count || 0;
}
function addPage(n){
  const img = document.createElement('img');
  img.loading = 'lazy';
  img.src = u('pages/' + ID + '/' + n);
  img.alt = 'page ' + n;
  $('#wrap').appendChild(img);
  rendered = n;
}
async function tick(){
  const d = await meta();
  if (!d) return;
  $('#title').textContent = d.title || d.input || ID;
  total = await count();
  while (rendered < total){ addPage(rendered + 1); }
  $('#cnt').textContent = total ? (rendered + ' / ' + total) : '';
  if (d.status === 'running' || d.status === 'queued'){
    $('#status').textContent = d.message || '…';
  } else if (d.status === 'error'){
    $('#status').textContent = d.message || '';
    done = true;
  } else if (total > 0 && rendered >= total){
    $('#status').textContent = total + ' pages';
    done = true;
  }
}
(async () => {
  await tick();
  const h = setInterval(async () => { if (done){ clearInterval(h); return; } await tick(); }, 2500);
})();
window.addEventListener('scroll', () => {
  if (window.innerHeight + window.scrollY > document.body.offsetHeight - 1200 && !done){ tick(); }
});
</script></body></html>"""


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index():
    return PAGE


@app.get("/favicon.ico")
def favicon():
    return Response(content=FAVICON, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=604800"})


@app.get("/read/{job_id}", response_class=HTMLResponse)
def reader(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found or expired")
    return READER


@app.get("/api/jobs")
def list_jobs():
    with _lock:
        items = sorted(_jobs.values(), key=lambda j: j.get("created", 0), reverse=True)[:40]
        out = [{"id": j["id"], "platform": j.get("platform", "eh"), "input": j.get("input"),
                "title": j.get("title"), "status": j["status"], "message": j.get("message"),
                "progress": j.get("progress", 0), "created": j.get("created"),
                "pdf": j.get("pdf"), "size": j.get("size", 0),
                "readable": bool(j.get("readable"))} for j in items]
    return {"jobs": out}


@app.post("/api/jobs")
async def create_job(request: Request):
    body = await request.json()
    try:
        platform, val = parse_input(body.get("url", ""))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    key = f"{platform}:{val}"
    with _lock:
        for j in _jobs.values():
            if f"{j.get('platform','eh')}:{j.get('input')}" == key and \
                    j["status"] in ("queued", "running", "done"):
                return {"id": j["id"], "reused": True, "title": j.get("title")}

    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "platform": platform, "input": val, "title": None,
           "pages": None, "status": "queued", "created": time.time(),
           "progress": 0, "files": 0, "readable": False,
           "original": bool(body.get("original")), "message": "Queued"}

    with _lock:
        _jobs[job_id] = job
    save_state()
    enqueue(job_id)
    return {"id": job_id, "title": job.get("title"), "pages": job.get("pages"),
            "platform": platform}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    j = _jobs.get(job_id)
    if not j:
        raise HTTPException(404)
    j["cancel"] = True
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    j = _jobs.get(job_id)
    if not j:
        raise HTTPException(404)
    return j


@app.get("/api/jobs/{job_id}/pages")
def job_pages(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found or expired")
    return {"count": len(job_images(job_id, ttl=0.8))}


@app.get("/pages/{job_id}/{n}")
def serve_page(job_id: str, n: int):
    imgs = job_images(job_id)
    if n < 1 or n > len(imgs):
        raise HTTPException(404)
    p = imgs[n - 1]
    etag = f'"{job_id}-{n}-{int(p.stat().st_mtime)}"'
    return FileResponse(str(p), headers={"Cache-Control": "public, max-age=86400",
                                         "ETag": etag})


@app.get("/dl/{job_id}")
def download(job_id: str):
    p = OUT_DIR / f"{job_id}.pdf"
    if not p.exists():
        raise HTTPException(404, "PDF not found or expired")
    j = _jobs.get(job_id, {})
    name = re.sub(r'[\\/:*?"<>|]+', "_", (j.get("title") or job_id))[:80] or job_id
    return FileResponse(str(p), media_type="application/pdf", filename=f"{name}.pdf")


@app.get("/api/search")
def search(platform: str = "jm", q: str = "", by: str = "title", page: int = 1):
    q = (q or "").strip()
    if platform not in ("eh", "jm", "pixiv"):
        return {"error": f"Unsupported platform: {platform}"}
    if not q:
        return {"error": "Please enter a keyword"}
    try:
        if platform == "eh":
            results = eh_search(q, by, page)
        elif platform == "pixiv":
            results = pixiv_search(q, by, page)
        else:
            if not JM_OK:
                return {"error": "jmcomic is not installed on the server"}
            results = jm_search(q, by, page)
        return {"results": results, "platform": platform, "page": page}
    except Exception as e:
        return {"error": f"Search failed: {str(e)[:200]}"}


@app.get("/api/status")
def status():
    du = shutil.disk_usage("/")
    used_pct = int(du.used * 100 / du.total)
    size = data_size()
    warns = []
    if used_pct >= DISK_CRIT_PCT:
        warns.append(f"Disk usage {used_pct}% exceeds the critical threshold {DISK_CRIT_PCT}%")
    elif used_pct >= DISK_WARN_PCT:
        warns.append(f"Disk usage {used_pct}% exceeds the warning threshold {DISK_WARN_PCT}%")
    if size >= MAX_TOTAL_BYTES:
        warns.append(f"Cache {size // 1048576} MB hit the {MAX_TOTAL_BYTES // 1048576} MB limit, "
                     f"auto-cleanup in progress, oldest first")
    with _lock:
        counts = {}
        for j in _jobs.values():
            counts[j["status"]] = counts.get(j["status"], 0) + 1
    return {
        "disk": {"total": du.total, "used": du.used, "free": du.free, "used_pct": used_pct},
        "data": {"bytes": size, "mb": size // 1048576,
                 "limit_mb": MAX_TOTAL_BYTES // 1048576},
        "jobs": {"total": len(_jobs), "limit": MAX_JOBS, "by_status": counts},
        "limits": {"keep_days": KEEP_DAYS, "max_jobs": MAX_JOBS,
                   "max_gb": MAX_TOTAL_BYTES / 1024 ** 3,
                   "disk_warn_pct": DISK_WARN_PCT, "disk_crit_pct": DISK_CRIT_PCT},
        "warnings": warns,
    }


@app.post("/api/cleanup")
def manual_cleanup():
    removed = cleanup_storage()
    return {"removed": len(removed), "data_mb": data_size() // 1048576}


@app.get("/api/cookies")
def cookie_state():
    return {"configured": COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 0}


@app.post("/api/cookies")
async def save_cookies(request: Request):
    b = await request.json()
    rows = ["# Netscape HTTP Cookie File"]

    def add(domain, name, value):
        if value:
            rows.append("\t".join([domain, "FALSE", "/", "FALSE", "0", name, value.strip()]))

    for dom in (".e-hentai.org", ".exhentai.org"):
        add(dom, "ipb_member_id", b.get("ipb_member_id", ""))
        add(dom, "ipb_pass_hash", b.get("ipb_pass_hash", ""))
    add(".exhentai.org", "igneous", b.get("igneous", ""))
    if len(rows) <= 1:
        return {"ok": False, "error": "ipb_member_id and ipb_pass_hash are required"}
    COOKIE_FILE.write_text("\n".join(rows) + "\n", "utf-8")
    os.chmod(COOKIE_FILE, 0o600)
    return {"ok": True}


def _warmup():
    """Warm up the JM client in the background so the first search/submit does not wait for a domain refresh."""
    try:
        jm_client()
    except Exception:
        pass


def bg():
    load_state()
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=_warmup, daemon=True).start()
    cleanup_storage()
    with _lock:
        for jid, j in _jobs.items():
            if j.get("status") == "queued":
                enqueue(jid)


bg()
