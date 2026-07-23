# WHAT TO RUN AND WHERE TO PUT IT

## 1. Where the files go

Your thesis repo is `C:\Users\yassi\thesis-climate-energy`. Copy the `src`
folder from this package so it sits directly at the repo root:

```
C:\Users\yassi\thesis-climate-energy\
  src\                      <-- copy the whole src\ folder here
    config.py
    mapping.py
    c2e_loader.py
    cf_methods.py
    heat_cooling.py
    build_modified_network.py
    dispatch.py
    reporting.py
    run_pipeline.py
    inspect_c2e.py
    preflight_check.py
  venv\                     (already there)
  C2E\                      (already there - your C2E csv files)
  output\                   (created automatically)
```

The file `_make_synthetic_selftest.py` is NOT needed on your machine. It only
builds fake data so the pipeline could be tested. You can ignore or delete it.

Do not move your `venv\Capacity_optimization\networks\...nc` files or your
`C2E\...csv` files; the config already points at them.

## 2. One-time setup: install the two new packages

Activate your venv, then:

```
.\venv\Scripts\Activate
pip install python-docx
```

(PyPSA, pandas, numpy, matplotlib, gurobipy you already have. `python-docx`
generates the Word reports; `tsam` is only needed if the full-year solve falls
back to segmentation, and you already used it before.)

## 3. Check config.py once

Open `src\config.py` and confirm:
- `WEATHER_YEAR = 2015`  (the Gotske .nc you are testing)
- `C2E_BASELINE = 2015`, `C2E_FUTURE = 2042`  (change FUTURE to 2099 later)
- `METHODS = ['qdm', 'direct', 'delta']`
- The demand filenames in `c2e_demand_files()` match your actual C2E heating /
  cooling / temperature CSV names. If you do not have those demand files yet,
  set `DO_HEAT_DEMAND`, `DO_HEAT_PUMP_COP`, `DO_COOLING` to `False` for now and
  the supply-only run still works.

## 4. Run these commands, in order, from the repo root

```
python -m src.inspect_c2e        # 1. prints what your C2E files contain
python -m src.preflight_check    # 2. checks files, packages, Gurobi licence
python -m src.run_pipeline       # 3. runs everything (all three methods)
```

Both invocation styles now work (the modules carry a path shim):
`python -m src.run_pipeline` from the repo root, or `cd src; python run_pipeline.py`.

### Your C2E filenames are already configured

Confirmed for your files:
- Supply: `PV_<period>.csv`, `Wind-power_<period>_E-126_7580_onshore_True_density_corrected.csv`, `..._False_...csv`
- Heating: `heating-demand_<period>_fully-electrified.csv` (the fully-electrified
  variant is used because the Gotske networks model a fully sector-coupled
  net-zero 2050 system; the currently-electrified `heating-demand_<period>.csv`
  is the alternative if your supervisor prefers it)
- Cooling: `cooling-demand_<period>.csv`
- Temperature: NOT present in C2E. See the COP note below.

These are set in `src/config.py` -> `c2e_demand_files()`. No edit needed unless
your supervisor wants the currently-electrified heating instead.

### Heat-pump COP (no temperature file)

C2E provides no temperature series, so heat-pump COP cannot be recomputed from
measured temperature. `config.py` sets `HEAT_COP_MODE = 'keep'`: COP stays at the
network's original ERA5-based values. This is the conservative, defensible
default - it slightly understates winter heat-electricity relief, so if anything
it biases toward MORE load shedding, never inventing a spurious improvement. An
optional `'proxy'` mode reconstructs an effective temperature from the heating-
demand change and applies the Ruhnau COP curve; use it only if your supervisor
wants COP included, and flag it as an approximation. The "no temperature file"
message in the log is expected and harmless.

### Cooling demand (literature-anchored)

C2E gives cooling as a relative change (>100% increase) in units that do not map
onto the network's demand level, so the absolute level must be anchored. The
pipeline anchors the BASELINE cooling level to a citable share of each region's
annual electricity demand (`COOLING_BASELINE_SHARE`, default 0.03 = 3%, a
present-day-Europe figure from IEA), then applies C2E's relative increase and
seasonal shape on top. This follows NREL's ReEDS principle of adjusting an
existing baseline and reporting results in comparison rather than as absolute.
The result is realistic MW that cannot explode regardless of the C2E file units.
Vary `COOLING_BASELINE_SHARE` (e.g. 0.01 / 0.03 / 0.05) as a sensitivity.

### Supply vs demand methods (important design point)

The three methods (qdm / direct / delta) apply to the SUPPLY side (capacity
factors), where the ERA5-vs-C2E pipeline-bias question lives and which is your
methodological contribution. The DEMAND side (heating + cooling) uses ONE
consistent method in every run (`DEMAND_METHOD`, default 'monthly' relative
change), so that every run is a coherent climate state: future supply AND future
demand together. This matches how C2E and the JRC PESETA framework operate -
they impose one consistent future climate across supply and demand, never future
supply with historical demand. As a result, differences in the headline numbers
across the three runs are attributable purely to the supply-side method, cleanly
isolated. Heating and cooling are unit-agnostic (only the C2E future/baseline
ratio is used), so your absolute GW-scale C2E demand files work directly. Both
heating and cooling are robust to the long all-zero stretches (e.g. winter
cooling) via guarded ratios.

Step 3 will:
- dispatch the ORIGINAL network once (shared reference),
- for each method: build the climate-modified network, dispatch it, and write a
  full set of figures, tables, and a Word report,
- write a cross-method comparison document.

To run a single method while testing:

```
python -m src.run_pipeline --method qdm
```

## 5. Where the results appear

Everything lands under `output\`. Start with:
- `output\COMPARISON_wy2015_c2e2042\COMPARISON_wy2015_c2e2042.docx`
  (the headline cross-method story), then
- `output\run_wy2015_c2e2042_qdm\REPORT_qdm.docx`
  (the full detailed results for the primary method).

Each method folder has the seven numbered analysis subfolders plus `tables\`
with a CSV behind every figure. See METHODOLOGY_AND_USAGE.md section 4.

## 6. Expected runtime

Roughly your previous per-dispatch time (about 0.5-2 hours for the original on
your 32 GB Gurobi machine), times four solves (one original + three methods).
The modified solves are usually faster than the original. If memory is tight,
the full-year solve falls back to tsam segmentation automatically.

## 7. To extend later (already supported)

- 2099 horizon: set `C2E_FUTURE = 2099` in config.py and re-run.
- Other weather years: change `WEATHER_YEAR` (and the matching .nc must exist).
- Turn demand layers on/off with the `DO_*` flags in config.py.
