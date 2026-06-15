"""Unit tests of the v16 heating, cooling and hydro transforms. See the pipeline guide PDF."""
import numpy as np
import pandas as pd
import pypsa

import config
import heat_cooling as hc

TOL = 1e-6


def _net_with_heat():
    n = pypsa.Network()
    idx = pd.date_range('2015-01-01', periods=2920, freq='3h')  # 3-hourly year
    n.set_snapshots(idx)
    n.snapshot_weightings.loc[:, :] = 3.0
    n.add('Carrier', 'AC'); n.add('Carrier', 'urban central heat'); n.add('Carrier', 'electricity')
    n.add('Bus', 'DE0', carrier='AC')
    n.add('Bus', 'DE0 urban central heat', carrier='urban central heat')
    doy = idx.dayofyear.values
    hod = idx.hour.values
    # heat: seasonal + a daily shape + a constant hot-water floor
    heat = 5000 * np.clip(1 + np.cos(2 * np.pi * doy / 365), 0, None) \
        * (1 + 0.3 * np.cos(2 * np.pi * (hod - 7) / 24)) + 800.0
    n.add('Load', 'DE0 urban central heat', bus='DE0 urban central heat', p_set=heat)
    eload = 40000 * (1 + 0.1 * np.cos(2 * np.pi * doy / 365))
    n.add('Load', 'DE0 elec', bus='DE0', carrier='electricity', p_set=eload)
    return n, idx


def _c2e_heat(idx, scale=1.0, noise=0.0, seed=0):
    doy = idx.dayofyear.values
    hod = idx.hour.values
    rng = np.random.default_rng(seed)
    base = np.clip(1.0 + np.cos(2 * np.pi * doy / 365), 0, None) \
        * (1 + 0.2 * np.cos(2 * np.pi * (hod - 6) / 24))
    base = base * scale + noise * rng.standard_normal(len(idx))
    return {'Germany': pd.Series(np.clip(base, 0, None), index=idx)}


def test_heating_direct():
    n, idx = _net_with_heat()
    w = n.snapshot_weightings.generators
    base = _c2e_heat(idx, scale=1.0, seed=1)
    fut = _c2e_heat(idx, scale=0.7, seed=1)  # 30% less heating, same shape
    orig = n.loads_t.p_set['DE0 urban central heat'].copy()
    orig_annual = float((orig.values * w.values).sum())
    hc.modify_heat_demand(n, 'direct', fut, base, log=lambda *_: None)
    new = n.loads_t.p_set['DE0 urban central heat']
    new_annual = float((new.values * w.values).sum())
    ratio = (fut['Germany'].values * w.values).sum() / (base['Germany'].values * w.values).sum()
    # annual energy above the floor scales by the C2E ratio; floor preserved, so
    # total annual is between ratio*orig and orig. Check the variable part:
    floor = float(np.percentile(orig.values, 5))
    var_orig = orig_annual - floor * float(w.sum())
    var_new = new_annual - floor * float(w.sum())
    assert abs(var_new / var_orig - ratio) < 1e-3, \
        f'direct heating variable-energy ratio {var_new/var_orig:.4f} != C2E ratio {ratio:.4f}'
    # shape: correlation of new (minus floor) with C2E future should be ~1
    cc = np.corrcoef((new.values - floor).clip(min=0), fut['Germany'].values)[0, 1]
    assert cc > 0.98, f'direct heating shape not transplanted (corr {cc:.3f})'
    print(f'PASS heating/direct: annual ratio {var_new/var_orig:.3f}=={ratio:.3f}, shape corr {cc:.3f}')


def test_heating_qdm_uniform():
    n, idx = _net_with_heat()
    base = _c2e_heat(idx, scale=1.0, seed=2)
    fut = _c2e_heat(idx, scale=0.8, seed=2)  # uniform 0.8x, same realisation
    orig = n.loads_t.p_set['DE0 urban central heat'].copy()
    hc.modify_heat_demand(n, 'qdm', fut, base, log=lambda *_: None)
    new = n.loads_t.p_set['DE0 urban central heat']
    # winter months (heating active) should be scaled by ~0.8; allow window blur
    winter = idx.month.isin([1, 2, 12])
    r = (new.values[winter].sum()) / (orig.values[winter].sum())
    assert 0.74 < r < 0.86, f'qdm uniform-0.8 winter ratio {r:.3f} not ~0.8'
    # within-day shape preserved: pick a winter day, ratio constant across its hours
    day = (idx.normalize() == pd.Timestamp('2015-01-15'))
    mult = new.values[day] / np.where(orig.values[day] > 1e-9, orig.values[day], np.nan)
    mult = mult[np.isfinite(mult)]
    assert np.nanstd(mult) < 1e-6, f'qdm multiplier not constant within a day (std {np.nanstd(mult):.2e})'
    print(f'PASS heating/qdm: winter ratio {r:.3f}~0.8, within-day multiplier constant')


def test_cooling_cancel_and_add():
    n, idx = _net_with_heat()
    w = n.snapshot_weightings.generators
    doy = idx.dayofyear.values
    cool_base = {'Germany': pd.Series(np.clip(0.5 - 0.6 * np.cos(2 * np.pi * doy / 365), 0, None), index=idx)}
    elec0 = n.loads_t.p_set['DE0 elec'].copy()
    # (a) future == baseline -> electricity unchanged (extract then re-add cancel)
    s = hc.modify_cooling(n, 'qdm', {'Germany': cool_base['Germany'].copy()}, cool_base, log=lambda *_: None)
    diff = float(np.abs(n.loads_t.p_set['DE0 elec'].values - elec0.values).max())
    assert diff < 1e-6, f'cooling future==baseline changed load by {diff:.3e} (should cancel)'
    # (b) future = 2x baseline -> net positive added energy
    n2, idx2 = _net_with_heat()
    cb = {'Germany': pd.Series(np.clip(0.5 - 0.6 * np.cos(2 * np.pi * idx2.dayofyear.values / 365), 0, None), index=idx2)}
    cf = {'Germany': cb['Germany'] * 2.0}
    e0 = float((n2.loads_t.p_set['DE0 elec'].values * n2.snapshot_weightings.generators.values).sum())
    hc.modify_cooling(n2, 'qdm', cf, cb, log=lambda *_: None)
    e1 = float((n2.loads_t.p_set['DE0 elec'].values * n2.snapshot_weightings.generators.values).sum())
    assert e1 > e0, f'doubling cooling did not raise demand ({e0:.0f}->{e1:.0f})'
    print(f'PASS cooling: future==base cancels (max d {diff:.1e}); 2x base adds {(e1-e0)/1e6:.2f} TWh')


def test_hydro_direct_inflow():
    n = pypsa.Network()
    idx = pd.date_range('2015-01-01', periods=2920, freq='3h')
    n.set_snapshots(idx); n.snapshot_weightings.loc[:, :] = 3.0
    n.add('Carrier', 'AC'); n.add('Carrier', 'hydro')
    n.add('Bus', 'NO1', carrier='AC')
    n.add('StorageUnit', 'NO1 hydro', bus='NO1', carrier='hydro', p_nom=10000)
    rng = np.random.default_rng(5)
    inflow = np.clip(2000 + 1500 * np.sin(2 * np.pi * idx.dayofyear.values / 365) + 200 * rng.standard_normal(2920), 0, None)
    n.storage_units_t.inflow['NO1 hydro'] = inflow
    w = n.snapshot_weightings.generators
    base = {'Norway': pd.Series(inflow / 1000.0, index=idx)}          # arbitrary units
    fut = {'Norway': pd.Series(inflow / 1000.0 * 1.2, index=idx)}     # +20%
    orig_annual = float((inflow * w.values).sum())
    hc.modify_hydro(n, 'direct', fut, base, None, None, log=lambda *_: None)
    new_annual = float((n.storage_units_t.inflow['NO1 hydro'].values * w.values).sum())
    assert abs(new_annual / orig_annual - 1.2) < 1e-3, \
        f'direct inflow ratio {new_annual/orig_annual:.4f} != 1.2'
    print(f'PASS hydro/direct: inflow annual ratio {new_annual/orig_annual:.3f}==1.2 (units cancel)')


if __name__ == '__main__':
    test_heating_direct()
    test_heating_qdm_uniform()
    test_cooling_cancel_and_add()
    test_hydro_direct_inflow()
    print('\nALL v16 DEMAND/HYDRO TESTS PASSED')
