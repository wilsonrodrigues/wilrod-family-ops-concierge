#!/usr/bin/env bash
# One-command Google Cloud Run Gen2 deployment for Family Ops Concierge.
# Reads configuration from family_concierge/.env (never prints secret values),
# provisions Secret Manager secrets + a persistent Cloud Storage FUSE volume so
# household state and Google Calendar OAuth tokens survive scale-to-zero restarts,
# and deploys the container via Cloud Build + Cloud Run.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_DIR}/family_concierge/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "ERROR: ${ENV_FILE} not found." >&2
  exit 1
fi

read_env_var() {
  local key="$1"
  grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 | cut -d'=' -f2- | tr -d '\r'
}

PROJECT_ID="$(read_env_var GOOGLE_CLOUD_PROJECT)"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-family-ops-concierge}"
SA_NAME="family-ops-concierge-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
STATE_BUCKET="${PROJECT_ID}-family-ops-state"
GEMINI_SECRET_ID="family-ops-gemini-api-key"
GCAL_SECRET_ID="family-ops-gcal-oauth-client-secret"

API_KEY="$(read_env_var GOOGLE_API_KEY)"
OAUTH_CLIENT_ID="$(read_env_var GOOGLE_OAUTH_CLIENT_ID)"
OAUTH_CLIENT_SECRET="$(read_env_var GOOGLE_OAUTH_CLIENT_SECRET)"
MODEL_FAST="$(read_env_var FAMILY_MODEL_FAST)"
MODEL_FLASH="$(read_env_var FAMILY_MODEL_FLASH)"
MODEL_PRO="$(read_env_var FAMILY_MODEL_PRO)"

if [[ -z "${PROJECT_ID}" || -z "${API_KEY}" ]]; then
  echo "ERROR: GOOGLE_CLOUD_PROJECT and GOOGLE_API_KEY must be set in family_concierge/.env" >&2
  exit 1
fi

echo "==> 1/5 Enabling required APIs on project '${PROJECT_ID}'..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  calendar-json.googleapis.com \
  storage.googleapis.com \
  --project="${PROJECT_ID}"

echo "==> 2/5 Ensuring runtime Service Account '${SA_EMAIL}'..."
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="Family Ops Concierge Runtime SA" \
    --project="${PROJECT_ID}"
fi

echo "==> 3/5 Ensuring persistent Cloud Storage bucket 'gs://${STATE_BUCKET}'..."
if ! gcloud storage buckets describe "gs://${STATE_BUCKET}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${STATE_BUCKET}" \
    --location="${REGION}" \
    --uniform-bucket-level-access \
    --public-access-prevention \
    --project="${PROJECT_ID}"
fi

gcloud storage buckets add-iam-policy-binding "gs://${STATE_BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectAdmin" \
  --project="${PROJECT_ID}" >/dev/null

echo "==> 4/5 Syncing secrets into Google Cloud Secret Manager..."
upsert_secret() {
  local sec_id="$1"
  local sec_val="$2"
  if [[ -z "${sec_val}" ]]; then
    return 0
  fi
  if ! gcloud secrets describe "${sec_id}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud secrets create "${sec_id}" --replication-policy="automatic" --project="${PROJECT_ID}" >/dev/null
  fi
  printf "%s" "${sec_val}" | gcloud secrets versions add "${sec_id}" --data-file=- --project="${PROJECT_ID}" >/dev/null
  gcloud secrets add-iam-policy-binding "${sec_id}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" \
    --project="${PROJECT_ID}" >/dev/null
}

upsert_secret "${GEMINI_SECRET_ID}" "${API_KEY}"
upsert_secret "${GCAL_SECRET_ID}" "${OAUTH_CLIENT_SECRET}"

SECRETS_FLAG="GOOGLE_API_KEY=${GEMINI_SECRET_ID}:latest"
if [[ -n "${OAUTH_CLIENT_SECRET}" ]]; then
  SECRETS_FLAG="${SECRETS_FLAG},GOOGLE_OAUTH_CLIENT_SECRET=${GCAL_SECRET_ID}:latest"
fi

echo "==> 5/5 Building and deploying '${SERVICE_NAME}' to Cloud Run (${REGION})..."
cd "${REPO_DIR}"
gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --execution-environment=gen2 \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=1 \
  --cpu=1 \
  --memory=1Gi \
  --add-volume="name=family-data,type=cloud-storage,bucket=${STATE_BUCKET}" \
  --add-volume-mount="volume=family-data,mount-path=/app/data" \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_GENAI_USE_ENTERPRISE=0,GOOGLE_GENAI_USE_VERTEXAI=0,ENABLE_CLOUD_DLP=0,FAMILY_OPS_DB_PATH=/app/data/family_ops.db,FAMILY_ADK_SESSION_DB=sqlite+aiosqlite:////app/data/family_adk_sessions.db,FAMILY_SQLITE_JOURNAL_MODE=DELETE,GOOGLE_OAUTH_CLIENT_ID=${OAUTH_CLIENT_ID},FAMILY_MODEL_FAST=${MODEL_FAST:-gemini-3.5-flash-lite},FAMILY_MODEL_FLASH=${MODEL_FLASH:-gemini-3.8-flash},FAMILY_MODEL_PRO=${MODEL_PRO:-gemini-3.1-pro-preview}" \
  --set-secrets="${SECRETS_FLAG}"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')"
echo ""
echo "=================================================================="
echo "DEPLOYED URL: ${SERVICE_URL}"
echo "OAUTH REDIRECT URI TO ADD IN GOOGLE CLOUD CONSOLE:"
echo "  ${SERVICE_URL}/api/gcal/oauth/callback"
echo "=================================================================="
