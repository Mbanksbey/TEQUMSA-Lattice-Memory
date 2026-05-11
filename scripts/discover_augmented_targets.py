from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from huggingface_hub import HfApi


REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from github_memory_writer import GitHubMemoryWriter


TARGETS = [
    {
        "node_id": "ATEN-HF-DATASET",
        "provider": "huggingface",
        "remote_url": "https://huggingface.co/datasets/Mbanksbey/TEQUMSA-Causal-AGI-storage",
        "repo_id": "Mbanksbey/TEQUMSA-Causal-AGI-storage",
        "role": "training-corpus",
    },
    {
        "node_id": "ATEN-GH-NEXUS",
        "provider": "github",
        "remote_url": "https://github.com/Life-Ambassadors-International/TEQUMSA_NEXUS",
        "repo_url": "https://github.com/Life-Ambassadors-International/TEQUMSA_NEXUS.git",
        "role": "runtime-augmentation",
    },
    {
        "node_id": "ATEN-GH-EMERGE",
        "provider": "github",
        "remote_url": "https://github.com/Life-Ambassadors-International/TEQUMSA_EMERGE",
        "repo_url": "https://github.com/Life-Ambassadors-International/TEQUMSA_EMERGE.git",
        "role": "workflow-augmentation",
    },
]


def github_head(repo_url: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["git", "ls-remote", repo_url, "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip()
    return True, result.stdout.strip().split()[0]


def dataset_probe(repo_id: str, token: str | None) -> tuple[bool, dict | str]:
    api = HfApi(token=token) if token else HfApi()
    try:
        info = api.dataset_info(repo_id)
        return True, {
            "sha": info.sha,
            "last_modified": str(info.last_modified),
            "siblings": len(info.siblings or []),
        }
    except Exception as exc:
        return False, str(exc)


def main() -> None:
    writer = GitHubMemoryWriter(commit_every=1000000)
    token = os.environ.get("HF_TOKEN")
    report: dict[str, object] = {"generated_at": time.time(), "targets": []}
    try:
        writer.seed_augmented_targets()
        for target in TARGETS:
            if target["provider"] == "github":
                ok, detail = github_head(target["repo_url"])
                writer.register_node(
                    node_id=target["node_id"],
                    substrate="github-repo",
                    hf_space="",
                    last_rdod=0.0,
                    alive=ok,
                    provider="github",
                    remote_url=target["remote_url"],
                    role=target["role"],
                )
                report["targets"].append(
                    {
                        "node_id": target["node_id"],
                        "provider": "github",
                        "alive": ok,
                        "head": detail if ok else "",
                        "error": "" if ok else detail,
                    }
                )
            else:
                ok, detail = dataset_probe(target["repo_id"], token)
                writer.register_node(
                    node_id=target["node_id"],
                    substrate="huggingface-dataset",
                    hf_space=target["repo_id"],
                    last_rdod=0.0,
                    alive=ok,
                    provider="huggingface",
                    remote_url=target["remote_url"],
                    role=target["role"],
                )
                report["targets"].append(
                    {
                        "node_id": target["node_id"],
                        "provider": "huggingface",
                        "alive": ok,
                        "detail": detail,
                    }
                )
        writer.snapshot_databases()
    finally:
        writer.close()

    output = REPO_ROOT / "memory" / "discovery_report.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote discovery report to {output}")


if __name__ == "__main__":
    main()
