"""Run: python -m unittest discover -s scripts/bench -p test_memory_stats.py"""
import json
import unittest
import tempfile
import os
import csv
from unittest.mock import Mock, patch

from memory_stats import memory_intervals, summarize_memory
from measure_zk_resources import StepResult, Sample, Runner, aggregate, write_outputs


def step(rep, cells, ok=True, name="prove"):
    s = StepResult(1, "prove", name, "prove", rep, "test", "command", ok, 0,
                   "process", wall_s=sum(w for _, w in cells),
                   rss_peak_bytes=max((v for v, _ in cells), default=0))
    s._rss_intervals = cells
    s.rss_measurement_status = "ok" if cells else "unavailable"
    for key, value in summarize_memory(cells).items():
        setattr(s, key, value)
    return s


class MemoryTests(unittest.TestCase):
    def test_duration_weighting_and_quantiles(self):
        stats = summarize_memory([(10, 9), (100, 1)])
        self.assertEqual(stats["rss_mean_bytes"], 19)
        self.assertEqual(stats["rss_median_bytes"], 10)
        self.assertEqual(stats["rss_p95_bytes"], 100)
        self.assertEqual(stats["rss_integral_byte_s"], 190)
        self.assertAlmostEqual(stats["rss_stdev_bytes"], 27)

    def test_irregular_sampling_clipped_window_and_zeros(self):
        cells, quality = memory_intervals([(0, 0, 0), (2, 100, 1), (8, 200, 1), (10, 0, 0)], 1, 10)
        self.assertEqual(cells, [(100, 4), (200, 4), (0, 1)])
        self.assertEqual(quality["rss_max_sample_gap_s"], 6)
        self.assertEqual(quality["rss_observed_fraction"], 8 / 9)
        self.assertEqual(summarize_memory(cells)["rss_mean_bytes"], 1200 / 9)

    def test_unobserved_and_sparse_commands(self):
        cells, quality = memory_intervals([(0, 0, 0), (1, 0, 0)], 0, 1)
        self.assertEqual(cells, [])
        self.assertEqual(quality["rss_measurement_status"], "unavailable")
        self.assertIsNone(summarize_memory(cells)["rss_mean_bytes"])
        _, quality = memory_intervals([(0, 0, 0), (.5, 20, 1), (1, 0, 0)], 0, 1)
        self.assertEqual(quality["rss_measurement_status"], "sparse")

    def test_phase_median_is_not_mean_of_stage_medians(self):
        agg = aggregate([step(1, [(10, 9)]), step(1, [(100, 1)], name="second")])
        rep = agg["memory_repetitions"][0]
        self.assertEqual(rep["rss_median_bytes"], 10)
        self.assertEqual(rep["rss_mean_bytes"], 19)
        self.assertEqual(rep["rss_integral_byte_s"], 190)
        self.assertEqual(agg["phases"][0]["rss_peak_bytes_mean"], 100)

    def test_repetition_statistics_and_missing_steps(self):
        agg = aggregate([step(1, [(10, 9)]), step(2, [(100, 1)]),
                         step(3, [(500, 2)], ok=False), step(4, [])])
        row = agg["phases"][0]
        self.assertEqual(row["rss_n_reps_valid"], 2)
        self.assertEqual(row["rss_mean_bytes_mean"], 55)
        self.assertEqual(row["rss_mean_bytes_median"], 55)
        # Incomplete multi-step phases must not silently report a partial mean.
        agg = aggregate([step(1, [(10, 9)]), step(1, [], name="missing")])
        self.assertIsNone(agg["phases"][0]["rss_mean_bytes_mean"])

    def test_strict_json_and_private_distribution(self):
        s = step(1, [])
        row = s.to_row()
        self.assertNotIn("_rss_intervals", row)
        json.dumps(row, allow_nan=False)
        json.dumps(aggregate([s]), allow_nan=False)

    def test_invalid_samples(self):
        with self.assertRaises(ValueError):
            memory_intervals([(0, 10, 1), (0, 20, 1)], 0, 1)
        with self.assertRaises(ValueError):
            summarize_memory([(10, -1)])

    def test_runner_and_exports_with_instrumented_samples(self):
        sampler = Mock()
        sampler._t0 = 10.0
        sampler.stop.return_value = [Sample(0, proc_rss=0),
                                     Sample(.2, proc_rss=100, nproc=1),
                                     Sample(.8, proc_rss=200, nproc=1),
                                     Sample(1.2, proc_rss=0)]
        with tempfile.TemporaryDirectory(prefix="memory-test-") as outdir:
            runner = Runner(outdir, .1, 1, quiet=True)
            with patch("measure_zk_resources.ResourceSampler", return_value=sampler), \
                 patch("measure_zk_resources.subprocess.Popen") as process, \
                 patch.object(runner, "_rusage_children", return_value=None), \
                 patch("measure_zk_resources.time.perf_counter", side_effect=[10.1, 11.1]):
                process.return_value.wait.return_value = 0
                process.return_value.pid = 123
                result = runner.run("fake", outdir, size=1, phase="prove", step="prove", group="prove")
            self.assertEqual(result.rss_measurement_status, "ok")
            self.assertAlmostEqual(result.rss_mean_bytes, 140)
            manifest = dict(steps=[result.to_row()], aggregates=aggregate(runner.steps))
            write_outputs(outdir, manifest, None)
            with open(os.path.join(outdir, "memory_repetitions.csv"), newline="") as f:
                row = next(csv.DictReader(f))
            self.assertAlmostEqual(float(row["rss_mean_bytes"]), 140)
            self.assertEqual(float(row["rss_median_bytes"]), 100)
            self.assertTrue(os.path.isfile(os.path.join(outdir, result.samples_file)))
            json.dumps(manifest, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
