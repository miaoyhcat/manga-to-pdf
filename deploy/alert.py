#!/usr/bin/env python3
"""Storage / disk alert: query the app status endpoint and email when a
threshold is crossed (at most once per 6 hours).

Credentials live in /opt/manga/alert.env (chmod 600, root):
    SMTP_HOST=smtp.qq.com
    SMTP_PORT=465
    SMTP_USER=xxx@qq.com
    SMTP_PASS=授权码
    ALERT_TO=you@example.com
    SITE_URL=https://manga.example.com/manga/
"""
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

ENV = Path("/opt/manga/alert.env")
STATE = Path("/opt/manga/data/.last_alert")
STATUS_URL = "http://127.0.0.1:8099/api/status"
COOLDOWN = 6 * 3600


def load_env():
    cfg = {}
    if ENV.exists():
        for line in ENV.read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def fetch_status():
    with urllib.request.urlopen(STATUS_URL, timeout=15) as r:
        return json.load(r)


def send_mail(cfg, subject, body):
    host = cfg.get("SMTP_HOST", "smtp.qq.com")
    port = int(cfg.get("SMTP_PORT", "465"))
    user = cfg.get("SMTP_USER", "")
    pwd = cfg.get("SMTP_PASS", "")
    to = cfg.get("ALERT_TO", "")
    if not (user and pwd and to):
        print("SMTP not configured, skipping mail")
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=25) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())
    return True


def main():
    cfg = load_env()
    try:
        st = fetch_status()
    except Exception as e:
        print("status fetch failed:", e)
        return 1

    warns = st.get("warnings") or []
    disk = st.get("disk", {})
    data = st.get("data", {})
    print(f"磁盘 {disk.get('used_pct')}% · 缓存 {data.get('mb')}MB · 警告 {len(warns)}")

    if not warns:
        return 0

    now = time.time()
    if STATE.exists():
        try:
            if now - float(STATE.read_text().strip()) < COOLDOWN:
                print("in cooldown, skipping")
                return 0
        except Exception:
            pass

    body = "\n".join([
        "Manga site storage alert",
        "",
        *[f"- {w}" for w in warns],
        "",
        f"Disk: {disk.get('used') // 1048576} MB / {disk.get('total') // 1048576} MB"
        f" ({disk.get('used_pct')}%)",
        f"Cache: {data.get('mb')} MB (limit {data.get('limit_mb')} MB)",
        f"Free disk: {disk.get('free') // 1048576} MB",
        "",
        "Site: " + cfg.get("SITE_URL", "(not configured)"),
        "Oldest downloads are removed automatically when the size cap is hit; disk alerts need manual action.",
    ])
    try:
        if send_mail(cfg, "Manga site alert: " + warns[0][:40], body):
            STATE.write_text(str(int(now)))
            print("alert mail sent")
    except Exception as e:
        print("mail send failed:", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
