#!/usr/bin/env bash
# Cloud Build でコンテナをビルドし Artifact Registry に push する.
# 使い方: PROJECT_ID=... [REGION=asia-northeast1] [SERVICE=chat-bot] infra/scripts/build.sh
# 出力されたイメージ名を terraform の var.image に渡す.
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID を設定してください}"
REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-chat-bot}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TAG="${TAG:-$(date +%Y%m%d-%H%M%S)-$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo local)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${SERVICE}/${SERVICE}:${TAG}"

gcloud builds submit "$ROOT" --project "$PROJECT_ID" --region "$REGION" --tag "$IMAGE" >&2
echo "$IMAGE"
