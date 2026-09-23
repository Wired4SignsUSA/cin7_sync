import unittest

from engine.reorder_math import round_to_pack_nearest as r


class PackRoundingTests(unittest.TestCase):
    def test_rounds_down_to_nearest_roll(self):
        self.assertEqual(r(60, 50), 50)

    def test_rounds_up_to_nearest_roll(self):
        self.assertEqual(r(80, 50), 100)

    def test_half_rounds_up(self):
        self.assertEqual(r(75, 50), 100)

    def test_never_zero(self):
        self.assertEqual(r(10, 50), 50)

    def test_floor_forces_round_up(self):
        # nearest would be 50 but lead-time+safety still needs 55
        self.assertEqual(r(60, 50, min_qty=55), 100)

    def test_floor_satisfied_keeps_round_down(self):
        self.assertEqual(r(60, 50, min_qty=45), 50)

    def test_exact_multiple_unchanged(self):
        self.assertEqual(r(100, 50), 100)

    def test_no_pack_or_no_qty_passthrough(self):
        self.assertEqual(r(37, 0), 37)
        self.assertEqual(r(0, 50), 0)


if __name__ == "__main__":
    unittest.main()
