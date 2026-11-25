"""Real local semantic-model tests; do not claim these as human acceptance."""
import unittest
import numpy as np
from semantic import features

class Features(unittest.TestCase):
    def test_bounded_features_and_silence_are_finite(self):
        for size in (0, 16000, 128000, 320000):
            result = features(np.zeros(size, dtype=np.float32))
            self.assertEqual(result.shape, (1, 80, 800))
            self.assertTrue(np.isfinite(result).all())

    def test_retains_the_end_of_long_turns(self):
        rng = np.random.default_rng(42)
        audio = rng.normal(0, 0.1, 160000).astype(np.float32)
        np.testing.assert_array_equal(features(audio), features(audio[-128000:]))

if __name__ == '__main__':
    unittest.main()
