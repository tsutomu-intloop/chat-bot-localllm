#!/usr/bin/env bash
# Terraform でインフラを構築し、アプリをビルドしてデプロイする.
#   1. API 有効化 + Artifact Registry を作成 (イメージ push 先を先に用意)
#   2. Cloud Build でイメージをビルド
#   3. 全リソースを apply (Firestore / Secret / IAM / Cloud Run)
# 使い方: infra/terraform/terraform.tfvars を用意したうえで infra/scripts/deploy.sh
set -euo pipefail

TF_DIR="$(cd "$(dirname "$0")/../terraform" && pwd)"
tfvar() { sed -nE "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"([^\"]*)\".*/\1/p" "$TF_DIR/terraform.tfvars" | head -1; }

[[ -f "$TF_DIR/terraform.tfvars" ]] || { echo "terraform.tfvars がありません (terraform.tfvars.example をコピーしてください)" >&2; exit 1; }
PROJECT_ID="${PROJECT_ID:-$(tfvar project_id)}"
REGION="${REGION:-$(tfvar region)}"; REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-$(tfvar service_name)}"; SERVICE="${SERVICE:-chat-bot}"

terraform -chdir="$TF_DIR" init -input=false

# 1. イメージの push 先を先に作成 (image は未確定のためダミー値)
terraform -chdir="$TF_DIR" apply -input=false \
  -target=google_project_service.services \
  -target=google_artifact_registry_repository.app \
  -var "image=bootstrap"

# 2. ビルド
IMAGE="$(PROJECT_ID="$PROJECT_ID" REGION="$REGION" SERVICE="$SERVICE" "$(dirname "$0")/build.sh")"
echo "image: $IMAGE"

# 3. 全体を適用
terraform -chdir="$TF_DIR" apply -input=false -var "image=$IMAGE"
terraform -chdir="$TF_DIR" output service_url
