variable "project_id" {
  description = "GCP プロジェクト ID"
  type        = string
}

variable "region" {
  description = "Cloud Run / Artifact Registry / Firestore のリージョン"
  type        = string
  default     = "asia-northeast1"
}

variable "service_name" {
  description = "チャットボットの Cloud Run サービス名"
  type        = string
  default     = "chat-bot"
}

variable "image" {
  description = "デプロイするコンテナイメージ (infra/scripts/build.sh の出力). 例: asia-northeast1-docker.pkg.dev/PROJECT/chat-bot/chat-bot:20261110-abc123"
  type        = string
}

# ---------- Local LLM ----------
variable "llm_service_name" {
  description = "既存の Local LLM (Ollama 等) の Cloud Run サービス名. チャットボットに呼び出し権限 (run.invoker) を付与する"
  type        = string
  default     = "ollama"
}

variable "llm_service_region" {
  description = "Local LLM の Cloud Run リージョン (未指定時は var.region)"
  type        = string
  default     = null
}

variable "llm_base_url" {
  description = "LLM の URL を明示する場合に指定 (未指定時は llm_service_name の URL を自動取得)"
  type        = string
  default     = null
}

variable "llm_api_type" {
  description = "ollama または openai"
  type        = string
  default     = "ollama"

  validation {
    condition     = contains(["ollama", "openai"], var.llm_api_type)
    error_message = "llm_api_type は ollama または openai を指定してください。"
  }
}

variable "llm_model" {
  description = "既定のモデル名"
  type        = string
  default     = "gemma3:4b"
}

variable "system_prompt" {
  description = "既定のシステムプロンプト (null の場合アプリの既定値)"
  type        = string
  default     = null
}

# ---------- Firestore ----------
variable "firestore_database" {
  description = "Firestore データベース ID"
  type        = string
  default     = "(default)"
}

variable "create_firestore_database" {
  description = "Firestore データベースを Terraform で作成するか (既に存在する場合は false にするか terraform import する)"
  type        = bool
  default     = true
}

variable "firestore_location" {
  description = "Firestore のロケーション (未指定時は var.region)"
  type        = string
  default     = null
}

# ---------- 認証 / 公開範囲 ----------
variable "auth_mode" {
  description = "local (ユーザ名/パスワード) または iap"
  type        = string
  default     = "local"

  validation {
    condition     = contains(["local", "iap"], var.auth_mode)
    error_message = "auth_mode は local または iap を指定してください。"
  }
}

variable "allow_signup" {
  description = "画面からのユーザ新規登録を許可するか"
  type        = bool
  default     = true
}

variable "allow_unauthenticated" {
  description = "チャットボットを allUsers に公開するか (アプリ側でログインを要求). false の場合は invoker_members のみアクセス可"
  type        = bool
  default     = true
}

variable "invoker_members" {
  description = "allow_unauthenticated=false の場合に Cloud Run へのアクセスを許可するメンバー (例: [\"group:team@example.com\"])"
  type        = list(string)
  default     = []
}

# ---------- スケーリング ----------
variable "min_instances" {
  type    = number
  default = 0
}

variable "max_instances" {
  type    = number
  default = 5
}

variable "cpu" {
  type    = string
  default = "1"
}

variable "memory" {
  type    = string
  default = "512Mi"
}

variable "deletion_protection" {
  description = "Cloud Run サービスと Firestore データベースの削除保護"
  type        = bool
  default     = true
}

variable "api_propagation_wait" {
  description = "API 有効化後、反映を待つ時間 (初回 apply で 403 が出る場合は延ばす)"
  type        = string
  default     = "90s"
}

variable "llm_use_id_token" {
  description = "LLM の Cloud Run が IAM 認証必須の場合 true (ID トークンを X-Serverless-Authorization で送る)"
  type        = bool
  default     = true
}

variable "llm_api_key" {
  description = "LLM サーバ (または前段のプロキシ) が要求する API キー. Secret Manager に保存して Cloud Run に渡す. 不要なら null"
  type        = string
  default     = null
  sensitive   = true
}

variable "llm_api_key_header" {
  description = "API キーを送るヘッダ名. Authorization の場合は Bearer 形式、それ以外 (X-API-Key 等) はキーをそのまま送る"
  type        = string
  default     = "Authorization"
}
