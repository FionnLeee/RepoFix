"""Exercise the private HTTPS gateway and a deterministic repair."""

import os
import ssl
import time
from uuid import uuid4

import httpx


def main():
    auth = httpx.BasicAuth(os.environ["DEMO_USERNAME"], os.environ["DEMO_PASSWORD"])
    verify = ssl.create_default_context(cafile=os.environ.get("DEMO_CA_FILE"))
    with httpx.Client(base_url=os.environ["DEMO_BASE_URL"], verify=verify, timeout=30,
                      trust_env=False) as client:
        assert client.get("/showcase").status_code == 401
        assert client.get("/api/runs").status_code == 401
        assert client.get("/api/internal/runs/test/claim", auth=auth).status_code == 404
        assert client.get("/showcase", auth=auth).status_code == 200
        health = client.get("/api/health", auth=auth).raise_for_status().json()
        assert health["liveEnabled"] is False
        assert client.post("/api/runs", auth=auth,
                           json={"mode": "live", "requestKey": uuid4().hex}).status_code == 409
        baselines = client.get("/api/baselines", auth=auth).raise_for_status().json()
        assert baselines
        created = client.post("/api/runs", auth=auth, json={
            "mode": "demo", "requestKey": uuid4().hex, "baselineId": baselines[0]["id"],
            "contextMode": "full", "reviewPolicy": "off",
        }).raise_for_status().json()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            run = client.get(f"/api/runs/{created['id']}", auth=auth).raise_for_status().json()
            if run["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}:
                break
            time.sleep(1)
        assert run["status"] == "SUCCEEDED", run["status"]
        assert run["result"]["verification"]["passed"] is True
        assert run["result"]["patch"]
    print("Hosted HTTPS smoke passed: login, internal route isolation, live disabled, verified deterministic repair.")


if __name__ == "__main__":
    main()
