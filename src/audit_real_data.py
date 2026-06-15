"""Pre-run audit of the real network and C2E files. Solves nothing; prints checks. See the pipeline guide PDF."""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warnings; warnings.filterwarnings('ignore')
import numpy as np, pandas as pd
import pypsa
import config
import c2e_loader
from mapping import (BUS_TO_COUNTRY, SUPPLY_CARRIER_TO_C2E, region_of, country_of_bus)
import heat_cooling as hc

LINE = "=" * 72

def hdr(t): print("\n" + LINE + "\n" + t + "\n" + LINE)


def main():
    hdr("AUDIT OF REAL DATA  (network + C2E), no solve")
    print(f"weather year={config.WEATHER_YEAR}  baseline={config.C2E_BASELINE}  future={config.C2E_FUTURE}")
    nf = config.network_file()
    print(f"network: {nf}")
    n = pypsa.Network(nf)
    ns = len(n.snapshots)
    w = n.snapshot_weightings.generators.values
    print(f"  {len(n.buses)} buses, {len(n.generators)} generators, "
          f"{len(n.storage_units)} storage units, {ns} snapshots")

    # ---------- E. snapshot alignment ----------
    hdr("E. SNAPSHOT / C2E ALIGNMENT")
    print(f"  network snapshots: {ns}  (first {n.snapshots[0]}, last {n.snapshots[-1]})")
    pv_path = config.c2e_supply_files(config.C2E_FUTURE)['pv']
    if os.path.exists(pv_path):
        pv = c2e_loader.load_c2e_file(pv_path, ns, config.SNAPSHOT_FREQ)
        any_c = next(iter(pv))
        print(f"  C2E PV after resample: {len(pv[any_c])} steps for '{any_c}'  "
              f"-> {'MATCH' if len(pv[any_c])==ns else 'MISMATCH!'}")
    else:
        print(f"  !! PV file not found: {pv_path}")

    # ---------- C. supply mapping ----------
    hdr("C. SUPPLY (wind/solar) MAPPING + TURBINE FILES")
    from collections import Counter
    matched = Counter(); skipped = Counter()
    sf = config.c2e_supply_files(config.C2E_FUTURE)
    avail = {v: os.path.exists(p) for v, p in sf.items()}
    print(f"  supply files present: {avail}")
    for g in n.generators_t.p_max_pu.columns:
        car = str(n.generators.loc[g, 'carrier'])
        var = SUPPLY_CARRIER_TO_C2E.get(car)
        c = country_of_bus(str(n.generators.loc[g, 'bus']))
        if var and c:
            matched[car] += 1
        else:
            skipped[car] += 1
    print(f"  mapped CF generators by carrier: {dict(matched)}")
    print(f"  SKIPPED CF generators by carrier: {dict(skipped)}")
    # list which turbine files exist in C2E dir
    c2e_files = sorted(os.listdir(config.C2E_DIR)) if os.path.isdir(config.C2E_DIR) else []
    pref = f'Wind-power_{config.C2E_FUTURE}_'
    turb = sorted({f.split('_onshore')[0].replace(pref, '')
                   for f in c2e_files if f.startswith(pref) and 'onshore' in f})
    print(f"  wind turbine variants available for {config.C2E_FUTURE}: {turb}")
    print(f"  config WIND_TURBINE = {config.WIND_TURBINE}")
    print("  NOTE: qdm uses RELATIVE change (turbine-robust); only 'direct'")
    print("        is sensitive to the absolute turbine power curve.")

    # ---------- A. heating units (thermal vs electric) ----------
    hdr("A. HEATING UNITS  (thermal vs electric?)")
    heat_loads = hc.find_heat_loads(n)
    print(f"  network heat loads found: {len(heat_loads)}")
    # network total heat demand and per-country January peak
    net_heat_by_country = {}
    net_heat_total = 0.0
    for l in heat_loads:
        c = country_of_bus(str(n.loads.loc[l, 'bus']))
        e = float((n.loads_t.p_set[l].values * w).sum()) / 1e6
        net_heat_total += e
        if c:
            net_heat_by_country.setdefault(c, 0.0)
            net_heat_by_country[c] += e
    print(f"  network total heat demand: {net_heat_total:,.0f} TWh/yr (thermal, served via heat-pump/CHP/resistive links)")

    hf = config.c2e_demand_files(config.C2E_FUTURE)['heating']
    hb = config.c2e_demand_files(config.C2E_BASELINE)['heating']
    if os.path.exists(hb):
        c2e_heat = c2e_loader.load_c2e_file(hb, ns, config.SNAPSHOT_FREQ)
        # compare a few big hydro/heat countries
        print(f"\n  {'country':14s} {'network heat TWh':>16s} {'C2E heat TWh':>14s} {'ratio net/C2E':>14s}")
        for c in ['Germany', 'France', 'Italy', 'Spain', 'Sweden', 'Norway']:
            if c in net_heat_by_country and c in c2e_heat:
                # C2E series is hourly power (GW) resampled to the 3h grid; its
                # ENERGY = sum(power * snapshot_weight) to match the network basis.
                c2e_e = float((c2e_heat[c].values * w).sum()) / 1e3  # GW*h -> TWh
                ratio = net_heat_by_country[c] / c2e_e if c2e_e > 0 else float('nan')
                print(f"  {c:14s} {net_heat_by_country[c]:16,.1f} {c2e_e:14,.1f} {ratio:14.2f}")
        print("\n  INTERPRETATION:")
        print("   - ratio ~1     => C2E heating is THERMAL, same basis as the network.")
        print("                     Relative-change application is CLEAN. (best case)")
        print("   - ratio ~2-4   => network thermal is ~COP x C2E electric, i.e. C2E")
        print("                     heating is ELECTRICITY (post heat-pump). Then the")
        print("                     RELATIVE change still applies, but it bundles the")
        print("                     COP change; document that COP is implicitly included")
        print("                     and do NOT also modify COP separately.")
        print("   - ratio random => mapping/units problem; investigate before trusting.")
        print("   (Either way the pipeline uses ONLY the future/baseline ratio, so the")
        print("    level is fine; this check tells you how to INTERPRET the heat result.)")
    else:
        print(f"  !! C2E baseline heating not found: {hb}")

    # ---------- B. hydro ----------
    hdr("B. HYDRO  (does it exist in the network + map to C2E?)")
    has_inflow = (hasattr(n, 'storage_units_t') and hasattr(n.storage_units_t, 'inflow')
                  and len(n.storage_units_t.inflow.columns) > 0)
    print(f"  storage_units_t.inflow present: {has_inflow} "
          f"({len(n.storage_units_t.inflow.columns) if has_inflow else 0} units with inflow)")
    if has_inflow:
        tot_inf = float((n.storage_units_t.inflow.values * w[:, None]).sum()) / 1e6
        print(f"  total reservoir inflow: {tot_inf:,.0f} TWh/yr")
        mapped = sum(1 for su in n.storage_units_t.inflow.columns
                     if country_of_bus(str(n.storage_units.loc[su, 'bus'] if su in n.storage_units.index else su)) in
                     c2e_loader.load_c2e_file(config.c2e_hydro_files(config.C2E_FUTURE)['inflow'], ns, config.SNAPSHOT_FREQ)) \
                 if os.path.exists(config.c2e_hydro_files(config.C2E_FUTURE)['inflow']) else 0
        print(f"  inflow units mapping to a C2E country: {mapped}")
    ror = [g for g in n.generators.index
           if str(n.generators.loc[g, 'carrier']).lower() in ('ror', 'run of river')]
    print(f"  run-of-river generators (carrier 'ror'): {len(ror)}")
    hyf = config.c2e_hydro_files(config.C2E_FUTURE)
    print(f"  hydro files present: inflow={os.path.exists(hyf['inflow'])}, ror={os.path.exists(hyf['ror'])}")
    if not has_inflow and not ror:
        print("  >> network has no hydro inflow/ror to modify; DO_HYDRO will be a no-op (fine).")

    # ---------- D. CO2 constraint ----------
    hdr("D. CO2 CONSTRAINT (the one we will price)")
    print(f"  global constraints: {list(n.global_constraints.index)}")
    for name in n.global_constraints.index:
        row = n.global_constraints.loc[name]
        print(f"    {name}: sense={row.get('sense','?')} constant={row.get('constant','?')} "
              f"type={row.get('type','?')}")
    if 'co2_emissions' in n.carriers.columns:
        emitters = n.carriers.index[n.carriers['co2_emissions'] > 0].tolist()
        print(f"  carriers with co2_emissions>0: {emitters}")
    print("  NOTE: the CO2 shadow price is only readable AFTER the hard-cap solve;")
    print("        run_pipeline Step 1 does that automatically.")

    # ---------- F. v15 Gotske-exactness checks ----------
    # ---------- v16: cooling levels + turbine file checks ----------
    hdr("E2. v16 COOLING LEVELS AND TURBINE FILES")
    for p in (config.C2E_BASELINE, config.C2E_FUTURE):
        for k, f in config.c2e_supply_files(p).items():
            print(f"  {k} {p}: {'OK' if os.path.exists(f) else 'MISSING'}  {os.path.basename(f)}")
    print(f"  wind turbine selected: {config.WIND_TURBINE} "
          f"(specific-power match to PyPSA-Eur V112/NREL-5MW)")
    try:
        import c2e_loader as cl
        import mapping
        dem_b = cl.load_c2e_file(config.c2e_demand_files(config.C2E_BASELINE)['cooling'], len(n.snapshots), config.SNAPSHOT_FREQ)
        dem_f = cl.load_c2e_file(config.c2e_demand_files(config.C2E_FUTURE)['cooling'], len(n.snapshots), config.SNAPSHOT_FREQ)
        wts = n.snapshot_weightings.generators.values
        elec = n.loads.index[n.loads.carrier == 'electricity'] if 'carrier' in n.loads.columns else []
        print("  C2E cooling (GWh/h files -> x1000 MW). Sanity vs national electricity demand:")
        for c in ['Spain', 'Italy', 'France', 'Germany', 'Norway']:
            if c in dem_b:
                b_twh = float((dem_b[c].values * 1000 * wts).sum()) / 1e6
                f_twh = float((dem_f[c].values * 1000 * wts).sum()) / 1e6
                cols = [l for l in elec if mapping.country_of_bus(str(n.loads.loc[l, "bus"])) == c]
                d_twh = float((n.loads_t.p_set[cols].values * wts[:, None]).sum()) / 1e6 if cols else float('nan')
                share = 100 * b_twh / d_twh if d_twh == d_twh and d_twh > 0 else float('nan')
                print(f"    {c}: base {b_twh:6.2f} TWh ({share:5.1f}% of elec demand "
                      f"{d_twh:7.1f} TWh) -> future {f_twh:6.2f} TWh "
                      f"[{(f_twh/b_twh-1)*100 if b_twh>0 else float('nan'):+.0f}%]")
        print("  expect single-digit %% shares (south > north); ~1000x off => unit bug;")
        print("  > 50%% share would mean the embedded-cooling subtraction is unsafe.")
    except Exception as e:
        print(f"  (cooling sanity skipped: {e})")

    hdr("F. v15 GOTSKE-EXACTNESS CHECKS (freeze scope, emission loads, shedding)")
    # CO2 accounting stores: must NOT be pinned by the freeze
    co2_buses = [b for b in n.buses.index if 'co2' in str(b).lower()]
    print(f"  co2-related buses: {co2_buses}")
    for st in n.stores.index:
        if 'co2' in str(st).lower():
            row = n.stores.loc[st]
            print(f"  store '{st}': bus={row.get('bus')} capital_cost={row.get('capital_cost', 0):.3f} "
                  f"e_nom={row.get('e_nom', 0):.3e} e_nom_opt={row.get('e_nom_opt', 0):.3e} "
                  f"extendable={row.get('e_nom_extendable')} cyclic={row.get('e_cyclic')} "
                  f"e_min_pu={row.get('e_min_pu', 0)}")
    print("  -> 'co2 atmosphere' must have capital_cost=0 so the v15 freeze leaves it")
    print("     extendable (otherwise the dispatch carries a hidden CO2 cap).")
    nzc = int((n.stores.get('capital_cost', 0) > 0).sum()) if len(n.stores) else 0
    print(f"  stores with capital_cost>0 (will be pinned): {nzc} of {len(n.stores)}")
    gz = n.generators
    free_g = gz.index[(gz.get('capital_cost', 0).fillna(0) <= 0)].tolist() if len(gz) else []
    print(f"  zero-capital-cost generators (stay FREE, e.g. EU fuel supply): {free_g[:10]}"
          + (" ..." if len(free_g) > 10 else ""))
    # emission loads on the atmosphere bus (the part the old metric missed)
    atm = [b for b in n.buses.index if str(b) == 'co2 atmosphere']
    if atm:
        atm_loads = n.loads.index[n.loads.bus == atm[0]].tolist()
        print(f"  LOADS on 'co2 atmosphere' (counted in v15 emissions): {atm_loads}")
        for ld in atm_loads:
            if hasattr(n.loads_t, 'p_set') and ld in n.loads_t.p_set.columns:
                tot = -(n.loads_t.p_set[ld].values * n.snapshot_weightings.generators.values).sum() / 1e6
            else:
                ps = n.loads.loc[ld].get('p_set', 0.0)
                tot = -(float(ps) * n.snapshot_weightings.generators.sum()) / 1e6
            print(f"    {ld}: net injection {tot:+.1f} Mt/yr")
    # shedding placement preview
    nlv = len(n.buses.query('carrier == "low voltage"'))
    heatc = ['residential rural heat', 'services rural heat',
             'residential urban decentral heat', 'services urban decentral heat',
             'urban central heat']
    nheat = sum(len(n.buses.query('carrier == @c')) for c in heatc)
    print(f"  shedding will attach to {nlv} low-voltage buses (load_el) "
          f"and {nheat} heat buses (load_heat) at 1e5 EUR/MWh, extendable")
    base = getattr(config, 'CO2_1990_BASELINE_MT', None)
    print(f"  1990 reference for %% reporting: {base} Mt (Gotske data/co2_totals.csv, "
          f"all sectors except LULUCF, waste, other, indirect)")

    hdr("AUDIT COMPLETE")
    print("Read A-E above. If A shows ratio~1 (thermal) or ~2-4 (electric, documented),")
    print("B shows hydro mapped (or correctly empty), C shows most CF generators mapped,")
    print("D shows a CO2Limit, and E shows MATCH -> proceed to the validation run.")
    print("The validation run's KEY check: ORIGINAL (unmodified) wy%s load shedding"
          % config.WEATHER_YEAR)
    print("must be near zero (~Gotske 99.9%% adequacy). Only then trust the 2042 result.")


if __name__ == '__main__':
    main()
