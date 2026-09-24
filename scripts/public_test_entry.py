"""Inspect a public base checkout for a bounded development-test smoke command."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "agent-worker"))
from repopilot.public_tests import discover  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--explicit-command")
    args = parser.parse_args()
    print(json.dumps(discover(args.checkout, args.base_commit, args.repo, args.explicit_command), indent=2))


if __name__ == "__main__":
    main()
