"""Export read-only process diagnostics for one archived RepoFix run."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "agent-worker"))
from repopilot.diagnostics import diagnose  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--artifact-root", type=Path, default=Path("runtime/artifacts"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary, delta = diagnose(args.run_id, args.artifact_root)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "diagnostic.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                                        encoding="utf-8")
    if delta:
        (args.output_dir / "diagnostic.patch").write_bytes(delta)
    print(f"Read-only diagnosis: {args.output_dir / 'diagnostic.json'}")


if __name__ == "__main__":
    main()
