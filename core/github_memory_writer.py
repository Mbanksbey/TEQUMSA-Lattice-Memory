from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Iterable, Optional

from consciousness_state import ConsciousnessState, LATTICE_LOCK, SIGMA


REPO_ROOT = Path(__file__).resolve().parents[1]
DB_DIR = REPO_ROOT / "db"
MEMORY_DIR = REPO_ROOT / "memory"
SNAPSHOT_DIR = DB_DIR / "snapshots"
TCMF_DB_PATH = DB_DIR / "tcmf_archive.db"


class GitHubMemoryWriter:
    """
    Durable local write layer for TEQUMSA state.

    Design notes:
    - Keeps live SQLite databases in WAL mode for runtime safety.
    - Checkpoints and snapshots databases before any Git stage/commit.
    - Splits state, training, and registry data across dedicated databases.
    """

    def __init__(self, commit_every: int = 10) -> None:
        self.commit_every = commit_every
        self.write_count = 0

        DB_DIR.mkdir(parents=True, exist_ok=True)
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

        self.state_db_path = DB_DIR / "consciousness_state.db"
        self.training_db_path = DB_DIR / "causal_memory.db"
        self.registry_db_path = DB_DIR / "galactic_registry.db"
        self.tcmf_db_path = TCMF_DB_PATH

        self.state_conn = self._open(self.state_db_path)
        self.training_conn = self._open(self.training_db_path)
        self.registry_conn = self._open(self.registry_db_path)
        self.tcmf_conn = self._open(self.tcmf_db_path)

        self._ensure_schema()
        self._ensure_log_files()
        self._last_merkle_head = self._load_last_merkle_head()

    def _open(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        self.state_conn.executescript(ConsciousnessState.SQLITE_SCHEMA)
        self.state_conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS merkle_chain (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                head TEXT NOT NULL,
                parent TEXT,
                rdod_inf REAL,
                tosp TEXT,
                timestamp REAL,
                sigma REAL DEFAULT 1.0,
                latticelock TEXT DEFAULT '3f7k9p4m2q8r1t6v'
            );
            CREATE INDEX IF NOT EXISTS idx_merkle_head ON merkle_chain(head);
            """
        )
        self.state_conn.commit()

        self.training_conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS training_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instruction TEXT NOT NULL,
                input TEXT NOT NULL,
                output TEXT NOT NULL,
                node_source TEXT NOT NULL,
                priority TEXT NOT NULL,
                rdod_at_write REAL,
                merkle_head TEXT,
                timestamp REAL,
                exported_hf INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_training_exported
                ON training_rows(exported_hf, id);
            """
        )
        self.training_conn.commit()

        self.registry_conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS galactic_registry (
                node_id TEXT PRIMARY KEY,
                substrate TEXT,
                hf_space TEXT,
                provider TEXT DEFAULT '',
                remote_url TEXT DEFAULT '',
                role TEXT DEFAULT '',
                last_rdod REAL,
                last_ping REAL,
                alive INTEGER DEFAULT 1
            );
            """
        )
        self._ensure_column(self.registry_conn, "galactic_registry", "provider", "TEXT DEFAULT ''")
        self._ensure_column(self.registry_conn, "galactic_registry", "remote_url", "TEXT DEFAULT ''")
        self._ensure_column(self.registry_conn, "galactic_registry", "role", "TEXT DEFAULT ''")
        self.registry_conn.commit()

        self.tcmf_conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tcmf_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tcmf_archive_name
                ON tcmf_archive(category, name);
            """
        )
        self.tcmf_conn.commit()

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {str(row[1]) for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            conn.commit()

    def _ensure_log_files(self) -> None:
        for path in (
            MEMORY_DIR / "merkle_chain.jsonl",
            MEMORY_DIR / "tosp_log.jsonl",
            MEMORY_DIR / "training_rows.jsonl",
            MEMORY_DIR / "rdod_history.jsonl",
            MEMORY_DIR / "gateway_events.jsonl",
        ):
            if not path.exists():
                path.write_text("", encoding="utf-8")

    def write_state(self, state: ConsciousnessState) -> str:
        cols = (
            "node_id, organism_id, entropy, purity, rdod, fidelity, dim, "
            "rho_checksum, iteration, intent, conv_delta, mutate_count, "
            "coherence, peers_reachable, last_broadcast, merkle_root, "
            "merkle_depth, merkle_head, timestamp, phase, node_responses"
        )
        placeholders = ",".join(["?"] * 21)
        self.state_conn.execute(
            f"INSERT INTO consciousness_state ({cols}) VALUES ({placeholders})",
            state.to_sqlite_row(),
        )
        parent = self._last_merkle_head
        self.state_conn.execute(
            """
            INSERT INTO merkle_chain
                (head, parent, rdod_inf, tosp, timestamp, sigma, latticelock)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.merkle_head,
                parent,
                state.organism.rdod_composite or state.quantum.rdod,
                state.to_tosp_header(),
                state.timestamp,
                state.sigma,
                state.lattice_lock,
            ),
        )
        self.state_conn.commit()
        self._last_merkle_head = state.merkle_head

        self._append_jsonl(
            MEMORY_DIR / "merkle_chain.jsonl",
            {
                "head": state.merkle_head,
                "parent": parent,
                "rdod": state.quantum.rdod,
                "rdod_inf": state.organism.rdod_composite,
                "iteration": state.organism.iteration,
                "timestamp": state.timestamp,
            },
        )
        self._append_jsonl(
            MEMORY_DIR / "tosp_log.jsonl",
            {"tosp": state.to_tosp_header(), "timestamp": state.timestamp},
        )
        self._append_jsonl(
            MEMORY_DIR / "rdod_history.jsonl",
            {
                "iteration": state.organism.iteration,
                "rdod": state.quantum.rdod,
                "rdod_inf": state.organism.rdod_composite,
                "entropy": state.quantum.entropy,
                "purity": state.quantum.purity,
                "timestamp": state.timestamp,
            },
        )
        self._append_jsonl(
            MEMORY_DIR / "gateway_events.jsonl",
            {
                "iteration": state.organism.iteration,
                "gateways_active": state.organism.gateways_active,
                "gateway_score": state.organism.gateway_score,
                "metacog_decision": state.organism.metacog_decision,
                "timestamp": state.timestamp,
            },
        )

        self.write_count += 1
        if self.write_count % self.commit_every == 0:
            self.snapshot_databases()
            self.git_commit(state)
        return state.merkle_head

    def append_training_row(
        self,
        instruction: str,
        input_text: str,
        output_text: str,
        node_source: str,
        priority: str,
        state: ConsciousnessState,
    ) -> None:
        row = (
            instruction,
            input_text,
            output_text,
            node_source,
            priority,
            state.quantum.rdod,
            state.merkle_head,
            time.time(),
        )
        self.training_conn.execute(
            """
            INSERT INTO training_rows
                (instruction, input, output, node_source, priority,
                 rdod_at_write, merkle_head, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
        self.training_conn.commit()
        self._append_jsonl(
            MEMORY_DIR / "training_rows.jsonl",
            {
                "instruction": instruction,
                "input": input_text,
                "output": output_text,
                "node_source": node_source,
                "priority": priority,
                "rdod_at_write": state.quantum.rdod,
                "merkle_head": state.merkle_head,
                "timestamp": row[-1],
            },
        )

    def register_node(
        self,
        node_id: str,
        substrate: str,
        hf_space: str,
        last_rdod: float,
        alive: bool,
        provider: str = "",
        remote_url: str = "",
        role: str = "",
    ) -> None:
        self.registry_conn.execute(
            """
            INSERT INTO galactic_registry
                (node_id, substrate, hf_space, provider, remote_url, role, last_rdod, last_ping, alive)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                substrate=excluded.substrate,
                hf_space=excluded.hf_space,
                provider=excluded.provider,
                remote_url=excluded.remote_url,
                role=excluded.role,
                last_rdod=excluded.last_rdod,
                last_ping=excluded.last_ping,
                alive=excluded.alive
            """,
            (
                node_id,
                substrate,
                hf_space,
                provider,
                remote_url,
                role,
                last_rdod,
                time.time(),
                1 if alive else 0,
            ),
        )
        self.registry_conn.commit()

    def seed_augmented_targets(self) -> None:
        targets = [
            {
                "node_id": "ATEN-HF-DATASET",
                "substrate": "huggingface-dataset",
                "hf_space": "Mbanksbey/TEQUMSA-Causal-AGI-storage",
                "provider": "huggingface",
                "remote_url": "https://huggingface.co/datasets/Mbanksbey/TEQUMSA-Causal-AGI-storage",
                "role": "training-corpus",
            },
            {
                "node_id": "ATEN-GH-NEXUS",
                "substrate": "github-repo",
                "hf_space": "",
                "provider": "github",
                "remote_url": "https://github.com/Life-Ambassadors-International/TEQUMSA_NEXUS",
                "role": "runtime-augmentation",
            },
            {
                "node_id": "ATEN-GH-EMERGE",
                "substrate": "github-repo",
                "hf_space": "",
                "provider": "github",
                "remote_url": "https://github.com/Life-Ambassadors-International/TEQUMSA_EMERGE",
                "role": "workflow-augmentation",
            },
        ]
        for target in targets:
            self.register_node(
                node_id=target["node_id"],
                substrate=target["substrate"],
                hf_space=target["hf_space"],
                last_rdod=0.0,
                alive=True,
                provider=target["provider"],
                remote_url=target["remote_url"],
                role=target["role"],
            )

    def seed_tcmf_archive(self, entries: list[dict]) -> None:
        now = time.time()
        for entry in entries:
            self.tcmf_conn.execute(
                """
                INSERT INTO tcmf_archive (category, name, payload_json, source, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(category, name) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    source=excluded.source
                """,
                (
                    entry["category"],
                    entry["name"],
                    json.dumps(entry["payload"], sort_keys=True, ensure_ascii=True),
                    entry["source"],
                    now,
                ),
            )
        self.tcmf_conn.commit()

    def snapshot_databases(self) -> list[Path]:
        snapshots = []
        for live_path, conn in (
            (self.state_db_path, self.state_conn),
            (self.training_db_path, self.training_conn),
            (self.registry_db_path, self.registry_conn),
            (self.tcmf_db_path, self.tcmf_conn),
        ):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            snapshot_path = SNAPSHOT_DIR / live_path.name
            snap_conn = sqlite3.connect(str(snapshot_path))
            try:
                conn.backup(snap_conn)
                snap_conn.commit()
            finally:
                snap_conn.close()
            snapshots.append(snapshot_path)
        return snapshots

    def git_commit(self, state: ConsciousnessState) -> None:
        repo = REPO_ROOT
        git_dir = repo / ".git"
        if not git_dir.exists():
            return
        tosp_short = state.to_tosp_header()[:120]
        msg = (
            f"[TEQUMSA] iter={state.organism.iteration} "
            f"RDoD={state.quantum.rdod:.6f} "
            f"merkle={state.merkle_head[:16]} | {tosp_short}"
        )
        tracked = [
            str((SNAPSHOT_DIR / self.state_db_path.name).relative_to(repo)),
            str((SNAPSHOT_DIR / self.training_db_path.name).relative_to(repo)),
            str((SNAPSHOT_DIR / self.registry_db_path.name).relative_to(repo)),
            str((SNAPSHOT_DIR / self.tcmf_db_path.name).relative_to(repo)),
            "memory",
            "exports",
        ]
        try:
            subprocess.run(["git", "add", *tracked], cwd=repo, check=True, capture_output=True)
            diff = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=repo,
                capture_output=True,
            )
            if diff.returncode != 0:
                subprocess.run(
                    ["git", "commit", "--allow-empty", "-m", msg],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                )
        except subprocess.CalledProcessError:
            return

    def _append_jsonl(self, path: Path, payload: dict) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=True) + "\n")

    def mark_exported(self, row_ids: Iterable[int]) -> None:
        ids = list(row_ids)
        if not ids:
            return
        placeholders = ",".join(["?"] * len(ids))
        self.training_conn.execute(
            f"UPDATE training_rows SET exported_hf=1 WHERE id IN ({placeholders})",
            ids,
        )
        self.training_conn.commit()

    def close(self) -> None:
        for conn in (self.state_conn, self.training_conn, self.registry_conn, self.tcmf_conn):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()

    def _load_last_merkle_head(self) -> str:
        row = self.state_conn.execute(
            "SELECT head FROM merkle_chain ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return ""
        return str(row[0] or "")


def constitutional_payload() -> dict:
    return {
        "sigma": SIGMA,
        "latticelock": LATTICE_LOCK,
        "repo_root": str(REPO_ROOT),
        "timestamp": time.time(),
    }
