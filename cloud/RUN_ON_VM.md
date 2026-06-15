# FINAL run guide (v15) — stress-test C2E 2042 weather on the Gotske 2015 system

This is the exact, ordered procedure to run on the GCP VM. Read it once, then
follow top to bottom. Every command says WHERE it runs:
  [PC]  = PowerShell on your laptop
  [VM]  = inside the VM (prompt: yassi@thesis-vm:~$)

## What this run does (so the result is unambiguous)
- Takes the Gotske 2015 fixed-capacity system EXACTLY as built (no capacity changes).
- Applies the C2E future-2042 climate signal to the SAME weather variables Gotske
  vary: solar, onshore wind, offshore wind, hydro inflow, run-of-river, heating
  demand. Plus cooling demand (the one thesis addition beyond Gotske, IEA-anchored).
- Re-dispatches under Gotske's own CO2 treatment: the hard net-zero cap is removed
  and replaced by a CO2 PRICE = the design-year CO2 shadow price (their documented
  method: config custom_co2_price=False, add_co2_lim=False).
- Reports unserved energy + CO2 emissions (Gotske's two headline criteria) plus a
  deep reliability/curtailment/generation/storage breakdown.
- Three CF methods: qdm (primary), direct (Gotske-style raw swap), delta (naive
  baseline). They differ ONLY in how the C2E climate signal is mapped onto the
  network's ERA5 capacity factors; everything else is identical.

---

## STEP 1 [PC] — upload the v12 code
Download + unzip thesis_pipeline_v12.zip. Then upload its src (adjust the path to
where you unzipped it):
```
gcloud compute scp --recurse C:\Users\yassi\Downloads\deliverable\src\* thesis-vm:/home/yassi/thesis-climate-energy/src/ --zone=europe-west2-a
```

## STEP 2 [PC] — get into the VM
```
gcloud compute ssh thesis-vm --zone=europe-west2-a
```

## STEP 3 [VM] — activate environment
```
cd ~/thesis-climate-energy
source venv/bin/activate
export THESIS_REPO=$HOME/thesis-climate-energy
```
(You should see (venv) at the start of the prompt.)

## STEP 4 [VM] — audit the real data (no solve, ~1 min)
```
python -m src.audit_real_data
```
Confirm: heating ratio ~1-2 (thermal, clean); hydro present + mapped; supply CF
generators mapped; CO2Limit present; snapshot alignment MATCH. (All of these were
already confirmed in the last audit; this is a sanity re-check after the upload.)

## STEP 5 [VM] — clear old caches. MANDATORY for v15: every cached result and
## the cached original dispatch were produced by older code (old freeze, old
## shedding, old emissions metric) and are STALE. The comparison flags stale
## rows (stale_vs_current_code in master_metrics.csv) but do not rely on that.
```
rm -f output/_original_wy2015_results.pkl output/_co2_price_wy2015.pkl output/_dispatched_original_wy2015.nc output/_method_*.pkl
```

## STEP 6 [VM] — run the PRIMARY method (qdm). This also runs the validation baseline.
```
nohup python -m src.run_pipeline --method qdm > run_qdm.out 2>&1 &
tail -f run_qdm.out
```
Ctrl-C stops WATCHING (the run keeps going; safe to disconnect/close laptop).
It is done when run_qdm.out shows "PIPELINE done." (~1-2 h: one cap-extraction
solve + the qdm dispatch). Re-check progress later with:
```
tail -n 40 ~/thesis-climate-energy/run_qdm.out
```

## STEP 7 [VM] — THE VALIDATION GATE (do this before trusting anything)
```
python -c "import pickle; r=pickle.load(open('output/_original_wy2015_results.pkl','rb')); print('pipeline_version:', r.get('pipeline_version')); print('CO2 price EUR/t:', r['co2_price_EUR_per_t']); print('ORIGINAL unserved electricity TWh (Gotske basis):', r.get('gotske_unserved_elec_TWh')); print('ORIGINAL unserved heat TWh:', r.get('unserved_heat_TWh')); print('ORIGINAL adequacy % (Gotske basis):', r.get('gotske_resource_adequacy_pct')); print('ORIGINAL net emissions Mt:', r.get('co2_emissions_Mt')); print('  store cross-check Mt:', r.get('co2_emissions_storecheck_Mt'))"

# v15 PASS criteria:
#   pipeline_version v15; CO2 price ~468 (read from the design file, not 664);
#   ORIGINAL adequacy on the Gotske basis ~99.9 percent or better;
#   ORIGINAL net emissions NEAR ZERO (their paper: about -0.5 percent of 1990,
#   i.e. roughly -20 to -25 Mt; hundreds of Mt either way means something is wrong);
#   the store cross-check matching net emissions to within rounding.
```
INTERPRET:
- ORIGINAL resource adequacy ~99.9% (unserved ~0 TWh) AND emissions near net-zero
  => your pipeline faithfully reproduces the Gotske 2015 system on its own weather.
  The CO2-price mechanism is correct. PROCEED to step 8.
- If ORIGINAL unserved is large OR emissions are far from ~0 => setup mismatch.
  STOP, send me these numbers; do not trust the 2042 result yet.

## STEP 8 [VM] — run the other two methods (original is cached, not re-solved)
```
nohup python -m src.run_pipeline --method direct > run_direct.out 2>&1 &
```
wait for "PIPELINE done." in run_direct.out, then:
```
nohup python -m src.run_pipeline --method delta > run_delta.out 2>&1 &
```
Each reuses the cached original + CO2 price, so only the modified dispatch runs.
The cross-method COMPARISON document rebuilds after each and includes every method
run so far.

## STEP 9 [PC] — pull all results back to your laptop
```
gcloud compute scp --recurse thesis-vm:/home/yassi/thesis-climate-energy/output C:\Users\yassi\thesis-climate-energy\results_final --zone=europe-west2-a
```

## STEP 10 [PC] — stop the VM (so it stops billing)
```
gcloud compute instances stop thesis-vm --zone=europe-west2-a
```

---

## What you get (per method, under output/run_<timestamp>/wy2015_c2e2042_<method>/)
- REPORT_<method>.docx — narrative report with all figures + tables.
- 01_generation/ .. 08_reliability_deep/ — figures (PNG).
- tables/ — ~28 CSVs (machine-readable for your own deep analysis), including:
    deep_reliability_metrics.csv  (adequacy %, unserved %, peak unserved MW,
        hours with shortfall, #events, max/mean event duration, net emissions Mt,
        emissions % of 1990 if configured, backup TWh, CO2 price)
    unserved_energy_timeseries_MW.csv, unserved_energy_cumulative_TWh.csv,
    unserved_by_month_GWh.csv, emissions_by_technology_Mt.csv,
    emissions_by_month_Mt.csv, backup_generation_timeseries_MW.csv,
    gen_by_carrier/region/region_carrier.csv, curtailment_by_carrier/region.csv,
    cf_change_detail.csv (the climate signal per generator), store_soc.csv, ...
- networks/ — the solved modified network (.nc) for any further analysis.
Plus output/run_<timestamp>/COMPARISON_wy2015_c2e2042/ — cross-method master_metrics.csv
and comparison document.

## Notes on faithfulness (for the methods chapter)
- Dispatch setup = Gotske exactly (fixed capacities; hard CO2 cap removed; CO2
  price = design-year shadow price; unserved energy + emissions as metrics).
- Weather variables varied = Gotske's set (solar, onshore/offshore wind, hydro
  inflow, run-of-river, heating). COP kept at original values (no C2E temperature
  file; conservative, documented). Cooling added (thesis extension; IEA-anchored
  3% baseline share; C2E supplies the shape + relative increase).
- The three CF methods are the thesis contribution for bridging C2E (climate-model
  pipeline) onto the network's ERA5 climatology: direct = raw swap (Gotske-style),
  delta = monthly change factor (naive), qdm = Quantile Delta Mapping (Cannon et
  al. 2015; primary; preserves the projected change in variability and extremes,
  and keeps the network's 2015 weather chronology so the climate signal is
  isolated rather than confounded with one random 2042 realisation).
- To run the 2099 horizon later: set C2E_FUTURE = 2099 in src/config.py and repeat
  steps 5-9 (clear caches first).
- To see the CO2-cap artefact for contrast: set CO2_DISPATCH_MODE = 'hard_cap'.
