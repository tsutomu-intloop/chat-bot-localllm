output "service_url" {
  description = "チャットボットの URL"
  value       = google_cloud_run_v2_service.app.uri
}

output "service_account_email" {
  value = google_service_account.app.email
}

output "artifact_registry_repository" {
  description = "イメージの push 先"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.app.repository_id}"
}

output "llm_base_url" {
  value = local.llm_base_url
}
