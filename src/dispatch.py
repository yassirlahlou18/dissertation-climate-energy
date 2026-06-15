"""Prepares and dispatches one network and extracts the results dictionary. See the pipeline guide PDF."""

from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import time
import numpy as np
import pandas as pd

import config
from mapping import region_of

RE_CARRIERS = ['solar', 'solar rooftop', 'onwind', 'offwind-ac', 'offwind-dc']


def _co2_atmosphere_bus(n):
    """Find the CO2 'atmosphere' bus used by PyPSA-Eur-Sec, if present."""
    cands = [b for b in n.buses.index if 'co2' in str(b).lower() and 'atmos' in str(b).lower()]
    if cands:
        return cands[0]
    # fallback: a bus whose carrier is 'co2'
    if 'carrier' in n.buses.columns:
        c = n.buses.index[n.buses['carrier'].astype(str).str.lower() == 'co2']
        if len(c):
            return c[0]
    return None


def _co2_link_slots(n, co2_bus):
    """Map each link touching the CO2 atmosphere bus to (slot_k, efficiency_col,
    p_col). PyPSA-Eur-Sec emits via a side output (bus2/bus3/...) with the
    matching efficiency2/efficiency3 giving tCO2 per MWh of bus0 throughput."""
    slots = {}
    for k in range(0, 6):
        buscol = 'bus0' if k == 0 else f'bus{k}'
        if buscol not in n.links.columns:
            continue
        effcol = None if k == 0 else ('efficiency' if k == 1 else f'efficiency{k}')
        pcol = f'p{k}'
        for lk in n.links.index[n.links[buscol] == co2_bus]:
            slots.setdefault(lk, []).append((k, effcol, pcol))
    return slots


def get_co2_shadow_price(n):
    """Design-year CO2 shadow price (Lagrange multiplier) from a SOLVED network.
    Returns EUR/tCO2 (positive), or None. Gotske use this as the dispatch CO2 tax."""
    name = None
    if 'CO2Limit' in n.global_constraints.index:
        name = 'CO2Limit'
    else:
        cands = [i for i in n.global_constraints.index
                 if 'co2' in i.lower() and 'sequestr' not in i.lower()]
        name = cands[0] if cands else None
    if name is None:
        return None
    mu = n.global_constraints.loc[name].get('mu', None)
    if mu is None or (isinstance(mu, float) and np.isnan(mu)):
        return None
    return abs(float(mu))


def apply_co2_price(n, co2_price, log=print):
    """Apply the CO2 price exactly as Gotske's add_co2_price() does
    (multi-weather-year-assessment/scripts/update_network.py), block by block:

      1. bus2 == 'co2 atmosphere' and efficiency2 != 0 (power/heat/fuel links,
         e.g. OCGT, CCGT, gas boilers, SMR; biogas-to-gas has efficiency2 < 0
         and is thereby CREDITED):       marginal_cost += co2_price * efficiency2
      2. bus3 == 'co2 atmosphere' (CHP): marginal_cost += co2_price * efficiency3
      3. bus1 == 'co2 atmosphere' (process emissions, with and without CC):
                                         marginal_cost += co2_price * efficiency
      4. links named 'DAC' (atmosphere -> stored):
                                         marginal_cost += -co2_price * efficiency
         i.e. direct air capture is PAID the carbon price per tonne removed.

    Falls back to carrier-intensity pricing for simple/synthetic networks that
    have no 'co2 atmosphere' bus.
    """
    co2_bus = _co2_atmosphere_bus(n)
    if 'marginal_cost' not in n.links.columns:
        n.links['marginal_cost'] = 0.0

    if co2_bus is not None:
        counts = {}
        # 1) bus2 emitters (and negative-emission links like biogas to gas)
        if 'bus2' in n.links.columns and 'efficiency2' in n.links.columns:
            b2 = n.links.query('bus2 == @co2_bus and efficiency2 != 0')
            n.links.loc[b2.index, 'marginal_cost'] = (
                b2['marginal_cost'].fillna(0) + co2_price * b2['efficiency2'])
            counts['bus2'] = len(b2)
        # 2) bus3 CHP
        if 'bus3' in n.links.columns and 'efficiency3' in n.links.columns:
            b3 = n.links.query('bus3 == @co2_bus')
            n.links.loc[b3.index, 'marginal_cost'] = (
                b3['marginal_cost'].fillna(0) + co2_price * b3['efficiency3'])
            counts['bus3_chp'] = len(b3)
        # 3) bus1 process emissions
        b1 = n.links.query('bus1 == @co2_bus')
        n.links.loc[b1.index, 'marginal_cost'] = (
            b1['marginal_cost'].fillna(0) + co2_price * n.links.loc[b1.index, 'efficiency'])
        counts['bus1_process'] = len(b1)
        # 4) DAC credit
        dac = n.links.index[n.links.index.str.contains('DAC')]
        if len(dac):
            n.links.loc[dac, 'marginal_cost'] = (
                n.links.loc[dac, 'marginal_cost'].fillna(0)
                - co2_price * n.links.loc[dac, 'efficiency'])
        counts['dac_credited'] = len(dac)
        log(f"  [CO2] Gotske pricing at {co2_price:,.1f} EUR/tCO2: {counts}")
        return sum(counts.values())

    # ---- fallback: carrier-intensity method (synthetic networks) ----
    if 'co2_emissions' not in n.carriers.columns:
        log("  [CO2] no atmosphere bus and no carrier.co2_emissions -> cannot price"); return 0
    ce = n.carriers['co2_emissions']
    npriced = 0
    g_em = n.generators['carrier'].map(ce).fillna(0.0)
    eff = n.generators.get('efficiency', pd.Series(1.0, index=n.generators.index)).replace(0, 1.0)
    add = co2_price * g_em / eff
    mask = add > 0
    n.generators.loc[mask, 'marginal_cost'] = n.generators.loc[mask, 'marginal_cost'].fillna(0) + add[mask]
    npriced += int(mask.sum())
    l_em = n.links['carrier'].map(ce).fillna(0.0) if 'carrier' in n.links.columns else pd.Series(dtype=float)
    if len(l_em):
        leff = n.links.get('efficiency', pd.Series(1.0, index=n.links.index)).replace(0, 1.0)
        ladd = co2_price * l_em / leff
        lmask = ladd > 0
        n.links.loc[lmask, 'marginal_cost'] = n.links.loc[lmask, 'marginal_cost'].fillna(0) + ladd[lmask]
        npriced += int(lmask.sum())
    log(f"  [CO2] (fallback) priced {npriced} components at {co2_price:,.1f} EUR/tCO2")
    return npriced


def get_co2_emissions_Mt(n, detail=False):
    """Net CO2 emissions (Mt) = net injection into the 'co2 atmosphere' bus,
    counting EVERYTHING that touches it, exactly as Gotske's accounting does
    (calculate_unserved_energy_and_co2_emissions.py):
      - every LINK slot on the atmosphere bus (bus0..bus4): emitters positive
        (OCGT/CCGT/CHP/boilers/SMR/process emissions), removals negative
        (DAC, biogas to gas, CC variants);
      - every LOAD on the atmosphere bus: the exogenous emission loads
        ('oil emissions' for plastics/kerosene/land transport, 'process
        emissions', 'agriculture machinery oil emissions'), which a link-only
        sum silently misses.
    Cross-check: the change in the 'co2 atmosphere' store level over the year
    equals the same net injection by construction; both are returned when
    detail=True so any mismatch flags an accounting hole.
    Falls back to carrier-intensity accounting if there is no atmosphere bus.
    """
    w = n.snapshot_weightings.generators
    co2_bus = _co2_atmosphere_bus(n)
    if co2_bus is not None:
        by_source = {}
        # ---- link slots ----
        slots = _co2_link_slots(n, co2_bus)
        for lk, slist in slots.items():
            car = str(n.links.loc[lk, 'carrier']) if lk in n.links.index else 'unknown'
            for k, effcol, pcol in slist:
                if not hasattr(n.links_t, pcol):
                    continue
                pdf = getattr(n.links_t, pcol)
                if lk not in pdf.columns:
                    continue
                inj = float((-(pdf[lk].values) * w.values).sum())  # +into atmosphere
                if inj != 0.0:
                    by_source[car] = by_source.get(car, 0.0) + inj
        # ---- loads attached to the atmosphere bus (exogenous emissions) ----
        atm_loads = n.loads.index[n.loads.bus == co2_bus]
        for ld in atm_loads:
            if hasattr(n.loads_t, 'p') and ld in n.loads_t.p.columns:
                pser = n.loads_t.p[ld].values
            elif hasattr(n.loads_t, 'p_set') and ld in n.loads_t.p_set.columns:
                pser = n.loads_t.p_set[ld].values
            else:
                pset = n.loads.loc[ld].get('p_set', 0.0)
                pser = np.full(len(w), float(pset) if pset == pset else 0.0)
            inj = float((-(pser) * w.values).sum())  # load withdraws +p; negative p_set injects
            if inj != 0.0:
                by_source[str(ld)] = by_source.get(str(ld), 0.0) + inj
        net_t = sum(by_source.values())
        if detail:
            # store-balance cross-check
            check = None
            try:
                atm_stores = n.stores.index[n.stores.bus == co2_bus]
                if len(atm_stores) and hasattr(n.stores_t, 'e') and atm_stores[0] in n.stores_t.e.columns:
                    e = n.stores_t.e[atm_stores[0]]
                    e0 = float(n.stores.loc[atm_stores[0]].get('e_initial', 0.0) or 0.0)
                    check = (float(e.iloc[-1]) - e0) / 1e6
            except Exception:
                check = None
            return net_t / 1e6, {k: v / 1e6 for k, v in by_source.items()}, check
        return net_t / 1e6
    # fallback (synthetic)
    if 'co2_emissions' not in n.carriers.columns:
        return (None, {}, None) if detail else None
    ce = n.carriers['co2_emissions']
    g_em = n.generators['carrier'].map(ce).fillna(0.0)
    geff = n.generators.get('efficiency', pd.Series(1.0, index=n.generators.index)).replace(0, 1.0)
    if not hasattr(n.generators_t, 'p'):
        return (None, {}, None) if detail else None
    per = (n.generators_t.p.multiply(w, axis=0).sum()) * g_em / geff
    e = float(per.sum())
    if detail:
        bs = (per[per != 0].groupby(n.generators.carrier).sum() / 1e6).to_dict()
        return e / 1e6, bs, None
    return e / 1e6

def prepare_for_dispatch(n, label, log=print, remove_all_gc=False, co2_price=None):
    log(f"[{label}] prepare")

    # ---- Capacity freeze, replicating Gotske's freeze_network() exactly ----
    # (multi-weather-year-assessment/scripts/update_network.py). Only components
    # with capital_cost > 0 ("real" technologies) are pinned at their optimised
    # capacity and made non-extendable. Zero-capital-cost components stay free:
    # that includes the EU fuel-supply generators (gas/oil/coal, whose p_nom must
    # not cap the fuel DRAW RATE at the design-year maximum) and the
    # 'co2 atmosphere' accounting store (pinning it would silently re-impose a
    # CO2 cap on stressed runs). Battery dischargers are pinned by name as in
    # their code (their capital cost sits on the store/charger). Lines are all
    # pinned. Storage units (hydro) are already non-extendable in the design
    # network and are left untouched, as in their code.
    def _pin(comp, mask, nom, opt):
        if not mask.any():
            return 0
        vals = comp.loc[mask, opt]
        bad = vals.isna() | (vals == 0)
        if bad.any():
            comp.loc[bad[bad].index, opt] = comp.loc[bad[bad].index, nom]
        comp.loc[mask, nom] = comp.loc[mask, opt]
        return int(mask.sum())

    g = n.generators
    if 'p_nom_extendable' in g.columns:
        mreal = (g.get('capital_cost', pd.Series(0.0, index=g.index)).fillna(0) > 0) & g['p_nom_extendable']
        npin = _pin(g, mreal, 'p_nom', 'p_nom_opt')
        g.loc[mreal, 'p_nom_extendable'] = False
        nfree = int((g['p_nom_extendable']).sum())
        log(f"  generators: pinned {npin} real (capital_cost>0); left {nfree} zero-cost free (fuel supply etc.)")

    lk = n.links
    if 'p_nom_extendable' in lk.columns:
        cc = lk.get('capital_cost', pd.Series(0.0, index=lk.index)).fillna(0)
        mreal = (cc > 0) & lk['p_nom_extendable']
        mbat = lk.index.str.contains('battery discharger') & lk['p_nom_extendable']
        mall = mreal | mbat
        npin = _pin(lk, mall, 'p_nom', 'p_nom_opt')
        lk.loc[mall, 'p_nom_extendable'] = False
        log(f"  links: pinned {npin} (capital_cost>0 + battery dischargers); left {int(lk['p_nom_extendable'].sum())} free")

    if 's_nom_extendable' in n.lines.columns and len(n.lines):
        ln = n.lines
        mask = ln['s_nom_extendable']
        if mask.any():
            _pin(ln, mask, 's_nom', 's_nom_opt')
        ln.loc[:, 's_nom_extendable'] = False
        log(f"  lines: all pinned at s_nom_opt")

    st = n.stores
    if 'e_nom_extendable' in st.columns and len(st):
        ccs = st.get('capital_cost', pd.Series(0.0, index=st.index)).fillna(0)
        mreal = (ccs > 0) & st['e_nom_extendable']
        npin = _pin(st, mreal, 'e_nom', 'e_nom_opt')
        st.loc[mreal, 'e_nom_extendable'] = False
        free = list(st.index[st['e_nom_extendable']])
        log(f"  stores: pinned {npin} real; left free (zero-cost accounting): "
            f"{free[:4]}{'...' if len(free) > 4 else ''}")
    # storage_units: untouched (hydro already non-extendable in the design network)

    # ---- Load shedding, replicating Gotske's add_load_shedding() exactly ----
    # (update_network.py): shedding generators ONLY at low-voltage electricity
    # buses (carrier 'load_el') and at the five heat bus types (carrier
    # 'load_heat'), marginal_cost = 1e5 EUR/MWh (their VOLL, citing the
    # macroeconomic/willingness-to-pay intersection, Frontiers in Energy
    # Research 2015), p_nom_extendable = True with capital_cost = 0 so shedding
    # is unbounded in size and purely priced. No shedding at fuel/industry/H2
    # buses: their unserved-energy metric covers electricity and heat.
    voll = float(getattr(config, 'LOAD_SHEDDING_COST', 1e5))
    if 'load_el' not in n.carriers.index:
        n.add("Carrier", "load_el")
    if 'load_heat' not in n.carriers.index:
        n.add("Carrier", "load_heat")

    nodes_lv = n.buses.query('carrier == "low voltage"').index
    if len(nodes_lv) == 0:
        # fallback for simple/synthetic networks without a low-voltage layer
        nodes_lv = n.buses.query('carrier == "AC"').index
    new_el = [b for b in nodes_lv if f"{b} load shedding" not in n.generators.index]
    if new_el:
        n.add("Generator", pd.Index(new_el) + " load shedding",
               bus=new_el, carrier='load_el', marginal_cost=voll,
               p_nom_extendable=True, capital_cost=0)

    heat_carriers = ['residential rural heat', 'services rural heat',
                     'residential urban decentral heat',
                     'services urban decentral heat', 'urban central heat']
    n_heat = 0
    for hc_carrier in heat_carriers:
        nodes_h = n.buses.query('carrier == @hc_carrier').index
        new_h = [b for b in nodes_h if f"{b} load shedding" not in n.generators.index]
        if new_h:
            n.add("Generator", pd.Index(new_h) + " load shedding",
                   bus=new_h, carrier='load_heat', marginal_cost=voll,
                   p_nom_extendable=True, capital_cost=0)
            n_heat += len(new_h)
    log(f"  load shedding: {len(new_el)} electricity buses (load_el) + "
        f"{n_heat} heat buses (load_heat), VOLL={voll:,.0f} EUR/MWh, extendable")

    # lv_limit is left in place as Gotske do; with all lines and links fixed it
    # is inert (no expandable transmission for it to bind on).

    # ---- CO2 treatment (Gotske et al. 2024 method) ----
    mode = getattr(config, 'CO2_DISPATCH_MODE', 'gotske_price')
    if mode == 'gotske_price':
        # remove the hard net-zero cap and apply the design-year shadow price as
        # a CO2 tax (their config: add_co2_lim=False, custom_co2_price=False).
        price = co2_price if co2_price is not None else getattr(config, 'CO2_PRICE_FALLBACK', None)
        if price is None:
            log("  [CO2] WARNING: gotske_price mode but no CO2 price supplied; "
                "keeping hard cap this run (will produce the artefact). Provide "
                "co2_price from the design-year solve.")
        else:
            if 'CO2Limit' in n.global_constraints.index:
                n.global_constraints.drop('CO2Limit', inplace=True)
                log("  removed hard CO2Limit cap (replaced by CO2 price)")
            apply_co2_price(n, price, log)
    elif mode == 'hard_cap':
        log("  [CO2] keeping hard net-zero cap (contrast mode)")

    if remove_all_gc:
        rem = list(n.global_constraints.index)
        if rem:
            n.global_constraints.drop(rem, inplace=True)
            log(f"  removed remaining GC: {rem}")

    if hasattr(n.generators_t, 'p_max_pu') and len(n.generators_t.p_max_pu) > 0:
        nn = int(n.generators_t.p_max_pu.isna().sum().sum())
        if nn:
            n.generators_t.p_max_pu.fillna(0, inplace=True)
            log(f"  filled {nn} NaN in p_max_pu")

    # ---- numerical hygiene, replicating Gotske's prepare_network() ----
    # clip_p_max_pu = 1e-2: zero out tiny availability values (their resolve
    # script does this for p_max_pu, p_min_pu, and hydro inflow).
    clip = float(getattr(config, 'CLIP_P_MAX_PU', 1e-2))
    if clip and clip > 0:
        for df in (getattr(n.generators_t, 'p_max_pu', None),
                   getattr(n.generators_t, 'p_min_pu', None),
                   getattr(n.storage_units_t, 'inflow', None)):
            if df is not None and len(df):
                df.where(df > clip, other=0., inplace=True)
        log(f"  clipped availability/inflow below {clip}")
    # noisy_costs: their small random marginal-cost perturbation (seed 174) that
    # breaks degeneracy; capital-cost noise is inert with everything fixed.
    if bool(getattr(config, 'NOISY_COSTS', True)):
        for t in n.iterate_components():
            if 'marginal_cost' in t.df:
                np.random.seed(174)
                t.df['marginal_cost'] += 1e-2 + 2e-3 * (np.random.random(len(t.df)) - 0.5)
        for t in n.iterate_components(['Line', 'Link']):
            np.random.seed(123)
            if 'length' in t.df:
                t.df['capital_cost'] += (1e-1 + 2e-2 * (np.random.random(len(t.df)) - 0.5)) * t.df['length']
        log("  applied noisy_costs (Gotske seeds 174/123)")
    return n


def dispatch(n, label, log=print):
    log(f"[{label}] full-year dispatch ({len(n.snapshots)} snapshots)")
    t0 = time.time()
    try:
        status, term = n.optimize(solver_name=config.SOLVER, **config.GUROBI_OPTS)
        dt = time.time() - t0
        log(f"  {status}/{term} in {dt/60:.1f} min")
        if status == 'ok':
            return n, dt, 'full_year'
        if 'infeasible' in str(term).lower():
            _diagnose_infeasibility(n, label, log)
    except Exception as e:
        log(f"  full-year error: {e}")

    # tsam fallback: segment count MUST divide the snapshot count evenly
    ns = len(n.snapshots)
    segs = config.NUM_SEGMENTS
    if ns % segs != 0:
        # pick the largest divisor of ns that is <= requested segs
        divisors = [d for d in range(1, ns + 1) if ns % d == 0 and d <= segs]
        segs = max(divisors) if divisors else ns // 2
        log(f"  (adjusted segments {config.NUM_SEGMENTS} -> {segs} to divide {ns} evenly)")
    log(f"[{label}] fallback: tsam segmentation ({segs})")
    t0 = time.time()
    try:
        n_seg = n.cluster.temporal.segment(segs)
        status, term = n_seg.optimize(solver_name=config.SOLVER, **config.GUROBI_OPTS)
        dt = time.time() - t0
        log(f"  segmented {status}/{term} in {dt/60:.1f} min")
        if status == 'ok':
            return n_seg, dt, 'segmented'
    except Exception as e:
        log(f"  segmentation error: {e}")
    return None, time.time() - t0, 'failed'


def _diagnose_infeasibility(n, label, log):
    """When a model is infeasible, surface the most likely cause: a bus whose
    load exceeds available supply + load shedding, or runaway demand magnitude."""
    log(f"  [{label}] INFEASIBILITY DIAGNOSTIC:")
    try:
        if hasattr(n.loads_t, 'p_set') and len(n.loads_t.p_set.columns):
            peak = n.loads_t.p_set.max()
            worst = peak.sort_values(ascending=False).head(5)
            log(f"    largest peak loads (MW): " +
                ", ".join(f"{name}={val:,.0f}" for name, val in worst.items()))
            huge = peak[peak > 5e5]
            if len(huge):
                log(f"    *** {len(huge)} loads exceed 500 GW - almost certainly a "
                    f"units/scaling bug (e.g. cooling). These make the model infeasible.")
        # buses with load but no load_shedding generator
        ls_buses = set(n.generators.bus[n.generators.carrier == 'load_shedding'])
        load_buses = set(n.loads.bus)
        uncovered = load_buses - ls_buses
        if uncovered:
            log(f"    *** {len(uncovered)} load buses have NO load-shedding backstop: "
                f"{sorted(list(uncovered))[:8]}{'...' if len(uncovered) > 8 else ''}")
            log(f"        (a load on such a bus with insufficient supply -> infeasible)")
    except Exception as e:
        log(f"    (diagnostic failed: {e})")


def _bus_carrier_of_gen(n):
    bc = n.buses['carrier'] if 'carrier' in n.buses.columns else pd.Series('AC', index=n.buses.index)
    return n.generators.bus.map(bc).fillna('unknown')


SHED_CARRIERS = ('load_el', 'load_heat', 'load_shedding')  # current + legacy


def _gotske_electricity_demand(n, w):
    """Total electricity demand the Gotske way (pypsa_metrics.calculate_
    endogenous_demand): exogenous electricity loads (carriers 'electricity' and
    'industry electricity') plus endogenous electricity consumed via links whose
    bus0 is an AC or low voltage bus, excluding DC transmission, batteries,
    LDES, and distribution links. Returns (series MW, total TWh)."""
    elec_buses = set(n.buses.query('carrier == "AC"').index) | set(
        n.buses.query('carrier == "low voltage"').index)
    # exogenous
    if 'carrier' in n.loads.columns:
        exo_idx = n.loads.index[n.loads.carrier.isin(['electricity', 'industry electricity'])]
    else:
        exo_idx = pd.Index([])
    if len(exo_idx) == 0:  # fallback: loads attached to electricity buses
        exo_idx = n.loads.index[n.loads.bus.isin(elec_buses)]
    exo = (n.loads_t.p_set[exo_idx.intersection(n.loads_t.p_set.columns)].sum(axis=1)
           if hasattr(n.loads_t, 'p_set') and len(n.loads_t.p_set.columns) else
           pd.Series(0.0, index=n.snapshots))
    # endogenous via links
    lk = n.links
    cand = lk.index[lk.bus0.isin(elec_buses)]
    if 'carrier' in lk.columns:
        cand = cand[~lk.loc[cand, 'carrier'].isin(['DC'])]
    for word in ('battery', 'LDES', 'distribution', 'load shedding'):
        cand = cand[~cand.str.contains(word)]
    endo = (n.links_t.p0[cand.intersection(n.links_t.p0.columns)].clip(lower=0).sum(axis=1)
            if hasattr(n.links_t, 'p0') and len(n.links_t.p0.columns) else
            pd.Series(0.0, index=n.snapshots))
    tot = exo + endo
    return tot, float((tot.values * w.values).sum()) / 1e6


def extract_results(n, elapsed):
    w = n.snapshot_weightings.generators
    r = {'elapsed_seconds': elapsed, 'objective': float(n.objective)}

    gen_p = n.generators_t.p
    carriers = n.generators.carrier
    regions = n.generators.bus.map(region_of)
    bus_carrier = _bus_carrier_of_gen(n)
    bc_all = n.buses['carrier'] if 'carrier' in n.buses.columns else pd.Series('AC', index=n.buses.index)

    # generation by carrier
    gbc = gen_p.T.groupby(carriers).sum().T
    gen_energy = gbc.multiply(w, axis=0).sum() / 1e6
    r['gen_by_carrier_TWh'] = gen_energy.to_dict()

    # generation by region (electricity buses only)
    elec_mask = bus_carrier.isin(['AC', 'low voltage']).values
    if elec_mask.any():
        elec_gens = gen_p.loc[:, elec_mask]
        gbr = elec_gens.T.groupby(regions[elec_mask]).sum().T
    else:
        gbr = gen_p.T.groupby(regions).sum().T
    r['gen_by_region_TWh'] = (gbr.multiply(w, axis=0).sum() / 1e6).to_dict()

    # generation by (region, carrier)
    gen_rc = gen_p.copy()
    gen_rc.columns = pd.MultiIndex.from_arrays([regions.values, carriers.values])
    rc_energy = (gen_rc.multiply(w, axis=0).sum() / 1e6).groupby(level=[0, 1]).sum()
    r['gen_by_region_carrier_TWh'] = {f"{reg}|{car}": float(v)
                                      for (reg, car), v in rc_energy.items() if abs(v) > 1e-4}

    # generation by bus carrier (sector)
    gen_by_bc = gen_p.T.groupby(bus_carrier).sum().T
    r['gen_by_buscarrier_TWh'] = (gen_by_bc.multiply(w, axis=0).sum() / 1e6).to_dict()

    # load shedding
    r['load_shedding_TWh'] = float(sum(gen_energy.get(c, 0.0) for c in SHED_CARRIERS))
    r['unserved_elec_TWh'] = float(gen_energy.get('load_el', gen_energy.get('load_shedding', 0.0)))
    r['unserved_heat_TWh'] = float(gen_energy.get('load_heat', 0.0))
    ls_gens = n.generators.index[carriers.isin(SHED_CARRIERS)]
    ls_bus = gen_p[ls_gens].multiply(w, axis=0).sum() / 1e3
    ls_bus.index = [i.replace(' load shedding', '').replace(' load_shedding', '') for i in ls_bus.index]
    r['load_shedding_by_bus_GWh'] = ls_bus[ls_bus > 1e-3].sort_values(ascending=False).to_dict()
    ls_region = pd.Series(ls_bus.values, index=[region_of(b) for b in ls_bus.index])
    r['load_shedding_by_region_GWh'] = ls_region.groupby(level=0).sum().loc[
        lambda s: s > 1e-3].sort_values(ascending=False).to_dict()
    ls_bc = pd.Series(ls_bus.values, index=[bc_all.get(b, 'unknown') for b in ls_bus.index])
    r['load_shedding_by_sector_GWh'] = ls_bc.groupby(level=0).sum().loc[
        lambda s: s > 1e-3].sort_values(ascending=False).to_dict()

    # curtailment: RE ONLY
    re_gens = n.generators.index[carriers.isin(RE_CARRIERS)]
    if len(re_gens):
        potential_re = n.generators_t.p_max_pu[re_gens] * n.generators.loc[re_gens, 'p_nom']
        curt_re = (potential_re - gen_p[re_gens]).clip(lower=0)
        curt_bc = curt_re.T.groupby(carriers[re_gens]).sum().T
        curt_e = curt_bc.multiply(w, axis=0).sum() / 1e6
        r['curt_by_carrier_TWh'] = curt_e[curt_e > 0.01].to_dict()
        r['curtailment_TWh'] = float(curt_e.sum())
        curt_reg = curt_re.T.groupby(regions[re_gens]).sum().T
        cre = curt_reg.multiply(w, axis=0).sum() / 1e6
        r['curt_by_region_TWh'] = cre[cre > 0.01].sort_values(ascending=False).to_dict()
        pot_bc = potential_re.T.groupby(carriers[re_gens]).sum().T.multiply(w, axis=0).sum() / 1e6
        r['curtailment_rate_by_carrier'] = {c: float(curt_e.get(c, 0) / pot_bc[c])
                                            for c in pot_bc.index if pot_bc[c] > 0.01}
    else:
        curt_bc = pd.DataFrame(index=n.snapshots)
        r['curt_by_carrier_TWh'] = {}; r['curtailment_TWh'] = 0.0
        r['curt_by_region_TWh'] = {}; r['curtailment_rate_by_carrier'] = {}

    # demand served, split by sector and region
    if hasattr(n.loads_t, 'p') and len(n.loads_t.p):
        load_p = n.loads_t.p
    elif hasattr(n.loads_t, 'p_set'):
        load_p = n.loads_t.p_set
    else:
        load_p = pd.DataFrame(index=n.snapshots)
    if len(load_p.columns):
        load_bus = n.loads.bus.reindex(load_p.columns)
        load_bc = load_bus.map(bc_all).fillna('unknown')
        dem_e = load_p.multiply(w, axis=0).sum() / 1e6
        r['total_demand_TWh'] = float(dem_e.sum())
        r['demand_by_sector_TWh'] = dem_e.groupby(load_bc.values).sum().to_dict()
        dem_reg = pd.Series(dem_e.values, index=[region_of(b) for b in load_bus])
        r['demand_by_region_TWh'] = dem_reg.groupby(level=0).sum().to_dict()
    else:
        r['total_demand_TWh'] = 0.0

    # RE share
    total_gen = float(gen_energy.drop(list(SHED_CARRIERS), errors='ignore').sum())
    re_gen = float(sum(gen_energy.get(c, 0) for c in RE_CARRIERS))
    r['total_generation_TWh'] = total_gen
    r['re_share_pct'] = re_gen / total_gen * 100 if total_gen else 0

    # ---- CO2 emissions (a headline metric in the Gotske framing) ----
    # Uses the network's actual emissions mechanism (co2 atmosphere bus via
    # emitting-link side outputs), falling back to carrier intensities.
    try:
        em = get_co2_emissions_Mt(n)
        if em is not None:
            r['co2_emissions_Mt'] = em
    except Exception as _e:
        r['co2_emissions_Mt'] = None

    # link energy
    if hasattr(n.links_t, 'p0') and len(n.links_t.p0):
        lc = n.links.carrier.fillna('unknown')
        le = n.links_t.p0.abs().T.groupby(lc).sum().T.multiply(w, axis=0).sum() / 1e6
        r['link_energy_TWh'] = le[le > 0.1].sort_values(ascending=False).to_dict()

    # line flows
    if hasattr(n.lines_t, 'p0') and len(n.lines_t.p0):
        lf = n.lines_t.p0.abs().multiply(w, axis=0).sum() / 1e6
        lf.index = n.lines.bus0.map(region_of) + ' - ' + n.lines.bus1.map(region_of)
        r['line_flows_TWh'] = lf.groupby(level=0).sum().sort_values(ascending=False).head(30).to_dict()

    # storage
    if hasattr(n.stores_t, 'e') and len(n.stores_t.e):
        store_e = n.stores_t.e.T.groupby(n.stores.carrier).sum().T
        r['store_max_TWh'] = (store_e.max() / 1e6).to_dict()
    if hasattr(n.storage_units_t, 'p') and len(n.storage_units_t.p):
        su = n.storage_units_t.p
        su_dis = su.clip(lower=0).T.groupby(n.storage_units.carrier).sum().T
        r['storage_discharge_TWh'] = (su_dis.multiply(w, axis=0).sum() / 1e6).to_dict()

    # RE utilisation
    if len(re_gens):
        re_actual = gen_p[re_gens].multiply(w, axis=0).sum()
        re_pot = potential_re.multiply(w, axis=0).sum()
        util = (re_actual / re_pot.replace(0, np.nan)).dropna()
        r['re_utilisation_by_carrier'] = util.groupby(carriers[re_gens]).mean().to_dict()

    # time series
    ls_sector_labels = [bc_all.get(b.replace(' load shedding', '').replace(' load_shedding', ''), 'unknown') for b in ls_gens]
    ts = {'gen_by_carrier': gbc, 'gen_by_region': gbr,
          'total_gen': gen_p.drop(columns=list(ls_gens), errors='ignore').sum(axis=1),
          'curt_by_carrier': curt_bc,
          'load_shedding': gen_p[ls_gens].sum(axis=1) if len(ls_gens) else pd.Series(0, index=n.snapshots),
          'weights': w}
    if len(ls_gens):
        ts['load_shedding_by_sector'] = gen_p[ls_gens].T.groupby(ls_sector_labels).sum().T
    ts['total_demand'] = load_p.sum(axis=1) if len(load_p.columns) else pd.Series(0, index=n.snapshots)
    if hasattr(n.stores_t, 'e') and len(n.stores_t.e):
        ts['store_e'] = n.stores_t.e.T.groupby(n.stores.carrier).sum().T / 1e6
    if hasattr(n.storage_units_t, 'p') and len(n.storage_units_t.p):
        ts['storage_p'] = n.storage_units_t.p.T.groupby(n.storage_units.carrier).sum().T
    r['pipeline_version'] = getattr(config, 'PIPELINE_VERSION', 'v15')
    r['_timeseries'] = ts

    # ========================================================================
    # DEEP-ANALYSIS METRICS (Gotske-style reliability + emissions detail)
    # ========================================================================
    wv = w.values
    snaps = n.snapshots
    months = np.asarray(snaps.month)

    # ---- reliability: loss-of-load events, duration, adequacy ----
    ls_ts = ts['load_shedding'].values  # MW unserved per snapshot (load_shedding gen output)
    dem_ts = ts['total_demand'].values
    total_dem_E = float((dem_ts * wv).sum())
    total_uns_E = float((ls_ts * wv).sum())
    r['resource_adequacy_pct'] = (1 - total_uns_E / total_dem_E) * 100 if total_dem_E > 0 else float('nan')
    r['unserved_energy_pct'] = (total_uns_E / total_dem_E) * 100 if total_dem_E > 0 else 0.0
    r['peak_unserved_MW'] = float(ls_ts.max()) if ls_ts.size else 0.0
    shortfall = ls_ts > 1.0  # snapshots with > 1 MW unserved
    r['n_shortfall_snapshots'] = int(shortfall.sum())
    r['hours_with_shortfall'] = float((wv[shortfall]).sum()) if shortfall.any() else 0.0
    # event runs (maximal consecutive shortfall blocks)
    events = []
    if shortfall.any():
        idx = np.where(shortfall)[0]
        start = idx[0]; prev = idx[0]
        for i in idx[1:]:
            if i == prev + 1:
                prev = i
            else:
                events.append((start, prev)); start = i; prev = i
        events.append((start, prev))
    ev_hours = [float(wv[s:e+1].sum()) for s, e in events]
    r['n_shortfall_events'] = len(events)
    r['max_event_duration_h'] = max(ev_hours) if ev_hours else 0.0
    r['mean_event_duration_h'] = float(np.mean(ev_hours)) if ev_hours else 0.0
    # cumulative + monthly unserved (for the Gotske figures + tables)
    r['_unserved_ts'] = pd.Series(ls_ts, index=snaps)
    r['_unserved_cumulative_TWh'] = pd.Series(np.cumsum(ls_ts * wv) / 1e6, index=snaps)
    r['unserved_by_month_GWh'] = {int(m): float((ls_ts[months == m] * wv[months == m]).sum() / 1e3)
                                  for m in range(1, 13)}

    # ---- Gotske headline adequacy: ELECTRICITY unserved energy vs total
    # electricity demand (exogenous + endogenous), per their
    # calculate_unserved_energy(); heat shedding reported separately. ----
    try:
        el_gens = n.generators.index[n.generators.carrier == 'load_el']
        if len(el_gens) == 0:
            el_gens = n.generators.index[n.generators.carrier == 'load_shedding']
        el_ts = (gen_p[el_gens.intersection(gen_p.columns)].sum(axis=1).values
                 if len(el_gens) else np.zeros(len(snaps)))
        elec_dem_ts, elec_dem_TWh = _gotske_electricity_demand(n, w)
        unserved_el_TWh = float((el_ts * wv).sum()) / 1e6
        r['elec_demand_TWh'] = elec_dem_TWh
        r['gotske_unserved_elec_TWh'] = unserved_el_TWh
        r['gotske_resource_adequacy_pct'] = (
            (1 - unserved_el_TWh / elec_dem_TWh) * 100 if elec_dem_TWh > 0 else float('nan'))
        # their LOLE focus: events with >1 MW shedding lasting longer than 1 day
        bin_el = el_ts > 1.0
        ev24 = 0
        if bin_el.any():
            idx = np.where(bin_el)[0]
            runs = np.split(idx, np.where(np.diff(idx) != 1)[0] + 1)
            for rn in runs:
                if float(wv[rn].sum()) > 24.0:
                    ev24 += 1
        r['n_elec_events_over_24h'] = ev24
        heat_gens = n.generators.index[n.generators.carrier == 'load_heat']
        ht_ts = (gen_p[heat_gens.intersection(gen_p.columns)].sum(axis=1).values
                 if len(heat_gens) else np.zeros(len(snaps)))
        r['_unserved_elec_ts'] = pd.Series(el_ts, index=snaps)
        r['_unserved_heat_ts'] = pd.Series(ht_ts, index=snaps)
    except Exception:
        pass
    # loss-of-load duration curve (unserved MW sorted descending) - classic
    # reliability diagnostic: how often is unserved power above a given level.
    r['_unserved_duration_curve'] = pd.Series(np.sort(ls_ts)[::-1])
    # top-10 worst shortfall events (timing, duration, peak, energy) for the
    # event-level discussion in the results chapter.
    ev_rows = []
    for s, e in events:
        dur_h = float(wv[s:e+1].sum())
        peak = float(ls_ts[s:e+1].max())
        energy_gwh = float((ls_ts[s:e+1] * wv[s:e+1]).sum() / 1e3)
        ev_rows.append({'start': str(snaps[s]), 'end': str(snaps[e]),
                        'duration_h': dur_h, 'peak_MW': peak, 'energy_GWh': energy_gwh})
    ev_rows = sorted(ev_rows, key=lambda x: -x['energy_GWh'])[:10]
    r['worst_events'] = ev_rows

    # ---- emissions by technology + monthly + vs 1990 ----
    try:
        net_mt, by_source, store_check = get_co2_emissions_Mt(n, detail=True)
        if net_mt is not None:
            r['co2_emissions_Mt'] = net_mt
            r['co2_emissions_storecheck_Mt'] = store_check
            r['co2_emissions_by_tech_Mt'] = {k: v for k, v in
                                             sorted(by_source.items(), key=lambda x: -abs(x[1]))
                                             if abs(v) > 1e-3}
        # emissions time series: link slots plus atmosphere loads
        co2_bus = _co2_atmosphere_bus(n)
        if co2_bus is not None:
            emit_ts = np.zeros(len(snaps))
            for lk, slist in _co2_link_slots(n, co2_bus).items():
                for k, effcol, pcol in slist:
                    if hasattr(n.links_t, pcol):
                        pdf = getattr(n.links_t, pcol)
                        if lk in pdf.columns:
                            emit_ts += -(pdf[lk].values)
            for ld in n.loads.index[n.loads.bus == co2_bus]:
                if hasattr(n.loads_t, 'p') and ld in n.loads_t.p.columns:
                    emit_ts += -(n.loads_t.p[ld].values)
                elif hasattr(n.loads_t, 'p_set') and ld in n.loads_t.p_set.columns:
                    emit_ts += -(n.loads_t.p_set[ld].values)
            r['co2_emissions_by_month_Mt'] = {int(m): float((emit_ts[months == m] * wv[months == m]).sum() / 1e6)
                                              for m in range(1, 13)}
            r['_emissions_ts'] = pd.Series(emit_ts, index=snaps)  # tCO2/h
    except Exception:
        pass
    base_1990 = getattr(config, 'CO2_1990_BASELINE_MT', None)
    if base_1990 and r.get('co2_emissions_Mt') is not None:
        r['co2_emissions_pct_of_1990'] = r['co2_emissions_Mt'] / base_1990 * 100

    # ---- backup (emitting) generation activation profile ----
    BACKUP = ['OCGT', 'CCGT', 'urban central gas CHP', 'gas CHP', 'SMR', 'oil', 'coal', 'lignite', 'gas']
    if hasattr(n.links_t, 'p0') and len(n.links_t.p0):
        lc = n.links.carrier.fillna('')
        backup_links = n.links.index[lc.isin(BACKUP)]
        if len(backup_links):
            bts = n.links_t.p0[backup_links.intersection(n.links_t.p0.columns)].clip(lower=0).sum(axis=1)
            r['backup_energy_TWh'] = float((bts.values * wv).sum() / 1e6)
            r['peak_backup_MW'] = float(bts.values.max())
            r['_backup_ts'] = bts

    # ---- monthly generation + curtailment (seasonal analysis) ----
    gen_tot_ts = ts['total_gen'].values
    curt_tot_ts = ts['curt_by_carrier'].sum(axis=1).values if len(ts['curt_by_carrier'].columns) else np.zeros(len(snaps))
    r['gen_by_month_TWh'] = {int(m): float((gen_tot_ts[months == m] * wv[months == m]).sum() / 1e6) for m in range(1, 13)}
    r['curt_by_month_TWh'] = {int(m): float((curt_tot_ts[months == m] * wv[months == m]).sum() / 1e6) for m in range(1, 13)}

    return r
