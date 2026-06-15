"""Capacity-factor climate-signal methods (qdm and direct). See the pipeline guide PDF."""


from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import numpy as np
import pandas as pd


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _months(index: pd.DatetimeIndex) -> np.ndarray:
    return np.asarray(index.month)


def _safe_ratio(future_mean: float, base_mean: float, floor: float = 1e-3) -> float:
    """Change factor with a guard against divide-by-zero (e.g. polar-night solar)."""
    if base_mean <= floor:
        return 1.0
    return future_mean / base_mean


# ----------------------------------------------------------------------------
# Method 1: DIRECT
# ----------------------------------------------------------------------------
def apply_direct(orig_cf: pd.Series,
                 c2e_future: pd.Series) -> pd.Series:
    """Replace original CF with the C2E future CF series (position-aligned).

    Both series must already be on the network snapshot grid (same length).
    The C2E series is assumed already bias-corrected against ERA5 at source.
    """
    if len(c2e_future) != len(orig_cf):
        raise ValueError(
            f"DIRECT: length mismatch orig={len(orig_cf)} future={len(c2e_future)}")
    out = np.clip(np.asarray(c2e_future.values, dtype=float), 0.0, 1.0)
    return pd.Series(out, index=orig_cf.index)


# ----------------------------------------------------------------------------
# Method 2: DELTA (legacy baseline)
# ----------------------------------------------------------------------------
def _empirical_cdf_value(sample: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Non-exceedance probability tau of each x within `sample` (interp ranks)."""
    s = np.sort(sample)
    n = s.size
    if n == 0:
        return np.full_like(x, 0.5, dtype=float)
    # plotting-position quantile levels of the sorted sample
    p = (np.arange(1, n + 1) - 0.5) / n
    # interpolate the inverse-CDF the other way: value -> tau
    tau = np.interp(x, s, p, left=p[0], right=p[-1])
    return np.clip(tau, 1e-6, 1 - 1e-6)


def _quantile(sample: np.ndarray, tau: np.ndarray) -> np.ndarray:
    s = np.sort(sample)
    n = s.size
    if n == 0:
        return np.zeros_like(tau)
    p = (np.arange(1, n + 1) - 0.5) / n
    return np.interp(tau, p, s, left=s[0], right=s[-1])


def apply_qdm_multiplicative(orig_cf: pd.Series,
                             c2e_baseline: pd.Series,
                             c2e_future: pd.Series,
                             by_month: bool = True,
                             jitter: float = 1e-6) -> pd.Series:
    """Quantile Delta Mapping, multiplicative (ratio) form - appropriate for
    bounded non-negative quantities like capacity factors.

    For each future value x_f:
      tau          = rank of x_f within the FUTURE C2E distribution
      delta(tau)   = x_f / Q_baseline_C2E(tau)            (relative change)
      x_corrected  = Q_orig_ERA5(tau) * delta(tau)

    i.e. we read the climate-change relative-change signal off the C2E
    future-vs-baseline pair at each quantile, then apply it to the ERA5
    (atlite/Gotske) climatology at the matching quantile. This removes the
    pipeline-level bias (because the historical reference is the model's own
    ERA5 CF) while preserving C2E's projected change at every quantile.

    Multiplicative form preserves relative changes and keeps CF >= 0; we clip
    to [0,1] at the end. `by_month=True` does the mapping per calendar month so
    the seasonal cycle is respected (standard practice).

    Note: C2E is already grid-box delta-quantile-mapped against ERA5 (Wohland
    et al. 2025), so the residual offset we correct here is the CONVERSION-chain
    difference (siting rule, turbine fleet, PV model) between C2E and the
    network's atlite/ERA5 world, not raw climate-model bias. QDM transfers only
    the per-quantile climate-change signal onto the network's own climatology.

    Reference: Cannon et al. (2015), eqs. for QDM_mult.
    """
    orig = np.asarray(orig_cf.values, dtype=float)
    base = np.asarray(c2e_baseline.values, dtype=float)
    fut = np.asarray(c2e_future.values, dtype=float)

    out = np.empty_like(orig)

    if by_month:
        mo = _months(orig_cf.index)
        mb = _months(c2e_baseline.index)
        mf = _months(c2e_future.index)
        for m in range(1, 13):
            io = np.where(mo == m)[0]
            ib = np.where(mb == m)[0]
            if_ = np.where(mf == m)[0]
            if io.size == 0:
                continue
            if ib.size == 0 or if_.size == 0:
                # no C2E data this month -> leave original unchanged
                out[io] = np.clip(orig[io], 0.0, 1.0)
                continue
            out[io] = _qdm_block(orig[io], base[ib], fut[if_], jitter)
    else:
        out = _qdm_block(orig, base, fut, jitter)

    return pd.Series(np.clip(out, 0.0, 1.0), index=orig_cf.index)


def _qdm_block(orig_block: np.ndarray,
               base_block: np.ndarray,
               fut_block: np.ndarray,
               jitter: float) -> np.ndarray:
    """QDM for one homogeneous block (e.g. one calendar month).

    NOTE on length alignment: orig_block is the series we are rewriting (length
    = number of network snapshots in this month). base/fut blocks are the C2E
    baseline/future for the same month and need NOT be the same length as
    orig_block - QDM maps via quantiles, not by position, so different sample
    sizes are fine. The output has the length of orig_block.
    """
    # rank each FUTURE value within the future distribution
    tau_fut = _empirical_cdf_value(fut_block, fut_block)
    base_at_tau = _quantile(base_block, tau_fut)
    # relative change at each future quantile
    delta = (fut_block + jitter) / (base_at_tau + jitter)

    # Now we need delta as a function of quantile that we can apply to the
    # ORIGINAL series. Map each original value to its quantile, then to the
    # delta at that quantile (interpolated over the sorted future sample).
    tau_orig = _empirical_cdf_value(orig_block, orig_block)

    # build delta(tau): sort future taus, carry deltas alongside
    order = np.argsort(tau_fut)
    tau_sorted = tau_fut[order]
    delta_sorted = delta[order]
    # de-duplicate taus for stable interpolation
    tau_unique, idx_unique = np.unique(tau_sorted, return_index=True)
    delta_unique = delta_sorted[idx_unique]
    delta_for_orig = np.interp(tau_orig, tau_unique, delta_unique,
                               left=delta_unique[0], right=delta_unique[-1])

    return orig_block * delta_for_orig


# ----------------------------------------------------------------------------
# Dispatch-facing wrapper
# ----------------------------------------------------------------------------
def build_modified_cf(method: str,
                      orig_cf: pd.Series,
                      c2e_future: pd.Series,
                      c2e_baseline: pd.Series | None = None) -> pd.Series:
    """Single entry point used by the network builder.

    method in {'direct', 'qdm'} (the legacy monthly 'delta' method was removed
    in v16; QDM supersedes it).
      - direct : raw substitution of the C2E future capacity factor
      - qdm    : per-quantile change applied on the network's own chronology
                 (needs c2e_baseline and c2e_future)
    """
    method = method.lower()
    if method == 'direct':
        return apply_direct(orig_cf, c2e_future)
    if method == 'qdm':
        if c2e_baseline is None:
            raise ValueError("qdm needs the C2E baseline series")
        return apply_qdm_multiplicative(orig_cf, c2e_baseline, c2e_future)
    raise ValueError(f"unknown method '{method}' (valid: direct, qdm)")
