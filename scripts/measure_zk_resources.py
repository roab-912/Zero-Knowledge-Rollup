#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
measure_zk_resources.py

Benchmark ressources (CPU, RAM, I/O) et durée des trois phases d'un pipeline
zk-SNARK Groth16, pour plusieurs tailles de circuit :

  1. env    : trusted setup (powersoftau phase 1, compilation circom, phase 2 zkey)
  2. prove  : calcul du témoin + génération de la preuve (snarkjs et/ou rapidsnark)
  3. verify : vérification de la preuve

Chaque commande est exécutée en sous-processus et échantillonnée pendant son
exécution (backend /proc sous Debian, psutil si présent). Les mesures brutes,
les agrégats et les figures sont écrits dans ./bench-out/YYYYMMDD_HHmmss/.

Exemples
--------
    # jeu complet: setup + preuve + vérif pour 1..64 transactions
    python3 scripts/measure_zk_resources.py --sizes 1,2,4,8,16,32,64

    # réutilise les circuits déjà générés dans ./circuits, 5 répétitions
    python3 scripts/measure_zk_resources.py --sizes 1,2,4 --skip-setup \
        --circuits-dir circuits --repeat 5

    # comparaison snarkjs / rapidsnark (docker) sur le serveur de test
    python3 scripts/measure_zk_resources.py --sizes 1,2,4 --skip-setup \
        --prover both --rapidsnark-mode docker --docker-container debian_rapidsnark

    # regénérer uniquement les figures depuis un run existant
    python3 scripts/measure_zk_resources.py --plot-only bench-out/20260730_101500
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import math
import os
import platform
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #

try:  # consoles non-UTF8 (Windows cp1252) : ne pas casser sur les accents/flèches
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

PHASES = ("env", "prove", "verify")
PHASE_LABELS = {
    "env": "Trusted setup",
    "prove": "Proof generation",
    "verify": "Verification",
}
PHASE_COLORS = {"env": "#4C72B0", "prove": "#DD8452", "verify": "#55A868"}

# sous-étapes de la phase env, pour le graphe de décomposition
ENV_GROUPS = ("ptau", "compile", "zkey")
ENV_GROUP_LABELS = {
    "ptau": "Powers of Tau (phase 1)",
    "compile": "Circuit compilation (circom)",
    "zkey": "Groth16 setup (phase 2)",
}
ENV_GROUP_COLORS = {"ptau": "#8172B3", "compile": "#937860", "zkey": "#DA8BC3"}

MB = 1024.0 * 1024.0
GB = 1024.0 * 1024.0 * 1024.0

DISK_DEV_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|hd[a-z]+|mmcblk\d+|xvd[a-z]+)$")


# --------------------------------------------------------------------------- #
# Échantillonnage des ressources
# --------------------------------------------------------------------------- #


def _clk_tck() -> float:
    try:
        return float(os.sysconf("SC_CLK_TCK"))
    except (ValueError, AttributeError, OSError):
        return 100.0


CLK_TCK = _clk_tck()
PAGE_SIZE = 4096
try:
    PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
except (ValueError, AttributeError, OSError):
    pass

HAS_PROC = os.path.isdir("/proc") and os.path.isfile("/proc/stat")

try:  # optionnel: utilisé seulement si /proc n'est pas disponible
    import psutil  # type: ignore

    HAS_PSUTIL = True
except Exception:  # pragma: no cover
    psutil = None  # type: ignore
    HAS_PSUTIL = False


def read_system_cpu() -> Optional[Dict[str, float]]:
    """Temps CPU cumulés de la machine, en secondes."""
    if not HAS_PROC:
        return None
    try:
        with open("/proc/stat", "r") as f:
            parts = f.readline().split()
    except OSError:
        return None
    if not parts or parts[0] != "cpu":
        return None
    vals = [float(x) / CLK_TCK for x in parts[1:11]]
    keys = ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal", "guest", "guest_nice"]
    out = {k: (vals[i] if i < len(vals) else 0.0) for i, k in enumerate(keys)}
    out["total"] = sum(out[k] for k in keys)
    out["busy"] = out["total"] - out["idle"] - out["iowait"]
    return out


def read_system_mem() -> Optional[Dict[str, float]]:
    """Mémoire système en octets (total / available / used)."""
    if not HAS_PROC:
        return None
    info: Dict[str, float] = {}
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                key, _, rest = line.partition(":")
                fields = rest.split()
                if fields:
                    info[key] = float(fields[0]) * 1024.0
    except OSError:
        return None
    total = info.get("MemTotal", 0.0)
    available = info.get("MemAvailable", info.get("MemFree", 0.0))
    return {
        "total": total,
        "available": available,
        "used": total - available,
        "cached": info.get("Cached", 0.0),
        "swap_used": info.get("SwapTotal", 0.0) - info.get("SwapFree", 0.0),
    }


def read_system_disk() -> Optional[Dict[str, float]]:
    """Octets lus/écrits cumulés sur les disques physiques."""
    if not HAS_PROC:
        return None
    read_b = 0.0
    write_b = 0.0
    try:
        with open("/proc/diskstats", "r") as f:
            for line in f:
                fields = line.split()
                if len(fields) < 10:
                    continue
                name = fields[2]
                if not DISK_DEV_RE.match(name):
                    continue
                read_b += float(fields[5]) * 512.0
                write_b += float(fields[9]) * 512.0
    except OSError:
        return None
    return {"read_bytes": read_b, "write_bytes": write_b}


def _proc_children_map() -> Dict[int, List[int]]:
    """Table ppid -> [pid] construite en un seul balayage de /proc."""
    tree: Dict[int, List[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as f:
                data = f.read()
        except OSError:
            continue
        # le nom du process est entre parenthèses et peut contenir des espaces
        close = data.rfind(b")")
        if close < 0:
            continue
        rest = data[close + 2 :].split()
        if len(rest) < 2:
            continue
        try:
            ppid = int(rest[1])
        except ValueError:
            continue
        tree.setdefault(ppid, []).append(int(entry))
    return tree


def _descendants(pid: int) -> List[int]:
    tree = _proc_children_map()
    out: List[int] = []
    stack = [pid]
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(tree.get(cur, []))
    return out


def _read_proc_stat_cpu(pid: int) -> Optional[Tuple[float, float, float]]:
    """(utime, stime, rss_bytes) d'un process depuis /proc/<pid>/stat."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
    except OSError:
        return None
    close = data.rfind(b")")
    if close < 0:
        return None
    fields = data[close + 2 :].split()
    # fields[0] == state, donc utime est l'index 11 du bloc post-comm
    if len(fields) < 22:
        return None
    try:
        utime = float(fields[11]) / CLK_TCK
        stime = float(fields[12]) / CLK_TCK
        rss = float(fields[21]) * PAGE_SIZE
    except (ValueError, IndexError):
        return None
    return utime, stime, rss


def _read_proc_io(pid: int) -> Optional[Dict[str, float]]:
    try:
        with open(f"/proc/{pid}/io", "r") as f:
            out: Dict[str, float] = {}
            for line in f:
                key, _, val = line.partition(":")
                try:
                    out[key.strip()] = float(val.strip())
                except ValueError:
                    continue
            return out
    except OSError:
        return None


@dataclass
class Sample:
    """Un point de mesure."""

    t: float  # secondes depuis le début du step
    proc_rss: float = 0.0  # somme RSS de l'arbre de process (octets)
    proc_cpu_s: float = 0.0  # temps CPU cumulé de l'arbre (s)
    proc_read: float = 0.0  # read_bytes cumulés (disque)
    proc_write: float = 0.0
    proc_rchar: float = 0.0  # octets lus au niveau syscall
    proc_wchar: float = 0.0
    nproc: int = 0
    sys_cpu_busy: float = 0.0
    sys_cpu_iowait: float = 0.0
    sys_mem_used: float = 0.0
    sys_read: float = 0.0
    sys_write: float = 0.0


class ResourceSampler:
    """Échantillonne un arbre de process + la machine pendant un step.

    Les compteurs cumulatifs par process (CPU, I/O) disparaissent quand le
    process meurt : on mémorise donc le dernier maximum vu par PID et on somme,
    ce qui reste correct pour des compteurs monotones.
    """

    def __init__(self, interval: float = 0.1) -> None:
        self.interval = max(0.01, interval)
        self.samples: List[Sample] = []
        self._pid: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._t0 = 0.0
        self._cpu_by_pid: Dict[int, float] = {}
        self._io_by_pid: Dict[int, Dict[str, float]] = {}
        self.backend = "proc" if HAS_PROC else ("psutil" if HAS_PSUTIL else "none")

    # -- collecte ---------------------------------------------------------- #

    def _collect_tree_proc(self, pid: int) -> Tuple[float, int]:
        """Met à jour les compteurs cumulés; retourne (rss_instantane, nproc)."""
        rss_now = 0.0
        alive = 0
        for p in _descendants(pid):
            st = _read_proc_stat_cpu(p)
            if st is None:
                continue
            alive += 1
            utime, stime, rss = st
            cpu = utime + stime
            if cpu >= self._cpu_by_pid.get(p, 0.0):
                self._cpu_by_pid[p] = cpu
            rss_now += rss
            io = _read_proc_io(p)
            if io:
                prev = self._io_by_pid.setdefault(p, {})
                for k in ("read_bytes", "write_bytes", "rchar", "wchar"):
                    v = io.get(k, 0.0)
                    if v >= prev.get(k, 0.0):
                        prev[k] = v
        return rss_now, alive

    def _collect_tree_psutil(self, pid: int) -> Tuple[float, int]:  # pragma: no cover
        rss_now = 0.0
        alive = 0
        try:
            root = psutil.Process(pid)
            procs = [root] + root.children(recursive=True)
        except Exception:
            return 0.0, 0
        for p in procs:
            try:
                with p.oneshot():
                    cpu_times = p.cpu_times()
                    cpu = float(cpu_times.user) + float(cpu_times.system)
                    rss_now += float(p.memory_info().rss)
                    alive += 1
                    if cpu >= self._cpu_by_pid.get(p.pid, 0.0):
                        self._cpu_by_pid[p.pid] = cpu
                    try:
                        io = p.io_counters()
                        prev = self._io_by_pid.setdefault(p.pid, {})
                        vals = {
                            "read_bytes": float(getattr(io, "read_bytes", 0.0)),
                            "write_bytes": float(getattr(io, "write_bytes", 0.0)),
                            "rchar": float(getattr(io, "read_chars", 0.0) or 0.0),
                            "wchar": float(getattr(io, "write_chars", 0.0) or 0.0),
                        }
                        for k, v in vals.items():
                            if v >= prev.get(k, 0.0):
                                prev[k] = v
                    except Exception:
                        pass
            except Exception:
                continue
        return rss_now, alive

    def _snapshot(self) -> Sample:
        s = Sample(t=time.perf_counter() - self._t0)
        if self._pid is not None:
            if self.backend == "proc":
                rss, alive = self._collect_tree_proc(self._pid)
            elif self.backend == "psutil":
                rss, alive = self._collect_tree_psutil(self._pid)
            else:
                rss, alive = 0.0, 0
            s.proc_rss = rss
            s.nproc = alive
        s.proc_cpu_s = sum(self._cpu_by_pid.values())
        s.proc_read = sum(d.get("read_bytes", 0.0) for d in self._io_by_pid.values())
        s.proc_write = sum(d.get("write_bytes", 0.0) for d in self._io_by_pid.values())
        s.proc_rchar = sum(d.get("rchar", 0.0) for d in self._io_by_pid.values())
        s.proc_wchar = sum(d.get("wchar", 0.0) for d in self._io_by_pid.values())

        cpu = read_system_cpu()
        if cpu:
            s.sys_cpu_busy = cpu["busy"]
            s.sys_cpu_iowait = cpu["iowait"]
        mem = read_system_mem()
        if mem:
            s.sys_mem_used = mem["used"]
        disk = read_system_disk()
        if disk:
            s.sys_read = disk["read_bytes"]
            s.sys_write = disk["write_bytes"]
        return s

    # -- cycle de vie ------------------------------------------------------ #

    def start(self) -> None:
        self._t0 = time.perf_counter()
        self.samples = [self._snapshot()]
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def attach(self, pid: int) -> None:
        self._pid = pid

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.samples.append(self._snapshot())
            except Exception:
                continue

    def stop(self) -> List[Sample]:
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            self.samples.append(self._snapshot())
        except Exception:
            pass
        return self.samples


# --------------------------------------------------------------------------- #
# Exécution instrumentée d'une commande
# --------------------------------------------------------------------------- #


@dataclass
class StepResult:
    size: int
    phase: str
    step: str
    group: str  # sous-groupe (ptau/compile/zkey/prove/verify)
    rep: int
    prover: str  # snarkjs | rapidsnark | ""
    cmd: str
    ok: bool
    returncode: int
    scope: str  # "process" (mesures fiables) | "system" (conteneur docker)
    wall_s: float = 0.0
    cpu_user_s: float = 0.0
    cpu_sys_s: float = 0.0
    cpu_total_s: float = 0.0
    cpu_pct_of_core: float = 0.0  # cpu_total/wall*100
    cpu_pct_of_machine: float = 0.0
    rss_peak_bytes: float = 0.0
    rss_mean_bytes: float = 0.0
    io_read_bytes: float = 0.0
    io_write_bytes: float = 0.0
    io_rchar_bytes: float = 0.0
    io_wchar_bytes: float = 0.0
    io_read_mb_per_s: float = 0.0
    io_write_mb_per_s: float = 0.0
    sys_cpu_busy_s: float = 0.0
    sys_cpu_iowait_s: float = 0.0
    sys_mem_used_peak_bytes: float = 0.0
    sys_mem_used_delta_bytes: float = 0.0
    sys_read_bytes: float = 0.0
    sys_write_bytes: float = 0.0
    n_samples: int = 0
    samples_file: str = ""
    log_file: str = ""

    def to_row(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        return d


SAMPLE_FIELDS = [
    "t_s",
    "proc_rss_bytes",
    "proc_cpu_s",
    "proc_cpu_pct",
    "proc_read_bytes",
    "proc_write_bytes",
    "nproc",
    "sys_cpu_pct",
    "sys_iowait_pct",
    "sys_mem_used_bytes",
    "sys_read_bytes",
    "sys_write_bytes",
]


def write_samples_csv(path: str, samples: List[Sample], ncpu: int) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(SAMPLE_FIELDS)
        base = samples[0] if samples else None
        for i, s in enumerate(samples):
            prev = samples[i - 1] if i > 0 else None
            dt = (s.t - prev.t) if prev else 0.0
            proc_cpu_pct = ((s.proc_cpu_s - prev.proc_cpu_s) / dt * 100.0) if (prev and dt > 0) else 0.0
            sys_cpu_pct = (
                ((s.sys_cpu_busy - prev.sys_cpu_busy) / dt / max(1, ncpu) * 100.0) if (prev and dt > 0) else 0.0
            )
            iowait_pct = (
                ((s.sys_cpu_iowait - prev.sys_cpu_iowait) / dt / max(1, ncpu) * 100.0) if (prev and dt > 0) else 0.0
            )
            w.writerow(
                [
                    f"{s.t:.4f}",
                    int(s.proc_rss),
                    f"{s.proc_cpu_s:.4f}",
                    f"{max(0.0, proc_cpu_pct):.2f}",
                    int(s.proc_read - (base.proc_read if base else 0.0)),
                    int(s.proc_write - (base.proc_write if base else 0.0)),
                    s.nproc,
                    f"{max(0.0, sys_cpu_pct):.2f}",
                    f"{max(0.0, iowait_pct):.2f}",
                    int(s.sys_mem_used),
                    int(s.sys_read - (base.sys_read if base else 0.0)),
                    int(s.sys_write - (base.sys_write if base else 0.0)),
                ]
            )


class Runner:
    """Exécute des commandes en mesurant leur consommation de ressources."""

    def __init__(self, outdir: str, interval: float, ncpu: int, dry_run: bool = False, quiet: bool = False) -> None:
        self.outdir = outdir
        self.interval = interval
        self.ncpu = ncpu
        self.dry_run = dry_run
        self.quiet = quiet
        self.raw_dir = os.path.join(outdir, "raw")
        self.samples_dir = os.path.join(outdir, "raw", "samples")
        self.logs_dir = os.path.join(outdir, "logs")
        for d in (self.raw_dir, self.samples_dir, self.logs_dir):
            os.makedirs(d, exist_ok=True)
        self.steps: List[StepResult] = []
        self._jsonl = os.path.join(self.raw_dir, "steps.jsonl")

    # -- helpers ----------------------------------------------------------- #

    @staticmethod
    def _rusage_children():
        try:
            import resource

            return resource.getrusage(resource.RUSAGE_CHILDREN)
        except Exception:  # pragma: no cover (Windows)
            return None

    @staticmethod
    def _maxrss_bytes(value: float) -> float:
        # ru_maxrss est en KiB sous Linux, en octets sous macOS
        return value * 1024.0 if sys.platform != "darwin" else value

    def _slug(self, size: int, step: str, rep: int) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", step)
        return f"n{size}_{safe}_r{rep}"

    # -- exécution --------------------------------------------------------- #

    def run(
        self,
        cmd: str,
        cwd: str,
        *,
        size: int,
        phase: str,
        step: str,
        group: str,
        rep: int = 1,
        prover: str = "",
        scope: str = "process",
        env: Optional[Dict[str, str]] = None,
    ) -> StepResult:
        slug = self._slug(size, step, rep)
        log_path = os.path.join(self.logs_dir, f"{slug}.log")
        samples_path = os.path.join(self.samples_dir, f"{slug}.csv")

        res = StepResult(
            size=size,
            phase=phase,
            step=step,
            group=group,
            rep=rep,
            prover=prover,
            cmd=cmd,
            ok=True,
            returncode=0,
            scope=scope,
            log_file=os.path.relpath(log_path, self.outdir),
            samples_file=os.path.relpath(samples_path, self.outdir),
        )

        if not self.quiet:
            print(f"    → [{phase}/{step}] {cmd}")

        if self.dry_run:
            res.wall_s = 0.0
            self.steps.append(res)
            return res

        sampler = ResourceSampler(self.interval)
        ru0 = self._rusage_children()
        sampler.start()
        t0 = time.perf_counter()

        try:
            with open(log_path, "w", encoding="utf-8", errors="replace") as log:
                log.write(f"# cmd : {cmd}\n# cwd : {cwd}\n\n")
                log.flush()
                proc = subprocess.Popen(
                    shlex.split(cmd),
                    cwd=cwd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
                sampler.attach(proc.pid)
                res.returncode = proc.wait()
        except FileNotFoundError as exc:
            res.returncode = 127
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(f"\n[ERREUR] binaire introuvable: {exc}\n")
        except Exception as exc:  # pragma: no cover
            res.returncode = 1
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(f"\n[ERREUR] {exc}\n")

        res.wall_s = time.perf_counter() - t0
        samples = sampler.stop()
        ru1 = self._rusage_children()
        res.ok = res.returncode == 0

        # --- CPU : rusage des enfants (exact) sinon échantillons ---------- #
        if ru0 is not None and ru1 is not None:
            res.cpu_user_s = max(0.0, ru1.ru_utime - ru0.ru_utime)
            res.cpu_sys_s = max(0.0, ru1.ru_stime - ru0.ru_stime)
            res.cpu_total_s = res.cpu_user_s + res.cpu_sys_s
            rss_ru = self._maxrss_bytes(ru1.ru_maxrss)
            rss_ru_prev = self._maxrss_bytes(ru0.ru_maxrss)
            rss_rusage = rss_ru if rss_ru > rss_ru_prev else 0.0
        else:
            rss_rusage = 0.0

        if samples:
            first, last = samples[0], samples[-1]
            sampled_cpu = max(0.0, last.proc_cpu_s - first.proc_cpu_s)
            if res.cpu_total_s <= 0.0:
                res.cpu_total_s = sampled_cpu
            rss_values = [s.proc_rss for s in samples if s.proc_rss > 0]
            res.rss_peak_bytes = max(rss_values) if rss_values else 0.0
            res.rss_mean_bytes = statistics.mean(rss_values) if rss_values else 0.0
            if rss_rusage > res.rss_peak_bytes:
                # le sampler peut manquer un pic très bref
                res.rss_peak_bytes = rss_rusage
            res.io_read_bytes = max(0.0, last.proc_read - first.proc_read)
            res.io_write_bytes = max(0.0, last.proc_write - first.proc_write)
            res.io_rchar_bytes = max(0.0, last.proc_rchar - first.proc_rchar)
            res.io_wchar_bytes = max(0.0, last.proc_wchar - first.proc_wchar)
            res.sys_cpu_busy_s = max(0.0, last.sys_cpu_busy - first.sys_cpu_busy)
            res.sys_cpu_iowait_s = max(0.0, last.sys_cpu_iowait - first.sys_cpu_iowait)
            mem_values = [s.sys_mem_used for s in samples if s.sys_mem_used > 0]
            res.sys_mem_used_peak_bytes = max(mem_values) if mem_values else 0.0
            res.sys_mem_used_delta_bytes = (last.sys_mem_used - first.sys_mem_used) if mem_values else 0.0
            res.sys_read_bytes = max(0.0, last.sys_read - first.sys_read)
            res.sys_write_bytes = max(0.0, last.sys_write - first.sys_write)
            res.n_samples = len(samples)
            write_samples_csv(samples_path, samples, self.ncpu)
        else:
            res.rss_peak_bytes = rss_rusage

        # Pour un step exécuté dans un conteneur, les compteurs du process hôte
        # ne voient rien : on se rabat sur les compteurs système.
        if scope == "system":
            if res.sys_cpu_busy_s > 0:
                res.cpu_total_s = res.sys_cpu_busy_s
            if res.sys_mem_used_delta_bytes > 0:
                res.rss_peak_bytes = max(res.rss_peak_bytes, res.sys_mem_used_delta_bytes)
            res.io_read_bytes = max(res.io_read_bytes, res.sys_read_bytes)
            res.io_write_bytes = max(res.io_write_bytes, res.sys_write_bytes)

        if res.wall_s > 0:
            res.cpu_pct_of_core = res.cpu_total_s / res.wall_s * 100.0
            res.cpu_pct_of_machine = res.cpu_pct_of_core / max(1, self.ncpu)
            res.io_read_mb_per_s = res.io_read_bytes / MB / res.wall_s
            res.io_write_mb_per_s = res.io_write_bytes / MB / res.wall_s

        if not self.quiet:
            status = "OK " if res.ok else f"KO({res.returncode})"
            print(
                f"      {status} {res.wall_s:8.2f} s | CPU {res.cpu_total_s:8.2f} s "
                f"({res.cpu_pct_of_core:6.1f}% d'un cœur) | RSS max {res.rss_peak_bytes / MB:8.1f} MiB "
                f"| I/O R {res.io_read_bytes / MB:7.1f} / W {res.io_write_bytes / MB:7.1f} MiB"
            )

        self.steps.append(res)
        with open(self._jsonl, "a", encoding="utf-8") as f:
            f.write(json.dumps(res.to_row(), ensure_ascii=False) + "\n")
        return res


# --------------------------------------------------------------------------- #
# Séquences de commandes zk
# --------------------------------------------------------------------------- #


def ptau_power(size: int, offset: int) -> int:
    """Puissance de tau nécessaire pour `size` transactions.

    Reprend la convention du dépôt : n=1 -> 8, n=2 -> 9, n=4 -> 10, ...
    """
    return int(math.log2(size)) + offset if size > 1 else offset


@dataclass
class Command:
    step: str
    group: str
    cmd: str


def env_commands(size: int, power: int, snarkjs: str, circom: str, entropy: str) -> List[Command]:
    p = f"pot{power}"
    beacon1 = "0102030405060708090a0b0c0d0e0f101112131015161718191a1b1c1d1e1f"
    beacon2 = "0102030405060708090a0b0c0d0e0f101112131515161718191a1b1c1d1e1f"
    e = entropy
    return [
        # --- phase 1 : powers of tau ---
        Command("ptau_new", "ptau", f"{snarkjs} powersoftau new bn128 {power} {p}_0000.ptau -v"),
        Command(
            "ptau_contribute_1",
            "ptau",
            f'{snarkjs} powersoftau contribute {p}_0000.ptau {p}_0001.ptau --name="First contribution" -v -e="{e}"',
        ),
        Command(
            "ptau_contribute_2",
            "ptau",
            f'{snarkjs} powersoftau contribute {p}_0001.ptau {p}_0002.ptau --name="Second contribution" -v -e="{e}"',
        ),
        Command(
            "ptau_export_challenge",
            "ptau",
            f"{snarkjs} powersoftau export challenge {p}_0002.ptau challenge_0003",
        ),
        Command(
            "ptau_challenge_contribute",
            "ptau",
            f'{snarkjs} powersoftau challenge contribute bn128 challenge_0003 response_0003 -e="{e}"',
        ),
        Command(
            "ptau_import_response",
            "ptau",
            f'{snarkjs} powersoftau import response {p}_0002.ptau response_0003 {p}_0003.ptau -n="Third contribution"',
        ),
        Command("ptau_verify", "ptau", f"{snarkjs} powersoftau verify {p}_0003.ptau"),
        Command(
            "ptau_beacon",
            "ptau",
            f'{snarkjs} powersoftau beacon {p}_0003.ptau {p}_beacon.ptau {beacon1} 10 -n="Final Beacon"',
        ),
        Command(
            "ptau_prepare_phase2",
            "ptau",
            f"{snarkjs} powersoftau prepare phase2 {p}_beacon.ptau {p}_final.ptau -v",
        ),
        Command("ptau_verify_final", "ptau", f"{snarkjs} powersoftau verify {p}_final.ptau"),
        # --- compilation du circuit ---
        Command("circom_compile", "compile", f"{circom} --r1cs --wasm --sym --inspect circuit.circom"),
        Command("r1cs_info", "compile", f"{snarkjs} r1cs info circuit.r1cs"),
        Command("r1cs_export_json", "compile", f"{snarkjs} r1cs export json circuit.r1cs circuit.r1cs.json"),
        # --- phase 2 : zkey ---
        Command("zkey_setup", "zkey", f"{snarkjs} groth16 setup circuit.r1cs {p}_final.ptau circuit_0000.zkey"),
        Command(
            "zkey_contribute_1",
            "zkey",
            f'{snarkjs} zkey contribute circuit_0000.zkey circuit_0001.zkey --name="1st Contributor" -v -e="{e}"',
        ),
        Command(
            "zkey_contribute_2",
            "zkey",
            f'{snarkjs} zkey contribute circuit_0001.zkey circuit_0002.zkey --name="2nd Contributor" -v -e="{e}"',
        ),
        Command("zkey_export_bellman", "zkey", f"{snarkjs} zkey export bellman circuit_0002.zkey challenge_phase2_0003"),
        Command(
            "zkey_bellman_contribute",
            "zkey",
            f'{snarkjs} zkey bellman contribute bn128 challenge_phase2_0003 response_phase2_0003 -e="{e}"',
        ),
        Command(
            "zkey_import_bellman",
            "zkey",
            f'{snarkjs} zkey import bellman circuit_0002.zkey response_phase2_0003 circuit_0003.zkey '
            f'-n="Third contribution"',
        ),
        Command("zkey_verify", "zkey", f"{snarkjs} zkey verify circuit.r1cs {p}_final.ptau circuit_0003.zkey"),
        Command(
            "zkey_beacon",
            "zkey",
            f'{snarkjs} zkey beacon circuit_0003.zkey circuit_final.zkey {beacon2} 10 -n="Final Beacon phase2"',
        ),
        Command("zkey_verify_final", "zkey", f"{snarkjs} zkey verify circuit.r1cs {p}_final.ptau circuit_final.zkey"),
        Command(
            "zkey_export_vkey",
            "zkey",
            f"{snarkjs} zkey export verificationkey circuit_final.zkey verification_key.json",
        ),
        Command(
            "zkey_export_verifier",
            "zkey",
            f"{snarkjs} zkey export solidityverifier circuit_final.zkey verifier.sol",
        ),
    ]


# --------------------------------------------------------------------------- #
# Préparation des répertoires de circuit
# --------------------------------------------------------------------------- #


def generate_input_file(n: int, output_dir: str) -> str:
    """Jeu d'entrées synthétique pour `n` transactions (même forme que le dépôt)."""
    data = {
        "src": [str(i) for i in range(n)],
        "srcBalance": [1_000_000.0] * n,
        "srcBalanceAfter": [999_999.0] * n,
        "dest": [str(i) for i in range(n, 2 * n)],
        "destBalance": [1_000_000.0] * n,
        "destBalanceAfter": [1_000_001.0] * n,
        "amount": [1.0] * n,
    }
    path = os.path.join(output_dir, "input.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    return path


def prepare_circuit_dir(size: int, work_dir: str, template_dir: str) -> str:
    """Crée <work_dir>/<size>/ avec circuit.circom (tag résolu), circomlib et input.json."""
    dir_path = os.path.join(work_dir, str(size))
    os.makedirs(dir_path, exist_ok=True)

    src_circom = os.path.join(template_dir, "circuit.circom")
    if not os.path.isfile(src_circom):
        raise FileNotFoundError(f"circuit.circom introuvable dans {template_dir}")
    dst_circom = os.path.join(dir_path, "circuit.circom")
    shutil.copy2(src_circom, dst_circom)
    with open(dst_circom, "r", encoding="utf-8") as f:
        content = f.read()
    with open(dst_circom, "w", encoding="utf-8") as f:
        f.write(content.replace("XXX", str(size)))

    src_lib = os.path.join(template_dir, "circomlib")
    if os.path.isdir(src_lib):
        shutil.copytree(src_lib, os.path.join(dir_path, "circomlib"), dirs_exist_ok=True)

    generate_input_file(size, dir_path)
    return dir_path


ARTIFACTS_OF_INTEREST = (
    "circuit.r1cs",
    "circuit_final.zkey",
    "witness.wtns",
    "proof.json",
    "public.json",
    "verification_key.json",
    "verifier.sol",
)


def artifact_sizes(circuit_dir: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for name in ARTIFACTS_OF_INTEREST:
        path = os.path.join(circuit_dir, name)
        if os.path.isfile(path):
            out[name] = os.path.getsize(path)
    ptau = 0
    for entry in os.listdir(circuit_dir) if os.path.isdir(circuit_dir) else []:
        if entry.endswith(".ptau"):
            ptau += os.path.getsize(os.path.join(circuit_dir, entry))
    if ptau:
        out["ptau_total"] = ptau
    return out


def r1cs_constraints(snarkjs: str, circuit_dir: str) -> Optional[int]:
    """Nombre de contraintes du circuit, extrait de `snarkjs r1cs info`."""
    r1cs = os.path.join(circuit_dir, "circuit.r1cs")
    if not os.path.isfile(r1cs):
        return None
    try:
        proc = subprocess.run(
            shlex.split(f"{snarkjs} r1cs info circuit.r1cs"),
            cwd=circuit_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except Exception:
        return None
    m = re.search(r"# of Constraints:\s*(\d+)", proc.stdout + proc.stderr)
    return int(m.group(1)) if m else None


def prune_heavy_artifacts(circuit_dir: str) -> int:
    """Supprime les fichiers intermédiaires volumineux; retourne les octets libérés."""
    freed = 0
    patterns = (
        lambda n: n.endswith(".ptau"),
        lambda n: re.fullmatch(r"circuit_000\d\.zkey", n) is not None,
        lambda n: n.startswith("challenge") or n.startswith("response"),
        lambda n: n == "circuit.r1cs.json",
        lambda n: n == "witness.wtns",
    )
    for entry in os.listdir(circuit_dir):
        path = os.path.join(circuit_dir, entry)
        if not os.path.isfile(path):
            continue
        if any(p(entry) for p in patterns):
            try:
                freed += os.path.getsize(path)
                os.remove(path)
            except OSError:
                pass
    return freed


# --------------------------------------------------------------------------- #
# Détection de rapidsnark
# --------------------------------------------------------------------------- #


@dataclass
class RapidsnarkConfig:
    available: bool = False
    mode: str = "none"  # native | docker-exec | docker-run | none
    binary: str = ""
    container: str = ""
    image: str = ""
    mount_prefix: str = ""
    reason: str = ""
    mounts: List[Tuple[str, str]] = field(default_factory=list)
    use_mounts: bool = False  # traduire les chemins via .Mounts plutôt que par convention

    def describe(self) -> str:
        if not self.available:
            return f"indisponible ({self.reason})"
        if self.mode == "native":
            return f"binaire natif : {self.binary}"
        if self.mode == "docker-exec":
            how = "chemins via les montages du conteneur" if self.use_mounts else f"préfixe {self.mount_prefix}"
            return f"docker exec {self.container} ({how}, prover {self.binary})"
        return f"docker run {self.image}"


NATIVE_CANDIDATES = (
    "rapidsnark",
    "prover",
)
NATIVE_PATH_CANDIDATES = (
    "circuits/rapidsnark/package/bin/prover",
    "circuits/rapidsnark/build/prover",
    "rapidsnark/package/bin/prover",
)


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30
        ).returncode == 0
    except Exception:
        return False


def container_running(name: str) -> bool:
    try:
        proc = subprocess.run(
            ["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return False
    return name in proc.stdout.split()


def image_available(image: str) -> bool:
    if not image:
        return False
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, timeout=60
        )
    except Exception:
        return False
    return proc.returncode == 0


def container_has_path(container: str, path: str, flag: str = "-e") -> bool:
    try:
        proc = subprocess.run(
            ["docker", "exec", container, "test", flag, path], capture_output=True, timeout=30
        )
    except Exception:
        return False
    return proc.returncode == 0


def container_resolve(container: str, path: str, flag: str = "-e") -> str:
    """Essaie le chemin tel quel puis avec un '/' initial.

    generate_proofs.py invoque le prover avec des chemins relatifs
    (`mnt/projet/...`), ce qui suppose que le répertoire de travail du
    conteneur est `/`. On accepte les deux formes.
    """
    for cand in (path, "/" + path.lstrip("/")):
        if container_has_path(container, cand, flag):
            return cand
    return ""


def docker_mounts(container: str) -> List[Tuple[str, str]]:
    """Liste des montages (source hôte, destination conteneur)."""
    try:
        proc = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                '{{range .Mounts}}{{.Source}}\t{{.Destination}}{{"\\n"}}{{end}}',
                container,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    mounts: List[Tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        if "\t" not in line:
            continue
        src, dst = (p.strip() for p in line.split("\t", 1))
        if src and dst:
            mounts.append((src, dst))
    return mounts


def host_to_container(path: str, mounts: Sequence[Tuple[str, str]]) -> str:
    """Traduit un chemin hôte en chemin conteneur via les montages (plus long préfixe)."""
    def norm(p: str) -> str:
        return os.path.realpath(p).replace(os.sep, "/")

    real = norm(path)
    best: Tuple[int, str] = (-1, "")
    for src, dst in mounts:
        src_real = norm(src).rstrip("/") or "/"
        if real == src_real:
            rel = ""
        elif real.startswith(src_real + "/"):
            rel = real[len(src_real) + 1 :]
        else:
            continue
        cand = dst.rstrip("/") + ("/" + rel if rel else "")
        if len(src_real) > best[0]:
            best = (len(src_real), cand)
    return best[1]


def detect_rapidsnark(
    args: argparse.Namespace, repo_root: str, sizes: Sequence[int], work_dir: str
) -> RapidsnarkConfig:
    mode = args.rapidsnark_mode

    if mode in ("auto", "native"):
        binary = args.rapidsnark_bin or ""
        if not binary:
            for cand in NATIVE_CANDIDATES:
                found = shutil.which(cand)
                if found:
                    binary = found
                    break
        if not binary:
            for rel in NATIVE_PATH_CANDIDATES:
                cand = os.path.join(repo_root, rel)
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    binary = os.path.abspath(cand)
                    break
        if binary and (os.path.isfile(binary) or shutil.which(binary)):
            return RapidsnarkConfig(available=True, mode="native", binary=binary)
        if mode == "native":
            return RapidsnarkConfig(reason="aucun binaire rapidsnark trouvé (--rapidsnark-bin)")

    if mode in ("auto", "docker"):
        if not docker_available():
            return RapidsnarkConfig(reason="docker indisponible et pas de binaire natif")
        container = args.docker_container
        if container and container_running(container):
            prefix = args.docker_mount_prefix.rstrip("/")
            mounts = docker_mounts(container)

            # 1. le binaire prover doit exister dans le conteneur
            prover = container_resolve(container, args.docker_prover_path, "-x")
            if not prover:
                if not args.docker_image:
                    return RapidsnarkConfig(
                        reason=(
                            f"conteneur {container} actif mais le prover "
                            f"{args.docker_prover_path} y est introuvable "
                            f"(voir --docker-prover-path)"
                        ),
                        mounts=mounts,
                    )
            else:
                # 2. le dossier des circuits doit être visible depuis le conteneur.
                #    On privilégie la traduction via les montages réels, avec repli
                #    sur la convention de generate_proofs.py (mnt/projet/<n>).
                probe_host = os.path.join(work_dir, str(sizes[0])) if sizes else work_dir
                mapped = host_to_container(probe_host, mounts)
                convention = f"{prefix}/{sizes[0]}" if sizes else prefix
                resolved = ""
                for cand in ([mapped] if mapped else []) + [convention]:
                    resolved = container_resolve(container, cand)
                    if resolved:
                        break
                if resolved:
                    return RapidsnarkConfig(
                        available=True,
                        mode="docker-exec",
                        container=container,
                        binary=prover,
                        mount_prefix=prefix,
                        mounts=mounts,
                        use_mounts=bool(mapped) and resolved != convention,
                    )
                if not args.docker_image:
                    detail = (
                        f"montages du conteneur : "
                        + ", ".join(f"{s} -> {d}" for s, d in mounts)
                        if mounts
                        else "aucun montage déclaré"
                    )
                    return RapidsnarkConfig(
                        reason=(
                            f"conteneur {container} actif mais le dossier des circuits "
                            f"({probe_host}) n'y est pas visible ; essayé "
                            f"{mapped or '-'} et {convention}. {detail}. "
                            f"Utilisez --work-dir sur le dossier monté, "
                            f"--docker-mount-prefix, ou --docker-image"
                        ),
                        mounts=mounts,
                    )
        if image_available(args.docker_image):
            return RapidsnarkConfig(
                available=True,
                mode="docker-run",
                image=args.docker_image,
                binary=args.docker_prover_path,
            )
        if args.docker_image:
            return RapidsnarkConfig(reason=f"image docker {args.docker_image} absente localement")
        return RapidsnarkConfig(reason="ni conteneur rapidsnark actif ni --docker-image fourni")

    return RapidsnarkConfig(reason="mode 'off'")


def rapidsnark_command(cfg: RapidsnarkConfig, size: int, circuit_dir: str) -> Tuple[str, str]:
    """Retourne (commande, scope de mesure) pour le prover rapidsnark."""
    if cfg.mode == "native":
        return (
            f"{cfg.binary} circuit_final.zkey witness.wtns proof.json public.json",
            "process",
        )
    if cfg.mode == "docker-exec":
        # même invocation que generate_proofs.py :
        #   docker exec <c> mnt/projet/rapidsnark/package/bin/prover \
        #       mnt/projet/<n>/{circuit_final.zkey,witness.wtns,proof.json,public.json}
        base = ""
        if cfg.use_mounts:
            base = host_to_container(circuit_dir, cfg.mounts)
        if not base:
            base = f"{cfg.mount_prefix}/{size}"
        return (
            f"docker exec {cfg.container} {cfg.binary} "
            f"{base}/circuit_final.zkey {base}/witness.wtns {base}/proof.json {base}/public.json",
            "system",
        )
    # docker-run : on monte le dossier du circuit dans le conteneur
    abs_dir = os.path.abspath(circuit_dir)
    return (
        f"docker run --rm -v {abs_dir}:/work -w /work {cfg.image} {cfg.binary} "
        f"circuit_final.zkey witness.wtns proof.json public.json",
        "system",
    )


# --------------------------------------------------------------------------- #
# Agrégation
# --------------------------------------------------------------------------- #

METRICS = (
    "wall_s",
    "cpu_total_s",
    "cpu_user_s",
    "cpu_sys_s",
    "rss_peak_bytes",
    "io_read_bytes",
    "io_write_bytes",
    "io_rchar_bytes",
    "io_wchar_bytes",
    "sys_cpu_iowait_s",
)


def aggregate(steps: List[StepResult]) -> Dict[str, Any]:
    """Agrège les steps par (size, phase, prover) et par (size, groupe env)."""
    phases: Dict[Tuple[int, str, str], Dict[str, Any]] = {}
    for s in steps:
        key = (s.size, s.phase, s.prover)
        # une répétition = une exécution complète de la phase ; on somme les
        # steps d'une même répétition, puis on moyenne sur les répétitions
        entry = phases.setdefault(key, {"reps": {}})
        rep = entry["reps"].setdefault(s.rep, {m: 0.0 for m in METRICS})
        for m in METRICS:
            if m == "rss_peak_bytes":
                rep[m] = max(rep[m], getattr(s, m))
            else:
                rep[m] += getattr(s, m)
        rep["ok"] = rep.get("ok", True) and s.ok

    out_phases: List[Dict[str, Any]] = []
    for (size, phase, prover), entry in sorted(phases.items()):
        reps = list(entry["reps"].values())
        row: Dict[str, Any] = {"n": size, "phase": phase, "prover": prover, "n_reps": len(reps)}
        row["ok"] = all(r.get("ok", True) for r in reps)
        for m in METRICS:
            vals = [r[m] for r in reps]
            row[f"{m}_mean"] = statistics.mean(vals)
            row[f"{m}_min"] = min(vals)
            row[f"{m}_max"] = max(vals)
            row[f"{m}_stdev"] = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        wall = row["wall_s_mean"]
        row["cpu_pct_of_core"] = (row["cpu_total_s_mean"] / wall * 100.0) if wall > 0 else 0.0
        out_phases.append(row)

    groups: Dict[Tuple[int, str], Dict[str, float]] = {}
    for s in steps:
        if s.phase != "env":
            continue
        g = groups.setdefault((s.size, s.group), {m: 0.0 for m in METRICS})
        for m in METRICS:
            if m == "rss_peak_bytes":
                g[m] = max(g[m], getattr(s, m))
            else:
                g[m] += getattr(s, m)

    out_groups = [
        dict({"n": size, "group": group}, **{f"{m}_sum": vals[m] for m in METRICS})
        for (size, group), vals in sorted(groups.items())
    ]
    return {"phases": out_phases, "env_groups": out_groups}


def phase_lookup(phases: List[Dict[str, Any]], prover: str) -> Dict[Tuple[int, str], Dict[str, Any]]:
    """Table (n, phase) -> agrégat, restreinte à un prover (les steps sans
    prover, comme la phase environnement, sont toujours conservés)."""
    out: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for row in phases:
        if row["prover"] and prover and row["prover"] != prover:
            continue
        out[(row["n"], row["phase"])] = row
    return out


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #


def setup_style(use_science: bool, big_font: bool) -> None:
    import matplotlib as mpl

    mpl.use("Agg")
    import matplotlib.pyplot as plt

    if use_science:
        try:
            import scienceplots  # noqa: F401

            plt.style.use(["science", "grid"])
        except Exception:
            print("[info] scienceplots absent, style matplotlib par défaut")
    mpl.rcParams["text.usetex"] = False
    mpl.rcParams["mathtext.fontset"] = "dejavusans"
    mpl.rcParams["font.family"] = "DejaVu Sans"
    mpl.rcParams["figure.autolayout"] = False
    # hachures fines : elles ne doivent qu'appuyer le libellé sous la barre,
    # pas concurrencer la couleur qui porte l'information de phase
    mpl.rcParams["hatch.linewidth"] = 0.4
    if big_font:
        mpl.rcParams.update(
            {
                "font.size": 16,
                "axes.titlesize": 20,
                "axes.labelsize": 18,
                "xtick.labelsize": 15,
                "ytick.labelsize": 15,
                "legend.fontsize": 15,
            }
        )


NO_TITLES = False  # --no-titles : figures destinées à un article, titre en légende


def save_fig(fig, figs_dir: str, name: str, manifest_figs: Dict[str, str]) -> None:
    if NO_TITLES:
        for ax in fig.axes:
            ax.set_title("")
    for ext in ("png", "svg"):
        path = os.path.join(figs_dir, f"{name}.{ext}")
        fig.savefig(path, dpi=200 if ext == "png" else None, bbox_inches="tight")
    manifest_figs[name] = f"figs/{name}.png"
    import matplotlib.pyplot as plt

    plt.close(fig)
    print(f"    figure : figs/{name}.png")


def _xpos(sizes: Sequence[int]):
    import numpy as np

    return np.arange(len(sizes))


def pct_label(text: str) -> str:
    """Libellé d'axe en pourcentage, en échappant `%` si LaTeX est actif."""
    import matplotlib.pyplot as plt

    sign = "\\%" if plt.rcParams.get("text.usetex") else "%"
    return f"{text} ({sign})"


PROVER_SHORT = {"snarkjs": "sjs", "rapidsnark": "rs"}
PROVER_HATCH = ("", "///", "...")


def fig_stacked(
    agg: Dict[str, Any],
    sizes: Sequence[int],
    provers: Sequence[str],
    metric: str,
    scale: float,
    ylabel: str,
    title: str,
    figs_dir: str,
    manifest_figs: Dict[str, str],
    name: str,
    annotate_total: bool = True,
) -> None:
    """Barres verticales empilées env / prove / verify.

    Une barre par taille de circuit, ou une barre par (taille, prover) quand
    plusieurs provers ont été mesurés : la phase de preuve diffère alors d'une
    colonne à l'autre, tandis que l'environnement et la vérification, communs,
    sont répétés à l'identique pour que les totaux restent comparables.
    """
    import matplotlib.pyplot as plt
    import numpy as np

    provers = [p for p in provers if p] or [""]
    n_prov = len(provers)
    x = _xpos(sizes)
    width = 0.68 / n_prov
    fig, ax = plt.subplots(
        figsize=(max(7.0, (1.15 * n_prov) * len(sizes) + 4.0), 5.5)
    )

    any_data = False
    totals: List[Tuple[float, float]] = []
    minor_ticks: List[float] = []
    minor_labels: List[str] = []

    for j, prover in enumerate(provers):
        table = phase_lookup(agg["phases"], prover)
        offset = (j - (n_prov - 1) / 2) * width
        pos = x + offset
        bottoms = np.zeros(len(sizes))
        for phase in PHASES:
            vals = np.array(
                [table.get((n, phase), {}).get(f"{metric}_mean", 0.0) / scale for n in sizes],
                dtype=float,
            )
            if not vals.any():
                continue
            any_data = True
            ax.bar(
                pos,
                vals,
                bottom=bottoms,
                # une seule entrée de légende par phase
                label=PHASE_LABELS[phase] if j == 0 else None,
                color=PHASE_COLORS[phase],
                hatch=PROVER_HATCH[j % len(PROVER_HATCH)],
                edgecolor="black",
                linewidth=0.4,
                width=width,
            )
            bottoms += vals
        totals.extend(zip(pos, bottoms))
        if n_prov > 1:
            minor_ticks.extend(pos)
            minor_labels.extend([PROVER_SHORT.get(prover, prover)] * len(sizes))

    if not any_data:
        plt.close(fig)
        return

    if annotate_total:
        for xi, total in totals:
            if total > 0:
                ax.annotate(
                    f"{total:.4g}",
                    (xi, total),
                    textcoords="offset points",
                    xytext=(0, 4),
                    ha="center",
                    fontsize=8 if n_prov > 1 else 9,
                )

    ax.set_xticks(x)
    ax.set_xticklabels([str(n) for n in sizes])
    if n_prov > 1:
        # nom du prover sous chaque barre, taille du circuit sous le groupe
        ax.set_xticks(minor_ticks, minor=True)
        ax.set_xticklabels(minor_labels, minor=True, fontsize=8)
        ax.tick_params(axis="x", which="minor", length=0)
        ax.tick_params(axis="x", which="major", pad=16, length=0)
        # rappel du codage des hachures
        for j, prover in enumerate(provers):
            ax.bar(
                [np.nan],
                [np.nan],
                color="white",
                hatch=PROVER_HATCH[j % len(PROVER_HATCH)],
                edgecolor="black",
                linewidth=0.4,
                label=f"{prover} ({PROVER_SHORT.get(prover, prover)})",
            )
    ax.set_xlabel("Batch size $N$ (transactions per proof)")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    # marge en haut pour que la légende ne recouvre aucune barre
    peak = max((t for _, t in totals), default=0.0)
    if peak > 0:
        ax.set_ylim(0, peak * (1.34 if n_prov > 1 else 1.22))
    ax.legend(loc="upper left", ncol=2 if n_prov > 1 else 1, framealpha=0.9)
    ax.grid(True, axis="y", ls="--", lw=0.5, alpha=0.6)
    save_fig(fig, figs_dir, name, manifest_figs)


def fig_stacked_log(
    agg: Dict[str, Any],
    sizes: Sequence[int],
    prover: str,
    figs_dir: str,
    manifest_figs: Dict[str, str],
) -> None:
    """Même graphe empilé que le principal, mais en échelle log (setup >> verify)."""
    import matplotlib.pyplot as plt
    import numpy as np

    table = phase_lookup(agg["phases"], prover)
    x = _xpos(sizes)
    width = 0.26
    fig, ax = plt.subplots(figsize=(max(7.0, 1.2 * len(sizes) + 4.0), 5.5))
    for i, phase in enumerate(PHASES):
        vals = [table.get((n, phase), {}).get("wall_s_mean", 0.0) for n in sizes]
        if not any(vals):
            continue
        ax.bar(
            x + (i - 1) * width,
            vals,
            width=width,
            label=PHASE_LABELS[phase],
            color=PHASE_COLORS[phase],
            edgecolor="black",
            linewidth=0.4,
        )
    ax.set_yscale("log")
    ax.set_xticks(x)
    ax.set_xticklabels([str(n) for n in sizes])
    ax.set_xlabel("Batch size $N$ (transactions per proof)")
    ax.set_ylabel("Wall-clock time (s, log scale)")
    ax.set_title("Per-phase wall-clock time (logarithmic scale)")
    ax.legend()
    ax.grid(True, axis="y", which="both", ls="--", lw=0.5, alpha=0.6)
    save_fig(fig, figs_dir, "05_duree_par_phase_log", manifest_figs)


def fig_share(
    agg: Dict[str, Any], sizes: Sequence[int], prover: str, figs_dir: str, manifest_figs: Dict[str, str]
) -> None:
    """Part relative (%) de chaque phase dans la durée totale."""
    import matplotlib.pyplot as plt
    import numpy as np

    table = phase_lookup(agg["phases"], prover)
    x = _xpos(sizes)
    totals = np.array(
        [sum(table.get((n, p), {}).get("wall_s_mean", 0.0) for p in PHASES) for n in sizes], dtype=float
    )
    if not totals.any():
        return
    bottoms = np.zeros(len(sizes))
    fig, ax = plt.subplots(figsize=(max(7.0, 1.15 * len(sizes) + 4.0), 5.5))
    with np.errstate(divide="ignore", invalid="ignore"):
        for phase in PHASES:
            vals = np.array([table.get((n, phase), {}).get("wall_s_mean", 0.0) for n in sizes], dtype=float)
            pct = np.where(totals > 0, vals / totals * 100.0, 0.0)
            ax.bar(
                x,
                pct,
                bottom=bottoms,
                label=PHASE_LABELS[phase],
                color=PHASE_COLORS[phase],
                edgecolor="black",
                linewidth=0.4,
                width=0.68,
            )
            bottoms += pct
    ax.set_ylim(0, 100)
    ax.set_xticks(x)
    ax.set_xticklabels([str(n) for n in sizes])
    ax.set_xlabel("Batch size $N$ (transactions per proof)")
    ax.set_ylabel(pct_label("Share of total wall-clock time"))
    ax.set_title("Relative time distribution across pipeline phases")
    ax.legend(loc="lower right")
    save_fig(fig, figs_dir, "06_repartition_relative", manifest_figs)


def fig_env_breakdown(
    agg: Dict[str, Any], sizes: Sequence[int], figs_dir: str, manifest_figs: Dict[str, str]
) -> None:
    """Décomposition de la phase environnement en ptau / compilation / zkey."""
    import matplotlib.pyplot as plt
    import numpy as np

    table = {(r["n"], r["group"]): r for r in agg["env_groups"]}
    if not table:
        return
    x = _xpos(sizes)
    bottoms = np.zeros(len(sizes))
    fig, ax = plt.subplots(figsize=(max(7.0, 1.15 * len(sizes) + 4.0), 5.5))
    any_data = False
    for group in ENV_GROUPS:
        vals = np.array([table.get((n, group), {}).get("wall_s_sum", 0.0) for n in sizes], dtype=float)
        if not vals.any():
            continue
        any_data = True
        ax.bar(
            x,
            vals,
            bottom=bottoms,
            label=ENV_GROUP_LABELS[group],
            color=ENV_GROUP_COLORS[group],
            edgecolor="black",
            linewidth=0.4,
            width=0.68,
        )
        bottoms += vals
    if not any_data:
        plt.close(fig)
        return
    ax.set_xticks(x)
    ax.set_xticklabels([str(n) for n in sizes])
    ax.set_xlabel("Batch size $N$ (transactions per proof)")
    ax.set_ylabel("Wall-clock time (s)")
    ax.set_title("Breakdown of the trusted-setup phase")
    ax.legend()
    ax.grid(True, axis="y", ls="--", lw=0.5, alpha=0.6)
    save_fig(fig, figs_dir, "07_decomposition_environnement", manifest_figs)


def fig_rss_grouped(
    agg: Dict[str, Any], sizes: Sequence[int], prover: str, figs_dir: str, manifest_figs: Dict[str, str]
) -> None:
    """Pic de RSS par phase (barres groupées : un pic ne s'additionne pas)."""
    import matplotlib.pyplot as plt

    table = phase_lookup(agg["phases"], prover)
    x = _xpos(sizes)
    width = 0.26
    fig, ax = plt.subplots(figsize=(max(7.0, 1.2 * len(sizes) + 4.0), 5.5))
    any_data = False
    for i, phase in enumerate(PHASES):
        vals = [table.get((n, phase), {}).get("rss_peak_bytes_mean", 0.0) / MB for n in sizes]
        if not any(vals):
            continue
        any_data = True
        ax.bar(
            x + (i - 1) * width,
            vals,
            width=width,
            label=PHASE_LABELS[phase],
            color=PHASE_COLORS[phase],
            edgecolor="black",
            linewidth=0.4,
        )
    if not any_data:
        plt.close(fig)
        return
    ax.set_xticks(x)
    ax.set_xticklabels([str(n) for n in sizes])
    ax.set_xlabel("Batch size $N$ (transactions per proof)")
    ax.set_ylabel("Peak resident set size (MiB)")
    ax.set_title("Peak memory footprint per phase")
    ax.legend()
    ax.grid(True, axis="y", ls="--", lw=0.5, alpha=0.6)
    save_fig(fig, figs_dir, "03_memoire_pic_par_phase", manifest_figs)


def fig_io(
    agg: Dict[str, Any], sizes: Sequence[int], prover: str, figs_dir: str, manifest_figs: Dict[str, str]
) -> None:
    """Volumes I/O disque lus / écrits, empilés par phase."""
    import matplotlib.pyplot as plt
    import numpy as np

    table = phase_lookup(agg["phases"], prover)
    x = _xpos(sizes)
    width = 0.38
    fig, ax = plt.subplots(figsize=(max(7.5, 1.25 * len(sizes) + 4.0), 5.5))
    any_data = False
    hatches = {"env": "", "prove": "//", "verify": ".."}
    for offset, metric, label_suffix in ((-0.5, "io_read_bytes_mean", "read"), (0.5, "io_write_bytes_mean", "write")):
        bottoms = np.zeros(len(sizes))
        for phase in PHASES:
            vals = np.array([table.get((n, phase), {}).get(metric, 0.0) / MB for n in sizes], dtype=float)
            if not vals.any():
                continue
            any_data = True
            ax.bar(
                x + offset * width,
                vals,
                bottom=bottoms,
                width=width,
                color=PHASE_COLORS[phase],
                hatch=hatches[phase],
                edgecolor="black",
                linewidth=0.4,
                label=f"{PHASE_LABELS[phase]} — {label_suffix}",
            )
            bottoms += vals
    if not any_data:
        plt.close(fig)
        return
    ax.set_xticks(x)
    ax.set_xticklabels([str(n) for n in sizes])
    ax.set_xlabel("Batch size $N$ (transactions per proof)\n(left bar: read, right bar: write)")
    ax.set_ylabel("Disk I/O volume (MiB)")
    ax.set_title("Disk I/O load per phase")
    ax.legend(fontsize=8, ncol=2)
    ax.grid(True, axis="y", ls="--", lw=0.5, alpha=0.6)
    save_fig(fig, figs_dir, "04_io_par_phase", manifest_figs)


def fig_scaling(
    agg: Dict[str, Any], sizes: Sequence[int], prover: str, figs_dir: str, manifest_figs: Dict[str, str]
) -> None:
    """Passage à l'échelle : durée par phase en log-log."""
    import matplotlib.pyplot as plt

    table = phase_lookup(agg["phases"], prover)
    if len(sizes) < 2:
        return
    fig, ax = plt.subplots(figsize=(8.0, 5.5))
    markers = {"env": "o", "prove": "^", "verify": "s"}
    any_data = False
    for phase in PHASES:
        pts = [(n, table[(n, phase)]["wall_s_mean"]) for n in sizes if (n, phase) in table]
        pts = [(n, v) for n, v in pts if v > 0]
        if not pts:
            continue
        any_data = True
        ax.plot(
            [p[0] for p in pts],
            [p[1] for p in pts],
            marker=markers[phase],
            color=PHASE_COLORS[phase],
            label=PHASE_LABELS[phase],
        )
    if not any_data:
        plt.close(fig)
        return
    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    ax.set_xlabel("Batch size $N$ (transactions per proof)")
    ax.set_ylabel("Wall-clock time (s)")
    ax.set_title("Scaling of pipeline phases with batch size")
    ax.legend()
    ax.grid(True, which="both", ls="--", lw=0.5, alpha=0.6)
    save_fig(fig, figs_dir, "08_passage_echelle", manifest_figs)


def fig_cpu_efficiency(
    agg: Dict[str, Any], sizes: Sequence[int], prover: str, ncpu: int, figs_dir: str, manifest_figs: Dict[str, str]
) -> None:
    """Parallélisme effectif : temps CPU / temps écoulé (1 = un cœur saturé)."""
    import matplotlib.pyplot as plt

    table = phase_lookup(agg["phases"], prover)
    fig, ax = plt.subplots(figsize=(8.0, 5.5))
    x = _xpos(sizes)
    width = 0.26
    any_data = False
    for i, phase in enumerate(PHASES):
        vals = []
        for n in sizes:
            row = table.get((n, phase))
            wall = row["wall_s_mean"] if row else 0.0
            vals.append((row["cpu_total_s_mean"] / wall) if (row and wall > 0) else 0.0)
        if not any(vals):
            continue
        any_data = True
        ax.bar(
            x + (i - 1) * width,
            vals,
            width=width,
            label=PHASE_LABELS[phase],
            color=PHASE_COLORS[phase],
            edgecolor="black",
            linewidth=0.4,
        )
    if not any_data:
        plt.close(fig)
        return
    ax.axhline(1.0, color="black", ls=":", lw=1.0, label="one saturated core")
    if ncpu > 1:
        ax.axhline(float(ncpu), color="grey", ls="--", lw=1.0, label=f"{ncpu} cores (machine)")
    ax.set_xticks(x)
    ax.set_xticklabels([str(n) for n in sizes])
    ax.set_xlabel("Batch size $N$ (transactions per proof)")
    ax.set_ylabel("Mean cores busy (CPU time / wall-clock time)")
    ax.set_title("Effective parallelism per phase")
    ax.legend(fontsize=9)
    ax.grid(True, axis="y", ls="--", lw=0.5, alpha=0.6)
    save_fig(fig, figs_dir, "09_parallelisme_effectif", manifest_figs)


def fig_prover_comparison(
    agg: Dict[str, Any], sizes: Sequence[int], figs_dir: str, manifest_figs: Dict[str, str]
) -> None:
    """snarkjs vs rapidsnark sur la phase de preuve, si les deux sont mesurés."""
    import matplotlib.pyplot as plt

    provers = sorted({r["prover"] for r in agg["phases"] if r["phase"] == "prove" and r["prover"]})
    if len(provers) < 2:
        return
    fig, ax = plt.subplots(figsize=(8.0, 5.5))
    x = _xpos(sizes)
    width = 0.8 / len(provers)
    colors = ["#DD8452", "#4C72B0", "#55A868"]
    for i, prover in enumerate(provers):
        table = {r["n"]: r for r in agg["phases"] if r["phase"] == "prove" and r["prover"] == prover}
        vals = [table.get(n, {}).get("wall_s_mean", 0.0) for n in sizes]
        ax.bar(
            x + (i - (len(provers) - 1) / 2) * width,
            vals,
            width=width,
            label=prover,
            color=colors[i % len(colors)],
            edgecolor="black",
            linewidth=0.4,
        )
    ax.set_xticks(x)
    ax.set_xticklabels([str(n) for n in sizes])
    ax.set_xlabel("Batch size $N$ (transactions per proof)")
    ax.set_ylabel("Proof generation time (s)")
    ax.set_title("Prover comparison: proof generation time")
    ax.legend()
    ax.grid(True, axis="y", ls="--", lw=0.5, alpha=0.6)
    save_fig(fig, figs_dir, "10_comparaison_provers", manifest_figs)


def fig_timeline(
    outdir: str,
    steps: List[Dict[str, Any]],
    size: int,
    ncpu: int,
    figs_dir: str,
    manifest_figs: Dict[str, str],
) -> None:
    """Chronogramme CPU% / RSS d'un run complet pour une taille donnée."""
    import matplotlib.pyplot as plt

    rows = [s for s in steps if s["size"] == size and s.get("samples_file")]
    rows = [s for s in rows if os.path.isfile(os.path.join(outdir, s["samples_file"]))]
    if not rows:
        return

    t_cursor = 0.0
    cpu_x: List[float] = []
    cpu_y: List[float] = []
    rss_x: List[float] = []
    rss_y: List[float] = []
    spans: List[Tuple[float, float, str]] = []

    for s in rows:
        path = os.path.join(outdir, s["samples_file"])
        t_start = t_cursor
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for rec in csv.DictReader(f):
                    t = t_cursor + float(rec["t_s"])
                    cpu_x.append(t)
                    cpu_y.append(float(rec["proc_cpu_pct"]) / max(1, ncpu))
                    rss_x.append(t)
                    rss_y.append(float(rec["proc_rss_bytes"]) / MB)
        except (OSError, KeyError, ValueError):
            continue
        t_cursor = t_start + max(float(s.get("wall_s", 0.0)), (cpu_x[-1] - t_start) if cpu_x else 0.0)
        spans.append((t_start, t_cursor, s["phase"]))

    if not cpu_x:
        return

    def smooth(values: List[float], window: int = 5) -> List[float]:
        """Moyenne glissante : le CPU instantané est quantifié par les ticks
        d'horloge, la courbe brute est donc très bruitée."""
        if len(values) < window:
            return values
        half = window // 2
        out = []
        for i in range(len(values)):
            lo = max(0, i - half)
            hi = min(len(values), i + half + 1)
            out.append(sum(values[lo:hi]) / (hi - lo))
        return out

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11.0, 7.0), sharex=True)
    seen = set()
    for start, end, phase in spans:
        label = PHASE_LABELS[phase] if phase not in seen else None
        seen.add(phase)
        for ax in (ax1, ax2):
            ax.axvspan(start, end, color=PHASE_COLORS[phase], alpha=0.16, label=label if ax is ax1 else None)

    ax1.plot(cpu_x, cpu_y, lw=0.5, color="#999999", alpha=0.7, label="raw")
    ax1.plot(cpu_x, smooth(cpu_y), lw=1.2, color="#333333", label="5-point moving average")
    ax1.set_ylabel(pct_label("CPU utilisation, machine-wide"))
    ax1.set_title(f"Resource timeline for a batch of $N = {size}$ transactions")
    ax1.legend(fontsize=9, loc="upper right")
    ax1.grid(True, ls="--", lw=0.5, alpha=0.6)

    ax2.plot(rss_x, rss_y, lw=0.9, color="#B22222")
    ax2.set_ylabel("Resident set size (MiB)")
    ax2.set_xlabel("Time since start of run (s)")
    ax2.grid(True, ls="--", lw=0.5, alpha=0.6)

    save_fig(fig, figs_dir, f"11_chronogramme_n{size}", manifest_figs)


def fig_artifacts(
    circuits_info: Dict[str, Any], sizes: Sequence[int], figs_dir: str, manifest_figs: Dict[str, str]
) -> None:
    """Taille des artefacts produits (zkey, r1cs, preuve...)."""
    import matplotlib.pyplot as plt

    keys = ["ptau_total", "circuit.r1cs", "circuit_final.zkey", "witness.wtns", "proof.json"]
    present = [
        k for k in keys if any(circuits_info.get(str(n), {}).get("artifacts", {}).get(k) for n in sizes)
    ]
    if not present:
        return
    fig, ax = plt.subplots(figsize=(8.0, 5.5))
    markers = ["o", "^", "s", "D", "v"]
    for i, k in enumerate(present):
        pts = [
            (n, circuits_info[str(n)]["artifacts"][k] / MB)
            for n in sizes
            if circuits_info.get(str(n), {}).get("artifacts", {}).get(k)
        ]
        if not pts:
            continue
        ax.plot([p[0] for p in pts], [p[1] for p in pts], marker=markers[i % len(markers)], label=k)
    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    ax.set_xlabel("Batch size $N$ (transactions per proof)")
    ax.set_ylabel("File size (MiB)")
    ax.set_title("Size of generated artefacts")
    ax.legend(fontsize=9)
    ax.grid(True, which="both", ls="--", lw=0.5, alpha=0.6)
    save_fig(fig, figs_dir, "12_taille_artefacts", manifest_figs)


def build_figures(outdir: str, manifest: Dict[str, Any], args: argparse.Namespace) -> None:
    global NO_TITLES
    NO_TITLES = args.no_titles
    figs_dir = os.path.join(outdir, "figs")
    os.makedirs(figs_dir, exist_ok=True)
    setup_style(not args.no_science_style, args.big_font)

    agg = manifest["aggregates"]
    steps = manifest["steps"]
    sizes = manifest["params"]["sizes"]
    ncpu = manifest["env"].get("cpu_count", 1) or 1
    manifest_figs: Dict[str, str] = {}

    measured = {r["prover"] for r in agg["phases"] if r["phase"] == "prove" and r["prover"]}
    # ordre d'exécution (snarkjs, la référence, en premier) plutôt qu'alphabétique
    order = manifest["params"].get("provers") or sorted(measured)
    provers = [p for p in order if p in measured] + sorted(measured - set(order))
    main_prover = args.main_prover if args.main_prover in provers else (provers[0] if provers else "")

    print("\n>>> Génération des figures")

    # le graphe demandé : barres verticales empilées env/prove/verify,
    # une colonne par prover mesuré
    stacked_provers = provers if len(provers) > 1 else [main_prover]
    title_suffix = (
        f" ({main_prover})" if len(stacked_provers) == 1 and main_prover else ""
    )
    fig_stacked(
        agg,
        sizes,
        stacked_provers,
        "wall_s",
        1.0,
        "Wall-clock time (s)",
        f"Wall-clock cost per pipeline phase{title_suffix}",
        figs_dir,
        manifest_figs,
        "01_duree_par_phase_empilee",
    )
    fig_stacked(
        agg,
        sizes,
        stacked_provers,
        "cpu_total_s",
        1.0,
        "CPU time (s)",
        "CPU time per pipeline phase",
        figs_dir,
        manifest_figs,
        "02_cpu_par_phase_empilee",
    )
    fig_rss_grouped(agg, sizes, main_prover, figs_dir, manifest_figs)
    fig_io(agg, sizes, main_prover, figs_dir, manifest_figs)
    fig_stacked_log(agg, sizes, main_prover, figs_dir, manifest_figs)
    fig_share(agg, sizes, main_prover, figs_dir, manifest_figs)
    fig_env_breakdown(agg, sizes, figs_dir, manifest_figs)
    fig_scaling(agg, sizes, main_prover, figs_dir, manifest_figs)
    fig_cpu_efficiency(agg, sizes, main_prover, ncpu, figs_dir, manifest_figs)
    fig_prover_comparison(agg, sizes, figs_dir, manifest_figs)
    fig_artifacts(manifest.get("circuits", {}), sizes, figs_dir, manifest_figs)

    timeline_sizes = sizes if args.timeline_all else (sizes[-1:] if sizes else [])
    for n in timeline_sizes:
        fig_timeline(outdir, steps, n, ncpu, figs_dir, manifest_figs)

    manifest.setdefault("artifacts", {})["figures"] = manifest_figs


# --------------------------------------------------------------------------- #
# Sorties tabulaires et rapport
# --------------------------------------------------------------------------- #


def save_json(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, default=str)


def save_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames: List[str] = []
    for r in rows:
        for k in r:
            if k not in fieldnames:
                fieldnames.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def print_summary(agg: Dict[str, Any], sizes: Sequence[int], prover: str) -> None:
    table = phase_lookup(agg["phases"], prover)
    print("\n" + "=" * 104)
    print("RÉCAPITULATIF (moyennes par phase)")
    print("=" * 104)
    header = f"{'n':>6} | {'phase':>7} | {'durée (s)':>11} | {'CPU (s)':>10} | {'cœurs':>6} | {'RSS max (MiB)':>13} | {'R/W (MiB)':>17}"
    print(header)
    print("-" * len(header))
    for n in sizes:
        for phase in PHASES:
            row = table.get((n, phase))
            if not row:
                continue
            wall = row["wall_s_mean"]
            cores = row["cpu_total_s_mean"] / wall if wall > 0 else 0.0
            print(
                f"{n:>6} | {phase:>7} | {wall:>11.3f} | {row['cpu_total_s_mean']:>10.3f} | {cores:>6.2f} | "
                f"{row['rss_peak_bytes_mean'] / MB:>13.1f} | "
                f"{row['io_read_bytes_mean'] / MB:>7.1f} / {row['io_write_bytes_mean'] / MB:>7.1f}"
            )
    print("=" * 104)


def write_report(outdir: str, manifest: Dict[str, Any], prover: str) -> None:
    agg = manifest["aggregates"]
    sizes = manifest["params"]["sizes"]
    table = phase_lookup(agg["phases"], prover)
    lines: List[str] = []
    lines.append("# Benchmark ressources zk-SNARK\n")
    lines.append(f"- Date : {manifest['timestamp']}")
    lines.append(f"- Machine : {manifest['env']['platform']}, {manifest['env'].get('cpu_count')} cœurs, "
                 f"{manifest['env'].get('mem_total_gb', 0):.1f} GiB de RAM")
    lines.append(f"- Backend de mesure : {manifest['env'].get('sampler_backend')}")
    lines.append(f"- Prover principal : {prover or 'n/a'}")
    lines.append(f"- rapidsnark : {manifest['params'].get('rapidsnark_status')}")
    lines.append(f"- Tailles mesurées : {', '.join(str(s) for s in sizes)}")
    lines.append(f"- Répétitions preuve/vérification : {manifest['params']['repeat']}\n")
    lines.append("## Moyennes par phase\n")
    lines.append("| n | phase | durée (s) | CPU (s) | cœurs occupés | RSS max (MiB) | lecture (MiB) | écriture (MiB) |")
    lines.append("|---|-------|-----------|---------|---------------|---------------|---------------|----------------|")
    for n in sizes:
        for phase in PHASES:
            row = table.get((n, phase))
            if not row:
                continue
            wall = row["wall_s_mean"]
            cores = row["cpu_total_s_mean"] / wall if wall > 0 else 0.0
            lines.append(
                f"| {n} | {phase} | {wall:.3f} | {row['cpu_total_s_mean']:.3f} | {cores:.2f} | "
                f"{row['rss_peak_bytes_mean'] / MB:.1f} | {row['io_read_bytes_mean'] / MB:.1f} | "
                f"{row['io_write_bytes_mean'] / MB:.1f} |"
            )
    figs = manifest.get("artifacts", {}).get("figures", {})
    if figs:
        lines.append("\n## Figures\n")
        for name, rel in sorted(figs.items()):
            lines.append(f"- `{rel}`")
    failed = [s for s in manifest["steps"] if not s.get("ok", True)]
    if failed:
        lines.append("\n## Étapes en échec\n")
        for s in failed:
            lines.append(f"- n={s['size']} `{s['step']}` (code {s['returncode']}) — voir `{s['log_file']}`")
    with open(os.path.join(outdir, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# Environnement d'exécution
# --------------------------------------------------------------------------- #


def tool_version(cmd: str) -> str:
    try:
        proc = subprocess.run(shlex.split(f"{cmd} --version"), capture_output=True, text=True, timeout=120)
        out = (proc.stdout or proc.stderr).strip().splitlines()
        return out[0] if out else "?"
    except Exception:
        return "introuvable"


def collect_env(args: argparse.Namespace) -> Dict[str, Any]:
    mem = read_system_mem()
    env: Dict[str, Any] = {
        "user": getpass.getuser(),
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count() or 1,
        "mem_total_gb": (mem["total"] / GB) if mem else 0.0,
        "sampler_backend": "proc" if HAS_PROC else ("psutil" if HAS_PSUTIL else "none"),
        "versions": {
            "snarkjs": tool_version(args.snarkjs),
            "circom": tool_version(args.circom),
            "node": tool_version("node"),
        },
    }
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    env["cpu_model"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=30
        )
        if rev.returncode == 0:
            env["git_rev"] = rev.stdout.strip()
    except Exception:
        pass
    return env


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def parse_sizes(args: argparse.Namespace) -> List[int]:
    if args.sizes:
        sizes = sorted({int(s) for s in re.split(r"[,\s]+", args.sizes.strip()) if s})
    else:
        sizes = [2 ** i for i in range(args.max_exponent + 1)]
    for n in sizes:
        if n <= 0:
            raise SystemExit(f"taille invalide : {n}")
    return sizes


def run_benchmark(args: argparse.Namespace, outdir: str) -> Dict[str, Any]:
    repo_root = os.path.abspath(args.repo_root)
    sizes = parse_sizes(args)
    ncpu = os.cpu_count() or 1

    # --- où vivent les circuits ---
    if args.work_dir:
        work_dir = os.path.abspath(args.work_dir)
    elif args.skip_setup:
        work_dir = os.path.abspath(os.path.join(repo_root, args.circuits_dir))
    else:
        work_dir = os.path.join(outdir, "circuits")
    os.makedirs(work_dir, exist_ok=True)

    template_dir = os.path.abspath(args.template_dir)

    # --- provers ---
    rs_cfg = RapidsnarkConfig(reason="désactivé")
    if args.prover in ("rapidsnark", "both", "auto") and args.rapidsnark_mode != "off":
        rs_cfg = detect_rapidsnark(args, repo_root, sizes, work_dir)
        if not rs_cfg.available and args.prover == "rapidsnark":
            raise SystemExit(f"[erreur] rapidsnark demandé mais {rs_cfg.describe()}")

    provers: List[str] = []
    if args.prover in ("snarkjs", "both", "auto"):
        provers.append("snarkjs")
    if rs_cfg.available and args.prover in ("rapidsnark", "both", "auto"):
        provers.append("rapidsnark")
    if not provers:
        raise SystemExit("[erreur] aucun prover disponible")

    print("=" * 78)
    print(f"Sortie              : {outdir}")
    print(f"Tailles             : {sizes}")
    print(f"Dossier de travail  : {work_dir}")
    print(f"Phase environnement : {'ignorée (--skip-setup)' if args.skip_setup else 'mesurée'}")
    print(f"Provers             : {', '.join(provers)}")
    print(f"rapidsnark          : {rs_cfg.describe()}")
    print(f"Backend de mesure   : {'/proc' if HAS_PROC else ('psutil' if HAS_PSUTIL else 'durée seule')}")
    print(f"Répétitions         : {args.repeat}")
    print("=" * 78)

    if not HAS_PROC and not HAS_PSUTIL:
        print(
            "[attention] ni /proc ni psutil : seules les durées seront fiables.\n"
            "            Lancez le script sous Debian (ou `pip install psutil`)."
        )

    runner = Runner(outdir, args.sample_interval, ncpu, dry_run=args.dry_run, quiet=args.quiet)
    circuits_info: Dict[str, Any] = {}

    for size in sizes:
        power = ptau_power(size, args.ptau_offset)
        circuit_dir = os.path.join(work_dir, str(size))
        print(f"\n### Circuit n={size} (2^{power} powers of tau) ###")

        info: Dict[str, Any] = {"n": size, "ptau_power": power, "dir": os.path.relpath(circuit_dir, outdir)}

        # ---------------- phase env ---------------- #
        if not args.skip_setup:
            if os.path.isdir(circuit_dir) and args.clean_setup:
                shutil.rmtree(circuit_dir)
            prepare_circuit_dir(size, work_dir, template_dir)
            for c in env_commands(size, power, args.snarkjs, args.circom, args.entropy):
                res = runner.run(
                    c.cmd,
                    circuit_dir,
                    size=size,
                    phase="env",
                    step=c.step,
                    group=c.group,
                )
                if not res.ok and not args.keep_going:
                    print(f"  ⛔ échec de `{c.step}` — passage à la taille suivante")
                    break
        else:
            if not os.path.isfile(os.path.join(circuit_dir, "circuit_final.zkey")):
                print(f"  [ignoré] {circuit_dir} : circuit_final.zkey absent")
                continue

        # ---------------- phase prove ---------------- #
        # snarkjs : une seule commande (fullprove = témoin + preuve)
        # rapidsnark : le témoin doit être calculé à part, comme dans generate_proofs.py
        fullprove_cmd = (
            f"{args.snarkjs} groth16 fullprove input.json circuit_js/circuit.wasm "
            f"circuit_final.zkey proof.json public.json"
        )
        witness_cmd = f"{args.snarkjs} wtns calculate circuit_js/circuit.wasm input.json witness.wtns"

        for prover in provers:
            for rep in range(1, args.repeat + 1):
                print(f"  · preuve ({prover} rep {rep}/{args.repeat})")

                if prover == "snarkjs":
                    steps_to_run = [("fullprove_snarkjs", fullprove_cmd, "process")]
                else:
                    cmd, scope = rapidsnark_command(rs_cfg, size, circuit_dir)
                    steps_to_run = [
                        (f"witness_{prover}", witness_cmd, "process"),
                        (f"prove_{prover}", cmd, scope),
                    ]

                failed = False
                for step, cmd, scope in steps_to_run:
                    res = runner.run(
                        cmd,
                        circuit_dir,
                        size=size,
                        phase="prove",
                        step=step,
                        group="prove",
                        rep=rep,
                        prover=prover,
                        scope=scope,
                    )
                    if not res.ok and not args.keep_going:
                        failed = True
                        break
                if failed:
                    break
                if args.sleep_between > 0:
                    time.sleep(args.sleep_between)

            # ---------------- phase verify ---------------- #
            for rep in range(1, args.repeat + 1):
                print(f"  · vérification ({prover} rep {rep}/{args.repeat})")
                runner.run(
                    f"{args.snarkjs} groth16 verify verification_key.json public.json proof.json",
                    circuit_dir,
                    size=size,
                    phase="verify",
                    step=f"verify_{prover}",
                    group="verify",
                    rep=rep,
                    prover=prover,
                )
                if args.sleep_between > 0:
                    time.sleep(args.sleep_between)

        info["artifacts"] = artifact_sizes(circuit_dir)
        if args.count_constraints:
            info["constraints"] = r1cs_constraints(args.snarkjs, circuit_dir)
        if args.prune and not args.skip_setup:
            info["pruned_bytes"] = prune_heavy_artifacts(circuit_dir)
        circuits_info[str(size)] = info

    agg = aggregate(runner.steps)
    measured_sizes = [n for n in sizes if str(n) in circuits_info]
    manifest: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "outdir": os.path.abspath(outdir),
        "params": {
            "sizes": measured_sizes or sizes,
            "sizes_requested": sizes,
            "repeat": args.repeat,
            "sleep_between_s": args.sleep_between,
            "sample_interval_s": args.sample_interval,
            "skip_setup": args.skip_setup,
            "work_dir": work_dir,
            "ptau_offset": args.ptau_offset,
            "provers": provers,
            "rapidsnark_status": rs_cfg.describe(),
            "rapidsnark_mode": rs_cfg.mode,
            "cli": " ".join(sys.argv),
        },
        "env": collect_env(args),
        "circuits": circuits_info,
        "steps": [s.to_row() for s in runner.steps],
        "aggregates": agg,
    }
    return manifest


def write_outputs(outdir: str, manifest: Dict[str, Any], args: argparse.Namespace) -> None:
    save_csv(os.path.join(outdir, "steps.csv"), manifest["steps"])
    save_csv(os.path.join(outdir, "phases.csv"), manifest["aggregates"]["phases"])
    save_csv(os.path.join(outdir, "env_breakdown.csv"), manifest["aggregates"]["env_groups"])
    save_json(os.path.join(outdir, "manifest.json"), manifest)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Mesure CPU / RAM / I/O / durée des phases environnement, preuve et vérification d'un pipeline zk-SNARK.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    g = p.add_argument_group("tailles de circuit")
    g.add_argument("--sizes", type=str, default="", help="liste explicite, ex. 1,2,4,8,16")
    g.add_argument("--max-exponent", type=int, default=3, help="si --sizes absent : tailles 2^0..2^X")
    g.add_argument("--ptau-offset", type=int, default=8, help="puissance de tau = log2(n) + offset")

    g = p.add_argument_group("phases")
    g.add_argument("--skip-setup", action="store_true", help="ne pas mesurer la phase environnement (circuits existants)")
    g.add_argument("--clean-setup", action="store_true", help="supprimer le dossier du circuit avant le setup")
    g.add_argument("--repeat", type=int, default=3, help="répétitions des phases preuve et vérification")
    g.add_argument("--sleep-between", type=float, default=1.0, help="pause entre répétitions (s)")
    g.add_argument("--keep-going", action="store_true", help="continuer malgré l'échec d'une commande")
    g.add_argument("--prune", action="store_true", help="supprimer ptau/zkey intermédiaires après mesure")
    g.add_argument("--count-constraints", action="store_true", help="relever le nombre de contraintes du r1cs")

    g = p.add_argument_group("chemins")
    g.add_argument("--out-root", type=str, default="bench-out",
                   help="racine des runs ; le run va dans <racine>/YYYYMMDD_HHmmss")
    g.add_argument("--out-dir", type=str, default="", help="dossier de sortie explicite (ignore --out-root)")
    g.add_argument("--repo-root", type=str, default=".", help="racine du dépôt")
    g.add_argument("--template-dir", type=str, default="scripts", help="dossier contenant circuit.circom et circomlib")
    g.add_argument("--circuits-dir", type=str, default="circuits", help="circuits existants (avec --skip-setup)")
    g.add_argument("--work-dir", type=str, default="", help="dossier de travail des circuits (défaut : <sortie>/circuits)")

    g = p.add_argument_group("outils")
    g.add_argument("--snarkjs", type=str, default="snarkjs", help="commande snarkjs (ex. 'npx snarkjs')")
    g.add_argument("--circom", type=str, default="circom", help="commande circom")
    g.add_argument("--entropy", type=str, default="some random text", help="entropie des contributions")

    g = p.add_argument_group("prover")
    g.add_argument("--prover", choices=["auto", "snarkjs", "rapidsnark", "both"], default="auto",
                   help="auto = snarkjs + rapidsnark si détecté")
    g.add_argument("--main-prover", type=str, default="snarkjs", help="prover utilisé dans les graphes empilés")
    g.add_argument("--rapidsnark-mode", choices=["auto", "native", "docker", "off"], default="auto")
    g.add_argument("--rapidsnark-bin", type=str, default="", help="chemin du binaire prover natif")
    g.add_argument("--docker-container", type=str, default="debian_rapidsnark", help="conteneur déjà lancé")
    g.add_argument("--docker-mount-prefix", type=str, default="mnt/projet",
                   help="chemin des circuits vu depuis le conteneur")
    g.add_argument("--docker-image", type=str, default="", help="image à utiliser via docker run (fallback)")
    g.add_argument("--docker-prover-path", type=str, default="mnt/projet/rapidsnark/package/bin/prover",
                   help="chemin du binaire prover dans le conteneur")

    g = p.add_argument_group("mesure et sorties")
    g.add_argument("--sample-interval", type=float, default=0.1, help="période d'échantillonnage (s)")
    g.add_argument("--no-plots", action="store_true", help="ne pas générer les figures")
    g.add_argument("--plot-only", type=str, default="",
                   help="régénérer les figures d'un run existant, ex. bench-out/20260730_101500")
    g.add_argument("--timeline-all", action="store_true", help="un chronogramme par taille (sinon la plus grande)")
    g.add_argument("--no-science-style", action="store_true", help="ne pas utiliser scienceplots")
    g.add_argument("--no-titles", action="store_true",
                   help="figures sans titre (le titre devient la légende de l'article)")
    g.add_argument("--big-font", action="store_true", help="polices agrandies (slides, impression)")
    g.add_argument("--quiet", action="store_true", help="affichage minimal")
    g.add_argument("--dry-run", action="store_true", help="afficher les commandes sans les exécuter")

    args = p.parse_args()

    # --- régénération de figures uniquement --- #
    if args.plot_only:
        outdir = os.path.abspath(args.plot_only)
        manifest_path = os.path.join(outdir, "manifest.json")
        if not os.path.isfile(manifest_path):
            raise SystemExit(f"[erreur] {manifest_path} introuvable")
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        build_figures(outdir, manifest, args)
        save_json(manifest_path, manifest)
        provers = sorted({r["prover"] for r in manifest["aggregates"]["phases"] if r["phase"] == "prove" and r["prover"]})
        main_prover = args.main_prover if args.main_prover in provers else (provers[0] if provers else "")
        write_report(outdir, manifest, main_prover)
        print(f"\n>>> Figures régénérées dans {os.path.join(outdir, 'figs')}")
        return

    if args.repeat < 1:
        raise SystemExit("--repeat doit être >= 1")

    if args.out_dir:
        outdir = os.path.abspath(args.out_dir)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        outdir = os.path.abspath(os.path.join(args.out_root, ts))
    os.makedirs(outdir, exist_ok=True)

    t_start = time.perf_counter()
    manifest = run_benchmark(args, outdir)
    manifest["total_wall_s"] = time.perf_counter() - t_start

    provers = manifest["params"]["provers"]
    main_prover = args.main_prover if args.main_prover in provers else (provers[0] if provers else "")

    write_outputs(outdir, manifest, args)
    print_summary(manifest["aggregates"], manifest["params"]["sizes"], main_prover)

    if not args.no_plots and not args.dry_run:
        try:
            build_figures(outdir, manifest, args)
        except Exception as exc:
            print(f"[attention] génération des figures interrompue : {exc}")

    write_report(outdir, manifest, main_prover)
    save_json(os.path.join(outdir, "manifest.json"), manifest)

    failed = [s for s in manifest["steps"] if not s.get("ok", True)]
    print(f"\n>>> Terminé en {manifest['total_wall_s']:.1f} s — sorties dans {outdir}")
    print("    - mesures brutes  : raw/steps.jsonl, raw/samples/*.csv")
    print("    - agrégats        : steps.csv, phases.csv, env_breakdown.csv")
    print("    - figures         : figs/*.png, figs/*.svg")
    print("    - synthèse        : report.md, manifest.json")
    if failed:
        print(f"    [!] {len(failed)} étape(s) en échec — voir report.md et logs/")


if __name__ == "__main__":
    main()
