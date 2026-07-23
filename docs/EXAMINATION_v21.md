# EXAMINATION v15: line-by-line verification against Gøtske's actual code

Date: June 2026. Scope: full re-examination of the Stage 1 pipeline against the
published implementation of Gøtske et al. (2024), plus a results and reporting
upgrade. This document records what was checked, what was confirmed correct,
what was wrong and is now fixed, and what we deviate from deliberately. It also
serves as the implementation reference for the pipeline.

Method of examination: instead of relying on their paper text or config
comments, the complete repository github.com/ebbekyhl/multi-weather-year-assessment
was downloaded and the following files were read in full and compared function
by function against our code:

- scripts/update_network.py (their network preparation: weather swap, capacity
  freeze, load shedding, CO2 price)
- scripts/resolve_network.py (their dispatch solve: solver options, numerical
  hygiene, hydro constraint)
- scripts/calculate_unserved_energy_and_co2_emissions.py and
  scripts/pypsa_metrics.py (their exact metric definitions)
- config.yaml (their run settings) and data/co2_totals.csv (their 1990 basis)

Their Zenodo record (10.5281/zenodo.13379283) confirms what "stress test"
means in their work: "a dispatch optimization of the capacity layouts using
weather years different from the design year". So the stress test is BOTH the
.nc network file AND their dispatch method. Loading the file is half of it;
dispatching it their way is the other half. v15 now does both.

---

## 1. Confirmed correct (no change needed)

These parts of our pipeline were verified to match their code exactly:

- CO2 price source. Their add_co2_price() reads the price from
  n_design.global_constraints.loc['CO2Limit']['mu'], the shadow price stored in
  the design network file. Our v14 change to read the stored mu (468.3 EUR/t)
  rather than re-deriving it from a dispatch solve (664) is exactly their
  method. Their config: custom_co2_price False, add_co2_lim False.
- Hard cap removal. They call n.remove('GlobalConstraint', 'CO2Limit') when the
  price replaces the cap. We do the same.
- Variable set. Their opts string is hydro-solar-wind-heat, confirming the four
  weather-dependent variables we modify: wind and solar capacity factors,
  hydro (reservoir inflow plus run of river), and heating demand. Their
  update_renewables() swaps solar, solar rooftop, onwind, offwind-ac,
  offwind-dc and ror profiles, and storage unit inflow; update_heat() swaps the
  heat loads. Same variables as ours.
- Solver setup. Gurobi barrier (method 2), crossover 0, Seed 123, AggFill 0,
  PreDual 0, BARDENSETHRESH 200, skip_iterations True. We run the same set
  (with two added robustness options, see deviations).
- Store cyclicity. Their dispatch never touches e_cyclic or
  cyclic_state_of_charge; the design network settings carry through. Our code
  also never touches them. Confirmed clean on both sides.
- Split horizon. Their config has split_horizon False, so their operational
  year is January to December like ours.
- QDM mathematics. Re-derived and re-checked: the jitter guard handles the
  zero quantiles (solar nights), the interpolation over unique quantile values
  is stable, and the method preserves the network's own 2015 chronology while
  reshaping the distribution. This is our addition on top of their direct swap
  (see deviations), implemented per Cannon et al. (2015).

## 2. Real issues found and fixed in v15

### 2.1 The freeze pinned everything, creating a hidden CO2 cap and a fuel cap

Their freeze_network() pins ONLY components with capital_cost > 0 ("real"
technologies), plus battery dischargers by name, plus all lines. Components
with zero capital cost stay extendable on purpose. Two of those matter a lot:

- The 'co2 atmosphere' store (capital_cost 0). It is the accounting device the
  emissions flow into. Our old freeze pinned it at its design e_nom, which
  silently re-imposes a CO2 cap: a stressed dispatch cannot emit net positive
  beyond the design store size. The v13 run did not hit this only because its
  net emissions came out negative. A 2099 or direct-method run could have hit
  it silently.
- The EU fuel supply generators (gas, oil, coal at the EU bus, capital_cost 0).
  Pinning them caps the fuel DRAW RATE at the design year maximum, an
  artificial scarcity in exactly the stressed hours we care about.

Fix: prepare_for_dispatch() now replicates their rule exactly. Pinned:
generators, links and stores with capital_cost > 0, battery dischargers by
name, all lines. Left free: zero-capital-cost components. Storage units are
left untouched as in their code (hydro is already non-extendable in the design
network). Verified by unit test (section 6).

### 2.2 Load shedding: wrong cost, wrong scope, wrong sizing

Their add_load_shedding(): generators at the low voltage electricity buses
(technology 'load_el') and at the five heat bus types (technology
'load_heat'), marginal_cost 1e5 EUR/MWh (the willingness-to-pay value they
cite from Frontiers in Energy Research 2015), p_nom_extendable True with
capital_cost 0, so shedding is unbounded in size and purely priced. No
shedding at hydrogen, fuel or industry buses.

Ours was: every bus with a load, fixed p_nom at 1.5 times bus peak, cost 1e4.
Three deviations at once. The cost matters (1e4 can compete with extreme
backup chains; 1e5 is strictly last resort), the scope matters (their
unserved energy metric is electricity plus heat, not industry feedstocks),
and extendable sizing is cleaner than any fixed cap.

Fix: replicated theirs exactly, with one guarded fallback: networks without a
low voltage layer (our synthetic test system) attach load_el at AC buses.

### 2.3 DAC was not credited with the carbon price

Their add_co2_price() has four blocks: bus2 emitters (efficiency2 not 0,
negatives like biogas to gas thereby credited), bus3 CHP, bus1 process
emissions, and then DAC by name with marginal_cost MINUS price times
efficiency. Direct air capture is PAID the carbon price per tonne removed.
Our old code priced the first three patterns but skipped links whose bus0 is
the atmosphere, so DAC ran under-incentivised. With the cap removed, the
credit is what makes the price mechanism economically equivalent to the
relaxed cap, so this was a real distortion of the emissions side.

Fix: apply_co2_price() now mirrors their four blocks verbatim, including the
DAC credit. Verified by unit test: emitter marginal cost rises by price times
efficiency2, DAC marginal cost falls by price times efficiency.

### 2.4 The emissions metric missed the exogenous emission loads

Their accounting counts, besides every link slot touching the atmosphere bus,
the LOADS attached to it: 'oil emissions' (plastics decay and kerosene
combustion plus land transport oil), 'process emissions', and 'agriculture
machinery oil emissions'. These are fixed exogenous emission flows with
negative p_set, injecting CO2 into the atmosphere bus every hour. Our old
metric summed link slots only.

This is the main explanation of the strange v13 number. The 'co2' row of our
own demand table shows those loads at about -345.6 (injection of about
+345.6 Mt per year). v13 reported net emissions of -338.5 Mt; adding the
missed +345.6 gives roughly +7 Mt, in other words near zero, which is the
sensible answer for a net-zero design re-dispatched on its own weather. So
the -338 was mostly a metric hole, on top of the price being wrong.

Fix: get_co2_emissions_Mt() now counts link slots plus atmosphere-bus loads,
and returns a cross-check: the change in the atmosphere store level over the
year, which must equal the same net injection by construction. The deep
metrics table now prints both numbers side by side; any gap flags an
accounting hole. Verified by unit test: net injection equals the store delta
to the tonne, and the oil emissions load appears in the by-technology split.

### 2.5 Reporting bug: original run overwrote the modified tables

run_pipeline called make_figures for the modified run and then immediately for
the original reference into the SAME folder, so summary.csv,
gen_by_carrier.csv, curtailment_by_carrier.csv and friends ended up showing
the ORIGINAL run. This is why the uploaded v13 per-technology tables matched
the Original column of the headline comparison.

Fix: every original-reference output now carries an _original suffix, so each
table exists twice (modified under the plain name, original alongside), which
is strictly more useful. Verified in the synthetic end-to-end run: the pairs
differ.

### 2.6 Smaller alignment fixes

- clip_p_max_pu 1e-2: their prepare_network() zeroes availability and inflow
  values below 0.01 before solving. Added (config CLIP_P_MAX_PU).
- noisy_costs: their small random marginal cost perturbation (seed 174) that
  breaks degeneracy, plus the line and link capital cost noise (seed 123,
  inert with everything fixed). Added behind config NOISY_COSTS, on by
  default to match them.
- lv_limit: they leave the transmission volume constraint in place; with all
  lines and links fixed it is inert. We previously removed it; we now leave
  it too.
- pypsa 1.x: their madd calls are written for old pypsa; our add-with-list
  equivalents do the same thing on pypsa 1.2.

## 3. Metrics aligned to their definitions

Their headline unserved energy is ELECTRICITY ONLY: the 'load_el' shedders,
expressed against total electricity demand, where demand is the exogenous
electricity loads (technologies 'electricity' and 'industry electricity')
PLUS the endogenous electricity drawn through links from AC and low voltage
buses (heat pumps, electrolysis and so on), excluding DC transmission,
batteries, LDES and distribution links (their calculate_endogenous_demand()).
Their event statistic counts shedding episodes above 1 MW lasting longer than
one day.

v15 now computes, alongside our existing system-wide metrics:
- gotske_unserved_elec_TWh and gotske_resource_adequacy_pct on exactly their
  basis (new table adequacy_gotske.csv), heat shedding reported separately,
- n_elec_events_over_24h (their loss-of-load event focus),
- net emissions as percent of 1990 using their own data/co2_totals.csv
  (bundled in data/): 4614.1 Mt, the sum over all countries of all sectors
  except LULUCF, waste management, other and indirect. Their dispatch paper
  result for context: about -0.5 percent of 1990.

## 4. Deliberate, documented deviations

- Hydro reservoir constraint. Their headline config has hydroconstrained True:
  a weekly lower bound on reservoir state of charge derived from historical
  ENTSO-E filling levels (add_hydropower_constraint_soc in
  resolve_network.py). The data file lives outside their repo and we do not
  have it. Without it, reservoirs have full annual foresight, which is mildly
  optimistic for adequacy. Hypothesis, not verified: the effect on our
  comparison is small because it applies identically to the original and the
  climate runs, so the DIFFERENCE between them stays internally consistent.
  Config placeholder HYDRO_SOC_CONSTRAINT False records this.
- Solver robustness. We keep their Gurobi options and add BarConvTol 1e-5
  (theirs 1e-6) plus iteration and numeric focus safeguards that proved
  necessary on the VM. Effect on a converged LP optimum: negligible at the
  reported precision.
- The delta and QDM methods themselves. Their swap is what our 'direct' method
  is, valid for them because both their design and operational years come from
  the same ERA5 pipeline. Our future weather comes from a different model
  chain (C2E, CORDEX based), so a raw swap mixes the climate signal with the
  pipeline difference (about 20 to 30 percent spurious offsets measured in our
  audits). Delta and QDM exist to bridge that, QDM primary. Direct is retained
  as a sensitivity.
- Cooling demand is our thesis addition (anchored to the IEA 3 percent share,
  shape and growth from C2E). Gøtske have no cooling. Heat pump COP held at
  design values (no temperature series in our C2E set), slightly conservative.

## 5. What to expect when v15 is rerun (and why numbers will move)

- The ORIGINAL reference itself changes: fuel generators are no longer
  rate-capped, the atmosphere store is free, shedding costs 1e5 at the proper
  buses, DAC is credited, noisy costs are on. The original run is the first
  fully faithful reproduction of their dispatch on wy2015, so treat v15
  originals as the new baseline and discard v13 originals.
- Emissions become meaningful for the first time: the metric now closes
  against the store balance, the price is the stored 468.3, and the result
  should land near zero for the original (their paper: about -0.5 percent of
  1990). Expect single-digit to low-tens of Mt magnitudes, not hundreds.
- Reliability headline (zero shedding at 100 percent adequacy under QDM 2042)
  is expected to persist: it was price-independent and the freeze fixes relax
  constraints rather than tighten them. Hypothesis until the rerun confirms.
- Curtailment and cost will shift somewhat with the corrected price and DAC
  credit.
- IMPORTANT: the cached originals on the VM are stale. Delete
  output/_original_wy2015_results.pkl, output/_co2_price_wy2015.pkl,
  output/_dispatched_original_wy2015.nc and any output/_method_*.pkl before
  running, or the comparison will mix code versions. master_metrics.csv now
  carries pipeline_version and a stale_vs_current_code flag to catch exactly
  this.

## 6. Tests run before delivery

- test_co2_mechanism.py (new, in src/): a 48 hour toy system with a multi-bus
  OCGT emitter, a DAC link, an exogenous oil emissions load, a battery pair
  and an EU style fuel generator. Asserts the freeze scope (capital cost rule,
  battery discharger special case, atmosphere store left free), the shedding
  setup (load_el, 1e5, extendable), the pricing signs (emitter pays, DAC
  credited, cap removed), and that net emissions equal the store delta with
  the oil load counted. All pass.
- Full synthetic end-to-end through run_pipeline (qdm): completes, produces
  all new tables (adequacy_gotske, cost_decomposition, seasonal_summary,
  stress_indicators, residual load duration curve), original companions differ
  from modified (overwrite bug gone), comparison carries the new columns and
  version stamps, report opens with the auto-generated headline findings.

## 7. Implementation reference (what each piece does now)

- config.py: paths, scenario constants, LOAD_SHEDDING_COST 1e5,
  CLIP_P_MAX_PU 1e-2, NOISY_COSTS True, CO2_1990_BASELINE_MT 4614.1,
  HYDRO_SOC_CONSTRAINT False (placeholder), PIPELINE_VERSION v15.
- mapping.py: bus to country mapping (multi-bus countries DK ES GB IT share
  national data, Luxembourg proxied by Belgium).
- c2e_loader.py: reads C2E csvs per period, aligns to network snapshots.
- cf_methods.py: direct, delta (monthly mean factor), qdm (multiplicative
  quantile delta mapping per month with jitter guard); applied to wind, solar
  and ror profiles.
- heat_cooling.py: heating via country-month relative change ratios applied to
  the network's own thermal heat loads; cooling added at the IEA 3 percent
  anchor with C2E shape and growth; hydro inflow scaled by relative change
  (delta or qdm on the inflow series), run of river treated as a capacity
  factor; unmapped hydro units are now logged by name together with the C2E
  hydro file coverage.
- build_modified_network.py: orchestrates the modifications, writes the
  modified .nc and the climate signal tables.
- dispatch.py: prepare_for_dispatch (Gøtske-exact freeze, shedding, CO2 price,
  hygiene), dispatch (solver), get_co2_emissions_Mt (bus flow plus loads plus
  store cross-check), extract_results (all metrics including the Gøtske
  electricity adequacy), test hooks.
- reporting.py: per-method figures and tables with _original companions, the
  new analysis tables, auto headline findings, per-run README, method
  comparison with version stamps.
- run_pipeline.py: Step 1 reads the stored CO2 price from the design network,
  dispatches the original once (cached), then per method builds, dispatches,
  reports; finally the comparison.
- audit_real_data.py: pre-run checks on the real data, now including the v15
  exactness section (store attributes, atmosphere loads, freeze scope counts,
  shedding placement preview, 1990 basis).
- test_co2_mechanism.py: the mechanism unit test described above.

## 8. Open questions carried forward

- Which C2E wind turbine type matches PyPSA-Eur-Sec v0.6.0 assumptions
  (pending; affects none of the above).
- The CORDEX SSP4.5 scenario label on the C2E files versus the published C2E
  description (to confirm with the supervisor).
- Whether to source ENTSO-E reservoir data and implement their hydro SOC
  constraint for a sensitivity run (would remove the one structural deviation).

================================================================================

# EXAMINATION v16: method redesign (drop delta, two coherent worlds, heating/cooling per the supervisor)

Date: June 2026. This extends the v15 examination. Triggered by (a) a decision
to retire the legacy delta method, (b) a literature re-check of whether anything
supersedes QDM for this transfer, (c) the supervisor's notes on heating and
cooling, and (d) the confirmed project scenario (SSP4.5, 2015-2100, the C2E
files in the project folder).

## 1. Scenario and dataset, now pinned

- Scenario is SSP4.5 (IPCC), confirmed by the initial project description. The
  C2E file labels (2015 / 2042 / 2099) are slice years, not the published
  CESM2-SSP3-7.0 run; these files are a project-specific C2E production. Recorded
  so the write-up names the scenario correctly.
- C2E methodology, read from Wohland et al. (2025): C2E already performs
  per-grid-box delta-quantile-mapping of the meteorology against ERA5 over
  1995-2015 BEFORE the energy conversion. So the offset we remove with QDM is the
  CONVERSION-chain difference (the better-than-median siting rule, the specific
  turbine fleet, the GSEE PV model) between C2E and the network's atlite/ERA5
  world, not raw climate-model bias. This sharpens the justification: direct is a
  different internally-consistent modelled world, QDM is the bridge that extracts
  only the climate-change signal.

## 2. Methods: two coherent worlds (delta removed)

The pipeline now runs exactly TWO methods, each a self-consistent world:

- qdm (primary): the network keeps its own weather-year chronology; every
  weather-dependent variable is reshaped quantile by quantile by the C2E
  future/baseline change. Isolates the climate signal.
- direct: the system experiences the C2E future year wholesale (raw CF
  substitution; demand and hydro as energy-anchored shape transplants). This is
  Gotske's own weather-year stress philosophy (their update_renewables is literal
  weather-year substitution) extended to a future year, in the C2E modelled
  world.

The legacy monthly delta method is deleted from cf_methods.py, heat_cooling.py,
config.METHODS, the builders, the argparse choices, and the report text. It was
the awkward middle: it removed the dataset gap like QDM but kept only the mean
shift, so it carried no advantage over QDM and no transparency advantage over
direct. The qdm-vs-delta validation figure is archived in
docs/qdm_vs_delta_validation.png as the record of why.

Literature check (no better alternative adopted, with reasons):
- Plain empirical quantile mapping distorts the climate-change signal; QDM
  (Cannon et al. 2015) exists precisely to preserve changes in quantiles and
  extremes. Keep QDM.
- ISIMIP3BASD (Lange 2019), the impact-community standard, is the same
  trend-preserving quantile family, but parametric and designed for raw
  meteorological variables upstream of the energy conversion, which is exactly
  where C2E already applies quantile mapping. Not a replacement at our level.
- Multivariate methods (MBCn; Vrac and Friederichs 2015) fix inter-variable
  dependence. We do not need them: keeping the network's own 2015 chronology
  preserves the physical coherence between wind, solar and demand by
  construction. C2E makes the same argument for its grid-box approach.
- Deep-learning hybrids (e.g. SRDRN-QDM) are built ON TOP of QDM, not instead of
  it. Out of scope and unnecessary here.
Verdict: QDM at the energy-variable level, monthly, with the jitter guard,
remains the right primary and sits in the same family the dataset's own authors
use one level down.

## 3. Heating (the supervisor's "daily scaling factor")

The trap: the C2E baseline and future are free-running climate years, not
synchronised with the network's weather year (Wohland et al. 2025 state the model
is non-initialised). So a day-paired or hour-paired future/baseline ratio is
synoptic noise, not climate. Both methods avoid paired ratios:

- qdm heating: DAILY quantile-delta multipliers per country, computed on daily
  heat ENERGY within a 3-month centred window (months m-1, m, m+1, circular),
  then applied uniformly across the snapshots of each day so the network's own
  within-day profile is preserved. This is the daily scaling factor at
  distribution level: mild days and cold-extreme days each carry their own
  change, and the heating-season shortening appears at daily granularity instead
  of being smeared over a month. Guards: multipliers in [0, 5]; deep-summer days
  (window baseline near zero on both C2E sides) get multiplier 1.
- direct heating: SHAPE TRANSPLANT. Each load takes the C2E future hourly shape,
  its annual energy anchored via the C2E baseline, with the load's own 5th-
  percentile hot-water floor preserved:
    new(t) = floor + fut(t) * (orig_variable_annual / base_annual)
  The annual change then equals the C2E future/baseline annual ratio and the
  hour-to-hour realisation is the C2E future year, consistent with the raw CF
  substitution of the direct world.
- Units cancel in both (ratio or anchor), so the heating-file unit convention is
  irrelevant. C2E's two electrification scenarios also cancel under qdm ratios;
  we still load the fully-electrified file to match the sector-coupled network.

Verified by test_v16_demand.py: direct reproduces the annual ratio exactly and
transplants the shape (corr ~1.0); qdm reproduces a uniform factor and keeps the
within-day multiplier constant.

## 4. Cooling (the supervisor caught a real double-count)

The flaw in the old design: the Gotske electricity loads are built from
historical ENTSO-E shapes, so the weather year's cooling is ALREADY embedded in
them; the old IEA-3%-anchored add-on stacked a second cooling load on top.

The fix uses the key fact that C2E cooling comes from demand.ninja with country
sensitivities calibrated on observed demand-temperature response (Wohland et al.
2025; Staffell et al. 2023). So the C2E BASELINE cooling is precisely an estimate
of the cooling already embedded in the historical load, i.e. exactly the
"synthetic cooling demand extracted from historical data" the supervisor asked
for. Mechanics, per country, on the electricity loads:
  subtract the month-by-hour climatology of C2E baseline cooling (climatology,
  not hour-paired, for the free-running-year reason above), then add:
    qdm    : the same climatology of the FUTURE cooling -> net effect is the
             climatological cooling change on the network's own chronology
             (conservative on event coincidence; stated openly).
    direct : the raw C2E future hourly cooling series -> heat-wave cooling spikes
             coincide with that model year's wind and solar (physically
             consistent within the C2E world; the "more load shedding, more to
             talk about" case, especially for 2099).
Guards: final load floored at zero with clipped energy accounted and logged. The
IEA 3% anchor is retired as a mechanism and survives only as an audit cross-check
(COOLING_BASELINE_SHARE_CROSSCHECK).

This is also the ONE place C2E demand LEVELS enter the pipeline (heating and
hydro use level-cancelling anchors). C2E demand files are hourly GWh, converted
x1000 to MW; the audit prints a base-cooling-vs-national-demand sanity check
(expect single-digit % shares, south > north).

Verified by test_v16_demand.py: future == baseline leaves the electricity load
unchanged to 1e-6 (extract and re-add cancel); doubling the future cooling adds
net positive energy.

## 5. Hydro made consistent with the two worlds

modify_hydro now mirrors the method set: qdm keeps the multiplicative
quantile-delta on the inflow and ror series; direct uses an energy-anchored shape
transplant (future series scaled so annual energy moves by the future/baseline
ratio), units cancelling. The delta-era monthly-ratio and guarded-ratio helpers
are deleted. C2E hydro files are weekly (inflow) and daily (ror) cumulative GWh;
the averaging resample spreads each native value flat across the finer grid,
giving a constant rate within the native period, which is fine because both
methods use ratios where the per-period unit cancels (documented in
c2e_loader.py).

Verified by test_v16_demand.py: direct inflow scales annual energy by exactly the
future/baseline ratio.

## 6. Turbine selection resolved (the standing open question)

C2E offers three turbines; the choice matters for direct (absolute power curve)
and largely cancels for qdm (ratio). Matched by SPECIFIC POWER to PyPSA-Eur-Sec
v0.6.0:
  PyPSA-Eur onwind default  Vestas V112 3.0 MW : ~305 W/m2
  PyPSA-Eur offwind default NREL 5 MW reference: ~401 W/m2
  C2E SWT120_3600 ~318 W/m2 | SWT142_3150 ~199 | E-126_7580 ~600
SWT120_3600 is the closest match for both on- and offshore and is now the
configured turbine (WIND_TURBINE). The audit lists which turbine CSVs are
present and confirms the selection.

## 7. A latent bug checked and found ABSENT (recorded for safety)

C2E files key countries by FULL NAME (Germany, France, ...), and
country_of_bus() returns the same namespace, so heating, cooling and hydro
country lookups match correctly. This was verified against the original
real-data audit (which keys C2E by full country names and computed the Germany
heat ratio ~1.66). There is NO ISO-2 vs full-name mismatch. A new-code audit
line that briefly used ISO codes was corrected. If C2E files are ever
regenerated with ISO-2 keys, every demand/hydro modification would silently
become a no-op; the audit's per-country heat-ratio and cooling-share tables are
the tripwire (they would print empty or skip-all).

## 8. A real bug found and fixed during v16 testing

_month_hour_climatology() (cooling) used a groupby on a list of tuples followed
by .loc, which raised KeyError on real index types. Rewritten as a vectorised
groupby(['m','h']).transform('mean'). This would have crashed every cooling run;
it is now covered by test_v16_demand.py.

## 9. New outputs

- heating_change_by_country.csv: qdm prints per-country daily-multiplier
  min/mean/max; direct prints the annual ratio.
- cooling_change_by_country.csv: embedded base cooling extracted, future cooling
  added, net change, share of electricity demand, and clipped energy, per
  country.
Both sit alongside the existing cf_change tables as the climate-signal record.

## 10. Tests run before delivery (v16)

- test_co2_mechanism.py: unchanged, still passes (freeze scope, DAC credit, cap
  removal, emissions-store closure).
- test_v16_demand.py (new): heating direct annual-ratio + shape transplant;
  heating qdm uniform-factor + within-day shape preservation; cooling
  extract-then-add cancellation and additivity; hydro direct inflow ratio. All
  pass.
- Full synthetic end-to-end (both methods): completes, both dispatches optimal,
  heating/cooling fire on all countries, new tables written, original companions
  differ from modified.

## 11. Files changed in v16

cf_methods.py (delta removed, two-method dispatcher, QDM docstring),
heat_cooling.py (heating daily-QDM + direct transplant; cooling redesign; hydro
two-world; dead helpers removed), config.py (two methods, SWT120_3600, retire
DEMAND_METHOD and the IEA anchor, PIPELINE_VERSION v16), build_modified_network.py
and run_pipeline.py (wiring, new tables, method choices), reporting.py (blurbs,
comparison intro), audit_real_data.py (cooling/turbine section), c2e_loader.py
(hydro resampling note), make_synthetic.py (filenames from config), and the new
test_v16_demand.py.

================================================================================

# EXAMINATION v17: the heating and hydro transform fix (zero-inflated variables)

Date: June 2026. This is the fix for the two real method bugs found in the first
full v16 dual-method run. Both had the same root cause and now share one fix.

## 1. What was wrong (diagnosed from the v16 run)

The first real run produced 84 TWh of shedding under qdm and 113 TWh under
direct. Almost all of it was an artifact of two transform bugs, not a climate
result. The original 2015 reference was adequate (negative objective), so the
shedding appeared only after the demand and hydro modification.

- Heating, qdm: the per-quantile daily multiplier swung from 0 to the 5.0 cap,
  with 20 of 32 countries pinned at the cap on some days. Quintupling heat
  demand is unphysical for warming.
- Hydro, qdm: Norway shed 36 TWh (43% of all qdm shedding), but only 1.96 TWh
  under direct on the same C2E data. Sweden and Finland showed the same pattern
  at smaller scale.

Root cause, common to both: multiplicative quantile mapping applied to
zero-inflated variables (heating energy and reservoir inflow are statistically
precipitation: many near-zero values, a non-negative multiplicative scale).
Two known failure modes of that method:
  (a) the change factor q_fut(tau)/q_base(tau) is unstable where the baseline is
      near zero (the low tail), and the instability is WORSE where the network
      and C2E have different seasonal SUPPORT (C2E has almost no spring heating
      while the network has a large heating season): a per-quantile map between
      distributions that do not align is not meaningful;
  (b) pure multiplicative mapping does not conserve the mean change, so totals
      drift. For a CYCLIC reservoir, annual generation cannot exceed annual
      inflow, so a drift in the annual inflow VOLUME starves the reservoir and
      shows up as steady, year-round unserved energy (Norway).

## 2. The fix, grounded in the bias-correction literature

Heating and hydro inflow now use a smoothed seasonal CHANGE FACTOR with mean
conservation, in cf_methods.apply_change_factor, instead of the per-quantile
multiplier. Three standard ingredients:

  1. Smoothed day-of-year change factor: f(d) = clim_fut(d)/clim_base(d), where
     clim_* are the SMOOTHED (running-mean, 31-day) day-of-year climatologies of
     the C2E future and baseline. Indexing the factor by CALENDAR position, not
     by the value's quantile, is the key fix: it is meaningful even when the two
     distributions have different seasonal support. This is a change-factor /
     delta-scaling method (Lazoglou et al. 2024), the dominant approach in
     climate-impact studies.
  2. Dry-season flooring (the change-factor analogue of Singularity Stochastic
     Removal; Vrac et al. 2016): where the baseline climatology is below 5% of
     its annual max there is essentially nothing to rescale, so the factor is
     set to 1 (no-op) rather than a noise-driven ratio. A final clip to [0.2,
     3.0] is a physical guard.
  3. PresRAT mean conservation (Pierce et al. 2015): after applying f(d), the
     realised mean change on each window is forced to equal the raw C2E model's
     mean change. Heating uses a SEASONAL window; hydro inflow uses an ANNUAL
     window, which is what conserves the cyclic reservoir's inflow volume.

Bounded capacity factors (wind, solar, run-of-river) are LEFT on the existing
multiplicative QDM (Cannon et al. 2015). They are bounded in [0,1] and clip
harmlessly, and per-quantile change in the VRE extremes is the thing one wants
there. The fix is applied only to the zero-inflated variables that needed it.

Direct is unchanged on supply and cooling; its heating and hydro remain the
energy-anchored shape transplants, which already conserve volume by
construction (this is why direct's Norway was fine), with the open question
about direct's heating profile transplant left for the supervisor.

## 3. References (verified against the sources)

Cannon, Sobie, Murdock 2015, Journal of Climate 28:6938-6959 (QDM, kept for
bounded capacity factors). Pierce et al. 2015 (PresRAT, the mean-conservation
correction). Vrac et al. 2016 (SSR, dry-value handling). Lazoglou et al. 2024
(change-factor methods). Lehner, Nadeem, Formayer 2023, ASCMO 9:29-44 (the
comparison paper showing QDM with mean conservation and dry handling satisfies
mean-match, change-signal preservation and dry-frequency conservation).
Themessl et al. 2012 (doi 10.1007/s10584-011-0224-4) is the alternative
frequency-adaptation reference.

## 4. Tested before delivery

- test_v16_demand.py extended with two REGRESSION tests that reproduce the
  exact bugs: a heating case with mismatched C2E/network seasonal support (the
  multiplier now caps at 1.0, not 5.0) and a snowmelt-driven cyclic-reservoir
  inflow case (the qdm annual volume change now equals the model change to four
  decimals). Both pass, along with the original four demand tests.
- test_co2_mechanism.py still passes (the carbon machinery is untouched).
- Full synthetic end-to-end with both methods completes, both dispatches
  optimal, and the heating multipliers in the output tables are bounded (the v16
  run had them at the 5.0 cap).

## 5. Also fixed (reporting)

- The CO2 price is now written into the modified-run results, so the deep
  reliability table, the cost decomposition and master_metrics no longer print
  nan for it (the price was applied all along; only the display was missing).
- The comparison report's interpretation text, which still referenced the
  removed DELTA method and "all three" methods, now describes the two-method
  framing.
- The qdm method blurb now describes the change-factor treatment of heating and
  hydro honestly, rather than "daily quantile-delta multipliers".

## 6. Files changed in v17

cf_methods.py (new apply_change_factor with smoothed seasonal factor, PresRAT
mean conservation and dry flooring; _ccs_factor helper; the per-quantile
SSR-CCS attempt and the unused SSR trace helper were removed after testing
showed the per-quantile route over-amplifies on the mismatch case),
heat_cooling.py (heating qdm and hydro inflow qdm routed through
apply_change_factor; the old per-quantile daily multiplier and the _qdm_demand
inflow helper removed), run_pipeline.py (CO2 price stamped on modified
results), reporting.py (qdm blurb, comparison interpretation text), config.py
(PIPELINE_VERSION v17), and test_v16_demand.py (two regression tests).

================================================================================

# EXAMINATION v18: per-channel method assignment (the production configuration)

Date: July 2026. This makes the method choice per-variable instead of one global
method, which is the final production configuration for the sweep.

## The decision

Use DIRECT for everything EXCEPT heating demand and reservoir inflow, which use
QDM. Each variable is routed to the method that does not contaminate it:

- DIRECT (raw C2E future) for wind, solar, run-of-river and cooling. Here the
  C2E world is kept internally coherent, and its fuller stress (the capacity-
  factor level offset and the within-year coincidence of extremes) is what we
  want to see.
- QDM (smoothed seasonal change factor with mean conservation) for heating and
  reservoir inflow, the two channels where DIRECT would transplant a C2E
  seasonal SHAPE onto Gotske infrastructure sized for a different shape:
    * heating, because C2E's demand profile differs from the network's own;
    * reservoir inflow, because C2E inflow is river-discharge based while the
      network's is runoff based, so the seasonal timing differs even for the
      same past year, and the cyclic reservoir needs the annual volume conserved.
  run-of-river stays DIRECT (bounded CF, no volume constraint, timing mismatch
  harmless). Heat-pump COP follows the heating channel.

## What changed in the code

- config.py: new CHANNEL_METHODS dict (the per-channel assignment), RUN_PROFILE
  label ('mixed'), and USE_CHANNEL_METHODS flag. When True (default), the
  pipeline runs ONE mixed-profile production run; when False it falls back to the
  legacy pure-qdm and pure-direct runs (kept for the appendix comparison).
- build_modified_network.py: build() now accepts either a method string (legacy,
  applies to all channels) or a per-channel map. A resolver validates the map and
  routes each channel. KEY FIX: the C2E baseline is now loaded whenever ANY
  channel needs qdm; previously the supply baseline gated all baselines on
  method=='qdm', which would have starved the qdm hydro and heating channels of
  their baseline in a mixed run.
- heat_cooling.py: modify_hydro() takes a separate ror_method (defaults to the
  inflow method), so inflow=qdm and ror=direct can coexist.
- run_pipeline.py: iterates over run specs (label, method-or-map). Fixed a subtle
  bug where `methods = methods or config.METHODS` clobbered the None sentinel and
  silently forced the legacy two runs, skipping the mixed profile.
- reporting.py: added a 'mixed' method blurb describing the per-channel split and
  noting that cross-variable extremes between a DIRECT and a QDM channel are less
  tightly coupled than within one method.

## Tested

- New regression test (test_channel_method_routing) locks in the routing: the
  resolver honours a per-channel map, a bare string still maps all channels
  (legacy), invalid methods are rejected, and the SHIPPED config is the intended
  split. Fails loudly if anyone reverts to a single global method.
- Full synthetic end-to-end in mixed mode: one 'mixed' run, supply/ror/cooling on
  direct and hydro_inflow/heating on qdm confirmed in the log, both dispatches
  optimal, report and comparison produced, heating multipliers bounded, version
  stamped v18, stale flag False.
- All prior demand/hydro regression tests and the CO2-mechanism tests still pass.

================================================================================

# EXAMINATION v19: full-pipeline audit, hydro anomaly resolved, reporting rebuilt

Date: July 2026. A from-scratch audit of the whole pipeline, grounded in the C2E
paper itself (Wohland et al. 2025, arXiv:2508.09531, incl. SI A), plus the
resulting fixes. No method change to the production mixed configuration, which
the audit CONFIRMS and now grounds in the dataset's own documentation.

## What the research established (all verified at source)

1. C2E is CESM2 v2.1.2 under SSP3-7.0 (NOT CORDEX, NOT SSP4.5). Paper windows:
   1995-2015 historical, 2080-2100 future; the 2015/2042/2099 files are
   single-year slices of one realization of the transient run.
2. THE SOUTHERN HYDRO ANOMALY IS EXPLAINED, and it is not a pipeline bug:
   (a) single-year ratios mix forced signal with internal variability; the
   paper's Spain -40% is a 20y x 9-realization ensemble mean, not comparable;
   (b) the authors explicitly caution against sub-annual CESM2 hydrology
   (SI A.3; CESM2 misses snowmelt seasonality) while validating ANNUAL totals
   (~6% error); (c) the ror-vs-inflow contrast is structural: C2E ror is
   generation through a piecewise regression WITH A SPILL SEGMENT (saturates in
   wet conditions; Spain's flat 34.811 is the saturation regime, not a fill
   value), while reservoir inflow is uncapped water arrival.
3. Our hydro coverage (10 inflow countries, 6 ror countries) is C2E's own
   design (80% of inflows, 83% of ror generation; SI Fig. S2), not a mapping
   failure. Finland is excluded by the C2E authors for lack of ENTSO-E data.
4. The paper itself concludes single-year analyses "cannot approximate the
   ideal stochastic solution" - direct published support for the sweep framing.
5. The mixed method is now literature-aligned, not just artifact-avoiding: qdm
   inflow with ANNUAL conservation uses C2E exactly where its authors trust it
   (annual) and keeps the network's ERA5-based seasonality where they do not
   (sub-annual).

## Code changes (v19)

- config.py: SCENARIO_LABEL corrected to "CESM2 SSP3-7.0 (single-year slices)"
  with the verified provenance in comments (the old "CORDEX SSP4.5, confirmed"
  was wrong on both counts); duplicate CO2_1990_BASELINE_MT removed; new
  HYDRO_INFLOW_FACTOR_MODE = 'seasonal' (default, closest to C2E as given) |
  'annual' (scalar annual ratio only, a documented sensitivity aligned with the
  timescale the C2E authors validate). Version v19.
- heat_cooling.py modify_hydro: per-country applied annual inflow ratios and
  coverage volumes in the summary; 'annual' mode; run-of-river clip-to-1 energy
  loss now logged when >0.5% (was silent).
- build_modified_network.py / run_pipeline.py: hydro summary captured and
  written as hydro_change_by_country.csv and hydro_coverage.csv per run.
- reporting.py: (i) percent-change display returns n/a for from-zero and
  sign-flip cases (was printing billions of percent); (ii) new section 1b
  "Channel methods and applied climate signal": method-per-channel table,
  per-country applied hydro and heating ratios as tables AND bar figures, an
  automatic caution paragraph when any applied inflow ratio exceeds +/-50%
  (citing the single-year variability point), and the coverage table; (iii) new
  section 10 "Data provenance and caveats" with the verified dataset facts, the
  single-year caveat, the hydro construction explanation, the reservoir
  full-foresight deviation, and the comparability statement; (iv) header notes
  pipeline version and the snapshot-calendar-vs-weather-year label; (v) the
  comparison report rewritten for the mixed era, with stale rows explicitly
  flagged as produced by an older pipeline version.

## Tested

- Two new regression tests: hydro stats + annual mode (both modes conserve the
  annual change; coverage volumes correct; per-country ratios reported) and the
  percent-display fix. All 9 demand/hydro tests and the CO2 tests pass.
- Full synthetic end-to-end in mixed mode: new CSVs produced, report contains
  sections 1b, 10 and 11, channel table and hydro tables render, coverage line
  appears in the log.

## Still open (not code)

- The exact realization the 2015/2042/2099 files come from (ask Bryn or check
  the Zenodo record 10.5281/zenodo.15269455 file naming).
- Whether to also present the 'annual' inflow mode as a sensitivity in the
  thesis (one config switch, one rerun).
- Pure qdm and pure direct comparison rows are stale (v17); re-run under v19 if
  a clean three-way table is wanted.

## v19.1 correction (same day)

The v19 scenario relabel to "CESM2 SSP3-7.0" was WRONG for these files and has
been reverted. The published C2E paper does describe CESM2 SSP3-7.0 over
1995-2015/2080-2100, but the files in this project (2015/2042/2099, transient
2015-2100) are a DIFFERENT production of the C2E framework, confirmed by the
project description and the supervisor as CORDEX SSP4.5. The label is restored
to CORDEX SSP4.5; the paper is now cited for the conversion methodology only.
Everything else in v19 survives: the single-year internal-variability caveat
(stronger, if anything, under the milder SSP4.5), the ror-saturation vs
uncapped-inflow explanation (a property of the C2E conversion, not the driving
model), the coverage-by-design point, the transparency tables/figures, and the
qdm-inflow justification (network timing kept, definitional discharge-vs-runoff
difference cancels in the ratio, annual volume conserved). Remaining TODO:
record the exact GCM-RCM chain and realization from the supervisor.

================================================================================

# EXAMINATION v20: the sweep (parallel multi-year orchestration on the VM)

Date: July 2026. Adds the orchestration layer that turns the validated
single-run engine into the thesis's core result: every requested Gotske design
year stress-tested against C2E 2042 AND 2099, in parallel, restartably.

## Architecture (and the reasoning behind it)

- Each (weather_year, future) task is a SUBPROCESS of `python -m
  src.run_pipeline`, configured by env vars. config.py is global-mutable
  (WEATHER_YEAR / C2E_FUTURE read everywhere), so subprocesses are the only
  safe parallel unit. config.py now reads SWEEP_WY, SWEEP_FUTURE and
  SWEEP_GUROBI_THREADS from the environment (defaults unchanged).
- THE WORK UNIT IS A WEATHER YEAR: the futures of one year run sequentially
  inside one worker because they SHARE the cached original dispatch
  (_original_wy{Y}_results.pkl, computed once by the first future). Different
  years run in parallel (ThreadPoolExecutor over per-year chains). This avoids
  both the duplicate original solve and the cache write race that naive
  task-level parallelism would create.
- Restartable by construction: a JSON marker per completed task
  (output/sweep_status/<name>/wyY_fF.json); rerunning the same sweep name
  skips finished tasks. Success = subprocess exit 0 AND the per-run result
  cache exists (belt and braces). Per-task logs, per-task timeout
  (--timeout-hours, default 6), worker start staggering (--stagger-seconds 60)
  so memory peaks do not coincide, and a machine-readable _summary.json.
- Preflight before anything solves: design networks discovered on disk
  (missing years skipped and reported), each requested future checked for its
  essential C2E files (unusable futures dropped with the missing filenames
  listed), plan printed with wall-time math; --dry-run shows the plan only.
- RUN_TAG nests per-task outputs under output/<sweep-name>/wyY_fF/ so nothing
  collides and every run keeps its full thesis-quality report.

## Files

- src/sweep.py         orchestrator (parse_years: '2002-2021' | '2010,2015' |
                       'last:10' | 'all'; --workers, --threads-per-worker,
                       --timeout-hours, --sweep-name, --dry-run, --collect)
- src/collect_sweep.py harvests all per-run result caches into
                       sweep_master.csv (one row per weather_year x future,
                       modified metrics + original references + deltas +
                       %-of-1990) and the headline figures: unserved vs design
                       year per future, the adequacy exceedance tail, CO2
                       drift, and the shed-vs-emit plane; plus SWEEP_SUMMARY.md.
                       Can be run at ANY time, including mid-sweep.
- docs/SWEEP_RUNBOOK.md the operational VM guide: tmux launch, license
                       concurrency check, memory monitoring, sizing table
                       (20 years x 2 futures = 60 solves, ~30-35 h at 2
                       workers on n2-highmem-8), restart semantics, download.
- src/test_sweep_plan.py planning tests: year parsing, network discovery,
                       marker skip-done mechanics, future preflight.

## Tested

- Planning tests pass (parse_years variants incl. guards; missing networks
  skipped; markers remove done tasks from chains; unusable futures reported).
- Full synthetic end-to-end: `sweep --years 2015 --futures 2042 2099
  --workers 1 --collect` correctly DROPPED 2099 (files absent), ran the wy2015
  x 2042 task as a subprocess (ok, 0.9 min), wrote the marker, and the
  collector produced the one-row master CSV, all four figures and the summary.
  A rerun with the same sweep name skipped the task ("nothing to do").
- All prior suites still pass on the restored production config (Gurobi, v20,
  no test-solver leak).

## Deliberate limitations / open

- Success is judged by exit code + result cache, not by metric plausibility;
  eyeball the collector CSV for outliers (a solve that converged poorly would
  still count as ok).
- The Gurobi license concurrency check is a runbook step, not automated.
- The weather-year population question (what the sweep iterates over BEYOND
  the two C2E slices) remains a Bryn question; this sweep iterates Gotske
  design years x {2042, 2099}, which is the agreed scope.

================================================================================

# EXAMINATION v20.1: HOTFIX - the hydro climate signal was never applied

Date: 2 July 2026, after the first real v19 run. CRITICAL fix, found BEFORE the
sweep launched.

## What the real run revealed

The new v19 transparency table did its job: it showed applied_annual_ratio =
1.000 for ALL 10 reservoir countries (new_TWh == orig_TWh exactly), when the
C2E files demand Spain ~2.49, Norway ~0.70. The hydro inflow signal was NOT
being applied. A second clue sat in the log: "filled 20440 NaN in p_max_pu" =
exactly 7 ror generators x 2920 snapshots.

## Root cause

c2e_loader._resample_to_grid used series.resample(freq).mean() for ALL files.
For series NATIVELY COARSER than the 3h grid (weekly inflow, daily ror), this
places each native value in one bin and leaves every other bin NaN - it does
NOT spread values flat, contrary to the comment above it. Downstream:
- qdm inflow: the NaN-contaminated climatology collapsed the change factor to
  1.0 everywhere and the annual-conservation guard returned 1.0 -> inflow
  returned bit-identical (ratio exactly 1.000).
- direct ror: base_tot became NaN, passed the <=1e-9 guard, and fut x NaN
  wrote ALL-NaN capacity factors, which dispatch prepare silently filled
  (the 20440 line). ror was not merely unchanged; it was replaced by the
  fill value.
Hourly channels (supply, heating, cooling) are DOWNSAMPLED and were correct
throughout - which is why their report tables showed real signals.

## Consequences for prior results

The v18 AND v19 real runs (wy2015 x C2E2042) carried this silently: their
headline numbers (21.85 TWh unserved, -8.7 Mt CO2, 2,574 TWh curtailment)
include NO hydro climate change and an arbitrarily-filled ror channel. The
synthetic e2e HAD shown the signature twice (hydro ratio 1.000 against a
generated 0.9x future; "filled 5840 NaN" = 2x2920) and was misread as
"future=baseline" / "pre-existing behaviour". Results must be re-run.

## Fix (v20.1, inside the v20 package)

- c2e_loader._resample_to_grid: after resample().mean(), forward-fill then
  back-fill when NaN present (flat value within each native period, the
  documented intent); a hard guard now REFUSES to return any series containing
  NaN, so this failure class can never again pass silently.
- heat_cooling ror direct: base_tot guard extended to non-finite values.
- reporting section 1b: figures dir created before saving the hydro ratio
  figure; the figure has its own try/except so a figure failure can no longer
  print a misleading "table unavailable" note next to a rendered table.

## Verified

- New regression test (test_loader_upsampling_no_nan): weekly and daily files
  arrive NaN-free, flat within native periods, head back-filled, tail held.
- Full synthetic e2e: hydro table now reports applied_annual_ratio = 0.900
  against the generated 0.9x future (was 1.000); NO NaN-fill line anywhere in
  the log; the hydro ratio figure is written and embedded; the +/-50% caution
  correctly stays silent at 0.9. All suites pass (10 demand/hydro + CO2 +
  sweep planning); production config clean (Gurobi, v20).

## Expected effect on the next real run

The hydro signal will now actually enter: Southern inflows rise steeply
(Spain ~x2.5, Italy ~x2.3, Portugal ~x3.3), Nordics fall (Norway ~x0.70,
Sweden ~x0.75 - about -60 TWh of Nordic inflow), and ror gets the real C2E
daily series instead of a fill value. Headline unserved energy, curtailment
and CO2 WILL change; re-run wy2015 x 2042 once to re-baseline before quoting
numbers, then launch the sweep.


================================================================================

# EXAMINATION v20.2: the sweep-results package (cross-run analysis layer)

Date: July 2026. The collector is rebuilt from "CSV + 4 figures + short md"
into a complete, organised analysis package, because the per-run pickles
already carry regional, sectoral, monthly and event-level detail that the
thesis needs aggregated.

## What collect_sweep now produces (sweep_results_<stamp>/)

- report/SWEEP_REPORT.docx: headline statistics per future, five most robust
  and five most fragile designs (ranked by mean rank across futures), all
  figures embedded with captions, regional concentration table, the ten
  largest shortfall events anywhere in the sweep, provenance and caveats.
- tables/: sweep_master.csv (~38 columns per run incl. Gotske-basis adequacy,
  event stats, worst-event month, original references and deltas);
  stats_by_future.csv (flattened distribution stats); design_ranking.csv;
  shed_by_region_long.csv + shed_by_region_wide_{future}.csv; 
  shed_by_sector_long.csv; unserved_by_month_long.csv; worst_events.csv.
- figures/: the original four (unserved by design year, adequacy exceedance,
  CO2 drift, shed-vs-emit plane) PLUS region x year heatmaps per future,
  the 2042-vs-2099 vulnerability-persistence scatter, the monthly stress
  profile with min-max bands (does the pressure move seasonally?), worst-
  event timing (month x duration x energy), heat-vs-electricity composition
  bars, and overlaid unserved duration curves.
- README.md file index; SWEEP_SUMMARY.md prose summary.

All of it is computed from the existing per-run result caches: no re-solving,
re-runnable at any time including mid-sweep, robust to partial sweeps, single
futures, and missing keys. The docx builder degrades gracefully if python-docx
is absent.

## Tested

Fabricated a 12-run set (6 design years x 2 futures + 6 originals) carrying
every key the collector consumes, including region/sector/month dictionaries,
worst-event lists and duration-curve Series. The collector produced 9 tables,
11 figures and the report (6 sections, 5 tables, 11 embedded images);
heatmap and monthly-profile figures visually verified; design ranking and
flattened stats checked numerically. Master CSV: 38 columns.

================================================================================

# EXAMINATION v21: the multi-system adapter layer (PyPSA-only extension)

Date: July 2026. The pipeline gains a SYSTEM axis so additional published
PyPSA design families can be stress-tested with the same imposition math,
dispatch rules and reporting. Gotske becomes adapter #1 with BYTE-IDENTICAL
behaviour; neumann2023 (Neumann, Zeyen, Victoria, Brown 2023, Joule 7:1793,
"The potential role of a hydrogen network in Europe") is adapter #2, encoded
from the paper's own repository.

## Verified facts encoded (source: github.com/fneum/spatial-sector)

- 181 regions, 3-hourly, planning horizon 2050, MIT licence.
- Scenario grid (configs/config.main.yaml): lv in {1.0, opt} x sector_opts in
  {Co2L0-3H-T-H-B-I-A-solar+p3-linemaxext10}{'', -noH2network, -onwind+p0,
  -noH2network-onwind+p0} = 8 designs; filename grammar
  elec_s_181_lv{lv}__{sector_opts}_2050.nc.
- REMAINING USER VERIFY: the solved-networks Zenodo DOI (one line in the
  paper's Data availability statement) and the turbine class in their atlite
  config. Both are flagged in the registry and the onboarding doc.

## What was already generic (audit finding, no code needed)

country_of_bus is bus-prefix based; the CO2 shadow-price reader searches
constraint names; hydro selection is component-generic (storage units with
inflow; ror by carrier); heat loads are found by name substrings that follow
PyPSA-Eur-Sec conventions across versions; HEAT_COP_MODE defaults to 'keep'.
The genuine adapter surface reduced to: filename grammar + discovery, cache
identity, an ISO2 country fallback for finer bus zonings, and one carrier-map
extension.

## Changes

- NEW src/systems.py: the registry. Per system: label, filename grammar,
  discovery regex, provenance (paper/data/turbine), expectations for
  onboarding, notes. Helpers: default_design_id (gotske keeps SWEEP_WY
  semantics; others use SWEEP_DESIGN), network_path, design_key
  ('wy2015' for gotske = v20 identity; '{system}--{design}' otherwise),
  discover_designs, split_design_key.
- mapping.py: ISO2_TO_COUNTRY fallback in country_of_bus (37-zone table
  exact as before; 'DE1 3 residential rural heat' -> Germany; unknown ->
  None); SUPPLY_CARRIER_TO_C2E gains offwind-float.
- config.py: SYSTEM from SWEEP_SYSTEM env (default gotske); design_id(),
  design_key(), active_network_file(); run_dir uses design_key.
- run_pipeline / build_modified_network / reporting: every cache and folder
  identity ('_original_*', '_co2_price_*', '_dispatched_original_*',
  '_method_*', modified_*.nc, COMPARISON_*) generalised to design_key;
  gotske names BYTE-IDENTICAL to v20 so all existing caches, markers and
  sweeps remain valid. Report header carries system + design when not
  gotske; network loading via active_network_file.
- sweep.py: --system and --designs ('all' = discover on disk, or a comma
  list). Gotske CLI and code path untouched (tests unchanged and green);
  non-gotske uses plan_designs with per-design chains sharing that design's
  cached reference dispatch, filename-safe markers, and success checks
  against the new cache keys. Subprocess env: SWEEP_SYSTEM + SWEEP_DESIGN.
- collect_sweep.py: loader regex generalised to both cache grammars; run
  tuple now (system, design_id, weather_year|None, future, mod, orig);
  master/tables gain system + design_id columns; ranking indexes design_id;
  year-axis figures use the gotske subset; new fig11 per-design bars for
  non-year systems; scatter labels fall back to design ids; original-run
  reference resolved via the same key for both grammars.
- NEW src/onboard_system.py: the mandatory pre-sweep protocol as a tool.
  Prints and writes a report: pypsa version, snapshots/weightings vs
  expectations, component counts, extendability, global constraints + the
  shadow-price read, load-bus census (the shedding scope, made explicit),
  country resolution gaps, VRE carrier->channel->country coverage with
  unmatched lists, hydro units/countries vs C2E coverage, heat-load
  detection, C2E file presence per period, and a Gotske-calibrated memory
  estimate with an explicit over-64GB warning. --probe-solve runs ONE
  reference dispatch and reports wall time, peak RSS, and the
  serves-its-own-weather sanity (expect ~0 unserved). Exit code encodes the
  READY / NOT READY verdict.
- NEW src/test_systems.py (5 tests): gotske byte-compat (design_key +
  filenames via both paths), neumann grammar (8 unique designs, filename +
  key roundtrip), discovery separating families on a mixed folder, country
  resolution incl. ISO2 fallback and None for unknowns, carrier-map
  coverage.

## Verified

- All four suites green: demand/hydro (10), CO2 mechanism, sweep planning
  (unchanged), system adapters (5 new).
- Full synthetic end-to-end on the gotske path after a from-scratch fixture
  rebuild: exit 0; cache names byte-identical to v20
  (_method_mixed_wy2015_c2e2042.pkl etc.); applied hydro ratio 0.900 exactly
  against the generated 0.9x future; the generalised collector consumed the
  real pickles into 8 tables + 9 figures + SWEEP_REPORT.docx with the new
  system/design_id columns populated ('gotske', 'wy2015').
- One collector unpack missed on first pass (the duration-curve presence
  check) was caught BY this gate and fixed; the gate exists for exactly
  this.

## Not yet done (honest scope)

- No neumann2023 network has been test-loaded (files live behind the
  user-side DOI VERIFY); the onboarding tool is the instrument for that
  step and nothing solves before its verdict.
- The memory estimate is a scaling heuristic until --probe-solve runs once
  on real 181-node data.
- Cross-system comparison FIGURES beyond fig11 are deferred until real
  multi-system results exist; the master table already carries everything
  needed.

================================================================================

# EXAMINATION v21.1: third adapter (broad_ranges) and the regrid-frequency
# generalisation

Date: July 2026. Two additions driven by onboarding the first NON-3-hourly,
power-only family.

## New adapter: broad_ranges (Neumann & Brown)

The 'Broad ranges of investment configurations' project: PyPSA-Eur
POWER-ONLY, two scenarios (37 nodes at 4-hourly, 128 nodes at 2-hourly),
cost optimum plus a near-optimal ensemble (5 epsilon levels x 14
technology-extremising objectives per scenario, i.e. up to 140 alternative
designs within 1-8% of optimal cost). Everything encoded was read from the
paper repository itself (github.com/fneum/broad-ranges):
- filename grammar from rules/common.smk: optimum
  elec_s_{clusters}_ec_lcopt_{opts}.nc; near-optimal appends
  _E{epsilon}_O{objective} (e.g. 37_ec_lcopt_4H_E0.06_OGenerator+wind+min);
- their design solves use noisy_costs=true (Gotske's convention) and NO
  load shedding (ours is added at dispatch, as for every family);
- project archive: Zenodo 10.5281/zenodo.6642651. USER VERIFY: that the
  record ships results/networks/*.nc, plus the cutout weather year and
  turbine class from config.pypsaeur.yaml.
Role in the thesis: the sector-coupling ISOLATION test (does the winter-heat
failure mode exist without electrified heat?) and the equally-good-designs
ensemble within one family. sector_coupled=False: heating/cooling channels
are inert and flagged out of cross-family comparisons; the onboarding tool
now reads this flag instead of failing on missing heat loads.

## Regrid frequency is now network-derived (_grid_freq)

Previously every C2E series was regridded at the static config.SNAPSHOT_FREQ
('3h'): correct for gotske and neumann2023, silently WRONG for any 4-hourly
or 2-hourly family (mean-preserving resampling at the wrong frequency plus
positional trim = misaligned inputs with no error raised: exactly the
silent-no-op class this project's doctrine exists to kill). build() now
derives the frequency from the network's own snapshot weightings and threads
it through all six loader calls; the gotske path derives '3h' and is
byte-identical (re-verified end to end).

## Candidate dossier updates (research this session)

- van Greevenbroek et al. 2025, 'Little to lose' (Joule 9:101974, CC-BY):
  now known to be a PATHWAY study (2025-2050 in 5-year steps) built on TWO
  weather years, an easy 2020 and a difficult 1987; the stress-test
  candidates would be its 2050-horizon designs (brownfield). Solved-network
  deposit still VERIFY; no adapter until then.
- victoria2022 (Speed of transformations): unchanged, VERIFY.
Both are cited and described in the dissertation's new families section.

## Verified

- test_systems.py extended (6 tests): broad_ranges grammar roundtrip
  (optimum + near-optimal ids), three-family discovery disjoint on one mixed
  folder, sector_coupled flags, and _grid_freq unit checks (2h/3h/4h).
- Full battery green (demand/hydro 10, CO2, sweep planning, systems 6).
- Synthetic end-to-end on the gotske path with the derived frequency:
  exit 0, log shows 'grid: 2920 snapshots at 3h', cache names byte-identical,
  hydro transparency table present.

## Dissertation

The multi-system extension is now IN the dissertation (48 pages, 0 errors, 0
undefined refs): RQ3 extended to design families; a new methods section
'Additional design families under test' with the families table
(gotske / neumann2023 / broad-ranges) and the pending-verification
paragraph (van Greevenbroek incl. the 1987/2020 weather years; Victoria);
the sweep's family axis; the onboarding protocol in V&V; the
'single ecosystem' limitation; a Results 'Across design families' stub and
the Discussion RQ3 hook; bibliography +5 verified entries.

## v21.1 reverification addendum (critical audit)

A full re-audit of the shipped artefacts found and fixed two defect classes:
1. Sector-blind sweep preflight: c2e_future_ok unconditionally required the
   heating file, so a power-only (broad_ranges) sweep on a machine without
   heat-demand files would silently drop futures as "MISSING essentials".
   Now sector-aware via the adapter's sector_coupled flag; sweep-planning
   and systems suites re-run green.
2. Dissertation orphaned floats: six floats (pipeline schematic, C2E and
   families tables, the event figure, both appendix tables) carried labels
   but were never referenced from text, and the sweep-scaffold figure
   references existed only inside LaTeX comments. Nine in-text references
   added; recompiled to 50 pages, 0 errors, 0 undefined references,
   0 overfull boxes.
Also re-verified from the shipped zip itself (not the workspace): byte
identity with the workspace, all modules compile, the full four-suite
battery passes, and adapter discovery survives adversarial decoys
(objective ids with '+', '++', and capitals) with zero cross-family capture.
