"""Read-only local prerequisites check for a RepoFix interview demonstration."""

import argparse
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


def http_check(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.loads(response.read(1024 * 1024))
        return {"ok": True, "detail": payload if url.endswith("/health") else f"{len(payload)} entries"}
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return {"ok": False, "detail": type(exc).__name__}


def doctor(root, api_url):
    checks = {}
    try:
        docker = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                                capture_output=True, text=True, timeout=8, check=False)
        version = docker.stdout.strip() if docker.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        version = None
    checks["docker_daemon"] = {"ok": version is not None,
                               "detail": version or "Start Docker Desktop"}
    if version is not None:
        try:
            image = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", "python:3.12-slim"],
                                   capture_output=True, text=True, timeout=8, check=False)
            image_id = image.stdout.strip() if image.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            image_id = None
        checks["sandbox_image"] = {"ok": image_id is not None,
                                   "detail": image_id or "Pull python:3.12-slim before running the sandbox"}
    else:
        checks["sandbox_image"] = {"ok": False, "detail": "Docker daemon unavailable"}
    checks["api"] = http_check(f"{api_url.rstrip('/')}/health")
    checks["api_baselines"] = http_check(f"{api_url.rstrip('/')}/baseline-tasks")
    catalog = root / "runtime" / "baseline-catalog.json"
    checks["local_catalog"] = {"ok": catalog.is_file(), "detail": str(catalog)}
    embedding = Path(os.getenv("EMBEDDING_MODEL_DIR", root / "runtime" / "embedding-model"))
    checks["embedding_model"] = {"ok": embedding.is_dir() and any(embedding.iterdir()) if embedding.is_dir() else False,
                                 "detail": str(embedding)}
    artifacts = root / "runtime" / "artifacts"
    checks["artifacts"] = {"ok": artifacts.is_dir(), "detail": str(artifacts)}
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--api-url", default="http://localhost:3101")
    args = parser.parse_args()
    checks = doctor(args.root, args.api_url)
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    if not all(item["ok"] for item in checks.values()):
        print("Unavailable dependencies are prerequisites, not a benchmark or business failure. "
              "After starting the local stack, rerun this read-only check.")


if __name__ == "__main__":
    main()
