import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

CONFIGURE = Path(__file__).resolve().parents[3] / "scripts" / "configure.py"


def test_configure_replaces_empty_template_secrets_and_keeps_existing_credentials(tmp_path):
    target = tmp_path / ".env.hosted"
    target.write_text("POSTGRES_PASSWORD=\nRABBITMQ_PASSWORD=\nWORKER_TOKEN=\nLIVE_ENABLED=\n")
    first = subprocess.run([sys.executable, str(CONFIGURE), "--output", str(target)],
                           check=True, capture_output=True, text=True)
    values = dotenv_values(target)
    secrets = [values[key] for key in ("POSTGRES_PASSWORD", "RABBITMQ_PASSWORD", "WORKER_TOKEN")]
    assert all(len(value) == 48 for value in secrets)
    assert len(set(secrets)) == 3
    assert values["LIVE_ENABLED"] == "false"
    assert all(value not in first.stdout + first.stderr for value in secrets)
    subprocess.run([sys.executable, str(CONFIGURE), "--output", str(target)], check=True,
                   capture_output=True, text=True)
    assert dotenv_values(target) == values
