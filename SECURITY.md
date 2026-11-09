# Security policy

## What this project stores

- `config/cookies.txt` — E-Hentai login cookies (`ipb_member_id`, `ipb_pass_hash`, `igneous`)
- `config/pixiv_cookies.txt` — optional Pixiv `PHPSESSID`
- `alert.env` — SMTP credentials used only for sending threshold alerts

All three are git-ignored. Keep them at mode `600` and owned by the service user.

## Deployment notes

- The app binds `127.0.0.1` only; put a reverse proxy in front and consider HTTP basic auth or a
  VPN. There is **no built-in authentication** — anyone who can reach the port can start jobs.
- Do not expose `MIHOMO_API` (`9090`) publicly: it controls your proxy.
- If you enable the e-mail alerting, use a dedicated app password rather than your account
  password, and restrict the file to root.

## Reporting a vulnerability

Open a private security advisory on the repository, or e-mail the maintainer. Please do not file a
public issue for anything that could expose credentials or allow remote job execution.
