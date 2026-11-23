"""Secure secret resolution via Google Cloud Secret Manager with local developer env fallback.

Ensures zero hardcoded API keys or credentials exist anywhere in the codebase:
1. In Google Cloud (Cloud Run / Vertex AI Agent Engine) when `USE_SECRET_MANAGER=1` or when raw
   env keys are absent and `GOOGLE_CLOUD_PROJECT` is configured, secrets are fetched dynamically
   from `google.cloud.secretmanager.SecretManagerServiceClient` and cached in memory.
2. In local development, secrets fall back cleanly to runtime environment variables loaded from
   a git-ignored `.env` file without requiring GCP IAM network calls unless enabled.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from google.cloud import secretmanager

logger = logging.getLogger(__name__)

_SECRET_CACHE: dict[str, str] = {}
_SECRET_MISS_CACHE: set[str] = set()
_CACHE_LOCK = threading.Lock()


def _build_secret_version_resource(
    secret_id_or_resource: str,
    project_id: Optional[str] = None,
    version: str = "latest",
) -> Optional[str]:
    """Formats a Secret Manager version resource path (`projects/*/secrets/*/versions/*`)."""
    cleaned = (secret_id_or_resource or "").strip()
    if not cleaned:
        return None
    if cleaned.startswith("projects/"):
        if "/versions/" in cleaned:
            return cleaned
        return f"{cleaned.rstrip('/')}/versions/{version}"
    resolved_project = (
        project_id
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCP_PROJECT_ID")
        or ""
    ).strip()
    if not resolved_project:
        return None
    return f"projects/{resolved_project}/secrets/{cleaned}/versions/{version}"


def fetch_secret_from_gcp(
    secret_id_or_resource: str,
    *,
    project_id: Optional[str] = None,
    version: str = "latest",
    client: Optional[secretmanager.SecretManagerServiceClient] = None,
) -> Optional[str]:
    """Fetches a secret payload string from Google Cloud Secret Manager with thread-safe caching."""
    resource_name = _build_secret_version_resource(
        secret_id_or_resource,
        project_id=project_id,
        version=version,
    )
    if not resource_name:
        return None

    with _CACHE_LOCK:
        if resource_name in _SECRET_CACHE:
            return _SECRET_CACHE[resource_name]
        if client is None and resource_name in _SECRET_MISS_CACHE:
            return None

    try:
        sm_client = client or secretmanager.SecretManagerServiceClient()
        response = sm_client.access_secret_version(request={"name": resource_name}, timeout=4.0)
        secret_value = response.payload.data.decode("utf-8").strip()
        if secret_value:
            with _CACHE_LOCK:
                _SECRET_CACHE[resource_name] = secret_value
                _SECRET_MISS_CACHE.discard(resource_name)
            return secret_value
    except Exception as exc:  # noqa: BLE001
        with _CACHE_LOCK:
            if client is None:
                _SECRET_MISS_CACHE.add(resource_name)
        logger.warning(
            "Secret Manager lookup skipped/failed for '%s' (%s); checking environment fallback.",
            resource_name,
            type(exc).__name__,
        )
    return None


def get_secret(
    env_var_names: tuple[str, ...],
    *,
    gcp_secret_id_env: str = "",
    default_gcp_secret_id: str = "",
    client: Optional[secretmanager.SecretManagerServiceClient] = None,
) -> Optional[str]:
    """Resolves a secret from Google Cloud Secret Manager first (when enabled) or environment."""
    use_sm_flag = os.environ.get("USE_SECRET_MANAGER", "").strip().lower() in ("1", "true", "yes")
    configured_secret_id = (
        (os.environ.get(gcp_secret_id_env, "") if gcp_secret_id_env else "")
        or default_gcp_secret_id
    ).strip()
    has_gcp_project = bool(
        os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        or os.environ.get("GCP_PROJECT_ID", "").strip()
        or configured_secret_id.startswith("projects/")
    )

    # 1. Primary path when USE_SECRET_MANAGER=1: fetch directly from Google Cloud Secret Manager
    if use_sm_flag and configured_secret_id and has_gcp_project:
        sm_val = fetch_secret_from_gcp(configured_secret_id, client=client)
        if sm_val:
            return sm_val

    # 2. Check injected runtime environment variables (Cloud Run secret env mount or local .env)
    for env_name in env_var_names:
        val = os.environ.get(env_name, "").strip()
        if val:
            return val

    # 3. Automatic fallback to Secret Manager if env vars were not set and a GCP project is present
    if configured_secret_id and has_gcp_project:
        sm_val = fetch_secret_from_gcp(configured_secret_id, client=client)
        if sm_val:
            return sm_val

    return None


def resolve_gemini_api_key(
    *,
    client: Optional[secretmanager.SecretManagerServiceClient] = None,
) -> Optional[str]:
    """Resolves the Gemini API key securely and synchronizes `GOOGLE_API_KEY` in `os.environ`."""
    resolved = get_secret(
        ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        gcp_secret_id_env="GEMINI_API_KEY_SECRET_ID",
        default_gcp_secret_id="family-ops-gemini-api-key",
        client=client,
    )
    if resolved and not os.environ.get("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = resolved
    return resolved


def clear_secret_cache() -> None:
    """Clears cached Secret Manager values (used in unit tests and key rotation)."""
    with _CACHE_LOCK:
        _SECRET_CACHE.clear()
        _SECRET_MISS_CACHE.clear()
