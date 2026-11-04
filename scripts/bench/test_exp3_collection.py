"""Exercise the real CLI with a small fake SNARK executable and real Executor."""
import contextlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
import struct
import zlib
import sqlite3

from measure_rollup_local import measure_local
from measure_zk_resources import generate_input_file
from rollup_stages import LOCAL_V2
from classes.DataAvailability import DataAvailability
from models.Batch import Batch
from models.Transaction import Transaction
from classes.TxPool import TxPool

ROOT = Path(__file__).resolve().parents[2]


class CollectionTests(unittest.TestCase):
    def test_txpool_closes_connections_after_use(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = TxPool(str(Path(directory) / "pool.sqlite3"))
            with pool._connect() as conn:
                conn.execute("SELECT 1")
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_local_state_and_isolated_artifacts(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            result = measure_local(4, 1, directory)
            work = Path(directory)
            self.assertEqual(result["state_accounts"], 8)
            self.assertEqual([r["stage"] for r in result["rows"]], list(LOCAL_V2))
            self.assertTrue(all(r["wall_s"] > 0 for r in result["rows"]))
            data = json.loads((work / "input.json").read_text())
            self.assertEqual(data["srcBalanceAfter"], [999999.] * 4)
            self.assertEqual(data["destBalanceAfter"], [1000001.] * 4)
            self.assertTrue((work / "batch/batch_1.json").exists())
            self.assertTrue((work / "snapshot.json").exists())
            self.assertGreater(result["payload_bytes"], 0)

    def test_fixed_global_state_and_separate_computation(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            result = measure_local(4, 1, directory, state_accounts=32)
            snapshot = json.loads((Path(directory) / "snapshot.json").read_text())
            self.assertEqual(len(snapshot["balances"]), 32)
            self.assertEqual(snapshot["balances"]["31"], 1_000_000.)
            self.assertEqual(snapshot["balances"]["0"], 999_999.)
            self.assertEqual(snapshot["balances"]["4"], 1_000_001.)
            self.assertTrue(all(r["state_accounts"] == 32 for r in result["rows"]))
            self.assertTrue(all(r["local_schema_version"] == 2 for r in result["rows"]))
            self.assertIn("state_copy", [r["stage"] for r in result["rows"]])
            self.assertIn("compute_batch", [r["stage"] for r in result["rows"]])

    def test_global_state_must_contain_all_transfer_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "at least 2N"):
                measure_local(4, 1, directory, state_accounts=7)

    def test_split_da_preserves_existing_blob_format(self):
        with tempfile.TemporaryDirectory() as directory:
            da = DataAvailability(str(Path(directory) / "batch"), str(Path(directory) / "blob"))
            batch = Batch([Transaction("0", "1", 1., id=1, timestamp=0.)])
            expected = bytearray(struct.pack(">II", batch.id, 1))
            tx_bytes = json.dumps(batch[0].to_dict(), sort_keys=True).encode("utf-8")
            expected += struct.pack(">I", len(tx_bytes)) + tx_bytes
            serialized = da.serialize_blob_payload(batch)
            self.assertEqual(serialized, bytes(expected))
            self.assertEqual(da.compress_blob_payload(serialized), zlib.compress(bytes(expected), level=9))
            self.assertEqual(da.build_blob_payload(batch), da.compress_blob_payload(serialized))

    def run_fake(self, fail_witness=False, local=False, fullprove=False):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            circuit = work / "circuits/1"
            circuit.mkdir(parents=True)
            (circuit / "circuit_final.zkey").write_text("fake")
            generate_input_file(1, str(circuit))
            fake = work / "fake_snark.py"
            fake.write_text("import sys\nfrom pathlib import Path\n"
                            "args = sys.argv[1:]\n"
                            "if '--version' in args: print('test'); sys.exit(0)\n"
                            f"if args[:2] == ['wtns', 'calculate'] and {fail_witness!r}: sys.exit(9)\n"
                            "if args[:2] == ['groth16', 'prove']:\n"
                            "    Path('proof.json').write_text('{}')\n"
                            "    Path('public.json').write_text('[1]')\n", encoding="utf-8")
            output = work / "result"
            result = subprocess.run([
                sys.executable, str(ROOT / "scripts/bench/measure_zk_resources.py"),
                "--skip-setup", "--sizes", "1", "--repeat", "2", "--sleep-between", "0",
                "--circuits-dir", str(work / "circuits"), "--out-dir", str(output),
                "--prover", "snarkjs", "--rapidsnark-mode", "off",
                "--snarkjs", shlex.join([sys.executable, str(fake)]),
                "--no-plots", "--quiet"] + (["--rollup-local"] if local else [])
                + (["--fullprove"] if fullprove else []),
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads((output / "manifest.json").read_text(encoding="utf-8"))

    def test_split_snarkjs_proving_is_default(self):
        data = self.run_fake()
        self.assertEqual([r["step"] for r in data["steps"]],
                         ["witness_snarkjs", "prove_snarkjs"] * 2 + ["verify_snarkjs"] * 2)
        self.assertTrue(all(r["ok"] for r in data["steps"]))
        self.assertTrue(data["params"]["split_prove"])

    def test_legacy_fullprove_requires_explicit_option(self):
        data = self.run_fake(fullprove=True)
        self.assertEqual([r["step"] for r in data["steps"]],
                         ["fullprove_snarkjs"] * 2 + ["verify_snarkjs"] * 2)
        self.assertFalse(data["params"]["split_prove"])

    def test_failed_witness_does_not_prove_or_verify_stale_files(self):
        data = self.run_fake(fail_witness=True)
        self.assertEqual([r["step"] for r in data["steps"]], ["witness_snarkjs"])
        self.assertFalse(data["steps"][0]["ok"])

    def test_local_and_snark_collection_use_matching_workload(self):
        data = self.run_fake(local=True)
        self.assertEqual(len(data["local_steps"]), 2 * len(LOCAL_V2))
        self.assertTrue(data["params"]["split_prove"])
        self.assertEqual(sum(r["phase"] == "local" for r in data["steps"]), 2)
        self.assertEqual(sum(r["phase"] == "prove" for r in data["steps"]), 4)
        inventory = {row["stage"]: row for row in data["resource_inventory"]}
        self.assertEqual(inventory["state_copy"]["status"], "measured")
        self.assertEqual(inventory["compute_batch"]["status"], "measured")
        self.assertEqual(inventory["kzg_proof"]["status"], "not_measured")


if __name__ == "__main__":
    unittest.main()
