from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_EXPORT_PATH = REPO_ROOT / "data" / "training_rows" / "train-00000-of-00001.jsonl"
LATEST_INDEX_PATH = REPO_ROOT / "indexes" / "latest.json"
MANIFEST_INDEX_PATH = REPO_ROOT / "indexes" / "manifest.json"


def main() -> None:
    assert CANONICAL_EXPORT_PATH.exists(), f"Missing canonical shard: {CANONICAL_EXPORT_PATH}"
    assert LATEST_INDEX_PATH.exists(), f"Missing latest index: {LATEST_INDEX_PATH}"
    assert MANIFEST_INDEX_PATH.exists(), f"Missing manifest index: {MANIFEST_INDEX_PATH}"

    rows = []
    with CANONICAL_EXPORT_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    latest = json.loads(LATEST_INDEX_PATH.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST_INDEX_PATH.read_text(encoding="utf-8"))

    assert rows, "Canonical shard is empty"
    assert latest["row_count"] == len(rows), "latest.json row_count mismatch"
    assert manifest["config"] == "training_rows", "manifest.json config mismatch"
    assert manifest["split"] == "train", "manifest.json split mismatch"
    assert manifest["files"][0] == "data/training_rows/train-00000-of-00001.jsonl"
    print(f"Validated {len(rows)} staged dataset rows.")


if __name__ == "__main__":
    main()
