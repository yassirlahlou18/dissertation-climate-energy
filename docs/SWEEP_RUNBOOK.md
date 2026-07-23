# SWEEP RUNBOOK: many Gotske weather years x C2E {2042, 2099} on the GCP VM

This is the operational guide for `src/sweep.py`. Read it once before the first
launch; afterwards the whole thing is one command inside tmux.

## What the sweep does

For every requested Gotske design year Y with a network file on disk, and every
usable C2E future F, it runs the full pipeline (`python -m src.run_pipeline`)
as a subprocess with `SWEEP_WY=Y SWEEP_FUTURE=F`. The two futures of one year
run SEQUENTIALLY inside one worker (they share the cached original dispatch,
computed once). DIFFERENT years run in parallel across workers. Each task
writes a JSON marker on success; rerunning the same sweep name skips finished
tasks, so interruptions cost you at most one in-flight solve.

Per-task outputs land in `output/<sweep-name>/wyY_fF/...` (full per-run report,
tables, figures, dispatched networks). Machine-readable results accumulate as
`output/_method_mixed_wyY_c2eF.pkl` plus `output/_original_wyY_results.pkl`,
which is what the collector reads.

## One-time setup checks (5 minutes)

```bash
# on the VM, from the code root (the folder containing src/), venv active
export THESIS_REPO=/home/YOUR_USER/thesis-climate-energy

# 1. the design networks you want must exist:
ls $THESIS_REPO/venv/Capacity_optimization/networks/ | grep -c elec_wy
# (62 files if you downloaded the full Gotske Zenodo set)

# 2. C2E future files must exist for 2042 and 2099:
ls $THESIS_REPO/C2E/ | grep -E "2042|2099"

# 3. Gurobi license allows CONCURRENT solves (academic named-user licenses
#    normally allow unlimited sessions on the same machine, but verify):
python -c "import gurobipy; a=gurobipy.Env(); b=gurobipy.Env(); print('2 concurrent envs OK')"
#    If this fails with a license error, run the sweep with --workers 1.

# 4. disk: each task writes ~2 dispatched networks + figures (~0.5 GB/task).
df -h $THESIS_REPO
# 20 years x 2 futures ~ 25-30 GB. Clear old output/run_* folders if tight.

# 5. dry-run the plan (nothing solves):
python -m src.sweep --years last:20 --futures 2042 2099 --workers 2 --dry-run
```

## Launching

ALWAYS inside tmux (or nohup) so an SSH drop does not kill a multi-day sweep:

```bash
tmux new -s sweep
export THESIS_REPO=/home/YOUR_USER/thesis-climate-energy
python -m src.sweep --years 2002-2021 --futures 2042 2099 \
       --workers 2 --sweep-name sweep_recent20 --collect \
       2>&1 | tee sweep_recent20.console.log
# detach: Ctrl-b then d        re-attach later: tmux attach -t sweep
```

Useful variants:
- `--years all` : every year with a network on disk (the full 62)
- `--years last:10` : the 10 most recent available design years
- `--years 2015` or `--years 1985,1996,2010` : explicit picks
- `--threads-per-worker 4` : Gurobi threads per solve (default cores//workers)
- `--timeout-hours 6` : a hung solve fails and the sweep moves on
- `--dry-run` : print the plan, solve nothing
- `--collect` : build the master CSV + figures when the sweep finishes

## Sizing and wall-time math (n2-highmem-8: 8 vCPU, 64 GB)

One dispatch took ~52 min on this VM with all 8 threads. Budget ~1 h/solve.
A weather-year chain = 1 original + 2 modified = ~3 h.

| years | tasks (solves) | workers=2 wall | workers=1 wall |
|-------|----------------|----------------|----------------|
| 10    | 30             | ~15-18 h       | ~30 h          |
| 20    | 60             | ~30-35 h       | ~60 h          |
| 62    | 186            | ~95-110 h      | ~190 h         |

Notes on parallelism:
- 2 workers x 4 Gurobi threads is the safe default. Each solve is slightly
  slower than with 8 threads, but two run at once; net ~1.7x throughput.
- MEMORY is the binding constraint, not CPU. Watch it for the first hour:
  `watch -n 30 free -g`. If available memory approaches zero or swap grows,
  kill the sweep (Ctrl-C in tmux) and relaunch with `--workers 1` -- the
  markers mean nothing already finished is redone.
- Workers start 60 s apart (`--stagger-seconds`) so their memory peaks do not
  coincide. Leave it unless you know why you are changing it.
- For the FULL 62-year sweep, consider resizing the VM for the duration:
  `gcloud compute instances stop thesis-vm`
  `gcloud compute instances set-machine-type thesis-vm --machine-type n2-highmem-16`
  then run with `--workers 4`. Stop the VM afterwards; highmem-16 is ~2x cost.

## Monitoring

```bash
tmux attach -t sweep                     # live console
tail -f $THESIS_REPO/output/sweep_logs/<sweep-name>/wy2015_f2042.log   # one task
ls $THESIS_REPO/output/sweep_status/<sweep-name>/ | wc -l              # done count
watch -n 30 free -g                      # memory headroom
```

The console prints one line per finished task:
`[14:32] wy2015 f2042: ok (61.3 min)`. Failures print at the end with the log
path; `_summary.json` in the status folder has the machine-readable outcome.

## Failures and restarts

- A failed or timed-out task does NOT stop the sweep; it is reported at the end.
- To retry failures: just rerun the SAME command with the SAME --sweep-name.
  Markers skip everything that succeeded; only failed/missing tasks rerun.
- To force a redo of one task: delete its marker
  `rm output/sweep_status/<name>/wy2015_f2042.json` and rerun.
- If the VM itself restarts: tmux session is gone, but markers survive.
  Re-launch the same command; it resumes where it stopped (at most one
  in-flight solve is lost per worker).

## Collecting results (during or after)

`--collect` runs automatically at the end, but you can harvest at ANY time,
including mid-sweep (it reads whatever result caches exist):

```bash
python -m src.collect_sweep
```

Output: `output/sweep_results_<stamp>/`, a complete analysis package:
- `report/SWEEP_REPORT.docx` -- the crafted cross-sweep report: headline
  statistics per future, robust/fragile design tables, ALL figures embedded
  with captions, regional and worst-event tables, provenance and caveats.
- `tables/` -- sweep_master.csv (one row per run, ~38 columns incl. original
  references and deltas), stats_by_future.csv, design_ranking.csv (robust ->
  fragile), shed_by_region long + per-future region x year matrices,
  shed_by_sector_long.csv, unserved_by_month_long.csv, worst_events.csv.
- `figures/` -- fig01 unserved by design year, fig02 adequacy exceedance,
  fig03 CO2 drift, fig04 shed-vs-emit plane, fig05 region x year heatmaps
  (one per future), fig06 vulnerability persistence 2042-vs-2099 scatter,
  fig07 monthly stress profile, fig08 worst-event timing, fig09 heat-vs-
  electricity composition, fig10 unserved duration curves.
- `README.md` (file index) and `SWEEP_SUMMARY.md` (key numbers in prose).
Per-run detail (full REPORT docx, channel tables, dispatched networks) stays
in each run's own folder under `output/<sweep-name>/wyYYYY_fFFFF/`.

## Getting results off the VM

```bash
# from your laptop:
gcloud compute scp --recurse thesis-vm:~/thesis-climate-energy/output/sweep_results_* .
# the full per-run reports are large; take them selectively if needed:
gcloud compute scp --recurse thesis-vm:~/thesis-climate-energy/output/<sweep-name>/wy2015_f2042 .
```

## Known caveats (so nothing surprises you)

- The per-run REPORT docx is produced for every task; that is intentional
  (thesis-quality provenance per run) but adds a few minutes per task.
- The comparison docx inside each session folder compares only that session's
  profile; the cross-year story lives in the collector outputs, not there.
- Southern-European hydro rows carry the single-year-variability caution
  automatically when the applied inflow ratio exceeds +/-50% (see each
  report's section 1b and 10).
- If a design network year is missing on disk it is SKIPPED and reported in
  the plan, not treated as an error.
