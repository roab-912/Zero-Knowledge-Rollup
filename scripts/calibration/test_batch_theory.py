import unittest

import numpy as np

from scripts.calibration import batch_theory as theory


class TheoryTests(unittest.TestCase):
    def test_affine_elasticity_and_scheduled_kink(self):
        model = {"family": "affine", "alpha_s": 2., "beta_s_per_tx": 1., "c": 0., "p": None}
        q = theory.quantities(model, [1, 2, 8, 16], 10.)
        np.testing.assert_allclose(q["xi"], [2/3, 1/2, 1/5, 1/9])
        np.testing.assert_allclose(q["gamma"] + q["xi"], 1.)
        self.assertEqual(q["scheduled_xi"][:3], [1., 1., None])
        self.assertAlmostEqual(q["scheduled_xi"][3], 1/9)
        np.testing.assert_allclose(q["scheduled_tps"], [.1, .2, .8, 16/18])

    def test_derivative_matches_logarithmic_difference(self):
        for family, c, p in (("affine", 0., None), ("nlogn", .003, None), ("power", .004, 1.75)):
            model = {"family": family, "alpha_s": .7, "beta_s_per_tx": .002, "c": c, "p": p}
            q = theory.quantities(model, [32.], 10.)
            times = theory.quantities(model, [32*np.exp(-1e-5), 32*np.exp(1e-5)], 10.)["time_s"]
            gamma = np.diff(np.log(times))[0] / 2e-5
            self.assertAlmostEqual(q["gamma"][0], gamma, places=8)

    def test_deadline_filters_optimum_and_cadence_changes_objective(self):
        # Processing capacity peaks at N=2; fixed cadence favors N=8.
        self.assertEqual(theory.optimum([1, 2, 4, 8], [1, 1, 3, 9], 10), 2)
        self.assertEqual(theory.optimum([1, 2, 4, 8], [1, 1, 3, 9], 10, "cadence"), 8)
        self.assertEqual(theory.optimum([1, 2, 4, 8], [1, 1, 3, 9], 2, "cadence"), 2)
        self.assertIsNone(theory.optimum([1, 2], [3, 4], 2))

    @staticmethod
    def documents():
        grid = [1, 2, 4, 8, 16, 32, 64, 128]
        fit_sizes = grid[::2]
        model = {"family": "affine", "alpha_s": .2, "beta_s_per_tx": .01, "c": 0., "p": None}
        def doc(sizes, factor):
            return {"trials": [{"n": n, "prover": "snarkjs", "warmup": False, "status": "success",
                                "id": f"{n}-{i}", "proof_generation_s": factor*(.2+.01*n),
                                "total_s": factor*(.2+.01*n), "verification_s": .1}
                               for n in sizes for i in range(3)]}
        frozen = {"calibration_sizes": fit_sizes,
                  "models": {"snarkjs": {"proof_generation_s": {"model": model, "candidates": []}}}}
        return grid, doc(fit_sizes, 1.), doc(grid, 1.), frozen

    def test_validation_never_changes_frozen_model_or_recommendation(self):
        grid, calibration, validation, frozen = self.documents()
        for r in validation["trials"]:
            if r["n"] == 128:
                r["proof_generation_s"] *= 2
        result = theory.analyse_scope(calibration, validation, frozen, "snarkjs", "proof_generation_s",
                                      grid, 10., "deadline", 50, 1)
        self.assertEqual(result["model"], frozen["models"]["snarkjs"]["proof_generation_s"]["model"])
        self.assertEqual(result["optimal_batch_predicted"], 128)
        self.assertEqual(result["optimal_batch_observed"], 64)
        self.assertGreater(result["recommendation_loss"], 0)
        self.assertEqual(result["metrics"]["held_out_sizes"]["capacity_tps"]["size_count"], 4)
        self.assertAlmostEqual(result["metrics"]["held_out_sizes"]["capacity_tps"]["mape_percent"], 25.)

    def test_failed_attempt_excludes_empirical_admissibility(self):
        grid, calibration, validation, frozen = self.documents()
        validation["trials"][-1]["status"] = "timeout"
        result = theory.analyse_scope(calibration, validation, frozen, "snarkjs", "proof_generation_s",
                                      grid, 10., "deadline", 50, 1)
        self.assertEqual(result["optimal_batch_predicted"], 128)
        self.assertEqual(result["max_batch_all_observed_attempts_within_deadline"], 64)
        self.assertIsNone(result["recommendation_loss"])
        self.assertFalse(result["all_validation_attempts_successful_within_deadline"])
        self.assertEqual(result["rows"][-1]["observed"]["repetitions"], 2)

    def test_secants_compare_same_operator_and_local_scope_is_paired(self):
        grid, calibration, validation, frozen = self.documents()
        result = theory.analyse_scope(calibration, validation, frozen, "snarkjs", "proof_generation_s",
                                      grid, 10., "deadline", 50, 1)
        self.assertAlmostEqual(result["elasticity_secant_mae"], 0.)
        local = theory.grouped_times(validation, "snarkjs", "local_verified_s")
        self.assertAlmostEqual(local[128][0], 1.58)

    def test_local_extension_uses_only_calibration(self):
        grid, calibration, validation, frozen = self.documents()
        for row in validation["trials"]:
            row["total_s"] *= 100
        result = theory.analyse_scope(calibration, validation, frozen, "snarkjs", "local_verified_s",
                                      grid, 10., "deadline", 50, 1)
        self.assertAlmostEqual(result["model"]["alpha_s"], .3)
        self.assertAlmostEqual(result["model"]["beta_s_per_tx"], .01)
        self.assertIn("retrospective", result["model_origin"])


if __name__ == "__main__":
    unittest.main()
