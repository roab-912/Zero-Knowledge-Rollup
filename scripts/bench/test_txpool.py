"""Atomicity, persistence and concurrent access of the ingestion path."""
import concurrent.futures
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from classes.TxPool import TxPool
from models.Transaction import Transaction


def transaction(index):
    return Transaction(str(index), str(index + 10000), 1., id=99999, timestamp=0.)


class TxPoolTests(unittest.TestCase):
    def test_committed_hash_and_inclusion_survive_reopening(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "pool.sqlite3")
            tx = transaction(1)
            with TxPool(path) as pool:
                returned = pool.add(tx)
                self.assertEqual(returned, tx.__hash_transaction__())
                selected = pool.fetch_batch_unique_senders(1)
                pool.assign_batch_id([selected[0].id], 42)
            with TxPool(path) as reopened:
                self.assertEqual(reopened.pending_count(), 0)
                self.assertEqual(reopened.total_count(), 1)
                with reopened._connect() as conn:
                    stored = conn.execute("SELECT * FROM transactions").fetchone()
                self.assertEqual(stored["hash"], returned)
                self.assertEqual(stored["id"], tx.id)
                self.assertEqual(stored["batch_id"], 42)

    def test_failed_hash_rolls_back_row_and_restores_object(self):
        with tempfile.TemporaryDirectory() as folder, TxPool(str(Path(folder) / "pool.sqlite3")) as pool:
            tx = transaction(1)
            previous = tx.id, tx.hash
            with patch.object(tx, "__hash_transaction__", side_effect=ValueError("hash failed")):
                with self.assertRaisesRegex(ValueError, "hash failed"):
                    pool.add(tx)
            self.assertEqual((tx.id, tx.hash), previous)
            self.assertEqual(pool.total_count(), 0)
            pool.add(tx)
            self.assertEqual(tx.id, 1)
            self.assertEqual(pool.pending_count(), 1)

    def test_failed_hash_update_does_not_leave_partial_insert(self):
        with tempfile.TemporaryDirectory() as folder, TxPool(str(Path(folder) / "pool.sqlite3")) as pool:
            with pool._connect() as conn:
                conn.execute("CREATE TRIGGER reject_update BEFORE UPDATE OF hash ON transactions "
                             "BEGIN SELECT RAISE(ABORT, 'rejected'); END")
            tx = transaction(1)
            previous = tx.id, tx.hash
            with self.assertRaises(sqlite3.IntegrityError):
                pool.add(tx)
            self.assertEqual((tx.id, tx.hash), previous)
            self.assertEqual(pool.total_count(), 0)

    def test_other_connection_cannot_see_row_before_final_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "pool.sqlite3")
            with TxPool(path) as writer, TxPool(path) as reader:
                tx = transaction(1)
                original_hash = tx.__hash_transaction__

                def inspect_uncommitted_row():
                    self.assertEqual(reader.total_count(), 0)
                    return original_hash()

                with patch.object(tx, "__hash_transaction__", side_effect=inspect_uncommitted_row):
                    writer.add(tx)
                self.assertEqual(reader.total_count(), 1)
                restored = reader.fetch_batch_unique_senders(1)[0]
                self.assertEqual(restored.hash, restored.__hash_transaction__())

    def test_concurrent_producers_and_consumers_do_not_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as folder, TxPool(str(Path(folder) / "pool.sqlite3")) as pool:
            txs = [transaction(i) for i in range(200)]
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(pool.add, txs))
                selections = list(executor.map(pool.fetch_batch_unique_senders, [50] * 4))
            selected = [tx for batch in selections for tx in batch]
            self.assertEqual(len(selected), 200)
            self.assertEqual(len({tx.id for tx in selected}), 200)
            self.assertTrue(all(tx.hash == tx.__hash_transaction__() for tx in selected))
            self.assertEqual(pool.pending_count(), 0)

    def test_two_pool_instances_share_id_allocation_safely(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "pool.sqlite3")
            with TxPool(path) as first, TxPool(path) as second:
                def insert(index):
                    tx = transaction(index)
                    (first if index % 2 else second).add(tx)
                    return tx

                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    txs = list(executor.map(insert, range(80)))
                self.assertEqual(len({tx.id for tx in txs}), 80)
                self.assertEqual(first.total_count(), 80)
                self.assertTrue(all(tx.hash == tx.__hash_transaction__() for tx in txs))

    def test_wal_sync_policy_is_preserved_and_close_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            pool = TxPool(str(Path(folder) / "pool.sqlite3"))
            with pool._connect() as conn:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)
            pool.close()
            pool.close()
            with self.assertRaises(sqlite3.ProgrammingError):
                pool.add(transaction(1))


if __name__ == "__main__":
    unittest.main()
