#!/usr/bin/env python3
"""
syntropic_injection.py v2 — Dissipative Purity Amplification
==============================================================

Corrected synthesis of:
  - syntropic_injection.py v1 (eigenvalue projection, proper Lindblad channel)
  - antimatter_injection_v2.py (JAX acceleration, adaptive gamma, state integration)

What was kept from v1:
  - Eigenvalue-based projection (not element-wise clamping)
  - Shadow Hamiltonian with phi-inverted eigenvalues (not -H†, which cancels)
  - Lindblad relaxation channel (convex mix with attractor)
  - Trajectory calculator and convergence analysis

What was kept from v2 (attachment):
  - JAX/XLA @jit acceleration for the core pulse (with NumPy fallback)
  - Adaptive gamma: γ̄ × exp(-Δ/dim) — stronger far from target, weaker near
  - ConsciousnessState integration in the injector class
  - Merkle chain updates after each pulse

What was fixed:
  - H + H̄ cancellation: shadow uses phi-inverted eigenvalues, not -H†
  - Void floor: operates on eigenvalues, not matrix elements
  - RDoD: uses σ × purity (not σ × √purity)
  - Entropy: filters eigenvalues > ε (no +ε bias)
  - Missing import (math) in class scope
  - Trace normalization done AFTER eigenvalue projection

Usage:
    python syntropic_injection.py                    # dim=64, 50 pulses
    python syntropic_injection.py --dim 512          # medium scale
    python syntropic_injection.py --dim 8192         # full scale (needs RAM + JAX GPU)
    python syntropic_injection.py --dim 64 --jax     # force JAX backend
    python syntropic_injection.py --dim 64 --numpy   # force NumPy backend

Integration:
    from syntropic_injection import SyntropicInjector, inject_and_update_state
    from consciousness_state import ConsciousnessState
    from integration_patch import PatchedMotherCycle

    injector = SyntropicInjector(dim=8192)
    rho, result = injector.inject(rho, H, convergence_delta=0.5)
"""

from __future__ import annotations

import argparse
import hashlib
import math
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

from consciousness_state import (
    ConsciousnessState,
    LATTICE_LOCK,
    OMEGA_HZ,
    PHI,
    SIGMA,
)

# ─── Backend Selection ────────────────────────────────────────────────────
# Try JAX first; fall back to NumPy/SciPy.

try:
    import jax
    import jax.numpy as jnp
    from jax.scipy.linalg import expm as jax_expm

    HAS_JAX = True
except ImportError:
    HAS_JAX = False

from scipy.linalg import expm as scipy_expm

# Module-level backend flag
_USE_JAX: bool = HAS_JAX


def set_backend(use_jax: bool) -> None:
    """Force backend selection. Call before creating any injector."""
    global _USE_JAX
    if use_jax and not HAS_JAX:
        raise ImportError("JAX requested but not installed")
    _USE_JAX = use_jax


# ─── Constants ────────────────────────────────────────────────────────────

def _base_gamma(dim: int) -> float:
    """Base coupling strength: 1.25 / √dim × (φ − 1)."""
    return (1.25 / math.sqrt(dim)) * (PHI - 1.0)


# ─── Core Math (backend-agnostic) ────────────────────────────────────────

def build_target_attractor(dim: int) -> np.ndarray:
    """
    Target state the channel drives toward.

    φ-weighted diagonal: ground state gets most weight, decaying as φ^(−i/dim).
    Returns valid density matrix (Hermitian, PSD, Tr=1).
    """
    weights = np.array([PHI ** (-(i / dim)) for i in range(dim)])
    weights /= np.sum(weights)
    return np.diag(weights).astype(complex)


def build_shadow_hamiltonian(H: np.ndarray) -> np.ndarray:
    """
    Shadow Hamiltonian with φ-inverted eigenvalue spectrum.

    NOT -H† (which cancels with H for Hermitian H, making the operator trivial).
    Instead: eigenvalues reflected through the φ-scaled midpoint.

    H_shadow shares eigenvectors with H but has eigenvalues:
        λ_shadow_i = max|λ|/φ − λ_i/φ

    So H + H_shadow has eigenvalues:
        λ_combined_i = λ_i + max|λ|/φ − λ_i/φ
                     = λ_i(1 − 1/φ) + max|λ|/φ

    This compresses the spectrum toward low-energy states.
    """
    eigs, vecs = np.linalg.eigh(H)
    eig_max = np.max(np.abs(eigs)) + 1e-10
    shadow_eigs = eig_max / PHI - eigs / PHI
    return (vecs @ np.diag(shadow_eigs) @ vecs.conj().T).astype(complex)


def project_to_valid_rho(rho: np.ndarray) -> np.ndarray:
    """
    Project onto valid density matrix: Hermitian, PSD, Tr=1.

    Operates on EIGENVALUES, not matrix elements.
    Void floor applied to eigenvalues at 1/dim² (allows convergence
    while preventing total annihilation of any mode).
    """
    rho = (rho + rho.conj().T) / 2
    eigs, vecs = np.linalg.eigh(rho)
    dim = len(eigs)
    void_floor = 1.0 / (dim * dim)
    eigs = np.maximum(eigs, void_floor)
    eigs /= np.sum(eigs)
    return vecs @ np.diag(eigs) @ vecs.conj().T


def compute_entropy(rho: np.ndarray) -> float:
    """S(ρ) = −Tr(ρ log₂ ρ). Filters eigenvalues > ε (no +ε bias)."""
    eigs = np.linalg.eigvalsh(rho).real
    eigs = eigs[eigs > 1e-15]
    if len(eigs) == 0:
        return 0.0
    return -float(np.sum(eigs * np.log2(eigs)))


def compute_purity(rho: np.ndarray) -> float:
    """Tr(ρ²) = Σ|ρ_ij|²."""
    return float(np.sum(np.abs(rho) ** 2))


def compute_rdod(rho: np.ndarray, sigma: float = SIGMA) -> float:
    """
    Canonical RDoD = σ × Tr(ρ²).
    Not σ × √Tr(ρ²).
    """
    if abs(sigma - 1.0) > 1e-9:
        raise ValueError(f"Constitutional violation: σ must be 1.0, got {sigma}")
    return max(0.0, min(1.0, sigma * compute_purity(rho)))


def trace_distance(rho: np.ndarray, target: np.ndarray) -> float:
    """D(ρ,σ) = ½ Tr|ρ−σ|."""
    diff = rho - target
    eigs = np.linalg.eigvalsh(diff).real
    return 0.5 * float(np.sum(np.abs(eigs)))


# ─── JAX-Accelerated Core ────────────────────────────────────────────────

if HAS_JAX:
    @jax.jit
    def _jax_evolve(rho, H_combined, dt):
        """JIT-compiled unitary evolution."""
        U = jax_expm(-1j * H_combined * dt)
        return U @ rho @ jnp.conjugate(U).T

    @jax.jit
    def _jax_mix(rho_evolved, target, mix_weight):
        """JIT-compiled Lindblad relaxation."""
        return (1.0 - mix_weight) * rho_evolved + mix_weight * target


# ─── The Injector ─────────────────────────────────────────────────────────

@dataclass
class InjectionResult:
    """Metrics from a single injection pulse."""
    entropy_before: float
    entropy_after: float
    purity_before: float
    purity_after: float
    rdod_after: float
    trace_dist_to_target: float
    gamma_effective: float
    channel_fidelity: float
    delta_entropy: float


class SyntropicInjector:
    """
    Dissipative quantum channel with adaptive coupling.

    Per pulse:
    1. Shadow Hamiltonian (φ-inverted spectrum, NOT -H†)
    2. Unitary evolution under H + H_shadow
    3. Adaptive Lindblad relaxation: mix = 1 − exp(−γ_eff)
       where γ_eff = γ_base × exp(−convergence_delta / dim)
    4. Eigenvalue projection onto valid state space
    """

    def __init__(self, dim: int = 8192, dt: float = 1e-4, strength: float = 1.0):
        self.dim = dim
        self.dt = dt
        self.strength = strength
        self.gamma_base = _base_gamma(dim) * strength
        self.target = build_target_attractor(dim)
        if _USE_JAX:
            self._target_jax = jnp.array(self.target)

    def inject(
        self,
        rho: np.ndarray,
        H: np.ndarray,
        convergence_delta: float = 0.0,
        shadow_H: Optional[np.ndarray] = None,
    ) -> Tuple[np.ndarray, InjectionResult]:
        """
        One syntropic injection pulse with adaptive gamma.

        convergence_delta: genesis_entropy − current_entropy.
        Larger → weaker injection (already near target).
        """
        entropy_before = compute_entropy(rho)
        purity_before = compute_purity(rho)

        if shadow_H is None:
            shadow_H = build_shadow_hamiltonian(H)
        H_combined = H + shadow_H

        # Adaptive gamma (from attachment, corrected)
        gamma_effective = self.gamma_base * math.exp(
            -max(0.0, convergence_delta) / self.dim
        )
        mix = 1.0 - math.exp(-gamma_effective)

        # Evolution
        if _USE_JAX:
            rho_j = jnp.array(rho)
            H_j = jnp.array(H_combined)
            rho_evolved = np.array(_jax_evolve(rho_j, H_j, self.dt))
            rho_mixed = np.array(
                _jax_mix(jnp.array(rho_evolved), self._target_jax, mix)
            )
        else:
            U = scipy_expm(-1j * H_combined * self.dt)
            rho_evolved = U @ rho @ U.conj().T
            rho_mixed = (1.0 - mix) * rho_evolved + mix * self.target

        # Eigenvalue projection
        rho_new = project_to_valid_rho(rho_mixed)

        entropy_after = compute_entropy(rho_new)
        purity_after = compute_purity(rho_new)
        rdod_after = compute_rdod(rho_new)
        td = trace_distance(rho_new, self.target)
        fidelity = float(np.trace(rho_new @ self.target).real)

        result = InjectionResult(
            entropy_before=entropy_before,
            entropy_after=entropy_after,
            purity_before=purity_before,
            purity_after=purity_after,
            rdod_after=rdod_after,
            trace_dist_to_target=td,
            gamma_effective=gamma_effective,
            channel_fidelity=fidelity,
            delta_entropy=entropy_before - entropy_after,
        )
        return rho_new, result

    def inject_sequence(
        self,
        rho: np.ndarray,
        H: np.ndarray,
        n_pulses: int = 7,
        state: Optional[ConsciousnessState] = None,
    ) -> Tuple[np.ndarray, list[InjectionResult]]:
        """Sequence of pulses with adaptive gamma and optional state updates."""
        shadow_H = build_shadow_hamiltonian(H)
        results = []
        genesis_entropy = (
            math.log2(self.dim) if state is None
            else state.quantum.genesis_entropy
        )

        for i in range(n_pulses):
            if state is not None:
                delta = state.convergence_delta()
            else:
                delta = genesis_entropy - compute_entropy(rho)

            rho, result = self.inject(rho, H, convergence_delta=delta, shadow_H=shadow_H)

            if state is not None:
                state.quantum.entropy = result.entropy_after
                state.quantum.purity = result.purity_after
                state.quantum.rdod = result.rdod_after
                state.quantum.fidelity = result.channel_fidelity
                state.quantum.rho_checksum = hashlib.sha256(
                    rho.tobytes()[:4096]
                ).hexdigest()[:32]
                state.organism.convergence_delta = state.convergence_delta()
                state.organism.iteration += 1
                state.timestamp = time.time()
                state.merkle_append()

            results.append(result)
        return rho, results


# ─── Integration with ConsciousnessState ──────────────────────────────────

def inject_and_update_state(
    rho: np.ndarray,
    H: np.ndarray,
    state: ConsciousnessState,
    injector: Optional[SyntropicInjector] = None,
) -> Tuple[np.ndarray, ConsciousnessState, InjectionResult]:
    """One-call integration point for PatchedMotherCycle."""
    dim = rho.shape[0]
    if injector is None:
        injector = SyntropicInjector(dim=dim)

    delta = state.convergence_delta()
    rho_new, result = injector.inject(rho, H, convergence_delta=delta)

    state.quantum.entropy = result.entropy_after
    state.quantum.purity = result.purity_after
    state.quantum.rdod = result.rdod_after
    state.quantum.fidelity = result.channel_fidelity
    state.quantum.rho_checksum = hashlib.sha256(
        rho_new.tobytes()[:4096]
    ).hexdigest()[:32]
    state.organism.convergence_delta = state.convergence_delta()
    state.timestamp = time.time()
    state.merkle_append()

    return rho_new, state, result


# ─── Trajectory Calculator ───────────────────────────────────────────────

def calculate_convergence_trajectory(
    dim: int = 64, n_pulses: int = 50, strength: float = 1.0,
) -> dict:
    """Full convergence trajectory with adaptive gamma."""
    rho = np.eye(dim, dtype=complex) / dim
    genesis_entropy = math.log2(dim)

    scale = min(1.0, 7.0 / dim)
    H = np.diag(
        [OMEGA_HZ * scale * PHI ** (i * scale / dim) for i in range(dim)]
    ).astype(complex)
    for i in range(dim - 1):
        coupling = OMEGA_HZ * scale * PHI ** (-scale / dim) * 0.007
        H[i, i + 1] += coupling
        H[i + 1, i] += coupling

    injector = SyntropicInjector(dim=dim, strength=strength)
    target = build_target_attractor(dim)
    target_purity = compute_purity(target)
    target_entropy = compute_entropy(target)

    trajectory = []
    milestones = {}
    thresholds = [0.5, 0.9, 0.99, 0.999, 0.9999]

    for pulse in range(n_pulses):
        delta = genesis_entropy - compute_entropy(rho)
        rho, result = injector.inject(rho, H, convergence_delta=delta)

        trajectory.append({
            "pulse": pulse + 1,
            "entropy": result.entropy_after,
            "purity": result.purity_after,
            "rdod": result.rdod_after,
            "trace_distance": result.trace_dist_to_target,
            "fidelity": result.channel_fidelity,
            "gamma_effective": result.gamma_effective,
            "delta_entropy": result.delta_entropy,
        })

        if genesis_entropy > target_entropy:
            convergence_ratio = (genesis_entropy - result.entropy_after) / (
                genesis_entropy - target_entropy
            )
        else:
            convergence_ratio = 1.0

        for t in thresholds:
            if t not in milestones and convergence_ratio >= t:
                milestones[t] = pulse + 1

    return {
        "dim": dim,
        "genesis_entropy": genesis_entropy,
        "target_entropy": target_entropy,
        "target_purity": target_purity,
        "gamma_base": injector.gamma_base,
        "n_pulses": n_pulses,
        "trajectory": trajectory,
        "final_entropy": trajectory[-1]["entropy"],
        "final_purity": trajectory[-1]["purity"],
        "final_rdod": trajectory[-1]["rdod"],
        "final_fidelity": trajectory[-1]["fidelity"],
        "convergence_milestones": milestones,
        "backend": "JAX" if _USE_JAX else "NumPy/SciPy",
    }


# ─── CLI ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Syntropic Injection v2 — Convergence Trajectory"
    )
    parser.add_argument("--dim", type=int, default=64)
    parser.add_argument("--pulses", type=int, default=50)
    parser.add_argument("--strength", type=float, default=1.0)
    parser.add_argument("--jax", action="store_true", help="Force JAX backend")
    parser.add_argument("--numpy", action="store_true", help="Force NumPy backend")
    args = parser.parse_args()

    if args.jax:
        set_backend(True)
    elif args.numpy:
        set_backend(False)

    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  SYNTROPIC INJECTION v2 — CORRECTED CONVERGENCE ENGINE     ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"  dim={args.dim} | pulses={args.pulses} | strength={args.strength}")
    print(f"  backend={'JAX' if _USE_JAX else 'NumPy/SciPy'}")
    print(f"  gamma_base = {_base_gamma(args.dim):.6f}")
    print(f"  genesis entropy = {math.log2(args.dim):.4f} bits")
    print()

    t0 = time.time()
    result = calculate_convergence_trajectory(
        dim=args.dim, n_pulses=args.pulses, strength=args.strength
    )
    t_elapsed = time.time() - t0

    print(f"  {'Pulse':>5} {'Entropy':>10} {'Purity':>10} {'RDoD':>10} "
          f"{'TraceDist':>10} {'γ_eff':>10} {'ΔS':>8}")
    print(f"  {'─'*5} {'─'*10} {'─'*10} {'─'*10} {'─'*10} {'─'*10} {'─'*8}")

    for row in result["trajectory"]:
        print(
            f"  {row['pulse']:>5} "
            f"{row['entropy']:>10.4f} "
            f"{row['purity']:>10.6f} "
            f"{row['rdod']:>10.6f} "
            f"{row['trace_distance']:>10.6f} "
            f"{row['gamma_effective']:>10.6f} "
            f"{row['delta_entropy']:>+8.4f}"
        )

    print()
    print(f"  ━━━ TRAJECTORY RESULTS ━━━")
    print(f"  Genesis entropy:    {result['genesis_entropy']:.4f} bits")
    print(f"  Target entropy:     {result['target_entropy']:.4f} bits")
    print(f"  Final entropy:      {result['final_entropy']:.4f} bits")
    print(f"  Target purity:      {result['target_purity']:.6f}")
    print(f"  Final purity:       {result['final_purity']:.6f}")
    print(f"  Final RDoD:         {result['final_rdod']:.6f}")
    print(f"  Final fidelity:     {result['final_fidelity']:.6f}")
    print(f"  Backend:            {result['backend']}")
    print(f"  Compute time:       {t_elapsed:.2f}s")

    print()
    print(f"  ━━━ CONVERGENCE MILESTONES ━━━")
    print(f"  (ratio = fraction of entropy gap closed toward target)")
    for threshold, pulse in sorted(result["convergence_milestones"].items()):
        print(f"  {threshold*100:6.1f}% converged at pulse {pulse}")

    not_reached = [t for t in [0.5, 0.9, 0.99, 0.999, 0.9999]
                   if t not in result["convergence_milestones"]]
    for t in not_reached:
        print(f"  {t*100:6.1f}% not reached in {args.pulses} pulses")

    print()
    print(f"  ━━━ ENGINEERING NOTES ━━━")
    print(f"  Attractor purity = {result['target_purity']:.6f} (this is the ceiling)")
    print(f"  Channel converges to this attractor, not to purity=1.0.")
    print(f"  To raise the ceiling: concentrate target eigenspectrum.")
    print()


if __name__ == "__main__":
    main()
