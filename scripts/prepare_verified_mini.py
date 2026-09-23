"""Cache the published Verified Mini tasks and official SWE-bench harness metadata.

Only the Mini issue text, pinned commit and image are passed to the agent. Gold patches,
test patches and evaluation scripts stay in the local evaluation dataset.
"""

import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "runtime" / "swebench"
MINI = "MariusHobbhahn/swe-bench-verified-mini"
MINI_REVISION = "b316c349947c29963fce3f4a65967c9807a4b673"
VERIFIED = "SWE-bench/SWE-bench_Verified"
VERIFIED_REVISION = "78f471bf655a3137b2e8a75af1501690ec009ec3"
FIRST_SIX = (0, 8, 16, 25, 33, 41)
TEN = (0, 4, 8, 12, 16, 25, 29, 33, 37, 41)


def get_json(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def dataset_rows(name, count):
    rows = []
    for offset in range(0, count, 100):
        params = urllib.parse.urlencode({"dataset": name, "config": "default", "split": "test",
                                         "offset": offset, "length": min(100, count - offset)})
        rows.extend(item["row"] for item in get_json("https://datasets-server.huggingface.co/rows?" + params)["rows"])
    if len(rows) != count or len({row["instance_id"] for row in rows}) != count:
        raise ValueError(f"Unexpected {name} dataset shape")
    return rows


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_frozen(path, value):
    data = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if path.exists() and path.read_bytes() != data:
        raise ValueError(f"Refusing to overwrite frozen dataset: {path}")
    path.write_bytes(data)


def main():
    for dataset, revision in ((MINI, MINI_REVISION), (VERIFIED, VERIFIED_REVISION)):
        actual = get_json("https://huggingface.co/api/datasets/" + dataset)["sha"]
        if actual != revision:
            raise ValueError(f"Dataset revision changed: {dataset} {actual}")
    mini_rows = dataset_rows(MINI, 50)
    official = {row["instance_id"]: row for row in dataset_rows(VERIFIED, 500)}
    if not set(row["instance_id"] for row in mini_rows) <= set(official):
        raise ValueError("Mini contains an instance absent from official Verified")

    CACHE.mkdir(parents=True, exist_ok=True)
    augmented = []
    for row in mini_rows:
        reference = official[row["instance_id"]]
        for key in ("repo", "base_commit", "problem_statement", "patch", "test_patch"):
            if row[key] != reference[key]:
                raise ValueError(f"Mini / Verified {key} differs for {row['instance_id']}")
        for key in ("FAIL_TO_PASS", "PASS_TO_PASS"):
            left = json.loads(row[key]) if isinstance(row[key], str) else row[key]
            right = json.loads(reference[key]) if isinstance(reference[key], str) else reference[key]
            if left != right:
                raise ValueError(f"Mini / Verified {key} differs for {row['instance_id']}")
        row["image"] = reference["image"]
        write_frozen(CACHE / f"{row['instance_id']}.json", row)
        augmented.append({**row, **{key: reference[key] for key in
                                    ("eval_type", "log_parser", "eval_script")}})

    raw_path = CACHE / "swe-bench-verified-mini-test.json"
    harness_path = CACHE / "swe-bench-verified-mini-test-harness.json"
    write_frozen(raw_path, mini_rows)
    write_frozen(harness_path, augmented)
    provenance = {"mini_source": MINI, "mini_revision": MINI_REVISION,
                  "official_metadata_source": VERIFIED, "official_revision": VERIFIED_REVISION,
                  "raw_sha256": digest(raw_path), "augmented_sha256": digest(harness_path),
                  "matched_instances": 50, "source_field_differences": {}}
    (CACHE / "verified-mini-harness-augmentation.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    for filename, positions in (("verified-mini-six-selection.json", FIRST_SIX),
                                ("verified-mini-ten-selection.json", TEN)):
        selection = {"dataset": MINI, "dataset_revision": MINI_REVISION,
                     "positions": positions,
                     "selected_ids": [mini_rows[index]["instance_id"] for index in positions],
                     "dataset_sha256": digest(raw_path), "gold_screening": False}
        path = CACHE / filename
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing["selected_ids"] != selection["selected_ids"]:
                raise ValueError(f"Existing selection differs: {path}")
        else:
            path.write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(provenance, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
