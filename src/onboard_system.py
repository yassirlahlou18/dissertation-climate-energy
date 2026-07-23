"""Onboard a design network from any registered system BEFORE it earns a
place in the sweep. Codifies the protocol that caught every past silent bug:
inventory -> mapping dry-run -> coverage census -> (optional) one probe
dispatch. Produces a human-readable report and a machine-readable verdict.

Usage (from the code root):

    python -m src.onboard_system --system neumann2023 \
        --design "lv1.0__Co2L0-3H-T-H-B-I-A-solar+p3-linemaxext10"
    python -m src.onboard_system --system gotske --design wy2015
    # add --probe-solve to also run ONE reference dispatch (hours; watch RAM)

Nothing is modified; the network is only read (probe-solve solves a copy of
its own dispatch problem and writes nothing back to the source file).
"""
from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import os
import time
import argparse

import config
import systems
import mapping


# Gotske wy2015 dispatch is the calibration point for the memory estimate:
# ~1.1e6 primal variables solved comfortably within the 64 GB VM (~52 min).
_GOTSKE_VARS = None   # filled lazily from expectations below
_GOTSKE_REF = dict(vars_approx=1.1e6, gb_peak=28.0, minutes=52.0)


def _fmt(n):
    return f"{n:,}" if isinstance(n, int) else f"{n:,.1f}"


def inventory(n, sysname, did, lines):
    lines.append(f"# Onboarding report: system={sysname}  design={did}")
    lines.append(f"file: {systems.network_path(sysname, did)}")
    lines.append(f"pypsa import version attr: {getattr(n, '_pypsa_version', getattr(n, 'pypsa_version', 'n/a'))}")
    T = len(n.snapshots)
    w = float(n.snapshot_weightings.objective.iloc[0]) if hasattr(n.snapshot_weightings, 'objective') else float(n.snapshot_weightings.iloc[0, 0])
    lines.append(f"snapshots: {T} x {w:g}h  (index {n.snapshots[0]} .. {n.snapshots[-1]})")
    exp = systems.get(sysname).get('expected', {})
    if exp.get('snapshots') and exp['snapshots'] != T:
        lines.append(f"!! expected {exp['snapshots']} snapshots, found {T}")
    lines.append("")
    lines.append("## Component counts")
    for comp in ('buses', 'generators', 'links', 'lines', 'loads',
                 'storage_units', 'stores'):
        df = getattr(n, comp)
        lines.append(f"- {comp}: {len(df)}")
    lines.append("")
    lines.append("## Extendability (design-vs-dispatch sanity: pinning covers these)")
    for comp, col in (('generators', 'p_nom_extendable'),
                      ('links', 'p_nom_extendable'),
                      ('lines', 's_nom_extendable'),
                      ('stores', 'e_nom_extendable'),
                      ('storage_units', 'p_nom_extendable')):
        df = getattr(n, comp)
        if col in df.columns:
            lines.append(f"- {comp}: {int(df[col].sum())} extendable of {len(df)}")
    lines.append("")
    lines.append("## Global constraints (CO2 hook)")
    gc = n.global_constraints
    if len(gc):
        for i, row in gc.iterrows():
            mu = row.get('mu', float('nan'))
            lines.append(f"- {i}: sense={row.get('sense','?')} const={row.get('constant','?')} mu={mu}")
    else:
        lines.append("- NONE (co2 pricing would need a provided value)")
    try:
        import dispatch as dsp
        price = dsp.get_co2_shadow_price(n)
        lines.append(f"- get_co2_shadow_price(): {price:.3f} EUR/tCO2")
    except Exception as e:
        lines.append(f"!! get_co2_shadow_price failed: {e}")
    return T


def mapping_dry_run(n, sysname, lines):
    ok = True
    lines.append("")
    lines.append("## Bus census (load-carrying buses = shedding scope)")
    load_buses = set(n.loads.bus)
    car = n.buses.loc[sorted(load_buses & set(n.buses.index)), 'carrier'].value_counts()
    for c, k in car.items():
        lines.append(f"- {k:5d} load buses with carrier '{c}'")
    lines.append("")
    lines.append("## Country resolution")
    unresolved = sorted({b for b in n.buses.index
                         if mapping.country_of_bus(b) is None})
    lines.append(f"- unresolved buses: {len(unresolved)}"
                 + (f"  e.g. {unresolved[:5]}" if unresolved else ""))
    if unresolved:
        ok = False
    lines.append("")
    lines.append("## VRE mapping (carrier -> C2E channel -> country)")
    from mapping import SUPPLY_CARRIER_TO_C2E
    gens = n.generators
    vre = gens[gens.carrier.isin(SUPPLY_CARRIER_TO_C2E)]
    lines.append(f"- VRE generators matched by carrier map: {len(vre)} of {len(gens)}")
    for c, k in vre.carrier.value_counts().items():
        lines.append(f"    {k:5d} x {c} -> {SUPPLY_CARRIER_TO_C2E[c]}")
    other = sorted(set(gens.carrier) - set(SUPPLY_CARRIER_TO_C2E)
                   - {'ror', 'run of river'})
    lines.append(f"- non-VRE generator carriers (untouched by supply channel): {other}")
    nocountry = [g for g in vre.index
                 if mapping.country_of_bus(str(vre.at[g, 'bus'])) is None]
    if nocountry:
        ok = False
        lines.append(f"!! VRE generators without country: {len(nocountry)} e.g. {nocountry[:4]}")
    lines.append("")
    lines.append("## Hydropower")
    su = n.storage_units
    has_inflow = [s for s in su.index
                  if s in getattr(n.storage_units_t, 'inflow', {})]
    lines.append(f"- storage units: {len(su)}; with inflow series: {len(has_inflow)}")
    inflow_countries = sorted({mapping.country_of_bus(str(su.at[s, 'bus']))
                               for s in has_inflow} - {None})
    lines.append(f"- inflow countries: {inflow_countries}")
    c2e_inflow = {'Austria', 'Bulgaria', 'Switzerland', 'Spain', 'France',
                  'Italy', 'Norway', 'Portugal', 'Romania', 'Sweden'}
    lines.append(f"- of which C2E-covered: {sorted(set(inflow_countries) & c2e_inflow)}")
    ror = n.generators[n.generators.carrier.astype(str).str.lower().isin(
        ['ror', 'run of river'])]
    lines.append(f"- run-of-river generators: {len(ror)}")
    lines.append("")
    lines.append("## Heat loads (change-factor target)")
    import heat_cooling as hc
    hl = hc.find_heat_loads(n)
    lines.append(f"- heat loads found: {len(hl)}"
                 + (f"  e.g. {hl[:3]}" if hl else "  !! NONE"))
    if not hl:
        if systems.get(sysname).get('sector_coupled', True):
            ok = False
        else:
            lines.append("  (power-only system: heating/cooling channels inert by design)")
    lines.append("")
    lines.append("## C2E files for this system's channels")
    for per in (config.C2E_BASELINE, config.C2E_FUTURE, 2099):
        missing = [os.path.basename(p)
                   for p in list(config.c2e_supply_files(per).values())
                   + [config.c2e_demand_files(per)['heating']]
                   if not os.path.exists(p)]
        lines.append(f"- period {per}: " + ("all essentials present" if not missing
                                            else f"MISSING {missing}"))
    return ok


def memory_estimate(n, T, lines):
    nvars = (len(n.generators) + len(n.links) + 2 * len(n.storage_units)
             + len(n.stores)) * T
    scale = nvars / _GOTSKE_REF['vars_approx']
    lines.append("")
    lines.append("## Size and memory estimate (Gotske-calibrated, rough)")
    lines.append(f"- ~{_fmt(int(nvars))} primal variables "
                 f"(~{scale:.1f}x the gotske wy2015 dispatch)")
    lines.append(f"- ballpark peak memory: {_GOTSKE_REF['gb_peak'] * scale:,.0f} GB "
                 f"(gotske ref ~{_GOTSKE_REF['gb_peak']:.0f} GB); "
                 f"ballpark wall: {_GOTSKE_REF['minutes'] * scale:,.0f} min/solve")
    if _GOTSKE_REF['gb_peak'] * scale > 55:
        lines.append("!! likely EXCEEDS a 64 GB VM: plan n2-highmem-16/32 for "
                     "these runs, or use a lower-resolution deposit if published")
    lines.append("- this is a scaling heuristic; --probe-solve gives the truth")


def probe_solve(sysname, did, lines):
    import resource
    import pypsa
    import dispatch as dsp
    lines.append("")
    lines.append("## Probe solve (ONE reference dispatch)")
    t0 = time.time()
    n = pypsa.Network(systems.network_path(sysname, did))
    price = dsp.get_co2_shadow_price(n)
    dsp.prepare_for_dispatch(n, f"onboard_{sysname}", log=lambda *a: None,
                             co2_price=price)
    n = dsp.dispatch(n, f"onboard_{sysname}", log=print)
    mins = (time.time() - t0) / 60
    peak_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
    res = dsp.extract_results(n, mins * 60)
    lines.append(f"- status: solved | wall {mins:.1f} min | peak RSS ~{peak_gb:.1f} GB")
    lines.append(f"- reference unserved: {res.get('load_shedding_TWh', float('nan')):.3f} TWh "
                 f"(a healthy design serves its own weather: expect ~0)")
    lines.append(f"- reference net CO2: {res.get('co2_emissions_Mt', float('nan')):.2f} Mt")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--system', required=True, choices=sorted(systems.SYSTEMS))
    ap.add_argument('--design', required=True)
    ap.add_argument('--probe-solve', action='store_true')
    args = ap.parse_args(argv)

    path = systems.network_path(args.system, args.design)
    if not os.path.exists(path):
        print(f"network file not found: {path}")
        print("place the SOLVED network there (see docs/MULTISYSTEM_ONBOARDING.md)")
        return 2
    import pypsa
    n = pypsa.Network(path)
    lines = []
    T = inventory(n, args.system, args.design, lines)
    ok = mapping_dry_run(n, args.system, lines)
    memory_estimate(n, T, lines)
    if args.probe_solve:
        probe_solve(args.system, args.design, lines)
    lines.append("")
    lines.append(f"VERDICT: {'READY for a full run' if ok else 'NOT READY: fix the !! items above'}")
    out = os.path.join(config.OUTPUT_ROOT,
                       f"onboarding_{args.system}_{args.design.replace('/', '_')[:60]}.md")
    os.makedirs(config.OUTPUT_ROOT, exist_ok=True)
    with open(out, 'w') as fh:
        fh.write("\n".join(lines))
    print("\n".join(lines))
    print(f"\nreport written: {out}")
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
