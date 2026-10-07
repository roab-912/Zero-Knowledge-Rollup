#!/usr/bin/env python3
"""Reproducible, sequential batch measurements and held-out validation.

See batch_calibration.md for scope, assumptions and executable commands.
No circuit setup, chain submission, or full campaign runs on import.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import io
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import shutil
import signal
import statistics
import struct
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = ROOT / "bench-out"
RESULT_FILE = "result.json"
sys.path.insert(0, str(ROOT))
from scripts.bench import measure_zk_resources as existing
from scripts.bench import generate_environments as environments
from models.Batch import Batch
from models.State import State
from models.Transaction import Transaction
from classes.Executor import Executor

GRID = [2 ** i for i in range(10)]
CALIBRATION = GRID[::2]
POWERS = [1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
MODE = "sequential_fresh_witness_and_prover_processes_v1"
SCOPES = ("proof_s", "proof_generation_s", "total_s")
ARTIFACTS = ("circuit_js/circuit.wasm", "circuit_final.zkey",
             "verification_key.json", "circuit.circom", "circuit.r1cs",
             "circuit.r1cs.json")
REQUIRED = ARTIFACTS[:3]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def output_directory(command, requested=None, resume=False):
    """Reserve one new run under bench-out; resume only an explicit campaign."""
    root = OUTPUT_ROOT.resolve()
    if resume and requested is None:
        raise ValueError("--resume requires --out pointing to the existing campaign")
    if requested is not None:
        # Relative output paths are always relative to the repository, not CWD.
        path = Path(requested)
        path = (path if path.is_absolute() else ROOT / path).resolve()
        if path == root or not path.is_relative_to(root):
            raise ValueError(f"--out must be a subdirectory of {root}")
        if resume:
            if not (path / RESULT_FILE).is_file():
                raise ValueError("cannot resume: campaign result.json missing (legacy campaigns are read-only)")
            return path
        if path.exists():
            raise ValueError("output directory exists; choose a new directory or use --resume")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        path = root / f"{stamp}_{command}_{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    print(f"Output directory: {path}", flush=True)
    return path


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    """Exclusive outputs: a completed record is never overwritten."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())


def commit_record(path, value):
    """Atomic completion; interrupted attempts keep logs but no completed row."""
    path = Path(path)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    write_json(tmp, value)
    if path.exists():
        raise FileExistsError(path)
    os.replace(tmp, path)  # protected by the campaign's advisory lock


def save_result(directory, document):
    """Atomically checkpoint the entire run; completed trial entries are retained."""
    target = Path(directory) / RESULT_FILE
    temporary = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        write_json(temporary, document)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_result(directory):
    document = read_json(Path(directory) / RESULT_FILE)
    if document.get("schema") != 2 or not isinstance(document.get("manifest"), dict):
        raise ValueError("unsupported result.json schema")
    return document


def checkpoint(directory, document):
    rows, manifest = document["trials"], document["manifest"]
    document["summary"] = {
        "campaign": str(directory), "campaign_id": manifest["campaign_id"],
        "updated_utc": utc_now(),
        "statuses": {s: sum(r["status"] == s for r in rows)
                     for s in sorted({r["status"] for r in rows})},
        "completed_trials": len(rows), "planned_trials": len(manifest["plan"]),
    }
    save_result(directory, document)


@contextmanager
def campaign_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+b") as f:
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            if not f.read(1):
                f.write(b"0")
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def executable(value, snarkjs=False):
    """Resolve an executable, never interpret a shell command string."""
    value = os.path.expanduser(str(value))
    found = shutil.which(value)
    if not found and Path(value).is_file():
        found = str(Path(value).resolve())
    if not found:
        return None
    path = Path(found).resolve()
    if snarkjs and path.suffix.lower() in (".cmd", ".ps1", ".bat"):
        cli = path.parent / "node_modules/snarkjs/build/cli.cjs"
        node = shutil.which("node")
        if cli.is_file() and node:
            return [node, str(cli)]
        return None
    if snarkjs and path.suffix.lower() in (".js", ".cjs"):
        node = shutil.which("node")
        return [node, str(path)] if node else None
    return [str(path)]


def rapidsnark_executable(value="prover"):
    """Find a native Linux/Windows binary, including a sibling source checkout.

    An explicit CLI path or RAPIDSNARK_BIN is authoritative: never silently
    substitute a different installation if the configured one is unavailable.
    """
    def native(candidate):
        command = executable(candidate)
        if command and (os.name == "nt" or os.access(command[0], os.X_OK)):
            return command
        return None

    if value != "prover":
        return native(value)
    configured = os.environ.get("RAPIDSNARK_BIN")
    if configured:
        return native(configured)
    command = native("prover")
    if command:
        return command
    # ROOT.parent also works when the caller is root but the repository and
    # rapidsnark checkout belong to another user (e.g. /home/r24barbi).
    for base in (ROOT.parent, Path.home(), ROOT):
        for relative in ("rapidsnark/package/bin/prover", "rapidsnark/build/prover"):
            command = native(str(base / relative))
            if command:
                return command
    return None


def probe(argv):
    if not argv:
        return {"value": None, "reason": "executable unavailable"}
    try:
        r = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=10)
        return {"value": (r.stdout + r.stderr).strip()[:2000],
                "returncode": r.returncode}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"value": None, "reason": str(e)}


def environment(commands):
    processor = platform.processor()
    if Path("/proc/cpuinfo").is_file():
        for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
            if line.startswith("model name"):
                processor = line.partition(":")[2].strip()
                break
    memory = existing.read_system_mem()
    if memory is None and existing.HAS_PSUTIL:
        m = existing.psutil.virtual_memory()
        memory = {"total": m.total, "available": m.available}
    if memory is None and os.name == "nt":
        import ctypes
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (k, ctypes.c_ulonglong) for k in
                ("total", "available", "page_total", "page_available",
                 "virtual_total", "virtual_available", "extended")]
        m = MemoryStatus()
        m.length = ctypes.sizeof(m)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            memory = {"total": m.total, "available": m.available}
    git = ["git", "-c", "safe.directory=" + ROOT.as_posix()]
    tools = {}
    for name, command in commands.items():
        tools[name] = {"command": command, "sha256":
                       [digest(p) for p in command] if command else None}
    tools["snarkjs"]["version"] = probe(
        commands["snarkjs"] + ["--version"] if commands["snarkjs"] else None)
    # Rapidsnark builds need not expose a version flag: executable hash is stable.
    tools["rapidsnark"]["version"] = {
        "value": None, "reason": "no portable version flag; binary SHA-256 recorded"}
    tools["node"] = probe([shutil.which("node"), "--version"] if shutil.which("node") else None)
    if "circom" in commands:
        tools["circom"]["version"] = probe(commands["circom"] + ["--version"] if commands["circom"] else None)
    return {
        "hostname": platform.node(), "platform": platform.platform(),
        "processor": processor, "logical_cpus": os.cpu_count(),
        "python": platform.python_version(), "tools": tools,
        "memory_bytes": memory,
        "memory_missing_reason": None if memory else "OS memory counters unavailable",
        "parallelism": {k: os.environ.get(k) for k in
                        ("OMP_NUM_THREADS", "OMP_THREAD_LIMIT", "GOMP_CPU_AFFINITY",
                         "OMP_PROC_BIND", "NODE_OPTIONS", "UV_THREADPOOL_SIZE")},
        "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "git_commit": probe(git + ["rev-parse", "HEAD"]),
        "git_status": probe(git + ["status", "--porcelain"]),
        "source_hashes": {str(p.relative_to(ROOT)): digest(p) for p in
                          [Path(__file__), Path(existing.__file__), Path(environments.__file__),
                           ROOT / "models/Batch.py", ROOT / "models/State.py",
                           ROOT / "models/Transaction.py", ROOT / "classes/Executor.py"]},
    }


def machine_signature(env):
    """Ignore fluctuating free memory and unrelated untracked output files."""
    keys = ("hostname", "platform", "processor", "logical_cpus", "python",
            "tools", "parallelism", "cpu_affinity", "git_commit", "source_hashes")
    return {**{k: env[k] for k in keys},
            "memory_total": (env["memory_bytes"] or {}).get("total")}


def analysis_versions():
    result = {}
    for name in ("numpy", "scipy"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


def zkey_dimensions(path):
    """Read the Groth16 header, following snarkjs zkey_utils/readHeaderGroth16.

    Bound all reads; never load elliptic-curve arrays or infer padding from N.
    """
    try:
        with Path(path).open("rb") as f:
            magic, version, count = struct.unpack("<4sII", f.read(12))
            if magic != b"zkey" or version != 1 or count > 100:
                raise ValueError("unsupported zkey header")
            sections = {}
            for _ in range(count):
                section, size = struct.unpack("<IQ", f.read(12))
                if section in sections or f.tell() + size > Path(path).stat().st_size:
                    raise ValueError("invalid zkey sections")
                sections[section] = (f.tell(), size)
                f.seek(size, 1)
            f.seek(sections[1][0])
            if struct.unpack("<I", f.read(4))[0] != 1:
                raise ValueError("not a Groth16 key")
            start, size = sections[2]
            f.seek(start)
            for _ in range(2):
                width = struct.unpack("<I", f.read(4))[0]
                if width > 512 or f.tell() + width + 12 > start + size:
                    raise ValueError("invalid field width")
                f.seek(width, 1)
            variables, public, domain = struct.unpack("<III", f.read(12))
            return {"nVars": variables, "nPublic": public, "domainSize": domain}, None
    except (OSError, ValueError, KeyError, struct.error) as e:
        return None, str(e)


def inventory(directory):
    out = {}
    for n in GRID:
        base = Path(directory) / str(n)
        files = {}
        for name in ARTIFACTS:
            p = base / name
            files[name] = ({"bytes": p.stat().st_size, "sha256": digest(p)}
                           if p.is_file() else None)
        dimensions = {}
        header = base / "circuit.r1cs.json"
        if header.is_file():
            # The existing exporter puts dimensions before its enormous arrays.
            with header.open(encoding="utf-8") as f:
                head = f.read(65536)
            for k in ("nConstraints", "nVars", "nPubInputs", "nPrvInputs", "nOutputs", "nLabels"):
                match = re.search(r'"' + k + r'"\s*:\s*(\d+)', head)
                dimensions[k] = int(match[1]) if match else None
        domain, domain_reason = zkey_dimensions(base / "circuit_final.zkey")
        out[str(n)] = {
            "files": files, "dimensions": dimensions or None,
            "dimensions_missing_reason": None if dimensions else "R1CS JSON header unavailable",
            "internal_domain": domain,
            "internal_domain_missing_reason": domain_reason,
            "supported": all(files[k] is not None for k in REQUIRED),
        }
    return out


def data_seed(seed, kind, n, repetition, warmup):
    # Different campaigns draw fresh data; both provers get the same input.
    key = f"{seed}:{kind}:{n}:{repetition}:{warmup}".encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "little")


def make_plan(args):
    rng = random.Random(args.seed)
    jobs = []
    for warmup, count in ((True, args.warmups), (False, args.repeat)):
        part = [{"prover": p, "n": n, "repetition": r, "warmup": warmup,
                 "data_seed": data_seed(args.seed, args.command, n, r, warmup),
                 "id": f"{'w' if warmup else 'r'}-{r:03d}-{n}-{p}"}
                for r in range(count) for n in args.sizes for p in args.provers]
        rng.shuffle(part)
        jobs.extend(part)
    return jobs


def prepare_batch(n, seed, directory):
    """Reuse synthetic generator, Batch and Executor; reset state on each call.

    Disjoint sender/receiver pairs preserve the existing circuit input semantics.
    Seeded amounts vary the existing unit-transfer fixture without overdrafts.
    """
    path = existing.generate_input_file(n, str(directory))
    data = read_json(path)
    rng = random.Random(seed)
    amounts = [rng.randint(1, 1000) for _ in range(n)]
    state = State({str(i): 1_000_000 for i in range(2 * n)})
    batch = Batch([Transaction(data["src"][i], data["dest"][i], amounts[i],
                               id=i + 1, timestamp=0) for i in range(n)])
    batch.id, batch.timestamp = 0, 0
    with redirect_stdout(io.StringIO()):
        after = Executor().compute_batch(batch, state.copy())
    if after is None:
        raise ValueError("generated transactions were rejected by Executor")
    data.update(amount=amounts,
                srcBalance=[state.get_balance(t.from_address) for t in batch],
                destBalance=[state.get_balance(t.to_adress) for t in batch],
                srcBalanceAfter=[after.get_balance(t.from_address) for t in batch],
                destBalanceAfter=[after.get_balance(t.to_adress) for t in batch])
    Path(path).write_text(json.dumps(data), encoding="utf-8")
    return {"initial_state_sha256": state.hash(), "final_state_sha256": after.hash()}


def stop_process_tree(process):
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            # Still reap the direct child if taskkill itself cannot complete.
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def run_step(argv, directory, name, timeout, interval):
    """Monotonic wall time includes process startup and actual artifact reads."""
    log = Path(directory) / (name + ".log")
    sampler = existing.ResourceSampler(interval)
    start = time.perf_counter()
    status, message, code = "success", None, None
    process = None
    sampler.start()
    try:
        with log.open("wb") as output:
            process = subprocess.Popen(argv, cwd=directory, stdout=output,
                                       stderr=subprocess.STDOUT,
                                       start_new_session=os.name != "nt")
            sampler.attach(process.pid)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                status, message = "timeout", f"step exceeded {timeout} seconds"
                stop_process_tree(process)
                code = process.returncode
            except BaseException:
                stop_process_tree(process)
                raise
    except OSError as e:
        status, message = "error", str(e)
    finally:
        wall = time.perf_counter() - start
        samples = sampler.stop()
    with log.open("rb") as f:
        f.seek(max(0, log.stat().st_size - 8192))
        tail = f.read().decode("utf-8", errors="replace")
    if status == "success" and code != 0:
        # A SIGKILL alone does not prove OOM (could be an administrator kill).
        status = "out_of_memory" if re.search(
            r"out of memory|bad_alloc|cannot allocate memory|heap limit", tail, re.I) else "error"
        message = f"exit code {code}"
    cpu = sum(sampler._cpu_by_pid.values()) if sampler._cpu_by_pid else None
    rss = max((s.proc_rss for s in samples if s.nproc), default=None)
    resources = {"cpu_s": cpu, "rss_peak_bytes": rss}
    for target, key in (("read_bytes", "read_bytes"), ("write_bytes", "write_bytes")):
        vals = [v[key] for v in sampler._io_by_pid.values() if key in v]
        resources[target] = sum(vals) if vals else None
    resources.update(
        method=f"{sampler.backend} sampled process tree, interval={interval}s",
        includes_children=True,
        limitation="sampled lower bounds; short-lived children and final CPU/IO increments may be missed; RSS is occupancy",
        missing_reasons={k: "no accessible process samples/counters (install psutil on Windows)"
                         for k, v in resources.items() if v is None})
    return {"command": argv, "wall_s": wall, "status": status,
            "returncode": code, "message": message, "log": str(log),
            "output_tail": tail, "resources": resources}


def trial(args, job, manifest, commands):
    work = args.out / "attempts" / (job["id"] + "-" + uuid.uuid4().hex[:12])
    work.mkdir(parents=True)
    row = {**job, "campaign_id": manifest["campaign_id"], "campaign": args.command,
           "execution_mode": MODE, "started_utc": utc_now(), "steps": {},
           "status": "success", "diagnostic": None,
           "preparation_s": None, "artifact_load_s": None,
           "artifact_load_missing_reason": "CLI embeds real reads in witness_s/proof_s; no isolated loading hook",
           "witness_s": None, "proof_s": None, "verification_s": None,
           "proof_generation_s": None, "total_s": None,
           "rollup_end_to_end_s": None,
           "rollup_end_to_end_missing_reason": "off-chain experiment excludes DA, L1 and queueing",
           "artifacts": manifest["artifacts"][str(job["n"])],
           "attempt_directory": str(work)}
    base = args.circuits_dir / str(job["n"])
    missing = [str(base / k) for k in REQUIRED if not (base / k).is_file()]
    if missing or not commands["snarkjs"] or not commands[job["prover"]]:
        row.update(status="unavailable", diagnostic={"missing_artifacts": missing,
                   "missing_tools": [k for k in {"snarkjs", job["prover"]} if not commands[k]]})
        return row
    start = time.perf_counter()
    phase = "preparation"
    try:
        cpu = time.process_time()
        row["state"] = prepare_batch(job["n"], job["data_seed"], work)
        row["preparation_s"] = time.perf_counter() - start
        row["preparation_resources"] = {
            "cpu_s": time.process_time() - cpu, "includes_children": False,
            "method": "time.process_time (Python parent)",
            "rss_peak_bytes": None, "read_bytes": None, "write_bytes": None,
            "missing_reason": "phase-local parent RSS and IO are not instrumented"}
        witness = work / "witness.wtns"
        proof, public = work / "proof.json", work / "public.json"
        witness_cmd = commands["snarkjs"] + ["wtns", "calculate",
            str(base / REQUIRED[0]), str(work / "input.json"), str(witness)]
        prefix = commands[job["prover"]]
        proof_cmd = prefix + (["groth16", "prove"] if job["prover"] == "snarkjs" else []) + [
            str(base / REQUIRED[1]), str(witness), str(proof), str(public)]
        for phase, cmd in (("witness", witness_cmd), ("proof", proof_cmd)):
            result = run_step(cmd, work, phase, args.timeout, args.sample_interval)
            row["steps"][phase] = result
            row[phase + "_s"] = result["wall_s"]
            if result["status"] != "success":
                row.update(status=result["status"], diagnostic={"phase": phase, "message": result["message"]})
                return row
        # Capture the contiguous measured perimeter before diagnostics and verify.
        row["total_s"] = time.perf_counter() - start
        row["proof_generation_s"] = row["witness_s"] + row["proof_s"]
        row["input_sha256"] = digest(work / "input.json")
        row["witness_sha256"] = digest(witness)
        row["witness_bytes"] = witness.stat().st_size
        row["proof_bytes"] = proof.stat().st_size
        # Existing rapidsnark adapter also removes NUL padding before JSON parsing.
        for p in (proof, public):
            raw = p.read_bytes()
            if b"\x00" in raw:
                p.write_bytes(raw.replace(b"\x00", b""))
        phase = "verification"
        result = run_step(commands["snarkjs"] + ["groth16", "verify",
                          str(base / REQUIRED[2]), str(public), str(proof)],
                          work, phase, args.timeout, args.sample_interval)
        row["steps"][phase] = result
        row["verification_s"] = result["wall_s"]
        if result["status"] in ("timeout", "out_of_memory"):
            row.update(status=result["status"], diagnostic={"phase": phase, "message": result["message"]})
        elif result["returncode"] is None:
            row.update(status="error", diagnostic=result["message"])
        elif result["returncode"] != 0 or not re.search(r"\bOK!?", result["output_tail"]) or read_json(public) != ["1"]:
            row.update(status="invalid_proof", diagnostic="Groth16 verification or public success signal rejected")
    except (OSError, ValueError, MemoryError) as e:
        row.update(status="out_of_memory" if isinstance(e, MemoryError) else "error",
                   diagnostic={"phase": phase, "message": str(e)})
    finally:
        row["attempt_elapsed_s"] = time.perf_counter() - start
    return row


def records(directory, manifest):
    rows = read_result(directory)["trials"]
    plan = {job["id"]: job for job in manifest["plan"]}
    seen = set()
    for row in rows:
        job = plan.get(row["id"])
        if (job is None or row["id"] in seen or row["campaign_id"] != manifest["campaign_id"]
                or any(row.get(k) != v for k, v in job.items())):
            raise ValueError(f"record identity mismatch: {row['id']}")
        seen.add(row["id"])
    return rows


def check_pairs(rows):
    pairs = {}
    for row in rows:
        if row["status"] != "success":
            continue
        key = (row["n"], row["repetition"], row["warmup"])
        value = (row["input_sha256"], row["witness_sha256"])
        if key in pairs and pairs[key] != value:
            raise ValueError(f"provers did not use identical input/witness at {key}")
        pairs[key] = value


def load_frozen(path):
    document = read_json(path)
    model = document.get("model", document)
    if model.get("schema") != 1 or model.get("campaign") != "calibration":
        raise ValueError("expected a frozen calibration model")
    source = Path(model["calibration_directory"])
    marker = (read_result(source).get("frozen") if (source / RESULT_FILE).exists()
              else read_json(source / "frozen.json"))
    if not marker:
        raise ValueError("calibration model has not been frozen")
    if marker["model_sha256"] != digest(path):
        raise ValueError("frozen model hash mismatch")
    if marker["calibration_data_sha256"] != calibration_digest(source):
        raise ValueError("calibration records changed after freezing")
    return model


def calibration_digest(directory):
    if (Path(directory) / RESULT_FILE).exists():
        document = read_result(directory)
        # Summaries and freeze metadata may change without changing measurements.
        payload = {k: document.get(k, []) for k in ("manifest", "trials", "resume_warmups")}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    # Historical multi-file campaigns remain readable for frozen-model auditing.
    h = hashlib.sha256()
    for p in [Path(directory) / "manifest.json"] + sorted((Path(directory) / "trials").glob("*.json")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def collect(args):
    with campaign_lock(args.out):
        path = args.out / RESULT_FILE
        document = read_result(args.out) if path.exists() else None
        if document and document.get("frozen"):
            raise ValueError("calibration is frozen; use a new output directory for another experiment")
        commands = {"snarkjs": executable(args.snarkjs, True),
                    "rapidsnark": rapidsnark_executable(args.rapidsnark),
                    "circom": executable(args.circom)}
        if "rapidsnark" in args.provers:
            if commands["rapidsnark"]:
                print(f"Rapidsnark binary: {commands['rapidsnark'][0]}", flush=True)
            else:
                print("Rapidsnark unavailable: pass --rapidsnark /absolute/path/to/prover "
                      "or set RAPIDSNARK_BIN; the file must exist and be executable.",
                      file=sys.stderr, flush=True)
        print("Inspecting environment and artifact hashes...", flush=True)
        env, artifacts = environment(commands), inventory(args.circuits_dir)
        frozen = load_frozen(args.model) if args.command == "validation" else None
        config = {k: getattr(args, k) for k in
                  ("sizes", "provers", "repeat", "warmups", "timeout", "seed", "sample_interval")}
        config.update(circuits_dir=str(args.circuits_dir), execution_mode=MODE)
        config.update(auto_setup=not args.no_setup, setup_sizes=args.setup_sizes,
                      setup_timeout=args.setup_timeout, circom=args.circom)
        if frozen:
            if machine_signature(env) != frozen["machine_signature"]:
                raise ValueError("validation environment differs from calibration (tools/hardware/parallelism/source)")
            if artifacts != frozen["artifacts"]:
                raise ValueError("validation artifacts differ from the frozen calibration inventory")
            if args.provers != frozen["provers"]:
                raise ValueError("validation must use the frozen prover selection")
        model_hash = digest(args.model) if frozen else None
        if document is not None:
            if not args.resume:
                raise ValueError("campaign exists; use --resume with the same configuration")
            manifest = document["manifest"]
            preparing = document["status"] in ("setting_up", "setup_failed") and not document["trials"]
            if (manifest["campaign"] != args.command or manifest["config"] != config
                    or (not preparing and manifest["artifacts"] != artifacts) or manifest["model_sha256"] != model_hash
                    or machine_signature(manifest["environment"]) != machine_signature(env)):
                raise ValueError("resume configuration, environment, model or artifacts changed")
        else:
            if args.resume:
                raise ValueError("cannot resume: result.json missing")
            manifest = {
                "schema": 1, "campaign_id": uuid.uuid4().hex, "campaign": args.command,
                "created_utc": utc_now(), "config": config, "environment": env,
                "artifacts": artifacts, "plan": make_plan(args), "model_sha256": model_hash,
                "model_path": str(args.model) if frozen else None,
                "scope": {
                    "proof_s": "fresh prover process: artifact loading + Groth16 + output writing",
                    "proof_generation_s": "sequential witness_s + proof_s (fresh processes)",
                    "total_s": "contiguous preparation + witness + proof, including instrumentation overhead",
                    "verification_s": "separate local snarkjs verification; excluded from total_s",
                    "exclusions": "compilation, cryptographic setup, pool, DA, L1, scheduling/queueing",
                    "warmups": "fresh processes warm OS caches only; no persistent JIT/service",
                },
            }
            document = {"schema": 2, "manifest": manifest, "trials": [],
                        "resume_warmups": [], "status": "setting_up" if not args.no_setup and not frozen else "running"}
            checkpoint(args.out, document)
        if not args.no_setup and not frozen and document["status"] in ("setting_up", "setup_failed"):
            setup = document.setdefault("setup", {"steps": [], "elapsed_s": 0.0,
                                                   "scope": "one-off environment generation, excluded from all batch times"})
            document["status"] = "setting_up"
            setup.pop("error", None)
            checkpoint(args.out, document)
            started = time.perf_counter()

            def progress(step):
                setup["steps"].append({"timestamp_utc": utc_now(), **step})
                checkpoint(args.out, document)
                print(f"setup N={step['n']} {step['step']}: {step['status']}", flush=True)

            try:
                environments.generate_missing_environments(
                    args.circuits_dir, args.setup_sizes, args.out / "setup", commands["snarkjs"],
                    commands["circom"],
                    lambda argv, directory, name: run_step(argv, directory, name,
                                                           args.setup_timeout, args.sample_interval), progress)
            except (OSError, RuntimeError, ValueError) as exc:
                setup["error"] = str(exc)
                document["status"] = "setup_failed"
                print(f"Setup failed: {exc}", file=sys.stderr, flush=True)
            finally:
                setup["elapsed_s"] += time.perf_counter() - started
                checkpoint(args.out, document)
            if document["status"] == "setup_failed":
                return 2
            manifest["artifacts"] = inventory(args.circuits_dir)
        completed = {r["id"] for r in records(args.out, manifest)}
        document["status"] = "running"
        checkpoint(args.out, document)
        # Resume does not pretend that warm OS caches survived an interruption.
        if args.resume and args.warmups:
            for job in manifest["plan"]:
                if job["warmup"]:
                    row = trial(args, job, manifest, commands)
                    document["resume_warmups"].append(row)
                    checkpoint(args.out, document)
        for job in manifest["plan"]:
            if job["id"] in completed:
                continue
            row = trial(args, job, manifest, commands)
            document["trials"].append(row)
            checkpoint(args.out, document)
            print(f"{job['id']}: {row['status']}", flush=True)
        rows = records(args.out, manifest)
        check_pairs(rows)
        if frozen:
            report = validation_report(rows, frozen, args.bootstrap, args.seed)
            report.update(model_sha256=model_hash, campaign_id=manifest["campaign_id"])
            document["model"] = frozen
            document["validation"] = report
        document["status"] = "complete"
        checkpoint(args.out, document)
        print(json.dumps(document["summary"]))
        print(f"Results: {path}")
        return 0 if all(r["status"] == "success" for r in rows) else 2


def numeric():
    import numpy as np
    from scipy.optimize import nnls
    return np, nnls


def basis(ns, family, power=None):
    np, _ = numeric()
    n = np.asarray(ns, dtype=float)
    cols = [np.ones_like(n), n]
    if family == "nlogn":
        cols.append(n * np.log(n))
    elif family == "power":
        cols.append(n ** power)
    return np.column_stack(cols)


def fit_candidate(ns, times, family, power=None):
    np, nnls = numeric()
    x, y = basis(ns, family, power), np.asarray(times, dtype=float)
    # Equal importance per size and relative-time residuals, not per repetition.
    weighted = x / y[:, None]
    scale = np.linalg.norm(weighted, axis=0)
    coef, _ = nnls(weighted / scale, np.ones(len(y)))
    coef /= scale
    return {"family": family, "p": power, "alpha_s": float(coef[0]),
            "beta_s_per_tx": float(coef[1]), "c": float(coef[2]) if len(coef) == 3 else 0.0,
            "scaled_condition_number": float(np.linalg.cond(weighted / scale))}


def evaluate(model, ns):
    np, _ = numeric()
    n = np.asarray(ns, dtype=float)
    a, b, c = model["alpha_s"], model["beta_s_per_tx"], model["c"]
    t, derivative = a + b * n, np.full_like(n, b)
    if model["family"] == "nlogn":
        t += c * n * np.log(n)
        derivative += c * (1 + np.log(n))
    elif model["family"] == "power":
        p = model["p"]
        t += c * n ** p
        derivative += c * p * n ** (p - 1)
    return t, n / t, 1 - n * derivative / t


def select_model(ns, times):
    np, _ = numeric()
    if len(ns) < 4:
        raise ValueError("at least four successful calibration sizes are required")
    candidates = []
    for family, power in [("affine", None), ("nlogn", None)] + [("power", p) for p in POWERS]:
        errors = []
        for i in range(len(ns)):
            keep = [j for j in range(len(ns)) if j != i]
            fold = fit_candidate([ns[j] for j in keep], [times[j] for j in keep], family, power)
            predicted = evaluate(fold, [ns[i]])[0][0]
            errors.append(abs(math.log(predicted / times[i])))
        candidates.append({"family": family, "p": power,
                           "cv_mean_abs_log_error": float(np.mean(errors)), "fold_errors": errors})
    best = min(c["cv_mean_abs_log_error"] for c in candidates)
    tolerance = max(0.02, best * 0.10)
    eligible = [c for c in candidates if c["cv_mean_abs_log_error"] <= best + tolerance]
    rank = {"affine": 0, "nlogn": 1, "power": 2}
    chosen = min(eligible, key=lambda c: (rank[c["family"]], c["cv_mean_abs_log_error"]))
    return fit_candidate(ns, times, chosen["family"], chosen["p"]), candidates


def buckets(rows, prover, scope):
    grouped = {}
    for r in rows:
        if r["prover"] == prover and not r["warmup"] and r["status"] == "success":
            value = r.get(scope)
            if value is not None and math.isfinite(value) and value > 0:
                grouped.setdefault(r["n"], []).append(value)
    return grouped


def interval(values):
    np, _ = numeric()
    return [float(x) for x in np.quantile(values, [0.025, 0.975], axis=0)]


def throughput_samples(values, n, count, rng):
    np, _ = numeric()
    if len(values) < 2:
        return None
    return n / np.mean(rng.choice(values, size=(count, len(values)), replace=True), axis=1)


def describe_fit(grouped, admissible, bootstrap, seed, max_time=None):
    np, _ = numeric()
    ns = sorted(grouped)
    if len(ns) < 4:
        return {"classification": "insufficient_data", "successful_sizes": ns,
                "recommended_batch": None, "reason": "fewer than four successful sizes"}
    means = [statistics.mean(grouped[n]) for n in ns]
    fitted, candidates = select_model(ns, means)
    t, tps, xi = evaluate(fitted, GRID)
    rng = np.random.default_rng(seed)
    boot_models, boot_tps = [], []
    uncertainty_available = all(len(grouped[n]) >= 2 for n in ns)
    if uncertainty_available:
        for _ in range(bootstrap):
            ys = [float(np.mean(rng.choice(grouped[n], len(grouped[n]), replace=True))) for n in ns]
            # Conditional parameter uncertainty: selected family and p remain fixed.
            m = fit_candidate(ns, ys, fitted["family"], fitted["p"])
            boot_models.append(m)
            boot_tps.append(evaluate(m, GRID)[1])
    predicted_ci = np.quantile(boot_tps, [0.025, 0.975], axis=0) if boot_tps else None
    predictions = [{"n": n, "time_s": float(t[i]), "tps": float(tps[i]),
                    "xi": float(xi[i]), "fixed_cost_per_tx_s": fitted["alpha_s"] / n,
                    "parameter_tps_ci95": predicted_ci[:, i].tolist() if predicted_ci is not None else None,
                    "extrapolation": n < min(ns) or n > max(ns), "used_for_fit": n in ns}
                   for i, n in enumerate(GRID)]
    allowed = [p for p in predictions if p["n"] in admissible and
               (max_time is None or p["time_s"] <= max_time)]
    recommendation = max(allowed, key=lambda p: p["tps"]) if allowed else None
    a, c = fitted["alpha_s"], fitted["c"]
    optimum = None
    if a > 0 and c > 0:
        if fitted["family"] == "nlogn":
            optimum = a / c
        elif fitted["family"] == "power":
            optimum = (a / (c * (fitted["p"] - 1))) ** (1 / fitted["p"])
        # Only accept the required positive -> negative elasticity crossing.
        if optimum and (not math.isfinite(optimum) or optimum <= 1 or
                        not (evaluate(fitted, [optimum * .999])[2][0] > 0 >
                             evaluate(fitted, [optimum * 1.001])[2][0])):
            optimum = None
    zone = []
    if recommendation:
        rec_ci = recommendation["parameter_tps_ci95"]
        zone = [p["n"] for p in allowed if p["tps"] >= .98 * recommendation["tps"] or
                (rec_ci is not None and p["parameter_tps_ci95"][1] >= rec_ci[0])]
        classification = ("uncertain_plateau" if len(zone) > 1 or not uncertainty_available else
                          "predicted_interior_maximum" if optimum and
                          min(p["n"] for p in allowed) < recommendation["n"] < max(p["n"] for p in allowed)
                          else "best_at_experimental_boundary")
    else:
        classification = "insufficient_data"
    residuals, warnings = [], []
    for n, mean in zip(ns, means):
        predicted = float(evaluate(fitted, [n])[0][0])
        sem = statistics.stdev(grouped[n]) / math.sqrt(len(grouped[n])) if len(grouped[n]) > 1 else None
        incompatible = abs(predicted - mean) > max(.1 * mean, 3 * sem if sem is not None else 0)
        residuals.append({"n": n, "observed_mean_s": mean,
                          "relative_residual": (mean - predicted) / mean,
                          "sem_s": sem, "smooth_model_mismatch": incompatible})
    if any(r["smooth_model_mismatch"] for r in residuals):
        warnings.append("residuals exceed max(10% of mean, 3 SEM); inspect artifact dimension jumps and model misspecification")
    param_ci = {k: interval([m[k] for m in boot_models]) for k in
                ("alpha_s", "beta_s_per_tx", "c")} if boot_models else None
    weak = [k for k, limits in (param_ci or {}).items()
            if limits[1] > 0 and (limits[0] == 0 or limits[1] - limits[0] > max(abs(fitted[k]), 1e-15))]
    if fitted["scaled_condition_number"] > 1000:
        warnings.append("ill-conditioned design: coefficients may not be separately identifiable")
    if weak:
        warnings.append("weakly identified coefficients: " + ", ".join(weak))
    if fitted["family"] == "power":
        warnings.append("p selected from a finite grid; parameter intervals condition on this p and omit model-selection uncertainty")
    if not uncertainty_available:
        warnings.append("at least two repetitions at every fitted size are needed for parameter uncertainty")
    aggregates = []
    for n in ns:
        draws = throughput_samples(grouped[n], n, bootstrap, rng)
        aggregates.append({"n": n, "repetitions": len(grouped[n]),
                           "tps": n * len(grouped[n]) / sum(grouped[n]),
                           "measurement_tps_ci95": interval(draws) if draws is not None else None})
    return {"classification": classification, "model": fitted, "candidates": candidates,
            "calibration_aggregates": aggregates,
            "parameter_ci95": param_ci, "predictions": predictions,
            "recommended_batch": recommendation["n"] if recommendation else None,
            "optimal_zone": zone, "continuous_maximum": optimum,
            "admissible_sizes": [p["n"] for p in allowed],
            "residuals": residuals, "warnings": warnings}


def freeze(args):
    with campaign_lock(args.campaign):
        source = read_result(args.campaign)
        manifest = source["manifest"]
        if manifest["campaign"] != "calibration":
            raise ValueError("only calibration measurements may select or fit models")
        if source.get("frozen"):
            raise ValueError("model already frozen; create a new calibration campaign to change it")
        rows = records(args.campaign, manifest)
        if len(rows) != len(manifest["plan"]):
            raise ValueError("calibration interrupted: resume it before freezing")
        check_pairs(rows)
        supported = [n for n in GRID if manifest["artifacts"][str(n)]["supported"]]
        if args.admissible_sizes:
            supported = sorted(set(supported) & set(args.admissible_sizes))
        models = {}
        for prover in manifest["config"]["provers"]:
            # Failures remain diagnostic records, never zero-TPS samples.
            failed = {r["n"] for r in rows if r["prover"] == prover and r["status"] != "success"}
            admissible = [n for n in supported if n not in failed]
            models[prover] = {scope: describe_fit(buckets(rows, prover, scope), admissible,
                                                args.bootstrap, args.seed, args.max_time)
                              for scope in SCOPES}
        model = {
            "schema": 1, "campaign": "calibration", "frozen_utc": utc_now(),
            "calibration_directory": str(args.campaign), "campaign_id": manifest["campaign_id"],
            "calibration_data_sha256": calibration_digest(args.campaign),
            "machine_signature": machine_signature(manifest["environment"]),
            "artifacts": manifest["artifacts"], "provers": manifest["config"]["provers"],
            "calibration_sizes": manifest["config"]["sizes"], "prediction_grid": GRID,
            "method": {
                "library_versions": analysis_versions(),
                "interpretation": "effective seconds on a fixed configuration; W and R not separately identified",
                "fit": "nonnegative relative-time least squares on arithmetic mean times per size",
                "selection": "leave-one-size-out mean absolute log-time error; prefer affine then nlogn then power within best+max(0.02,10% best)",
                "p_candidates": POWERS, "bootstrap": args.bootstrap, "seed": args.seed,
                "parameter_uncertainty": "within-size resampling; conditional on selected family and p; no model-selection/systematic uncertainty",
                "optimal_zone": "within 2% of best prediction OR overlapping pointwise bootstrap 95% intervals; descriptive, not a simultaneous equivalence test",
                "boundary": f"{GRID[-1]} is an experimental bound, not a physical or protocol limit",
                "max_predicted_time_s": args.max_time,
            }, "models": models,
        }
        target = args.out / RESULT_FILE
        fit_manifest = {
            "schema": 1, "campaign": "fit", "created_utc": utc_now(),
            "calibration_directory": str(args.campaign),
            "calibration_campaign_id": manifest["campaign_id"],
            "calibration_data_sha256": model["calibration_data_sha256"],
            "method": model["method"], "admissible_sizes": args.admissible_sizes}
        commit_record(target, {"schema": 2, "status": "complete", "manifest": fit_manifest,
                               "model": model})
        source["frozen"] = {
            "model_path": str(target), "model_sha256": digest(target),
            "calibration_data_sha256": model["calibration_data_sha256"]}
        save_result(args.campaign, source)
        print(f"Frozen predictions: {target}")
        for p, scopes in models.items():
            for scope, result in scopes.items():
                print(f"{p}/{scope}: {result['classification']}, N={result['recommended_batch']}")


def validation_report(rows, frozen, bootstrap, seed):
    np, _ = numeric()
    rng = np.random.default_rng(seed)
    out = {"bootstrap": bootstrap, "seed": seed, "library_versions": analysis_versions(),
           "uncertainty": "measurement bootstrap only; frozen predictions are never refit",
           "models": {}}
    for prover, scopes in frozen["models"].items():
        out["models"][prover] = {}
        for scope, fitted in scopes.items():
            grouped = buckets(rows, prover, scope)
            predictions = {p["n"]: p for p in fitted.get("predictions", [])}
            measures, draws = {}, {}
            for n, times in sorted(grouped.items()):
                tps = len(times) * n / sum(times)
                draws[n] = throughput_samples(times, n, bootstrap, rng)
                pred = predictions.get(n, {}).get("tps")
                measures[n] = {"n": n, "repetitions": len(times), "tps": tps,
                               "measurement_tps_ci95": interval(draws[n]) if draws[n] is not None else None,
                               "predicted_tps": pred,
                               "relative_tps_error": pred / tps - 1 if pred is not None else None,
                               "held_out_size": n not in frozen["calibration_sizes"]}
            elasticity = []
            for n in sorted(measures):
                if 2 * n not in measures:
                    continue
                observed = math.log(measures[2*n]["tps"] / measures[n]["tps"]) / math.log(2)
                pred = (math.log(predictions[2*n]["tps"] / predictions[n]["tps"]) / math.log(2)
                        if n in predictions and 2*n in predictions else None)
                ci = interval(np.log(draws[2*n] / draws[n]) / math.log(2)) if (
                    draws[n] is not None and draws[2*n] is not None) else None
                elasticity.append({"n": n, "next_n": 2*n, "observed": observed,
                                   "predicted_secant": pred, "measurement_ci95": ci})
            best = max(measures, key=lambda n: measures[n]["tps"]) if measures else None
            rec = fitted.get("recommended_batch")
            loss = 1 - measures[rec]["tps"] / measures[best]["tps"] if rec in measures else None
            loss_ci, zone = None, []
            if best is not None:
                best_ci = measures[best]["measurement_tps_ci95"]
                zone = [n for n, v in measures.items() if n == best or
                        (best_ci and v["measurement_tps_ci95"] and v["measurement_tps_ci95"][1] >= best_ci[0])]
                if rec in measures and all(d is not None for d in draws.values()):
                    loss_ci = interval(1 - draws[rec] / np.max(list(draws.values()), axis=0))
            errors = [abs(v["relative_tps_error"]) for v in measures.values()
                      if v["held_out_size"] and v["relative_tps_error"] is not None]
            failures = [{"id": r["id"], "n": r["n"], "status": r["status"]}
                        for r in rows if r["prover"] == prover and not r["warmup"] and r["status"] != "success"]
            out["models"][prover][scope] = {
                "measurements": list(measures.values()), "elasticity_secants": elasticity,
                "best_observed_batch": best, "observed_optimal_zone": zone,
                "recommended_batch": rec, "recommendation_loss": loss,
                "recommendation_loss_measurement_ci95": loss_ci,
                "loss_missing_reason": None if loss is not None else "recommended batch has no successful validation measurement",
                "held_out_mean_absolute_relative_tps_error": statistics.mean(errors) if errors else None,
                "failures": failures, "comparison_is_partial": bool(failures) or set(measures) != set(GRID),
                "caveat": "success-conditioned throughput; missing/failed sizes excluded, not treated as zero; pointwise intervals, no simultaneous equivalence claim",
            }
    return out


def size_list(value):
    try:
        sizes = sorted(set(int(x) for x in value.split(",")))
    except ValueError as e:
        raise argparse.ArgumentTypeError("expected comma-separated integers") from e
    if not sizes or any(n not in GRID for n in sizes):
        raise argparse.ArgumentTypeError(f"sizes must be powers of two from 1 to {GRID[-1]}")
    return sizes


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("calibration", "validation"):
        cmd = sub.add_parser(name, help="collect a separate resumable campaign")
        cmd.add_argument("--out", type=Path,
                         help="new subdirectory of bench-out (default: unique timestamped run); existing campaign with --resume")
        cmd.add_argument("--circuits-dir", type=Path, default=ROOT / "circuits")
        cmd.add_argument("--circom", default="circom", help="circuit compiler executable")
        cmd.add_argument("--no-setup", action="store_true", help="do not generate missing environments")
        cmd.add_argument("--setup-sizes", type=size_list, default=GRID,
                         help=f"environments prepared before calibration (default: all {len(GRID)} prediction sizes)")
        cmd.add_argument("--setup-timeout", type=float, default=21600,
                         help="timeout per setup command in seconds (default: 6 hours)")
        cmd.add_argument("--provers", nargs="+", choices=("rapidsnark", "snarkjs"), default=["rapidsnark", "snarkjs"])
        cmd.add_argument("--sizes", type=size_list, default=CALIBRATION if name == "calibration" else GRID)
        cmd.add_argument("--repeat", type=int, default=10)
        cmd.add_argument("--warmups", type=int, default=1)
        cmd.add_argument("--timeout", type=float, default=600, help="timeout per subprocess, seconds")
        cmd.add_argument("--sample-interval", type=float, default=.05)
        cmd.add_argument("--seed", type=int, default=20261006 if name == "calibration" else 20261007)
        cmd.add_argument("--snarkjs", default="snarkjs", help="executable or cli.cjs path; no shell string")
        cmd.add_argument("--rapidsnark", default="prover",
                         help="native binary path; default searches RAPIDSNARK_BIN, PATH, then sibling/home rapidsnark checkouts")
        cmd.add_argument("--resume", action="store_true")
        if name == "validation":
            cmd.add_argument("--model", type=Path, required=True,
                             help="frozen fit result.json")
            cmd.add_argument("--bootstrap", type=int, default=500)
    fit = sub.add_parser("fit", help="select, fit and freeze predictions using calibration only")
    fit.add_argument("--campaign", type=Path, required=True)
    fit.add_argument("--out", type=Path, help="new subdirectory of bench-out (default: unique timestamped fit run)")
    fit.add_argument("--bootstrap", type=int, default=500)
    fit.add_argument("--seed", type=int, default=17)
    fit.add_argument("--admissible-sizes", type=size_list)
    fit.add_argument("--max-time", type=float, help="optional predicted latency constraint in seconds, per scope")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.seed < 0:
        p.error("--seed must be nonnegative")
    if hasattr(args, "bootstrap") and args.bootstrap < 50:
        p.error("--bootstrap must be at least 50")
    if args.command == "fit":
        args.campaign = args.campaign.resolve()
        if args.max_time is not None and (not math.isfinite(args.max_time) or args.max_time <= 0):
            p.error("--max-time must be positive and finite")
        args.out = output_directory(args.command, args.out)
        freeze(args)
        return 0
    if (args.repeat < 1 or args.warmups < 0 or not math.isfinite(args.timeout) or args.timeout <= 0
            or not math.isfinite(args.sample_interval) or args.sample_interval < .01
            or not math.isfinite(args.setup_timeout) or args.setup_timeout <= 0):
        p.error("repeat>=1, warmups>=0, timeout>0, setup-timeout>0, sample-interval>=0.01 required")
    if len(set(args.provers)) != len(args.provers):
        p.error("duplicate provers")
    if args.command == "calibration" and not set(args.sizes) <= set(CALIBRATION):
        p.error("calibration sizes must be a subset of " + ",".join(map(str, CALIBRATION)))
    args.out = output_directory(args.command, args.out, args.resume)
    args.circuits_dir = args.circuits_dir.resolve()
    if args.command == "validation":
        args.model = args.model.resolve()
    return collect(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
