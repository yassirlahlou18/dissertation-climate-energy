"""Main orchestrator that runs the whole pipeline. See the pipeline guide PDF."""

from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import os
import argparse
import datetime
import logging
import pandas as pd

import config


def _logger():
    os.makedirs(config.OUTPUT_ROOT, exist_ok=True)
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    os.makedirs(config.SESSION_DIR, exist_ok=True)
    lf = os.path.join(config.SESSION_DIR, f'pipeline_{ts}.log')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s',
                        handlers=[logging.StreamHandler(), logging.FileHandler(lf)])
    return logging.getLogger(), lf


def main(methods=None, force_rerun_original=False):
    import pypsa
    import build_modified_network as bmn
    import pandas as pd
    import dispatch as dsp
    import reporting as rep

    log, lf = _logger()
    # NOTE: do NOT default `methods` here. `methods is None` is the signal that no
    # explicit method was requested, which lets the run-spec logic below choose
    # the mixed production profile (config.USE_CHANNEL_METHODS). Defaulting to
    # config.METHODS here would force the legacy two pure runs and silently skip
    # the mixed profile.
    log.info(f"PIPELINE start | methods={methods} | log={lf}")

    # ---- dispatch ORIGINAL once, cached to disk and reused across runs ----
    import pickle
    os.makedirs(config.OUTPUT_ROOT, exist_ok=True)
    orig_cache = os.path.join(
        config.OUTPUT_ROOT,
        f"_original_{config.design_key()}_results.pkl")
    price_cache = os.path.join(
        config.OUTPUT_ROOT,
        f"_co2_price_{config.design_key()}.pkl")

    results_orig = None
    co2_price = None
    if os.path.exists(orig_cache) and os.path.exists(price_cache) and not force_rerun_original:
        try:
            with open(orig_cache, 'rb') as f:
                results_orig = pickle.load(f)
            with open(price_cache, 'rb') as f:
                co2_price = pickle.load(f)
            log.info(f"Reusing cached ORIGINAL results + CO2 price ({co2_price:,.1f} EUR/tCO2)")
            log.info("  (delete the cache files or pass force_rerun_original=True to recompute)")
        except Exception as e:
            log.info(f"  cache unreadable ({e}); recomputing original")
            results_orig = None; co2_price = None

    if results_orig is None:
        mode = getattr(config, 'CO2_DISPATCH_MODE', 'gotske_price')
        if mode == 'gotske_price':
            # STEP 1: solve original WITH the hard CO2 cap to get the design-year
            # shadow price (Gotske: CO2 price = Lagrange multiplier of the cap).
            log.info("Step 1: read design-year CO2 shadow price")
            n_cap = pypsa.Network(config.network_file())
            # The Gotske network .nc already stores the CO2Limit dual (mu) from
            # their capacity optimization - that mu IS the shadow price they use
            # as the dispatch CO2 price. Read it directly (faithful + avoids the
            # hard net-zero-capped re-solve, which is the most numerically
            # difficult solve and unnecessary here).
            co2_price = dsp.get_co2_shadow_price(n_cap)
            if co2_price is not None:
                log.info(f"  design-year CO2 shadow price (from network mu) = {co2_price:,.1f} EUR/tCO2")
            else:
                # fallback only if mu is not stored: re-solve with the hard cap
                log.info("  mu not stored in network; re-solving original with hard cap to extract it")
                _saved = config.CO2_DISPATCH_MODE; config.CO2_DISPATCH_MODE = 'hard_cap'
                n_cap = dsp.prepare_for_dispatch(n_cap, 'ORIGINAL-capsolve', log.info)
                config.CO2_DISPATCH_MODE = _saved
                n_cap_solved, dt_cap, _ = dsp.dispatch(n_cap, 'ORIGINAL-capsolve', log.info)
                if n_cap_solved is None:
                    log.error("ORIGINAL cap-solve failed - aborting"); return
                co2_price = dsp.get_co2_shadow_price(n_cap_solved)
                if co2_price is None:
                    co2_price = getattr(config, 'CO2_PRICE_FALLBACK', None)
                    log.info(f"  could not read shadow price; using fallback {co2_price}")
                else:
                    log.info(f"  design-year CO2 shadow price = {co2_price:,.1f} EUR/tCO2")
            with open(price_cache, 'wb') as f:
                pickle.dump(co2_price, f)
        else:
            co2_price = None

        # STEP 2: the reference ORIGINAL dispatch, Gotske-style (cap removed, price on)
        log.info("Step 2: ORIGINAL reference dispatch (Gotske CO2-price setup)")
        n_orig = pypsa.Network(config.network_file())
        n_orig = dsp.prepare_for_dispatch(n_orig, 'ORIGINAL', log.info, co2_price=co2_price)
        n_orig_solved, dt_o, mode_o = dsp.dispatch(n_orig, 'ORIGINAL', log.info)
        results_orig = dsp.extract_results(n_orig_solved, dt_o) if n_orig_solved else None
        if results_orig is None:
            log.error("ORIGINAL dispatch failed - aborting"); return
        results_orig['co2_price_EUR_per_t'] = co2_price
        try:
            with open(orig_cache, 'wb') as f:
                pickle.dump(results_orig, f)
            log.info(f"Cached ORIGINAL results -> {orig_cache}")
        except Exception as e:
            log.info(f"  (could not cache original: {e})")
        if n_orig_solved is not None:
            n_orig_solved.export_to_netcdf(os.path.join(
                config.OUTPUT_ROOT,
                f"_dispatched_original_{config.design_key()}.nc"))

    # Decide what to run. In production, ONE mixed-profile run whose channels
    # follow config.CHANNEL_METHODS. In legacy mode, one pure run per method in
    # `methods`. Each spec is (label, method_or_map): the label names the output
    # folder, figures, report and cache; method_or_map is passed to bmn.build.
    if methods is not None:
        # explicit override (e.g. --method direct) always means a pure run
        run_specs = [(m, m) for m in methods]
    elif getattr(config, 'USE_CHANNEL_METHODS', False):
        run_specs = [(getattr(config, 'RUN_PROFILE', 'mixed'), config.CHANNEL_METHODS)]
    else:
        run_specs = [(m, m) for m in config.METHODS]
    log.info(f"PIPELINE runs: {[s[0] for s in run_specs]}")

    figs_orig = rep.make_figures(results_orig, run_specs[0][0], f'Original wy{config.WEATHER_YEAR}', suffix='_original')

    all_results = {}
    for label, spec in run_specs:
        log.info("=" * 70); log.info(f"RUN: {label}")
        # build modified network (spec is a method string or a per-channel map)
        mod_file, cf_change_df, demand_summaries = bmn.build(spec)
        # climate-signal tables for heating and cooling
        try:
            tdir = rep._dirs(label)['tables']
            cs = (demand_summaries.get('heat') or {}).get('country_stats', {})
            if cs:
                pd.DataFrame.from_dict(cs, orient='index').rename_axis('country') \
                  .to_csv(os.path.join(tdir, 'heating_change_by_country.csv'))
            cc = (demand_summaries.get('cool') or {}).get('by_country', {})
            if cc:
                pd.DataFrame.from_dict(cc, orient='index').rename_axis('country') \
                  .to_csv(os.path.join(tdir, 'cooling_change_by_country.csv'))
            hy = (demand_summaries.get('hydro') or {})
            if hy.get('inflow_country_stats'):
                pd.DataFrame.from_dict(hy['inflow_country_stats'], orient='index') \
                  .rename_axis('country') \
                  .to_csv(os.path.join(tdir, 'hydro_change_by_country.csv'))
            if hy:
                cov = {'inflow_vol_total_TWh': hy.get('inflow_vol_total_TWh', float('nan')),
                       'inflow_vol_modified_TWh': hy.get('inflow_vol_modified_TWh', float('nan')),
                       'inflow_units_modified': hy.get('inflow_modified', 0),
                       'inflow_method': hy.get('inflow_method', ''),
                       'ror_units_modified': hy.get('ror_modified', 0),
                       'ror_method': hy.get('ror_method', ''),
                       'skipped': hy.get('skipped', 0)}
                vt = cov['inflow_vol_total_TWh']; vm = cov['inflow_vol_modified_TWh']
                cov['inflow_vol_modified_pct'] = (100.0 * vm / vt) if (vt and vt > 1e-9) else float('nan')
                pd.DataFrame([cov]).to_csv(os.path.join(tdir, 'hydro_coverage.csv'), index=False)
        except Exception as e:
            log.info(f"  (climate-signal demand tables skipped: {e})")
        # dispatch modified
        n_mod = pypsa.Network(mod_file)
        n_mod = dsp.prepare_for_dispatch(n_mod, f'MOD-{label}', log.info, co2_price=co2_price)
        n_mod_solved, dt_m, mode_m = dsp.dispatch(n_mod, f'MOD-{label}', log.info)
        if n_mod_solved is None:
            log.error(f"  {label}: modified dispatch failed"); continue
        results_mod = dsp.extract_results(n_mod_solved, dt_m)
        results_mod['co2_price_EUR_per_t'] = co2_price
        n_mod_solved.export_to_netcdf(
            os.path.join(config.run_dir(label), 'networks',
                         f"dispatched_{label}_wy{config.WEATHER_YEAR}_c2e{config.C2E_FUTURE}.nc"))
        # report
        figs_mod = rep.make_figures(results_mod, label, f'{label.upper()} (C2E {config.C2E_FUTURE})',
                                    cf_change_df=cf_change_df)
        figs_orig_m = rep.make_figures(results_orig, label, f'Original wy{config.WEATHER_YEAR}', suffix='_original')
        rpt = rep.build_method_report(results_orig, results_mod, label, figs_orig_m, figs_mod)
        log.info(f"  report: {rpt}")
        all_results[label] = {'orig': results_orig, 'mod': results_mod}
        # cache this run's results so a comparison can include runs done in
        # SEPARATE invocations
        try:
            with open(os.path.join(config.OUTPUT_ROOT,
                                   f"_method_{label}_{config.design_key()}_c2e{config.C2E_FUTURE}.pkl"), 'wb') as f:
                pickle.dump(results_mod, f)
        except Exception as e:
            log.info(f"  (could not cache {label} results: {e})")

    # build comparison from ALL run caches present (so running runs one at a
    # time still produces a complete comparison). Includes the mixed profile and
    # any pure runs (qdm/direct) that were run for the appendix.
    combined = {}
    for m in [getattr(config, 'RUN_PROFILE', 'mixed'), 'qdm', 'direct']:
        if m in combined:
            continue
        mc = os.path.join(config.OUTPUT_ROOT,
                          f"_method_{m}_{config.design_key()}_c2e{config.C2E_FUTURE}.pkl")
        if os.path.exists(mc):
            try:
                with open(mc, 'rb') as f:
                    combined[m] = {'orig': results_orig, 'mod': pickle.load(f)}
            except Exception:
                pass
    if combined:
        cmp = rep.build_comparison_report(combined)
        log.info(f"COMPARISON ({len(combined)} run(s)): {cmp}")
    log.info("PIPELINE done.")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--method', default=None, choices=['direct', 'qdm'],
                    help='run a single supply method (original is cached and reused)')
    ap.add_argument('--rerun-original', action='store_true',
                    help='force recomputation of the original dispatch (ignore cache)')
    args = ap.parse_args()
    main([args.method] if args.method else None,
         force_rerun_original=args.rerun_original)
