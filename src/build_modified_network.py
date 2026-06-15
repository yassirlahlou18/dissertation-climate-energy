"""Builds a climate-modified network for one method (qdm or direct). See the pipeline guide PDF."""

from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import os
import argparse
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import config
from mapping import (BUS_TO_COUNTRY, SUPPLY_CARRIER_TO_C2E, region_of, country_of_bus)
import cf_methods as cfm
import c2e_loader
import heat_cooling as hc


def _load_supply(period, n_snapshots):
    files = config.c2e_supply_files(period)
    data = {}
    for var, path in files.items():
        if os.path.exists(path):
            data[var] = c2e_loader.load_c2e_file(path, n_snapshots, config.SNAPSHOT_FREQ)
        else:
            print(f"  WARNING: missing supply file {path}")
            data[var] = {}
    return data


def _load_hydro(period, n_snapshots):
    files = config.c2e_hydro_files(period)
    data = {}
    for var, path in files.items():
        if os.path.exists(path):
            try:
                data[var] = c2e_loader.load_c2e_file(path, n_snapshots, config.SNAPSHOT_FREQ)
            except Exception as e:
                print(f"  WARNING: could not load hydro {var} ({path}): {e}"); data[var] = None
        else:
            print(f"  NOTE: hydro file not found ({var}): {path}"); data[var] = None
    return data


def _load_demand(period, n_snapshots):
    files = config.c2e_demand_files(period)
    data = {}
    for var, path in files.items():
        if os.path.exists(path):
            try:
                data[var] = c2e_loader.load_c2e_file(path, n_snapshots, config.SNAPSHOT_FREQ)
            except Exception as e:
                print(f"  WARNING: could not load {var} ({path}): {e}")
                data[var] = None
        else:
            if var == 'temperature':
                print(f"  NOTE: no temperature file (expected - C2E has none). "
                      f"Heat-pump COP will be handled per HEAT_COP_MODE='{getattr(config,'HEAT_COP_MODE','keep')}'.")
            else:
                print(f"  NOTE: demand file not found ({var}): {path}")
            data[var] = None
    return data


def modify_supply(n, method, supply_future, supply_base, log=print):
    orig = n.generators_t.p_max_pu.copy()
    swap_log = []
    swapped = skipped = 0
    for gen in n.generators_t.p_max_pu.columns:
        carrier = str(n.generators.loc[gen, 'carrier'])
        bus = str(n.generators.loc[gen, 'bus'])
        var = SUPPLY_CARRIER_TO_C2E.get(carrier)
        country = country_of_bus(bus)
        if var is None or country is None:
            skipped += 1
            continue
        fut_dict = supply_future.get(var, {})
        base_dict = supply_base.get(var, {})
        if country not in fut_dict:
            skipped += 1
            continue
        orig_cf = n.generators_t.p_max_pu[gen]
        fut = fut_dict[country]
        base = base_dict.get(country)
        try:
            new = cfm.build_modified_cf(method, orig_cf, fut, c2e_baseline=base)
        except Exception as e:
            log(f"    {gen}: {e}")
            skipped += 1
            continue
        n.generators_t.p_max_pu[gen] = new.values
        swapped += 1
        swap_log.append((gen, carrier, region_of(bus), country))
    log(f"  [supply] method={method}: modified {swapped}, skipped {skipped}")
    return orig, swap_log


def build(method: str):
    import pypsa
    out = config.run_dir(method)
    os.makedirs(out, exist_ok=True)
    log_lines = []
    def log(s):
        print(s); log_lines.append(s)

    log("=" * 70)
    log(f"BUILD MODIFIED NETWORK | method={method} | wy{config.WEATHER_YEAR} "
        f"-> C2E {config.C2E_FUTURE}")
    log("=" * 70)

    net_file = config.network_file()
    log(f"[1] Loading {os.path.basename(net_file)}")
    n = pypsa.Network(net_file)
    ns = len(n.snapshots)
    log(f"    {len(n.buses)} buses, {len(n.generators)} gens, {ns} snapshots")

    log(f"[2] Loading C2E supply (baseline {config.C2E_BASELINE}, future {config.C2E_FUTURE})")
    supply_future = _load_supply(config.C2E_FUTURE, ns)
    supply_base = _load_supply(config.C2E_BASELINE, ns) if method == 'qdm' else {}

    log(f"[3] Modifying generator capacity factors")
    orig_pmpu, swap_log = modify_supply(n, method, supply_future, supply_base, log)

    # ---- hydropower (supply-side weather variable; same method as wind/solar) ----
    if getattr(config, 'DO_HYDRO', True):
        log(f"[3b] Modifying hydropower (inflow + run-of-river)")
        hyd_future = _load_hydro(config.C2E_FUTURE, ns)
        hyd_base = _load_hydro(config.C2E_BASELINE, ns)
        hc.modify_hydro(n, method,
                        hyd_future.get('inflow'), hyd_base.get('inflow'),
                        hyd_future.get('ror'), hyd_base.get('ror'), log)

    # ---- demand side ----
    # v16: demand uses the SAME method as supply, so each run is one coherent
    # climate world. qdm -> daily QDM heating multipliers and climatological
    # cooling change; direct -> C2E-future shape transplants and raw cooling.
    _dm = method
    demand_summaries = {}
    if config.DO_HEAT_DEMAND or config.DO_HEAT_PUMP_COP or config.DO_COOLING:
        log(f"[4] Loading C2E demand series (method = {method})")
        # demand always needs the baseline period to form a relative change
        dem_future = _load_demand(config.C2E_FUTURE, ns)
        dem_base = _load_demand(config.C2E_BASELINE, ns)

        if config.DO_HEAT_DEMAND and dem_future.get('heating'):
            log("    [4a] heating demand")
            demand_summaries['heat'] = hc.modify_heat_demand(
                n, _dm, dem_future['heating'], dem_base.get('heating'), log)
        if config.DO_HEAT_PUMP_COP:
            if dem_future.get('temperature'):
                log("    [4b] heat-pump COP (from C2E temperature)")
                demand_summaries['cop'] = hc.modify_heat_pump_cop(
                    n, dem_future['temperature'], dem_base.get('temperature'),
                    sink_t=config.HEAT_PUMP_SINK_T, log=log)
            elif config.HEAT_COP_MODE == 'proxy':
                log("    [4b] heat-pump COP (temperature proxy from heating-demand change)")
                demand_summaries['cop'] = hc.modify_cop_from_demand_proxy(
                    n, method, dem_future.get('heating'), dem_base.get('heating'),
                    sink_t=config.HEAT_PUMP_SINK_T, log=log)
            else:  # 'keep'
                log("    [4b] heat-pump COP: KEPT at original values "
                    "(no C2E temperature file; conservative choice, documented)")
        if config.DO_COOLING and dem_future.get('cooling'):
            log("    [4c] cooling demand")
            demand_summaries['cool'] = hc.modify_cooling(
                n, method, dem_future.get('cooling'), dem_base.get('cooling'),
                log=log)

    # ---- save ----
    log(f"[5] Saving modified network")
    net_subdir = os.path.join(out, 'networks'); os.makedirs(net_subdir, exist_ok=True)
    mod_file = os.path.join(net_subdir, f"modified_{method}_wy{config.WEATHER_YEAR}_c2e{config.C2E_FUTURE}.nc")
    n.export_to_netcdf(mod_file)
    log(f"    {mod_file}")

    # ---- climate-signal detail (returned for reporting) ----
    cf_change_df = _cf_change_table(n, orig_pmpu, swap_log)

    with open(os.path.join(out, f"build_log_{method}.txt"), 'w') as f:
        f.write("\n".join(log_lines))
    log("Done.")
    return mod_file, cf_change_df, demand_summaries


def _cf_change_table(n, orig_pmpu, swap_log):
    rows = []
    for gen, carrier, reg, country in swap_log:
        om = float(orig_pmpu[gen].mean()); nm = float(n.generators_t.p_max_pu[gen].mean())
        os_ = float(orig_pmpu[gen].std()); nstd = float(n.generators_t.p_max_pu[gen].std())
        rows.append({'generator': gen, 'carrier': carrier, 'region': reg, 'country': country,
                     'orig_mean_CF': om, 'new_mean_CF': nm,
                     'pct_change_mean': (nm - om) / om * 100 if om > 1e-4 else 0,
                     'orig_std': os_, 'new_std': nstd,
                     'pct_change_std': (nstd - os_) / os_ * 100 if os_ > 1e-4 else 0})
    import pandas as pd
    return pd.DataFrame(rows)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--method', default='qdm', choices=['direct', 'qdm'])
    args = ap.parse_args()
    build(args.method)
