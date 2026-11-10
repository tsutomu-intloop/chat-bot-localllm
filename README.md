# chat-bot-localllm

Cloud Run 上にデプロイしたローカル LLM（Ollama / vLLM などの OpenAI 互換サーバ）を参照するチャットボットです。
**ユーザごとに会話履歴を保存**し、過去の会話を再開・検索・複製（分岐）して再利用できます。

```
ブラウザ ──> chat-bot (Cloud Run, FastAPI + Web UI) ──(ID トークン)──> Local LLM (Cloud Run: Ollama 等)
                       │
                       └── 会話履歴 DB (SQLite / Cloud SQL PostgreSQL)
```

## 機能

| 機能 | 内容 |
| --- | --- |
| ユーザ管理 | ユーザ名/パスワードでのログイン（`AUTH_MODE=local`）、または IAP の Google アカウント（`AUTH_MODE=iap`） |
| 会話履歴 | ユーザごとに保存。サイドバーに「今日 / 昨日 / 過去7日間…」でグループ表示 |
| 再利用 | 過去の会話を開いてそのまま続きから会話（履歴は LLM に文脈として送信） |
| 検索 | タイトル・本文の全文検索 |
| 複製・分岐 | 会話全体の複製、任意のアシスタント応答の時点から新しい会話として分岐 |
| 編集・再生成 | 送信済みメッセージを編集して再送、最後の応答を再生成 |
| ピン留め・名前変更・削除 | よく使う会話を上部に固定 |
| 会話ごとの設定 | モデル選択・システムプロンプト |
| エクスポート | Markdown / JSON |
| ストリーミング | 応答を逐次表示、途中停止可（停止までの内容も保存） |

## ローカルで動かす

```bash
pip install -r requirements.txt
cp .env.example .env   # LLM_BASE_URL などを編集
```

```bash
set -a && source .env && set +a && uvicorn app.main:app --reload --port 8080
```

http://localhost:8080 を開き、「アカウントを作成する」からユーザを作成してください。

ローカルから IAM 認証付きの Cloud Run の LLM を呼ぶ場合は、`LLM_USE_ID_TOKEN=true` のうえ
サービスアカウントの認証情報が必要です（ユーザ認証情報では ID トークンを発行できないため）。
手軽に試すには Cloud Run プロキシを使い、`LLM_BASE_URL=http://localhost:11434`、`LLM_USE_ID_TOKEN=false` にします:

```bash
gcloud run services proxy ollama --region asia-northeast1 --port 11434
```

## 環境変数

| 変数 | 既定値 | 説明 |
| --- | --- | --- |
| `LLM_BASE_URL` | `http://localhost:11434` | Local LLM の Cloud Run URL |
| `LLM_API_TYPE` | `ollama` | `ollama`（`/api/chat`）または `openai`（`/v1/chat/completions`） |
| `LLM_MODEL` | `gemma3:4b` | 既定モデル。UI ではサーバの一覧から選択可能 |
| `LLM_USE_ID_TOKEN` | `false` | IAM 認証付き Cloud Run を呼ぶ際に ID トークンを付与 |
| `LLM_API_KEY` | - | OpenAI 互換サーバの API キー（必要な場合） |
| `LLM_TIMEOUT` | `300` | LLM 応答のタイムアウト秒 |
| `SYSTEM_PROMPT` | 日本語アシスタント | 既定のシステムプロンプト |
| `MAX_HISTORY_MESSAGES` | `40` | LLM に送る直近の履歴件数 |
| `DATABASE_URL` | `sqlite:///./data/chat.db` | 履歴 DB（SQLAlchemy URL） |
| `AUTH_MODE` | `local` | `local` / `iap` |
| `ALLOW_SIGNUP` | `true` | 画面からの新規登録を許可 |
| `SESSION_SECRET` | ランダム | セッション Cookie 署名鍵。**本番では必ず固定値を設定** |
| `COOKIE_SECURE` | `false` | HTTPS 環境では `true` |

## Cloud Run へのデプロイ

### 1. 履歴 DB（推奨: Cloud SQL for PostgreSQL）

Cloud Run のファイルシステムは揮発性のため、SQLite のままだと再起動で履歴が消えます。本番では Cloud SQL を使ってください。

```bash
gcloud sql instances create chat-bot-db --database-version=POSTGRES_16 --region=asia-northeast1 --tier=db-f1-micro
```

```bash
gcloud sql databases create chatbot --instance=chat-bot-db
```

```bash
gcloud sql users create chatbot --instance=chat-bot-db --password='STRONG_PASSWORD'
```

```bash
printf 'postgresql+psycopg://chatbot:STRONG_PASSWORD@/chatbot?host=/cloudsql/PROJECT_ID:asia-northeast1:chat-bot-db' | gcloud secrets create chat-bot-database-url --data-file=-
```

テーブルは起動時に自動作成されます。デプロイ用サービスアカウントに `roles/cloudsql.client` と
シークレットの `roles/secretmanager.secretAccessor` を付与してください。

### 2. デプロイ

```bash
PROJECT_ID=your-project REGION=asia-northeast1 LLM_SERVICE=ollama CLOUDSQL_INSTANCE=your-project:asia-northeast1:chat-bot-db DATABASE_URL_SECRET=chat-bot-database-url ./deploy.sh
```

`deploy.sh` は以下を行います:

- チャットボット用サービスアカウントを作成し、LLM サービスへの `roles/run.invoker` を付与
  （LLM 側は `--no-allow-unauthenticated` のまま非公開にできます）
- セッション署名鍵を Secret Manager に作成
- `gcloud run deploy --source .` でビルド＆デプロイ（ストリーミングのため `--timeout 3600`）

### 社内向けに Google アカウントで制限したい場合

Cloud Run に IAP を有効化し、`AUTH_MODE=iap` を設定すると、IAP が付与する
`X-Goog-Authenticated-User-Email` ヘッダでユーザを識別し、パスワード管理が不要になります。
（IAP を経由しないアクセスを防ぐため、Ingress を内部 + ロードバランサ、または IAP 必須にしてください。）

## API

| メソッド | パス | 説明 |
| --- | --- | --- |
| POST | `/api/auth/signup` `/api/auth/login` `/api/auth/logout` | 認証 |
| GET | `/api/conversations?q=` | 自分の会話一覧（検索） |
| POST | `/api/conversations` | 会話作成 |
| GET / PATCH / DELETE | `/api/conversations/{id}` | 取得 / 更新（タイトル・ピン・モデル・システムプロンプト） / 削除 |
| POST | `/api/conversations/{id}/messages` | メッセージ送信（SSE ストリーム応答）。`edit_message_id` で編集再送 |
| POST | `/api/conversations/{id}/regenerate` | 最後の応答を再生成（SSE） |
| POST | `/api/conversations/{id}/duplicate` | 複製・分岐（`upto_message_id`） |
| GET | `/api/conversations/{id}/export?format=md\|json` | エクスポート |
| GET | `/api/models` | LLM サーバのモデル一覧 |

## テスト

```bash
pip install -r requirements-dev.txt && pytest -q
```
