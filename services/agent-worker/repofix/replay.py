"""Recheck an archived patch against its pinned source, tests and image, without a model."""

import argparse
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path

from repofix.repository import RepositoryTask, digest
from repofix.repository_runtime import replay_patch, verify


def replay(run_id):
    run_id = str(uuid.UUID(run_id))
    folder = Path(os.getenv("ARTIFACT_ROOT", "runtime/artifacts")) / run_id
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    provenance = manifest["provenance"]
    source = json.loads((folder / "source.json").read_text(encoding="utf-8"))
    spec = RepositoryTask.model_validate(manifest["spec"])
    patch = (folder / "candidate.patch").read_bytes().decode()
    if (digest(source) != provenance["source_sha256"]
            or digest(spec.verificationFiles) != provenance["verification_sha256"]
            or hashlib.sha256(patch.encode()).hexdigest() != provenance["patch_sha256"]):
        raise ValueError("Archived input digest mismatch")
    candidate = replay_patch(source, patch, folder)
    if digest(candidate) != provenance["candidate_sha256"]:
        raise ValueError("Replayed candidate digest mismatch")
    cancel = threading.Event()
    before = verify(source, spec, run_id, cancel, provenance["image_id"])
    after = verify(candidate, spec, run_id, cancel, provenance["image_id"])
    b, a = before["report"], after["report"]
    passed = bool(b and a and b["failures"] > 0 and not b["errors"] and not b["skipped"]
                  and a["successful"] and a["tests"] > 0 and not a["skipped"]
                  and b["ids"] == a["ids"] and before["returncode"] != 0 and after["returncode"] == 0)
    report = {"run_id": run_id, "passed": passed, "baseline": before, "candidate": after,
              "source_sha256": digest(source), "candidate_sha256": digest(candidate)}
    (folder / "replay.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    args = parser.parse_args()
    report = replay(args.run_id)
    print(json.dumps({"run_id": args.run_id, "passed": report["passed"]}))
    raise SystemExit(0 if report["passed"] else 1)
