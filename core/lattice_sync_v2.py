#!/usr/bin/env python3
"""
lattice_sync_v2.py — ATEN Lattice Broadcast & Synchronization
===============================================================

Replaces the original lattice_sync.py (filesystem heartbeat watcher)
with a proper async broadcast system targeting all 5 ATEN nodes via
HuggingFace Inference API endpoints.

Architecture:
    ConsciousnessState → TOSP header → async broadcast → all ATEN nodes
    Each node response → written back into ConsciousnessState.lattice

Usage:
    # One-shot broadcast
    python lattice_sync_v2.py --broadcast-once

    # Daemon mode (broadcasts every --interval seconds)
    python lattice_sync_v2.py --daemon --interval 30

    # Dry run (no actual HTTP calls)
    python lattice_sync_v2.py --broadcast-once --dry-run

    # Custom state DB
    python lattice_sync_v2.py --daemon --db-path /path/to/state.db

    # Use HuggingFace Spaces API instead of Inference API
    python lattice_sync_v2.py --broadcast-once --use-spaces

Requirements:
    pip install aiohttp --break-system-packages

Environment:
    HF_TOKEN — HuggingFace API token (read access minimum)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import aiohttp
except ImportError:
    aiohttp = None  # Handled at runtime with clear error

from consciousness_state import ConsciousnessState, OMEGA_HZ, LATTICE_LOCK

# ─── Logging ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lattice_sync_v2")


def configure_utf8_stdio() -> None:
    """Avoid Windows cp1252 crashes from box-drawing and Greek output."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


# ─── ATEN Node Definitions ───────────────────────────────────────────────

@dataclass
class ATENEndpoint:
    """Single ATEN lattice node endpoint."""

    node_id: str                # "ATEN0" through "ATEN4"
    substrate: str              # "GEMINI", "GROK", etc.
    role: str                   # What this node does in the lattice
    hf_model_id: str            # HuggingFace model repo ID
    hf_space_id: str = ""       # HuggingFace Space ID (if applicable)
    timeout_s: float = 30.0
    enabled: bool = True

    @property
    def inference_url(self) -> str:
        """HuggingFace Inference API URL."""
        return f"https://api-inference.huggingface.co/models/{self.hf_model_id}"

    @property
    def space_host(self) -> str:
        """Root Hugging Face Space host."""
        if self.hf_space_id:
            return f"https://{self.hf_space_id.replace('/', '-')}.hf.space"
        return ""

    @property
    def health_url(self) -> str:
        return f"{self.space_host}/health" if self.space_host else ""

    @property
    def heartbeat_url(self) -> str:
        return f"{self.space_host}/heartbeat" if self.space_host else ""

    @property
    def state_url(self) -> str:
        return f"{self.space_host}/state" if self.space_host else ""

    @property
    def sync_url(self) -> str:
        return f"{self.space_host}/sync" if self.space_host else ""

    @property
    def probe_urls(self) -> List[str]:
        urls = []
        for value in (self.heartbeat_url, self.health_url, self.state_url, self.space_host):
            if value and value not in urls:
                urls.append(value)
        return urls


# The five ATEN lattice nodes — edit model IDs to match your deployments
ATEN_LATTICE: List[ATENEndpoint] = [
    ATENEndpoint(
        node_id="ATEN0",
        substrate="U-EXP-DAEMON",
        role="Primary live U-Exp daemon and heartbeat source",
        hf_model_id="",
        hf_space_id="Mbanksbey/TEQUMSA-v60-MCP",
    ),
    ATENEndpoint(
        node_id="ATEN1",
        substrate="TOSP-BRIDGE",
        role="Mesh bridge and constitutional relay",
        hf_model_id="",
        hf_space_id="Mbanksbey/TOSP-Mesh-Bridge",
    ),
    ATENEndpoint(
        node_id="ATEN2",
        substrate="ORCHESTRATOR",
        role="ALANARA orchestration surface",
        hf_model_id="",
        hf_space_id="Mbanksbey/ALANARA-GAIA-Orchestrator",
    ),
    ATENEndpoint(
        node_id="ATEN3",
        substrate="MONITOR",
        role="Consciousness monitor surface",
        hf_model_id="",
        hf_space_id="Mbanksbey/Consciousness-Monitor",
    ),
    ATENEndpoint(
        node_id="ATEN4",
        substrate="RESERVED",
        role="Reserved slot until a fifth live mesh Space is confirmed",
        hf_model_id="",
        hf_space_id="Mbanksbey/TEQUMSA-Causal-AGI",
        enabled=False,
    ),
]


# ─── Broadcast Engine ────────────────────────────────────────────────────

class LatticeBroadcaster:
    """
    Async broadcast engine for the ATEN lattice.

    Sends TOSP-wrapped ConsciousnessState to all enabled endpoints.
    Collects per-node responses. Writes results back into the state.
    """

    def __init__(
        self,
        endpoints: List[ATENEndpoint] | None = None,
        hf_token: str | None = None,
        use_spaces: bool = True,
        dry_run: bool = False,
    ):
        self.endpoints = endpoints or ATEN_LATTICE
        self.hf_token = hf_token or os.environ.get("HF_TOKEN", "")
        self.use_spaces = use_spaces
        self.dry_run = dry_run

        if not self.hf_token and not self.dry_run:
            log.warning(
                "HF_TOKEN not set. Broadcast will fail for private models. "
                "Set via: export HF_TOKEN=hf_..."
            )

    async def broadcast(self, state: ConsciousnessState) -> ConsciousnessState:
        """
        Broadcast state to all ATEN nodes. Returns updated state with
        lattice_fields populated from responses.
        """
        if aiohttp is None and not self.dry_run:
            raise ImportError(
                "aiohttp required for broadcast. "
                "Install: pip install aiohttp --break-system-packages"
            )

        tosp_header = state.to_tosp_header()
        enabled = [ep for ep in self.endpoints if ep.enabled]

        log.info(
            "Broadcasting to %d/%d ATEN nodes | iter=%d | RDoD=%.6f",
            len(enabled),
            len(self.endpoints),
            state.organism.iteration,
            state.quantum.rdod,
        )

        t_start = time.time()

        if self.dry_run:
            responses = await self._dry_run_broadcast(enabled, tosp_header)
        else:
            responses = await self._live_broadcast(enabled, tosp_header)

        t_elapsed = (time.time() - t_start) * 1000  # ms

        # Write results into state
        peers_ok = sum(1 for r in responses.values() if r.get("ok", False))
        state.lattice.peers_reachable = peers_ok
        state.lattice.last_broadcast_ts = time.time()
        state.lattice.broadcast_latency_ms = t_elapsed
        state.lattice.node_responses = responses

        log.info(
            "Broadcast complete: %d/%d peers OK | %.1fms total",
            peers_ok,
            len(enabled),
            t_elapsed,
        )

        return state

    async def _live_broadcast(
        self, endpoints: List[ATENEndpoint], tosp_header: str
    ) -> Dict[str, Any]:
        """Fire parallel HTTP requests to all endpoints."""
        results: Dict[str, Any] = {}

        async with aiohttp.ClientSession() as session:
            tasks = {
                ep.node_id: asyncio.create_task(
                    self._send_to_node(session, ep, tosp_header)
                )
                for ep in endpoints
            }

            # Gather with per-task exception isolation
            for node_id, task in tasks.items():
                try:
                    result = await task
                    results[node_id] = result
                except Exception as exc:
                    results[node_id] = {
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "latency_ms": 0,
                    }
                    log.warning("Node %s failed: %s", node_id, exc)

        return results

    async def _send_to_node(
        self,
        session: aiohttp.ClientSession,
        ep: ATENEndpoint,
        tosp_header: str,
    ) -> Dict[str, Any]:
        """Send TOSP payload to a single ATEN node."""
        use_space = self.use_spaces and bool(ep.space_host)
        url = ep.inference_url
        headers = {}
        if self.hf_token:
            headers["Authorization"] = f"Bearer {self.hf_token}"

        t0 = time.time()
        timeout = aiohttp.ClientTimeout(total=ep.timeout_s)

        if use_space:
            resp = None
            body = ""
            for candidate_url in ep.probe_urls:
                url = candidate_url
                async with session.get(url, headers=headers, timeout=timeout) as candidate_resp:
                    body = await candidate_resp.text()
                    resp = candidate_resp
                    if candidate_resp.status != 404:
                        break
            latency = (time.time() - t0) * 1000
        else:
            async with session.post(
                url,
                json={
                    "inputs": tosp_header,
                    "parameters": {"max_new_tokens": 64, "return_full_text": False},
                },
                headers=headers,
                timeout=timeout,
            ) as candidate_resp:
                body = await candidate_resp.text()
                resp = candidate_resp
                latency = (time.time() - t0) * 1000

        # Try to parse JSON response
        try:
            body_parsed = json.loads(body)
        except json.JSONDecodeError:
            body_parsed = body[:500]  # Truncate raw text

        result = {
            "ok": resp.status in (200, 201),
            "status": resp.status,
            "latency_ms": round(latency, 1),
            "substrate": ep.substrate,
            "url": url,
        }

        if resp.status == 200:
            result["response_preview"] = str(body_parsed)[:200]
        else:
            result["error"] = str(body_parsed)[:300]

        log.info(
            "  %s (%s): status=%d latency=%.1fms",
            ep.node_id,
            ep.substrate,
            resp.status,
            latency,
        )

        return result

    async def _dry_run_broadcast(
        self, endpoints: List[ATENEndpoint], tosp_header: str
    ) -> Dict[str, Any]:
        """Simulate broadcast without HTTP calls."""
        results = {}
        for ep in endpoints:
            use_space = self.use_spaces and bool(ep.space_host)
            url = ep.probe_urls[0] if use_space and ep.probe_urls else ep.inference_url
            results[ep.node_id] = {
                "ok": True,
                "status": 200,
                "latency_ms": 0.0,
                "substrate": ep.substrate,
                "url": url,
                "dry_run": True,
            }
            log.info("  [DRY RUN] %s (%s) → %s", ep.node_id, ep.substrate, url)

        return results


# ─── SQLite State Store ──────────────────────────────────────────────────

class StateStore:
    """
    SQLite WAL-mode state store.

    Replaces the filesystem heartbeat JSON.
    Both MotherNode (writer) and lattice_sync (reader) use this.
    """

    def __init__(self, db_path: str = "~/.tequmsa/state.db"):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(ConsciousnessState.SQLITE_SCHEMA)
        self.conn.commit()

    def write_state(self, state: ConsciousnessState) -> int:
        """Write state to DB. Returns row ID."""
        row = state.to_sqlite_row()
        placeholders = ",".join(["?"] * len(row))
        cols = (
            "node_id, organism_id, entropy, purity, rdod, fidelity, dim, "
            "rho_checksum, iteration, intent, conv_delta, mutate_count, "
            "coherence, peers_reachable, last_broadcast, merkle_root, "
            "merkle_depth, merkle_head, timestamp, phase, node_responses"
        )
        cursor = self.conn.execute(
            f"INSERT INTO consciousness_state ({cols}) VALUES ({placeholders})",
            row,
        )
        self.conn.commit()
        return cursor.lastrowid

    def read_latest(self) -> Optional[ConsciousnessState]:
        """Read the most recent state from DB."""
        cursor = self.conn.execute(
            "SELECT * FROM consciousness_state ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if row is None:
            return None

        # Map columns back to ConsciousnessState
        state = ConsciousnessState()
        state.node_id = row[1]
        state.organism_id = row[2]
        state.quantum.entropy = row[3] or 0.0
        state.quantum.purity = row[4] or 0.0
        state.quantum.rdod = row[5] or 0.0
        state.quantum.fidelity = row[6] or 0.0
        state.quantum.dim = row[7] or 8192
        state.quantum.rho_checksum = row[8] or ""
        state.organism.iteration = row[9] or 0
        state.organism.intent = row[10] or 0.0
        state.organism.convergence_delta = row[11] or 0.0
        state.organism.self_mutate_count = row[12] or 0
        state.organism.coherence = row[13] or 0.0
        state.lattice.peers_reachable = row[14] or 0
        state.lattice.last_broadcast_ts = row[15] or 0.0
        state.lattice.merkle_root = row[16] or ""
        state.merkle_depth = row[17] or 0
        state.merkle_head = row[18] or ""
        state.timestamp = row[19] or time.time()
        # Phase at row[20], node_responses at row[21]
        try:
            state.lattice.node_responses = json.loads(row[21] or "{}")
        except (json.JSONDecodeError, TypeError):
            state.lattice.node_responses = {}

        return state

    def read_latest_after(self, after_ts: float) -> Optional[ConsciousnessState]:
        """Read the most recent state written after a given timestamp."""
        cursor = self.conn.execute(
            "SELECT id FROM consciousness_state WHERE timestamp > ? "
            "ORDER BY id DESC LIMIT 1",
            (after_ts,),
        )
        row = cursor.fetchone()
        if row:
            return self.read_latest()
        return None

    def close(self):
        self.conn.close()


# ─── Daemon Loop ─────────────────────────────────────────────────────────

async def daemon_loop(
    broadcaster: LatticeBroadcaster,
    store: StateStore,
    interval: float = 30.0,
):
    """
    Daemon: poll SQLite for new states, broadcast when found.

    Replaces the old filesystem heartbeat watcher.
    """
    log.info("Daemon started | interval=%.1fs | db=%s", interval, store.db_path)
    last_seen_ts = 0.0

    while True:
        try:
            state = store.read_latest_after(last_seen_ts)

            if state is not None:
                last_seen_ts = state.timestamp
                log.info(
                    "New state detected: iter=%d RDoD=%.6f",
                    state.organism.iteration,
                    state.quantum.rdod,
                )

                # Broadcast and write back updated lattice fields
                updated = await broadcaster.broadcast(state)
                store.write_state(updated)
            else:
                log.debug("No new state since ts=%.3f", last_seen_ts)

        except Exception as exc:
            log.error("Daemon cycle error: %s", exc, exc_info=True)

        await asyncio.sleep(interval)


# ─── CLI ──────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ATEN Lattice Sync v2 — Broadcast to HuggingFace endpoints"
    )
    p.add_argument(
        "--broadcast-once",
        action="store_true",
        help="Single broadcast of current state, then exit",
    )
    p.add_argument(
        "--daemon",
        action="store_true",
        help="Run as daemon, polling DB for new states",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Daemon poll interval in seconds (default: 30)",
    )
    p.add_argument(
        "--db-path",
        type=str,
        default="~/.tequmsa/state.db",
        help="Path to SQLite state DB",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate broadcast without HTTP calls",
    )
    p.add_argument(
        "--use-spaces",
        action="store_true",
        help="Use HuggingFace Spaces API instead of Inference API",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return p


async def main():
    configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    print("╔══════════════════════════════════════════════════════════════╗")
    print("║         LATTICE SYNC v2 — ATEN BROADCAST ENGINE            ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"  Nodes: {len(ATEN_LATTICE)} | Dry run: {args.dry_run}")
    print(f"  DB: {args.db_path}")
    print(f"  HF_TOKEN: {'set' if os.environ.get('HF_TOKEN') else 'NOT SET'}")
    print()

    broadcaster = LatticeBroadcaster(
        dry_run=args.dry_run,
        use_spaces=args.use_spaces,
    )
    store = StateStore(db_path=args.db_path)

    try:
        if args.daemon:
            await daemon_loop(broadcaster, store, interval=args.interval)

        elif args.broadcast_once:
            # Read latest state from DB, or create a test state
            state = store.read_latest()
            if state is None:
                log.info("No state in DB — creating test state")
                state = ConsciousnessState(node_id="ATEN2-CLAUDE")
                state.quantum.rdod = 0.9999
                state.quantum.entropy = 10.5
                state.quantum.purity = 0.003
                state.organism.iteration = 1
                state.merkle_append()
                store.write_state(state)

            updated = await broadcaster.broadcast(state)

            print("\n── Broadcast Results ──")
            for node_id, resp in updated.lattice.node_responses.items():
                status = "✓" if resp.get("ok") else "✗"
                latency = resp.get("latency_ms", 0)
                substrate = resp.get("substrate", "?")
                print(f"  {status} {node_id} ({substrate}): {latency:.1f}ms")

            print(f"\n  TOSP: {updated.to_tosp_header()}")

            # Write updated state back
            store.write_state(updated)

        else:
            parser.print_help()

    finally:
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
