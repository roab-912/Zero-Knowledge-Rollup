"""Small deterministic checks; never launch a prover or a full campaign."""
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import sys
import struct

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.calibration import batch_calibration as bench


def row(n, elapsed, rep=0, status="success", warmup=False):
    return {"id": f"r-{rep}-{n}-snarkjs", "n": n, "prover": "snarkjs",
            "repetition": rep, "status": status, "warmup": warmup,
            "proof_s": elapsed, "proof_generation_s": elapsed, "total_s": elapsed}


class AnalysisTests(unittest.TestCase):
    def test_rapidsnark_sibling_checkout_without_path_even_as_other_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "user directory"
            binary = root / "rapidsnark/package/bin/prover"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            with patch.object(bench, "ROOT", root / "Zero-Knowledge-Rollup"), \
                    patch.object(bench.shutil, "which", return_value=None), \
                    patch.object(Path, "home", return_value=Path(tmp) / "root"), \
                    patch.dict(bench.os.environ, {"RAPIDSNARK_BIN": ""}):
                self.assertEqual(bench.rapidsnark_executable(), [str(binary.resolve())])

    def test_explicit_rapidsnark_overrides_environment_and_no_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "native prover"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            with patch.dict(bench.os.environ, {"RAPIDSNARK_BIN": "missing-prover"}):
                self.assertEqual(bench.rapidsnark_executable(str(binary)), [str(binary.resolve())])
            with patch.object(bench, "executable", return_value=None) as resolve:
                self.assertIsNone(bench.rapidsnark_executable("/missing/custom/prover"))
                resolve.assert_called_once_with("/missing/custom/prover")

    def test_rapidsnark_env_setting_is_authoritative(self):
        with patch.dict(bench.os.environ, {"RAPIDSNARK_BIN": "/missing/selected/prover"}), \
                patch.object(bench, "executable", return_value=None) as resolve:
            self.assertIsNone(bench.rapidsnark_executable())
            resolve.assert_called_once_with("/missing/selected/prover")

    @unittest.skipIf(bench.os.name == "nt", "POSIX executable permission")
    def test_rapidsnark_requires_execute_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "prover"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o644)
            self.assertIsNone(bench.rapidsnark_executable(str(binary)))
            binary.chmod(0o755)
            self.assertEqual(bench.rapidsnark_executable(str(binary)), [str(binary.resolve())])

    def test_affine_amortization_and_no_finite_optimum(self):
        ns = bench.CALIBRATION
        grouped = {n: [2 + .01*n] * 3 for n in ns}
        result = bench.describe_fit(grouped, bench.GRID, 50, 12)
        self.assertEqual(result["model"]["family"], "affine")
        self.assertAlmostEqual(result["model"]["alpha_s"], 2, places=8)
        self.assertIsNone(result["continuous_maximum"])
        self.assertEqual(result["recommended_batch"], max(bench.GRID))
        for prediction in result["predictions"]:
            n = prediction["n"]
            self.assertAlmostEqual(prediction["tps"], n/(2+.01*n))
            self.assertAlmostEqual(prediction["xi"], 2/(2+.01*n))

    def test_interior_crossing_and_supported_grid(self):
        grouped = {n: [4 + .001*n + .0001*n*n]*4 for n in bench.CALIBRATION}
        result = bench.describe_fit(grouped, bench.GRID, 50, 5)
        self.assertEqual(result["model"]["family"], "power")
        self.assertEqual(result["model"]["p"], 2)
        self.assertAlmostEqual(result["continuous_maximum"], 200, places=5)
        self.assertEqual(result["recommended_batch"], 256)
        self.assertEqual(result["classification"], "predicted_interior_maximum")
        self.assertGreater(bench.evaluate(result["model"], [190])[2][0], 0)
        self.assertLess(bench.evaluate(result["model"], [210])[2][0], 0)
        limited = bench.describe_fit(grouped, [1, 4, 16, 64], 50, 5)
        self.assertEqual(limited["recommended_batch"], 64)

    def test_grouped_cross_validation_leaves_entire_size_out(self):
        original = bench.fit_candidate
        calls = []
        def tracked(ns, times, family, power=None):
            calls.append(list(ns))
            return original(ns, times, family, power)
        ns = [1, 4, 16, 64]
        with patch.object(bench, "fit_candidate", side_effect=tracked):
            bench.select_model(ns, [2+.1*n for n in ns])
        folds = calls[:-1]
        self.assertEqual(len(folds), (2 + len(bench.POWERS))*len(ns))
        self.assertTrue(all(len(c) == 3 and len(set(ns)-set(c)) == 1 for c in folds))

    def test_validation_aggregation_secant_failure_and_no_refit(self):
        rows = [row(1, 1, 0), row(1, 3, 1), row(2, 2, 0), row(2, 2, 1),
                row(1, .0001, 9, "error"), row(1, .001, 10, warmup=True)]
        frozen = {"calibration_sizes": [1], "models": {"snarkjs": {
            "proof_s": {"recommended_batch": 1, "predictions": [
                {"n": 1, "tps": .5}, {"n": 2, "tps": 1}]}}}}
        with patch.object(bench, "fit_candidate", side_effect=AssertionError("validation refit")):
            result = bench.validation_report(rows, frozen, 100, 3)["models"]["snarkjs"]["proof_s"]
        self.assertEqual(result["measurements"][0]["tps"], .5)  # not mean(1, 1/3)
        self.assertEqual(result["recommendation_loss"], .5)
        self.assertEqual(result["best_observed_batch"], 2)
        self.assertAlmostEqual(result["elasticity_secants"][0]["observed"], 1)
        self.assertEqual(result["elasticity_secants"][0]["predicted_secant"], 1)
        self.assertTrue(result["measurements"][1]["held_out_size"])
        self.assertEqual(len(result["failures"]), 1)
        frozen["models"]["snarkjs"]["proof_s"]["recommended_batch"] = 4
        result = bench.validation_report(rows, frozen, 50, 3)["models"]["snarkjs"]["proof_s"]
        self.assertIsNone(result["recommendation_loss"])

    def test_insufficient_data_and_uncertainty(self):
        result = bench.describe_fit({1: [1], 4: [2]}, bench.GRID, 50, 4)
        self.assertEqual(result["classification"], "insufficient_data")
        grouped = {n: [1+.1*n] for n in [1, 4, 16, 64]}
        result = bench.describe_fit(grouped, bench.GRID, 50, 4)
        self.assertIsNone(result["parameter_ci95"])
        self.assertTrue(all(p["parameter_tps_ci95"] is None for p in result["predictions"]))

    def test_calibration_accepts_all_sizes_but_cannot_fit_validation(self):
        with patch.object(bench, "output_directory", return_value=Path("unused")), \
                patch.object(bench, "collect", return_value=0) as collect:
            bench.main(["calibration", "--sizes", "2,2048,8192"])
            self.assertEqual(collect.call_args.args[0].sizes, [2, 2048, 8192])
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            bench.save_result(directory, {"schema": 2, "manifest": {"campaign": "validation"}})
            with self.assertRaisesRegex(ValueError, "only calibration"):
                bench.freeze(SimpleNamespace(campaign=directory))


class ExecutionTests(unittest.TestCase):
    def test_single_trial_defaults_and_optional_extra_plateau_repetitions(self):
        args = bench.parser().parse_args(["calibration", "--no-setup"])
        plan = bench.make_plan(args)
        self.assertEqual(args.sizes, bench.GRID)
        self.assertEqual(len(plan), len(bench.GRID)*2)
        self.assertTrue(all(not j["warmup"] and j["repetition"] == 0 for j in plan))
        args.focus_repeat = 30
        plan = bench.make_plan(args)
        self.assertEqual(sum(j["n"] == 2048 and j["prover"] == "snarkjs" for j in plan), 30)
        self.assertEqual(sum(j["n"] == 1024 and j["prover"] == "snarkjs" for j in plan), 1)
        seed = next(j["data_seed"] for j in plan if j["n"] == 8192 and j["repetition"] == 0)
        args.command = "validation"
        args.seed += 1
        other = bench.make_plan(args)
        self.assertNotEqual(seed, next(j["data_seed"] for j in other if j["n"] == 8192 and j["repetition"] == 0))

    def test_unique_default_outputs_and_explicit_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bench-out"
            with patch.object(bench, "OUTPUT_ROOT", root):
                first = bench.output_directory("calibration")
                second = bench.output_directory("calibration")
                fitted = bench.output_directory("fit")
                validation = bench.output_directory("validation")
                self.assertEqual(len({first, second, fitted, validation}), 4)
                self.assertTrue(all(p.parent == root.resolve() and p.is_dir()
                                    for p in (first, second, fitted, validation)))
                with self.assertRaisesRegex(ValueError, "requires --out"):
                    bench.output_directory("calibration", resume=True)
                with self.assertRaisesRegex(ValueError, "result.json missing"):
                    bench.output_directory("calibration", first, resume=True)
                bench.save_result(first, {"schema": 2, "manifest": {}})
                self.assertEqual(bench.output_directory("calibration", first, resume=True), first)
                with self.assertRaisesRegex(ValueError, "directory exists"):
                    bench.output_directory("calibration", first)
                with self.assertRaisesRegex(ValueError, "subdirectory"):
                    bench.output_directory("calibration", root)
                with self.assertRaisesRegex(ValueError, "subdirectory"):
                    bench.output_directory("calibration", root / ".." / "outside")

    def test_fit_has_own_run_and_preserves_source_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            campaign, out = base / "calibration", base / "fit"
            plan, trials = [], []
            for n in (1, 4, 16, 64):
                for repetition in range(2):
                    r = row(n, 1 + .01*n, repetition)
                    job = {k: r[k] for k in ("id", "n", "prover", "repetition", "warmup")}
                    plan.append(job)
                    trials.append({**r, "campaign_id": "test", "input_sha256": "input",
                                   "witness_sha256": "witness"})
            env = {k: None for k in ("hostname", "platform", "processor", "logical_cpus", "python",
                                    "tools", "parallelism", "cpu_affinity", "git_commit", "source_hashes", "memory_bytes")}
            manifest = {
                "campaign": "calibration", "campaign_id": "test", "plan": plan,
                "environment": env, "config": {"provers": ["snarkjs"], "sizes": [1, 4, 16, 64]},
                "artifacts": {str(n): {"supported": True} for n in bench.GRID}}
            bench.save_result(campaign, {"schema": 2, "manifest": manifest, "trials": trials})
            before = bench.calibration_digest(campaign)
            args = SimpleNamespace(campaign=campaign, out=out, admissible_sizes=None,
                                   bootstrap=50, seed=17, max_time=None)
            bench.freeze(args)
            self.assertEqual([p.name for p in campaign.glob("*.json")], ["result.json"])
            self.assertEqual([p.name for p in out.glob("*.json")], ["result.json"])
            self.assertEqual(bench.read_result(out)["manifest"]["campaign"], "fit")
            self.assertEqual(bench.read_result(campaign)["frozen"]["model_path"], str(out / "result.json"))
            self.assertEqual(bench.calibration_digest(campaign), before)
            self.assertEqual(bench.load_frozen(out / "result.json")["campaign_id"], "test")
            changed = bench.read_result(campaign)
            changed["trials"][0]["proof_s"] += 1
            bench.save_result(campaign, changed)
            with self.assertRaisesRegex(ValueError, "changed after freezing"):
                bench.load_frozen(out / "result.json")

    def test_both_prover_commands_share_witness_and_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            circuit = base / "circuits" / "1"
            for name in bench.REQUIRED:
                (circuit / name).parent.mkdir(parents=True, exist_ok=True)
                (circuit / name).write_text("{}")
            args = SimpleNamespace(out=base / "out", circuits_dir=base / "circuits",
                                   command="calibration", timeout=5, sample_interval=.01)
            manifest = {"campaign_id": "test", "artifacts": {"1": {}}}
            calls, rows = [], []
            def fake_step(argv, work, name, timeout, interval):
                calls.append((name, argv))
                if name == "witness":
                    (work / "witness.wtns").write_bytes(b"shared deterministic witness")
                if name == "proof":
                    (work / "proof.json").write_text("{}")
                    (work / "public.json").write_text('["1"]')
                return {"status": "success", "wall_s": .1, "returncode": 0,
                        "output_tail": "OK!", "message": None}
            with patch.object(bench, "run_step", side_effect=fake_step):
                for prover in ("snarkjs", "rapidsnark"):
                    job = {"id": prover, "n": 1, "prover": prover, "data_seed": 1,
                           "repetition": 0, "warmup": False}
                    rows.append(bench.trial(args, job, manifest,
                                           {"snarkjs": ["snarkjs"], "rapidsnark": ["prover"]}))
            self.assertTrue(all(r["status"] == "success" for r in rows))
            bench.check_pairs(rows)
            self.assertEqual(calls[1][1][:3], ["snarkjs", "groth16", "prove"])
            self.assertEqual(calls[4][1][:2], ["prover", str(circuit / "circuit_final.zkey")])
            for index in (2, 5):
                self.assertEqual(calls[index][1][:4], ["snarkjs", "groth16", "verify",
                                                     str(circuit / "verification_key.json")])

    def test_data_are_deterministic_and_state_is_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            state = bench.prepare_batch(4, 123, directory)
            before = (directory/"input.json").read_bytes()
            self.assertEqual(state, bench.prepare_batch(4, 123, directory))
            self.assertEqual(before, (directory/"input.json").read_bytes())
            bench.prepare_batch(4, 124, directory)
            self.assertNotEqual(before, (directory/"input.json").read_bytes())
            data = bench.read_json(directory/"input.json")
            for a, old, new in zip(data["amount"], data["srcBalance"], data["srcBalanceAfter"]):
                self.assertEqual(old-a, new)

    def test_plan_reproducible_paired_and_campaigns_distinct(self):
        args = SimpleNamespace(seed=123, command="calibration", warmups=1,
                               repeat=10, sizes=[1, 4], provers=["snarkjs", "rapidsnark"])
        plan = bench.make_plan(args)
        self.assertEqual(plan, bench.make_plan(args))
        self.assertEqual(len(plan), 44)
        for j in plan:
            peer = next(k for k in plan if k["n"] == j["n"] and k["repetition"] == j["repetition"]
                        and k["warmup"] == j["warmup"] and k["prover"] != j["prover"])
            self.assertEqual(j["data_seed"], peer["data_seed"])
        args.command = "validation"
        self.assertNotEqual(plan[0]["data_seed"], bench.make_plan(args)[0]["data_seed"])

    def test_subprocess_error_timeout_and_success_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok = bench.run_step([sys.executable, "-c", "import time; time.sleep(.08)"], tmp, "ok", 5, .01)
            self.assertEqual(ok["status"], "success")
            self.assertGreaterEqual(ok["wall_s"], .08)
            self.assertLess(ok["wall_s"], 5)
            error = bench.run_step([sys.executable, "-c", "raise RuntimeError('diagnostic')"], tmp, "bad", 5, .01)
            self.assertEqual(error["status"], "error")
            self.assertIn("diagnostic", error["output_tail"])
            timeout = bench.run_step([sys.executable, "-c", "import time; time.sleep(10)"], tmp, "slow", .1, .01)
            self.assertEqual(timeout["status"], "timeout")
            oom = bench.run_step([sys.executable, "-c", "import sys; print('out of memory'); sys.exit(1)"], tmp, "oom", 5, .01)
            self.assertEqual(oom["status"], "out_of_memory")

    def test_invalid_proof_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            circuit = base/"circuits"/"1"
            for name in bench.REQUIRED:
                (circuit/name).parent.mkdir(parents=True, exist_ok=True)
                (circuit/name).write_text("{}")
            args = SimpleNamespace(out=base/"out", circuits_dir=base/"circuits",
                                   command="calibration", timeout=5, sample_interval=.01)
            job = {"id": "one", "n": 1, "prover": "snarkjs", "data_seed": 1}
            manifest = {"campaign_id": "test", "artifacts": {"1": {}}}
            def fake_step(argv, work, name, timeout, interval):
                if name == "witness":
                    (work/"witness.wtns").write_bytes(b"witness")
                if name == "proof":
                    (work/"proof.json").write_text("{}")
                    (work/"public.json").write_text('["1"]')
                return {"status": "success", "wall_s": .1, "returncode": 0,
                        "output_tail": "Invalid proof", "message": None}
            with patch.object(bench, "run_step", side_effect=fake_step):
                result = bench.trial(args, job, manifest, {"snarkjs": ["snarkjs"]})
            self.assertEqual(result["status"], "invalid_proof")
            self.assertEqual(bench.buckets([dict(result, warmup=False)], "snarkjs", "proof_s"), {})

    def test_completed_records_are_immutable_and_hash_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            bench.commit_record(directory/"trials"/"a.json", {"status": "success"})
            before = (directory/"trials"/"a.json").read_bytes()
            with self.assertRaises(FileExistsError):
                bench.commit_record(directory/"trials"/"a.json", {"status": "error"})
            self.assertEqual(before, (directory/"trials"/"a.json").read_bytes())
            bench.write_json(directory/"manifest.json", {})
            source_hash = bench.calibration_digest(directory)
            model = directory/"models.json"
            bench.write_json(model, {"schema": 1, "campaign": "calibration", "calibration_directory": tmp})
            bench.write_json(directory/"frozen.json", {"model_sha256": bench.digest(model),
                                                      "calibration_data_sha256": source_hash})
            bench.load_frozen(model)
            (directory/"trials"/"a.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "changed after freezing"):
                bench.load_frozen(model)

    def test_zkey_internal_domain_is_measured_not_inferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"small.zkey"
            protocol = struct.pack("<I", 1)
            header = struct.pack("<I", 32) + bytes(32) + struct.pack("<I", 32) + bytes(32)
            header += struct.pack("<III", 159, 1, 256)
            path.write_bytes(struct.pack("<4sII", b"zkey", 1, 2) +
                             struct.pack("<IQ", 1, len(protocol)) + protocol +
                             struct.pack("<IQ", 2, len(header)) + header)
            dimensions, reason = bench.zkey_dimensions(path)
            self.assertIsNone(reason)
            self.assertEqual(dimensions, {"nVars": 159, "nPublic": 1, "domainSize": 256})
            path.write_bytes(b"broken")
            self.assertIsNone(bench.zkey_dimensions(path)[0])

    def test_resume_after_interruption_keeps_completed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/"campaign"
            args = bench.parser().parse_args([
                "calibration", "--out", str(out), "--provers", "snarkjs",
                "--sizes", "1", "--repeat", "2", "--warmups", "0", "--no-setup"])
            env = {k: None for k in ("hostname", "platform", "processor", "logical_cpus", "python",
                                    "tools", "parallelism", "cpu_affinity", "git_commit", "source_hashes", "memory_bytes")}
            calls = []
            def fake_trial(args, job, manifest, commands):
                calls.append(job["id"])
                if len(calls) == 2:
                    raise KeyboardInterrupt()
                return {**job, "campaign_id": manifest["campaign_id"], "status": "unavailable"}
            with patch.object(bench, "environment", return_value=env), \
                 patch.object(bench, "inventory", return_value={}), \
                 patch.object(bench, "trial", side_effect=fake_trial):
                with self.assertRaises(KeyboardInterrupt):
                    bench.collect(args)
                initial = bench.read_result(out)["trials"][0]
                self.assertEqual(bench.read_result(out)["summary"]["completed_trials"], 1)
                args.resume = True
                self.assertEqual(bench.collect(args), 2)
                result = bench.read_result(out)
                self.assertEqual(initial, result["trials"][0])
                self.assertEqual(len(result["trials"]), 2)
                self.assertEqual(result["summary"]["completed_trials"], 2)
                self.assertEqual(result["status"], "complete")
                self.assertEqual([p.name for p in out.glob("*.json")], ["result.json"])
                self.assertEqual(len(calls), 3)

    def test_atomic_checkpoint_failure_keeps_previous_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            original = {"schema": 2, "manifest": {}, "trials": [{"id": "completed"}]}
            bench.save_result(directory, original)
            with patch.object(bench.os, "replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    bench.save_result(directory, {**original, "status": "complete"})
            self.assertEqual(bench.read_result(directory), original)
            self.assertEqual([p.name for p in directory.iterdir()], ["result.json"])

    def test_environment_generation_skips_complete_and_preserves_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            circuits, work, template = base / "circuits", base / "work", base / "template"
            template.mkdir()
            (template / "circuit.circom").write_text("component main = Circuit(XXX);")
            original = circuits / "1"
            original.mkdir(parents=True)
            (original / "circuit.circom").write_text("custom source for N=1")
            (original / "notes.txt").write_text("preserve this")
            calls, progress = [], []
            def runner(argv, stage, name):
                calls.append((argv, name))
                for artifact in bench.environments.REQUIRED_ENVIRONMENT:
                    p = stage / artifact
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text("generated")
                return {"status": "success", "wall_s": .01}
            generated = bench.environments.generate_missing_environments(
                circuits, [1, 2], work, ["node", "path with spaces/cli.cjs"], ["circom"],
                runner, progress.append, template)
            self.assertEqual(generated, [1, 2])
            self.assertEqual(bench.environments.missing_environment_sizes(circuits, [1, 2]), [])
            self.assertEqual((original / "circuit.circom").read_text(), "custom source for N=1")
            backup = next(circuits.glob(".before-setup-1-*"))
            self.assertEqual((backup / "notes.txt").read_text(), "preserve this")
            self.assertIn("Circuit(2)", (circuits / "2/circuit.circom").read_text())
            self.assertTrue(any(name == "zkey_verify_final" for _, name in calls))
            self.assertEqual(calls[0][0][:2], ["node", "path with spaces/cli.cjs"])
            with patch.object(bench.existing, "prepare_circuit_dir", side_effect=AssertionError("regenerated")):
                self.assertEqual(bench.environments.generate_missing_environments(
                    circuits, [1, 2], work, None, None, runner, progress.append, template), [])

    def test_setup_failure_does_not_publish_partial_circuit(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            template = base / "template"
            template.mkdir()
            (template / "circuit.circom").write_text("Circuit(XXX)")
            steps = []
            with self.assertRaisesRegex(RuntimeError, "setup failed for N=1"):
                bench.environments.generate_missing_environments(
                    base / "circuits", [1], base / "setup", ["snarkjs"], ["circom"],
                    lambda *_: {"status": "timeout", "message": "timeout", "log": "setup.log"},
                    steps.append, template)
            self.assertFalse((base / "circuits/1").exists())
            self.assertEqual(steps[-1]["status"], "timeout")

    def test_setup_is_checkpointed_separately_before_trials(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "campaign"
            args = bench.parser().parse_args([
                "calibration", "--out", str(out), "--sizes", "1", "--setup-sizes", "1",
                "--provers", "snarkjs", "--repeat", "1", "--warmups", "0"])
            env = {k: None for k in ("hostname", "platform", "processor", "logical_cpus", "python",
                                    "tools", "parallelism", "cpu_affinity", "git_commit", "source_hashes", "memory_bytes")}
            def generate(*arguments):
                progress = arguments[6]
                progress({"n": 1, "step": "compile", "status": "success", "wall_s": 2})
            def fake_trial(args, job, manifest, commands):
                self.assertEqual(bench.read_result(out)["setup"]["steps"][0]["wall_s"], 2)
                return {**job, "campaign_id": manifest["campaign_id"], "status": "unavailable"}
            with patch.object(bench, "environment", return_value=env), \
                 patch.object(bench, "inventory", return_value={}), \
                 patch.object(bench.environments, "generate_missing_environments", side_effect=generate) as setup, \
                 patch.object(bench, "trial", side_effect=fake_trial):
                self.assertEqual(bench.collect(args), 2)
                args.resume = True
                self.assertEqual(bench.collect(args), 2)
                self.assertEqual(setup.call_count, 1)
            result = bench.read_result(out)
            self.assertEqual(len(result["trials"]), 1)
            self.assertNotIn("total_s", result["setup"])


if __name__ == "__main__":
    unittest.main()
