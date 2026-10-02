"""Run:  python -m unittest tests.test_sampling -v   (no database needed)"""
import collections
import unittest

import sampling as S

POP = [{"id": i,
        "status": "Completed" if i % 10 < 6 else "Pending" if i % 10 < 9 else "Cancelled",
        "city": ["Solapur", "Pune", "Mumbai", "Nashik"][i % 4],
        "links": {f"u:{i % 7}", f"n:{i % 5}"}} for i in range(1, 201)]


class SamplingTests(unittest.TestCase):
    def test_simple_random_unique_and_reproducible(self):
        a = S.simple_random(POP, 20, seed=1)
        self.assertEqual(a.sample_size, 20)
        self.assertEqual(len({r["id"] for r in a.sample}), 20)
        b = S.simple_random(POP, 20, seed=1)
        self.assertEqual([r["id"] for r in a.sample], [r["id"] for r in b.sample])

    def test_systematic_constant_interval(self):
        ids = [r["id"] for r in S.systematic(POP, 20, seed=3).sample]
        self.assertEqual(len({b - a for a, b in zip(ids, ids[1:])}), 1)
        self.assertEqual(ids[1] - ids[0], 10)

    def test_stratified_proportional(self):
        r = S.stratified(POP, 20, "status", seed=4)
        got = {g["name"]: g["sampled"] for g in r.groups}
        self.assertEqual(got, {"Completed": 12, "Pending": 6, "Cancelled": 2})
        self.assertEqual(r.sample_size, 20)

    def test_stratified_every_group_represented(self):
        r = S.stratified(POP, 5, "city", seed=1)
        self.assertEqual(r.sample_size, 5)
        self.assertTrue(all(g["sampled"] >= 1 for g in r.groups))

    def test_cluster_takes_whole_groups(self):
        r = S.cluster(POP, 2, "city", seed=5)
        self.assertEqual(len({x["city"] for x in r.sample}), 2)
        self.assertEqual(r.sample_size, 100)
        with self.assertRaises(S.SamplingError):
            S.cluster(POP, 9, "city")

    def test_convenience_takes_first_records(self):
        self.assertEqual([r["id"] for r in S.convenience(POP, 7).sample], list(range(1, 8)))

    def test_quota_filled_and_shortfall_reported(self):
        r = S.quota(POP, "status", "Completed=5, Pending=3")
        self.assertEqual(collections.Counter(x["status"] for x in r.sample),
                         {"Completed": 5, "Pending": 3})
        short = S.quota(POP, "status", "cancelled=50")
        self.assertEqual(short.sample_size, 20)
        self.assertIn("not fully met", " ".join(short.details))

    def test_quota_text_rejected(self):
        for bad in ["Completed", "Completed=abc", "=5", "Completed=0", ""]:
            with self.assertRaises(S.SamplingError):
                S.parse_quotas(bad)

    def test_snowball_follows_connections(self):
        r = S.snowball(POP, 15, seeds=[1])
        self.assertEqual(r.sample_size, 15)
        self.assertEqual(r.sample[0]["id"], 1)
        iso = [{"id": 1, "links": {"a"}}, {"id": 2, "links": {"a"}}, {"id": 3, "links": {"z"}}]
        self.assertEqual(S.snowball(iso, 3, seeds=[1]).sample_size, 2)

    def test_bad_sizes_rejected(self):
        for n in (0, -1, 201):
            with self.assertRaises(S.SamplingError):
                S.simple_random(POP, n)
        with self.assertRaises(S.SamplingError):
            S.simple_random([], 5)


if __name__ == "__main__":
    unittest.main()
