#!/usr/bin/env python3
"""Isolated local rollup worker; timings exclude interpreter startup/imports.

Called by measure_zk_resources --rollup-local. No server, L1 connection or
production storage is opened. All files belong to the benchmark run.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from classes.BatchInput import write_batch_input
from classes.DataAvailability import DataAvailability
from classes.Executor import Executor
from classes.TxPool import TxPool
from models.Batch import Batch
from models.State import State
from models.Transaction import Transaction
if __package__:
    from .rollup_stages import LOCAL_V2
else:
    from rollup_stages import LOCAL_V2


def measure_local(size, rep, workdir, state_accounts=None):
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    # Same independent transfers and balances as generate_input_file.
    # Fixed timestamps and IDs make DA payloads reproducible across repetitions.
    accounts = 2 * size if state_accounts is None else state_accounts
    if size < 1 or rep < 1 or accounts < 2 * size:
        raise ValueError("Positive size/rep and at least 2N state accounts are required")
    state = State({str(i): 1_000_000.0 for i in range(accounts)})
    da = DataAvailability(str(workdir / "batch"), str(workdir / "blob"))
    executor = Executor()
    # A fresh, isolated pool per invocation; schema creation is one-time setup.
    # UUID paths also allow rerunning a worker without selecting stale rows.
    pool = TxPool(str(workdir / f"txpool_{uuid.uuid4().hex}.sqlite3"))
    rows = []

    def measure(stage, operation):
        cpu_start = time.process_time_ns()
        start = time.perf_counter_ns()
        result = operation()
        wall = (time.perf_counter_ns() - start) / 1e9
        cpu = (time.process_time_ns() - cpu_start) / 1e9
        rows.append(dict(size=size, rep=rep, stage=stage, ok=True,
                         wall_s=wall, cpu_total_s=cpu,
                         timing_scope="in_process_operation",
                         local_schema_version=2, state_accounts=accounts))
        return result

    # Preserve Executor logging behavior but send it to the worker log, as in
    # the live rollup. It is part of compute_batch's measured cost.
    transactions = measure("transaction_build", lambda: [
        Transaction(str(i), str(i + size), 1.0, id=i + 1, timestamp=0.0)
        for i in range(size)])
    def insert_transactions():
        for tx in transactions:
            pool.add(tx)

    measure("txpool_insert", insert_transactions)
    selected = measure("txpool_select", lambda: pool.fetch_batch_unique_senders(size))
    if len(selected) != size or pool.pending_count() != 0:
        raise ValueError("Unexpected txpool selection")
    batch = measure("batch_build", lambda: Batch(selected))
    batch.id, batch.timestamp = 1, 0.0
    measure("txpool_assign", lambda: pool.assign_batch_id([tx.id for tx in batch], batch.id))
    state_copy = measure("state_copy", state.copy)
    new_state = measure("compute_batch", lambda: executor.compute_batch(batch, state_copy))
    if new_state is None:
        raise ValueError("Executor rejected the benchmark batch")
    expected = {str(i): (999_999.0 if i < size else 1_000_001.0 if i < 2 * size else 1_000_000.0)
                for i in range(accounts)}
    if new_state.balances != expected:
        raise ValueError("Unexpected state transition")
    measure("input_prepare", lambda: write_batch_input(state, batch, new_state, workdir / "input.json"))
    measure("state_hash_before", state.hash)
    measure("state_hash_after", new_state.hash)
    serialized = measure("da_serialize", lambda: da.serialize_blob_payload(batch))
    payload = measure("da_compress", lambda: da.compress_blob_payload(serialized))
    measure("da_json_persist", lambda: da.publish_batch_data(state, batch, new_state))
    measure("da_blob_persist", lambda: da.write_blob_locally(batch, payload))

    def snapshot():
        # Same atomic replacement as ZKRollup._save_snapshot; buffered writes,
        # no fsync, matching the implementation.
        data = dict(batch_id=batch.id, state_root=new_state.hash(),
                    balances=new_state.to_dict(), saved_at=time.time())
        temporary = workdir / "snapshot.tmp"
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, workdir / "snapshot.json")

    measure("snapshot_persist", snapshot)
    if tuple(row["stage"] for row in rows) != LOCAL_V2:
        raise ValueError("Local measurement schema mismatch")
    return dict(version=2, size=size, rep=rep, rows=rows,
                payload_bytes=len(payload), serialized_bytes=len(serialized), state_accounts=accounts,
                txpool_pending_initial=size,
                workload="independent transfers; unique senders and recipients; amount=1",
                startup_included=False, executor_logging_included=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--rep", type=int, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-accounts", type=int, help="fixed global state cardinality (default: 2N)")
    args = parser.parse_args()
    if args.size < 1 or args.rep < 1:
        parser.error("size and rep must be positive")
    result = measure_local(args.size, args.rep, args.workdir, args.state_accounts)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
