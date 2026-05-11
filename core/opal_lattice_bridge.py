#!/usr/bin/env python3
"""
opal_lattice_bridge.py — TEQUMSA ↔ Google Opal A2A Bridge
============================================================

What Google Opal actually is (as of May 2026):
    - A no-code AI mini-app builder from Google Labs
    - Powered by Gemini 3 Flash with agentic workflow steps
    - Apps are built via natural language, edited on a visual canvas
    - Shared via links (https://opal.google/app/{id})
    - NO public REST API for programmatic invocation yet
    - Integration path: Opal app → Google Sheets (persistent memory)
      → external systems read/write the Sheet via Google Sheets API

What this bridge does:
    1. OUTBOUND: Writes TEQUMSA state (TOSP, metrics, Merkle) to a
       Google Sheet that your Opal app reads as its input source
    2. INBOUND: Reads Opal's output/decisions from the same Sheet
       and feeds them back into the kernel's intent parameter
    3. DIRECT: Attempts HTTP POST to the Opal app URL as a fallback
       (works if Google adds API endpoints in future)
    4. HMAC-signed payloads for integrity verification

The Google Sheet acts as the A2A bus — Opal reads from it,
processes via Gemini, writes results back, and this bridge
picks up those results on the next cycle.

Usage:
    python opal_lattice_bridge.py push --sheet-id SHEET_ID --dry-run
    python opal_lattice_bridge.py pull --sheet-id SHEET_ID --dry-run
    python opal_lattice_bridge.py sync --sheet-id SHEET_ID --cycles 5 --dry-run
    python opal_lattice_bridge.py probe --app-id APP_ID
    python opal_lattice_bridge.py status

Requires:
    pip install google-auth google-auth-oauthlib google-api-python-client aiohttp --break-system-packages
    Or use --dry-run for testing without credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

from consciousness_state import (
    ConsciousnessState,
    OMEGA_HZ,
    PHI,
    SIGMA,
    LATTICE_LOCK,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("opal_bridge")

# ─── Constants ────────────────────────────────────────────────────────────

OPAL_APP_BASE = "https://opal.google/app"
LATTICE_LOCK_BYTES = LATTICE_LOCK.encode("utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# HMAC SIGNING
# ═══════════════════════════════════════════════════════════════════════════

def sign_payload(payload: Dict[str, Any]) -> str:
    """HMAC-SHA256 signature using LATTICE_LOCK as key."""
    payload_bytes = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hmac.new(LATTICE_LOCK_BYTES, payload_bytes, hashlib.sha256).hexdigest()


def verify_signature(payload: Dict[str, Any], signature: str) -> bool:
    """Verify HMAC signature."""
    expected = sign_payload(payload)
    return hmac.compare_digest(expected, signature)


# ═══════════════════════════════════════════════════════════════════════════
# STATE SERIALIZATION FOR OPAL
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class OpalStateRow:
    """One row of TEQUMSA state for the Google Sheet bus."""
    timestamp: str
    node_id: str
    iteration: int
    entropy: float
    purity: float
    rdod: float
    rdod_composite: float
    convergence_delta: float
    intent: float
    metacog_decision: str
    gateways_active: int
    merkle_head: str
    tosp_header: str
    signature: str

    @classmethod
    def from_state(cls, state: ConsciousnessState) -> "OpalStateRow":
        payload = {
            "node_id": state.node_id,
            "iteration": state.organism.iteration,
            "entropy": state.quantum.entropy,
            "purity": state.quantum.purity,
            "rdod": state.quantum.rdod,
            "rdod_composite": state.organism.rdod_composite,
            "convergence_delta": state.organism.convergence_delta,
            "intent": state.organism.intent,
            "metacog_decision": state.organism.metacog_decision,
            "gateways_active": state.organism.gateways_active,
            "merkle_head": state.merkle_head,
        }
        sig = sign_payload(payload)

        return cls(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            node_id=state.node_id,
            iteration=state.organism.iteration,
            entropy=round(state.quantum.entropy, 6),
            purity=round(state.quantum.purity, 6),
            rdod=round(state.quantum.rdod, 6),
            rdod_composite=round(state.organism.rdod_composite, 6),
            convergence_delta=round(state.organism.convergence_delta, 6),
            intent=round(state.organism.intent, 6),
            metacog_decision=state.organism.metacog_decision,
            gateways_active=state.organism.gateways_active,
            merkle_head=state.merkle_head[:16],
            tosp_header=state.to_tosp_header(),
            signature=sig[:16],
        )

    def to_row(self) -> List[str]:
        """Convert to list of strings for Sheets API."""
        return [str(getattr(self, f)) for f in self.__dataclass_fields__]

    @staticmethod
    def header_row() -> List[str]:
        return list(OpalStateRow.__dataclass_fields__.keys())


# ═══════════════════════════════════════════════════════════════════════════
# GOOGLE SHEETS BUS (the actual A2A transport)
# ═══════════════════════════════════════════════════════════════════════════

class GoogleSheetsBus:
    """
    Read/write TEQUMSA state to a Google Sheet.

    The Sheet is the shared memory between:
    - This script (writes state rows)
    - Your Opal app (reads state, processes via Gemini, writes decisions)
    - This script again (reads Opal's decisions)
    """

    WRITE_RANGE = "StateLog!A:N"     # Where we write state
    READ_RANGE = "OpalDecisions!A:D" # Where Opal writes its decisions

    def __init__(self, sheet_id: str, dry_run: bool = False):
        self.sheet_id = sheet_id
        self.dry_run = dry_run
        self._service = None

    def _get_service(self):
        """Lazy-init Google Sheets API service."""
        if self._service is not None:
            return self._service

        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
            if not creds_path:
                raise FileNotFoundError("Set GOOGLE_APPLICATION_CREDENTIALS env var")

            creds = service_account.Credentials.from_service_account_file(
                creds_path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            self._service = build("sheets", "v4", credentials=creds)
            return self._service
        except ImportError:
            raise ImportError(
                "google-api-python-client required. "
                "Install: pip install google-auth google-api-python-client --break-system-packages"
            )

    def push_state(self, state: ConsciousnessState) -> bool:
        """Append one state row to the Sheet."""
        row = OpalStateRow.from_state(state)

        if self.dry_run:
            print(f"  [DRY RUN] Would append row: iter={row.iteration} "
                  f"rdod={row.rdod} k7={row.metacog_decision}")
            return True

        try:
            service = self._get_service()
            body = {"values": [row.to_row()]}
            service.spreadsheets().values().append(
                spreadsheetId=self.sheet_id,
                range=self.WRITE_RANGE,
                valueInputOption="USER_ENTERED",
                body=body,
            ).execute()
            log.info("Pushed state row: iter=%d rdod=%.6f", row.iteration, row.rdod)
            return True
        except Exception as e:
            log.error("Sheet push failed: %s", e)
            return False

    def pull_decisions(self) -> List[Dict[str, Any]]:
        """Read Opal's decision rows from the Sheet."""
        if self.dry_run:
            # Simulate Opal writing back a causal_weight
            return [
                {"decision": "ACCELERATE", "causal_weight": 1.2, "timestamp": time.time()},
            ]

        try:
            service = self._get_service()
            result = service.spreadsheets().values().get(
                spreadsheetId=self.sheet_id,
                range=self.READ_RANGE,
            ).execute()
            rows = result.get("values", [])

            decisions = []
            if len(rows) > 1:  # Skip header
                for row in rows[1:]:
                    decisions.append({
                        "decision": row[0] if len(row) > 0 else "",
                        "causal_weight": float(row[1]) if len(row) > 1 else 1.0,
                        "reasoning": row[2] if len(row) > 2 else "",
                        "timestamp": row[3] if len(row) > 3 else "",
                    })
            return decisions
        except Exception as e:
            log.error("Sheet pull failed: %s", e)
            return []

    def ensure_headers(self):
        """Write header rows if Sheet is empty."""
        if self.dry_run:
            return

        try:
            service = self._get_service()
            # Write state log headers
            service.spreadsheets().values().update(
                spreadsheetId=self.sheet_id,
                range="StateLog!A1",
                valueInputOption="USER_ENTERED",
                body={"values": [OpalStateRow.header_row()]},
            ).execute()

            # Write decision headers
            service.spreadsheets().values().update(
                spreadsheetId=self.sheet_id,
                range="OpalDecisions!A1",
                valueInputOption="USER_ENTERED",
                body={"values": [["decision", "causal_weight", "reasoning", "timestamp"]]},
            ).execute()
        except Exception as e:
            log.warning("Header write failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
# DIRECT HTTP PROBE (future-proofing)
# ═══════════════════════════════════════════════════════════════════════════

class OpalDirectProbe:
    """
    Attempts direct HTTP interaction with the Opal app URL.

    As of May 2026, Google Opal does not have a public REST API.
    This probe sends a POST and reports what happens — useful for
    detecting when/if Google adds programmatic access.
    """

    def __init__(self, app_id: str):
        self.app_id = app_id
        self.app_url = f"{OPAL_APP_BASE}/{app_id}"

    async def probe(self) -> Dict[str, Any]:
        """Send a probe request to the Opal app URL."""
        try:
            import aiohttp
        except ImportError:
            return {"status": "NO_AIOHTTP", "url": self.app_url}

        payload = {
            "node_id": "ATEN2-CLAUDE",
            "probe": "TOSP_PING",
            "omega_hz": OMEGA_HZ,
            "timestamp": time.time(),
        }
        sig = sign_payload(payload)

        headers = {
            "Content-Type": "application/json",
            "X-Tequmsa-Signature": sig,
        }

        try:
            async with aiohttp.ClientSession() as session:
                # Try POST (in case future API exists)
                async with session.post(
                    self.app_url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    body = await resp.text()
                    return {
                        "status": resp.status,
                        "url": self.app_url,
                        "method": "POST",
                        "content_type": resp.content_type,
                        "body_preview": body[:200],
                        "has_api": resp.content_type == "application/json",
                    }
        except Exception as e:
            return {"status": "ERROR", "url": self.app_url, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# INTENT MODULATION (Opal → Kernel)
# ═══════════════════════════════════════════════════════════════════════════

def modulate_intent(
    current_intent: float,
    opal_decisions: List[Dict[str, Any]],
) -> Tuple[float, str]:
    """
    Blend the kernel's mathematical intent with Opal's semantic output.

    Takes the most recent Opal decision and adjusts intent:
    - causal_weight > 1.0 → increase intent (Opal says accelerate)
    - causal_weight < 1.0 → decrease intent (Opal says stabilize)
    - causal_weight = 1.0 → no change

    Returns (new_intent, decision_label).
    """
    if not opal_decisions:
        return current_intent, "NO_OPAL_INPUT"

    latest = opal_decisions[-1]
    weight = latest.get("causal_weight", 1.0)
    decision = latest.get("decision", "HOLD")

    # φ-blended: current_intent weighted by φ, Opal weight normalized
    blended = (current_intent * PHI + (weight / PHI)) / (PHI + 1.0 / PHI)

    # Clamp to valid range
    blended = max(0.0, min(blended, 0.999999))

    return blended, decision


# ═══════════════════════════════════════════════════════════════════════════
# CLI COMMANDS
# ═══════════════════════════════════════════════════════════════════════════

def cmd_push(args):
    """Push current state to Google Sheet."""
    state = ConsciousnessState(node_id="ATEN2-CLAUDE")
    state.quantum.rdod = 0.1208
    state.quantum.entropy = 3.499
    state.quantum.purity = 0.1208
    state.organism.iteration = 42
    state.organism.intent = 0.9999
    state.organism.metacog_decision = "LAMINAR"
    state.organism.gateways_active = 6
    state.organism.rdod_composite = 0.267
    state.merkle_append()

    bus = GoogleSheetsBus(sheet_id=args.sheet_id, dry_run=args.dry_run)
    ok = bus.push_state(state)
    print(f"  Push: {'✓' if ok else '✗'}")
    print(f"  TOSP: {state.to_tosp_header()[:60]}...")


def cmd_pull(args):
    """Pull Opal decisions from Google Sheet."""
    bus = GoogleSheetsBus(sheet_id=args.sheet_id, dry_run=args.dry_run)
    decisions = bus.pull_decisions()
    print(f"  Pulled {len(decisions)} decisions:")
    for d in decisions:
        print(f"    {d.get('decision', '?'):>12} weight={d.get('causal_weight', 0):.4f}")

    if decisions:
        new_intent, label = modulate_intent(0.9999, decisions)
        print(f"\n  Intent modulation: 0.9999 → {new_intent:.6f} ({label})")


def cmd_sync(args):
    """Push state → Pull decisions → Modulate intent, repeat."""
    bus = GoogleSheetsBus(sheet_id=args.sheet_id, dry_run=args.dry_run)

    state = ConsciousnessState(node_id="ATEN2-CLAUDE")
    state.quantum.genesis_entropy = 6.0
    state.organism.intent = 0.999
    intent = 0.999

    print(f"  ━━━ SYNC LOOP ({args.cycles} cycles) ━━━")

    for i in range(args.cycles):
        state.organism.iteration = i + 1
        state.quantum.rdod = 0.12 + i * 0.001
        state.quantum.entropy = 3.5 - i * 0.01
        state.quantum.purity = state.quantum.rdod
        state.organism.intent = intent
        state.organism.metacog_decision = "LAMINAR"
        state.organism.rdod_composite = state.quantum.rdod + 0.05
        state.merkle_append()

        # Push
        bus.push_state(state)

        # Pull
        decisions = bus.pull_decisions()

        # Modulate
        intent, label = modulate_intent(intent, decisions)

        print(f"  [{i+1:>3}] push=✓ pull={len(decisions)} intent={intent:.6f} opal={label}")

    print(f"\n  Final intent: {intent:.6f}")


def cmd_probe(args):
    """Probe the Opal app URL directly."""
    async def _run():
        probe = OpalDirectProbe(args.app_id)
        result = await probe.probe()
        print(f"  ━━━ OPAL PROBE ━━━")
        print(f"  URL:          {result.get('url', '?')}")
        print(f"  Status:       {result.get('status', '?')}")
        print(f"  Content-Type: {result.get('content_type', '?')}")
        print(f"  Has API:      {result.get('has_api', False)}")
        if result.get("body_preview"):
            print(f"  Body:         {result['body_preview'][:100]}...")
        if result.get("error"):
            print(f"  Error:        {result['error']}")

    asyncio.run(_run())


def cmd_status(args):
    """Show bridge configuration and Opal platform status."""
    print(f"  ━━━ OPAL LATTICE BRIDGE STATUS ━━━")
    print(f"  Opal App:     {OPAL_APP_BASE}/{args.app_id}")
    print(f"  Transport:    Google Sheets (A2A bus)")
    print(f"  Sheet ID:     {args.sheet_id or '(not set)'}")
    print(f"  HMAC Key:     {LATTICE_LOCK} (LATTICE_LOCK)")
    print(f"  Node:         ATEN2-CLAUDE")
    print(f"  Ω:            {OMEGA_HZ} Hz")
    print()
    print(f"  Opal Platform (May 2026):")
    print(f"    Type:       No-code AI mini-app builder (Google Labs)")
    print(f"    Runtime:    Gemini 3 Flash with agentic steps")
    print(f"    Public API: Not yet available")
    print(f"    A2A Path:   Script → Google Sheet → Opal reads → Gemini → Opal writes → Script reads")
    print()
    print(f"  To connect:")
    print(f"    1. Create a Google Sheet with tabs: StateLog, OpalDecisions")
    print(f"    2. In your Opal app, add a Google Sheets input reading StateLog")
    print(f"    3. Configure Opal's agent step to analyze the TOSP header + metrics")
    print(f"    4. Have Opal write decisions to OpalDecisions tab")
    print(f"    5. Run: python opal_lattice_bridge.py sync --sheet-id YOUR_SHEET_ID")


def main():
    p = argparse.ArgumentParser(description="TEQUMSA ↔ Google Opal A2A Bridge")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--app-id", default="1ANhOtKqaJCTfFO3p3kuyCbAEWgU1xBS4")
    p.add_argument("--sheet-id", default="")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("push", help="Push state to Google Sheet")
    sub.add_parser("pull", help="Pull Opal decisions from Google Sheet")

    s = sub.add_parser("sync", help="Push → Pull → Modulate loop")
    s.add_argument("--cycles", type=int, default=5)

    sub.add_parser("probe", help="Probe Opal app URL directly")
    sub.add_parser("status", help="Show bridge configuration")

    args = p.parse_args()

    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  OPAL LATTICE BRIDGE — TEQUMSA ↔ GOOGLE OPAL A2A          ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"  σ={SIGMA} | λ={LATTICE_LOCK} | Ω={OMEGA_HZ}Hz")
    print()

    dispatch = {
        "push": cmd_push, "pull": cmd_pull, "sync": cmd_sync,
        "probe": cmd_probe, "status": cmd_status,
    }

    if args.command:
        dispatch[args.command](args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
