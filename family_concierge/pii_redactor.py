"""Active PII Redaction pipeline protecting both structured telemetry logs and household memory storage.

Implements a defense-in-depth scrubbing architecture:
1. Always-on deterministic local PII & credential redactor (`scrub_text` / `scrub_payload`) covering:
   - Email addresses (`[REDACTED_EMAIL]`)
   - Domestic & international phone numbers (`[REDACTED_PHONE]`)
   - Government IDs such as US SSN and Brazil CPF (`[REDACTED_GOV_ID]`)
   - Payment card numbers (`[REDACTED_PAYMENT_CARD]`)
   - API keys, OAuth tokens, and sensitive dictionary keys like `passcode_hash` / `data_base64`
2. Optional Google Cloud Sensitive Data Protection (`google.cloud.dlp_v2.DlpServiceClient`)
   de-identification pass when `ENABLE_CLOUD_DLP=1` and `GOOGLE_CLOUD_PROJECT` are configured,
   with automatic zero-downtime fallback to the local deterministic scrubber.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

from google.cloud import dlp_v2

logger = logging.getLogger(__name__)

# Sensitive dictionary keys whose values must NEVER be persisted in logs or memory
_SENSITIVE_FIELD_NAMES = {
    "api_key",
    "google_api_key",
    "gemini_api_key",
    "passcode",
    "new_passcode",
    "passcode_hash",
    "x_household_passcode",
    "authorization",
    "access_token",
    "refresh_token",
    "oauth_client_secret",
    "client_secret",
    "oauth_tokens_json",
    "code_verifier",
    "secret",
    "password",
    "data_base64",
}

# Ordered deterministic PII & credential patterns
_CREDENTIAL_RE = re.compile(
    r"((?:AIza)[0-9A-Za-z_-]{30,}|AQ\.[0-9A-Za-z._-]{20,}|GOCSPX-[0-9A-Za-z_-]{15,}|ya29\.[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z]{20,}|Bearer\s+[0-9A-Za-z._-]{20,})"
)
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
# US SSN (123-45-6789) and Brazil CPF (123.456.789-00)
_GOV_ID_RE = re.compile(
    r"\b(?:\d{3}-\d{2}-\d{4}|\d{3}\.\d{3}\.\d{3}-\d{2})\b"
)
# 13-19 digit payment cards with spaces or hyphens (avoids matching ISO dates YYYY-MM-DD)
_PAYMENT_CARD_RE = re.compile(
    r"\b(?:\d{4}[ -]){3}\d{1,4}\b"
)
# Phone numbers: +55 (11) 94368-3357, (415) 555-0199, +1-800-555-0199, 555-234-5678
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,3}\)[\s.-]?|\b\d{3}[\s.-])\d{3,5}[\s.-]\d{4}(?!\d)"
)

_CLOUD_DLP_INFO_TYPES = [
    {"name": "EMAIL_ADDRESS"},
    {"name": "PHONE_NUMBER"},
    {"name": "US_SOCIAL_SECURITY_NUMBER"},
    {"name": "BRAZIL_CPF_NUMBER"},
    {"name": "CREDIT_CARD_NUMBER"},
    {"name": "STREET_ADDRESS"},
]


def _scrub_text_deterministic(text: str) -> tuple[str, list[str]]:
    """Applies fast local regex scrubbing and returns `(redacted_text, redaction_categories)`."""
    if not text:
        return text, []

    triggered: list[str] = []
    out = text

    if _CREDENTIAL_RE.search(out):
        triggered.append("CREDENTIAL")
        out = _CREDENTIAL_RE.sub("[REDACTED_CREDENTIAL]", out)

    if _EMAIL_RE.search(out):
        triggered.append("EMAIL_ADDRESS")
        out = _EMAIL_RE.sub("[REDACTED_EMAIL]", out)

    if _GOV_ID_RE.search(out):
        triggered.append("GOVERNMENT_ID")
        out = _GOV_ID_RE.sub("[REDACTED_GOV_ID]", out)

    if _PAYMENT_CARD_RE.search(out):
        triggered.append("PAYMENT_CARD")
        out = _PAYMENT_CARD_RE.sub("[REDACTED_PAYMENT_CARD]", out)

    if _PHONE_RE.search(out):
        triggered.append("PHONE_NUMBER")
        out = _PHONE_RE.sub("[REDACTED_PHONE]", out)

    return out, triggered


def deidentify_via_cloud_dlp(
    text: str,
    *,
    project_id: Optional[str] = None,
    dlp_client: Optional[dlp_v2.DlpServiceClient] = None,
) -> Optional[str]:
    """Invokes Google Cloud Sensitive Data Protection (Cloud DLP API) to de-identify `text`."""
    resolved_project = (
        project_id
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCP_PROJECT_ID")
        or ""
    ).strip()
    if not resolved_project or not text.strip():
        return None

    try:
        client = dlp_client or dlp_v2.DlpServiceClient()
        parent = f"projects/{resolved_project}/locations/global"
        inspect_config = {"info_types": _CLOUD_DLP_INFO_TYPES}
        deidentify_config = {
            "info_type_transformations": {
                "transformations": [
                    {
                        "primitive_transformation": {
                            "replace_with_info_type_config": {}
                        }
                    }
                ]
            }
        }
        response = client.deidentify_content(
            request={
                "parent": parent,
                "inspect_config": inspect_config,
                "deidentify_config": deidentify_config,
                "item": {"value": text},
            }
        )
        return response.item.value
    except Exception as exc:  # noqa: BLE001
        logger.debug("Cloud DLP API unavailable (%s); relying on local PII redactor.", type(exc).__name__)
        return None


class PiiRedactionPipeline:
    """Unified PII scrubber applied before writing structured logs or long-term memory rows."""

    def __init__(
        self,
        *,
        enable_cloud_dlp: Optional[bool] = None,
        project_id: Optional[str] = None,
        dlp_client: Optional[dlp_v2.DlpServiceClient] = None,
    ) -> None:
        self._enable_cloud_dlp = (
            enable_cloud_dlp
            if enable_cloud_dlp is not None
            else os.environ.get("ENABLE_CLOUD_DLP", "").strip().lower() in ("1", "true", "yes")
        )
        self._project_id = project_id
        self._dlp_client = dlp_client

    def scrub_text(self, text: str) -> str:
        """Redacts PII and credentials from a string before storage or logging."""
        redacted, _ = self.scrub_text_with_report(text)
        return redacted

    def scrub_text_with_report(self, text: str) -> tuple[str, list[str]]:
        """Redacts PII from `text` and returns `(sanitized_text, redaction_types_applied)`."""
        if not text:
            return text, []

        # Layer 1: Always run deterministic local scrubbing first (zero network latency)
        sanitized, categories = _scrub_text_deterministic(text)

        # Layer 2: Optional Google Cloud DLP API pass for deep entity de-identification
        if self._enable_cloud_dlp:
            dlp_out = deidentify_via_cloud_dlp(
                sanitized,
                project_id=self._project_id,
                dlp_client=self._dlp_client,
            )
            if dlp_out is not None and dlp_out != sanitized:
                categories.append("CLOUD_DLP_DEIDENTIFIED")
                sanitized = dlp_out

        return sanitized, categories

    def scrub_payload(self, value: Any, *, max_str_len: int = 1200) -> Any:
        """Recursively scrubs nested dicts, lists, tuples, and strings for structured JSON logs."""
        if value is None or isinstance(value, (bool, int, float)):
            return value

        if isinstance(value, str):
            clipped = value if len(value) <= max_str_len else f"{value[:max_str_len]}...[truncated]"
            return self.scrub_text(clipped)

        if isinstance(value, bytes):
            return f"<binary {len(value)} bytes redacted>"

        if isinstance(value, dict):
            cleaned_dict: dict[str, Any] = {}
            for k, v in value.items():
                key_str = str(k)
                if key_str.lower() in _SENSITIVE_FIELD_NAMES:
                    cleaned_dict[key_str] = "[REDACTED_SENSITIVE_FIELD]"
                else:
                    cleaned_dict[key_str] = self.scrub_payload(v, max_str_len=max_str_len)
            return cleaned_dict

        if isinstance(value, (list, tuple)):
            return [self.scrub_payload(item, max_str_len=max_str_len) for item in value]

        return self.scrub_text(str(value))


default_pii_redactor = PiiRedactionPipeline()
