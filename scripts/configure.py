"""Create local-only secrets, optionally reusing the authorized TicketPilot model route."""

import argparse
import secrets
from pathlib import Path

from dotenv import dotenv_values

parser = argparse.ArgumentParser()
parser.add_argument("--ticketpilot-env", type=Path)
args = parser.parse_args()
target = Path(__file__).resolve().parents[1] / ".env"
current = dict(dotenv_values(target)) if target.exists() else {}
for key in ("POSTGRES_PASSWORD", "RABBITMQ_PASSWORD", "WORKER_TOKEN"):
    current.setdefault(key, secrets.token_hex(24))
if args.ticketpilot_env:
    source = dotenv_values(args.ticketpilot_env)
    if source.get("DEFAULT_MODEL") != "openai-compatible":
        raise SystemExit("TicketPilot route needs explicit mapping; no credentials copied.")
    for old, new in [
        ("COMPATIBLE_MODEL", "MODEL_NAME"),
        ("COMPATIBLE_BASE_URL", "MODEL_BASE_URL"),
        ("COMPATIBLE_API_KEY", "MODEL_API_KEY"),
    ]:
        if not source.get(old):
            raise SystemExit(f"Required configuration is empty: {old}")
        current[new] = source[old]
    current["LIVE_ENABLED"] = "true"
else:
    current.setdefault("LIVE_ENABLED", "false")


def quote(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


target.write_text("\n".join(f"{key}={quote(value)}" for key, value in current.items()) + "\n", encoding="utf-8")
print("Local .env configured; secret values are not displayed.")
