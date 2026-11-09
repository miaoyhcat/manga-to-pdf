import re
import sys

p = sys.argv[1] if len(sys.argv) > 1 else "/opt/mihomo/config.yaml"
s = open(p, encoding="utf-8").read()

if "- name: EH" in s:
    print("already patched")
    raise SystemExit

# 自动挑选可用节点名（按地区优先级）
prefs = ["台湾", "日本", "新加坡", "香港", "美国", "韩国", "德国"]
names = re.findall(r"^\s*- name: (.+)$", s, re.M)
picked = []
for key in prefs:
    for n in names:
        n = n.strip()
        if key in n and n not in picked and n not in ("EH",):
            picked.append(n)
        if len(picked) >= 6:
            break
    if len(picked) >= 6:
        break
if not picked:
    picked = [n.strip() for n in names if "剩余" not in n][:6]
print("picked:", picked)

m = re.search(r"^proxy-groups:\s*$", s, re.M)
assert m, "no proxy-groups"
grp = "\n- name: EH\n  type: fallback\n  url: https://e-hentai.org/favicon.ico\n  interval: 180\n  proxies:\n"
for n in picked:
    grp += f"    - {n}\n"
s = s[: m.end()] + grp + s[m.end():]

m2 = re.search(r"^rules:\s*$", s, re.M)
assert m2, "no rules"
rules = """
- DOMAIN-SUFFIX,e-hentai.org,EH
- DOMAIN-SUFFIX,exhentai.org,EH
- DOMAIN-SUFFIX,ehgt.org,EH
- DOMAIN-SUFFIX,hath.network,EH
- DOMAIN-SUFFIX,ehtracker.org,EH
"""
s = s[: m2.end()] + rules + s[m2.end():]

# DNS 监听端口改成非特权端口，避免和系统 DNS 冲突
s = s.replace("listen: 0.0.0.0:53", "listen: 127.0.0.1:1053")

open(p, "w", encoding="utf-8").write(s)
print("patched ->", p)
