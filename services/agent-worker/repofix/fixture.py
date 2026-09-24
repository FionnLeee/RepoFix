SOURCE = """def discounted_total(price, quantity, discount):
    return price * quantity - discount
"""

FIXED = """def discounted_total(price, quantity, discount):
    if not 0 <= discount <= 100:
        raise ValueError("discount must be between 0 and 100")
    return round(price * quantity * (1 - discount / 100), 2)
"""

DEVELOPMENT_TESTS = """import unittest
from pricing import discounted_total

class PricingTests(unittest.TestCase):
    def test_percentage(self):
        self.assertEqual(discounted_total(100, 2, 20), 160)
    def test_invalid(self):
        with self.assertRaises(ValueError):
            discounted_total(10, 1, -1)

if __name__ == "__main__":
    unittest.main()
"""

VERIFICATION_TESTS = """import unittest
from pricing import discounted_total

class IndependentTests(unittest.TestCase):
    def test_percentage(self):
        self.assertEqual(discounted_total(100, 2, 20), 160)
    def test_zero_discount(self):
        self.assertEqual(discounted_total(17, 3, 0), 51)
    def test_full_discount(self):
        self.assertEqual(discounted_total(17, 3, 100), 0)
    def test_rounding(self):
        self.assertEqual(discounted_total(19.99, 3, 15), 50.97)
    def test_negative_discount(self):
        with self.assertRaises(ValueError):
            discounted_total(10, 1, -1)
    def test_excess_discount(self):
        with self.assertRaises(ValueError):
            discounted_total(10, 1, 101)

if __name__ == "__main__":
    unittest.main()
"""
