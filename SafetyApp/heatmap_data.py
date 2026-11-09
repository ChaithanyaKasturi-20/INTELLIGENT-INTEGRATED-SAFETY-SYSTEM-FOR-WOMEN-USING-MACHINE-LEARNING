import math

from django.utils.text import slugify


CRIME_CATEGORIES = {
    'All Crimes Against Women': 'All Crimes Against Women',
    'Rape': 'Rape',
    'K&A': 'Kidnapping and Abduction',
    'DD': 'Dowry Deaths',
    'AoW': 'Assault on Women',
    'AoM': 'Assault on Minors',
    'DV': 'Domestic Violence',
    'WT': 'Women Trafficking',
}

CRIME_COLUMNS = ('Rape', 'K&A', 'DD', 'AoW', 'AoM', 'DV', 'WT')

STATE_ALIASES = {
    'A & N ISLANDS': 'Andaman and Nicobar',
    'A & N Islands': 'Andaman and Nicobar',
    'ANDHRA PRADESH': 'Andhra Pradesh',
    'Andhra Pradesh': 'Andhra Pradesh',
    'ARUNACHAL PRADESH': 'Arunachal Pradesh',
    'Arunachal Pradesh': 'Arunachal Pradesh',
    'ASSAM': 'Assam',
    'Assam': 'Assam',
    'BIHAR': 'Bihar',
    'Bihar': 'Bihar',
    'CHANDIGARH': 'Chandigarh',
    'Chandigarh': 'Chandigarh',
    'CHHATTISGARH': 'Chhattisgarh',
    'Chhattisgarh': 'Chhattisgarh',
    'D & N HAVELI': 'Dadra and Nagar Haveli',
    'D&N Haveli': 'Dadra and Nagar Haveli',
    'DAMAN & DIU': 'Daman and Diu',
    'Daman & Diu': 'Daman and Diu',
    'DELHI UT': 'Delhi',
    'Delhi UT': 'Delhi',
    'GOA': 'Goa',
    'Goa': 'Goa',
    'GUJARAT': 'Gujarat',
    'Gujarat': 'Gujarat',
    'HARYANA': 'Haryana',
    'Haryana': 'Haryana',
    'HIMACHAL PRADESH': 'Himachal Pradesh',
    'Himachal Pradesh': 'Himachal Pradesh',
    'JAMMU & KASHMIR': 'Jammu and Kashmir',
    'Jammu & Kashmir': 'Jammu and Kashmir',
    'JHARKHAND': 'Jharkhand',
    'Jharkhand': 'Jharkhand',
    'KARNATAKA': 'Karnataka',
    'Karnataka': 'Karnataka',
    'KERALA': 'Kerala',
    'Kerala': 'Kerala',
    'LAKSHADWEEP': 'Lakshadweep',
    'Lakshadweep': 'Lakshadweep',
    'MADHYA PRADESH': 'Madhya Pradesh',
    'Madhya Pradesh': 'Madhya Pradesh',
    'MAHARASHTRA': 'Maharashtra',
    'Maharashtra': 'Maharashtra',
    'MANIPUR': 'Manipur',
    'Manipur': 'Manipur',
    'MEGHALAYA': 'Meghalaya',
    'Meghalaya': 'Meghalaya',
    'MIZORAM': 'Mizoram',
    'Mizoram': 'Mizoram',
    'NAGALAND': 'Nagaland',
    'Nagaland': 'Nagaland',
    'ODISHA': 'Odisha',
    'Odisha': 'Odisha',
    'ORISSA': 'Odisha',
    'Orissa': 'Odisha',
    'PUDUCHERRY': 'Puducherry',
    'Puducherry': 'Puducherry',
    'PUNJAB': 'Punjab',
    'Punjab': 'Punjab',
    'RAJASTHAN': 'Rajasthan',
    'Rajasthan': 'Rajasthan',
    'SIKKIM': 'Sikkim',
    'Sikkim': 'Sikkim',
    'TAMIL NADU': 'Tamil Nadu',
    'Tamil Nadu': 'Tamil Nadu',
    'TELANGANA': 'Telangana',
    'Telangana': 'Telangana',
    'TRIPURA': 'Tripura',
    'Tripura': 'Tripura',
    'UTTAR PRADESH': 'Uttar Pradesh',
    'Uttar Pradesh': 'Uttar Pradesh',
    'UTTARAKHAND': 'Uttarakhand',
    'Uttarakhand': 'Uttarakhand',
    'UTTARANCHAL': 'Uttarakhand',
    'Uttaranchal': 'Uttarakhand',
    'WEST BENGAL': 'West Bengal',
    'West Bengal': 'West Bengal',
}


def canonical_state_name(value):
    name = str(value or '').strip()
    return STATE_ALIASES.get(name) or STATE_ALIASES.get(name.upper())


def state_geographic_id(name):
    return 'IN-STATE-{}'.format(slugify(name).upper()) if name else None


def _number_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


def build_state_heatmap_data(dataframe):
    records = {}
    years = set()
    unmatched_records = 0

    for _, row in dataframe.iterrows():
        source_name = str(row.get('State') or '').strip()
        year = _number_or_none(row.get('Year'))
        if not source_name or year is None:
            unmatched_records += 1
            continue

        canonical_name = canonical_state_name(source_name)
        if canonical_name is None:
            unmatched_records += 1
            continue

        geographic_id = state_geographic_id(canonical_name)
        years.add(year)
        values = {column: _number_or_none(row.get(column)) for column in CRIME_COLUMNS}
        all_crimes = (
            sum(values.values())
            if all(value is not None for value in values.values())
            else None
        )

        for category, label in CRIME_CATEGORIES.items():
            value = all_crimes if category == 'All Crimes Against Women' else values[category]
            key = (geographic_id, year, category)
            if key not in records:
                records[key] = {
                    'geo_id': geographic_id,
                    'geo_level': 'state',
                    'parent_geo_id': 'IN',
                    'state': source_name,
                    'geo_name': canonical_name,
                    'geo_state': canonical_name,
                    'year': year,
                    'crime': category,
                    'crime_label': label,
                    'value': value,
                    'source': 'Bundled dataset: CrimesOnWomenData.csv',
                }
            elif value is None or records[key]['value'] is None:
                records[key]['value'] = None
            else:
                records[key]['value'] += value

    return {
        'records': list(records.values()),
        'years': sorted(years),
        'unmatched_records': unmatched_records,
        'geographic_levels': ['state'] if records else [],
    }
