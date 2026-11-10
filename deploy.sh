#!/usr/bin/env bash
# チャットボットを Cloud Run にデプロイするスクリプト
# 使い方: PROJECT_ID=... REGION=asia-northeast1 LLM_SERVICE=ollama ./deploy.sh
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID を設定してください}"
REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-chat-bot}"
LLM_SERVICE="${LLM_SERVICE:-ollama}"           # 既存の Local LLM の Cloud Run サービス名
LLM_API_TYPE="${LLM_API_TYPE:-ollama}"
LLM_MODEL="${LLM_MODEL:-gemma3:4b}"
SA_NAME="${SA_NAME:-chat-bot-sa}"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"

LLM_URL="$(gcloud run services describe "$LLM_SERVICE" --region "$REGION" --format='value(status.url)')"
echo "LLM URL: $LLM_URL"

# チャットボット用サービスアカウント (LLM の呼び出し権限のみ付与)
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --display-name "Chat bot"
gcloud run services add-iam-policy-binding "$LLM_SERVICE" --region "$REGION" \
  --member "serviceAccount:${SA}" --role roles/run.invoker

# セッション署名鍵を Secret Manager に保存 (初回のみ)
gcloud secrets describe chat-bot-session-secret >/dev/null 2>&1 || \
  (openssl rand -base64 48 | gcloud secrets create chat-bot-session-secret --data-file=-)
gcloud secrets add-iam-policy-binding chat-bot-session-secret \
  --member "serviceAccount:${SA}" --role roles/secretmanager.secretAccessor >/dev/null

# Firestore (ネイティブモード) を用意し、読み書き権限を付与
FIRESTORE_DATABASE="${FIRESTORE_DATABASE:-(default)}"
gcloud services enable firestore.googleapis.com
gcloud firestore databases describe --database="$FIRESTORE_DATABASE" >/dev/null 2>&1 || \
  gcloud firestore databases create --database="$FIRESTORE_DATABASE" --location="$REGION" --type=firestore-native
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${SA}" --role roles/datastore.user --condition=None >/dev/null

gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --service-account "$SA" \
  --allow-unauthenticated \
  --timeout 3600 \
  --set-secrets "SESSION_SECRET=chat-bot-session-secret:latest" \
  --set-env-vars "^@^FIRESTORE_PROJECT=${PROJECT_ID}@FIRESTORE_DATABASE=${FIRESTORE_DATABASE}@LLM_BASE_URL=${LLM_URL}@LLM_API_TYPE=${LLM_API_TYPE}@LLM_MODEL=${LLM_MODEL}@LLM_USE_ID_TOKEN=true@COOKIE_SECURE=true@AUTH_MODE=${AUTH_MODE:-local}@ALLOW_SIGNUP=${ALLOW_SIGNUP:-true}"
