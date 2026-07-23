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


def test_heating_qdm_mismatch_bounded():
    """REGRESSION (the v16 bug): when C2E and the network have different
    seasonal support (C2E has almost no spring heating, the network has a large
    one), the per-quantile multiplier blew up to the 5x cap. The robust change
    factor must stay bounded and still match the model's seasonal change."""
    n, idx = _net_with_heat()
    w = n.snapshot_weightings.generators
    doy = idx.dayofyear.values
    # network: broad heating season (heating well into spring/autumn)
    net_season = np.clip(0.5 + 0.5 * np.cos(2 * np.pi * doy / 365), 0, None)
    n.loads_t.p_set['DE0 urban central heat'] = net_season * 5000 + 300.0
    # C2E: SHARP season, near zero through much of spring/autumn (the mismatch)
    c2e_season = np.clip(np.cos(2 * np.pi * doy / 365), 0, None) ** 2
    base = {'Germany': pd.Series(c2e_season * 4000, index=idx)}
    fut = {'Germany': pd.Series(c2e_season * 4000 * 0.88, index=idx)}  # ~12% less
    orig = n.loads_t.p_set['DE0 urban central heat'].copy()
    hc.modify_heat_demand(n, 'qdm', fut, base, log=lambda *_: None)
    new = n.loads_t.p_set['DE0 urban central heat']
    mult = np.where(orig.values > 1e-9, new.values / orig.values, 1.0)
    assert np.nanmax(mult) <= 3.0 + 1e-9, \
        f'heating multiplier blew up to {np.nanmax(mult):.2f} (regression!)'
    # winter (where both have heating) change should track the model ~0.88
    winter = idx.month.isin([12, 1, 2])
    r = new.values[winter].sum() / orig.values[winter].sum()
    assert 0.80 < r < 0.96, f'winter change {r:.3f} not ~0.88'
    print(f'PASS heating/qdm mismatch: max mult {np.nanmax(mult):.2f}<=3.0, winter {r:.3f}~0.88')


def test_hydro_qdm_conserves_volume():
    """REGRESSION (the Norway bug): qdm hydro inflow must conserve the ANNUAL
    volume (cyclic reservoir), matching the C2E model annual change. The old
    per-quantile QDM did not, starving Norway."""
    n = pypsa.Network()
    idx = pd.date_range('2015-01-01', periods=2920, freq='3h')
    n.set_snapshots(idx); n.snapshot_weightings.loc[:, :] = 3.0
    n.add('Carrier', 'AC'); n.add('Carrier', 'hydro')
    n.add('Bus', 'NO1', carrier='AC')
    n.add('StorageUnit', 'NO1 hydro', bus='NO1', carrier='hydro', p_nom=10000)
    rng = np.random.default_rng(7)
    doy = idx.dayofyear.values
    # snowmelt-driven: near zero in winter, sharp melt peak (skewed, zero-heavy)
    melt = np.clip(np.sin(np.pi * np.clip((doy - 90) / 120, 0, 1)) ** 3, 0, None)
    inflow = np.clip(melt * 500 * (1 + 0.2 * rng.standard_normal(2920)), 0, None)
    n.storage_units_t.inflow['NO1 hydro'] = inflow
    w = n.snapshot_weightings.generators
    base = {'Norway': pd.Series(np.clip(melt * 480 * (1 + 0.3 * rng.standard_normal(2920)), 0, None), index=idx)}
    fut = {'Norway': pd.Series(np.clip(melt * 480 * 1.05 * (1 + 0.3 * rng.standard_normal(2920)), 0, None), index=idx)}
    model_chg = float((fut['Norway'].values * w.values).sum() / (base['Norway'].values * w.values).sum())
    orig_vol = float((inflow * w.values).sum())
    hc.modify_hydro(n, 'qdm', fut, base, None, None, log=lambda *_: None)
    new_vol = float((n.storage_units_t.inflow['NO1 hydro'].values * w.values).sum())
    realised = new_vol / orig_vol
    assert abs(realised - model_chg) < 0.02, \
        f'qdm hydro realised volume change {realised:.4f} != model {model_chg:.4f} (regression!)'
    print(f'PASS hydro/qdm volume: realised {realised:.4f} == model {model_chg:.4f} (cyclic-safe)')


def test_loader_upsampling_no_nan():
    """v20.1 regression: weekly (inflow) and daily (ror) C2E files must arrive
    on the 3h grid with NO NaN and values held flat within each native period.
    The original resample().mean() left NaN everywhere except native stamps,
    which silently collapsed the qdm hydro factor to 1.0 and fed all-NaN
    capacity factors to direct ror (the 'filled 20440 NaN in p_max_pu' line)."""
    import tempfile, os
    import c2e_loader
    d = tempfile.mkdtemp()
    # weekly file, 52 stamps, value = week number (1..52)
    weeks = pd.date_range('2042-01-06', periods=52, freq='7D')
    pd.DataFrame([['Testland'] + list(range(1, 53))],
                 columns=['country'] + [t.strftime('%Y-%m-%d') for t in weeks]) \
        .to_csv(os.path.join(d, 'wk.csv'), index=False)
    out = c2e_loader.load_c2e_file(os.path.join(d, 'wk.csv'), 2920, '3h')['Testland']
    assert not out.isna().any(), 'weekly upsample must not contain NaN'
    assert abs(out.iloc[0] - 1.0) < 1e-9, 'head must back-fill the first week'
    assert abs(out.iloc[-1] - 52.0) < 1e-9, 'tail must hold the last week'
    # a mid-year 3h point must equal its containing week's value (flat fill)
    v = out.iloc[len(out) // 2]
    assert v == int(v) and 1 <= v <= 52, f'mid-year value not a flat week value: {v}'
    # daily file, 365 stamps
    days = pd.date_range('2042-01-01', periods=365, freq='1D')
    pd.DataFrame([['Testland'] + [10.0] * 365],
                 columns=['country'] + [t.strftime('%Y-%m-%d') for t in days]) \
        .to_csv(os.path.join(d, 'dy.csv'), index=False)
    out2 = c2e_loader.load_c2e_file(os.path.join(d, 'dy.csv'), 2920, '3h')['Testland']
    assert not out2.isna().any() and float(out2.max()) == 10.0 == float(out2.min())
    print('PASS loader upsampling: weekly and daily files arrive NaN-free, '
          'flat within native periods')


def test_hydro_stats_and_annual_mode():
    """v19: modify_hydro must report per-country applied annual ratios and
    coverage volumes; and HYDRO_INFLOW_FACTOR_MODE='annual' must apply the
    exact scalar annual ratio."""
    import config
    def make_net():
        n = pypsa.Network()
        idx = pd.date_range('2015-01-01', periods=2920, freq='3h')
        n.set_snapshots(idx); n.snapshot_weightings.loc[:, :] = 3.0
        n.add('Carrier', 'AC'); n.add('Carrier', 'hydro')
        n.add('Bus', 'NO1', carrier='AC'); n.add('Bus', 'XX0', carrier='AC')
        n.add('StorageUnit', 'NO1 hydro', bus='NO1', carrier='hydro', p_nom=1e4)
        n.add('StorageUnit', 'XX0 hydro', bus='XX0', carrier='hydro', p_nom=1e4)
        doy = idx.dayofyear.values
        melt = np.clip(np.sin(np.pi * np.clip((doy - 90) / 120, 0, 1)) ** 3, 0, None)
        n.storage_units_t.inflow['NO1 hydro'] = melt * 500
        n.storage_units_t.inflow['XX0 hydro'] = melt * 100   # uncovered country
        return n, idx, melt

    n, idx, melt = make_net()
    base = {'Norway': pd.Series(melt * 480, index=idx)}
    fut = {'Norway': pd.Series(melt * 480 * 1.30, index=idx)}
    old_mode = getattr(config, 'HYDRO_INFLOW_FACTOR_MODE', 'seasonal')
    try:
        config.HYDRO_INFLOW_FACTOR_MODE = 'annual'
        summ = hc.modify_hydro(n, 'qdm', fut, base, None, None, log=lambda *_: None)
    finally:
        config.HYDRO_INFLOW_FACTOR_MODE = old_mode
    st = summ['inflow_country_stats'].get('Norway')
    assert st is not None and abs(st['applied_annual_ratio'] - 1.30) < 1e-6, \
        f"annual mode ratio wrong: {st}"
    assert abs(summ['inflow_vol_modified_TWh'] / summ['inflow_vol_total_TWh'] - 500/600) < 0.01, \
        "coverage volume accounting wrong"
    # seasonal mode should ALSO hit the annual ratio (PresRAT annual conservation)
    n2, idx2, melt2 = make_net()
    summ2 = hc.modify_hydro(n2, 'qdm', fut, base, None, None, log=lambda *_: None)
    st2 = summ2['inflow_country_stats']['Norway']
    assert abs(st2['applied_annual_ratio'] - 1.30) < 0.02, f"seasonal mode annual ratio: {st2}"
    print("PASS hydro stats + annual mode: ratios reported, coverage volumes right, "
          "both modes conserve the annual change")


def test_pct_display_sane():
    """v19: percentage display returns NaN for from-zero and sign-flip cases."""
    import reporting as rep
    assert np.isnan(rep._pct(4e-7, 21.8)), "from-~zero must be NaN, not billions of %"
    assert np.isnan(rep._pct(-118.0, 2061.0)), "sign flip must be NaN"
    assert abs(rep._pct(100.0, 110.0) - 10.0) < 1e-9
    print("PASS pct display: from-zero and sign-flip render as n/a")


def test_channel_method_routing():
    """Lock in the per-channel method assignment: the build resolver must route
    each channel to its configured method, and the production config must be
    supply/ror/cooling = direct, hydro_inflow/heating = qdm. If someone later
    reverts to a single global method, this fails loudly."""
    import build_modified_network as bmn
    import config

    # 1) the resolver honours a per-channel map
    label, ch = bmn._resolve_channel_methods(
        {'supply': 'direct', 'hydro_inflow': 'qdm', 'heating': 'qdm',
         'ror': 'direct', 'cooling': 'direct', 'cop': 'qdm'})
    assert ch['supply'] == 'direct' and ch['hydro_inflow'] == 'qdm' \
        and ch['heating'] == 'qdm' and ch['ror'] == 'direct' \
        and ch['cooling'] == 'direct', f'resolver mis-routed: {ch}'

    # 2) a bare string still maps every channel to that method (legacy runs)
    label2, ch2 = bmn._resolve_channel_methods('qdm')
    assert all(v == 'qdm' for v in ch2.values()) and label2 == 'qdm', \
        f'legacy string routing broken: {ch2}'

    # 3) an invalid method is rejected
    try:
        bmn._resolve_channel_methods({'supply': 'banana'})
        raise AssertionError('invalid method was not rejected')
    except ValueError:
        pass

    # 4) the SHIPPED production config is the intended split
    cm = config.CHANNEL_METHODS
    assert cm['supply'] == 'direct' and cm['ror'] == 'direct' \
        and cm['cooling'] == 'direct', f'production supply/ror/cooling not direct: {cm}'
    assert cm['hydro_inflow'] == 'qdm' and cm['heating'] == 'qdm', \
        f'production hydro/heating not qdm: {cm}'
    print('PASS channel routing: production = direct[supply,ror,cooling] '
          '+ qdm[hydro_inflow,heating]; legacy string + validation OK')


if __name__ == '__main__':
    test_heating_direct()
    test_heating_qdm_uniform()
    test_heating_qdm_mismatch_bounded()
    test_cooling_cancel_and_add()
    test_hydro_direct_inflow()
    test_hydro_qdm_conserves_volume()
    test_loader_upsampling_no_nan()
    test_hydro_stats_and_annual_mode()
    test_pct_display_sane()
    test_channel_method_routing()
    print('\nALL v16 DEMAND/HYDRO TESTS PASSED')
