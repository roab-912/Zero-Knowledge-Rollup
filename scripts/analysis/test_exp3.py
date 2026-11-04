"""Scientific aggregation and transition checks for Experiment 3."""
import csv
from pathlib import Path
import tempfile
import unittest

import numpy as np

from exp3 import (GROUPS, GROUP_OF, build_pipelines, crossover_brackets,
                  group_observations, load_measurements, proving_comparison)
from scripts.bench.rollup_stages import LOCAL_SCHEMAS


def pipeline(times):
    times = np.asarray(times, dtype=float)
    stages = ["witness", "proof", "verify"][:times.shape[1]]
    return dict(backend="test", stages=stages, times=times,
                n=2 ** np.arange(len(times)),
                dominant=[tuple(s for s, t in zip(stages, ts) if t == max(ts)) for ts in times])


class Exp3Tests(unittest.TestCase):
    def test_groups_cover_every_local_step_exactly_once(self):
        flat = [stage for members in GROUPS.values() for stage in members]
        self.assertEqual(len(flat), len(set(flat)))
        self.assertEqual(set(GROUP_OF), set().union(*(set(s) for s in LOCAL_SCHEMAS.values())))

    def test_grouped_rss_is_maximum_and_work_is_additive(self):
        base = dict(n=1, prover="", rep=1, timing_scope="in_process_operation",
                    resource_scope="operation_cpu_only", local_schema_version=2, state_accounts="2")
        rows = [dict(base, stage="state_copy", wall_s=1., cpu_total_s=.5,
                     rss_peak_bytes=10., io_read_bytes=3., io_write_bytes=4.),
                dict(base, stage="compute_batch", wall_s=3., cpu_total_s=2.,
                     rss_peak_bytes=20., io_read_bytes=5., io_write_bytes=6.)]
        grouped = group_observations(rows)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["stage"], "computation")
        self.assertEqual(grouped[0]["wall_s"], 4.)
        self.assertEqual(grouped[0]["cpu_total_s"], 2.5)
        self.assertEqual(grouped[0]["rss_peak_bytes"], 20.)
        self.assertEqual(grouped[0]["io_read_bytes"], 8.)
        self.assertEqual(grouped[0]["io_write_bytes"], 10.)

    def test_grouped_sd_uses_paired_repetitions_and_pipeline_has_six_phases(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "steps.csv").write_text("phase,step,rep,size,ok\n", encoding="utf-8")
            rows = []
            for rep, copy, compute in [(1, 1., 3.), (2, 3., 1.)]:
                for stage in LOCAL_SCHEMAS[2]:
                    rows.append(dict(size=1, rep=rep, stage=stage, ok=True,
                                     wall_s=copy if stage == "state_copy" else compute if stage == "compute_batch" else 1.,
                                     cpu_total_s=1., timing_scope="in_process_operation",
                                     local_schema_version=2, state_accounts=2))
            with (run / "local_steps.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            detailed = load_measurements(run)[0]
            grouped = load_measurements(run, grouped=True)[0]
            computation = next(r for r in grouped if r["stage"] == "computation")
            self.assertEqual(computation["wall_s_mean"], 7.)
            self.assertEqual(computation["wall_s_stdev"], 0.)
            self.assertEqual(computation["n_reps"], 2)
            self.assertTrue(np.isnan(computation["io_read_bytes_mean"]))
            self.assertAlmostEqual(sum(r["wall_s_mean"] for r in detailed),
                                   sum(r["wall_s_mean"] for r in grouped))
            zk = [dict(n=1, prover="snarkjs", stage=s, wall_s_mean=t)
                  for s, t in [("witness", 2.), ("proof", 3.), ("verify", 4.), ("proving_total", 5.)]]
            detail_pipeline = build_pipelines(detailed + zk)[0]
            group_pipeline = build_pipelines(grouped + zk)[0]
            self.assertEqual(len(group_pipeline["stages"]), 6)
            self.assertNotIn("proving_total", group_pipeline["stages"])
            np.testing.assert_allclose(detail_pipeline["total"], group_pipeline["total"])
            self.assertGreater(group_pipeline["maximum"][0], detail_pipeline["maximum"][0])

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

    def test_local_schema_compatibility_and_state_cardinality(self):
        for version, stages in LOCAL_SCHEMAS.items():
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                run = Path(directory)
                (run / "steps.csv").write_text("phase,step,rep,size,ok\n", encoding="utf-8")
                fields = ["size", "rep", "stage", "ok", "wall_s", "cpu_total_s", "timing_scope"]
                if version == 2:
                    fields += ["local_schema_version", "state_accounts"]
                rows = [dict(size=4, rep=1, stage=stage, ok=True, wall_s=.01,
                             cpu_total_s=.005, timing_scope="in_process_operation") for stage in stages]
                if version == 2:
                    for row in rows:
                        row.update(local_schema_version=2, state_accounts=32)
                with (run / "local_steps.csv").open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
                data, rejected = load_measurements(run)
                self.assertFalse(rejected)
                self.assertEqual(len(data), len(stages))
                self.assertEqual({r["local_schema_version"] for r in data}, {version})
                self.assertEqual({r["state_accounts"] for r in data}, {"32"} if version == 2 else {""})
                self.assertEqual(build_pipelines(data)[0]["backend"], "local_only")
                groups = load_measurements(run, grouped=True)[0]
                self.assertEqual({r["stage"] for r in groups}, set(GROUPS))

    def test_cannot_mix_old_compute_including_copy_with_new_compute_only(self):
        rows = [dict(n=1, prover="", stage="compute_batch", wall_s_mean=.1,
                     local_schema_version=1),
                dict(n=2, prover="", stage="compute_batch", wall_s_mean=.1,
                     local_schema_version=2)]
        with self.assertRaisesRegex(ValueError, "Cannot mix local v1/v2"):
            build_pipelines(rows)

    def test_existing_dataset_phase_vs_proof_crossings(self):
        run = Path(__file__).resolve().parents[2] / "bench-out/20260825_120620"
        if not (run / "steps.csv").exists():
            self.skipTest("Optional historical raw dataset is not present in this checkout")
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
