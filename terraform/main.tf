terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  required_apis = toset([
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "calendar-json.googleapis.com",
    "dlp.googleapis.com",
    "cloudtrace.googleapis.com",
    "logging.googleapis.com",
  ])
}

resource "google_project_service" "enabled_apis" {
  for_each           = local.required_apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "concierge_repo" {
  location      = var.region
  repository_id = var.artifact_repo_name
  description   = "Container images for Family Ops Concierge ADK service"
  format        = "DOCKER"

  depends_on = [google_project_service.enabled_apis]
}

resource "google_service_account" "concierge_runtime_sa" {
  account_id   = "family-ops-concierge-sa"
  display_name = "Family Ops Concierge Least-Privilege Runtime SA"
}

resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = var.gemini_secret_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled_apis]
}

resource "google_secret_manager_secret_iam_member" "concierge_secret_accessor" {
  secret_id = google_secret_manager_secret.gemini_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.concierge_runtime_sa.email}"
}

resource "google_secret_manager_secret" "gcal_oauth_client_secret" {
  secret_id = var.gcal_oauth_secret_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled_apis]
}

resource "google_secret_manager_secret_iam_member" "concierge_gcal_secret_accessor" {
  secret_id = google_secret_manager_secret.gcal_oauth_client_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.concierge_runtime_sa.email}"
}

resource "google_project_iam_member" "concierge_dlp_user" {
  project = var.project_id
  role    = "roles/dlp.user"
  member  = "serviceAccount:${google_service_account.concierge_runtime_sa.email}"
}

resource "google_project_iam_member" "concierge_trace_writer" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.concierge_runtime_sa.email}"
}

resource "google_project_iam_member" "concierge_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.concierge_runtime_sa.email}"
}

resource "google_cloud_run_v2_service" "family_ops_concierge" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.concierge_runtime_sa.email

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "1Gi"
        }
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }

      env {
        name  = "USE_SECRET_MANAGER"
        value = "1"
      }

      env {
        name  = "GEMINI_API_KEY_SECRET_ID"
        value = google_secret_manager_secret.gemini_api_key.secret_id
      }

      env {
        name  = "ENABLE_CLOUD_DLP"
        value = var.enable_cloud_dlp ? "1" : "0"
      }

      env {
        name  = "GOOGLE_OAUTH_CLIENT_ID"
        value = var.google_oauth_client_id
      }

      env {
        name  = "GOOGLE_OAUTH_CLIENT_SECRET_SECRET"
        value = google_secret_manager_secret.gcal_oauth_client_secret.secret_id
      }

      env {
        name  = "GOOGLE_CALENDAR_ID"
        value = var.google_calendar_id
      }

      env {
        name  = "FAMILY_MODEL_FAST"
        value = var.family_model_fast
      }

      env {
        name  = "FAMILY_MODEL_FLASH"
        value = var.family_model_flash
      }

      env {
        name  = "FAMILY_MODEL_PRO"
        value = var.family_model_pro
      }

      env {
        name = "GOOGLE_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gemini_api_key.secret_id
            version = "latest"
          }
        }
      }

      startup_probe {
        http_get {
          path = "/api/auth/status"
          port = 8080
        }
        initial_delay_seconds = 3
        period_seconds        = 5
        failure_threshold     = 6
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.concierge_secret_accessor,
    google_secret_manager_secret_iam_member.concierge_gcal_secret_accessor,
    google_project_service.enabled_apis,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public_pwa_invoker" {
  count    = var.allow_unauthenticated ? 1 : 0
  project  = google_cloud_run_v2_service.family_ops_concierge.project
  location = google_cloud_run_v2_service.family_ops_concierge.location
  name     = google_cloud_run_v2_service.family_ops_concierge.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
