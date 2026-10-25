import json
import unittest
from decimal import Decimal

from earnings_consensus_telegram import parse_rows


class ConsensusRowsTests(unittest.TestCase):
    def test_missing_target_prices_are_excluded_in_both_directions(self):
        for direction, sign in (("up", 1), ("down", -1)):
            with self.subTest(direction=direction):
                rows = [
                    ["000001", "000001|목표가있음", "4.00", "10,000", "20",
                     "1,200", "1,000", str(sign * 2), "2026/12", "연결"],
                ]
                for target in ("", " \t", None):
                    row = rows[0].copy()
                    row[1] = "000002|목표가없음"
                    row[3] = target
                    row[7] = str(sign * 50)
                    rows.append(row)
                below_threshold = rows[0].copy()
                below_threshold[7] = str(sign * 0.99)
                rows.append(below_threshold)
                data = [{"id": "TB0"}, {"id": "TB1", "dest": [rows, rows]},
                        {"id": "TB2"}, {"id": "TB3"}]
                html = "var changeTnd = " + json.dumps(data) + ";"
                self.assertEqual(parse_rows(html, direction), [
                    ("목표가있음", Decimal("1200"), Decimal(sign * 2)),
                ])


if __name__ == "__main__":
    unittest.main()
