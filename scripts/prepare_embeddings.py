"""Download the pinned public ONNX model before starting workers; never execute repository code."""
import hashlib
import json
from pathlib import Path

from huggingface_hub import snapshot_download

REPO = "qdrant/bge-small-en-v1.5-onnx-q"
REVISION = "52398278842ec682c6f32300af41344b1c0b0bb2"

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1] / "runtime" / "embedding-model"
    snapshot_download(REPO, revision=REVISION, local_dir=root,
                      allow_patterns=["*.json", "*.txt", "model_optimized.onnx"])
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in root.iterdir() if p.is_file() and p.name != "manifest.json"}
    manifest = {"model": "BAAI/bge-small-en-v1.5", "repository": REPO, "revision": REVISION,
                "dimension": 384, "files": files}
    (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    print(json.dumps({"model": manifest["model"], "revision": REVISION, "files": len(files)}))
