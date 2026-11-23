variable "project_id" {
  description = "Google Cloud project ID hosting Family Ops Concierge."
  type        = string
}

variable "region" {
  description = "Google Cloud region for Cloud Run and Artifact Registry."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run v2 service name."
  type        = string
  default     = "family-ops-concierge"
}

variable "artifact_repo_name" {
  description = "Artifact Registry Docker repository ID."
  type        = string
  default     = "family-ops-agents"
}

variable "container_image" {
  description = "Container image URI deployed to Cloud Run."
  type        = string
}

variable "gemini_secret_id" {
  description = "Secret Manager secret ID storing the Gemini API key."
  type        = string
  default     = "family-ops-gemini-api-key"
}

variable "enable_cloud_dlp" {
  description = "Enable Google Cloud Sensitive Data Protection (Cloud DLP) API scrubbing (disable on personal projects to avoid DLP API charges; local PII scrubber stays active)."
  type        = bool
  default     = false
}

variable "allow_unauthenticated" {
  description = "Expose the PWA publicly on Cloud Run (protected by optional SHA-256 Household Passcode)."
  type        = bool
  default     = true
}

variable "min_instances" {
  description = "Minimum Cloud Run instances."
  type        = number
  default     = 0
}

variable "max_instances" {
  description = "Maximum Cloud Run instances."
  type        = number
  default     = 3
}

variable "google_oauth_client_id" {
  description = "Backend OAuth 2.0 Web Client ID enabling end-user 'Connect & Approve with Google' calendar flow."
  type        = string
  default     = ""
}

variable "gcal_oauth_secret_id" {
  description = "Secret Manager secret ID storing GOOGLE_OAUTH_CLIENT_SECRET."
  type        = string
  default     = "family-ops-gcal-oauth-client-secret"
}

variable "google_calendar_id" {
  description = "Optional default shared Family Google Calendar ID (e.g. family...@group.calendar.google.com)."
  type        = string
  default     = ""
}

variable "family_model_fast" {
  description = "Fast lookup / compaction Gemini model ID."
  type        = string
  default     = "gemini-3.5-flash-lite"
}

variable "family_model_flash" {
  description = "Multimodal Coordinator Gemini model ID."
  type        = string
  default     = "gemini-3.8-flash"
}

variable "family_model_pro" {
  description = "Deep strategic planning Gemini model ID."
  type        = string
  default     = "gemini-3.1-pro-preview"
}
