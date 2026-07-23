"""Quick checks before a real run. See the pipeline guide PDF."""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

import os
import config


def main():
    print("=" * 64); print("PRE-FLIGHT CHECK (climate-energy pipeline)"); print("=" * 64)
    errors = warns = 0

    print("\n[1] Network file")
    nf = config.network_file()
    if os.path.exists(nf):
        print(f"  OK ({os.path.getsize(nf)/1e6:.1f} MB): {nf}")
    else:
        print(f"  FAIL: not found {nf}"); errors += 1

    print("\n[2] C2E supply files")
    for period in sorted({config.C2E_BASELINE, config.C2E_FUTURE}):
        for var, p in config.c2e_supply_files(period).items():
            ok = os.path.exists(p)
            print(f"  {'OK ' if ok else 'MISS'} {period}/{var}: {os.path.basename(p)}")
            if not ok:
                errors += 1

    print("\n[3] C2E demand files (optional but needed for heat/cool)")
    for period in sorted({config.C2E_BASELINE, config.C2E_FUTURE}):
        for var, p in config.c2e_demand_files(period).items():
            ok = os.path.exists(p)
            print(f"  {'OK  ' if ok else 'note'} {period}/{var}: {os.path.basename(p)}")
            if not ok:
                warns += 1

    print("\n[4] Packages")
    for pkg in ['pypsa', 'pandas', 'numpy', 'matplotlib', 'docx']:
        try:
            m = __import__(pkg)
            print(f"  OK: {pkg} {getattr(m,'__version__','?')}")
        except ImportError:
            print(f"  FAIL: {pkg}"); errors += 1

    print("\n[5] Solver")
    if config.SOLVER == 'gurobi':
        try:
            import gurobipy
            v = gurobipy.gurobi.version()
            print(f"  OK: Gurobi {v[0]}.{v[1]}.{v[2]}")
        except Exception as e:
            print(f"  FAIL: {e}"); errors += 1
    else:
        print(f"  solver = {config.SOLVER}")

    print("\n[6] Scenario label")
    if config.SCENARIO_LABEL == 'VERIFY':
        print("  WARNING: SCENARIO_LABEL still 'VERIFY' - run inspect_c2e and set it")
        warns += 1
    else:
        print(f"  OK: {config.SCENARIO_LABEL}")

    print("\n" + "=" * 64)
    print(f"Errors: {errors} | Warnings: {warns}")
    print("READY (python -m src.run_pipeline)" if errors == 0 else "NOT READY - fix errors")


if __name__ == '__main__':
    main()
