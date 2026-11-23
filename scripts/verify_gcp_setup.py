"""Safe local verifier that links Family Ops Concierge to a personal GCP project & Gemini API key.

Never prints secret values (API keys or OAuth secrets). Checks:
1. `GOOGLE_CLOUD_PROJECT` in `family_concierge/.env` (and optionally syncs `terraform/terraform.tfvars`).
2. `GOOGLE_API_KEY` against the public Google Gemini API (`GOOGLE_GENAI_USE_ENTERPRISE=0`), automatically
   disabling `GOOGLE_GENAI_USE_ENTERPRISE` if a personal Gemini API key is detected and aligning
   `FAMILY_MODEL_FAST`, `FAMILY_MODEL_FLASH`, and `FAMILY_MODEL_PRO` with the models available on that key.
3. `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` readiness for Google Calendar OAuth 2.0.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

from dotenv import dotenv_values
from google import genai

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / "family_concierge" / ".env"
TFVARS_FILE = REPO_ROOT / "terraform" / "terraform.tfvars"

_PREFERRED_FAST = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.5-flash")
_PREFERRED_FLASH = ("gemini-3.8-flash", "gemini-3.6-flash", "gemini-3-flash-preview")
_PREFERRED_PRO = ("gemini-3.1-pro-preview", "gemini-3.8-flash")


def _update_env_file(updates: dict[str, str]) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    seen: set[str] = set()
    new_lines: list[str] = []
    for ln in lines:
        stripped = ln.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        new_lines.append(ln)
    for k, v in updates.items():
        if k not in seen:
            new_lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _pick_supported_model(available: set[str], current: str, fallbacks: tuple[str, ...]) -> str:
    if current and current in available:
        return current
    for candidate in fallbacks:
        if candidate in available:
            return candidate
    return current


def main() -> int:
    if not ENV_FILE.exists():
        print("ERROR: family_concierge/.env not found. Copy .env.example to family_concierge/.env first.")
        return 1

    vals = dotenv_values(ENV_FILE)
    project_id = (vals.get("GOOGLE_CLOUD_PROJECT") or "").strip()
    api_key = (vals.get("GOOGLE_API_KEY") or "").strip()
    oauth_cid = (vals.get("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
    oauth_sec = (vals.get("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()

    print("=== 1. Personal GCP Project Check ===")
    if project_id:
        print(f"  [OK] GOOGLE_CLOUD_PROJECT = {project_id}")
    else:
        print("  [ACTION NEEDED] Set GOOGLE_CLOUD_PROJECT=<your-personal-project-id> in family_concierge/.env")

    print("\n=== 2. Personal Gemini API Key & Model Tier Check ===")
    if not api_key:
        print("  [ACTION NEEDED] Set GOOGLE_API_KEY=<your-personal-gemini-api-key> in family_concierge/.env")
        return 1

    # Test against the standard public Gemini API first (personal keys do NOT use corporate enterprise routing)
    saved_ent = os.environ.pop("GOOGLE_GENAI_USE_ENTERPRISE", None)
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "0"
    env_updates: dict[str, str] = {}

    available_models: set[str] = set()
    used_enterprise = False
    try:
        public_client = genai.Client(api_key=api_key)
        for m in public_client.models.list():
            name = (m.name or "").replace("models/", "")
            actions = getattr(m, "supported_actions", None) or []
            if not actions or "generateContent" in actions:
                available_models.add(name)
        if vals.get("GOOGLE_GENAI_USE_ENTERPRISE", "").strip().lower() in ("1", "true", "yes"):
            env_updates["GOOGLE_GENAI_USE_ENTERPRISE"] = "0"
            print("  [AUTO-FIXED] Switched GOOGLE_GENAI_USE_ENTERPRISE=0 for personal Gemini API key.")
    except Exception:
        # Fallback check in case the user has not swapped out their corporate key yet
        if saved_ent:
            os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = saved_ent
        os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = "1"
        try:
            corp_client = genai.Client(api_key=api_key)
            for m in corp_client.models.list():
                name = (m.name or "").replace("models/", "")
                available_models.add(name)
            used_enterprise = True
            print("  [NOTE] Current GOOGLE_API_KEY is still your corporate key (GOOGLE_GENAI_USE_ENTERPRISE=1).")
            print("         Replace GOOGLE_API_KEY in family_concierge/.env with your personal key and re-run!")
        except Exception as exc:
            print(f"  [ERROR] Could not authenticate GOOGLE_API_KEY ({type(exc).__name__}). Double-check the key.")
            return 1

    cur_fast = (vals.get("FAMILY_MODEL_FAST") or "gemini-3.5-flash-lite").strip()
    cur_flash = (vals.get("FAMILY_MODEL_FLASH") or "gemini-3.8-flash").strip()
    cur_pro = (vals.get("FAMILY_MODEL_PRO") or "gemini-3.1-pro-preview").strip()

    chosen_fast = _pick_supported_model(available_models, cur_fast, _PREFERRED_FAST)
    chosen_flash = _pick_supported_model(available_models, cur_flash, _PREFERRED_FLASH)
    chosen_pro = _pick_supported_model(available_models, cur_pro, _PREFERRED_PRO)

    if chosen_fast != cur_fast:
        env_updates["FAMILY_MODEL_FAST"] = chosen_fast
    if chosen_flash != cur_flash:
        env_updates["FAMILY_MODEL_FLASH"] = chosen_flash
    if chosen_pro != cur_pro:
        env_updates["FAMILY_MODEL_PRO"] = chosen_pro

    if env_updates:
        _update_env_file(env_updates)

    mode_str = "Corporate Enterprise AI Studio" if used_enterprise else "Personal Public Gemini API"
    print(f"  [OK] Authenticated via {mode_str} ({len(available_models)} models available)")
    print(f"  [OK] FAMILY_MODEL_FAST  = {chosen_fast}")
    print(f"  [OK] FAMILY_MODEL_FLASH = {chosen_flash}")
    print(f"  [OK] FAMILY_MODEL_PRO   = {chosen_pro}")

    print("\n=== 3. Google Calendar OAuth 2.0 Credentials Check ===")
    if oauth_cid and oauth_sec:
        print(f"  [OK] GOOGLE_OAUTH_CLIENT_ID configured ({oauth_cid[:16]}...)")
        print("  [OK] GOOGLE_OAUTH_CLIENT_SECRET configured (redacted)")
    else:
        print("  [PENDING] Add GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET to family_concierge/.env")
        print("            (Required before clicking 'Connect & Approve with Google' in Family Setup)")

    if project_id and not used_enterprise:
        tfvars_content = (
            f'project_id             = "{project_id}"\n'
            f'region                 = "us-central1"\n'
            f'service_name           = "family-ops-concierge"\n'
            f'container_image        = "us-central1-docker.pkg.dev/{project_id}/family-ops-agents/family-ops-concierge:latest"\n'
            f'gemini_secret_id       = "family-ops-gemini-api-key"\n'
            f'gcal_oauth_secret_id   = "family-ops-gcal-oauth-client-secret"\n'
            f'google_oauth_client_id = "{oauth_cid}"\n'
            f'google_calendar_id     = ""\n'
            f'family_model_fast      = "{chosen_fast}"\n'
            f'family_model_flash     = "{chosen_flash}"\n'
            f'family_model_pro       = "{chosen_pro}"\n'
            f'enable_cloud_dlp       = false\n'
            f'allow_unauthenticated  = true\n'
        )
        TFVARS_FILE.write_text(tfvars_content, encoding="utf-8")
        print(f"\n=== 4. Terraform Config Synced ===\n  [OK] Updated git-ignored terraform/terraform.tfvars for project '{project_id}'")

    return 0


if __name__ == "__main__":
    sys.exit(main())
