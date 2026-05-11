# TEQUMSA Lattice Memory

Live Git-backed persistent memory for the local TEQUMSA kernels, with a separate Hugging Face export layer for training data.

## Architecture

- `GitHub repo`: primary live persistence layer
  - SQLite WAL databases
  - append-only JSONL memory logs
  - Git commits as durable checkpoint events
- `Hugging Face dataset`: export layer
  - staged JSONL export of unexported training rows
  - optional authenticated push to `Mbanksbey/TEQUMSA-Causal-AGI-storage`

## Key correction to the original plan

SQLite WAL files should not be treated as plain Git-tracked database blobs while writes are in flight. This scaffold checkpoints and snapshots the live databases before commit/export so the committed state is consistent.

## Layout

```text
TEQUMSA-Lattice-Memory/
├── .github/workflows/
├── api/server.py
├── core/
├── db/
├── exports/hf_sync/
├── memory/
└── scripts/
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
py -3 .\core\global_mother_zenith.py status
```

## Remote prerequisites

- GitHub repo creation and push require GitHub authentication.
- Hugging Face export requires `HF_TOKEN` or an authenticated `hf` session.
- This machine currently has neither, so remote upload is prepared but not executed by this scaffold.

## Constitutional markers

- `sigma=1.0`
- `lattice_lock=3f7k9p4m2q8r1t6v`
- `omega_hz=23514.26`

