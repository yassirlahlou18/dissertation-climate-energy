"""Country to PyPSA bus mapping, used by every stage. See the pipeline guide PDF."""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))


COUNTRY_TO_BUSES = {
    'Albania':                ['AL0'],
    'Austria':                ['AT0'],
    'Bosnia and Herzegovina': ['BA0'],
    'Belgium':                ['BE0'],
    'Bulgaria':               ['BG0'],
    'Switzerland':            ['CH0'],
    'Czech Republic':         ['CZ0'],
    'Germany':                ['DE0'],
    'Denmark':                ['DK0', 'DK1'],
    'Estonia':                ['EE2'],
    'Spain':                  ['ES0', 'ES3'],
    'Finland':                ['FI1'],
    'France':                 ['FR0'],
    'United Kingdom':         ['GB4', 'GB5'],
    'Greece':                 ['GR0'],
    'Croatia':                ['HR0'],
    'Hungary':                ['HU0'],
    'Ireland':                ['IE5'],
    'Italy':                  ['IT0', 'IT6'],
    'Lithuania':              ['LT2'],
    'Latvia':                 ['LV2'],
    'Montenegro':             ['ME0'],
    'Macedonia':              ['MK0'],
    'Netherlands':            ['NL0'],
    'Norway':                 ['NO1'],
    'Poland':                 ['PL0'],
    'Portugal':               ['PT0'],
    'Romania':                ['RO0'],
    'Serbia':                 ['RS0'],
    'Sweden':                 ['SE1'],
    'Slovenia':               ['SI0'],
    'Slovakia':               ['SK0'],
}

BUS_TO_COUNTRY = {}
for _country, _buses in COUNTRY_TO_BUSES.items():
    for _b in _buses:
        BUS_TO_COUNTRY[_b] = _country

# Luxembourg has no C2E series -> proxy with neighbouring Belgium.
BUS_TO_COUNTRY['LU0'] = 'Belgium'

# Carrier -> which C2E supply variable supplies its CF
SUPPLY_CARRIER_TO_C2E = {
    'solar':         'pv',
    'solar rooftop': 'pv',
    'onwind':        'wind_onshore',
    'offwind-ac':    'wind_offshore',
    'offwind-dc':    'wind_offshore',
}

# Heat-carrier substrings used to detect heat LOADS in loads_t.p_set.
# PyPSA-Eur-Sec heat buses end in '... heat' with a space-separated prefix.
HEAT_LOAD_KEYS = [
    'urban central heat',
    'urban decentral heat',
    'rural heat',
    'residential rural heat',
    'services rural heat',
    'residential urban decentral heat',
    'services urban decentral heat',
    'low-temperature heat',   # industry process heat (optional, see config)
]

# Heat-pump link carriers whose efficiency (COP) is temperature dependent.
HEAT_PUMP_CARRIERS = [
    'urban central air heat pump',
    'urban decentral air heat pump',
    'rural air heat pump',
    'rural ground heat pump',
    'residential rural ground heat pump',
    'services rural ground heat pump',
]


def region_of(bus_name: str) -> str:
    return bus_name.split(' ')[0]


def country_of_bus(bus_name: str):
    return BUS_TO_COUNTRY.get(region_of(bus_name))
