#!/usr/bin/env python3
"""Calibrate every size, freeze models, validate on fresh trials, plot results."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.calibration import batch_calibration as b
from scripts.calibration import batch_theory as theory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-batch", type=int, choices=[2**i for i in range(3, 14)], default=8192)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--focus-repeat", type=int, default=1,
                        help="minimum repetitions at 2048,4096,8192; e.g. 30")
    parser.add_argument("--warmups", type=int, default=0)
    parser.add_argument("--delta", type=float, default=300.)
    parser.add_argument("--timeout", type=float, default=600.)
    parser.add_argument("--no-setup", action="store_true")
    parser.add_argument("--rapidsnark", default="prover")
    parser.add_argument("--circuits-dir", type=Path, default=b.ROOT / "circuits")
    args = parser.parse_args(argv)
    if args.repeat < 1 or args.focus_repeat < 1 or args.warmups < 0 or not (0 < args.delta < float("inf")):
        parser.error("positive repetitions/deadline and nonnegative warmups required")
    if not (0 < args.timeout < float("inf")):
        parser.error("timeout must be positive and finite")
    b.GRID = [2**i for i in range(args.max_batch.bit_length())]
    b.CALIBRATION = b.GRID.copy()
    tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    cal, fit, val = [b.OUTPUT_ROOT / f"{tag}_{phase}" for phase in ("calibration", "fit", "validation")]
    common = ["--repeat", str(args.repeat), "--focus-repeat", str(args.focus_repeat),
              "--warmups", str(args.warmups), "--timeout", str(args.timeout),
              "--rapidsnark", args.rapidsnark, "--circuits-dir", str(args.circuits_dir.resolve())]
    if args.no_setup:
        common += ["--no-setup"]
    for command in (["calibration", "--out", str(cal), *common],
                    ["fit", "--campaign", str(cal), "--out", str(fit), "--max-time", str(args.delta)],
                    ["validation", "--model", str(fit / "result.json"), "--out", str(val), *common]):
        code = b.main(command)
        if code:
            return code
    return theory.main(["--validation", str(val / "result.json"),
                        "--delta", str(args.delta), "--max-batch", str(args.max_batch)])


if __name__ == "__main__":
    raise SystemExit(main())
