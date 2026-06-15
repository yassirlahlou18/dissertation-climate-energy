"""Prints the contents of the C2E files (columns, dates, countries). See the pipeline guide PDF."""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

import os
import config
from c2e_loader import inspect_file


def main():
    os.makedirs(config.OUTPUT_ROOT, exist_ok=True)
    out_path = os.path.join(config.OUTPUT_ROOT, "c2e_inspection.txt")
    blocks = []
    for period in sorted({config.C2E_BASELINE, config.C2E_FUTURE}):
        blocks.append(f"{'='*70}\nC2E PERIOD: {period}\n{'='*70}")
        for kind, path in {**config.c2e_supply_files(period),
                           **config.c2e_demand_files(period)}.items():
            blocks.append(f"\n--- {kind} ---")
            if os.path.exists(path):
                try:
                    blocks.append(inspect_file(path))
                except Exception as e:
                    blocks.append(f"  ERROR reading {path}: {e}")
            else:
                blocks.append(f"  (not found: {path})")
    text = "\n".join(blocks)
    print(text)
    with open(out_path, 'w') as f:
        f.write(text)
    print(f"\nWritten to {out_path}")
    print("\nACTION: confirm period/scenario, then set SCENARIO_LABEL and fix "
          "any filename mismatches in config.py before running the pipeline.")


if __name__ == '__main__':
    main()
