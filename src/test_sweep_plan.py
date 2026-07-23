"""Fast tests for the sweep PLANNING logic (src/sweep.py). No solving: these
verify year parsing, network discovery, the skip-done marker mechanics, and
the future-usability preflight, so a refactor cannot silently break the
sweep's restartability or scheduling.

Run:  python3 test_sweep_plan.py
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import os
import json
import shutil
import tempfile

import config
import sweep


def test_parse_years():
    assert sweep.parse_years('2002-2005') == [2002, 2003, 2004, 2005]
    assert sweep.parse_years('2010,2015,2019') == [2010, 2015, 2019]
    assert sweep.parse_years('2015') == [2015]
    assert sweep.parse_years('last:3', available=[1990, 2000, 2010, 2020]) == [2000, 2010, 2020]
    assert sweep.parse_years('all', available=[2001, 1999]) == [1999, 2001]
    try:
        sweep.parse_years('last:3')
        raise AssertionError('last:N without available must raise')
    except ValueError:
        pass
    print('PASS parse_years: ranges, lists, single, last:N, all, and guards')


def test_discovery_and_plan_with_markers():
    tmp = tempfile.mkdtemp(prefix='sweeptest_')
    old_netdir = config.NETWORK_DIR
    try:
        config.NETWORK_DIR = tmp
        # fake design networks for 2014 and 2015 only (2016 missing)
        for y in (2014, 2015):
            open(os.path.join(tmp, os.path.basename(config.network_file(y))), 'w').close()
        assert sweep.discover_network_years() == [2014, 2015]

        status = os.path.join(tmp, 'status'); os.makedirs(status)
        chains, notes = sweep.plan([2014, 2015, 2016], [2042, 2099], status)
        assert chains == [(2014, [2042, 2099]), (2015, [2042, 2099])], chains
        assert any('SKIP wy2016' in n_ for n_ in notes), notes

        # marker for (2015, 2042) -> that task skipped, chain keeps only 2099
        with open(sweep._marker(status, 2015, 2042), 'w') as fh:
            json.dump({'status': 'ok'}, fh)
        chains2, notes2 = sweep.plan([2014, 2015, 2016], [2042, 2099], status)
        assert chains2 == [(2014, [2042, 2099]), (2015, [2099])], chains2
        assert any('done  wy2015 f2042' in n_ for n_ in notes2), notes2

        # all done -> year drops out entirely
        with open(sweep._marker(status, 2015, 2099), 'w') as fh:
            json.dump({'status': 'ok'}, fh)
        chains3, _ = sweep.plan([2015], [2042, 2099], status)
        assert chains3 == [], chains3
        print('PASS discovery + plan: missing networks skipped, markers skip '
              'done tasks, per-year chains ordered')
    finally:
        config.NETWORK_DIR = old_netdir
        shutil.rmtree(tmp, ignore_errors=True)


def test_c2e_future_preflight():
    # 2042 essentials exist in the synthetic/real C2E dir when configured;
    # a nonsense period must report missing essentials.
    ok, missing = sweep.c2e_future_ok(1234)
    assert not ok and len(missing) >= 1
    print('PASS c2e preflight: unusable future is reported with its missing files')


if __name__ == '__main__':
    test_parse_years()
    test_discovery_and_plan_with_markers()
    test_c2e_future_preflight()
    print('\nALL SWEEP-PLANNING TESTS PASSED')
