"""v21 tests: the multi-system adapter layer. Guarantees (1) gotske behaves
BYTE-IDENTICALLY to v20 (filenames, cache keys), (2) the neumann2023 grammar
and discovery work, (3) country resolution survives finer bus zonings.

Run: python3 test_systems.py
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(_os.path.abspath(__file__))))
import os
import tempfile
import shutil

import config
import systems
import mapping


def test_gotske_byte_compat():
    assert config.SYSTEM == 'gotske'
    assert config.design_key() == f"wy{config.WEATHER_YEAR}"
    legacy = config.network_file()
    via_sys = systems.network_path('gotske', f"wy{config.WEATHER_YEAR}")
    assert os.path.basename(legacy) == os.path.basename(via_sys), (legacy, via_sys)
    assert config.active_network_file() == legacy
    print("PASS gotske byte-compat: design_key 'wy%d', filenames identical "
          "via both paths" % config.WEATHER_YEAR)


def test_neumann_grammar_and_registry():
    assert len(systems.NEUMANN2023_DESIGNS) == 8 == len(set(systems.NEUMANN2023_DESIGNS))
    did = systems.NEUMANN2023_DESIGNS[0]
    fn = systems.get('neumann2023')['filename'](did)
    assert fn == f"elec_s_181_{did}_2050.nc", fn
    key = systems.design_key('neumann2023', did)
    assert key == f"neumann2023--{did}"
    s2, d2 = systems.split_design_key(key)
    assert (s2, d2) == ('neumann2023', did)
    assert systems.split_design_key('wy2015') == ('gotske', 'wy2015')
    print("PASS neumann grammar: 8 designs, filename + key roundtrip")


def test_discovery_separates_families():
    tmp = tempfile.mkdtemp(prefix='systest_')
    old = config.NETWORK_DIR
    try:
        config.NETWORK_DIR = tmp
        open(os.path.join(tmp, systems.get('gotske')['filename']('wy2014')), 'w').close()
        open(os.path.join(tmp, systems.get('gotske')['filename']('wy2015')), 'w').close()
        did = systems.NEUMANN2023_DESIGNS[1]
        open(os.path.join(tmp, systems.get('neumann2023')['filename'](did)), 'w').close()
        g = systems.discover_designs('gotske')
        n = systems.discover_designs('neumann2023')
        assert g == ['wy2014', 'wy2015'], g
        assert n == [did], n
        print("PASS discovery: families separate cleanly on a mixed folder")
    finally:
        config.NETWORK_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_country_resolution_fine_zoning():
    # gotske zones still exact
    assert mapping.country_of_bus('IT6') == 'Italy'
    assert mapping.country_of_bus('LU0') == 'Belgium'
    # 181-node style regions resolve via ISO2 fallback, heat suffixes included
    assert mapping.country_of_bus('DE1 3') == 'Germany'
    assert mapping.country_of_bus('DE1 3 residential rural heat') == 'Germany'
    assert mapping.country_of_bus('ES4 0 urban central heat') == 'Spain'
    assert mapping.country_of_bus('LU1 0') == 'Belgium'
    assert mapping.country_of_bus('XX9 0') is None
    print("PASS country resolution: 37-zone exact, ISO2 fallback for finer "
          "zonings, unknown stays None")


def test_supply_carrier_map_extended():
    from mapping import SUPPLY_CARRIER_TO_C2E as M
    for c in ('solar', 'solar rooftop', 'onwind', 'offwind-ac', 'offwind-dc',
              'offwind-float'):
        assert c in M, c
    print("PASS carrier map: rooftop + all offshore variants covered")


def test_broad_ranges_grammar_and_freq():
    """v21.1: the power-only near-optimal adapter + the network-derived
    regrid frequency (a 4H/2H system must not be regridded at 3h)."""
    import pandas as pd
    # grammar roundtrip: optimum and a near-optimal id
    for did in ('37_ec_lcopt_4H', '37_ec_lcopt_4H_E0.06_OGenerator+wind+min',
                '128_ec_lcopt_2H'):
        fn = systems.get('broad_ranges')['filename'](did)
        assert fn == f"elec_s_{did}.nc", fn
        key = systems.design_key('broad_ranges', did)
        assert systems.split_design_key(key) == ('broad_ranges', did)
    assert systems.get('broad_ranges')['sector_coupled'] is False
    assert systems.get('gotske').get('sector_coupled', True) is True
    # discovery: all three families disjoint on one folder
    import tempfile, shutil
    tmp = tempfile.mkdtemp(prefix='sys3_')
    old = config.NETWORK_DIR
    try:
        config.NETWORK_DIR = tmp
        open(os.path.join(tmp, systems.get('gotske')['filename']('wy2015')), 'w').close()
        open(os.path.join(tmp, systems.get('neumann2023')['filename'](
            systems.NEUMANN2023_DESIGNS[0])), 'w').close()
        open(os.path.join(tmp, 'elec_s_37_ec_lcopt_4H.nc'), 'w').close()
        open(os.path.join(tmp, 'elec_s_37_ec_lcopt_4H_E0.06_OGenerator+wind+min.nc'), 'w').close()
        assert systems.discover_designs('gotske') == ['wy2015']
        assert len(systems.discover_designs('neumann2023')) == 1
        br = systems.discover_designs('broad_ranges')
        assert br == ['37_ec_lcopt_4H', '37_ec_lcopt_4H_E0.06_OGenerator+wind+min'], br
    finally:
        config.NETWORK_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)
    # network-derived frequency
    import build_modified_network as bmn
    class _N:
        snapshot_weightings = pd.DataFrame({'objective': [4.0, 4.0]})
    assert bmn._grid_freq(_N()) == '4h'
    _N.snapshot_weightings = pd.DataFrame({'objective': [3.0]})
    assert bmn._grid_freq(_N()) == '3h'
    _N.snapshot_weightings = pd.DataFrame({'objective': [2.0]})
    assert bmn._grid_freq(_N()) == '2h'
    print('PASS broad_ranges grammar + three-family discovery disjoint + '
          'network-derived regrid freq (2h/3h/4h)')


if __name__ == '__main__':
    test_gotske_byte_compat()
    test_neumann_grammar_and_registry()
    test_discovery_separates_families()
    test_country_resolution_fine_zoning()
    test_supply_carrier_map_extended()
    test_broad_ranges_grammar_and_freq()
    print("\nALL SYSTEM-ADAPTER TESTS PASSED")
