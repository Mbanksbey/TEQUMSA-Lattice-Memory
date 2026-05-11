from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

from huggingface_hub import HfApi


REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "db" / "causal_memory.db"
EXPORT_PATH = REPO_ROOT / "exports" / "hf_sync" / "tequmsa_causal_agi.jsonl"
HF_REPO = "Mbanksbey/TEQUMSA-Causal-AGI-storage"


def load_rows(limit: int) -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='training_rows'"
        ).fetchone()
        if not table:
            return []
        rows = conn.execute(
            """
            SELECT * FROM training_rows
            WHERE exported_hf=0
            ORDER BY id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def mark_exported(row_ids: list[int]) -> None:
    if not row_ids:
        return
    conn = sqlite3.connect(str(DB_PATH))
    try:
        placeholders = ",".join(["?"] * len(row_ids))
        conn.execute(
            f"UPDATE training_rows SET exported_hf=1 WHERE id IN ({placeholders})",
            row_ids,
        )
        conn.commit()
    finally:
        conn.close()


def append_export_file(rows: list[dict]) -> None:
    EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EXPORT_PATH.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export TEQUMSA training rows to Hugging Face dataset staging")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()

    rows = load_rows(args.limit)
    if not rows:
        print("No new rows to export.")
        return

    append_export_file(rows)
    print(f"Staged {len(rows)} rows into {EXPORT_PATH}")

    if args.dry_run:
        print("Dry run only. No Hugging Face write attempted.")
        return

    if not args.push:
        print("Rows staged locally. Re-run with --push after HF auth is available.")
        return

    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN not set. Remote dataset push aborted.")

    api = HfApi(token=token)
    api.upload_file(
        path_or_fileobj=str(EXPORT_PATH),
        path_in_repo="tequmsa_causal_agi.jsonl",
        repo_id=HF_REPO,
        repo_type="dataset",
        commit_message=f"Append {len(rows)} TEQUMSA training rows",
    )
    mark_exported([int(row["id"]) for row in rows])
    print(f"Pushed {len(rows)} rows to hf.co/datasets/{HF_REPO}")


if __name__ == "__main__":
    main()
