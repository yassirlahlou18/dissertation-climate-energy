"""Builds a small synthetic network and C2E files for self-testing only. See the pipeline guide PDF."""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import os
import numpy as np
import pandas as pd
import pypsa

COUNTRIES = ['Germany', 'France', 'Spain', 'Norway', 'Italy']
REGION = {'Germany': 'DE0', 'France': 'FR0', 'Spain': 'ES0', 'Norway': 'NO1', 'Italy': 'IT0'}
N = 2920
FREQ = '3h'


def _idx(year):
    return pd.date_range(f'{year}-01-01', periods=N, freq=FREQ)


def synth_cf(idx, base, amp, noise, seed):
    rng = np.random.default_rng(seed)
    doy = idx.dayofyear.values
    seas = base + amp * np.cos(2 * np.pi * doy / 365.0)
    return np.clip(seas + noise * rng.standard_normal(len(idx)), 0, 1)


def write_c2e(c2e_dir, period, future=False):
    os.makedirs(c2e_dir, exist_ok=True)
    idx = _idx(period)
    # future: lower wind mean + higher variability; higher solar
    wfac = 0.92 if future else 1.0
    wnoise = 0.26 if future else 0.18
    for kind, base, amp, noise in [('PV', 0.15, 0.10, 0.10),
                                   ('wind_on', 0.32 * wfac, 0.14, wnoise),
                                   ('wind_off', 0.42 * wfac, 0.16, wnoise)]:
        rows = []
        for i, c in enumerate(COUNTRIES):
            cf = synth_cf(idx, base, amp, noise, seed=hash((kind, c, period)) % 2**31)
            rows.append([c] + list(cf))
        df = pd.DataFrame(rows, columns=['country'] + [str(t) for t in idx])
        import config as _cfg
        sup = _cfg.c2e_supply_files(period)
        fn = os.path.basename({'PV': sup['pv'], 'wind_on': sup['wind_onshore'],
                               'wind_off': sup['wind_offshore']}[kind])
        df.to_csv(os.path.join(c2e_dir, fn), index=False)

    # demand: heating (high winter), cooling (high summer), temperature
    import config as _cfg
    dem = _cfg.c2e_demand_files(period)
    for kind, fn in [('heating', os.path.basename(dem['heating'])),
                     ('cooling', os.path.basename(dem['cooling'])),
                     ('temperature', os.path.basename(dem['temperature']))]:
        rows = []
        for c in COUNTRIES:
            doy = idx.dayofyear.values
            if kind == 'heating':
                v = np.clip(0.8 + 0.6 * np.cos(2 * np.pi * doy / 365.0), 0, None)
                if future:
                    v *= 0.8  # warmer -> less heating
            elif kind == 'cooling':
                v = np.clip(0.2 - 0.3 * np.cos(2 * np.pi * doy / 365.0), 0, None)
                if future:
                    v *= 2.0  # warmer -> more cooling
            else:  # temperature deg C
                v = 10 - 8 * np.cos(2 * np.pi * doy / 365.0)
                if future:
                    v += 2.5
            rows.append([c] + list(v))
        pd.DataFrame(rows, columns=['country'] + [str(t) for t in idx]).to_csv(
            os.path.join(c2e_dir, fn), index=False)


def build_network(path, weather_year):
    n = pypsa.Network()
    idx = _idx(weather_year)
    n.set_snapshots(idx)
    n.snapshot_weightings.loc[:, :] = 3.0  # 3-hourly

    for c, reg in REGION.items():
        n.add('Bus', reg, carrier='AC')
        n.add('Bus', f'{reg} 0 urban central heat', carrier='urban central heat')
        # electricity load
        rng = np.random.default_rng(abs(hash(reg)) % 2**31)
        base_load = {'DE0': 60000, 'FR0': 55000, 'ES0': 30000, 'NO1': 15000, 'IT0': 40000}[reg]
        doy = idx.dayofyear.values
        eload = base_load * (1 + 0.15 * np.cos(2 * np.pi * doy / 365.0)) * (1 + 0.05 * rng.standard_normal(N))
        n.add('Load', f'{reg} elec', bus=reg, p_set=eload)
        # heat load
        hload = base_load * 0.4 * np.clip(1 + np.cos(2 * np.pi * doy / 365.0), 0, None)
        n.add('Load', f'{reg} 0 urban central heat', bus=f'{reg} 0 urban central heat', p_set=hload)

        # generators
        for carrier, pnom in [('onwind', base_load * 1.5), ('solar', base_load * 1.2),
                              ('offwind-ac', base_load * 0.6)]:
            cf = synth_cf(idx, 0.3, 0.12, 0.18, seed=abs(hash((reg, carrier))) % 2**31)
            n.add('Generator', f'{reg} {carrier}', bus=reg, carrier=carrier,
                  p_nom=pnom, p_nom_opt=pnom, p_nom_extendable=False,
                  p_max_pu=cf, marginal_cost=0.01)
        # gas backup
        n.add('Generator', f'{reg} gas', bus=reg, carrier='gas', p_nom=base_load,
              p_nom_opt=base_load, p_nom_extendable=False, marginal_cost=80)

        # heat pump link (elec -> heat) with constant efficiency to start
        n.add('Link', f'{reg} urban central air heat pump', bus0=reg,
              bus1=f'{reg} 0 urban central heat',
              carrier='urban central air heat pump', p_nom=base_load,
              p_nom_opt=base_load, p_nom_extendable=False, efficiency=3.0)
        # resistive heater fallback so heat balance is always feasible
        n.add('Link', f'{reg} resistive', bus0=reg, bus1=f'{reg} 0 urban central heat',
              carrier='resistive heater', p_nom=base_load, p_nom_opt=base_load,
              p_nom_extendable=False, efficiency=0.95)

    # a couple of transmission lines
    n.add('Line', 'DE0-FR0', bus0='DE0', bus1='FR0', s_nom=10000, x=1, r=0.1)
    n.add('Line', 'FR0-ES0', bus0='FR0', bus1='ES0', s_nom=8000, x=1, r=0.1)
    n.add('Line', 'DE0-IT0', bus0='DE0', bus1='IT0', s_nom=6000, x=1, r=0.1)

    # CO2: give gas an emissions intensity and add a (near) net-zero cap so the
    # shadow-price extraction path is exercised, like the real Gotske networks.
    for car, em in [('gas', 0.2), ('onwind', 0.0), ('solar', 0.0), ('offwind-ac', 0.0)]:
        if car in n.carriers.index:
            n.carriers.loc[car, 'co2_emissions'] = em
        else:
            n.add('Carrier', car, co2_emissions=em)
    n.add('GlobalConstraint', 'CO2Limit', sense='<=',
          constant=2.0e7, carrier_attribute='co2_emissions')

    # hydro: a reservoir storage unit with inflow + a run-of-river generator,
    # in two hydro countries, so the hydro modification path is exercised.
    for reg, scale in [('NO1', 5000), ('FR0', 2000)]:
        if reg in [b for b in n.buses.index]:
            su = f'{reg} hydro'
            n.add('StorageUnit', su, bus=reg, carrier='hydro', p_nom=scale,
                  max_hours=6, p_nom_extendable=False)
            inflow = scale * (0.4 + 0.3 * np.sin(2*np.pi*idx.dayofyear.values/365.0))
            n.storage_units_t.inflow[su] = inflow
            g = f'{reg} ror'
            cf = synth_cf(idx, 0.45, 0.1, 0.05, seed=abs(hash((reg,'ror')))%2**31)
            n.add('Generator', g, bus=reg, carrier='ror', p_nom=scale*0.5,
                  p_nom_extendable=False)
            n.generators_t.p_max_pu[g] = cf
    if 'hydro' not in n.carriers.index: n.add('Carrier', 'hydro', co2_emissions=0.0)
    if 'ror' not in n.carriers.index: n.add('Carrier', 'ror', co2_emissions=0.0)

    n.export_to_netcdf(path)
    return path


if __name__ == '__main__':
    root = os.environ.get('SYNROOT', '/home/claude/thesis_pipeline/_synthetic')
    os.makedirs(os.path.join(root, 'networks'), exist_ok=True)
    os.makedirs(os.path.join(root, 'C2E'), exist_ok=True)
    build_network(os.path.join(root, 'networks',
                  'elec_wy2015_s370_37_lv1.0__Co2L0-3h-T-H-B-I-A-solar+p3-dist1_2050.nc'), 2015)
    write_c2e(os.path.join(root, 'C2E'), 2015, future=False)
    write_c2e(os.path.join(root, 'C2E'), 2042, future=True)
    print('synthetic data written to', root)
