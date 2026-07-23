# Climate-Energy Pipeline: Methodology and Usage

Author handoff for Yassir Lahlou's MPhil thesis (Impact of Climate Change on
Future European Energy Systems). This document explains the rebuilt pipeline
that (1) replaces the delta method with a defensible capacity-factor approach
and (2) adds heating, cooling, and heat-pump-COP modification, with all results
written as organised figures, tables, and Word documents.

---

## 1. What changed and why

### 1.1 The capacity-factor method (your "no delta" requirement)

There are three families of method for getting climate-changed capacity factors
into a fixed-design dispatch model. The pipeline implements all three as a
deliberate ladder so the thesis can show how much the headline numbers depend on
the choice.

**Direct swap.** Use the C2E future capacity-factor series as-is. This is valid
because C2E (Wohland et al. 2025) is already bias-corrected against ERA5 at
source, using univariate delta quantile mapping per grid box over 1995-2015. The
weakness: the Gotske networks were built with atlite/ERA5 capacity factors, and
even a bias-corrected C2E series differs from atlite-ERA5 in the *level* of the
climatology because the two pipelines compute capacity factors differently
(GSEE/windpowerlib vs atlite). So a direct swap mixes the climate-change signal
with residual pipeline-level differences. Included because you asked to see it.

**Delta (the method being removed).** Multiply the original ERA5 capacity factor
by the future/baseline monthly mean ratio. It isolates the climate signal and
avoids pipeline bias, which is why it existed. But applied as a monthly scalar it
only shifts the mean: it cannot move variability or the tails. For a study whose
entire finding is about load shedding during low-wind extremes, a method that
cannot move the tails is a real weakness. Retained only as the naive baseline.

**Quantile Delta Mapping (QDM) - the primary method.** Cannon, Sobie and Murdock
(2015, *J. Climate* 28, 6938-6959). QDM bias-corrects the full distribution of
the projection against the historical reference while *explicitly preserving the
model-projected relative change at every quantile*, not just the mean. The
climate-impact literature (Cannon 2015; Casanueva et al. 2020; the
trend-preservation reviews) is consistent that trend-preserving distributional
methods like QDM and SDM are preferred over both plain quantile mapping (which
corrupts the climate trend) and simple delta (which ignores variability changes).

In this pipeline QDM is applied multiplicatively (appropriate for bounded
non-negative capacity factors) and per calendar month (so the seasonal cycle is
respected). For each future value we read its quantile in the C2E future
distribution, take the relative change versus the C2E baseline at that quantile,
and apply that change to the network's own ERA5 climatology at the matching
quantile. This removes pipeline bias (the historical anchor is the model's own
ERA5 capacity factor) while carrying through C2E's projected change at every
quantile.

A synthetic validation (in the methodology figures) confirms the behaviour: when
the future climate has higher variability than the baseline, QDM widens the
distribution accordingly, whereas delta leaves variability essentially unchanged.

**Recommendation:** lead with QDM, present direct as a comparison, keep delta as
the naive baseline. The spread across the three methods is itself a publishable
finding: it quantifies how sensitive the load-shedding and curtailment results
are to a methodological choice that is often made implicitly.

### 1.2 Heating, cooling, and heat-pump COP

Warming changes heating demand and heat-pump efficiency together, and they partly
offset: warmer winters cut heat load, but they *also* raise the heat-pump COP
(smaller source-sink temperature gap), so each unit of delivered heat costs less
electricity. Both reduce winter electricity stress. Modifying the load alone (the
tempting shortcut) systematically overstates residual winter stress. When2Heat
(Ruhnau et al. 2019), PyPSA-Eur's own `time_dep_hp_cop` implementation, and
REMIND-PyPSA all treat heat demand and COP jointly, so the pipeline does too.

Three layers, all toggleable in `config.py`:

- **Heating demand** (`DO_HEAT_DEMAND`): scales `loads_t.p_set` on the heat
  buses using C2E heating-demand series via the chosen method.
- **Heat-pump COP** (`DO_HEAT_PUMP_COP`): recomputes the time-varying link
  efficiency from future temperature using the Ruhnau et al. air- and
  ground-source regressions, with sink temperature 55 C (PyPSA-Eur default).
- **Cooling** (`DO_COOLING`): in Gotske, cooling is folded into a constant
  electricity demand, so there is usually no separate cooling load to scale. The
  pipeline therefore *adds* explicit cooling electricity loads per region from
  the C2E cooling series, making the new demand visible and attributable. The
  absolute MW level is approximate and flagged for calibration.

C2E ships heating- and cooling-demand CSVs (it runs demand.ninja internally), so
the preferred path keeps the demand side on exactly the same methodology as the
supply side. If you only have temperature, a documented HDD/COP fallback exists.

---

## 2. Data scenario (confirmed)

Your C2E files are CORDEX-based, SSP4.5, with baseline 2015 and future periods
2042 and 2099. This is set in `config.py` as `SCENARIO_LABEL = "CORDEX SSP4.5"`.
Still run `python -m src.inspect_c2e` once to confirm the exact column layout,
country list, and time resolution of your specific files before the first run.

## 2b. Audit of the original code (bugs found and fixed)

While rebuilding, the previous scripts were reviewed critically. The fixes are
already applied in this pipeline; they matter for the numbers you report.

1. **Curtailment was over-counted (most important).** The original
   `extract_results` computed `potential = p_max_pu * p_nom` over ALL generators
   and summed `(potential - actual)` for the headline curtailment total. For
   dispatchable carriers (gas, nuclear) `p_max_pu` defaults to 1, so every idle
   gas MW and even the load-shedding generators were counted as "curtailment."
   This inflated the headline figure. The previously reported 187/327 TWh values
   are therefore too high. Here curtailment is restricted to variable-RE carriers
   only, and a curtailment RATE (curtailed / potential) is also reported.

2. **Generation by region mixed sectors.** The original grouped all generators
   by region regardless of whether they sat on an electricity, heat, or hydrogen
   bus. Here generation is split by bus carrier, so "generation by region" is the
   electricity total, with a separate sector breakdown.

3. **Leap-year alignment.** The original aligned C2E to the network purely by
   position and only warned on length mismatch. The loader now trims/pads leap
   years explicitly so 2928-vs-2920 mismatches do not silently skip generators.

4. **Demand denominator.** Load-shedding percentage now uses served demand
   (`loads_t.p`) as the denominator, computed consistently.

5. **Missing physics (not a code bug, a scope gap).** The previous pipeline only
   modified `p_max_pu`; heat-pump COP was held at historical efficiency. The
   heating work now modifies demand AND COP together.

---

## 3. How to run

From the repo root (`C:\Users\yassi\thesis-climate-energy`), with the venv active:

```
python -m src.inspect_c2e        # verify data (do this first)
python -m src.preflight_check    # check files, packages, Gurobi, scenario label
python -m src.run_pipeline       # run all methods end to end
```

Or a single method:

```
python -m src.run_pipeline --method qdm
```

Place the `src/` folder at the repo root. Expected runtime per dispatch on your
32 GB / Gurobi machine is roughly what you saw before (about 0.5-2 hours for the
original, less for the modified networks), times the number of methods plus one
shared original solve.

---

## 4. Output structure (organised)

```
output/
  c2e_inspection.txt
  pipeline_<timestamp>.log
  run_wy<YEAR>_c2e<FUTURE>_<method>/          one folder per method
    01_generation/      genmix, weekly_stack, monthly_stack, re_utilisation
    02_load_shedding/   timeseries, monthly, by_bus, by_region, by_sector
    03_curtailment/     by_carrier, by_region, rate_by_carrier
    04_regional/        gen_by_region, region_x_technology
    05_sector_coupling/ link_energy, demand_by_sector, transmission_flows,
                        supply_demand_balance
    06_storage/         store_soc
    07_climate_signal/  cf_change_by_technology, cf_change_by_region
    networks/           modified_*.nc, dispatched_*.nc
    tables/             ~22 CSVs backing every figure (incl. region x technology,
                        load shedding by region/sector/bus, curtailment rate,
                        demand by sector/region)
    REPORT_<method>.docx   full auto-filled document (sections 1-10)
  COMPARISON_wy<YEAR>_c2e<FUTURE>/
    COMPARISON_...docx
    figures/   cmp_loadshed, cmp_curtailment, cmp_cost
    tables/    master_metrics.csv, load_shedding_by_region_by_method.csv
```

Every figure has a backing CSV so any plot can be rebuilt in your thesis style.

---

## 5. Module map

| File | Role |
|---|---|
| `config.py` | All paths and options. Edit this first. |
| `mapping.py` | Country <-> bus mapping, carrier mapping, heat-bus detection. |
| `c2e_loader.py` | Robust C2E CSV loader (wide/long auto-detect) + inspector. |
| `cf_methods.py` | The three CF methods: direct, delta, QDM. The methodological core. |
| `heat_cooling.py` | Heating demand, heat-pump COP, cooling demand modification. |
| `build_modified_network.py` | Applies supply + demand changes, writes modified `.nc`. |
| `dispatch.py` | Prepare, Gurobi barrier solve (+ tsam fallback), extract results. |
| `reporting.py` | Figures, CSV tables, per-method and comparison Word documents. |
| `run_pipeline.py` | Orchestrates everything for all methods. |
| `inspect_c2e.py` | Verify scenario/period/format before running. |
| `preflight_check.py` | Pre-run sanity checks. |
| `make_synthetic.py` | Self-test fixture only; not part of a thesis run. |

---

## 6. What to confirm with Dr Pickering

- That QDM (multiplicative, monthly) is acceptable as the primary method, with
  direct and delta as comparisons. The synthetic variability test is the
  supporting evidence.
- The cooling treatment: adding explicit cooling electricity loads from C2E
  versus leaving cooling out of scope. The absolute level needs a calibration
  decision (current default is a documented placeholder).
- Whether to scale to all 62 weather years and the 2099 horizon once the single
  case is validated (the config already supports changing period and year).
- The exact C2E scenario label, resolved by `inspect_c2e`.

---

## 7. Key references

- Cannon, Sobie, Murdock (2015). Bias Correction of GCM Precipitation by Quantile
  Mapping. *J. Climate* 28, 6938-6959. (QDM.)
- Wohland et al. (2025). Climate2Energy. arXiv:2508.09531. (C2E source data and
  its own ERA5 bias correction; demand.ninja heating/cooling.)
- Staffell, Pfenninger, Johnson (2023). A global model of hourly space heating
  and cooling demand. *Nature Energy*. (demand.ninja / BAIT.)
- Ruhnau, Hirth, Praktiknjo (2019). Time series of heat demand and heat pump
  efficiency. *Sci. Data* 6:189. (When2Heat COP regressions.)
- Zheng et al. (2025). Strategies for climate-resilient global wind and solar
  power systems. *Nature* 643, 1263-1270. (The fixed-capacity climate-dispatch
  study design this thesis follows.)
- Gotske et al. (2024). The cost-optimised PyPSA-Eur-Sec networks used as the
  designs under test.
```
