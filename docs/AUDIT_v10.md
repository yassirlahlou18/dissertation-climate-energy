# First-principles audit (v10)

This records the full re-check of the pipeline against the thesis goal:
stress-test the FIXED Gotske 2015 system against C2E future weather (2042), and
measure how it performs, with three CF methods and detailed results.

## Verdict by component

1. CF methods (qdm / direct / delta) - CORRECT.
   - QDM (primary) preserves the network's 2015 weather CHRONOLOGY and shifts the
     distribution per quantile (Cannon et al. 2015, multiplicative). This is the
     right choice for ISOLATING the climate-change signal: it does not confound
     the result with one random realisation of 2042's weather sequence.
   - delta = monthly mean change factor (naive baseline). direct = raw C2E future
     CF (swaps in 2042 chronology; the transparent-but-confounded comparison).
   - Tested on synthetic data: methods give distinct, theory-consistent results.

2. Demand (heating + cooling) - CORRECT, decoupled from supply method.
   - Relative change (future/baseline ratio) applied to the network's own
     calibrated loads; unit-agnostic. Heating reduction (~ -5.5% total) is the
     expected warmer-winter effect. Cooling literature-anchored (3% baseline
     share, IEA) with C2E shape + increase. Consistent climate state per run
     (C2E + JRC PESETA practice).
   - OPEN (verify on real data with audit_real_data.py): whether C2E heating is
     THERMAL (ratio ~1 vs network heat) or ELECTRIC (ratio ~2-4, bundles COP).
     The level is fine either way (ratio only); this affects INTERPRETATION and
     whether COP is implicitly included.

3. CO2 treatment - CORRECT, matches Gotske exactly (their config:
   custom_co2_price=False, add_co2_lim=False). Remove the hard net-zero cap for
   the dispatch; apply the design-year CO2 shadow price (Lagrange multiplier) as
   a CO2 tax. Headline metrics = unserved energy + CO2 emissions.

4. Hydropower - FIXED in v10 (was the one real gap).
   - Gotske vary hydro inflow; we have hydro_inflow + hydro_ror files. Now:
     storage_units_t.inflow scaled by C2E inflow relative change; run-of-river
     generator CF modified by the chosen supply method. Tested on synthetic.

5. Heat-pump COP - kept at original values (no C2E temperature file). Conservative
   (understates winter relief -> biases toward MORE shedding, never invents
   improvement). Documented limitation. If audit shows C2E heating is electric,
   COP change is already bundled into the heating ratio.

6. Turbine choice - second-order. C2E turbines (E-126/SWT) differ from PyPSA-Eur's
   Vestas V112; but qdm/delta use the relative change, which is turbine-robust.
   Only 'direct' is sensitive. Documented as a sensitivity.

## Before trusting any result: two real-data steps

STEP 1 - audit (no solve, ~1 min):
    python -m src.audit_real_data
   Confirms heating units, hydro presence/mapping, supply mapping, CO2
   constraint, and snapshot alignment on the REAL files.

STEP 2 - validation (the control):
   Run the pipeline; the ORIGINAL (unmodified) wy2015 dispatch MUST show
   near-zero unserved energy (~Gotske's 99.9% resource adequacy). Only if the
   model reproduces Gotske on historical weather is the 2042 result a clean
   climate signal. If original shedding is large -> setup mismatch -> fix first.

## What is NOT yet proven
Everything above is verified in code and on synthetic data. It is NOT yet
confirmed on the real Gotske network + real C2E end-to-end. The audit + the
validation baseline ARE that confirmation. Treat the next run as the validation,
not as final results.

---

## v11 update: real-data audit findings (resolved)

Ran audit_real_data.py + diag_heat_co2.py on the REAL network + C2E. Two issues
found and FIXED; three confirmations.

FIXED:
1. CO2 mechanism. The Gotske/PyPSA-Eur-Sec network does NOT carry emissions on
   carrier.co2_emissions (only geothermal). Emissions flow through a 'co2
   atmosphere' bus: emitting LINKS (OCGT, CCGT, urban central gas CHP, SMR;
   process emissions) have a side output (bus2/bus3/...) with efficiency_k =
   tCO2 per MWh fuel (=0.2 for gas). The CO2 code was rewritten to:
     - price emissions: marginal_cost += co2_price * efficiency_k on each
       emitting link slot into 'co2 atmosphere' (pypsa-eur-consistent);
     - measure emissions: net injection into 'co2 atmosphere' across all link
       slots (emitters +, DAC/CC -). Falls back to carrier intensities for
       simple/synthetic networks.
   Design-year CO2 shadow price read cleanly: ~468 EUR/tCO2 (sane, citable).

2. Audit heat-ratio bug (in the audit SCRIPT, not the pipeline): it compared
   network ENERGY (TWh) to C2E MEAN POWER (~40), giving a bogus ~1600 ratio.
   Fixed to energy-vs-energy.

CONFIRMED CORRECT on real data:
A. Heating units: C2E heating is THERMAL GW (Germany peak ~181 GW, mean ~40 GW,
   annual ~351 TWh) vs network ~584 TWh -> ratio ~1.66. Thermal-to-thermal,
   relative-change application clean; COP correctly kept separate (not bundled).
   The 'fully-electrified' file is the right one (non-electrified peaks at 9 GW,
   far too small).
B. Hydro present and mapped: 22 storage units with inflow (556 TWh/yr reservoir
   inflow), 29 run-of-river generators, 11 inflow units map to C2E countries.
   The v10 hydro modification will bite.
C. Supply CF mapping: 37 onwind, 37 solar, 37 solar rooftop, 28 offwind-ac,
   23 offwind-dc all mapped; skipped are correctly non-weather (solar thermal)
   or handled by hydro (ror). Snapshot alignment MATCH (2920).

STILL THE KEY GATE: the validation run. ORIGINAL (unmodified) wy2015 dispatch
with the 468 EUR/tCO2 price must show near-zero unserved energy AND emissions
near the net-zero cap level. That confirms the CO2-bus pricing + emissions
accounting are correct on the real mechanism (only the fallback path was
exercised on synthetic). Only then trust the 2042 comparison.

---

## v12 update: faithfulness lock-in + deep-analysis outputs

Re-verified against Gotske's actual config (github.com/ebbekyhl/multi-weather-
year-assessment, config.yaml). Confirmed:
- CO2 in dispatch: custom_co2_price=False => price = Lagrange multiplier (shadow
  price) of the CO2 constraint from the capacity optimization; add_co2_lim=False
  => hard cap removed. The pipeline does exactly this (reads mu ~468 EUR/tCO2 on
  the real network, removes CO2Limit, prices the emitting links via the co2
  atmosphere bus). The CO2 work is about the DISPATCH carbon treatment only - it
  is NOT specific to heating/cooling; PyPSA-Eur-Sec uses one unified CO2
  atmosphere bus for all sectors.
- Gotske's weather variables: "hydro, solar, wind, heat" (their config comment).
  The pipeline now varies all of these (solar, onshore wind, offshore wind, hydro
  inflow, run-of-river, heating). Cooling is the single deliberate addition beyond
  Gotske (future warming raises it), anchored to IEA (3% baseline share); C2E
  supplies its shape + relative increase. COP kept at original (no C2E temperature;
  conservative, documented).

Three CF methods (the thesis's own contribution, since Gotske swap raw weather
years within one ERA5 pipeline and need no bias correction, whereas C2E is a
different climate-model pipeline):
- direct = raw C2E future swap (Gotske-style), confounds C2E-vs-ERA5 pipeline bias.
- delta  = monthly change factor (naive baseline).
- qdm    = Quantile Delta Mapping (Cannon et al. 2015), primary; preserves the
  projected change in every quantile (variability + extremes) and the network's
  2015 weather chronology, isolating the climate signal.

Deep-analysis outputs ADDED (08_reliability_deep + expanded tables), matching and
extending what Gotske report (their SI Figs 23-24): loss-of-load time series,
cumulative unserved energy, shortfall event count + duration distribution,
resource adequacy %, peak unserved MW, hours of shortfall; CO2 emissions by
technology + by month + net Mt (+ % of 1990 if a baseline is set); backup
activation profile; monthly generation + curtailment; plus all the existing
generation/curtailment/regional/sector/storage/climate-signal breakdowns. ~28
machine-readable CSVs per method run for independent analysis.

Run procedure: cloud/RUN_ON_VM.md (every step, PC vs VM marked).

---

## v13 update: self-audit catch (fixed-capacity confirmed; load-shedding sizing fixed)

Turned the scrutiny on our own code. Two findings:

1. CONFIRMED - fixed-capacity stress test is correct. prepare_for_dispatch sets
   p_nom_extendable=False (and e_nom/s_nom) on generators, links, storage_units,
   stores, lines, and copies p_nom_opt -> p_nom. The Gotske-optimised capacities
   are genuinely frozen; the dispatch re-optimises operation only. lv_limit
   (transmission expansion) removed; co2_sequestration_limit (physical) kept.

2. FIXED - load-shedding slack was capped at 10 GW/bus. Large buses (e.g. Germany
   electricity, and heat buses peaking ~180 GW) can have a shortfall above 10 GW
   under stressed 2042 weather, which would make the model INFEASIBLE on exactly
   the run we care about (while the unmodified validation run, with ~0 shedding,
   would pass and hide it). Load shedding is now sized to 1.5x each bus's peak
   demand (floor 10 GW), so any shortfall is MEASURED as unserved energy rather
   than causing infeasibility. The high VOLL marginal cost keeps it a strict last
   resort above CO2-priced backup. (Verified: synthetic peak unserved ~104 GW,
   which the old cap would have truncated/infeasibilised.)

Also ADDED (more detail for results): loss-of-load duration curve
(unserved_duration_curve_MW.csv + figure) and a top-10 worst-shortfall-events
table (worst_shortfall_events.csv: start, end, duration_h, peak_MW, energy_GWh),
both standard in resource-adequacy analysis.

---

## v14 update: solver convergence fix (barrier 1000-iteration failure)

The real run failed when Gurobi's barrier hit its DEFAULT 1000-iteration limit
(BarIterLimit=1000) without converging, with Crossover=0 (so no fallback to a
vertex solution). Two fixes:

1. STOP re-solving for the shadow price. The Gotske network .nc already stores the
   CO2Limit dual mu (= 468.3 EUR/tCO2, confirmed by the audit) from their capacity
   optimization - and that mu IS the dispatch CO2 price they use. run_pipeline now
   reads it directly from the loaded network (get_co2_shadow_price on the unsolved
   network) and only re-solves with the hard cap as a fallback if mu is absent.
   This removes the hard net-zero-capped solve, which was both the most
   numerically difficult solve AND unnecessary. More faithful, and avoids the
   failure point.

2. Robust barrier settings for the dispatch solves (config.GUROBI_OPTS):
   BarIterLimit=10000 (was default 1000), BarHomogeneous=1 (Gurobi's homogeneous
   self-dual barrier, recommended for hard/degenerate models), NumericFocus=2
   (careful numerics; the peak-load-sized load-shedding slacks span a wide
   coefficient range), BarConvTol relaxed 1e-6 -> 1e-5 (ample accuracy for these
   energy quantities, converges far more reliably). The existing tsam-segmentation
   fallback remains if a full-year solve still fails.

Net effect: Step 1 no longer solves at all on the real network (reads mu=468.3);
Steps 2-3 (original + modified dispatch) solve with the robust settings.
