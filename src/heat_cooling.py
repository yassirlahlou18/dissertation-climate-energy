"""Applies the climate signal to heating, cooling and hydro demand. See the pipeline guide PDF."""


from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import numpy as np
import pandas as pd

from mapping import (BUS_TO_COUNTRY, region_of, country_of_bus,
                     HEAT_LOAD_KEYS, HEAT_PUMP_CARRIERS)
import cf_methods as cfm


# ---- Ruhnau et al. 2019 air-source COP regression (sink-source dT) ----------
def cop_air(delta_t: np.ndarray) -> np.ndarray:
    return 6.81 - 0.121 * delta_t + 0.000630 * delta_t ** 2


def cop_ground(delta_t: np.ndarray) -> np.ndarray:
    # ground-source regression (less variable source temp)
    return 8.77 - 0.150 * delta_t + 0.000734 * delta_t ** 2


# ----------------------------------------------------------------------------
# Identify heat loads and heat-pump links in a network
# ----------------------------------------------------------------------------
def find_heat_loads(n) -> list[str]:
    """Return load names whose bus is a heat bus and have a time series."""
    if not hasattr(n.loads_t, 'p_set') or len(n.loads_t.p_set.columns) == 0:
        return []
    heat = []
    for load_name in n.loads_t.p_set.columns:
        bus = n.loads.loc[load_name, 'bus'] if load_name in n.loads.index else load_name
        if any(k in str(bus) for k in HEAT_LOAD_KEYS) or any(k in str(load_name) for k in HEAT_LOAD_KEYS):
            heat.append(load_name)
    return heat


def find_heat_pump_links(n) -> list[str]:
    if 'carrier' not in n.links.columns:
        return []
    mask = n.links['carrier'].astype(str).apply(
        lambda c: any(hp in c for hp in HEAT_PUMP_CARRIERS) or 'heat pump' in c)
    return list(n.links.index[mask])


# ----------------------------------------------------------------------------
# (A) Heating demand modification
# ----------------------------------------------------------------------------
def _daily_energy(series: pd.Series, weights: pd.Series) -> pd.Series:
    """Energy per calendar day (value x snapshot weight, summed per day)."""
    e = pd.Series(series.values * weights.values, index=series.index)
    return e.groupby(e.index.normalize()).sum()


def _qdm_daily_multiplier(orig_daily: pd.Series, base_daily: pd.Series,
                          fut_daily: pd.Series, mult_cap: float = 5.0,
                          floor_frac: float = 0.02) -> pd.Series:
    """Per-DAY QDM multiplier with a 3-month centred window (months m-1,m,m+1,
    circular), the v16 'daily scaling factor' for heating.

    Why a window: the C2E baseline and future are free-running climate-model
    years, not synchronised with the real weather year (Wohland et al. 2025),
    so day-paired ratios would be synoptic noise. Pooling ~90 daily samples per
    window gives robust quantile estimates while staying seasonally local;
    within the window the change is read off per quantile, so cold-extreme days
    carry their own change rather than the monthly mean.

    Guards: multipliers clipped to [0, mult_cap]; days whose window baseline is
    essentially zero heating (below floor_frac of the annual-mean daily energy
    on BOTH C2E sides) get multiplier 1 (summer no-op, avoids 0/0 noise).
    """
    months_o = orig_daily.index.month
    months_b = base_daily.index.month
    months_f = fut_daily.index.month
    out = pd.Series(1.0, index=orig_daily.index)
    base_floor = float(base_daily.mean()) * floor_frac
    for m in range(1, 13):
        win = {(m - 2) % 12 + 1, m, m % 12 + 1}
        io = np.where(months_o == m)[0]
        ib = np.where(np.isin(months_b, list(win)))[0]
        if_ = np.where(np.isin(months_f, list(win)))[0]
        if io.size == 0 or ib.size == 0 or if_.size == 0:
            continue
        ob = orig_daily.values[io]
        bb = base_daily.values[ib]
        fb = fut_daily.values[if_]
        if bb.mean() < base_floor and fb.mean() < base_floor:
            continue  # deep summer: leave at 1.0
        adj = cfm._qdm_block(ob, bb, fb, jitter=1e-6)
        with np.errstate(divide='ignore', invalid='ignore'):
            mult = np.where(ob > 1e-9, adj / ob, 1.0)
        out.iloc[io] = np.clip(mult, 0.0, mult_cap)
    return out


def modify_heat_demand(n, method, c2e_heat_future, c2e_heat_baseline=None,
                       log=print) -> dict:
    """Apply the C2E climate signal to the network heat loads. v16 design.

    The network's calibrated heat demand LEVEL is always preserved; only the
    climate change is imported. The two methods are two coherent worlds:

    qdm (primary): DAILY quantile delta multipliers per country (see
      _qdm_daily_multiplier), applied uniformly to all snapshots of each day so
      the network's own within-day profile is preserved. This is the 'daily
      scaling factor' at distribution level: mild days and cold-extreme days
      each carry their own change, and the heating-season shortening shows up
      at daily granularity instead of being smeared over months.

    direct: SHAPE TRANSPLANT. Each heat load takes the C2E future hourly shape
      with its annual level anchored via the C2E baseline:
        new(t) = floor + fut(t) * (variable_annual_orig / annual(base))
      where floor is the load's 5th percentile (the roughly constant hot-water
      part, which demand.ninja's space-heating signal should not erase) and
      variable_annual_orig is the original annual energy above that floor. The
      annual change then equals the C2E future/baseline annual ratio, and the
      hour-to-hour realisation is the C2E future year, consistent with the raw
      capacity-factor substitution of the direct world. Units cancel in the
      anchor ratio, so the heating-file unit convention is irrelevant here too.

    c2e_heat_* are dicts {country: Series on the snapshot grid}.
    """
    heat_loads = find_heat_loads(n)
    summary = {'n_heat_loads': len(heat_loads), 'modified': 0, 'skipped': 0,
               'by_country': {}, 'method': method, 'country_stats': {}}
    if not heat_loads:
        log("  [heat] no heat loads found in loads_t.p_set")
        return summary
    if c2e_heat_baseline is None:
        log("  [heat] no C2E baseline heating series -> heat loads UNCHANGED")
        return summary

    w = n.snapshot_weightings.generators
    snaps = n.loads_t.p_set.index

    # group heat loads by country
    by_country = {}
    for load_name in heat_loads:
        bus = n.loads.loc[load_name, 'bus'] if load_name in n.loads.index else load_name
        country = country_of_bus(str(bus))
        if country is None or country not in c2e_heat_future or country not in c2e_heat_baseline:
            summary['skipped'] += 1
            continue
        by_country.setdefault(country, []).append(load_name)

    for country, cols in by_country.items():
        fut = c2e_heat_future[country]
        base = c2e_heat_baseline[country]
        if method == 'qdm':
            nat = n.loads_t.p_set[cols].sum(axis=1)
            mult_d = _qdm_daily_multiplier(_daily_energy(nat, w),
                                           _daily_energy(base, w),
                                           _daily_energy(fut, w))
            mult_t = mult_d.reindex(snaps.normalize()).values
            for c in cols:
                n.loads_t.p_set[c] = np.clip(
                    n.loads_t.p_set[c].values * mult_t, 0, None)
            summary['country_stats'][country] = {
                'mult_min': float(np.nanmin(mult_d.values)),
                'mult_mean': float(np.nanmean(mult_d.values)),
                'mult_max': float(np.nanmax(mult_d.values))}
        elif method == 'direct':
            base_tot = float((base.values * w.values).sum())
            fut_v = np.clip(np.asarray(fut.values, float), 0, None)
            if base_tot <= 1e-9 or not np.isfinite(base_tot):
                summary['skipped'] += len(cols)
                continue
            for c in cols:
                orig = np.asarray(n.loads_t.p_set[c].values, float)
                floor = float(np.percentile(orig, 5))
                var_annual = float(((orig - floor).clip(min=0) * w.values).sum())
                new = floor + fut_v * (var_annual / base_tot)
                n.loads_t.p_set[c] = np.clip(new, 0, None)
            summary['country_stats'][country] = {
                'annual_ratio': float((fut.values * w.values).sum() / base_tot)}
        else:
            raise ValueError(f"unknown heating method '{method}' (valid: qdm, direct)")
        summary['modified'] += len(cols)
        summary['by_country'][country] = len(cols)

    log(f"  [heat] method={method}: modified {summary['modified']} heat loads "
        f"in {len(by_country)} countries, skipped {summary['skipped']}")
    return summary




def _qdm_demand(orig, base, fut, months):
    """QDM for an unbounded non-negative demand series (no [0,1] clip)."""
    orig = np.asarray(orig, float); base = np.asarray(base, float); fut = np.asarray(fut, float)
    months = np.asarray(months)
    out = np.empty_like(orig)
    # align month vectors for the C2E samples
    mb = pd.Series(base).index  # placeholder
    for m in range(1, 13):
        io = np.where(months == m)[0]
        if io.size == 0:
            continue
        # use whole-year C2E samples per month if month info available; here we
        # assume base/fut are full-year same-length as orig (typical), else
        # fall back to whole-series quantiles.
        if base.size == orig.size and fut.size == orig.size:
            ib = io; if_ = io
        else:
            ib = np.arange(base.size); if_ = np.arange(fut.size)
        block = cfm._qdm_block(orig[io], base[ib], fut[if_], 1e-6)
        # guard: never inflate a heat load beyond 5x its original (shoulder-month
        # near-zero quantiles can otherwise produce noise-driven blowups)
        cap = np.maximum(orig[io] * 5.0, np.nanmax(orig) * 0.01 if np.nanmax(orig) > 0 else 1.0)
        out[io] = np.clip(block, 0, cap)
    return out


# ----------------------------------------------------------------------------
# (B) Heat-pump COP modification
# ----------------------------------------------------------------------------
def modify_heat_pump_cop(n, temp_future, temp_baseline=None, sink_t=55.0,
                         method='shift', log=print) -> dict:
    """Recompute time-varying COP (link efficiency) under future temperatures.

    PyPSA stores time-varying link efficiency in n.links_t.efficiency.
    For each HP link we map bus -> country -> future temperature series, compute
    COP_future from the Ruhnau curve, and:
      method='shift'   : new_eff = old_eff * (COP_fut / COP_base)   [preserves
                         any plant-specific calibration already in the network]
      method='replace' : new_eff = COP_fut                          [overwrite]

    temp_* are dicts {country: Series (deg C) on snapshot grid}. If temperature
    is unavailable this step is skipped (and the report flags COP as unmodified).
    """
    summary = {'n_hp_links': 0, 'modified': 0, 'skipped': 0}
    hp_links = find_heat_pump_links(n)
    summary['n_hp_links'] = len(hp_links)
    if not hp_links or temp_future is None:
        log("  [COP] no HP links or no temperature data -> COP unmodified")
        return summary

    if not hasattr(n.links_t, 'efficiency'):
        n.links_t.efficiency = pd.DataFrame(index=n.snapshots)

    for link in hp_links:
        bus0 = n.links.loc[link, 'bus0']
        country = country_of_bus(str(bus0))
        carrier = str(n.links.loc[link, 'carrier'])
        if country is None or country not in temp_future:
            summary['skipped'] += 1
            continue

        t_fut = np.asarray(temp_future[country].values, float)
        cop_fn = cop_ground if 'ground' in carrier else cop_air
        cop_future = cop_fn(sink_t - t_fut)
        cop_future = np.clip(cop_future, 1.0, 7.0)

        if method == 'replace' or temp_baseline is None:
            new_eff = cop_future
        else:
            t_base = np.asarray(temp_baseline[country].values, float)
            cop_base = np.clip(cop_fn(sink_t - t_base), 1.0, 7.0)
            # existing efficiency (may be scalar or series)
            if link in getattr(n.links_t, 'efficiency', pd.DataFrame()).columns:
                old = np.asarray(n.links_t.efficiency[link].values, float)
            else:
                old = np.full(len(n.snapshots), float(n.links.loc[link, 'efficiency']))
            new_eff = old * (cop_future / np.clip(cop_base, 1e-3, None))

        n.links_t.efficiency[link] = np.clip(new_eff, 1.0, 7.0)
        summary['modified'] += 1

    log(f"  [COP] modified {summary['modified']} HP links, skipped {summary['skipped']}")
    return summary


# ----------------------------------------------------------------------------
# (C) Cooling demand
# ----------------------------------------------------------------------------
C2E_DEMAND_GWH_PER_H_TO_MW = 1000.0  # C2E demand csvs are hourly GWh


def _month_hour_climatology(series: pd.Series) -> pd.Series:
    """Month x hour-of-day mean of a snapshot-grid series, evaluated back on
    every snapshot. ~30 samples per cell with one year of data; chronology-free
    by construction (the climate years are not synchronised with real years)."""
    idx = series.index
    months = np.asarray(idx.month)
    hours = np.asarray(idx.hour)
    vals = np.asarray(series.values, float)
    # mean per (month, hour) cell, mapped back to every timestamp
    df = pd.DataFrame({'m': months, 'h': hours, 'v': vals})
    cell_mean = df.groupby(['m', 'h'])['v'].transform('mean')
    return pd.Series(cell_mean.values, index=idx)


def modify_cooling(n, method, c2e_cooling_future, c2e_cooling_baseline,
                   log=print) -> dict:
    """v16 cooling: extract the embedded historical cooling, then add the
    future cooling, per method. Replaces the retired IEA 3% anchor design.

    Rationale: the Gotske electricity loads are built from historical ENTSO-E
    shapes, so the weather year's cooling is already embedded in them; adding
    cooling on top double counts the base. C2E's cooling comes from
    demand.ninja with country sensitivities calibrated on observed demand
    response (Wohland et al. 2025; Staffell et al. 2023), so the C2E BASELINE
    cooling is precisely an estimate of that embedded component, extracted
    synthetically from historical data.

    Mechanics, per country, on the country's electricity loads (distributed
    proportionally to each load's annual energy):
      subtract clim_base(t)   the month x hour climatology of C2E baseline
                              cooling (hour-paired subtraction is invalid
                              because the model years are free-running)
      then add, by method:
        qdm    : clim_fut(t), the same climatology of the FUTURE cooling, so
                 the net effect is the climatological cooling change on the
                 network's own chronology. Conservative on event coincidence
                 (peaks land at climatologically right hours, not synchronised
                 with the weather year's specific heat waves); stated openly.
        direct : the raw C2E future hourly series, so heat-wave cooling spikes
                 coincide with that model year's wind and solar, physically
                 consistent within the C2E world.
      guard: final load floored at 0; clipped energy is accounted and logged.

    Units: C2E demand is hourly GWh; converted here (x1000 to MW). This is the
    ONE place in the pipeline where C2E demand LEVELS enter (heating and hydro
    use level-cancelling anchors), so the audit prints a sanity check of base
    cooling vs national electricity demand.
    """
    summary = {'method': method, 'modified_loads': 0, 'skipped_countries': [],
               'by_country': {}}
    if not c2e_cooling_future or not c2e_cooling_baseline:
        log("  [cool] missing C2E cooling (need BOTH baseline and future) -> no-op")
        return summary

    if 'carrier' in n.loads.columns:
        elec_loads = n.loads.index[n.loads.carrier == 'electricity']
    else:
        elec_loads = pd.Index([])
    if len(elec_loads) == 0:
        ac_buses = set(n.buses.query('carrier in ["AC", "low voltage"]').index)
        elec_loads = n.loads.index[n.loads.bus.isin(ac_buses)]
    elec_loads = [l for l in elec_loads if l in n.loads_t.p_set.columns]
    if not elec_loads:
        log("  [cool] no electricity loads found -> no-op")
        return summary

    w = n.snapshot_weightings.generators

    by_country = {}
    for l in elec_loads:
        c = country_of_bus(str(n.loads.loc[l, 'bus']))
        if c is not None:
            by_country.setdefault(c, []).append(l)

    for country, cols in by_country.items():
        if country not in c2e_cooling_future or country not in c2e_cooling_baseline:
            summary['skipped_countries'].append(country)
            continue
        base = c2e_cooling_baseline[country] * C2E_DEMAND_GWH_PER_H_TO_MW
        fut = c2e_cooling_future[country] * C2E_DEMAND_GWH_PER_H_TO_MW
        clim_b = _month_hour_climatology(base)
        add_t = (_month_hour_climatology(fut) if method == 'qdm'
                 else pd.Series(np.clip(fut.values, 0, None), index=fut.index))
        net = (add_t.values - clim_b.values)

        ann = {c: float((n.loads_t.p_set[c].values * w.values).sum()) for c in cols}
        tot = sum(ann.values())
        if tot <= 0:
            summary['skipped_countries'].append(country)
            continue
        clipped_mwh = 0.0
        for c in cols:
            share = ann[c] / tot
            newv = n.loads_t.p_set[c].values + net * share
            neg = np.minimum(newv, 0.0)
            clipped_mwh += float((-neg * w.values).sum())
            n.loads_t.p_set[c] = np.maximum(newv, 0.0)
            summary['modified_loads'] += 1

        base_twh = float((base.values * w.values).sum()) / 1e6
        fut_added_twh = float((add_t.values * w.values).sum()) / 1e6
        summary['by_country'][country] = {
            'embedded_base_cooling_TWh': base_twh,
            'added_future_cooling_TWh': fut_added_twh,
            'net_change_TWh': float((net * w.values).sum()) / 1e6,
            'share_of_elec_demand_pct': 100 * base_twh / (tot / 1e6) if tot else float('nan'),
            'clipped_TWh': clipped_mwh / 1e6,
        }
        if clipped_mwh / 1e6 > 0.001 * tot / 1e6:
            log(f"    [cool] {country}: clipping at zero removed "
                f"{clipped_mwh/1e6:.3f} TWh (>{0.1}% of demand), check levels")

    nc = len(summary['by_country'])
    net_eu = sum(v['net_change_TWh'] for v in summary['by_country'].values())
    base_eu = sum(v['embedded_base_cooling_TWh'] for v in summary['by_country'].values())
    log(f"  [cool] method={method}: {nc} countries; embedded base extracted "
        f"{base_eu:.1f} TWh, net cooling change {net_eu:+.1f} TWh; "
        f"skipped {sorted(set(summary['skipped_countries']))}")
    return summary


def modify_cop_from_demand_proxy(n, method, c2e_heat_future, c2e_heat_baseline,
                                 sink_t=55.0, hdd_base=15.5, log=print) -> dict:
    """Reconstruct an effective monthly temperature shift from the C2E heating-
    demand change, then apply the Ruhnau COP curve.

    Rationale: heating demand from demand.ninja/BAIT scales (approximately) with
    heating degree days HDD = max(hdd_base - T, 0). For a month with mean source
    temperature T_base and demand D_base, a future demand D_fut implies, under
    the proportional-HDD assumption,
        HDD_fut / HDD_base = D_fut / D_base
        (hdd_base - T_fut) = (D_fut/D_base) * (hdd_base - T_base)
    We do not know T_base from C2E, so we assume a typical heating-season monthly
    mean source temperature climatology per month (Northern-Europe default) and
    solve for T_fut, then COP_fut = Ruhnau(sink_t - T_fut). This is clearly an
    approximation and is only used when HEAT_COP_MODE='proxy'. Default 'keep'
    avoids it entirely.
    """
    summary = {'n_hp_links': 0, 'modified': 0, 'mode': 'proxy'}
    if c2e_heat_future is None or c2e_heat_baseline is None:
        log("  [COP-proxy] need both baseline and future heating demand -> skipped")
        return summary
    hp_links = find_heat_pump_links(n)
    summary['n_hp_links'] = len(hp_links)
    if not hp_links:
        log("  [COP-proxy] no HP links")
        return summary

    # typical monthly mean outdoor temperature (deg C), Northern/Central Europe
    T_clim = {1: 1, 2: 2, 3: 5, 4: 9, 5: 14, 6: 17, 7: 19,
              8: 18, 9: 14, 10: 10, 11: 5, 12: 2}

    if not hasattr(n.links_t, 'efficiency'):
        n.links_t.efficiency = pd.DataFrame(index=n.snapshots)

    for link in hp_links:
        bus0 = n.links.loc[link, 'bus0']
        country = country_of_bus(str(bus0))
        carrier = str(n.links.loc[link, 'carrier'])
        if country is None or country not in c2e_heat_future or country not in c2e_heat_baseline:
            continue
        fut = c2e_heat_future[country]; base = c2e_heat_baseline[country]
        # monthly demand ratio
        fut_m = fut.groupby(fut.index.month).mean()
        base_m = base.groupby(base.index.month).mean()
        cop_fn = cop_ground if 'ground' in carrier else cop_air
        eff = np.empty(len(n.snapshots))
        months = np.asarray(n.snapshots.month)
        for m in range(1, 13):
            idx = np.where(months == m)[0]
            if idx.size == 0:
                continue
            Tb = T_clim.get(m, 8)
            hdd_b = max(hdd_base - Tb, 0.1)
            ratio = float(fut_m.get(m, np.nan)) / max(float(base_m.get(m, np.nan)), 1e-6)
            if not np.isfinite(ratio):
                ratio = 1.0
            hdd_f = ratio * hdd_b
            T_fut = hdd_base - hdd_f
            cop_f = float(np.clip(cop_fn(sink_t - T_fut), 1.0, 7.0))
            eff[idx] = cop_f
        n.links_t.efficiency[link] = eff
        summary['modified'] += 1
    log(f"  [COP-proxy] modified {summary['modified']} HP links")
    return summary


# ============================================================================
# Hydropower (Gotske vary hydro inflow; we have hydro_inflow + hydro_ror files)
# ============================================================================
def modify_hydro(n, method, inflow_future, inflow_base,
                 ror_future, ror_base, log=print) -> dict:
    """Apply the C2E climate-change signal to hydropower, the weather-dependent
    parts Gotske also vary.

    Two mechanisms in PyPSA-Eur:
      (1) Reservoir + pumped hydro: storage_units_t.inflow (energy, MW). We scale
          the network's own inflow by the C2E inflow RELATIVE change (unit-
          agnostic: only future/baseline ratio is used). Reservoir inflow is an
          energy budget, so a multiplicative ratio is the right operation.
      (2) Run-of-river: generators with carrier 'ror', a capacity factor in
          [0,1] in generators_t.p_max_pu. This is a CF, so we use the SAME CF
          method family as wind/solar (qdm; direct uses an energy-anchored shape transplant), clipped to [0,1].

    Relative-change keeps everything anchored to the network's calibrated hydro
    and isolates the climate signal. Robust to missing countries (left
    unchanged, logged).
    """
    import cf_methods as cfm
    summary = {'inflow_modified': 0, 'ror_modified': 0, 'skipped': 0}
    wv = n.snapshot_weightings.generators.values

    # ---- (1) reservoir / pumped-hydro inflow ----
    has_inflow = (hasattr(n, 'storage_units_t')
                  and hasattr(n.storage_units_t, 'inflow')
                  and len(n.storage_units_t.inflow.columns) > 0)
    if has_inflow and inflow_future:
        unmapped = []
        for su in list(n.storage_units_t.inflow.columns):
            bus = n.storage_units.loc[su, 'bus'] if su in n.storage_units.index else su
            country = country_of_bus(str(bus))
            if country is None or country not in inflow_future:
                summary['skipped'] += 1
                unmapped.append(f"{su} -> {country}")
                continue
            orig = np.asarray(n.storage_units_t.inflow[su].values, float)
            fut = np.asarray(inflow_future[country].values, float)
            base = (np.asarray(inflow_base[country].values, float)
                    if inflow_base and country in inflow_base else None)
            months = np.asarray(n.storage_units_t.inflow[su].index.month)
            if base is None:
                # no baseline -> cannot form a relative change; leave unchanged
                summary['skipped'] += 1
                continue
            if method == 'direct':
                # shape transplant: the C2E future inflow year, anchored to the
                # unit's own annual inflow via the C2E baseline (units cancel,
                # so the GWh-per-week file convention is irrelevant here)
                base_tot = float((base * wv).sum())
                orig_tot = float((orig * wv).sum())
                if base_tot <= 1e-9 or not np.isfinite(base_tot):
                    summary['skipped'] += 1
                    unmapped.append(f"{su} (zero C2E baseline inflow)")
                    continue
                new = np.clip(fut, 0, None) * (orig_tot / base_tot)
            elif method == 'qdm':
                new = _qdm_demand(orig, base, fut, months)
            else:
                raise ValueError(f"unknown hydro method '{method}' (valid: qdm, direct)")
            n.storage_units_t.inflow[su] = np.clip(new, 0, None)
            summary['inflow_modified'] += 1
        if unmapped:
            log(f"  [hydro] inflow units WITHOUT C2E coverage ({len(unmapped)}), left at design values: "
                f"{unmapped}")
            log(f"  [hydro] C2E hydro-inflow file covers: {sorted(inflow_future.keys())}")

    # ---- (2) run-of-river capacity factor ----
    ror_gens = [g for g in n.generators.index
                if str(n.generators.loc[g, 'carrier']).lower() in ('ror', 'run of river', 'hydro')]
    ror_gens = [g for g in ror_gens
                if hasattr(n.generators_t, 'p_max_pu') and g in n.generators_t.p_max_pu.columns]
    if ror_gens and ror_future:
        for g in ror_gens:
            bus = n.generators.loc[g, 'bus']
            country = country_of_bus(str(bus))
            if country is None or country not in ror_future:
                summary['skipped'] += 1
                continue
            orig_cf = n.generators_t.p_max_pu[g]
            fut = ror_future[country]
            base = ror_base.get(country) if ror_base else None
            try:
                if method == 'direct':
                    # the C2E ror file is daily ENERGY, not a capacity factor:
                    # transplant the future shape, anchored so the original
                    # annual CF energy scales by the C2E future/baseline ratio
                    if base is None:
                        raise ValueError('no C2E baseline ror')
                    base_tot = float((np.asarray(base.values, float) * wv).sum())
                    orig_tot = float((np.asarray(orig_cf.values, float) * wv).sum())
                    if base_tot <= 1e-9:
                        raise ValueError('zero C2E baseline ror')
                    vals = np.clip(np.asarray(fut.values, float), 0, None) * (orig_tot / base_tot)
                else:
                    vals = cfm.build_modified_cf('qdm', orig_cf, fut, c2e_baseline=base).values
            except Exception as e:
                log(f"    [hydro-ror] {g}: {e}"); summary['skipped'] += 1; continue
            n.generators_t.p_max_pu[g] = np.clip(vals, 0, 1)
            summary['ror_modified'] += 1

    log(f"  [hydro] method={method}: inflow scaled on {summary['inflow_modified']} "
        f"storage units, ror CF on {summary['ror_modified']} generators, "
        f"skipped {summary['skipped']}")
    return summary
