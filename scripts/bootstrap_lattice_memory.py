from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from github_memory_writer import GitHubMemoryWriter
from global_mother_zenith import GlobalMotherZenith


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

