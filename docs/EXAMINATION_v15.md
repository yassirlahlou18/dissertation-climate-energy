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
