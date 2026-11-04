"""Scientific aggregation and transition checks for Experiment 3."""
import csv
from pathlib import Path
import tempfile
import unittest

import numpy as np

from exp3 import build_pipelines, crossover_brackets, load_measurements, proving_comparison


def pipeline(times):
    times = np.asarray(times, dtype=float)
    stages = ["witness", "proof", "verify"][:times.shape[1]]
    return dict(backend="test", stages=stages, times=times,
                n=2 ** np.arange(len(times)),
                dominant=[tuple(s for s, t in zip(stages, ts) if t == max(ts)) for ts in times])


class Exp3Tests(unittest.TestCase):
    def test_crossing_is_not_necessarily_dominant(self):
        rows = crossover_brackets(pipeline([[2, 1, 10], [1, 2, 10]]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "pairwise_crossing")

    def test_equality_alone_is_not_a_switch(self):
        self.assertEqual(crossover_brackets(pipeline([[2, 1], [1, 1], [2, 1]])), [])
        rows = crossover_brackets(pipeline([[2, 1], [1, 1], [1, 2]]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "dominant_stage_switch")
        self.assertEqual((rows[0]["N_low"], rows[0]["N_high"]), (1, 4))

    def test_failed_and_partial_proving_reps_discarded_together(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            fields = ["size", "prover", "phase", "step", "rep", "ok", "wall_s", "cpu_total_s"]
            rows = []
            # Rep 1: witness=1, proof=3; rep 2: witness=3, proof=1.
            # Their total has SD=0, not sqrt(SD(witness)^2+SD(proof)^2).
            for rep, witness, proof in [(1, 1, 3), (2, 3, 1), (3, 50, 50)]:
                for stage, value in [("witness", witness), ("prove", proof)]:
                    rows.append(dict(size=1, prover="rapidsnark", phase="prove",
                                     step=f"{stage}_rapidsnark", rep=rep,
                                     ok=not (rep == 3 and stage == "prove"), wall_s=value,
                                     cpu_total_s=value))
            rows.append(dict(size=1, prover="rapidsnark", phase="prove",
                             step="witness_rapidsnark", rep=4, ok=True, wall_s=100))
            with (run / "steps.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            data, rejected = load_measurements(run)
            self.assertEqual(len(rejected), 2)
            total = next(r for r in data if r["stage"] == "proving_total")
            self.assertEqual(total["n_reps"], 2)
            self.assertEqual(total["wall_s_mean"], 4)
            self.assertEqual(total["wall_s_stdev"], 0)
            self.assertTrue(np.isnan(total["rss_peak_bytes_mean"]))

    def test_complete_grids_do_not_fill_missing_stages(self):
        rows = [dict(n=n, prover="rapidsnark", stage=s, wall_s_mean=t)
                for n, s, t in [(1, "witness", .1), (1, "proof", 1), (1, "verify", 2),
                                (2, "witness", .2), (2, "proof", 3)]]
        p = build_pipelines(rows)[0]
        np.testing.assert_array_equal(p["n"], [1])
        self.assertEqual(p["excluded_sizes"], [2])

    def test_missing_workflow_cannot_produce_a_rate(self):
        with self.assertRaisesRegex(ValueError, "Incomplete recurring workflow"):
            build_pipelines([dict(n=1, prover="snarkjs", stage="verify", wall_s_mean=1)])

    def test_existing_dataset_phase_vs_proof_crossings(self):
        run = Path(__file__).resolve().parents[2] / "bench-out/20260825_120620"
        rows, rejected = load_measurements(run)
        self.assertFalse(rejected)
        pipelines = build_pipelines(rows)
        rapid = next(p for p in pipelines if p["backend"] == "rapidsnark")
        switch = next(r for r in crossover_brackets(rapid) if r["kind"] == "dominant_stage_switch")
        self.assertEqual((switch["stage_before"], switch["stage_after"]), ("verify", "proof"))
        self.assertEqual((switch["N_low"], switch["N_high"]), (1024, 2048))
        with tempfile.TemporaryDirectory() as directory:
            _, transitions = proving_comparison(pipelines, Path(directory), 1e-6)
        switch = next(r for r in transitions if r["prover"] == "rapidsnark")
        self.assertEqual((switch["N_low"], switch["N_high"]), (512, 1024))
        self.assertEqual(len(rapid["n"]), 14)


if __name__ == "__main__":
    unittest.main()
