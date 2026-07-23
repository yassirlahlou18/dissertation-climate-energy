"""Single configuration file for the pipeline. See the pipeline guide PDF."""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))


import os

# ---- run identity ----------------------------------------------------------
# SWEEP_WY / SWEEP_FUTURE are set by src/sweep.py for each subprocess so many
# (weather year, future) tasks can run in parallel without touching this file.
WEATHER_YEAR = int(os.environ.get("SWEEP_WY", 2015))
# v21 multi-system: which published design family this run belongs to, and
# which design within it. Defaults preserve v20 behaviour exactly (gotske,
# design id 'wy{WEATHER_YEAR}'). See src/systems.py for the registry.
SYSTEM = os.environ.get("SWEEP_SYSTEM", "gotske").lower()
C2E_BASELINE = 2015          # historical/reference C2E period (label as in your files)
C2E_FUTURE = int(os.environ.get("SWEEP_FUTURE", 2042))   # future C2E period
# Scenario label: CORDEX SSP4.5, per the project description and the supervisor
# (the authoritative provenance for THESE files). Note the distinction: the
# PUBLISHED C2E reference application (Wohland et al. 2025, arXiv:2508.09531)
# uses dedicated CESM2 SSP3-7.0 runs over 1995-2015 / 2080-2100; these files
# (2015/2042/2099 single-year slices, transient 2015-2100) are a DIFFERENT
# production of the same C2E conversion framework driven by CORDEX SSP4.5
# simulations. Cite the paper for the conversion METHODOLOGY, not for these
# files' climate signals. Each year file remains a SINGLE YEAR of one
# realization, so future/baseline ratios mix forced signal with internal
# variability regardless of the driving model. TODO: record the exact GCM-RCM
# chain and realization from the supervisor for the dissertation data section.
SCENARIO_LABEL = "CORDEX SSP4.5"

# Methods to run. The pipeline runs each requested method end-to-end.
#   'direct' = use bias-corrected C2E future CF as-is (you asked to see this)
#   (legacy 'delta' removed in v16)
#   'qdm'    = Quantile Delta Mapping (PRIMARY, defensible method)
# NOTE: these methods apply to the SUPPLY side (capacity factors), where the
# ERA5-vs-C2E pipeline-bias question lives and is your methodological contribution.
# v16: TWO methods, two coherent worlds.
#   qdm    : the network keeps its own weather-year chronology; every variable
#            is reshaped quantile by quantile by the C2E future/baseline change
#            (climate signal isolated; Cannon et al. 2015).
#   direct : the system experiences the C2E future year wholesale (raw CF
#            substitution; demand and hydro as level-anchored shape transplants)
#            which is Gotske's own weather-year stress philosophy extended to a
#            future year, in the C2E modelled world.
# The legacy monthly 'delta' method is REMOVED (superseded by qdm; the
# validation figure is archived in docs/qdm_vs_delta_validation.png).
METHODS = ['qdm', 'direct']

# ---------------------------------------------------------------------------
# PER-CHANNEL METHOD ASSIGNMENT  (the production configuration)
# ---------------------------------------------------------------------------
# Final decision: use DIRECT for everything EXCEPT heating and hydro inflow,
# which use QDM. Rationale:
#   - direct (raw C2E future) keeps the C2E world internally coherent on the
#     supply side (wind, solar, run-of-river, cooling): the level offset and the
#     within-year coincidence of extremes are the fuller stress we want to see.
#   - QDM is used for the two channels where DIRECT would transplant a C2E SHAPE
#     onto Gotske infrastructure sized for a different shape, importing a
#     dataset-definition artifact rather than a climate signal:
#       * heating demand  : C2E's heating profile differs from the network's own;
#                           QDM keeps the network profile and applies only the
#                           C2E future/baseline change.
#       * hydro inflow    : C2E inflow is river-discharge based, the network's is
#                           runoff based, so the seasonal timing differs even for
#                           the same past year; QDM keeps the network's timing and
#                           conserves the annual volume (cyclic reservoirs).
# run-of-river stays DIRECT (bounded CF, no reservoir volume constraint, timing
# mismatch harmless). Heat-pump COP follows the heating channel.
#
# Channel keys: 'supply' (wind/solar), 'ror', 'hydro_inflow', 'heating',
# 'cooling', 'cop'. Values must be 'direct' or 'qdm'.
CHANNEL_METHODS = {
    'supply':       'direct',
    'ror':          'direct',
    'hydro_inflow': 'qdm',
    'heating':      'qdm',
    'cooling':      'direct',
    'cop':          'qdm',      # follows heating
}
# Label used for this mixed run's output folder and reports.
RUN_PROFILE = 'mixed'

# Hydro-inflow change-factor mode (qdm channel only):
#   'seasonal' (default): smoothed day-of-year factor from the C2E baseline/
#       future climatologies, with dry-season flooring and ANNUAL PresRAT
#       conservation. Keeps the full C2E signal, closest to the data as given.
#   'annual': a single scalar per country (the C2E annual future/baseline
#       ratio). Uses ONLY the annual information, which is the timescale the
#       C2E authors validate (~6% error) while explicitly cautioning against
#       sub-annual CESM2 hydrology (Wohland et al. 2025, SI A.3). Offered as a
#       documented sensitivity; both modes conserve the annual volume.
HYDRO_INFLOW_FACTOR_MODE = 'seasonal' 
# When True, run_pipeline runs the single mixed-profile production run above.
# When False, it falls back to the legacy behaviour of running each entry of
# METHODS as a pure single-method run (useful for the qdm-vs-direct appendix).
USE_CHANNEL_METHODS = True

# DEMAND method (heating + cooling). Applied CONSISTENTLY in every run, so each
# run is a coherent climate state (future supply AND future demand together) -
# as in C2E and the JRC PESETA framework. Demand is not where the pipeline-bias
# question lives, so it does not vary by supply method. 'monthly' applies the
# C2E monthly relative change (preserves the seasonal warming signal: the
# -10/-50% heating and >100% cooling pattern C2E reports). Options: 'monthly'
# (recommended) | 'qdm' (quantile relative change) | 'hourly' (raw fut/base ratio).
# DEMAND_METHOD retired in v16: heating granularity is tied to the method
# (qdm -> daily QDM with a 3-month centred window; direct -> shape transplant).

# Demand-side scope toggles
DO_HEAT_DEMAND = True        # scale heat loads (loads_t.p_set)
DO_HEAT_PUMP_COP = True      # recompute time-varying COP (see HEAT_COP_MODE)
DO_COOLING = True            # add explicit cooling electricity loads from C2E

# Supply-side scope toggle for hydropower (Gotske vary hydro inflow; we have the
# files). Scales storage_units_t.inflow (reservoir/pumped) and run-of-river CF.
DO_HYDRO = True

# Cooling baseline anchor: cooling is currently a small share of European
# electricity demand (IEA). C2E gives the SHAPE and RELATIVE change; we anchor
# the absolute baseline level to this citable share of each region's annual
# electricity demand, then apply C2E's relative increase. Vary for sensitivity
# (e.g. 0.01, 0.03, 0.05). 0.03 (3%) is a reasonable present-day Europe default.
# v16 cooling redesign: the IEA 3% anchor is RETIRED as a mechanism. C2E's
# baseline cooling (demand.ninja, calibrated on observed demand-temperature
# response) is the estimate of the cooling already embedded in the historical
# loads; we subtract its climatology and add the future cooling per method.
# The 3% figure survives only as an order-of-magnitude cross-check in the audit.
COOLING_BASELINE_SHARE_CROSSCHECK = 0.03

# Heat-pump COP handling. C2E provides NO temperature file, so measured-T COP is
# unavailable. Options:
#   'keep'    : leave COP at the network's original ERA5-based values. Conservative
#               for a reliability study (winter heat-electricity relief slightly
#               understated -> if anything biases toward MORE load shedding).
#               No spurious signal introduced. RECOMMENDED default.
#   'proxy'   : reconstruct a country temperature proxy by inverting the BAIT/HDD
#               relation implied by the C2E heating-demand change, then apply the
#               Ruhnau COP curve. More complete but adds an assumption layer.
# If DO_HEAT_PUMP_COP is True but a temperature file is missing, the pipeline
# uses HEAT_COP_MODE automatically.
HEAT_COP_MODE = 'keep'

SNAPSHOT_FREQ = '3h'
# C2E wind turbine selection, matched to PyPSA-Eur-Sec v0.6.0 assumptions by
# SPECIFIC POWER (the main driver of capacity-factor level):
#   PyPSA-Eur onwind default  Vestas V112 3.0MW : ~305 W/m2
#   PyPSA-Eur offwind default NREL 5MW reference: ~401 W/m2
#   C2E options: SWT120_3600 ~318 W/m2 | SWT142_3150 ~199 | E-126_7580 ~600
# SWT120_3600 is the closest match for BOTH on- and offshore among the three
# (318 vs 305 onshore; 318 vs 401 offshore, the least-bad option). Matters most
# for the direct method (level transfer); largely cancels in QDM ratios.
WIND_TURBINE = "SWT120_3600"
HEAT_PUMP_SINK_T = 55.0      # deg C, PyPSA-Eur default

# ---- paths ------------------------------------------------------------------
# REPO can be overridden with the THESIS_REPO environment variable so the SAME
# config works on Windows (laptop) and Linux (GCP VM) with no edits:
#   Linux/VM:  export THESIS_REPO=/home/USER/thesis-climate-energy
#   Windows:   (leave unset; uses the default below)
REPO = os.environ.get("THESIS_REPO", r"C:\Users\yassi\thesis-climate-energy")
# os.path.join uses the running OS's separator, so build subpaths portably:
NETWORK_DIR = os.path.join(REPO, "venv", "Capacity_optimization", "networks")
C2E_DIR = os.path.join(REPO, "C2E")
OUTPUT_ROOT = os.path.join(REPO, "output")

def network_file(weather_year=WEATHER_YEAR):
    return os.path.join(
        NETWORK_DIR,
        f"elec_wy{weather_year}_s370_37_lv1.0__Co2L0-3h-T-H-B-I-A-solar+p3-dist1_2050.nc")

# C2E file paths. EDIT these to match your real filenames after inspection.

def design_id():
    """The active design within the active SYSTEM. gotske: 'wy{WEATHER_YEAR}'
    (SWEEP_WY keeps working); other systems: from SWEEP_DESIGN."""
    import systems as _sys
    return _sys.default_design_id(SYSTEM)


def design_key():
    """Identity string for cache filenames and run folders. gotske ->
    'wy2015' (byte-identical to v20); others -> '{system}--{design_id}'."""
    import systems as _sys
    return _sys.design_key(SYSTEM, design_id())


def active_network_file():
    """System-aware network path. gotske -> network_file() unchanged."""
    if SYSTEM == 'gotske':
        return network_file()
    import systems as _sys
    return _sys.network_path(SYSTEM, design_id())


def c2e_supply_files(period):
    return {
        'pv':             os.path.join(C2E_DIR, f"PV_{period}.csv"),
        'wind_onshore':   os.path.join(C2E_DIR, f"Wind-power_{period}_{WIND_TURBINE}_onshore_True_density_corrected.csv"),
        'wind_offshore':  os.path.join(C2E_DIR, f"Wind-power_{period}_{WIND_TURBINE}_onshore_False_density_corrected.csv"),
    }

def c2e_demand_files(period):
    # Confirmed file names (lowercase, 2042 example):
    #   cooling-demand_2042.csv
    #   heating-demand_2042.csv                      (currently-electrified share)
    #   heating-demand_2042_fully-electrified.csv    (full electrification)
    # The Gotske networks model a fully sector-coupled net-zero 2050 system, so
    # the fully-electrified heating variant is the correct match.
    # There is NO temperature file in C2E -> heat-pump COP cannot use measured
    # temperature; see HEAT_COP_MODE below.
    return {
        'heating':     os.path.join(C2E_DIR, f"heating-demand_{period}_fully-electrified.csv"),
        'cooling':     os.path.join(C2E_DIR, f"cooling-demand_{period}.csv"),
        'temperature': os.path.join(C2E_DIR, f"temperature_{period}.csv"),  # may not exist
    }

def c2e_hydro_files(period):
    # Hydropower is weather-dependent and Gotske vary it. Two series:
    #   hydro_inflow_{period}.csv : reservoir/pumped-hydro inflow (energy, MW)
    #                               -> scales storage_units_t.inflow
    #   hydro_ror_{period}.csv    : run-of-river availability (capacity factor)
    #                               -> scales run-of-river generator p_max_pu
    return {
        'inflow': os.path.join(C2E_DIR, f"hydro_inflow_{period}.csv"),
        'ror':    os.path.join(C2E_DIR, f"hydro_ror_{period}.csv"),
    }

# ---- CO2 treatment in dispatch (Gotske et al. 2024 method) -----------------
# Gotske's documented approach (their config.yaml: custom_co2_price=False,
# add_co2_lim=False): for the fixed-capacity dispatch, REMOVE the hard net-zero
# CO2 cap and instead apply a CO2 PRICE equal to the Lagrange multiplier (shadow
# price) of the CO2 constraint from the design-year capacity optimization. This
# lets backup generation activate under stress (priced), so unserved energy and
# emissions become the meaningful metrics. A hard cap instead drives the shadow
# price to absurd values and suppresses shedding (the artefact we observed).
#   'gotske_price'  : remove cap, apply design-year shadow price as CO2 tax (DEFAULT)
#   'hard_cap'      : keep the original hard net-zero cap (produces the artefact)
CO2_DISPATCH_MODE = 'gotske_price'
CO2_PRICE_FALLBACK = None   # EUR/tCO2 if shadow price unreadable; None -> error


# ---- solver (Gurobi, Bryn-confirmed pypsa-eur defaults) --------------------
SOLVER = 'gurobi'
GUROBI_OPTS = dict(
    Method=2, Crossover=0, Threads=0, Seed=123,
    AggFill=0, PreDual=0, GURO_PAR_BARDENSETHRESH=200,
    # Robustness for this large, numerically hard sector-coupled LP:
    #  BarIterLimit: well above Gurobi's default 1000 (the barrier was hitting
    #    that limit before converging and reporting failure);
    #  BarHomogeneous=1: the homogeneous self-dual barrier, Gurobi's recommended
    #    setting for models that struggle to converge / are near-degenerate;
    #  NumericFocus=2: more careful numerics (the load-shedding slacks span a
    #    wide coefficient range);
    #  BarConvTol=1e-5: relative complementarity tolerance - 1e-5 is plenty
    #    accurate for these energy quantities and converges far more reliably
    #    than 1e-6 on this problem.
    BarIterLimit=10000, BarHomogeneous=1, NumericFocus=2, BarConvTol=1e-5,
)
# Parallel sweeps set SWEEP_GUROBI_THREADS so N workers share the cores
# (e.g. 2 workers x 4 threads on an 8-vCPU VM) instead of each taking all 8.
GUROBI_OPTS['Threads'] = int(os.environ.get('SWEEP_GUROBI_THREADS', GUROBI_OPTS.get('Threads', 0)))
NUM_SEGMENTS = 1460          # tsam fallback

# Load shedding: Gotske's exact values (update_network.py add_load_shedding):
# marginal_cost 1e5 EUR/MWh (intersection of macroeconomic and survey-based
# willingness to pay, Frontiers in Energy Research 2015), generators are
# p_nom_extendable with capital_cost 0, so shedding is unbounded and purely
# priced. Placed only at low-voltage electricity buses (technology 'load_el')
# and the five heat bus types (technology 'load_heat').
LOAD_SHEDDING_COST = 1e5     # EUR/MWh (VOLL, Gotske value)

# Numerical hygiene, replicating Gotske's resolve_network.py prepare_network():
CLIP_P_MAX_PU = 1e-2         # zero availability/inflow values below this
NOISY_COSTS = True           # their small degeneracy-breaking cost noise

# 1990 CO2 reference for the modelled sectors, computed from Gotske's own
# data/co2_totals.csv (bundled in data/): sum over all countries of all sectors
# except LULUCF, waste management, other, and indirect (i.e. electricity, heat,
# all transport including aviation and navigation, industry including process
# emissions, agriculture). Enables reporting net emissions as % of 1990 the way
# their paper does (their dispatch result: about -0.5% of 1990).
CO2_1990_BASELINE_MT = 4614.1

# Gotske's headline runs also constrain hydro reservoir state of charge to stay
# above the historical ENTSO-E minimum (hydroconstrained: True in their config,
# add_hydropower_constraint_soc in resolve_network.py). That requires their
# external ENTSO-E reservoir filling dataset which we do not have. Documented
# deviation: without it, reservoirs have full annual foresight, which is mildly
# OPTIMISTIC for adequacy in both the original and the climate runs alike, so
# the original-vs-future comparison stays internally consistent.
HYDRO_SOC_CONSTRAINT = False

PIPELINE_VERSION = 'v20'


# ---- output session (timestamped so reruns never overwrite) ----------------
# Each pipeline invocation gets its own dated folder under output/. Override
# with the RUN_TAG environment variable if you want a custom label instead.
import datetime as _dt
RUN_TAG = os.environ.get("RUN_TAG",
                         "run_" + _dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
SESSION_DIR = os.path.join(OUTPUT_ROOT, RUN_TAG)


def run_dir(method):
    """Per-method output folder, inside the timestamped session folder."""
    return os.path.join(SESSION_DIR,
                        f"{design_key()}_c2e{C2E_FUTURE}_{method}")
