output "cloud_run_service_url" {
  description = "HTTPS endpoint URL of the deployed Family Ops Concierge PWA and ADK API."
  value       = google_cloud_run_v2_service.family_ops_concierge.uri
}

output "runtime_service_account_email" {
  description = "Least-privilege runtime service account attached to Cloud Run."
  value       = google_service_account.concierge_runtime_sa.email
}

output "artifact_registry_repository" {
  description = "Artifact Registry repository path for pushing agent container builds."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.concierge_repo.repository_id}"
}

output "gemini_secret_resource_name" {
  description = "Google Cloud Secret Manager resource name for the Gemini API key."
  value       = google_secret_manager_secret.gemini_api_key.name
}
