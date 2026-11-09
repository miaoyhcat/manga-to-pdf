# manga-to-pdf

自建的网页工具：把画廊链接（或编号）变成 **PDF**，也能**直接在浏览器里翻页阅读**。
支持 **E-Hentai / ExHentai**、**禁漫 JM (18comic)**、**Pixiv**。

> 英文文档为主：[README.md](README.md)

> **仅供个人使用。** 本项目只是给公开下载器套了个网页界面。抓取的内容版权归原作者，
> 请遵守各站点服务条款与当地法律，不要二次分发。

## 功能

| | |
|---|---|
| **来源** | E-Hentai / ExHentai、禁漫 JM、Pixiv（作品 / 用户主页） |
| **搜索** | 按作品名或作者名，支持翻页，三个来源都支持 |
| **阅读** | 站内翻页阅读，下载过程中就能先看 |
| **输出** | `img2pdf` 合成 PDF（JPEG/PNG 无损） |
| **存储** | 按「保留天数 → 任务数 → 总体积」自动清理，优先删最早的 |
| **预警** | `/api/status` 接口 + 磁盘/缓存超阈值时可选发邮件 |
| **多语言** | 界面英文（默认）/ 中文 / 日文，可运行时切换 |
| **容错** | E-Hentai 出口 IP 被封时自动切换代理节点 |

## 架构

```
浏览器 ──HTTP──> nginx ──> FastAPI (127.0.0.1:8099)
                              │
             ┌────────────────┼──────────────────┐
       gallery-dl           jmcomic           Pixiv AJAX
       (E-Hentai)             (JM)             (免登录)
             │                  │                  │
          HTTP 代理            直连              HTTP 代理
             └──────────────────┴──── img2pdf ────┘
```

- **E-Hentai**：服务器不在可直连区域时需要代理；限流时自动轮换代理组里的出口节点
- **禁漫**：用 [`jmcomic`](https://github.com/hect0x7/JMComic-Crawler-Python)
- **Pixiv**：走公开 `/ajax/*` 接口，**公开作品无需登录**；默认 1200px，勾选「下载原图」取原图；
  R-18 需要 `PHPSESSID`（放 `config/pixiv_cookies.txt`）

## 快速部署

```bash
sudo mkdir -p /opt/manga && sudo chown "$USER" /opt/manga
cd /opt/manga
python3 -m venv venv && ./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt     # 国内可加 -i https://mirrors.aliyun.com/pypi/simple/

# 放好代码后
sudo cp deploy/manga.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now manga
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8099/     # 200
```

nginx 片段见 `deploy/nginx-manga.conf`，然后访问 `http://<主机>/manga/`。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MANGA_HOME` | `/opt/manga` | 数据与配置根目录 |
| `MANGA_PROXY` | `http://127.0.0.1:7890` | E-Hentai / Pixiv 用的代理，留空直连 |
| `MANGA_KEEP_DAYS` | `5` | 完成的任务保留天数 |
| `MANGA_MAX_JOBS` | `150` | 最多保留任务数 |
| `MANGA_MAX_GB` | `12` | 缓存总上限 |
| `MANGA_DISK_WARN` / `CRIT` | `85` / `93` | 磁盘告警阈值（%） |

体积超上限时会清到**上限的 85%**，避免每下载一本就再删一本。

## 支持的输入

画廊链接、`jm1454202`、`1454202`、`pixiv151885064`、`px151885064`、
`https://www.pixiv.net/users/12345`。

## 开发自检

```bash
python -m py_compile app/main.py
python scripts/check_frontend.py app/main.py   # 用 node --check 校验内联 JS
```

第二条很重要：页面挂在子路径下，内联脚本里一个括号错位就会让整页「能打开但什么都不工作」，
Python 侧看不出来。

## 许可

MIT，见 [LICENSE](LICENSE)。
