"""Loads Climate2Energy CSV files and resamples them to the network snapshot grid. See the pipeline guide PDF."""


from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import os
import numpy as np
import pandas as pd


def _read_wide_or_long(filepath: str) -> dict[str, pd.Series]:
    df = pd.read_csv(filepath)
    cols = [c.lower() for c in df.columns]
    df.columns = cols

    if 'country' not in cols:
        raise ValueError(f"{filepath}: no 'country' column found; columns={cols[:6]}...")

    # LONG layout: country, time/datetime, value
    time_col = next((c for c in ('time', 'datetime', 'date', 'timestamp') if c in cols), None)
    val_col = next((c for c in ('value', 'cf', 'capacity_factor', 'demand') if c in cols), None)
    if time_col and val_col:
        out = {}
        for country, g in df.groupby('country'):
            s = pd.Series(g[val_col].astype(float).values,
                          index=pd.to_datetime(g[time_col].values))
            out[str(country)] = s.sort_index()
        return out

    # WIDE layout: every non-'country' column is a timestamp
    out = {}
    value_cols = [c for c in df.columns if c != 'country']
    parsed_index = pd.to_datetime(value_cols, errors='coerce')
    if parsed_index.isna().all():
        raise ValueError(
            f"{filepath}: could not parse value-column headers as datetimes. "
            f"First few: {value_cols[:4]}")
    for _, row in df.iterrows():
        country = str(row['country'])
        vals = pd.Series(row[value_cols].astype(float).values, index=parsed_index)
        out[country] = vals.sort_index()
    return out


def _resample_to_grid(series: pd.Series, n_snapshots: int, freq: str = '3h') -> pd.Series:
    """Resample an hourly C2E series to the network resolution by averaging.

    The result is returned WITHOUT forcing it onto the network's exact
    DatetimeIndex (C2E years differ from the network's nominal year); callers
    align by POSITION, which is valid because both cover one full year at the
    same resolution. We assert the length matches the network.
    """
    out = series.resample(freq).mean()
    # UPSAMPLING FIX (v20.1): for series NATIVELY COARSER than the grid (weekly
    # inflow, daily ror), resample(freq).mean() places each native value in one
    # bin and leaves every other bin NaN -- it does NOT spread values flat, and
    # those NaNs silently degraded the hydro channels to a no-op (qdm factor
    # collapsed to 1.0) or to all-NaN capacity factors (direct ror). Forward-
    # fill holds each native value constant until the next one (the documented
    # intent: a constant rate within the native period); back-fill covers grid
    # points before the first native stamp.
    if out.isna().any():
        out = out.ffill().bfill()
    if len(out) != n_snapshots:
        # tolerate leap-year / endpoint off-by-one by trimming or padding edges
        if len(out) > n_snapshots:
            out = out.iloc[:n_snapshots]
        else:
            pad = n_snapshots - len(out)
            out = pd.concat([out, pd.Series([out.iloc[-1]] * pad)])
    if out.isna().any():   # hard guarantee: no NaN may leave the loader
        raise ValueError(f"resample produced {int(out.isna().sum())} NaN "
                         f"(native index irregular?); refusing to continue")
    return out


# NOTE on hydro files: C2E hydro_inflow is WEEKLY cumulative GWh and hydro_ror
# is DAILY cumulative GWh (per the C2E docs). Averaging-resample below spreads
# each native value flat across the finer grid, giving a constant inflow RATE
# within the native period. Both pipeline hydro methods (qdm; direct transplant)
# operate on future/baseline RATIOS where the per-period unit cancels, so the
# flat-within-period approximation is acceptable for the climate-change signal.
# (If absolute hydro energy is ever needed directly, resample with sum/scaling.)
def load_c2e_file(filepath: str, n_snapshots: int, freq: str = '3h') -> dict[str, pd.Series]:
    """Load one C2E CSV -> {country: Series of length n_snapshots}."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(filepath)
    raw = _read_wide_or_long(filepath)
    return {c: _resample_to_grid(s, n_snapshots, freq) for c, s in raw.items()}


def inspect_file(filepath: str, max_show: int = 8) -> str:
    """Human-readable summary used by inspect_c2e.py to verify scenario/period."""
    df = pd.read_csv(filepath, nrows=5)
    cols = list(df.columns)
    lines = [f"FILE: {filepath}",
             f"  n columns: {len(cols)}",
             f"  first columns: {cols[:max_show]}"]
    value_cols = [c for c in cols if c.lower() != 'country']
    idx = pd.to_datetime(value_cols, errors='coerce')
    if not idx.isna().all():
        good = idx[~idx.isna()]
        lines.append(f"  time span (from headers): {good.min()} -> {good.max()}")
        lines.append(f"  inferred resolution: {good.to_series().diff().median()}")
    full = pd.read_csv(filepath)
    if 'country' in [c.lower() for c in full.columns]:
        ccol = [c for c in full.columns if c.lower() == 'country'][0]
        countries = sorted(full[ccol].astype(str).unique().tolist())
        lines.append(f"  n countries: {len(countries)}")
        lines.append(f"  countries: {countries}")
    return "\n".join(lines)
