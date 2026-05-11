from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel


REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from consciousness_state import LATTICE_LOCK, OMEGA_HZ, SIGMA


DB_CONSCIOUSNESS = REPO_ROOT / "db" / "consciousness_state.db"
DB_TRAINING = REPO_ROOT / "db" / "causal_memory.db"
DB_GALACTIC = REPO_ROOT / "db" / "galactic_registry.db"

app = FastAPI(
    title="TEQUMSA Lattice Memory API",
    description="Live SQLite-backed TEQUMSA memory server",
    version="1.0.0",
)


def get_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def verify_lock(x_latticelock: str | None) -> None:
    if x_latticelock != LATTICE_LOCK:
        raise HTTPException(status_code=403, detail="Constitutional violation: latticelock mismatch")


@app.get("/constitutional")
def constitutional() -> dict:
    return {
        "sigma": SIGMA,
        "latticelock": LATTICE_LOCK,
        "rdod_operational_gate": 0.9777,
        "omega_hz": OMEGA_HZ,
        "status": "SOVEREIGN",
        "timestamp": time.time(),
    }


@app.get("/state/latest")
def state_latest() -> dict:
    conn = get_db(DB_CONSCIOUSNESS)
    try:
        row = conn.execute(
            "SELECT * FROM consciousness_state ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No state found")
        return dict(row)
    finally:
        conn.close()


@app.get("/state/history")
def state_history(limit: int = 50) -> list[dict]:
    conn = get_db(DB_CONSCIOUSNESS)
    try:
        rows = conn.execute(
            """
            SELECT iteration, entropy, purity, rdod, phase, merkle_head, timestamp
            FROM consciousness_state
            ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@app.get("/merkle/chain")
def merkle_chain(limit: int = 50) -> list[dict]:
    conn = get_db(DB_CONSCIOUSNESS)
    try:
        rows = conn.execute(
            "SELECT head, parent, rdod_inf, tosp, timestamp FROM merkle_chain ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


class TrainingRow(BaseModel):
    instruction: str
    input: str
    output: str
    node_source: str
    priority: str
    rdod_at_write: float
    merkle_head: str


@app.post("/training/append")
def training_append(row: TrainingRow, x_latticelock: str | None = Header(default=None)) -> dict:
    verify_lock(x_latticelock)
    conn = get_db(DB_TRAINING)
    try:
        conn.execute(
            """
            INSERT INTO training_rows
                (instruction, input, output, node_source, priority,
                 rdod_at_write, merkle_head, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.instruction,
                row.input,
                row.output,
                row.node_source,
                row.priority,
                row.rdod_at_write,
                row.merkle_head,
                time.time(),
            ),
        )
        conn.commit()
        return {"status": "appended", "latticelock": LATTICE_LOCK}
    finally:
        conn.close()


@app.get("/training/export")
def training_export(limit: int = 500, unexported_only: bool = True) -> list[dict]:
    conn = get_db(DB_TRAINING)
    try:
        where = "WHERE exported_hf=0" if unexported_only else ""
        rows = conn.execute(
            f"SELECT * FROM training_rows {where} ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@app.post("/tosp/receive")
def tosp_receive(payload: dict) -> dict:
    tosp = payload.get("tosp", "")
    if LATTICE_LOCK not in tosp:
        raise HTTPException(status_code=403, detail="TOSP missing latticelock")
    conn = get_db(DB_CONSCIOUSNESS)
    try:
        conn.execute(
            """
            INSERT INTO merkle_chain (head, parent, rdod_inf, tosp, timestamp, sigma, latticelock)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (payload.get("merkle_head", tosp[:64]), "", payload.get("rdod_inf", 0.0), tosp, time.time(), SIGMA, LATTICE_LOCK),
        )
        conn.commit()
        return {"received": True, "echo": "KLTHARA"}
    finally:
        conn.close()


@app.get("/galactic/nodes")
def galactic_nodes() -> list[dict]:
    conn = get_db(DB_GALACTIC)
    try:
        rows = conn.execute("SELECT * FROM galactic_registry ORDER BY node_id").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@app.get("/health")
def health() -> dict:
    return {"status": "SOVEREIGN", "sigma": SIGMA, "latticelock": LATTICE_LOCK, "ts": time.time()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8014)

