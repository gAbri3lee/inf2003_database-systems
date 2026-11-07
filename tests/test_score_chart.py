"""Presentation must reflect the existing score, even with custom priorities."""
from html.parser import HTMLParser
import unittest

import pandas as pd

from ui import find_flat


class ChartContent(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.labels = []
        self.text = []
        self.descriptions = []
        self._description = None
        self.calculations = []
        self._calculation = None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('role') == 'img':
            self.labels.append(attrs.get('aria-label', ''))
        if tag == 'dd':
            self._description = []
        if tag == 'div' and attrs.get('class') == 'hw-score-calculation':
            self._calculation = []

    def handle_endtag(self, tag):
        if tag == 'div' and self._calculation is not None:
            self.calculations.append(' '.join(self._calculation))
            self._calculation = None
        if tag == 'dd' and self._description is not None:
            self.descriptions.append(' '.join(self._description))
            self._description = None

    def handle_data(self, data):
        self.text.append(data)
        if self._description is not None:
            self._description.append(data)
        if self._calculation is not None:
            self._calculation.append(data)


class ScoreChartTests(unittest.TestCase):
    def test_overview_shows_existing_weights_and_normalised_maxima(self):
        chart = ChartContent(find_flat._ff_score_chart(find_flat.DEFAULT_WEIGHTS))
        self.assertIn('weight 70; maximum 22.6 points', chart.labels[1])
        self.assertIn('weight 60; maximum 19.4 points', chart.labels[2])
        text = ' '.join(chart.text)
        for number in ['22.6', '19.4', '16.1', '12.9', '9.7', '6.5']:
            self.assertIn(number, text)
        self.assertIn('310', text)
        self.assertEqual(chart.calculations, [])

    def test_selected_flat_uses_existing_percentiles_without_rescoring(self):
        row = pd.Series(dict(score=67.6, flat_type='5 ROOM', resale_price=600000,
            distance_to_mrt_m=292, nearest_mrt='Admiralty MRT',
            distance_to_pri_school_m=111, nearest_school='Greenwood Primary',
            hawker_distance_m=343, nearest_hawker='Kampung Admiralty',
            park_distance_m=1980, nearest_park='Sembawang Alley',
            distance_to_cbd_m=18000, remaining_lease_months=864,
            s_afford=92.6, s_mrt=74.9, s_school=97.7, s_hawker=76.7,
            s_park=1.0, s_cbd=13.9, s_lease=53.9))
        before = row.copy()
        chart = ChartContent(find_flat._ff_score_chart(find_flat.DEFAULT_WEIGHTS, row))
        self.assertIn('Affordability: earned 20.9 of 22.6 points; weight 70', chart.labels)
        self.assertIn('MRT access: earned 14.5 of 19.4 points; weight 60', chart.labels)
        self.assertIn('Green space: earned 0.1 of 9.7 points; weight 30', chart.labels)
        self.assertIn('67.6', ' '.join(chart.text))
        self.assertEqual(len(chart.calculations), 1)
        self.assertIn('Maximum points = weight ÷ total weight × 100', chart.calculations[0])
        self.assertIn('Earned points = percentile score × weight ÷ total weight', chart.calculations[0])
        # Each category explains its actual earnings below its maximum calculation.
        for category, score, weight, earned, maximum in [
            ('Affordability', 92.6, 70, 20.9, 22.6), ('MRT access', 74.9, 60, 14.5, 19.4),
            ('Primary school', 97.7, 40, 12.6, 12.9), ('Hawker food', 76.7, 40, 9.9, 12.9),
            ('Green space', 1.0, 30, 0.1, 9.7), ('City centre', 13.9, 20, 0.9, 6.5),
            ('Lease left', 53.9, 50, 8.7, 16.1),
        ]:
            calculation = f'{score:.1f} × {weight} ÷ 310 = {earned:.1f} earned points.'
            matches = [text for text in chart.descriptions if calculation in text]
            self.assertEqual(len(matches), 1, calculation)
            self.assertLess(matches[0].index('maximum points.'), matches[0].index(calculation))
            self.assertIn(f'This flat earns {earned:.1f} / {maximum:.1f} points for {category}.', matches[0])
        pd.testing.assert_series_equal(row, before)

    def test_selected_calculation_box_uses_the_actual_search_weight_total(self):
        weights = {**find_flat.DEFAULT_WEIGHTS, 'lease': 35}
        row = pd.Series(dict(score=20.3, distance_to_mrt_m=435, nearest_mrt='Mattar MRT',
                             **{'s_' + key: 99.7 if key == 'mrt' else 0.0
                                for key in find_flat.FACTORS}))
        chart = ChartContent(find_flat._ff_score_chart(weights, row))
        self.assertEqual(len(chart.calculations), 1)
        self.assertIn('60 ÷ 295 × 100 = 20.3 maximum points.', chart.calculations[0])
        self.assertIn('99.7 × 60 ÷ 295 = 20.3 earned points.', chart.calculations[0])

    def test_custom_weights_and_ties_are_explained_from_actual_values(self):
        weights = dict.fromkeys(find_flat.FACTORS, 0)
        weights.update(afford=25, mrt=25, school=50)
        before = weights.copy()
        chart = ChartContent(find_flat._ff_score_chart(weights))
        self.assertIn('Primary school: weight 50; maximum 50.0 points', chart.labels)
        self.assertIn('Affordability: weight 25; maximum 25.0 points', chart.labels)
        self.assertIn('MRT access: weight 25; maximum 25.0 points', chart.labels)
        self.assertEqual(weights, before)

    def test_zero_weight_is_excluded_even_with_a_high_percentile(self):
        weights = dict.fromkeys(find_flat.FACTORS, 0)
        weights['mrt'] = 10
        row = pd.Series(dict(score=0, s_mrt=0, s_afford=100, distance_to_mrt_m=None))
        chart = ChartContent(find_flat._ff_score_chart(weights, row))
        self.assertIn('MRT access: earned 0.0 of 100.0 points; weight 10', chart.labels)
        self.assertIn('Affordability: excluded; weight 0', chart.labels)
        self.assertIn('no data for this flat', ' '.join(chart.text))
        self.assertTrue(any('This flat earns 0.0 / 0.0 points for Affordability.' in text
                            for text in chart.descriptions))

    def test_all_zero_weights_do_not_divide_by_zero_or_show_a_fake_total(self):
        chart = ChartContent(find_flat._ff_score_chart(dict.fromkeys(find_flat.FACTORS, 0)))
        self.assertIn('Set at least one priority above 0', ' '.join(chart.text))
        self.assertNotIn('100 maximum points', chart.labels)

    def test_metric_explanations_escape_data_before_rendering_html(self):
        weights = dict.fromkeys(find_flat.FACTORS, 0)
        weights['mrt'] = 60
        row = pd.Series(dict(score=75, s_mrt=75, distance_to_mrt_m=300,
                             nearest_mrt='<script>alert(1)</script> & MRT'))
        source = find_flat._ff_score_chart(weights, row)
        self.assertNotIn('<script>', source)
        self.assertIn('&lt;script&gt;', source)
        self.assertIn('&amp; MRT', source)


if __name__ == '__main__':
    unittest.main()
