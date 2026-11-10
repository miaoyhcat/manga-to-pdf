# manga-to-pdf

### [English](README.md)  ·  [简体中文](README.zh-CN.md)  ·  [日本語](README.ja.md)  ·  **한국어**

갤러리 링크(또는 ID)를 **PDF**로 만들고, **브라우저에서 바로 읽을 수 있게** 해 주는
셀프 호스팅 웹 앱입니다. **E-Hentai / ExHentai**, **JM (18comic)**, **Pixiv** 를 지원합니다.

> ## ⚠️ 사용 전에 꼭 읽어 주세요
>
> **개인용 · 비상업적.** **본인**의 작은 서버를 돌리기 위한 취미 프로젝트입니다.
> 서비스가 아니며, 공개 인스턴스를 운영하거나 권장하지 않습니다.
>
> **정식 발매를 응원해 주세요.** 책을 구매하고, 이용하는 사이트를 구독하고, 약관을 지켜 주세요.
> 이 도구는 유료 결제·DRM·구독·지역 제한을 **우회하지 않습니다**. 유료이거나 제한된 작품은
> 공식 플랫폼을 이용하세요.
>
> **미완성입니다.** 워커 1개, 최선 노력(best-effort) 재시도, UI 네 개 언어, 수동 검증.
> 사이트가 바뀌면 바로 깨집니다.
>
> **일부 기능은 공식에만 있습니다.** 진행률 동기화, 댓글, 업데이트 알림, 계정 즐겨찾기,
> 오프라인 서재 — 이런 것은 없습니다. 필요하면 공식 앱을 쓰세요.
>
> **직접 배포하세요, 남의 서버를 붐비게 하지 마세요.** 공개 목적은 본인 사본을 돌리는 것입니다.
> 이미 있는 배포를 공공 서비스처럼 취급하지 마세요.
>
> 전문은 [NOTICE.md](NOTICE.md) 를 보세요.


## 기능

| | |
|---|---|
| **소스** | E-Hentai / ExHentai, JM (18comic), Pixiv (작품 · 사용자 페이지) |
| **검색** | **통합 검색** — 한 번의 입력으로 세 사이트를 모두 조회, 중복 병합 후 **관련도 순 정렬**. 개별 사이트 검색·페이지 넘김도 지원 |
| **리더** | 브라우저에서 페이지 보기, 다운로드 중에도 읽기 가능 |
| **출력** | `img2pdf` 로 PDF 생성 (JPEG/PNG 무손실) |
| **저장** | 보관 기간 → 작업 수 → 총 용량 순으로 오래된 것부터 자동 정리 |
| **알림** | `/api/status` + 디스크/캐시 임계값 초과 시 이메일(선택) |
| **다국어** | 영어(기본) / 中文 / 日本語 / 한국어, 실행 중 전환 가능 |
| **복원력** | E-Hentai 출구 IP가 차단되면 프록시 노드를 자동 전환 |

## 구조

```
브라우저 ──HTTP──> nginx ──> FastAPI (127.0.0.1:8099)
                                │
             ┌──────────────────┼───────────────────┐
       gallery-dl            jmcomic            Pixiv AJAX
       (E-Hentai)              (JM)              (로그인 불필요)
             │                  │                    │
          HTTP 프록시           직접              HTTP 프록시
             └──────────────────┴────── img2pdf ────┘
```

- **Pixiv** 는 공개 `/ajax/*` 엔드포인트를 쓰므로 공개 작품은 **로그인 없이** 받을 수 있습니다.
  기본은 1200px, 원본이 필요하면 체크박스를 켜세요. R-18 은 `PHPSESSID` 가 필요합니다.
- **E-Hentai** 는 서버가 직접 닿지 않는 지역이면 프록시가 필요합니다.

## 빠른 시작

```bash
sudo mkdir -p /opt/manga && sudo chown "$USER" /opt/manga
cd /opt/manga
python3 -m venv venv && ./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt

sudo cp deploy/manga.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now manga
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8099/
```

nginx 예시는 `deploy/nginx-manga.conf` 입니다. `http://<host>/manga/` 로 접속하세요.

## 설정 (환경 변수)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `MANGA_HOME` | `/opt/manga` | 데이터·설정 루트 |
| `MANGA_PROXY` | `http://127.0.0.1:7890` | E-Hentai / Pixiv 용 프록시, 비우면 직접 |
| `MANGA_KEEP_DAYS` | `5` | 완료 작업 보관 일수 |
| `MANGA_MAX_JOBS` | `150` | 최대 작업 수 |
| `MANGA_MAX_GB` | `12` | 캐시 총량 상한 |
| `MANGA_DISK_WARN` / `CRIT` | `85` / `93` | 디스크 경고 임계값(%) |

상한을 넘으면 **상한의 85%** 까지 정리합니다.

## 입력 형식

갤러리 URL, `jm1454202`, `1454202`, `pixiv151885064`, `px151885064`,
`https://www.pixiv.net/users/12345`.

## 개발 시 확인

```bash
python -m py_compile app/main.py
python scripts/check_frontend.py app/main.py   # 인라인 JS 를 node --check 로 검증
```

두 번째가 중요합니다. 앱은 서브 경로에 배치되므로 인라인 스크립트의 괄호 하나가 어긋나면
"열리지만 아무것도 동작하지 않는" 상태가 되고, Python 쪽에서는 알 수 없습니다.

## 라이선스

MIT — [LICENSE](LICENSE) 참고.
