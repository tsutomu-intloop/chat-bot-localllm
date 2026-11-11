locals {
  llm_region         = coalesce(var.llm_service_region, var.region)
  firestore_location = coalesce(var.firestore_location, var.region)
  llm_base_url       = coalesce(var.llm_base_url, data.google_cloud_run_v2_service.llm.uri)
  use_llm_api_key    = nonsensitive(var.llm_api_key != null && var.llm_api_key != "")

  env = merge(
    {
      LLM_BASE_URL       = local.llm_base_url
      LLM_API_TYPE       = var.llm_api_type
      LLM_MODEL          = var.llm_model
      LLM_USE_ID_TOKEN   = tostring(var.llm_use_id_token)
      LLM_API_KEY_HEADER = var.llm_api_key_header
      FIRESTORE_PROJECT  = var.project_id
      FIRESTORE_DATABASE = var.firestore_database
      AUTH_MODE          = var.auth_mode
      ALLOW_SIGNUP       = tostring(var.allow_signup)
      COOKIE_SECURE      = "true"
    },
    var.system_prompt == null ? {} : { SYSTEM_PROMPT = var.system_prompt },
  )
}

# ---------- API の有効化 ----------
resource "google_project_service" "services" {
  for_each = toset([
    "run.googleapis.com",
    "firestore.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "iam.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# API 有効化直後は反映まで数分かかることがあり、直後のリソース作成が
# "API has not been used in project ... or it is disabled" (403) で失敗するため待機する
resource "time_sleep" "apis_ready" {
  create_duration = var.api_propagation_wait
  triggers = {
    services = join(",", sort([for s in google_project_service.services : s.service]))
  }
}

# ---------- 既存の Local LLM (Cloud Run) ----------
data "google_cloud_run_v2_service" "llm" {
  name     = var.llm_service_name
  location = local.llm_region
}

# ---------- Artifact Registry ----------
resource "google_artifact_registry_repository" "app" {
  repository_id = var.service_name
  location      = var.region
  format        = "DOCKER"
  description   = "chat-bot-localllm のコンテナイメージ"

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  depends_on = [time_sleep.apis_ready]
}

# ---------- サービスアカウント ----------
resource "google_service_account" "app" {
  account_id   = "${var.service_name}-sa"
  display_name = "Chat bot (Cloud Run)"
  depends_on   = [time_sleep.apis_ready]
}

# Firestore の読み書き
resource "google_project_iam_member" "app_datastore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = google_service_account.app.member
}

# Local LLM の呼び出し (LLM 側は認証必須のまま非公開にできる)
resource "google_cloud_run_v2_service_iam_member" "llm_invoker" {
  name     = data.google_cloud_run_v2_service.llm.name
  location = local.llm_region
  role     = "roles/run.invoker"
  member   = google_service_account.app.member
}

# ---------- Firestore ----------
resource "google_firestore_database" "chat" {
  count = var.create_firestore_database ? 1 : 0

  name                    = var.firestore_database
  location_id             = local.firestore_location
  type                    = "FIRESTORE_NATIVE"
  delete_protection_state = var.deletion_protection ? "DELETE_PROTECTION_ENABLED" : "DELETE_PROTECTION_DISABLED"
  deletion_policy         = var.deletion_protection ? "ABANDON" : "DELETE"

  depends_on = [time_sleep.apis_ready]
}

# ---------- セッション署名鍵 (Secret Manager) ----------
# 注意: 生成値は Terraform state にも保存されるため、state は GCS 等の保護された場所で管理すること
resource "random_password" "session_secret" {
  length  = 64
  special = false
}

resource "google_secret_manager_secret" "session_secret" {
  secret_id = "${var.service_name}-session-secret"
  replication {
    auto {}
  }
  depends_on = [time_sleep.apis_ready]
}

resource "google_secret_manager_secret_version" "session_secret" {
  secret      = google_secret_manager_secret.session_secret.id
  secret_data = random_password.session_secret.result
}

resource "google_secret_manager_secret_iam_member" "app_session_secret" {
  secret_id = google_secret_manager_secret.session_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.app.member
}

# ---------- LLM の API キー (Secret Manager, 必要な場合のみ) ----------
resource "google_secret_manager_secret" "llm_api_key" {
  count     = local.use_llm_api_key ? 1 : 0
  secret_id = "${var.service_name}-llm-api-key"
  replication {
    auto {}
  }
  depends_on = [time_sleep.apis_ready]
}

resource "google_secret_manager_secret_version" "llm_api_key" {
  count       = local.use_llm_api_key ? 1 : 0
  secret      = google_secret_manager_secret.llm_api_key[0].id
  secret_data = var.llm_api_key
}

resource "google_secret_manager_secret_iam_member" "app_llm_api_key" {
  count     = local.use_llm_api_key ? 1 : 0
  secret_id = google_secret_manager_secret.llm_api_key[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.app.member
}

# ---------- チャットボット (Cloud Run) ----------
resource "google_cloud_run_v2_service" "app" {
  name                = var.service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection

  template {
    service_account = google_service_account.app.email
    # LLM の応答ストリーミングが長時間になる場合に備えて最大値に
    timeout = "3600s"

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = var.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        cpu_idle = true
      }

      dynamic "env" {
        for_each = local.env
        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name = "SESSION_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.session_secret.secret_id
            version = "latest"
          }
        }
      }

      dynamic "env" {
        for_each = local.use_llm_api_key ? [1] : []
        content {
          name = "LLM_API_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.llm_api_key[0].secret_id
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
      }
      liveness_probe {
        http_get {
          path = "/healthz"
        }
      }
    }
  }

  depends_on = [
    time_sleep.apis_ready,
    google_secret_manager_secret_version.session_secret,
    google_secret_manager_secret_iam_member.app_session_secret,
    google_secret_manager_secret_version.llm_api_key,
    google_secret_manager_secret_iam_member.app_llm_api_key,
    google_project_iam_member.app_datastore,
    google_firestore_database.chat,
  ]
}

# 公開設定 (アプリ側でログインを要求)
resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = var.allow_unauthenticated ? 1 : 0
  name     = google_cloud_run_v2_service.app.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "invokers" {
  for_each = var.allow_unauthenticated ? toset([]) : toset(var.invoker_members)
  name     = google_cloud_run_v2_service.app.name
  location = var.region
  role     = "roles/run.invoker"
  member   = each.value
}
