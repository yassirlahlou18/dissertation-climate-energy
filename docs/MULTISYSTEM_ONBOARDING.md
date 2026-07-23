# Onboarding additional systems (user actions)

## Adapter roster (v21.1)

| system id | family | status |
|---|---|---|
| gotske | Gotske et al. 2024, 37n/3h sector-coupled, weather-year vintages | operational (primary) |
| neumann2023 | Neumann et al. 2023 hydrogen-network study, 181n/3h | operational; data DOI = user VERIFY (below) |
| broad_ranges | Neumann & Brown broad-ranges, POWER-ONLY, 37n/4H + 128n/2H, near-optimal ensemble | operational; Zenodo 10.5281/zenodo.6642651, VERIFY it ships results/networks/*.nc |

Documented, no adapter yet (data verification pending): van Greevenbroek et
al. 2025 'Little to lose' (pathway designs on weather years 1987 and 2020;
Joule 9:101974) and Victoria et al. 2022 (transition pathways).

## broad_ranges quick path

1. Open Zenodo record 10.5281/zenodo.6642651 and check for
   results/networks/*.nc. If present, download the two optima first:
   elec_s_37_ec_lcopt_4H.nc and elec_s_128_ec_lcopt_2H.nc (near-optimal
   files add _E{eps}_O{objective}; a curated subset of ~10 spans the
   ensemble).
2. Place next to the other networks; then:
   python -m src.onboard_system --system broad_ranges --design 37_ec_lcopt_4H
   (power-only: the report will note heat/cooling channels inert; that is
   correct, not an error). Then --probe-solve; the 37-node/4-hourly optimum
   is the SMALLEST network in the whole project and also serves as a fast
   pipeline shakedown.
3. Sweep: python -m src.sweep --system broad_ranges --designs all
   --futures 2042 2099 --workers 2 --sweep-name broad_full --collect

# Onboarding a second system: neumann2023 (user actions)

The pipeline (v21) can now sweep multiple published PyPSA design families.
Adapter #2 is the Neumann et al. 2023 hydrogen-network study (Joule
7:1793-1817): PyPSA-Eur-Sec, 181 regions, 3-hourly, net-zero, 8 designs
spanning transmission philosophies (electricity grid x hydrogen network x
onshore-wind restriction). This file is what YOU do; the code is ready.

## 1. Find and download the SOLVED networks (15 min)

- Open the paper (doi:10.1016/j.joule.2023.06.016) and read its
  "Data and code availability" statement: it names the Zenodo record with
  the RESULT networks. (The code repo is github.com/fneum/spatial-sector,
  MIT; do NOT confuse the PyPSA-Eur per-release "pre-built networks" on
  Zenodo, which are unsolved inputs.)
- From that record, download the solved networks matching:
      elec_s_181_lv{1.0|opt}__Co2L0-3H-T-H-B-I-A-solar+p3-linemaxext10*.nc
  Start with TWO for the headline contrast:
      lv1.0__...-linemaxext10            (no grid expansion, with H2 network)
      lv1.0__...-linemaxext10-noH2network
  (lvopt pair second; onwind+p0 variants are sensitivities.)
- If the record also carries LOWER-RESOLUTION solved networks, grab one:
  it is the memory fallback.

## 2. Place them

Put the .nc files in the SAME folder as the Gotske networks:
    <repo>/venv/Capacity_optimization/networks/
Discovery separates the families automatically.

## 3. Onboard (mandatory, minutes, nothing solves)

    export THESIS_REPO=...   # as usual
    python -m src.onboard_system --system neumann2023 \
        --design "lv1.0__Co2L0-3H-T-H-B-I-A-solar+p3-linemaxext10"

Read the report it prints/writes. It must end READY. Pay attention to:
- the load-bus census (this IS the shedding scope; transport/industry buses
  carrying loads will be listed so the convention is explicit);
- VRE mapping coverage (any unmatched carriers are listed by name);
- the memory estimate. If it warns >64 GB: use the lower-resolution deposit,
  or resize the VM (n2-highmem-16/32) for these runs only.

## 4. Probe (once, on the VM, watch RAM)

    python -m src.onboard_system --system neumann2023 --design "..." --probe-solve

One reference dispatch: gives true wall time + peak memory, and checks the
design serves its own weather (~0 unserved). Watch `free -g` alongside.

## 5. Sweep it

    python -m src.sweep --system neumann2023 --designs all \
        --futures 2042 2099 --workers 1 --sweep-name neumann_full --collect

(workers 1 until the probe proves two fit in memory; 8 designs x 2 futures
+ 8 references = 24 solves.) Collection lands in the same package with
system/design columns and per-design figures.

## VERIFY list carried from the analysis (unchanged)

- [ ] Solved-networks DOI from the paper (step 1) + licence of the record.
- [ ] Turbine class in their atlite config (affects the direct-channel
      offset; SWT120_3600 assumed until checked).
- [ ] van Greevenbroek et al. 2025 deposit (Tier 2 ensemble) existence.
- [ ] Victoria 2022: 2050 network at genuine full-year resolution (Tier 3).
