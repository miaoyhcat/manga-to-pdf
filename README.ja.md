# manga-to-pdf

### [English](README.md)  ·  [简体中文](README.zh-CN.md)  ·  **日本語**  ·  [한국어](README.ko.md)

> ## ⚠️ 使用前に必ずお読みください
>
> **個人利用のみ・非商用。** 自分用の小さなサーバーを動かすための趣味のツールです。
> サービスではなく、公開インスタンスの運営や推奨は行っていません。
>
> **公式のリリースを応援してください。** 作品を購入し、利用するサイトに課金し、規約を守ってください。
> 本ツールは課金・DRM・購読・地域制限を回避**しません**。有料・制限付きの作品は公式アプリをご利用ください。
>
> **未完成です。** ワーカーは 1 本、リトライはベストエフォート、UI は 4 言語のみ、テストは手動。
> サイト側の変更ですぐ壊れます。
>
> **一部の機能は公式のみです。** 読書進捗の同期、コメント、更新通知、アカウント連携の
> お気に入り、オフライン書庫などはありません。必要なら公式アプリを使ってください。
>
> **自分でホストしてください。** 公開の目的は自分のコピーを動かすことです。
> 既存のデプロイを公共サービスとして扱わないでください。詳細は [NOTICE.md](NOTICE.md)。

ギャラリーのリンク（または ID）から **PDF** を作り、**ブラウザ上でそのまま読める** セルフホスト型の
ウェブアプリです。**E-Hentai / ExHentai**、**JM（18comic）**、**Pixiv** に対応しています。

> 言語: [English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

> **個人利用のみ。** 本プロジェクトは公開されているダウンローダーに Web UI を付けただけのものです。
> 取得したコンテンツの著作権は各作者に帰属します。各サイトの利用規約とお住まいの地域の法律を守り、
> 再配布はしないでください。

## 機能

| | |
|---|---|
| **ソース** | E-Hentai / ExHentai、JM（18comic）、Pixiv（作品・ユーザーページ） |
| **検索** | **統合検索** —— 1 回の入力で 3 サイトを横断、重複を統合し **関連度順** に並べ替え。単独サイト検索・ページ送りも可 |
| **閲覧** | ブラウザ内でページ閲覧。ダウンロード中でも先に読める |
| **出力** | `img2pdf` で PDF 化（JPEG/PNG は無劣化） |
| **保存** | 「保持日数 → 件数 → 合計サイズ」の順で古い順に自動削除 |
| **アラート** | `/api/status` と、閾値超過時のメール通知（任意） |
| **多言語** | UI は英語（既定）/ 中文 / 日本語。実行中に切替可能 |
| **耐障害性** | E-Hentai で出口 IP が制限されたらプロキシノードを自動で切替 |

## 構成

```
ブラウザ ──HTTP──> nginx ──> FastAPI (127.0.0.1:8099)
                               │
             ┌─────────────────┼──────────────────┐
       gallery-dl            jmcomic           Pixiv AJAX
       (E-Hentai)              (JM)             (ログイン不要)
             │                   │                   │
         HTTP プロキシ          直接             HTTP プロキシ
             └───────────────────┴──── img2pdf ────┘
```

- **Pixiv** は公開 `/ajax/*` エンドポイントを使うため、公開作品なら**ログイン不要**です。
  既定は 1200px、原寸が必要なら「原寸画像を取得」にチェック。R-18 は `PHPSESSID` が必要です。
- **E-Hentai** はサーバーから到達できない地域ではプロキシが必要です。

## クイックスタート

```bash
sudo mkdir -p /opt/manga && sudo chown "$USER" /opt/manga
cd /opt/manga
python3 -m venv venv && ./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt

sudo cp deploy/manga.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now manga
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8099/
```

nginx の設定例は `deploy/nginx-manga.conf`。`http://<host>/manga/` で開けます。

## 設定（環境変数）

| 変数 | 既定値 | 説明 |
|---|---|---|
| `MANGA_HOME` | `/opt/manga` | データと設定のルート |
| `MANGA_PROXY` | `http://127.0.0.1:7890` | E-Hentai / Pixiv 用プロキシ（空なら直接） |
| `MANGA_KEEP_DAYS` | `5` | 完了タスクの保持日数 |
| `MANGA_MAX_JOBS` | `150` | 保持するタスク数の上限 |
| `MANGA_MAX_GB` | `12` | キャッシュの合計上限 |
| `MANGA_DISK_WARN` / `CRIT` | `85` / `93` | ディスク使用率の閾値（%） |

上限を超えた場合は**上限の 85%** まで削除します。

## 入力形式

ギャラリー URL、`jm1454202`、`1454202`、`pixiv151885064`、`px151885064`、
`https://www.pixiv.net/users/12345`。

## 開発時のチェック

```bash
python -m py_compile app/main.py
python scripts/check_frontend.py app/main.py   # インライン JS を node --check
```

2 つ目は重要です。アプリはサブパスに配置されるため、インラインスクリプトの括弧が 1 つずれると
「開くけど何も動かない」状態になり、Python 側では検出できません。

## ライセンス

MIT — [LICENSE](LICENSE) を参照。
