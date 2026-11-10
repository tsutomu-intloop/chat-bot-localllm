terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0, < 9.0"
    }
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5"
    }
  }

  # チームで運用する場合は GCS バックエンドで state を共有してください (backend.tf.example 参照)
}

provider "google" {
  project = var.project_id
  region  = var.region
}
