"""Sweep orchestrator: run the pipeline across many Gotske weather years and
C2E future slices, in parallel, restartably, on one machine (laptop or VM).

Design (why it looks like this):
  * Each (weather_year, future) task is a SEPARATE SUBPROCESS of
    `python -m src.run_pipeline`, configured via env vars (SWEEP_WY,
    SWEEP_FUTURE, RUN_TAG, SWEEP_GUROBI_THREADS). config.py is global-mutable,
    so subprocesses are the only safe parallel unit.
  * The WORK UNIT IS A WEATHER YEAR: its futures run SEQUENTIALLY inside one
    worker because they share the cached original dispatch (computed once by
    the first future, reused by the rest). Different YEARS run in parallel.
  * Restartable: a JSON marker is written per completed task; on rerun,
    completed tasks are skipped. Every task logs to its own file.
  * Preflight: missing design networks or C2E future files are reported and
    those tasks excluded BEFORE anything solves.

Typical VM usage (from the code root, venv active):

    export THESIS_REPO=/home/USER/thesis-climate-energy
    python -m src.sweep --years 2002-2021 --futures 2042 2099 --workers 2 --collect

See docs/SWEEP_RUNBOOK.md for the full VM runbook.
"""
from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import os
import sys
import json
import time
import argparse
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import systems


# --------------------------------------------------------------------------
# planning helpers (pure functions, unit-tested)
# --------------------------------------------------------------------------
def parse_years(spec: str, available: list[int] | None = None) -> list[int]:
    """'2002-2021' | '2010,2015,2019' | 'last:10' | 'all' -> sorted year list.
    'last:N' and 'all' need `available` (years with an existing network)."""
    spec = spec.strip().lower()
    if spec == 'all':
        if not available:
            raise ValueError("--years all needs discoverable networks")
        return sorted(available)
    if spec.startswith('last:'):
        n = int(spec.split(':', 1)[1])
        if not available:
            raise ValueError("--years last:N needs discoverable networks")
        return sorted(available)[-n:]
    if '-' in spec and ',' not in spec:
        a, b = spec.split('-', 1)
        return list(range(int(a), int(b) + 1))
    return sorted({int(x) for x in spec.split(',') if x.strip()})


def discover_network_years() -> list[int]:
    """Years for which a Gotske design network file exists on disk."""
    import re
    d = config.NETWORK_DIR
    if not os.path.isdir(d):
        return []
    years = []
    for f in os.listdir(d):
        m = re.match(r'elec_wy(\d{4})_', f)
        if m:
            years.append(int(m.group(1)))
    return sorted(set(years))


def c2e_future_ok(period: int, system: str = 'gotske') -> tuple[bool, list[str]]:
    """A future slice is usable if its supply files exist, plus heating for
    sector-coupled systems (hydro is optional coverage everywhere; heating is
    NOT an essential for power-only families, whose heat channel is inert).
    Returns (ok, missing_essentials)."""
    missing = []
    for k, p in config.c2e_supply_files(period).items():
        if not os.path.exists(p):
            missing.append(os.path.basename(p))
    if systems.get(system).get('sector_coupled', True):
        hp = config.c2e_demand_files(period)['heating']
        if not os.path.exists(hp):
            missing.append(os.path.basename(hp))
    return (len(missing) == 0), missing


def plan(years: list[int], futures: list[int], status_dir: str
         ) -> tuple[list[tuple[int, list[int]]], list[str]]:
    """Build per-year chains of not-yet-done futures. Returns
    (chains, notes): chains = [(year, [futures_to_run...]), ...]."""
    notes = []
    chains = []
    for y in years:
        nf = config.network_file(y)
        if not os.path.exists(nf):
            notes.append(f"SKIP wy{y}: network file missing ({os.path.basename(nf)})")
            continue
        todo = []
        for f in futures:
            if os.path.exists(_marker(status_dir, y, f)):
                notes.append(f"done  wy{y} f{f}: marker present, skipping")
            else:
                todo.append(f)
        if todo:
            chains.append((y, todo))
    return chains, notes


def plan_designs(system: str, dids: list[str], futures: list[int],
                 status_dir: str) -> tuple[list[tuple[str, list[int]]], list[str]]:
    """Design-id based planning for non-gotske systems: per-design chains of
    not-yet-done futures, skipping designs whose network file is missing."""
    notes, chains = [], []
    for did in dids:
        nf = systems.network_path(system, did)
        if not os.path.exists(nf):
            notes.append(f"SKIP {did}: network file missing ({os.path.basename(nf)})")
            continue
        todo = []
        for f in futures:
            if os.path.exists(_marker_d(status_dir, did, f)):
                notes.append(f"done  {did} f{f}: marker present, skipping")
            else:
                todo.append(f)
        if todo:
            chains.append((did, todo))
    return chains, notes


def _slug(did: str) -> str:
    return did.replace('/', '_').replace('\\', '_')


def _marker_d(status_dir: str, did: str, f: int) -> str:
    return os.path.join(status_dir, f"{_slug(did)}_f{f}.json")


def _marker(status_dir: str, y: int, f: int) -> str:
    return os.path.join(status_dir, f"wy{y}_f{f}.json")


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------
def run_task(y: int, f: int, sweep_name: str, log_dir: str, status_dir: str,
             threads: int, timeout_h: float, code_root: str) -> dict:
    """One (year, future) pipeline run in a subprocess. Returns outcome dict."""
    env = dict(os.environ)
    system = getattr(run_task, '_system', 'gotske')
    if system == 'gotske':
        env['SWEEP_WY'] = str(y); did = f"wy{y}"
    else:
        env['SWEEP_SYSTEM'] = system
        env['SWEEP_DESIGN'] = str(y); did = str(y)   # y carries the design id
    env['SWEEP_FUTURE'] = str(f)
    env['SWEEP_GUROBI_THREADS'] = str(threads)
    env['RUN_TAG'] = os.path.join(sweep_name, f"{_slug(did)}_f{f}")
    logf = os.path.join(log_dir, f"{_slug(did)}_f{f}.log")
    t0 = time.time()
    out = {'weather_year': y, 'future': f, 'log': logf}
    with open(logf, 'w') as lh:
        try:
            rc = subprocess.run(
                [sys.executable, '-m', 'src.run_pipeline'],
                cwd=code_root, env=env, stdout=lh, stderr=subprocess.STDOUT,
                timeout=timeout_h * 3600).returncode
        except subprocess.TimeoutExpired:
            out.update(status='timeout', minutes=round((time.time() - t0) / 60, 1))
            return out
        except Exception as e:
            out.update(status='error', error=str(e),
                       minutes=round((time.time() - t0) / 60, 1))
            return out
    out['minutes'] = round((time.time() - t0) / 60, 1)
    # success test: exit 0 AND the per-run result cache exists (belt+braces)
    key = systems.design_key(system, did)
    cache = os.path.join(config.OUTPUT_ROOT,
                         f"_method_{getattr(config, 'RUN_PROFILE', 'mixed')}"
                         f"_{key}_c2e{f}.pkl")
    if rc == 0 and os.path.exists(cache):
        out['status'] = 'ok'
        mk = (_marker(status_dir, y, f) if system == 'gotske'
              else _marker_d(status_dir, did, f))
        with open(mk, 'w') as mh:
            json.dump(out, mh, indent=1)
    else:
        out.update(status='failed', returncode=rc, cache_present=os.path.exists(cache))
    return out


def run_chain(y: int, futures: list[int], **kw) -> list[dict]:
    """All futures of one weather year, sequentially (they share the cached
    original dispatch, so this order computes it once and reuses it)."""
    results = []
    for f in futures:
        r = run_task(y, f, **kw)
        results.append(r)
        print(f"  [{time.strftime('%H:%M')}] wy{y} f{f}: {r['status']} "
              f"({r.get('minutes', '?')} min)", flush=True)
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--system', default='gotske', choices=sorted(systems.SYSTEMS),
                    help='which published design family to sweep (default gotske)')
    ap.add_argument('--designs', default='all',
                    help="non-gotske systems: 'all' (discover on disk) or a "
                         "comma-separated list of design ids")
    ap.add_argument('--years', required=False, default=None,
                    help="'2002-2021' | '2010,2015,2019' | 'last:10' | 'all'")
    ap.add_argument('--futures', nargs='+', type=int, default=[2042, 2099])
    ap.add_argument('--workers', type=int, default=2,
                    help='parallel weather-year chains (2 is safe on an 8-vCPU VM)')
    ap.add_argument('--threads-per-worker', type=int, default=None,
                    help='Gurobi threads per solve; default = cores // workers')
    ap.add_argument('--timeout-hours', type=float, default=6.0,
                    help='per-task ceiling; a hung solve fails instead of blocking')
    ap.add_argument('--sweep-name', default=None,
                    help='output folder name under output/ (default sweep_<stamp>)')
    ap.add_argument('--stagger-seconds', type=int, default=60,
                    help='delay between worker starts to avoid simultaneous memory peaks')
    ap.add_argument('--dry-run', action='store_true', help='print the plan and exit')
    ap.add_argument('--collect', action='store_true',
                    help='run collect_sweep at the end')
    args = ap.parse_args(argv)

    code_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sweep_name = args.sweep_name or ('sweep_' + time.strftime('%Y%m%d_%H%M'))
    status_dir = os.path.join(config.OUTPUT_ROOT, 'sweep_status', sweep_name)
    log_dir = os.path.join(config.OUTPUT_ROOT, 'sweep_logs', sweep_name)
    os.makedirs(status_dir, exist_ok=True); os.makedirs(log_dir, exist_ok=True)

    futures = list(args.futures)
    if args.system == 'gotske':
        if not args.years:
            ap.error('--years is required for the gotske system')
        avail = discover_network_years()
        years = parse_years(args.years, avail)
        print(f"SWEEP '{sweep_name}' | system=gotske | repo={config.REPO}")
        print(f"  networks on disk: {len(avail)} years"
              + (f" ({avail[0]}..{avail[-1]})" if avail else ""))
    else:
        run_task._system = args.system
        avail_d = systems.discover_designs(args.system)
        if args.designs == 'all':
            dids = avail_d
        else:
            dids = [d.strip() for d in args.designs.split(',') if d.strip()]
        print(f"SWEEP '{sweep_name}' | system={args.system} | repo={config.REPO}")
        print(f"  designs on disk: {len(avail_d)}"
              + (f" e.g. {avail_d[0]}" if avail_d else " (none found!)"))
    for f in futures:
        ok, missing = c2e_future_ok(f, args.system)
        if not ok:
            print(f"  C2E {f}: MISSING essentials {missing} -> dropping this future")
    futures = [f for f in futures if c2e_future_ok(f, args.system)[0]]
    if not futures:
        print("no usable futures; aborting"); return 2

    if args.system == 'gotske':
        chains, notes = plan(years, futures, status_dir)
    else:
        chains, notes = plan_designs(args.system, dids, futures, status_dir)
    for n_ in notes:
        print("  " + n_)
    n_tasks = sum(len(fs) for _, fs in chains)
    n_orig = len(chains)
    threads = args.threads_per_worker or max(1, (os.cpu_count() or 8) // args.workers)
    print(f"  plan: {len(chains)} weather-year chains, {n_tasks} tasks "
          f"(+{n_orig} original dispatches computed inside first tasks)")
    print(f"  workers={args.workers}, gurobi threads/solve={threads}, "
          f"timeout={args.timeout_hours}h/task")
    est = (n_tasks + n_orig) * 1.0 / max(1, args.workers)
    print(f"  rough wall-time at ~1h/solve: ~{est:.0f} h "
          f"(chains: {[f'wy{y}:{fs}' for y, fs in chains][:6]}{' ...' if len(chains) > 6 else ''})")
    if args.dry_run:
        return 0
    if not chains:
        print("nothing to do (all done or nothing available)")
    else:
        kw = dict(sweep_name=sweep_name, log_dir=log_dir, status_dir=status_dir,
                  threads=threads, timeout_h=args.timeout_hours, code_root=code_root)
        results = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = []
            for i, (y, fs) in enumerate(chains):
                if i and i < args.workers and args.stagger_seconds:
                    time.sleep(args.stagger_seconds)
                futs.append(ex.submit(run_chain, y, fs, **kw))
            for fu in as_completed(futs):
                results.extend(fu.result())
        ok = [r for r in results if r['status'] == 'ok']
        bad = [r for r in results if r['status'] != 'ok']
        print(f"\nSWEEP done: {len(ok)} ok, {len(bad)} failed/timeout")
        for r in bad:
            print(f"  FAILED wy{r['weather_year']} f{r['future']} "
                  f"({r['status']}) -> {r['log']}")
        with open(os.path.join(status_dir, '_summary.json'), 'w') as sh:
            json.dump(results, sh, indent=1)
    if args.collect:
        import collect_sweep
        collect_sweep.main(['--profile', getattr(config, 'RUN_PROFILE', 'mixed')])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
