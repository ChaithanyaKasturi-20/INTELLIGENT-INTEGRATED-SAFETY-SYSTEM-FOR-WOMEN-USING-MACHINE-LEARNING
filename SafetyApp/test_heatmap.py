import json

import pandas as pd
from django.test import TestCase

from .heatmap_data import build_state_heatmap_data


class HeatmapTestCase(TestCase):
    def test_heatmap_uses_real_state_totals_and_discloses_their_scope(self):
        response = self.client.get('/Heatmap')

        self.assertEqual(response.status_code, 200)
        map_data = json.loads(response.context['map_data'])
        andhra_pradesh_2021 = next(
            item for item in map_data
            if item['geo_state'] == 'Andhra Pradesh'
            and item['year'] == 2021
            and item['crime'] == 'All Crimes Against Women'
        )

        self.assertEqual(andhra_pradesh_2021['value'], 16549)
        self.assertEqual(andhra_pradesh_2021['geo_id'], 'IN-STATE-ANDHRA-PRADESH')
        self.assertEqual(andhra_pradesh_2021['geo_level'], 'state')
        self.assertEqual(andhra_pradesh_2021['parent_geo_id'], 'IN')
        self.assertIn('CrimesOnWomenData.csv', andhra_pradesh_2021['source'])
        self.assertNotIn('district_data', response.context)
        self.assertEqual(json.loads(response.context['geographic_levels']), ['state'])
        self.assertContains(response, 'State totals as a share of the highest reported value')
        self.assertContains(response, 'not local crime probability')
        self.assertContains(response, 'District and local-area crime data is not available')
        self.assertNotContains(response, 'Nearby crime density layer')

    def test_map_uses_transparent_fill_stable_region_ids_and_explicit_scope(self):
        response = self.client.get('/Heatmap')
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn('fillOpacity: value === null ? 0.04 : 0.2', content)
        self.assertIn('weight: 1.15', content)
        self.assertIn('opacity: 0.88', content)
        self.assertIn('dataByGeography[stateGeoId(name)]', content)
        self.assertIn('renderer: boundaryRenderer', content)
        self.assertIn('layer.openPopup(event.latlng)', content)
        self.assertIn('0–19%', content)
        self.assertIn('20–39%', content)
        self.assertIn('40–59%', content)
        self.assertIn('60–79%', content)
        self.assertIn('80–100%', content)
        self.assertIn('useMyLocationBtn.addEventListener(\'click\', handleUseMyLocation)', content)
        self.assertIn('maxZoom: 18', content)
        self.assertIn('handleUseMyLocation();', content)
        self.assertIn('Location permission was denied.', content)
        self.assertIn('state-level crime map remains available', content)

    def test_missing_counts_stay_unreported_and_unknown_regions_are_not_mapped(self):
        dataframe = pd.DataFrame([
            {
                'State': 'ANDHRA PRADESH',
                'Year': 2020,
                'Rape': None,
                'K&A': 2,
                'DD': 1,
                'AoW': 3,
                'AoM': 4,
                'DV': 5,
                'WT': 6,
            },
            {
                'State': 'Unknown District',
                'Year': 2020,
                'Rape': 9,
                'K&A': 0,
                'DD': 0,
                'AoW': 0,
                'AoM': 0,
                'DV': 0,
                'WT': 0,
            },
        ])

        result = build_state_heatmap_data(dataframe)
        rape = next(
            item for item in result['records']
            if item['crime'] == 'Rape'
        )
        total = next(
            item for item in result['records']
            if item['crime'] == 'All Crimes Against Women'
        )

        self.assertIsNone(rape['value'])
        self.assertIsNone(total['value'])
        self.assertEqual(result['unmatched_records'], 1)
        self.assertEqual(result['geographic_levels'], ['state'])
        self.assertEqual(len(result['records']), 8)
