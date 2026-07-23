"""System registry: the adapter layer that lets the pipeline stress-test
MULTIPLE published design families with the same imposition math, dispatch
rules and reporting.

Design principles (v21):
- Gotske is adapter #1 and must behave BYTE-IDENTICALLY to v20: same network
  filenames, same cache keys ('wy2015'-style), same run folders. All existing
  caches, sweeps and reports remain valid.
- A system contributes ONLY what is genuinely system-specific: its filename
  grammar, its design-id vocabulary, discovery on disk, provenance metadata,
  and expectations for the onboarding checks. Everything the pipeline already
  does generically (country-from-bus-prefix, carrier->C2E channel map, heat
  load detection by name, hydro by component type, CO2 dual by constraint
  search) stays shared, with small generalisations living in mapping.py.
- The active system is chosen by the SWEEP_SYSTEM env var (default 'gotske'),
  the active design by SWEEP_DESIGN (default: 'wy{WEATHER_YEAR}' for gotske).

Verified facts encoded for neumann2023 (from the paper's own repository,
github.com/fneum/spatial-sector, configs/config.main.yaml):
  181 regions, 3-hourly, planning horizon 2050, MIT licence;
  scenario grid = lv in {1.0, opt} x sector_opts in
    {Co2L0-3H-T-H-B-I-A-solar+p3-linemaxext10,
     ...-noH2network, ...-onwind+p0, ...-noH2network-onwind+p0}.
  The SOLVED-networks Zenodo DOI is a USER VERIFY (paper's Data availability
  statement); filenames follow the PyPSA-Eur-Sec grammar
  elec_s_181_lv{lv}__{sector_opts}_2050.nc.
"""
from __future__ import annotations

import os
import re

_N23_SOPTS = "Co2L0-3H-T-H-B-I-A-solar+p3-linemaxext10"
NEUMANN2023_DESIGNS = [
    f"lv{lv}__{_N23_SOPTS}{suffix}"
    for lv in ("1.0", "opt")
    for suffix in ("", "-noH2network", "-onwind+p0", "-noH2network-onwind+p0")
]

SYSTEMS = {
    'gotske': dict(
        label="Gotske et al. 2024 (PyPSA-Eur-Sec, 37 nodes, 3-hourly, "
              "one design per weather year 1960-2021)",
        kind='weather_year',
        filename=lambda did: (f"elec_{did}_s370_37_lv1.0__"
                              f"Co2L0-3h-T-H-B-I-A-solar+p3-dist1_2050.nc"),
        discover_re=r'^elec_(wy\d{4})_s370_37_.*_2050\.nc$',
        paper="Nature Communications 15:10680 (2024)",
        data="Zenodo 10.5281/zenodo.10891263",
        turbine="SWT120_3600",
        sector_coupled=True,
        expected=dict(snapshots=2920, elec_buses=37, heat_load_buses=185,
                      co2_constraint='CO2Limit'),
    ),
    'neumann2023': dict(
        label="Neumann, Zeyen, Victoria, Brown 2023 'The potential role of a "
              "hydrogen network in Europe' (PyPSA-Eur-Sec, 181 nodes, 3-hourly)",
        kind='scenario',
        filename=lambda did: f"elec_s_181_{did}_2050.nc",
        discover_re=r'^elec_s_\d+_(lv[^_]+__[^_]+)_2050\.nc$',
        known_designs=NEUMANN2023_DESIGNS,
        paper="Joule 7:1793-1817 (2023), doi:10.1016/j.joule.2023.06.016",
        data="VERIFY: solved-networks DOI from the paper's Data availability "
             "statement (code repo: github.com/fneum/spatial-sector, MIT)",
        turbine="SWT120_3600 assumed; VERIFY against their atlite config "
                "before quoting offset-sensitive numbers",
        sector_coupled=True,
        expected=dict(snapshots=2920, co2_constraint='CO2Limit'),
        notes=("Memory: ~5x Gotske spatially; run the onboarding "
               "--probe-solve ONCE and watch memory before scheduling any "
               "sweep. Transport/industry buses may carry loads; shedding "
               "remains on all load-carrying buses as for gotske "
               "(documented convention), the onboarding census makes the "
               "scope explicit."),
    ),
    'broad_ranges': dict(
        label="Neumann & Brown 'Broad ranges of investment configurations' "
              "(PyPSA-Eur POWER-ONLY, near-optimal ensemble; 37-node/4-hourly "
              "and 128-node/2-hourly scenarios)",
        kind='scenario',
        filename=lambda did: f"elec_s_{did}.nc",
        discover_re=r'^elec_s_(\d+_ec_lcopt_\dH(?:_E[0-9.]+_O[^/]+)?)\.nc$',
        known_designs=['37_ec_lcopt_4H', '128_ec_lcopt_2H'],
        paper="Neumann & Brown, 'Broad ranges of investment configurations "
              "for renewable power systems, robust to cost uncertainty and "
              "near-optimality' (project archive on Zenodo; lineage: Neumann "
              "& Brown 2021, EPSR 190:106690)",
        data="Zenodo 10.5281/zenodo.6642651 (whole-project archive incl. a "
             "results/ tree; VERIFY it ships results/networks/*.nc before "
             "downloading blind)",
        turbine="VERIFY (their config.pypsaeur.yaml); weather year of the "
                "underlying cutout also VERIFY there",
        sector_coupled=False,
        expected=dict(co2_constraint='CO2Limit'),
        notes=("POWER-ONLY: heating and cooling channels are inert by design "
               "(sector_coupled=False); this is the sector-coupling isolation "
               "test plus an in-family near-optimal ensemble. Grammar from "
               "the paper repo's own rules (github.com/fneum/broad-ranges, "
               "rules/common.smk): optimum elec_s_{clusters}_ec_lcopt_{opts}"
               ".nc; near-optimal appends _E{epsilon}_O{objective}, e.g. "
               "37_ec_lcopt_4H_E0.06_OGenerator+wind+min (5 epsilons x 14 "
               "objectives x 2 scenarios). Their design solves use "
               "noisy_costs=true and NO load shedding (we add VOLL at "
               "dispatch, as for every system). Non-3h grid handled by the "
               "v21.1 network-derived regrid frequency."),
    ),
}


def current_name() -> str:
    return os.environ.get('SWEEP_SYSTEM', 'gotske').lower()


def get(name: str | None = None) -> dict:
    n = (name or current_name())
    if n not in SYSTEMS:
        raise KeyError(f"unknown system '{n}'; known: {sorted(SYSTEMS)}")
    return SYSTEMS[n]


def default_design_id(system: str | None = None) -> str:
    """gotske: 'wy{WEATHER_YEAR}' (keeps SWEEP_WY semantics intact);
    other systems: SWEEP_DESIGN is required."""
    n = system or current_name()
    if n == 'gotske':
        import config
        return os.environ.get('SWEEP_DESIGN', f"wy{config.WEATHER_YEAR}")
    did = os.environ.get('SWEEP_DESIGN')
    if not did:
        raise RuntimeError(f"system '{n}' needs SWEEP_DESIGN (a design id, "
                           f"e.g. one of {get(n).get('known_designs', [])[:2]} ...)")
    return did


def network_path(system: str | None = None, design_id: str | None = None) -> str:
    import config
    n = system or current_name()
    did = design_id or default_design_id(n)
    return os.path.join(config.NETWORK_DIR, get(n)['filename'](did))


def design_key(system: str | None = None, design_id: str | None = None) -> str:
    """The identity string used in cache filenames and run folders.
    gotske -> 'wy2015' (BYTE-IDENTICAL to v20, so all existing caches and
    sweep markers stay valid). Others -> '{system}--{design_id}'."""
    n = system or current_name()
    did = design_id or default_design_id(n)
    return did if n == 'gotske' else f"{n}--{did}"


def discover_designs(system: str | None = None) -> list[str]:
    """Design ids for which a network file exists in NETWORK_DIR."""
    import config
    n = system or current_name()
    rx = re.compile(get(n)['discover_re'])
    out = []
    d = config.NETWORK_DIR
    if not os.path.isdir(d):
        return out
    for f in sorted(os.listdir(d)):
        m = rx.match(f)
        if m:
            out.append(m.group(1))
    return out


def split_design_key(key: str) -> tuple[str, str]:
    """Inverse of design_key, for the collector: 'wy2015' -> ('gotske',
    'wy2015'); 'neumann2023--lv1.0__...' -> ('neumann2023', 'lv1.0__...')."""
    if '--' in key:
        sys_name, did = key.split('--', 1)
        return sys_name, did
    return 'gotske', key
