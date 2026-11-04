#!/usr/bin/env python3
"""Compare an archived TxPool implementation and the current single-add path.

No bulk API or delayed commits: both implementations call add(tx) N times.
Initialization, transaction construction and validation are outside the timer.
WAL shutdown is timed separately and also included in the combined total.
"""
import argparse
import contextlib
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import statistics
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from classes.TxPool import TxPool
from models.Transaction import Transaction


def sample(pool_type, n, rep, name, outdir):
    path = outdir / f"n{n}_r{rep}_{name}_{uuid.uuid4().hex}.sqlite3"
    pool = pool_type(str(path))
    transactions = [Transaction(str(i), str(n + i), 1., id=i + 1, timestamp=0.) for i in range(n)]
    closed = False
    try:
        start = time.perf_counter()
        for tx in transactions:
            pool.add(tx)
        insert_s = time.perf_counter() - start
        if pool.pending_count() != n or pool.total_count() != n:
            raise ValueError("Missing transactions")
        close = getattr(pool, "close", None)
        start = time.perf_counter()
        if close is not None:
            close()
        close_s = time.perf_counter() - start if close is not None else 0.
        closed = True
        # Verify committed IDs and hashes independently, after the writer closes.
        import sqlite3
        with contextlib.closing(sqlite3.connect(path)) as connection:
            rows = connection.execute("SELECT id,hash FROM transactions ORDER BY id").fetchall()
        expected = [(tx.id, tx.__hash_transaction__()) for tx in transactions]
        if rows != expected:
            raise ValueError("Committed ID/hash mismatch")
        return dict(size=n, rep=rep, implementation=name, insert_s=insert_s,
                    close_s=close_s, insert_and_close_s=insert_s + close_s,
                    tx_per_s=n / insert_s, verified=True, database=str(path))
    finally:
        if not closed and hasattr(pool, "close"):
            pool.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-source", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--sizes", default="512,8192")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    sizes = sorted({int(n) for n in args.sizes.split(",")})
    if args.repeat < 1 or not sizes or min(sizes) < 1:
        parser.error("positive sizes and repeat required")
    spec = importlib.util.spec_from_file_location("baseline_txpool", args.baseline_source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args.outdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for n in sizes:
        for rep in range(1, args.repeat + 1):
            # Alternate order to reduce systematic warm-cache ordering effects.
            order = [("before", module.TxPool), ("after", TxPool)]
            if rep % 2 == 0:
                order.reverse()
            for name, implementation in order:
                row = sample(implementation, n, rep, name, args.outdir)
                rows.append(row)
                with (args.outdir / "samples.csv").open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
                print(f"N={n} rep={rep} {name}: insert={row['insert_s']:.4f}s "
                      f"close={row['close_s']:.4f}s", flush=True)
    summaries = []
    for n in sizes:
        entry = dict(size=n)
        for name in ("before", "after"):
            values = [r["insert_s"] for r in rows if r["size"] == n and r["implementation"] == name]
            totals = [r["insert_and_close_s"] for r in rows if r["size"] == n and r["implementation"] == name]
            entry[name + "_mean_s"] = statistics.mean(values)
            entry[name + "_stdev_s"] = statistics.pstdev(values)
            entry[name + "_with_close_mean_s"] = statistics.mean(totals)
        entry["speedup"] = entry["before_mean_s"] / entry["after_mean_s"]
        summaries.append(entry)
    result = dict(platform=platform.platform(), python=sys.version, sqlite=__import__("sqlite3").sqlite_version,
                  baseline_sha256=hashlib.sha256(args.baseline_source.read_bytes()).hexdigest(),
                  current_sha256=hashlib.sha256((ROOT / "classes/TxPool.py").read_bytes()).hexdigest(),
                  repeat=args.repeat, summaries=summaries,
                  limits="Local isolated ingestion; excludes HTTP and SNARK; speedup is not a server prediction")
    (args.outdir / "comparison.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
