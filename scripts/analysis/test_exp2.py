"""Numerical model recovery and protection against validation leakage."""
import unittest

import numpy as np

from exp2 import TimeModel, calibrate, calibration_mask, evaluate, evaluate_intervals, fit_time, optimum_comparison
from exp1 import log_slope


class Exp2Tests(unittest.TestCase):
    def test_interval_elasticity_uses_same_estimator_and_detects_trend(self):
        n = np.array([1, 2, 8, 32, 64, 128])
        model = TimeModel("affine", 1., .01, 0., 2., 0.)
        s = dict(n=n, throughput=model.throughput(n), phase="prove", prover="test",
                 calibration_mask=np.array([True, True, False, False, True, False]))
        rows, scores = evaluate_intervals(s, {model.name: model}, 64)
        np.testing.assert_allclose([r["gamma_interval_error"] for r in rows], 0, atol=1e-12)
        self.assertEqual([r["split"] for r in rows], ["train", "mixed", "heldout", "mixed", "mixed"])
        extra = [r for r in scores if r["validation_kind"] == "extrapolation"]
        self.assertEqual(len(extra), 1)
        self.assertEqual(extra[0]["split"], "mixed")
        self.assertEqual(extra[0]["n_intervals"], 1)
        # Force an observed drop where the model predicts a gain.
        s["throughput"][-1] = .8 * s["throughput"][-2]
        rows, _ = evaluate_intervals(s, {model.name: model}, 64)
        last = rows[-1]
        self.assertLess(last["gamma_observed_interval"], 0)
        self.assertGreater(last["gamma_predicted_interval"], 0)
        self.assertAlmostEqual(last["gamma_interval_error"],
                               np.log(last["gain_predicted"] / .8) / np.log(2))

    def test_gain_scores_do_not_hide_level_bias_in_point_scores(self):
        n = 2. ** np.arange(6)
        model = TimeModel("affine", 1., .01, 0., 2., 0.)
        # Identical shape, but the model overestimates throughput by 25%.
        s = dict(n=n, t=1.25 * model.time(n), throughput=model.throughput(n) / 1.25,
                 phase="prove", prover="test")
        _, scores, _ = evaluate(s, {model.name: model}, 8, tolerance=.05)
        validation = next(r for r in scores if r["split"] == "validation")
        self.assertAlmostEqual(validation["throughput_MAPE_pct"], 25)
        self.assertEqual(validation["throughput_within_tolerance_pct"], 0)
        rows, _ = evaluate_intervals(s, {model.name: model}, 8)
        np.testing.assert_allclose([r["gain_error_pct"] for r in rows], 0, atol=1e-12)

    def test_near_optimal_choice_uses_throughput_loss_not_size_distance(self):
        model = TimeModel("affine", 1., 1., 0., 2., 0.)
        s = dict(n=np.array([1, 2, 4, 8, 16]), throughput=np.array([20, 50, 100, 95, 94]),
                 phase="prove", prover="test")
        result = optimum_comparison(s, model, tolerance=.05)
        self.assertEqual(result["predicted_best_tested_N"], 16)
        self.assertAlmostEqual(result["throughput_regret_pct"], 6)
        self.assertEqual(result["observed_near_optimal_sizes"], "4 8")
        self.assertFalse(result["recommendation_within_tolerance"])
        self.assertTrue(optimum_comparison(s, model, tolerance=.1)["recommendation_within_tolerance"])

    def test_sparse_designs_and_validation_roles(self):
        n = 2 ** np.arange(14)
        log = calibration_mask(n, 4096, "logspace", 6)
        large = calibration_mask(n, 4096, "large", 6)
        self.assertEqual(log.sum(), 6)
        self.assertEqual(large.sum(), 6)
        self.assertTrue(log[0] and log[12] and not log[13])
        np.testing.assert_array_equal(n[large], [1, 256, 512, 1024, 2048, 4096])
        s = dict(n=n, t=1. + .03*n, phase="prove", prover="test", calibration_mask=large)
        s["throughput"] = n / s["t"]
        first = calibrate(s, 4096, 2.)
        changed = {**s, "t": s["t"].copy()}
        changed["t"][~large] *= 100
        self.assertEqual(first, calibrate(changed, 4096, 2.))
        points, scores, _ = evaluate(s, first, 4096)
        rows = [r for r in scores if r["model"] == "affine"]
        self.assertEqual({r["split"]: r["n_sizes"] for r in rows},
                         dict(train=6, validation=8, interpolation=7, extrapolation=1))
        self.assertTrue(all(r["split"] == "validation" for r in points if r["n"] == 2))

    def test_manual_plan_validation(self):
        n = 2 ** np.arange(14)
        chosen = [1, 16, 256, 1024, 4096]
        np.testing.assert_array_equal(n[calibration_mask(n, 4096, "manual", sizes=chosen)], chosen)
        for invalid in ([1, 16, 256, 3000], [1, 16, 256, 8192], [1, 16, 256, 256]):
            with self.assertRaises(ValueError):
                calibration_mask(n, 4096, "manual", sizes=invalid)
        with self.assertRaises(ValueError):
            calibration_mask(n, 256, "large", count=12)

    def test_exact_affine_recovery(self):
        n = 2. ** np.arange(11)
        model = fit_time(n, 2. + .03 * n, "affine")
        np.testing.assert_allclose(model.time(n), 2. + .03 * n, rtol=1e-6)
        np.testing.assert_allclose(model.elasticity(n), 2. / (2. + .03 * n), rtol=1e-6)
        self.assertIsNone(model.intrinsic_optimum())

    def test_superlinear_recovery_and_optimum(self):
        n = 2. ** np.arange(11)
        truth = TimeModel("superlinear", 2., .02, .0002, 2., 0.)
        model = fit_time(n, truth.time(n), "superlinear")
        np.testing.assert_allclose(model.throughput(n), truth.throughput(n), rtol=1e-5)
        self.assertAlmostEqual(model.intrinsic_optimum(), 100., places=3)
        self.assertGreater(model.elasticity(50), 0)
        self.assertLess(model.elasticity(200), 0)

    def test_analytic_derivative(self):
        model = TimeModel("superlinear", 1., .02, .0003, 1.6, 0.)
        n = np.geomspace(1, 8192, 1001)
        np.testing.assert_allclose(model.elasticity(n)[1:-1],
                                   log_slope(n, model.throughput(n))[1:-1], atol=2e-5)

    def test_held_out_values_cannot_change_fit(self):
        n = 2. ** np.arange(14)
        s = dict(n=n, t=1. + .03 * n, phase="prove", prover="synthetic")
        first = calibrate(s, 1024, 2.)
        changed = {**s, "t": s["t"].copy()}
        changed["t"][n > 1024] *= 100
        second = calibrate(changed, 1024, 2.)
        for name in first:
            self.assertEqual(first[name], second[name])

    def test_optimum_on_measured_grid_and_regret(self):
        model = TimeModel("superlinear", 2., .02, .0002, 2., 0.)
        n = np.array([1, 32, 64, 128, 256])
        s = dict(n=n, throughput=model.throughput(n), phase="prove", prover="test")
        result = optimum_comparison(s, model)
        self.assertEqual(result["integer_best_in_range"], 100)
        self.assertEqual(result["predicted_best_tested_N"], 128)
        self.assertEqual(result["throughput_regret_pct"], 0)
        decreasing = TimeModel("superlinear", 0., 1., 1., 2., 0.)
        self.assertEqual(optimum_comparison(s, decreasing)["integer_best_in_range"], 1)

    def test_validation_scores_and_invalid_split(self):
        n = 2. ** np.arange(14)
        s = dict(n=n, t=1. + .03 * n, phase="prove", prover="test")
        s["throughput"] = n / s["t"]
        models = calibrate(s, 1024, 2.)
        points, metrics, _ = evaluate(s, models, 1024)
        validation = [r for r in metrics if r["split"] == "validation"]
        self.assertEqual(len(validation), 2)
        self.assertEqual(validation[0]["n_sizes"], 3)
        self.assertLess(validation[0]["throughput_MAPE_pct"], 1e-5)
        self.assertEqual(sum(p["split"] == "validation" for p in points), 6)
        single = calibrate(s, 4096, 2.)
        _, single_scores, _ = evaluate(s, single, 4096)
        self.assertEqual([r["n_sizes"] for r in single_scores if r["split"] == "validation"], [1, 1])
        with self.assertRaises(ValueError):
            calibrate(s, 8192, 2.)
        with self.assertRaises(ValueError):
            calibrate(s, 4, 2.)


if __name__ == "__main__":
    unittest.main()
