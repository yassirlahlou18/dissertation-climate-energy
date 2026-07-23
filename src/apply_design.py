"""Pin a solved design's capacities onto a PRISTINE template network.

Why this exists: the design workstream exports design_capacities_full.csv
(one row per component, pinned schema). To evaluate that design under OTHER
weather (the {design} x {weather} matrix), the clean route is a fresh copy
of the untouched template with the design's capacities written in, so no
already-imposed weather can be double-scaled. This tool does exactly that
join, asserts FULL bidirectional coverage (every network component has a
row, every row has a component), writes value_opt into both the nominal
attribute and its *_opt twin (so the stress pipeline's pinning step is a
consistent no-op), and never touches extendability flags, time series or
constraints.

Usage:
  python -m src.apply_design --template <pristine.nc> \
      --capacities <design_capacities_full.csv> --out <pinned.nc>
"""
from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import argparse

import numpy as np
import pandas as pd

_CLASSES = {'Generator': ('generators', 'p_nom'),
            'Link': ('links', 'p_nom'),
            'Line': ('lines', 's_nom'),
            'Store': ('stores', 'e_nom'),
            'StorageUnit': ('storage_units', 'p_nom')}


def apply_design(n, caps: pd.DataFrame, log=print):
    problems = []
    n_set = 0
    for cls, (attr, nom) in _CLASSES.items():
        df = getattr(n, attr)
        sub = caps[caps['component'] == cls]
        have, want = set(df.index), set(sub['name'])
        if have - want:
            problems.append(f"{cls}: {len(have-want)} components missing from "
                            f"CSV, e.g. {sorted(have-want)[:3]}")
        if want - have:
            problems.append(f"{cls}: {len(want-have)} CSV rows not in network, "
                            f"e.g. {sorted(want-have)[:3]}")
        bad_attr = sub[sub['attribute'] != nom]
        if len(bad_attr):
            problems.append(f"{cls}: {len(bad_attr)} rows with attribute != {nom}")
        if sub['value_opt'].isna().any():
            problems.append(f"{cls}: NaN in value_opt")
        if problems:
            continue
        vals = sub.set_index('name')['value_opt'].reindex(df.index)
        df[nom] = vals.values
        opt_col = f"{nom}_opt"
        if opt_col in df.columns:
            df[opt_col] = vals.values
        n_set += len(df)
        log(f"  {cls}: pinned {len(df)} components ({nom} and {opt_col})")
    if problems:
        for p in problems:
            log(f"  COVERAGE FAIL: {p}")
        raise RuntimeError("pin-onto-template coverage failed: "
                           + " | ".join(problems))
    log(f"  total pinned: {n_set} components; extendability flags, series and "
        f"constraints untouched")
    return n


def main():
    import pypsa
    ap = argparse.ArgumentParser()
    ap.add_argument('--template', required=True)
    ap.add_argument('--capacities', required=True)
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    n = pypsa.Network(a.template)
    caps = pd.read_csv(a.capacities)
    apply_design(n, caps, log=print)
    n.export_to_netcdf(a.out)
    print(f"pinned network -> {a.out}")


if __name__ == '__main__':
    main()
