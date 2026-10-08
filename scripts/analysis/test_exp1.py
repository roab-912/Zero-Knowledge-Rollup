"""Vérifications des identités et du domaine admissible de l'Exp 1."""
import unittest
import csv
from pathlib import Path

import numpy as np

from exp1 import load_series, log_slope, observed_optima


class Exp1Tests(unittest.TestCase):
    def test_power_law_on_irregular_grid(self):
        n = np.array([1, 3, 8, 32, 100])
        np.testing.assert_allclose(log_slope(n, 7 * n ** 1.7), 1.7)

    def test_variable_resource_changes_throughput_elasticity(self):
        n = np.array([1, 2, 4, 8, 16])
        work, capacity = 3 * n ** 1.2, n ** .5
        eta = log_slope(n, n / (work / capacity))
        xi = 1 - log_slope(n, work)
        np.testing.assert_allclose(eta, xi + log_slope(n, capacity))
        self.assertTrue(np.all(xi < 0))
        self.assertTrue(np.all(eta > 0))

    def test_missing_work_is_not_zero(self):
        self.assertTrue(np.all(np.isnan(log_slope([1, 2], [0, 1]))))

    def test_discrete_optimum_and_empty_domain(self):
        s = dict(phase="prove", prover="test", n=np.array([1, 2, 8]),
                 t=np.array([1., 1.5, 10.]), rss_max=np.array([1., 2., 3.]))
        s["throughput"] = s["n"] / s["t"]
        self.assertEqual(observed_optima([s], None, None)[0]["N_best_measured"], 2)
        fixed = observed_optima([s], 2., None)[0]
        self.assertEqual(fixed["N_best_measured"], 2)
        self.assertEqual(fixed["throughput_tx_s"], 1.)
        self.assertEqual(observed_optima([s], .5, None)[0]["N_best_measured"], "")

    def test_complete_dataset_identities(self):
        run = Path(__file__).resolve().parents[2] / "bench-out/20260825_120620"
        series = load_series(run, ["prove", "verify", "env"])
        self.assertEqual(len(series), 4)
        for s in series:
            self.assertEqual(len(s["n"]), 14)
            np.testing.assert_allclose(s["w"] / s["r_eff"], s["t"])
            np.testing.assert_allclose(s["eta"], s["xi_w"] + s["eta_r"], atol=1e-12)

    def test_verify_statistics_from_all_raw_repetitions(self):
        run = Path(__file__).resolve().parents[2] / "bench-out/20260825_120620"
        series = load_series(run, ["verify"])
        self.assertEqual(len(series), 1)
        s = series[0]
        self.assertEqual(s["prover"], "")
        np.testing.assert_array_equal(s["n_reps"], 20)
        with (run / "steps.csv").open(encoding="utf-8", newline="") as f:
            raw = [r for r in csv.DictReader(f) if r["phase"] == "verify" and r["ok"] == "True"]
        for i, n in enumerate(s["n"]):
            rows = [r for r in raw if int(r["size"]) == n]
            for metric, mean_key, sd_key in [("wall_s", "t", "wall_sd"),
                                             ("cpu_total_s", "w", "cpu_sd")]:
                values = [float(r[metric]) for r in rows]
                self.assertAlmostEqual(s[mean_key][i], np.mean(values))
                self.assertAlmostEqual(s[sd_key][i], np.std(values))
            self.assertEqual(s["rss_max"][i], max(float(r["rss_peak_bytes"]) for r in rows))


if __name__ == "__main__":
    unittest.main()
