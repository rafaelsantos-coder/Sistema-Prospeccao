import math
import unittest

from app.services.seller_feasibility import coordinates, EARTH_METERS, nearest_available


def box(identifier, meters, *, free=1, status="Online"):
    return dict(id=identifier, nome=f"NAP-{identifier}", original_lat=math.degrees(meters / EARTH_METERS),
                original_lng=0, lat=0, lng=0, livres=free, status=status)


class FiberAvailabilityTest(unittest.TestCase):
    def test_nearest_eligible_with_deterministic_tie(self):
        rows = [box(1, 10, free=0), box(2, 15, status="Offline"), box(4, 25), box(3, 25), box(5, 500)]
        self.assertEqual(nearest_available(rows, (0, 0))["id"], 3)

    def test_radius_inclusive_before_rounding(self):
        self.assertIsNotNone(nearest_available([box(1, 800)], (0, 0)))
        self.assertIsNone(nearest_available([box(1, 800.01)], (0, 0)))
        self.assertIsNone(nearest_available([], (0, 0)))

    def test_pin_and_address_number(self):
        self.assertEqual(coordinates("https://www.google.com/maps?q=-27.870000,-54.480000"), (-27.87, -54.48))
        self.assertIsNone(coordinates("Rua Santa Rosa, 800"))
        self.assertIsNone(coordinates("-91.0,-54.0"))
