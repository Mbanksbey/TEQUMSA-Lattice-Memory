from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from github_memory_writer import GitHubMemoryWriter
from global_mother_zenith import CIVILIZATIONS, CYCLES, FREQUENCIES, GlobalMotherZenith


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap TEQUMSA lattice memory from Zenith pulses")
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--dim", type=int, default=144)
    parser.add_argument("--rank", type=int, default=64)
    parser.add_argument("--attractor-k", type=int, default=7)
    parser.add_argument("--append-training-row", action="store_true")
    args = parser.parse_args()

    writer = GitHubMemoryWriter(commit_every=max(args.cycles, 1))
    kernel = GlobalMotherZenith(
        dim=args.dim,
        rank=args.rank,
        attractor_k=args.attractor_k,
        db_path=str(REPO_ROOT / "db" / "bootstrap_state.db"),
        node_id="ATEN0-ZENITH",
    )

    last = None
    try:
        writer.seed_augmented_targets()
        writer.seed_tcmf_archive(
            [
                {
                    "category": "civilization",
                    "name": name,
                    "payload": {"era_bya": era, "frequency_hz": hz, "contribution": contribution},
                    "source": "global_mother_zenith.py",
                }
                for name, era, hz, contribution in CIVILIZATIONS
            ]
            + [
                {
                    "category": "cycle",
                    "name": str(num),
                    "payload": {
                        "bya": bya,
                        "label": name,
                        "shards": shards,
                        "syntropic": syntropic,
                        "failure_mode": failure_mode,
                    },
                    "source": "global_mother_zenith.py",
                }
                for num, bya, name, shards, syntropic, failure_mode in CYCLES
            ]
            + [
                {
                    "category": "frequency",
                    "name": name,
                    "payload": {"hz": values[0], "role": values[1], "substrate": values[2]},
                    "source": "global_mother_zenith.py",
                }
                for name, values in FREQUENCIES.items()
            ]
        )
        for _ in range(args.cycles):
            last = kernel.pulse()
            writer.write_state(kernel.state)

        if args.append_training_row and kernel.state.merkle_head:
            writer.append_training_row(
                instruction="Summarize current lattice state",
                input_text="Report local Zenith convergence state.",
                output_text=kernel.state.summary(),
                node_source=kernel.state.node_id,
                priority="high",
                state=kernel.state,
            )

        writer.register_node(
            node_id=kernel.state.node_id,
            substrate="local-python",
            hf_space="",
            last_rdod=kernel.state.quantum.rdod,
            alive=True,
            provider="local",
            remote_url="file://local-kernel",
            role="zenith-bootstrap",
        )
        writer.snapshot_databases()
    finally:
        kernel.finalize()
        writer.close()

    print(f"Bootstrapped {args.cycles} cycles into {REPO_ROOT / 'db'}")
    if last:
        print(
            f"Final: cycle={last['cycle']} S={last['S']:.4f} P={last['P']:.6f} "
            f"RDoD_inf={last['RDoD_inf']:.6f} merkle_depth={kernel.state.merkle_depth}"
        )


if __name__ == "__main__":
    main()
