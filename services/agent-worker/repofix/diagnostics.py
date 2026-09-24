"""Read archived checkpoints without converting a process diff into a submitted patch."""

import base64
import binascii
import hashlib
import json
import uuid

from repofix.checkpoint import load_registered


def image_delta(files):
    names = sorted(name for name in files if name.startswith("delta/"))
    if not names:
        return b""
    if names != [f"delta/{index:06d}" for index in range(len(names))]:
        raise ValueError("Checkpoint delta is incomplete")
    return base64.b64decode("".join(files[name] for name in names), validate=True)


def diagnose(run_id, artifact_root):
    run_id = str(uuid.UUID(run_id))
    folder = artifact_root / run_id
    result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    references = sorted((folder / "checkpoints").glob("g*/*.json"))
    checkpoints = []
    invalid = []
    latest_verified = set()
    for path in references:
        if path.name in {"latest.json", "inflight.json"}:
            continue
        try:
            generation = int(path.parent.name.removeprefix("g"))
            payload = json.loads(path.read_text(encoding="utf-8"))
            reference = {
                "id": payload["id"], "generation": generation,
                "sha256": hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                                       allow_nan=False).encode()).hexdigest(),
            }
            latest_path = path.parent / "latest.json"
            if latest_path.is_file():
                latest_ref = json.loads(latest_path.read_text(encoding="utf-8"))
                if latest_ref.get("id") == payload["id"]:
                    reference = latest_ref
                    latest_verified.add(payload["id"])
            # Older snapshots have self-consistency checks; latest additionally has its saved reference hash.
            checkpoint = load_registered(artifact_root, run_id, reference)
            delta = image_delta(checkpoint.files)
            checkpoints.append((checkpoint, delta))
        except (ValueError, KeyError, OSError, TypeError, binascii.Error) as exc:
            invalid.append({"file": path.name, "reason": type(exc).__name__})
    checkpoints.sort(key=lambda item: (item[0].binding["generation"], item[0].sequence))
    first = next(((item, delta) for item, delta in checkpoints if delta), None)
    last = checkpoints[-1] if checkpoints else None
    last_with_delta = next(((item, delta) for item, delta in reversed(checkpoints)
                            if delta and item.id in latest_verified), None)
    submitted = any(item.phase == "submitted" for item, _ in checkpoints)
    official_patch = result.get("patch") or ""
    summary = {
        "schema_version": 1,
        "run_id": run_id,
        "stop_reason": result.get("stop_reason") or result.get("error"),
        "model_calls": result.get("model_calls"),
        "valid_checkpoints": len(checkpoints),
        "invalid_checkpoint_files": invalid,
        "first_observed_delta": ({
            "generation": first[0].binding["generation"], "sequence": first[0].sequence,
            "phase": first[0].phase, "model_calls": first[0].state.model_calls,
            "bytes": len(first[1]),
        } if first else None),
        "last_checkpoint": ({
            "generation": last[0].binding["generation"], "sequence": last[0].sequence,
            "phase": last[0].phase, "model_calls": last[0].state.model_calls,
        } if last else None),
        "submitted_checkpoint_seen": submitted,
        "formal_candidate_present": bool(official_patch),
        "diagnostic_delta": ({
            "generation": last_with_delta[0].binding["generation"],
            "sequence": last_with_delta[0].sequence,
            "checkpoint_id": last_with_delta[0].id,
            "base": last_with_delta[0].files.get("image-base"),
            "bytes": len(last_with_delta[1]),
            "sha256": hashlib.sha256(last_with_delta[1]).hexdigest(),
            "integrity": "latest_reference_hash_and_workspace_digest",
            "status": "process_snapshot_unsubmitted_unverified",
        } if last_with_delta else None),
    }
    return summary, last_with_delta[1] if last_with_delta else None
