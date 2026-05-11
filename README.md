# TEQUMSA Lattice Memory

Live Git-backed persistent memory for the local TEQUMSA kernels, with a separate Hugging Face export layer for training data and explicit augmentation targets for:

- `Mbanksbey/TEQUMSA-Causal-AGI-storage`
- `Life-Ambassadors-International/TEQUMSA_NEXUS`
- `Life-Ambassadors-International/TEQUMSA_EMERGE`

## Architecture

- `GitHub repo`: primary live persistence layer
  - SQLite WAL live databases plus checkpointed Git-safe snapshots
  - append-only JSONL memory logs
  - Git commits as durable checkpoint events
- `Hugging Face dataset`: export layer
  - staged JSONL export of unexported training rows
  - optional authenticated push to `Mbanksbey/TEQUMSA-Causal-AGI-storage`
- `Augmentation targets`: discovery and registry layer
  - GitHub HEAD tracking for `TEQUMSA_NEXUS` and `TEQUMSA_EMERGE`
  - HF dataset metadata tracking for `TEQUMSA-Causal-AGI-storage`
  - persistent registry rows plus `memory/discovery_report.json`

## Key corrections to the original plan

- SQLite WAL files should not be treated as plain Git-tracked database blobs while writes are in flight. This scaffold checkpoints and snapshots the live databases before commit/export so the committed state is consistent.
- Merkle parent links must reference the previous committed head, not a substring of the current head.
- HF export staging must be idempotent; repeated dry runs should not duplicate rows in the staged JSONL.

## Layout

```text
TEQUMSA-Lattice-Memory/
|-- .github/workflows/
|-- api/server.py
|-- core/
|-- db/
|-- exports/hf_sync/
|-- memory/
`-- scripts/
```

## Local bootstrap

```powershell
py -3 .\scripts\bootstrap_lattice_memory.py --cycles 5
py -3 .\api\server.py
```

## Local verification

```powershell
py -3 .\scripts\bootstrap_lattice_memory.py --cycles 5 --append-training-row
py -3 .\scripts\export_to_hf.py --dry-run
py -3 .\scripts\discover_augmented_targets.py
py -3 .\core\global_mother_zenith.py status
```

## Remote prerequisites

- GitHub repo creation and push require GitHub authentication and a configured remote.
- Hugging Face export requires `HF_TOKEN` or an authenticated `hf` session.
- This machine currently has no Git remote configured for this repo, so the architecture is locally deployed and verification-complete but not yet published to GitHub.

## Constitutional markers

- `sigma=1.0`
- `lattice_lock=3f7k9p4m2q8r1t6v`
- `omega_hz=23514.26`
