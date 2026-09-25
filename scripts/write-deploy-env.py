#!/usr/bin/env python3
"""Write a private Compose env file without interpolation or secret output."""
import json
import os
from pathlib import Path
import sys

from provision import private_write, ProvisionError, require

KEYS = (
    "ENTRA_TENANT_ID", "GATEWAY_APP_ID", "WEBUI_CLIENT_ID", "WEBUI_CLIENT_SECRET",
    "WEBUI_SECRET_KEY", "UPSTREAM_API_KEY", "UPSTREAM_MODEL", "UPSTREAM_BASE_URL",
    "WEBUI_URL", "OPENID_REDIRECT_URI",
)


def render(env):
    lines = []
    for key in KEYS:
        value = env.get(key, "")
        require(value and "REPLACE_WITH_" not in value, f"{key} is missing or a placeholder")
        require(not any(c in value for c in "\n\r\x00"), f"{key} must be a single-line value")
        # JSON escaping matches Compose double-quoted env values. Double dollars
        # prevent interpolation while preserving literal dollars in the value.
        lines.append(f'{key}={json.dumps(value).replace("$", "$$")}')
    require(env["WEBUI_URL"].startswith("https://"), "Production WEBUI_URL must use HTTPS")
    require(env["OPENID_REDIRECT_URI"] == env["WEBUI_URL"].rstrip("/") + "/oauth/oidc/callback", "Production callback does not match WEBUI_URL")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    try:
        require(len(sys.argv) == 2, "Usage: write-deploy-env.py OUTPUT_PATH")
        private_write(Path(sys.argv[1]), render(os.environ))
    except ProvisionError as exc:
        sys.exit(str(exc))
