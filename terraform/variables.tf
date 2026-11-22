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
  description = "Enable Google Cloud Sensitive Data Protection (Cloud DLP) API scrubbing."
  type        = bool
  default     = true
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
