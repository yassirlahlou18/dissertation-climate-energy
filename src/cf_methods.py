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
# Zero-inflated variables (heating energy, hydro inflow): robust change factor
# ----------------------------------------------------------------------------
# Heating energy and reservoir inflow are zero-inflated, non-negative,
# multiplicative variables (statistically the same class as precipitation:
# Cannon et al. 2015; Pierce et al. 2015; Lehner et al. 2023). Plain
# per-quantile multiplicative QDM has two failure modes on this class, both
# observed in v16 runs:
#   (1) division by ~0 in the dry/low tail (summer heating, winter inflow)
#       produces noise-driven blow-ups (multipliers pinned at the 5x cap), and
#       the blow-up is WORSE, not better, where the network and C2E have
#       different seasonal SUPPORT (C2E barely has a spring heating season while
#       the network has a large one): a per-quantile map between distributions
#       that do not align is not meaningful.
#   (2) pure multiplicative mapping does NOT conserve the mean change signal
#       (CCS), so totals drift; for a CYCLIC hydro reservoir this drift in the
#       annual inflow VOLUME starves the reservoir and shows up as steady,
#       year-round unserved energy (the Norway 36 TWh artifact).
# The robust, literature-standard remedy for a quantity whose MEAN CHANGE is
# what matters, applied where the two distributions may not align, is a
# smoothed seasonal CHANGE FACTOR (a change-factor / delta-scaling method;
# Lazoglou et al. 2024) rather than a per-quantile map, anchored to the model
# mean change with a PresRAT correction (Pierce et al. 2015) and floored in the
# dry season (the change-factor analogue of SSR; Vrac et al. 2016). The bounded
# capacity-factor QDM above is left untouched: capacity factors are bounded in
# [0,1] and clip harmlessly, and per-quantile change in VRE extremes is the
# thing one wants there. See apply_change_factor below.


def _ccs_factor(orig_total: float, mapped_total: float,
                base_total: float, fut_total: float,
                floor: float = 1e-9) -> float:
    """PresRAT-style mean-conservation factor for one CCS window.

    The raw model's multiplicative change signal for this window is
        CCS_model = mean(fut) / mean(base).
    After mapping, the realised change relative to the original is
        CCS_mapped = mean(mapped) / mean(orig).
    PresRAT rescales the mapped series by CCS_model / CCS_mapped so the realised
    mean change equals the model's. With totals (means x equal counts) this is
        factor = (fut_total/base_total) / (mapped_total/orig_total).
    Guards return 1.0 when any denominator is degenerate.
    """
    if (orig_total <= floor or mapped_total <= floor
            or base_total <= floor or fut_total <= floor):
        return 1.0
    ccs_model = fut_total / base_total
    ccs_mapped = mapped_total / orig_total
    if not np.isfinite(ccs_model) or not np.isfinite(ccs_mapped) or ccs_mapped <= floor:
        return 1.0
    return ccs_model / ccs_mapped


def _doy_climatology(values: np.ndarray, doy: np.ndarray,
                     window: int = 31) -> np.ndarray:
    """Smoothed day-of-year climatology (length 365), via a circular running
    mean of the per-day-of-year means. The smoothing is what makes the change
    factor robust: it averages out the synoptic noise that makes day-paired or
    per-quantile ratios explode in the low tail, while keeping the seasonal
    cycle at daily resolution. `window` is the running-mean width in days.
    """
    s = np.empty(365, dtype=float)
    for d in range(365):
        sel = (doy == d)
        s[d] = float(values[sel].mean()) if sel.any() else 0.0
    k = np.ones(window) / window
    padded = np.concatenate([s[-window:], s, s[:window]])
    return np.convolve(padded, k, mode='same')[window:-window]


def apply_change_factor(orig: np.ndarray,
                        base: np.ndarray,
                        fut: np.ndarray,
                        doy_orig: np.ndarray,
                        doy_base: np.ndarray | None = None,
                        doy_fut: np.ndarray | None = None,
                        months_orig: np.ndarray | None = None,
                        months_base: np.ndarray | None = None,
                        months_fut: np.ndarray | None = None,
                        window: int = 31,
                        floor_frac: float = 0.05,
                        clip: tuple = (0.2, 3.0),
                        ccs_scale: str = 'seasonal') -> np.ndarray:
    """Smoothed day-of-year change-factor with PresRAT-style mean conservation,
    for zero-inflated non-negative variables (heating energy, hydro inflow).

    This is the multiplicative "change factor" / "delta scaling" method (a
    change-factor method in the sense of Lazoglou et al. 2024; the dominant
    approach in climate-impact studies, e.g. Gasparrini-type health work and
    the heating/cooling degree-day literature), made robust for a zero-inflated
    variable by three standard ingredients:

      1. Smoothed seasonal change factor. f(d) = clim_fut(d) / clim_base(d),
         where clim_* are the SMOOTHED day-of-year climatologies of the C2E
         future and baseline (running mean, `window` days). Indexing the factor
         by CALENDAR position, not by the value's quantile, is the key fix: it
         is meaningful even when the network and C2E distributions have
         different seasonal support (e.g. C2E has almost no spring heating while
         the network has plenty), the case where per-quantile mapping produces
         spurious blow-ups.
      2. Dry-season flooring (the change-factor analogue of SSR; Vrac et al.
         2016). Where the baseline climatology is below `floor_frac` of its
         annual max, there is essentially no heating/inflow to rescale, so the
         factor is set to 1 (no-op) rather than a noise-driven ratio. The factor
         is also clipped to `clip` as a final physical guard.
      3. PresRAT mean conservation (Pierce et al. 2015). After applying f(d),
         the realised mean change on each CCS window (monthly / seasonal /
         annual) is forced to equal the raw C2E model's mean change on that
         window. For a cyclic hydro reservoir this conserves the ANNUAL inflow
         VOLUME, which is the physically binding quantity; for heating it pins
         the seasonal totals to the model's change.

    The network's own within-day and synoptic SHAPE is preserved throughout
    (only a smooth daily factor multiplies it). Units cancel in every ratio, so
    the C2E file unit convention is irrelevant.

    orig is the series being rewritten (network chronology). base/fut are the
    C2E baseline/future samples; doy_* and months_* give their day-of-year and
    month (defaults assume same length/grid as orig). Returns a non-negative
    array the length of orig.

    References: Cannon et al. 2015 (J. Climate 28:6938-6959); Pierce et al. 2015
    (PresRAT, mean conservation); Vrac et al. 2016 (SSR, dry-value handling);
    Lehner et al. 2023 (ASCMO 9:29-44, comparison). Themesl et al. 2012 is the
    alternative frequency-adaptation reference.
    """
    orig = np.asarray(orig, float)
    base = np.asarray(base, float)
    fut = np.asarray(fut, float)
    doy_orig = np.asarray(doy_orig)
    doy_base = doy_orig if doy_base is None else np.asarray(doy_base)
    doy_fut = doy_orig if doy_fut is None else np.asarray(doy_fut)
    if months_orig is None:
        months_orig = ((doy_orig // 30) % 12) + 1  # only used to bin CCS windows
    months_orig = np.asarray(months_orig)
    months_base = (months_orig if months_base is None else np.asarray(months_base))
    months_fut = (months_orig if months_fut is None else np.asarray(months_fut))

    # ---- step 1: smoothed seasonal change factor ----
    clim_b = _doy_climatology(base, doy_base, window)
    clim_f = _doy_climatology(fut, doy_fut, window)
    thr = clim_b.max() * floor_frac if clim_b.max() > 0 else np.inf
    with np.errstate(divide='ignore', invalid='ignore'):
        f_doy = np.where(clim_b > thr, clim_f / np.maximum(clim_b, 1e-12), 1.0)
    f_doy = np.clip(f_doy, clip[0], clip[1])
    out = orig * f_doy[np.clip(doy_orig, 0, 364)]

    # ---- step 2: PresRAT mean conservation on the chosen window ----
    if ccs_scale == 'monthly':
        groups = [[m] for m in range(1, 13)]
    elif ccs_scale == 'annual':
        groups = [list(range(1, 13))]
    else:  # seasonal (default)
        groups = [[12, 1, 2], [3, 4, 5], [6, 7, 8], [9, 10, 11]]

    for g in groups:
        mo = np.isin(months_orig, g)
        mb = np.isin(months_base, g)
        mf = np.isin(months_fut, g)
        if not mo.any() or not mb.any() or not mf.any():
            continue
        orig_mean = float(orig[mo].mean())
        mapped_mean = float(out[mo].mean())
        base_mean = float(base[mb].mean())
        fut_mean = float(fut[mf].mean())
        f = _ccs_factor(orig_mean, mapped_mean, base_mean, fut_mean)
        out[mo] = out[mo] * f

    return np.clip(out, 0.0, None)


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
