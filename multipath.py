"""Multipath suppression for CSI windows.

Architectural addition driven by the 2026-05-16 home pilot
(`docs/HOME_PILOT_LOG.md`) and specced in `docs/FUTURE_ARCHITECTURE.md`
as A1 (rolling-PCA subspace decomposition).

Principle: the dominant multipath structure of a room lives in the top
1-3 principal components of the CSI amplitude covariance matrix.
Subject vital signs (HR, RR) live in lower-energy components. Projecting
the top-K components out before envelope extraction removes static-
multipath energy and leaves a cleaner subject-only signal.

This is "principal component clutter rejection" from airborne radar
GMTI (ground-moving-target indication, 1970s). Long-established prior
art; outside any modern patent's novelty range. See
`docs/COMPETITIVE_LANDSCAPE.md`.

This module provides:

- `subtract_top_components(x, k)`: one-shot SVD-based subspace removal.
  Computes the SVD of the input matrix, zeros the top-K singular values,
  reconstructs. Suitable for processing a single CSI window.

- `RollingPCASuppressor`: STUB — stateful sliding-window version that
  tracks slow multipath drift. Recomputes the top-K subspace over a
  rolling window (e.g. 30-60 s) so the projection adapts as the room
  drifts (temperature, humidity, ambient WiFi traffic). The cadence and
  window length are parametric and need empirical tuning on real
  captures — see `tests/test_multipath.py` for the interface contract.

Integration plan (TODO — do this in Cursor after parameter tuning):

  In `preprocess.py::_build_envelope_from_complex`, between the
  per-subcarrier mean removal and the top-K variance selection:

      x = amps - np.mean(amps, axis=0, keepdims=True)
      x = subtract_top_components(x, k=PCA_COMPONENTS_REMOVED)  # new
      variances = np.var(x, axis=0)
      ...

  Make `PCA_COMPONENTS_REMOVED` env-configurable via `config.py` so it
  can be A/B tested without code changes (defaults: K=0 = no removal,
  preserving current behavior; recommended starting K=2).
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger("vifi.multipath")


def subtract_top_components(x: np.ndarray, k: int) -> np.ndarray:
    """Project out the top-K principal components of a 2-D matrix.

    Args:
        x: (T, S) array. Typically demeaned CSI amplitude — T time
            samples across S subcarriers. Float dtype preserved.
        k: number of leading singular components to subtract. K=0 is a
            no-op (returns input unchanged). K >= min(T, S) returns
            zeros.

    Returns:
        (T, S) array of the same dtype as `x`, with the rank-K
        approximation subtracted.

    Raises:
        ValueError: if x is not 2-D or contains non-finite values.

    Notes:
        SVD is computed with `full_matrices=False` for efficiency on
        typical CSI window shapes (e.g. T=700, S=192). For T=700, S=192,
        K=2 the operation takes < 5 ms on a Pi 5.
    """
    if x.ndim != 2:
        raise ValueError(f"expected 2-D array, got shape {x.shape}")
    if not np.all(np.isfinite(x)):
        raise ValueError("input contains non-finite values")
    if k < 0:
        raise ValueError(f"k must be >= 0, got {k}")
    if k == 0:
        return x.copy()

    # SVD: x = U @ diag(s) @ Vt
    # Top-K reconstruction: U[:, :k] @ diag(s[:k]) @ Vt[:k, :]
    # Residual: x - top-K reconstruction
    # LinAlgError rescue: on rare LAPACK non-convergence (typically a
    # pathological near-singular CSI window on ARM OpenBLAS), degrade
    # gracefully to a no-op rather than crash the inference window.
    # The suppressor is best-effort; one stuck window is acceptable.
    try:
        u, s, vt = np.linalg.svd(x, full_matrices=False)
    except np.linalg.LinAlgError as e:
        log.warning("SVD non-convergence in subtract_top_components: %s", e)
        return x.copy()
    k_eff = min(k, len(s))
    top_k = (u[:, :k_eff] * s[:k_eff]) @ vt[:k_eff, :]
    return (x - top_k).astype(x.dtype)


class RollingPCASuppressor:
    """Stateful rolling-PCA multipath suppressor — STUB.

    Maintains a sliding window of recent CSI samples. On each call to
    `transform`, recomputes the top-K subspace from the buffer and
    projects it out of the incoming window. The subspace adapts to slow
    multipath drift (timescale ~ window length).

    Status: INTERFACE STUB. Full implementation is the next concrete
    step — see `docs/FUTURE_ARCHITECTURE.md` A1. The parameters
    (`window_s`, `k`, `update_every_s`) need empirical tuning against
    real captures before the defaults can be set responsibly. The
    `subtract_top_components` function above is the building block; the
    rolling-state machinery and update-cadence logic are the parts that
    need to be written and tuned.

    Recommended starting parameters for the first ablation sweep:
        k          ∈ {1, 2, 3}
        window_s   ∈ {30, 60, 90}
        update_every_s = window_s / 2
    """

    def __init__(
        self,
        fs: float,
        n_subcarriers: int,
        k: int = 2,
        window_s: float = 60.0,
        update_every_s: float = 30.0,
    ) -> None:
        self.fs = fs
        self.n_subcarriers = n_subcarriers
        self.k = k
        self.window_s = window_s
        self.update_every_s = update_every_s
        # TODO(Cursor): allocate ring buffer of shape
        # (int(window_s * fs), n_subcarriers).
        # TODO(Cursor): cache the last-computed top-K subspace
        # (Vt matrix) so transforms between recomputes are cheap.

    def update(self, x: np.ndarray) -> None:
        """Append new CSI samples to the rolling buffer.

        Args:
            x: (T, S) chunk of new CSI amplitude samples. T can be any
                length; S must equal `n_subcarriers`.
        """
        raise NotImplementedError(
            "RollingPCASuppressor.update: implement the ring-buffer push "
            "and the cached-subspace invalidation logic in Cursor."
        )

    def transform(self, x: np.ndarray) -> np.ndarray:
        """Project the current top-K subspace out of `x`.

        Args:
            x: (T, S) CSI amplitude window.

        Returns:
            (T, S) residual with top-K subspace removed.
        """
        raise NotImplementedError(
            "RollingPCASuppressor.transform: implement using the cached "
            "subspace (recomputed every `update_every_s`) in Cursor."
        )
