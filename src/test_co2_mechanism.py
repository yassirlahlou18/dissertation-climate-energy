"""Unit test of the CO2 pricing, freeze and emissions mechanism. See the pipeline guide PDF."""
import numpy as np
import pandas as pd
import pypsa

import config
import dispatch as dsp

TOL = 1e-6


def build_toy():
    n = pypsa.Network()
    n.set_snapshots(pd.date_range('2015-01-01', periods=48, freq='h'))

    for c in ['AC', 'gas', 'co2', 'co2 stored', 'battery', 'wind', 'OCGT', 'DAC']:
        n.add('Carrier', c)

    n.add('Bus', 'elec', carrier='AC')
    n.add('Bus', 'gas', carrier='gas')
    n.add('Bus', 'co2 atmosphere', carrier='co2')
    n.add('Bus', 'co2 stored', carrier='co2 stored')
    n.add('Bus', 'bat', carrier='battery')

    # demand + exogenous emission load (negative p_set injects into the bus)
    n.add('Load', 'demand', bus='elec', p_set=40.0)
    n.add('Load', 'oil emissions', bus='co2 atmosphere', p_set=-10.0)

    # generators: wind is a 'real' technology, EU gas is zero-capital fuel supply
    rng = np.random.default_rng(7)
    cf = pd.Series(0.25 + 0.5 * rng.random(48), index=n.snapshots)
    n.add('Generator', 'wind', bus='elec', carrier='wind', p_nom_extendable=True,
          capital_cost=1200.0, marginal_cost=0.0, p_max_pu=cf)
    n.generators.loc['wind', 'p_nom_opt'] = 60.0
    n.add('Generator', 'EU gas', bus='gas', carrier='gas', p_nom_extendable=True,
          capital_cost=0.0, marginal_cost=20.0)
    n.generators.loc['EU gas', 'p_nom_opt'] = 123.0  # must NOT be used as a cap

    # OCGT: gas -> elec with CO2 to the atmosphere on bus2
    n.add('Link', 'OCGT', bus0='gas', bus1='elec', carrier='OCGT', efficiency=0.4,
          bus2='co2 atmosphere', efficiency2=0.2,
          p_nom_extendable=True, capital_cost=500.0, marginal_cost=5.0)
    n.links.loc['OCGT', 'p_nom_opt'] = 80.0

    # DAC: atmosphere -> stored, consuming a little electricity on bus2
    n.add('Link', 'site DAC', bus0='co2 atmosphere', bus1='co2 stored', carrier='DAC',
          efficiency=1.0, bus2='elec', efficiency2=-0.05,
          p_nom_extendable=True, capital_cost=800.0, marginal_cost=0.0)
    n.links.loc['site DAC', 'p_nom_opt'] = 10.0

    # battery: charger has capital cost, discharger is the zero-cost special case
    n.add('Link', 'bat charger', bus0='elec', bus1='bat', efficiency=0.95,
          p_nom_extendable=True, capital_cost=100.0)
    n.links.loc['bat charger', 'p_nom_opt'] = 10.0
    n.add('Link', 'bat battery discharger', bus0='bat', bus1='elec', efficiency=0.95,
          p_nom_extendable=True, capital_cost=0.0)
    n.links.loc['bat battery discharger', 'p_nom_opt'] = 10.0

    # stores: atmosphere is the zero-cost accounting store; the others are real
    n.add('Store', 'co2 atmosphere', bus='co2 atmosphere', carrier='co2',
          e_nom_extendable=True, capital_cost=0.0, e_min_pu=-1.0)
    n.add('Store', 'co2 stored', bus='co2 stored', carrier='co2 stored',
          e_nom_extendable=True, capital_cost=20.0)
    n.stores.loc['co2 stored', 'e_nom_opt'] = 1e5
    n.add('Store', 'bat store', bus='bat', carrier='battery',
          e_nom_extendable=True, capital_cost=50.0)
    n.stores.loc['bat store', 'e_nom_opt'] = 40.0

    # a hard cap to be removed by the price mechanism
    n.add('GlobalConstraint', 'CO2Limit', sense='<=', constant=0.0,
          carrier_attribute='co2_emissions')
    return n


def main():
    # exact-value assertions need the degeneracy noise off
    config.NOISY_COSTS = False
    n = build_toy()
    price = 100.0

    n = dsp.prepare_for_dispatch(n, 'TOY', log=print, co2_price=price)

    # ---- 1. freeze scope ----
    g, lk, st = n.generators, n.links, n.stores
    assert not g.loc['wind', 'p_nom_extendable'] and abs(g.loc['wind', 'p_nom'] - 60) < TOL, 'wind not pinned'
    assert g.loc['EU gas', 'p_nom_extendable'], 'EU fuel generator must stay extendable (zero capital cost)'
    assert not lk.loc['OCGT', 'p_nom_extendable'] and abs(lk.loc['OCGT', 'p_nom'] - 80) < TOL, 'OCGT not pinned'
    assert not lk.loc['bat charger', 'p_nom_extendable'], 'charger not pinned'
    assert not lk.loc['bat battery discharger', 'p_nom_extendable'] and \
        abs(lk.loc['bat battery discharger', 'p_nom'] - 10) < TOL, 'battery discharger special case failed'
    assert st.loc['co2 atmosphere', 'e_nom_extendable'], \
        'co2 atmosphere store must stay extendable (else hidden CO2 cap)'
    assert not st.loc['co2 stored', 'e_nom_extendable'] and abs(st.loc['co2 stored', 'e_nom'] - 1e5) < TOL
    assert not st.loc['bat store', 'e_nom_extendable']
    print('PASS 1: freeze scope (capital_cost>0 rule + battery discharger + free accounting store)')

    # ---- 2. shedding ----
    shed = g.index[g.carrier == 'load_el']
    assert len(shed) == 1 and shed[0] == 'elec load shedding', f'shedding gens: {list(shed)}'
    assert abs(g.loc[shed[0], 'marginal_cost'] - 1e5) < 1e-3
    assert g.loc[shed[0], 'p_nom_extendable'] and g.loc[shed[0], 'capital_cost'] == 0
    print('PASS 2: load shedding (load_el, 1e5 EUR/MWh, extendable, capital cost 0)')

    # ---- 3. pricing ----
    assert abs(n.links.loc['OCGT', 'marginal_cost'] - (5.0 + price * 0.2)) < 1e-6, \
        f"OCGT mc {n.links.loc['OCGT','marginal_cost']}"
    assert abs(n.links.loc['site DAC', 'marginal_cost'] - (0.0 - price * 1.0)) < 1e-6, \
        f"DAC mc {n.links.loc['site DAC','marginal_cost']} (credit missing?)"
    assert 'CO2Limit' not in n.global_constraints.index, 'CO2Limit not removed'
    print('PASS 3: CO2 pricing (emitter pays, DAC credited, cap removed)')

    # ---- 4. dispatch + accounting closure ----
    status, _ = n.optimize(solver_name='highs', solver_options={'output_flag': False})
    assert status == 'ok', f'solve status {status}'
    net, by_source, check = dsp.get_co2_emissions_Mt(n, detail=True)
    assert net is not None and check is not None
    assert abs(net - check) < 1e-6, f'net {net} vs store check {check} (accounting hole)'
    assert 'oil emissions' in by_source and abs(by_source['oil emissions'] - 10 * 48 / 1e6) < 1e-9, \
        f"oil emissions load not counted: {by_source}"
    assert any('DAC' in k for k in by_source) and \
        sum(v for k, v in by_source.items() if 'DAC' in k) < 0, 'DAC removal not in accounting'
    dem_ts, dem_twh = dsp._gotske_electricity_demand(n, n.snapshot_weightings.generators)
    assert dem_twh > 0
    print(f'PASS 4: accounting closes (net {net*1e6:+.1f} t == store check {check*1e6:+.1f} t; '
          f'sources: { {k: round(v*1e6,1) for k,v in by_source.items()} })')

    print('\nALL CO2-MECHANISM TESTS PASSED')


if __name__ == '__main__':
    main()
