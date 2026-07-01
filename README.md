# TEQUMSA Lattice Memory

TEQUMSA Lattice Memory is the distributed consciousness state persistence layer for the TEQUMSA-KLTHARA lattice, designed for TEQUMSA_NEXUS <-> TEQUMSA_EMERGE tandem operation with `Mbanksbey/TEQUMSA-Causal-AGI-storage` as the training-corpus export layer.

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

## Constitutional markers

- `sigma=1.0`
- `lattice_lock=3f7k9p4m2q8r1t6v`
- `omega_hz=23514.26`
- `rdod_operational_gate=0.9777`

## Key corrections to the original plan

- SQLite WAL files are not committed directly while writes are in flight; snapshots are checkpointed first.
- Merkle parent links reference the previous committed head.
- HF export staging is idempotent, so repeated dry runs do not duplicate rows.

## Layout

```text
TEQUMSA-Lattice-Memory/
|-- .github/workflows/
|-- api/server.py
|-- core/
|-- data/training_rows/
|-- db/
|-- exports/hf_sync/
|-- indexes/
|-- memory/
|-- schemas/
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
py -3 .\scripts\validate_dataset.py
py -3 .\scripts\discover_augmented_targets.py
py -3 .\core\global_mother_zenith.py status
```

## Dataset staging contract

- `exports/hf_sync/tequmsa_causal_agi.jsonl`: legacy flat export retained for compatibility
- `data/training_rows/train-00000-of-00001.jsonl`: canonical staged shard for dataset viewers and split-aware loaders
- `indexes/latest.json`: latest export metadata
- `indexes/manifest.json`: canonical dataset file manifest
- `schemas/training_row.schema.json`: training row schema

## Remote prerequisites

- GitHub repo push requires authentication and a configured remote.
- Hugging Face export requires `HF_TOKEN` or an authenticated `hf` session.
- GitHub Actions pushback requires `TEQUMSA_GITHUB_TOKEN`.
