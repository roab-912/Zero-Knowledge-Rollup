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
les agrégats, les figures et des tables LaTeX prêtes pour l'article sont
écrits dans ./bench-out/YYYYMMDD_HHmmss/.

Figures produites (une figure = une affirmation, réutilisable comme légende) :
  01_phase_cost_stacked         coût du cycle de vie complet (setup + preuve + vérif)
  01b_recurring_cost_stacked    coût récurrent seul, une colonne par prover
  02_machine_sizing_per_phase   RAM et cœurs par phase (boîtes à moustaches)
  03_setup_amortization         amortissement du trusted setup one-off
  04_usd_cost_per_tx            coût marginal de preuve en $/tx (--usd-per-hour)

Tables LaTeX produites (tables/, prêtes à insérer — adapter caption/label) :
  tab_pipeline_resources  ressources par (N, phase) — vue détaillée
  tab_prover_comparison   provers face à face à la taille de référence
  tab_lifecycle           cycle de vie à N fixé : phase, fréquence, x vs verify
  tab_phase_ratios        T_setup / T_proof / T_ver et leurs ratios selon N
  tab_amortization        seuils k où le setup passe sous 50 % puis 10 % du coût
  tab_provisioning        vCPU / RAM / instance / $ par rôle (dimensionnement)
  tab_proving_io          taille des entrées/sorties de la preuve selon N
  tab_artifacts           empreinte de stockage des artefacts (preuve constante)
  tab_cost_usd            coût de preuve en USD (--usd-per-hour)

Les vues latence / débit / coût amorti / efficacité de scaling de l'article
sont déjà produites par generate_graph_from_bench_out.py et ne sont pas
dupliquées ici.

Exemples
--------
    # jeu complet: setup + preuve + vérif pour 1..64 transactions
    python3 scripts/measure_zk_resources.py --sizes 1,2,4,8,16,32,64

    # réutilise les circuits déjà générés dans ./circuits, 5 répétitions
    python3 scripts/measure_zk_resources.py --sizes 1,2,4 --skip-setup \
        --circuits-dir circuits --repeat 5

    # comparaison snarkjs / rapidsnark (docker) sur le serveur de test.
    # Le conteneur est démarré s'il est à l'arrêt ; --require-rapidsnark fait
    # échouer le run tout de suite plutôt que de ne mesurer que snarkjs.
    python3 scripts/measure_zk_resources.py --sizes 1,2,4 --skip-setup \
        --prover both --rapidsnark-mode docker --docker-container debian_rapidsnark \
        --require-rapidsnark

    # regénérer figures + tables depuis un run existant, avec les coûts en USD
    python3 scripts/measure_zk_resources.py --plot-only bench-out/20260730_101500 \
        --usd-per-hour 1.84
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
import textwrap
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
    "env": "Setup",
    "prove": "Proof generation",
    "verify": "Verification",
}
PHASE_COLORS = {"env": "#4C72B0", "prove": "#DD8452", "verify": "#55A868"}

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
    "input.json",
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
# emplacements usuels d'une compilation locale sous Debian/WSL, hors dépôt
NATIVE_ABS_CANDIDATES = (
    "~/rapidsnark/package/bin/prover",
    "~/rapidsnark/build/prover",
    "/opt/rapidsnark/package/bin/prover",
    "/usr/local/bin/prover",
    "/mnt/projet/rapidsnark/package/bin/prover",
)


def docker_state() -> Tuple[bool, str]:
    """(daemon joignable, raison de l'indisponibilité)."""
    if shutil.which("docker") is None:
        return False, "la commande docker est absente du PATH"
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=30)
    except Exception as exc:
        return False, f"`docker info` a échoué ({exc})"
    if proc.returncode == 0:
        return True, ""
    # sous WSL, c'est presque toujours le daemon qui n'est pas démarré
    return False, (
        "le daemon docker ne répond pas (démarrer Docker Desktop, ou "
        "`sudo service docker start` dans la distribution WSL)"
    )


def container_status(name: str) -> str:
    """'running', 'exited', … ou '' si le conteneur n'existe pas."""
    if not name:
        return ""
    try:
        proc = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", name],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def container_running(name: str) -> bool:
    return container_status(name) == "running"


def start_container(name: str) -> bool:
    """Redémarre un conteneur existant mais arrêté (cas courant après reboot)."""
    try:
        proc = subprocess.run(
            ["docker", "start", name], capture_output=True, text=True, timeout=120
        )
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    # `docker start` rend la main avant que le conteneur ne soit vraiment prêt
    for _ in range(10):
        if container_running(name):
            return True
        time.sleep(0.5)
    return False


def find_rapidsnark_image() -> str:
    """Première image locale dont le nom évoque rapidsnark (repli sans conteneur)."""
    try:
        proc = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    for line in proc.stdout.splitlines():
        name = line.strip()
        if "rapidsnark" in name.lower() and "<none>" not in name:
            return name
    return ""


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


def prover_runs(cmd: Sequence[str]) -> bool:
    """Le prover démarre-t-il réellement ?

    Sans argument, rapidsnark affiche son usage et sort en erreur : ce qui
    compte est de ne pas retomber sur 126/127 (binaire non exécutable ou
    introuvable), qui trahit une compilation pour une autre architecture ou une
    glibc incompatible — un cas qui, sinon, n'apparaît qu'à la première mesure.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    except Exception:
        return False
    return proc.returncode not in (126, 127)


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
            candidates = [os.path.join(repo_root, rel) for rel in NATIVE_PATH_CANDIDATES]
            candidates += [os.path.expanduser(p) for p in NATIVE_ABS_CANDIDATES]
            for cand in candidates:
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    binary = os.path.abspath(cand)
                    break
        if binary and (os.path.isfile(binary) or shutil.which(binary)):
            if prover_runs([binary]):
                return RapidsnarkConfig(available=True, mode="native", binary=binary)
            if mode == "native":
                return RapidsnarkConfig(
                    reason=f"{binary} présent mais non exécutable ici (architecture ou glibc ?)"
                )
            print(f"[attention] {binary} présent mais non exécutable : repli sur docker")
        if mode == "native":
            return RapidsnarkConfig(
                reason=(
                    "aucun binaire rapidsnark trouvé — passer --rapidsnark-bin, ou "
                    "le compiler dans " + os.path.join(repo_root, NATIVE_PATH_CANDIDATES[0])
                )
            )

    if mode in ("auto", "docker"):
        ok, why = docker_state()
        if not ok:
            return RapidsnarkConfig(reason=f"pas de binaire natif et {why}")
        container = args.docker_container
        status = container_status(container)
        # cas le plus fréquent après un reboot : le conteneur existe mais est arrêté
        if container and status and status != "running" and args.docker_autostart:
            print(f"[info] conteneur {container} à l'arrêt ({status}) : démarrage")
            if start_container(container):
                status = "running"
            else:
                print(f"[attention] impossible de démarrer {container}")
        if container and status == "running":
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
            elif not prover_runs(["docker", "exec", container, prover]):
                if not args.docker_image:
                    return RapidsnarkConfig(
                        reason=(
                            f"conteneur {container} actif mais {prover} ne s'exécute pas "
                            f"(binaire incompatible ?)"
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
        # repli : une image locale suffit, on lancera un conteneur jetable
        image = args.docker_image or find_rapidsnark_image()
        if image_available(image):
            if not args.docker_image:
                print(f"[info] pas de conteneur exploitable : repli sur l'image {image}")
            return RapidsnarkConfig(
                available=True,
                mode="docker-run",
                image=image,
                binary=args.docker_prover_path,
            )
        if args.docker_image:
            return RapidsnarkConfig(reason=f"image docker {args.docker_image} absente localement")
        detail = f"conteneur {container} : {status or 'inexistant'}" if container else "aucun conteneur demandé"
        return RapidsnarkConfig(
            reason=(
                f"aucun prover rapidsnark utilisable ({detail}, aucune image locale "
                f"rapidsnark) — vérifier --docker-container / --docker-image"
            )
        )

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

# Le jeu de figures est volontairement resserré : une figure = une affirmation
# qui sert directement la section d'évaluation de l'article. Ces phrases sont
# reprises dans report.md et dans le manifest, pour servir de base aux légendes.
# Les vues latence / débit / coût amorti par tx / efficacité de scaling sont
# déjà produites par generate_graph_from_bench_out.py et ne sont pas dupliquées.
FIGURE_CLAIMS = {
    "01_phase_cost_stacked": (
        "End-to-end cost of one batch, split into its three phases: the one-off "
        "setup dominates the total, while the recurring phases stay small."
    ),
    "01b_recurring_cost_stacked": (
        "Recurring per-batch cost (proving + verification) with one column per "
        "prover: verification is identical, so the whole gap comes from proving."
    ),
    "02_machine_sizing_per_phase": (
        "Peak RAM and busy cores per phase, as box plots over the repetitions "
        "(min/max whiskers, median line, mean marker): the one-off setup sets "
        "the machine requirement; once it has been run, the recurring proving "
        "and verification phases fit on a smaller machine."
    ),
    "03_setup_amortization": (
        "Per-batch time T_setup/k + T_proof as the number of proved batches k "
        "grows: the one-off setup rapidly becomes a minority cost, which "
        "justifies excluding it from the per-batch critical path."
    ),
    "04_usd_cost_per_tx": (
        "Marginal proving cost per transaction in USD at a fixed hourly machine "
        "rate: the economic counterpart of the amortized proving time."
    ),
}


def save_fig(fig, figs_dir: str, name: str, manifest_figs: Dict[str, str], note: str = "") -> None:
    if NO_TITLES:
        for ax in fig.axes:
            ax.set_title("")
    if note:
        # réserve de mesure : la note reste sous l'axe grâce à bbox_inches="tight".
        # Elle est repliée : une ligne unique élargirait la boîte englobante et
        # donc la figure exportée.
        width_chars = max(80, int(fig.get_figwidth() * 13))
        wrapped = "\n".join(textwrap.wrap(note, width=width_chars))
        fig.text(0.5, -0.02, wrapped, ha="center", va="top", fontsize=8, style="italic")
    for ext in ("png", "svg"):
        path = os.path.join(figs_dir, f"{name}.{ext}")
        fig.savefig(path, dpi=300 if ext == "png" else None, bbox_inches="tight")
    manifest_figs[name] = f"figs/{name}.png"
    import matplotlib.pyplot as plt

    plt.close(fig)
    print(f"    figure : figs/{name}.png")


def _xpos(sizes: Sequence[int]):
    import numpy as np

    return np.arange(len(sizes))


PROVER_SHORT = {"snarkjs": "sjs", "rapidsnark": "rs"}
PROVER_HATCH = ("", "///", "...")
PROVER_MARKERS = {"rapidsnark": "o", "snarkjs": "^"}


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
    phases: Sequence[str] = PHASES,
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
    # largeur bornée : au-delà, la figure s'aplatit et devient illisible en colonne d'article
    fig, ax = plt.subplots(
        figsize=(min(13.0, max(9.0, 0.75 * n_prov * len(sizes) + 3.5)), 6.0)
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
        for phase in phases:
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
    # text.usetex = False : le croisillon est du texte brut, pas une macro LaTeX
    ax.set_xlabel("Batch size $N$ (# transactions per proof)")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    # marge en haut pour que la légende ne recouvre aucune barre
    peak = max((t for _, t in totals), default=0.0)
    if peak > 0:
        ax.set_ylim(0, peak * (1.34 if n_prov > 1 else 1.22))
    ax.legend(loc="upper left", ncol=2 if n_prov > 1 else 1, framealpha=0.9)
    ax.grid(True, axis="y", ls="--", lw=0.5, alpha=0.6)
    save_fig(fig, figs_dir, name, manifest_figs)


# --------------------------------------------------------------------------- #
# Dimensionnement machine et amortissement (analyses transverses)
# --------------------------------------------------------------------------- #


def _human(b: float) -> str:
    return f"{b / GB:.2f} GiB" if b >= GB else f"{b / MB:.0f} MiB"


def sizing_summary(agg: Dict[str, Any], sizes: Sequence[int], provers: Sequence[str]) -> Optional[Dict[str, Any]]:
    """Chiffres du claim « downsizing » à la plus grande taille mesurée :
    RSS max et cœurs occupés du setup vs des phases récurrentes."""
    provers = [p for p in provers if p] or [""]
    main = provers[0]
    tmain = phase_lookup(agg["phases"], main)
    for n in reversed(list(sizes)):
        env = tmain.get((n, "env"), {})
        e_rss = env.get("rss_peak_bytes_mean", 0.0)
        p_rss = 0.0
        p_cores = 0.0
        for p in provers:
            row = phase_lookup(agg["phases"], p).get((n, "prove"))
            if not row:
                continue
            p_rss = max(p_rss, row.get("rss_peak_bytes_mean", 0.0))
            if row.get("wall_s_mean", 0.0) > 0:
                p_cores = max(p_cores, row["cpu_total_s_mean"] / row["wall_s_mean"])
        if e_rss <= 0 or p_rss <= 0:
            continue
        v_row = tmain.get((n, "verify"))
        e_wall = env.get("wall_s_mean", 0.0)
        return {
            "n": n,
            "env_rss": e_rss,
            "prove_rss": p_rss,
            "verify_rss": v_row.get("rss_peak_bytes_mean", 0.0) if v_row else 0.0,
            "env_cores": (env.get("cpu_total_s_mean", 0.0) / e_wall) if e_wall > 0 else 0.0,
            "prove_cores": p_cores,
            "ram_ratio": e_rss / p_rss,
        }
    return None


def _sizing_note(agg: Dict[str, Any], sizes: Sequence[int], provers: Sequence[str]) -> str:
    s = sizing_summary(agg, sizes, provers)
    if not s:
        return ""
    note = (
        f"Peak RAM at N={s['n']}: setup {_human(s['env_rss'])}, "
        f"proving {_human(s['prove_rss'])}, verification {_human(s['verify_rss'])}."
    )
    if s["ram_ratio"] >= 1.2:
        note += f" A proving-only node needs ~{s['ram_ratio']:.1f}x less RAM than the setup machine."
    return note


def rep_distributions(
    steps: Sequence[Dict[str, Any]], size: int, phase: str, prover: str
) -> Tuple[List[float], List[float]]:
    """(RSS max, cœurs occupés) par répétition, pour une (taille, phase, prover).

    Même reconstruction que `aggregate` : une répétition est l'ensemble des
    steps qui la composent — RSS pris au maximum, temps CPU et temps écoulé
    sommés — ce qui donne un échantillon par répétition, base des boîtes.
    """
    rss: Dict[int, float] = {}
    cpu: Dict[int, float] = {}
    wall: Dict[int, float] = {}
    for s in steps:
        if s["size"] != size or s["phase"] != phase:
            continue
        if prover and s.get("prover") and s["prover"] != prover:
            continue
        rep = s.get("rep", 1)
        rss[rep] = max(rss.get(rep, 0.0), float(s.get("rss_peak_bytes", 0.0)))
        cpu[rep] = cpu.get(rep, 0.0) + float(s.get("cpu_total_s", 0.0))
        wall[rep] = wall.get(rep, 0.0) + float(s.get("wall_s", 0.0))
    rss_vals = [v for v in rss.values() if v > 0]
    core_vals = [cpu[r] / wall[r] for r in wall if wall[r] > 0 and cpu.get(r, 0.0) > 0]
    return rss_vals, core_vals


def _box_series(ax, series, positions, width, color, hatch: str = ""):
    """Boîte à moustaches min/médiane/max + moyenne, aux couleurs de la phase."""
    if not series:
        return
    bp = ax.boxplot(
        series,
        positions=positions,
        widths=width,
        whis=(0, 100),  # moustaches = min et max observés
        showmeans=True,
        # marqueur de moyenne à la couleur de la série : quand la dispersion est
        # nulle (setup mesuré une fois) la boîte s'aplatit, et c'est lui qui
        # porte l'identification de la phase
        meanprops={"marker": "o", "markerfacecolor": color,
                   "markeredgecolor": "black", "markersize": 5},
        medianprops={"color": "black", "lw": 1.2},
        boxprops={"facecolor": color, "alpha": 0.75, "lw": 0.6, "hatch": hatch},
        whiskerprops={"lw": 0.8},
        capprops={"lw": 0.8},
        patch_artist=True,
        manage_ticks=False,
    )
    return bp


def fig_machine_sizing(
    agg: Dict[str, Any],
    steps: Sequence[Dict[str, Any]],
    sizes: Sequence[int],
    provers: Sequence[str],
    ncpu: int,
    figs_dir: str,
    manifest_figs: Dict[str, str],
    cpu_note: str = "",
) -> None:
    """RAM de pointe et cœurs occupés par phase : dimensionnement du serveur.

    Porte l'affirmation « downsizing » : le setup, one-off, fixe la taille de
    la machine ; les phases récurrentes (preuve, vérification) tiennent ensuite
    sur une configuration plus modeste. Chaque série est une boîte à
    moustaches sur les répétitions : min et max aux moustaches, médiane au
    trait, moyenne au marqueur blanc.
    """
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import numpy as np
    from matplotlib.patches import Patch

    provers = [p for p in provers if p] or [""]

    # une série = une phase ; la preuve est déclinée par prover, distinguée par
    # une hachure (même convention que la figure des barres empilées)
    series: List[Tuple[str, str, str, str, str]] = [
        ("env", "", PHASE_LABELS["env"] + " (one-off)", PHASE_COLORS["env"], "")
    ]
    for j, p in enumerate(provers):
        label = PHASE_LABELS["prove"] + (f" — {p}" if len(provers) > 1 else "")
        series.append(("prove", p, label, PHASE_COLORS["prove"], PROVER_HATCH[j % len(PROVER_HATCH)]))
    series.append(("verify", provers[0], PHASE_LABELS["verify"], PHASE_COLORS["verify"], ""))

    # collecte : {index de série -> {index de taille -> échantillons}}
    rss_data: Dict[int, Dict[int, List[float]]] = {}
    core_data: Dict[int, Dict[int, List[float]]] = {}
    for j, (phase, prover, _lbl, _c, _h) in enumerate(series):
        for i, n in enumerate(sizes):
            rss_vals, core_vals = rep_distributions(steps, n, phase, prover)
            if rss_vals:
                rss_data.setdefault(j, {})[i] = rss_vals
            if core_vals:
                core_data.setdefault(j, {})[i] = core_vals
    if not rss_data:
        return

    all_rss = [v for per_size in rss_data.values() for vals in per_size.values() for v in vals]
    peak_rss, low_rss = max(all_rss), min(all_rss)
    # bascule en GiB seulement quand les valeurs y restent lisibles (au-delà de
    # 4 GiB de pic) : sinon les petites phases tomberaient à « 0,06 GiB »
    unit_div, unit_name = (GB, "GiB") if peak_rss >= 4 * GB else (MB, "MiB")

    n_series = len(series)
    width = 0.8 / n_series
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(min(13.0, max(9.5, 0.9 * len(sizes) + 3.5)), 8.4), sharex=True
    )

    for j, (_phase, _prover, _lbl, color, hatch) in enumerate(series):
        offset = (j - (n_series - 1) / 2) * width
        for ax, data, div in ((ax1, rss_data, unit_div), (ax2, core_data, 1.0)):
            per_size = data.get(j, {})
            if not per_size:
                continue
            idx = sorted(per_size)
            _box_series(
                ax,
                [[v / div for v in per_size[i]] for i in idx],
                [i + offset for i in idx],
                width * 0.85,
                color,
                hatch,
            )

    # --- panneau 1 : RSS max (la ressource qui dimensionne la RAM) -------- #
    # échelle log seulement si l'amplitude le justifie ; sinon l'axe linéaire
    # se lit directement en MiB/GiB
    log_ram = peak_rss / max(low_rss, 1.0) >= 20
    if log_ram:
        ax1.set_yscale("log")
        # valeurs en clair (512, 1024…) plutôt que 10^2, 6x10^2
        fmt = mticker.FuncFormatter(lambda v, _pos: f"{v:g}" if v > 0 else "")
        ax1.yaxis.set_major_formatter(fmt)
        ax1.yaxis.set_minor_formatter(fmt)
        ax1.tick_params(axis="y", which="minor", labelsize=7)
    else:
        ax1.set_ylim(0, peak_rss / unit_div * 1.30)
        ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _pos: f"{v:g}"))
    ax1.set_ylabel(f"Peak resident set size ({unit_name})")
    handles = [
        Patch(facecolor=c, alpha=0.75, hatch=h, edgecolor="black", lw=0.6, label=lbl)
        for _p, _pr, lbl, c, h in series
    ]
    handles.append(
        plt.Line2D([], [], marker="o", color="black", markerfacecolor="lightgrey",
                   ls="none", ms=5, label="mean (median = line, whiskers = min/max)")
    )
    ax1.legend(handles=handles, fontsize=8, ncol=2)
    ax1.grid(True, which="both" if log_ram else "major", axis="y", ls="--", lw=0.5, alpha=0.6)

    # --- panneau 2 : cœurs occupés (temps CPU / temps écoulé) ------------- #
    ax2.axhline(1.0, color="black", ls=":", lw=1.0, label="one saturated core")
    tops = [v for per_size in core_data.values() for vals in per_size.values() for v in vals]
    top = max(tops + [1.0])
    if 1 < ncpu <= top * 3:
        ax2.axhline(float(ncpu), color="grey", ls="--", lw=1.0, label=f"{ncpu} cores (machine)")
        top = max(top, float(ncpu))
    ax2.set_ylim(0, top * 1.25)
    ax2.set_xticks(np.arange(len(sizes)))
    ax2.set_xticklabels([str(n) for n in sizes])
    ax2.set_xlim(-0.6, len(sizes) - 0.4)
    ax2.set_xlabel("Batch size $N$ (# transactions per proof)")
    ax2.set_ylabel("Cores busy")
    ax2.legend(fontsize=8)
    ax2.grid(True, axis="y", ls="--", lw=0.5, alpha=0.6)

    note = _sizing_note(agg, sizes, provers)
    note = (
        note + " Boxes span the repetitions of each phase (whiskers: min/max, "
        "line: median, marker: mean); the setup is measured once per size."
    ).strip()
    if cpu_note:
        note = (note + " " + cpu_note).strip()
    save_fig(fig, figs_dir, "02_machine_sizing_per_phase", manifest_figs, note=note)


def fig_setup_amortization(
    agg: Dict[str, Any],
    sizes: Sequence[int],
    provers: Sequence[str],
    figs_dir: str,
    manifest_figs: Dict[str, str],
) -> None:
    """Temps par lot T_setup/k + T_proof en fonction du nombre de lots k.

    Chiffre l'exclusion du trusted setup du chemin critique : c'est un coût
    fixe, amorti sur tous les lots prouvés avec le même circuit — l'analogue,
    côté infrastructure, de la loi d'amortissement C0/N du modèle.
    """
    import matplotlib.pyplot as plt
    import numpy as np

    provers = [p for p in provers if p]
    if not provers:
        return
    tmain = phase_lookup(agg["phases"], provers[0])
    n_ref, setup = None, 0.0
    for n in reversed(list(sizes)):
        s = tmain.get((n, "env"), {}).get("wall_s_mean", 0.0)
        if s > 0 and any(
            phase_lookup(agg["phases"], p).get((n, "prove"), {}).get("wall_s_mean", 0.0) > 0
            for p in provers
        ):
            n_ref, setup = n, s
            break
    if n_ref is None:
        return

    proves = {}
    kmax = 10
    for p in provers:
        t = phase_lookup(agg["phases"], p).get((n_ref, "prove"), {}).get("wall_s_mean", 0.0)
        if t > 0:
            proves[p] = t
            kmax = max(kmax, int(setup / t * 40) + 10)
    if not proves:
        return
    ks = np.unique(np.logspace(0, math.log10(kmax), 240).astype(int))
    ks = ks[ks >= 1]

    fig, ax = plt.subplots(figsize=(9.0, 5.6))
    for p, prove in proves.items():
        (line,) = ax.plot(
            ks, setup / ks + prove,
            marker=PROVER_MARKERS.get(p, ""), markevery=0.15, ms=4,
            label=f"{p} ($T_{{proof}}$ = {prove:.2f} s)",
        )
        # asymptote : le coût par lot tend vers le seul temps de preuve
        ax.axhline(prove, color=line.get_color(), ls=":", lw=0.9)
        k50 = setup / prove  # part du setup dans le coût du lot = 50 %
        ax.axvline(k50, color=line.get_color(), ls="--", lw=0.8, alpha=0.7)
        ax.annotate(
            f"k = {k50:,.0f}\n(setup share = 50%)",
            (k50, setup / k50 + prove),
            textcoords="offset points", xytext=(6, 8),
            fontsize=8, color=line.get_color(),
        )
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Batches proved with the same circuit, $k$")
    ax.set_ylabel("Amortized time per batch (s): $T_{setup}/k + T_{proof}$")
    ax.set_title(
        f"Amortization of the one-off trusted setup "
        f"($N = {n_ref}$, $T_{{setup}}$ = {setup:.0f} s)"
    )
    ax.legend(fontsize=9)
    ax.grid(True, which="both", ls="--", lw=0.5, alpha=0.6)
    save_fig(
        fig, figs_dir, "03_setup_amortization", manifest_figs,
        note="Keys are produced once per circuit and reused for every batch; "
        "witness computation can be pipelined with sequencing.",
    )


def fig_usd_cost(
    agg: Dict[str, Any],
    sizes: Sequence[int],
    provers: Sequence[str],
    usd_per_hour: float,
    figs_dir: str,
    manifest_figs: Dict[str, str],
) -> None:
    """Coût marginal de preuve par transaction, en USD, à tarif horaire donné.

    Reprend la métrique « USD per Proving a Transaction » de Chaliasos et al.
    (AFT'24) : temps de preuve × tarif horaire machine / taille du lot. Le
    setup, one-off, en est exclu (voir la figure d'amortissement).
    """
    import matplotlib.pyplot as plt

    provers = [p for p in provers if p]
    if usd_per_hour <= 0 or not provers:
        return
    order = {"rapidsnark": 0, "snarkjs": 1}
    provers = sorted(provers, key=lambda p: order.get(p, 9))

    fig, ax = plt.subplots(figsize=(9.0, 5.6))
    any_data = False
    for p in provers:
        table = {r["n"]: r for r in agg["phases"] if r["phase"] == "prove" and r["prover"] == p}
        pts = [
            (n, table[n]["wall_s_mean"] * usd_per_hour / 3600.0 / n)
            for n in sizes
            if n in table and table[n]["wall_s_mean"] > 0
        ]
        if not pts:
            continue
        any_data = True
        ax.plot(
            [q[0] for q in pts],
            [q[1] for q in pts],
            marker=PROVER_MARKERS.get(p, "s"),
            label=p,
        )
    if not any_data:
        plt.close(fig)
        return
    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    # text.usetex = False : le croisillon est du texte brut, pas une macro LaTeX
    ax.set_xlabel("Batch size $N$ (# transactions per proof)")
    ax.set_ylabel("Proving cost per transaction (USD)")
    ax.set_title(f"Marginal proving cost per transaction at \\${usd_per_hour:.2f}/h")
    ax.legend()
    ax.grid(True, which="both", ls="--", lw=0.5, alpha=0.6)
    save_fig(
        fig, figs_dir, "04_usd_cost_per_tx", manifest_figs,
        note="Wall-clock proving time x hourly machine rate; the one-off setup is excluded.",
    )


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

    # 01 : le cycle de vie complet — une seule colonne par taille, sans détail
    # par prover : la figure porte le poids relatif des trois phases, pas la
    # comparaison des provers (objet de la figure 01b)
    fig_stacked(
        agg,
        sizes,
        [main_prover],
        "wall_s",
        1.0,
        "Time (s)",
        "",
        figs_dir,
        manifest_figs,
        "01_phase_cost_stacked",
    )

    # 01b : coût récurrent seul (preuve + vérification), avec le détail par
    # prover — le setup, one-off, en est exclu
    fig_stacked(
        agg,
        sizes,
        provers if len(provers) > 1 else [main_prover],
        "wall_s",
        1.0,
        "Time (s)",
        "",
        figs_dir,
        manifest_figs,
        "01b_recurring_cost_stacked",
        phases=("prove", "verify"),
    )

    # un prover mesuré dans un conteneur n'est vu qu'à travers les compteurs
    # système : la note l'indique sur les figures qui reposent sur le temps CPU
    container_provers = sorted(
        {
            s["prover"]
            for s in steps
            if s["phase"] == "prove" and s.get("scope") == "system" and s["prover"]
        }
    )
    cpu_note = (
        f"CPU time for {', '.join(container_provers)} is derived from system-wide counters "
        f"(container execution) and includes launcher overhead."
        if container_provers
        else ""
    )

    # 02 : dimensionnement machine — RAM et cœurs par phase (claim « downsizing »)
    fig_machine_sizing(agg, steps, sizes, provers or [main_prover], ncpu, figs_dir, manifest_figs, cpu_note)
    # 03 : amortissement du setup one-off (justifie son exclusion du chemin critique)
    fig_setup_amortization(agg, sizes, provers, figs_dir, manifest_figs)
    # 04 : coût marginal en USD par transaction (si un tarif horaire est fourni)
    fig_usd_cost(agg, sizes, provers, getattr(args, "usd_per_hour", 0.0), figs_dir, manifest_figs)

    manifest.setdefault("artifacts", {})["figures"] = manifest_figs


# --------------------------------------------------------------------------- #
# Sorties tabulaires, tables LaTeX et rapport
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


def _tex_sci(v: float) -> str:
    """Nombre pour LaTeX : notation scientifique hors de [1e-2, 1e3]."""
    if v == 0:
        return "0"
    e = int(math.floor(math.log10(abs(v))))
    if -2 <= e <= 3:
        return f"{v:.4g}"
    m = v / (10 ** e)
    return f"${m:.2f}\\times10^{{{e}}}$"


def _tex_bytes(b: float) -> str:
    if b <= 0:
        return "--"
    if b >= GB:
        return f"{b / GB:.2f}\\,GiB"
    return f"{b / MB:.0f}\\,MiB"


TEX_HEADER = "% Généré par measure_zk_resources.py — adapter caption/label avant insertion."


def _tex_table(caption: str, label: str, colspec: str, header: str, rows: List[str],
               comments: Sequence[str] = ()) -> List[str]:
    """Squelette commun des tables de l'article (arraystretch + resizebox)."""
    return [
        TEX_HEADER,
        *[f"% {c}" for c in comments],
        "\\begin{table}[t]",
        "\\centering",
        f"\\caption{{{caption}}}",
        f"\\label{{{label}}}",
        "\\renewcommand{\\arraystretch}{1.2}",
        "\\resizebox{\\columnwidth}{!}{",
        f"\\begin{{tabular}}{{{colspec}}}",
        "\\hline",
        header,
        "\\hline",
        *rows,
        "\\hline",
        "\\end{tabular}}",
        "\\end{table}",
    ]


def _cores(row: Optional[Dict[str, Any]]) -> float:
    if not row or row.get("wall_s_mean", 0.0) <= 0:
        return 0.0
    return row["cpu_total_s_mean"] / row["wall_s_mean"]


def _ratio(a: float, b: float) -> str:
    """Rapport a/b en notation compacte (2 400x, 2,9x, --)."""
    if b <= 0 or a <= 0:
        return "--"
    r = a / b
    if r >= 100:
        return f"{r:,.0f}$\\times$".replace(",", "\\,")
    return f"{r:.1f}$\\times$" if r >= 10 else f"{r:.2f}$\\times$"


# ------------------------- tables « analyse » ------------------------------ #


def tab_lifecycle(tmain, lookups, n_ref: int, provers: Sequence[str]) -> List[str]:
    """Cycle de vie à N fixé : une ligne par phase, avec fréquence et ratio
    explicite par rapport à la vérification (la phase de référence, on-chain)."""
    ver = tmain.get((n_ref, "verify"))
    t_ver = ver["wall_s_mean"] if ver else 0.0
    rows: List[str] = []

    def row(label: str, freq: str, r: Optional[Dict[str, Any]]) -> None:
        if not r:
            return
        rows.append(
            f"{label} & {freq} & {r['wall_s_mean']:.2f} & {r['cpu_total_s_mean']:.2f} & "
            f"{_cores(r):.1f} & {_tex_bytes(r['rss_peak_bytes_mean'])} & "
            f"{_ratio(r['wall_s_mean'], t_ver)} \\\\"
        )

    row("Trusted setup", "$1\\times$ per circuit", tmain.get((n_ref, "env")))
    for p in provers:
        label = "Proof generation" + (f" ({p})" if len(provers) > 1 else "")
        row(label, "$1\\times$ per batch", lookups[p].get((n_ref, "prove")))
    row("Verification", "$1\\times$ per batch", ver)
    if not rows:
        return []
    env = tmain.get((n_ref, "env"))
    gap = (
        f"The setup costs {_ratio(env['wall_s_mean'], t_ver)} the verification time, but it is"
        if env and t_ver > 0
        else "The setup is"
    )
    return _tex_table(
        caption=(
            f"Life cycle of the Groth16 pipeline at $N_b = {n_ref}$. {gap} "
            "paid only once per circuit, whereas proving and verification are paid on every "
            "batch -- the frequency column is what makes the gap irrelevant in steady state."
        ),
        label="tab:zk_lifecycle",
        colspec="|l|l|r|r|r|r|r|",
        header=(
            "\\textbf{Phase} & \\textbf{Frequency} & \\textbf{Wall (s)} & \\textbf{CPU (s)} & "
            "\\textbf{Cores} & \\textbf{Peak RSS} & \\textbf{$\\times$ vs verify} \\\\"
        ),
        rows=rows,
    )


def tab_phase_ratios(tmain, lookups, sizes: Sequence[int], main_prover: str) -> List[str]:
    """Asymétrie setup / preuve / vérification en fonction de N : le setup croît
    avec la taille du circuit, la vérification reste constante."""
    rows: List[str] = []
    for n in sizes:
        env = tmain.get((n, "env"))
        prove = lookups.get(main_prover, tmain).get((n, "prove"))
        ver = tmain.get((n, "verify"))
        if not (env and prove and ver):
            continue
        t_s, t_p, t_v = env["wall_s_mean"], prove["wall_s_mean"], ver["wall_s_mean"]
        rows.append(
            f"{n} & {t_s:.1f} & {t_p:.2f} & {t_v:.2f} & "
            f"{_ratio(t_s, t_p)} & {_ratio(t_p, t_v)} \\\\"
        )
    if not rows:
        return []
    suffix = f" (proving times measured with {main_prover})" if main_prover else ""
    return _tex_table(
        caption=(
            "Phase-time ratios as a function of the batch size $N_b$" + suffix + ". "
            "The one-off setup grows super-linearly with the circuit size while verification "
            "stays constant, so the asymmetry widens with $N_b$."
        ),
        label="tab:zk_phase_ratios",
        colspec="|r|r|r|r|r|r|",
        header=(
            "$N_b$ & $T_{\\mathit{setup}}$ (s) & $T_{\\mathit{proof}}$ (s) & "
            "$T_{\\mathit{ver}}$ (s) & $T_{\\mathit{setup}}/T_{\\mathit{proof}}$ & "
            "$T_{\\mathit{proof}}/T_{\\mathit{ver}}$ \\\\"
        ),
        rows=rows,
    )


def tab_amortization(tmain, lookups, n_ref: int, provers: Sequence[str],
                     batch_period_s: float) -> List[str]:
    """Seuils d'amortissement du setup : nombre de lots k au-delà duquel le
    setup pèse moins de 50 %, puis moins de 10 % du coût cumulé par lot.

    Part du setup dans le coût par lot : (T_setup/k) / (T_setup/k + T_proof).
    = 50 % pour k = T_setup/T_proof ; = 10 % pour k = 9 T_setup/T_proof.
    """
    env = tmain.get((n_ref, "env"))
    if not env:
        return []
    t_setup = env["wall_s_mean"]
    rows: List[str] = []
    for p in provers:
        r = lookups[p].get((n_ref, "prove"))
        if not r or r["wall_s_mean"] <= 0 or t_setup <= 0:
            continue
        t_proof = r["wall_s_mean"]
        k50 = t_setup / t_proof
        k10 = 9.0 * k50
        hours = k10 * batch_period_s / 3600.0
        horizon = f"{hours:.1f}\\,h" if hours < 48 else f"{hours / 24:.1f}\\,d"
        # séparateur de milliers typographique, sans toucher aux macros LaTeX
        fk = lambda k: f"{k:,.0f}".replace(",", "\\,")
        rows.append(
            f"{p} & {t_setup:.1f} & {t_proof:.2f} & {fk(k50)} & {fk(k10)} & "
            f"$\\approx${horizon} \\\\"
        )
    if not rows:
        return []
    return _tex_table(
        caption=(
            f"Amortization thresholds of the one-off trusted setup at $N_b = {n_ref}$: "
            "number of batches $k$ after which the setup accounts for less than 50\\,\\% and "
            "less than 10\\,\\% of the cumulated per-batch time "
            "$T_{\\mathit{setup}}/k + T_{\\mathit{proof}}$. The last column converts the "
            f"10\\,\\% threshold into operating time at one batch every {batch_period_s:.0f}\\,s."
        ),
        label="tab:zk_amortization",
        colspec="|l|r|r|r|r|r|",
        header=(
            "\\textbf{Prover} & $T_{\\mathit{setup}}$ (s) & $T_{\\mathit{proof}}$ (s) & "
            "$k$ (setup $=50\\,\\%$) & $k$ ($10\\,\\%$) & \\textbf{Horizon} \\\\"
        ),
        rows=rows,
        comments=[
            "Seuils : k_50 = T_setup/T_proof, k_10 = 9 T_setup/T_proof "
            "(part du setup dans T_setup/k + T_proof).",
        ],
    )


# Catalogue indicatif (AWS on-demand, us-east-1) : sert uniquement à traduire
# un besoin (vCPU, RAM) en une instance et un tarif ; les prix évoluent.
CLOUD_CATALOG: Tuple[Tuple[str, int, float, float], ...] = (
    ("c7i.large", 2, 4.0, 0.0893),
    ("m7i.large", 2, 8.0, 0.1008),
    ("c7i.xlarge", 4, 8.0, 0.1785),
    ("m7i.xlarge", 4, 16.0, 0.2016),
    ("c7i.2xlarge", 8, 16.0, 0.3570),
    ("m7i.2xlarge", 8, 32.0, 0.4032),
    ("c7i.4xlarge", 16, 32.0, 0.7140),
    ("m7i.4xlarge", 16, 64.0, 0.8064),
    ("c7i.8xlarge", 32, 64.0, 1.4280),
    ("m7i.8xlarge", 32, 128.0, 1.6128),
)


def _pick_instance(vcpu: int, ram_gib: float) -> Optional[Tuple[str, int, float, float]]:
    fits = [i for i in CLOUD_CATALOG if i[1] >= vcpu and i[2] >= ram_gib]
    return min(fits, key=lambda i: i[3]) if fits else None


def tab_provisioning(tmain, lookups, sizes: Sequence[int],
                     provers: Sequence[str], headroom: float = 1.5) -> List[str]:
    """Dimensionnement opérationnel par phase : la figure 02 traduite en
    décision de provisioning (vCPU, RAM, instance, tarif)."""

    def need(rows: List[Dict[str, Any]]) -> Optional[Tuple[int, float]]:
        rows = [r for r in rows if r and r.get("wall_s_mean", 0.0) > 0]
        if not rows:
            return None
        vcpu = max(1, math.ceil(max(_cores(r) for r in rows)))
        ram_b = max(r["rss_peak_bytes_mean"] for r in rows) * headroom
        # arrondi au demi-GiB supérieur : assez fin pour distinguer les phases
        ram = max(0.5, math.ceil(ram_b / GB * 2.0) / 2.0)
        return vcpu, ram

    entries: List[Tuple[str, str, Optional[Tuple[int, float]]]] = [
        ("Setup machine", "Trusted setup (one-off)",
         need([tmain.get((n, "env")) for n in sizes])),
    ]
    for p in provers:
        label = "Proving node" + (f" ({p})" if len(provers) > 1 else "")
        entries.append((label, "Proof generation (per batch)",
                        need([lookups[p].get((n, "prove")) for n in sizes])))
    entries.append(("Verifier node", "Verification (per batch)",
                    need([tmain.get((n, "verify")) for n in sizes])))

    rows: List[str] = []
    for role, phase, req in entries:
        if not req:
            continue
        vcpu, ram = req
        inst = _pick_instance(vcpu, ram)
        inst_name = inst[0] if inst else "--"
        price = f"{inst[3]:.3f}" if inst else "--"
        rows.append(
            f"{role} & {phase} & {vcpu} & {ram:g}\\,GiB & \\texttt{{{inst_name}}} & {price} \\\\"
        )
    if not rows:
        return []
    n_max = max(sizes) if sizes else 0
    return _tex_table(
        caption=(
            f"Provisioning derived from the measured footprint (worst case over $N_b \\leq {n_max}$, "
            f"with a {headroom:g}$\\times$ memory headroom and RAM rounded up to the next power of two). "
            "Once the setup has been run, the recurring nodes fit on a markedly smaller instance. "
            "Instance types and rates are indicative AWS on-demand prices (us-east-1)."
        ),
        label="tab:zk_provisioning",
        colspec="|l|l|r|r|l|r|",
        header=(
            "\\textbf{Role} & \\textbf{Phase} & \\textbf{vCPU} & \\textbf{RAM} & "
            "\\textbf{Instance} & \\textbf{USD/h} \\\\"
        ),
        rows=rows,
        comments=["Tarifs indicatifs : vérifier avant publication."],
    )


# artefact -> (libellé, phase productrice, fréquence)
ARTIFACT_ROLES: Tuple[Tuple[str, str, str, str], ...] = (
    ("input.json", "Batch input (witness input)", "Sequencer", "$1\\times$ per batch"),
    ("ptau_total", "Powers of Tau (phase 1)", "Setup", "$1\\times$ per circuit size"),
    ("circuit.r1cs", "R1CS constraint system", "Setup (circom)", "$1\\times$ per circuit"),
    ("circuit_final.zkey", "Proving key", "Setup (phase 2)", "$1\\times$ per circuit"),
    ("verification_key.json", "Verification key", "Setup (phase 2)", "$1\\times$ per circuit"),
    ("verifier.sol", "On-chain verifier", "Setup (export)", "$1\\times$ per circuit (deployed)"),
    ("witness.wtns", "Witness", "Proving", "$1\\times$ per batch (ephemeral)"),
    ("proof.json", "Groth16 proof", "Proving", "$1\\times$ per batch (on-chain)"),
    ("public.json", "Public inputs", "Proving", "$1\\times$ per batch (on-chain)"),
)


def _size_cell(b: float) -> str:
    if b <= 0:
        return "--"
    if b >= MB:
        return f"{b / MB:.1f}\\,MiB"
    if b >= 1024:
        return f"{b / 1024:.1f}\\,KiB"
    return f"{b:.0f}\\,B"


def circuit_artifacts(manifest: Dict[str, Any], outdir: str, n: int) -> Dict[str, float]:
    """Tailles des artefacts d'un circuit : celles du manifest, complétées au
    besoin par une lecture disque (manifests antérieurs à un nouvel artefact)."""
    entry = (manifest.get("circuits") or {}).get(str(n)) or {}
    out: Dict[str, float] = dict(entry.get("artifacts") or {})
    rel = entry.get("dir")
    if rel:
        base = rel if os.path.isabs(rel) else os.path.join(outdir, rel)
        for name in ARTIFACTS_OF_INTEREST:
            if name not in out:
                path = os.path.join(base, name)
                if os.path.isfile(path):
                    out[name] = float(os.path.getsize(path))
    return out


# artefacts manipulés par la génération de preuve : entrées, clé, sorties
PROVING_IO: Tuple[Tuple[str, str, str], ...] = (
    ("input.json", "Batch input", "in"),
    ("circuit_final.zkey", "Proving key", "in"),
    ("witness.wtns", "Witness", "tmp"),
    ("public.json", "Public inputs", "out"),
    ("proof.json", "Proof", "out"),
)


def tab_proving_io(manifest: Dict[str, Any], outdir: str, sizes: Sequence[int]) -> List[str]:
    """Taille des éléments manipulés par la génération de preuve, par $N_b$ :
    entrées (input, clé de preuve), témoin intermédiaire, sorties (public,
    preuve). Rend visible que seules les sorties restent de taille constante."""
    per_n = {n: circuit_artifacts(manifest, outdir, n) for n in sizes}
    per_n = {n: a for n, a in per_n.items() if a}
    if not per_n:
        return []
    cols = [c for c in PROVING_IO if any(a.get(c[0], 0) > 0 for a in per_n.values())]
    if not cols:
        return []

    rows = [
        f"{n} & " + " & ".join(_size_cell(per_n[n].get(key, 0)) for key, _l, _d in cols) + " \\\\"
        for n in sorted(per_n)
    ]
    tag = {"in": "input", "tmp": "intermediate", "out": "output"}
    header = "$N_b$ & " + " & ".join(
        f"\\textbf{{{label}}}\\,\\footnotesize({tag[d]})" for _k, label, d in cols
    ) + " \\\\"
    biggest = max(per_n)
    proof_b = per_n[biggest].get("proof.json", 0)
    cap = (
        "Size of the data handled by proof generation, as a function of the batch size "
        f"$N_b$: inputs, intermediate witness and outputs."
    )
    if proof_b:
        cap += (
            f" Inputs and witness grow linearly with $N_b$, while the proof stays at "
            f"{proof_b:.0f}\\,B and the public inputs stay constant -- the on-chain payload "
            "does not depend on how many transactions the batch contains."
        )
    return _tex_table(
        caption=cap,
        label="tab:zk_proving_io",
        colspec="|r|" + "r|" * len(cols),
        header=header,
        rows=rows,
    )


def tab_artifacts(manifest: Dict[str, Any], outdir: str, sizes: Sequence[int]) -> List[str]:
    """Empreinte de stockage par artefact : les clés du setup grossissent avec
    le circuit, la preuve reste de taille constante — d'où un coût de
    vérification on-chain indépendant de $N_b$."""
    circuits = manifest.get("circuits") or {}
    avail = [n for n in sizes if str(n) in circuits and circuits[str(n)].get("artifacts")]
    if not avail:
        return []
    n_lo, n_hi = avail[0], avail[-1]
    a_lo = circuit_artifacts(manifest, outdir, n_lo)
    a_hi = circuit_artifacts(manifest, outdir, n_hi)

    size_cell = _size_cell
    rows: List[str] = []
    for key, label, produced, freq in ARTIFACT_ROLES:
        lo, hi = a_lo.get(key, 0), a_hi.get(key, 0)
        if lo <= 0 and hi <= 0:
            continue
        rows.append(
            f"{label} & {size_cell(lo)} & {size_cell(hi)} & {produced} & {freq} \\\\"
        )
    if not rows:
        return []
    proof_hi = a_hi.get("proof.json", 0)
    cap = (
        f"Storage footprint of the pipeline artefacts, from $N_b = {n_lo}$ to $N_b = {n_hi}$. "
        "Setup artefacts (Powers of Tau, proving key) grow with the circuit, whereas the "
    )
    if proof_hi:
        cap += (
            f"proof stays at {proof_hi}\\,B regardless of $N_b$ -- the reason why the on-chain "
            "verification cost per batch is constant."
        )
    else:
        cap += "proof stays constant in size regardless of $N_b$."
    return _tex_table(
        caption=cap,
        label="tab:zk_artifacts",
        colspec="|l|r|r|l|l|",
        header=(
            f"\\textbf{{Artefact}} & $N_b = {n_lo}$ & $N_b = {n_hi}$ & "
            "\\textbf{Produced by} & \\textbf{Frequency} \\\\"
        ),
        rows=rows,
    )


def write_latex_tables(
    outdir: str,
    manifest: Dict[str, Any],
    provers: Sequence[str],
    main_prover: str,
    usd_per_hour: float,
) -> None:
    """Tables LaTeX prêtes à coller dans l'article (style tabulaire du papier :
    \\renewcommand{\\arraystretch}{1.2} + \\resizebox{\\columnwidth})."""
    agg = manifest["aggregates"]
    sizes = manifest["params"]["sizes"]
    if not agg.get("phases"):
        return
    tables_dir = os.path.join(outdir, "tables")
    os.makedirs(tables_dir, exist_ok=True)
    provers = [p for p in provers if p] or ([main_prover] if main_prover else [""])
    written: Dict[str, str] = {}

    def emit(name: str, lines: List[str]) -> None:
        path = os.path.join(tables_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        written[name] = f"tables/{name}"
        print(f"    table  : tables/{name}")

    tmain = phase_lookup(agg["phases"], main_prover)

    # --- table 1 : ressources par (taille, phase) ------------------------- #
    lines = [
        "% Généré par measure_zk_resources.py — adapter caption/label avant insertion.",
        "\\begin{table}[t]",
        "\\centering",
        "\\caption{Resource footprint of the Groth16 pipeline as a function of the"
        " batch size $N_b$ (mean over repetitions; I/O measured at the"
        " read()/write() syscall level).}",
        "\\label{tab:zk_resources}",
        "\\renewcommand{\\arraystretch}{1.2}",
        "\\resizebox{\\columnwidth}{!}{",
        "\\begin{tabular}{|r|l|r|r|r|r|r|}",
        "\\hline",
        "$N_b$ & \\textbf{Phase} & \\textbf{Wall (s)} & \\textbf{CPU (s)} & "
        "\\textbf{Cores} & \\textbf{Peak RSS} & \\textbf{I/O R / W} \\\\",
        "\\hline",
    ]

    def resource_row(table, n: int, phase: str, label: str) -> Optional[str]:
        r = table.get((n, phase))
        if not r:
            return None
        wall = r["wall_s_mean"]
        cores = r["cpu_total_s_mean"] / wall if wall > 0 else 0.0
        io = f"{_tex_bytes(r['io_rchar_bytes_mean'])} / {_tex_bytes(r['io_wchar_bytes_mean'])}"
        return (
            f"{n} & {label} & {wall:.2f} & {r['cpu_total_s_mean']:.2f} & "
            f"{cores:.1f} & {_tex_bytes(r['rss_peak_bytes_mean'])} & {io} \\\\"
        )

    for n in sizes:
        block: List[str] = []
        r = resource_row(tmain, n, "env", "Trusted setup (one-off)")
        if r:
            block.append(r)
        for p in provers:
            label = "Proof generation" + (f" ({p})" if len(provers) > 1 else "")
            r = resource_row(phase_lookup(agg["phases"], p), n, "prove", label)
            if r:
                block.append(r)
        r = resource_row(tmain, n, "verify", "Verification")
        if r:
            block.append(r)
        if block:
            lines.extend(block)
            lines.append("\\hline")
    lines += ["\\end{tabular}}", "\\end{table}"]
    emit("tab_pipeline_resources.tex", lines)

    # --- table 2 : provers face à face à la taille de référence ----------- #
    lookups = {p: phase_lookup(agg["phases"], p) for p in provers}
    n_ref = None
    for n in reversed(list(sizes)):
        if all(lookups[p].get((n, "prove")) for p in provers):
            n_ref = n
            break
    if n_ref is not None:
        env = tmain.get((n_ref, "env"))
        ver = tmain.get((n_ref, "verify"))

        def metric_row(label: str, fn, fmt: str = "{:.2f}") -> str:
            cells = []
            for p in provers:
                v = fn(lookups[p][(n_ref, "prove")])
                cells.append(v if isinstance(v, str) else fmt.format(v))
            return f"{label} & " + " & ".join(cells) + " \\\\"

        cap = (
            f"Prover comparison for the same Groth16 circuit at $N_b = {n_ref}$."
        )
        if env:
            cap += (
                f" The one-off trusted setup took {env['wall_s_mean']:.0f}\\,s"
                f" (peak {_tex_bytes(env['rss_peak_bytes_mean'])}) and is shared by all provers."
            )
        if ver:
            cap += f" Verification takes {ver['wall_s_mean']:.2f}\\,s, independently of the prover."
        if usd_per_hour > 0:
            cap += f" USD figures assume a machine rate of \\${usd_per_hour:.2f}/h."

        rows = [
            metric_row("Wall-clock per proof (s)", lambda r: r["wall_s_mean"]),
            metric_row("CPU time per proof (s)", lambda r: r["cpu_total_s_mean"]),
            metric_row(
                "Mean busy cores",
                lambda r: (r["cpu_total_s_mean"] / r["wall_s_mean"]) if r["wall_s_mean"] > 0 else 0.0,
                "{:.1f}",
            ),
            metric_row("Peak RSS", lambda r: _tex_bytes(r["rss_peak_bytes_mean"])),
            metric_row(
                "Proving throughput (tx/s)",
                lambda r: (n_ref / r["wall_s_mean"]) if r["wall_s_mean"] > 0 else 0.0,
                "{:.1f}",
            ),
            metric_row("CPU time per tx (ms)", lambda r: r["cpu_total_s_mean"] / n_ref * 1000.0),
        ]
        if usd_per_hour > 0:
            rows.append(
                metric_row("USD per proof", lambda r: _tex_sci(r["wall_s_mean"] * usd_per_hour / 3600.0))
            )
            rows.append(
                metric_row(
                    "USD per transaction",
                    lambda r: _tex_sci(r["wall_s_mean"] * usd_per_hour / 3600.0 / n_ref),
                )
            )
        lines = [
            "% Généré par measure_zk_resources.py — adapter caption/label avant insertion.",
            "\\begin{table}[t]",
            "\\centering",
            f"\\caption{{{cap}}}",
            "\\label{tab:zk_prover_comparison}",
            "\\renewcommand{\\arraystretch}{1.2}",
            "\\resizebox{\\columnwidth}{!}{",
            "\\begin{tabular}{|l|" + "r|" * len(provers) + "}",
            "\\hline",
            "\\textbf{Metric} & " + " & ".join(f"\\textbf{{{p}}}" for p in provers) + " \\\\",
            "\\hline",
            *rows,
            "\\hline",
            "\\end{tabular}}",
            "\\end{table}",
        ]
        emit("tab_prover_comparison.tex", lines)

    # --- table 3 + CSV : coûts en USD (métriques de Chaliasos et al.) ----- #
    if usd_per_hour > 0:
        cost_rows: List[Dict[str, Any]] = []
        for r in sorted(
            (r for r in agg["phases"] if r["phase"] == "prove" and r["prover"]),
            key=lambda r: (r["n"], r["prover"]),
        ):
            wall = r["wall_s_mean"]
            if wall <= 0:
                continue
            usd = wall * usd_per_hour / 3600.0
            cost_rows.append(
                {
                    "n": r["n"],
                    "prover": r["prover"],
                    "seconds_per_proof": round(wall, 4),
                    "usd_per_proof": usd,
                    "usd_per_tx": usd / r["n"],
                }
            )
        if cost_rows:
            save_csv(os.path.join(tables_dir, "cost_usd.csv"), cost_rows)
            written["cost_usd.csv"] = "tables/cost_usd.csv"
            lines = [
                "% Généré par measure_zk_resources.py — adapter caption/label avant insertion.",
                "% Métriques reprises de Chaliasos et al., AFT'24 (\\cite{...}) :",
                "% Seconds per Proof, USD per Proof, USD per Proving a Transaction.",
                "\\begin{table}[t]",
                "\\centering",
                f"\\caption{{Off-chain proving cost of one batch, at a machine rate of"
                f" \\${usd_per_hour:.2f}/h (one-off setup excluded).}}",
                "\\label{tab:zk_cost_usd}",
                "\\renewcommand{\\arraystretch}{1.2}",
                "\\resizebox{\\columnwidth}{!}{",
                "\\begin{tabular}{|r|l|r|r|r|}",
                "\\hline",
                "$N_b$ & \\textbf{Prover} & \\textbf{s / proof} & "
                "\\textbf{USD / proof} & \\textbf{USD / tx} \\\\",
                "\\hline",
            ]
            for c in cost_rows:
                lines.append(
                    f"{c['n']} & {c['prover']} & {c['seconds_per_proof']:.2f} & "
                    f"{_tex_sci(c['usd_per_proof'])} & {_tex_sci(c['usd_per_tx'])} \\\\"
                )
            lines += ["\\hline", "\\end{tabular}}", "\\end{table}"]
            emit("tab_cost_usd.tex", lines)

    # --- tables d'analyse : cycle de vie, ratios, amortissement, ---------- #
    # --- dimensionnement, stockage --------------------------------------- #
    batch_period = float(manifest["params"].get("batch_period_s") or 12.0)
    n_life = n_ref if n_ref is not None else (sizes[-1] if sizes else None)
    if n_life is not None:
        for name, tex in (
            ("tab_lifecycle.tex", tab_lifecycle(tmain, lookups, n_life, provers)),
            ("tab_amortization.tex", tab_amortization(tmain, lookups, n_life, provers, batch_period)),
        ):
            if tex:
                emit(name, tex)
    for name, tex in (
        ("tab_phase_ratios.tex", tab_phase_ratios(tmain, lookups, sizes, main_prover)),
        ("tab_provisioning.tex", tab_provisioning(tmain, lookups, sizes, provers)),
        ("tab_proving_io.tex", tab_proving_io(manifest, outdir, sizes)),
        ("tab_artifacts.tex", tab_artifacts(manifest, outdir, sizes)),
    ):
        if tex:
            emit(name, tex)

    if written:
        manifest.setdefault("artifacts", {})["tables"] = written


def write_report(outdir: str, manifest: Dict[str, Any], prover: str, usd_per_hour: float = 0.0) -> None:
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

    # --- dimensionnement machine (claim « downsizing ») ------------------- #
    s = sizing_summary(agg, sizes, manifest["params"].get("provers") or [prover])
    if s:
        lines.append("\n## Dimensionnement machine\n")
        lines.append(
            f"À N={s['n']} : RSS max du setup {_human(s['env_rss'])} "
            f"({s['env_cores']:.1f} cœurs occupés), de la preuve {_human(s['prove_rss'])} "
            f"({s['prove_cores']:.1f} cœurs), de la vérification {_human(s['verify_rss'])}."
        )
        if s["ram_ratio"] >= 1.2:
            lines.append(
                f"Une fois le setup exécuté (one-off), un nœud dédié à la preuve peut donc être "
                f"provisionné avec ~{s['ram_ratio']:.1f}× moins de RAM que la machine de setup."
            )

    # --- coûts en USD ------------------------------------------------------ #
    if usd_per_hour > 0:
        rows = sorted(
            (r for r in agg["phases"] if r["phase"] == "prove" and r["prover"] and r["wall_s_mean"] > 0),
            key=lambda r: (r["n"], r["prover"]),
        )
        if rows:
            lines.append(f"\n## Coût en USD (tarif machine {usd_per_hour:.2f} $/h, setup exclu)\n")
            lines.append("| n | prover | s / preuve | $ / preuve | $ / transaction |")
            lines.append("|---|--------|------------|------------|-----------------|")
            for r in rows:
                usd = r["wall_s_mean"] * usd_per_hour / 3600.0
                lines.append(
                    f"| {r['n']} | {r['prover']} | {r['wall_s_mean']:.3f} | "
                    f"{usd:.6f} | {usd / r['n']:.3e} |"
                )

    figs = manifest.get("artifacts", {}).get("figures", {})
    if figs:
        lines.append("\n## Figures\n")
        lines.append("Chaque figure porte une affirmation unique, réutilisable comme légende :\n")
        for name, rel in sorted(figs.items()):
            claim = FIGURE_CLAIMS.get(name, "")
            lines.append(f"- `{rel}`" + (f" — {claim}" if claim else ""))
    tables = manifest.get("artifacts", {}).get("tables", {})
    if tables:
        lines.append("\n## Tables LaTeX\n")
        lines.append("Prêtes à insérer dans l'article (adapter caption/label) :\n")
        for name, rel in sorted(tables.items()):
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
        if not rs_cfg.available and (args.prover == "rapidsnark" or args.require_rapidsnark):
            raise SystemExit(
                f"[erreur] rapidsnark demandé mais {rs_cfg.describe()}\n"
                f"          (relancer sans --require-rapidsnark pour mesurer snarkjs seul)"
            )
        if not rs_cfg.available and args.prover in ("both", "auto"):
            # sans ce garde-fou, un run de plusieurs heures se termine sans la
            # colonne rapidsnark et doit être refait
            print(f"[attention] rapidsnark indisponible : {rs_cfg.reason}")
            print("            le run ne mesurera que snarkjs "
                  "(--require-rapidsnark pour échouer immédiatement).")

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
            "batch_period_s": args.batch_period_s,
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
    g.add_argument("--no-docker-autostart", dest="docker_autostart", action="store_false",
                   help="ne pas démarrer automatiquement le conteneur s'il est arrêté")
    g.add_argument("--require-rapidsnark", action="store_true",
                   help="échouer si rapidsnark n'est pas disponible, au lieu de mesurer snarkjs seul")
    g.add_argument("--docker-mount-prefix", type=str, default="mnt/projet",
                   help="chemin des circuits vu depuis le conteneur")
    g.add_argument("--docker-image", type=str, default="", help="image à utiliser via docker run (fallback)")
    g.add_argument("--docker-prover-path", type=str, default="mnt/projet/rapidsnark/package/bin/prover",
                   help="chemin du binaire prover dans le conteneur")

    g = p.add_argument_group("mesure et sorties")
    g.add_argument("--sample-interval", type=float, default=0.1, help="période d'échantillonnage (s)")
    g.add_argument("--batch-period-s", type=float, default=12.0,
                   help="période de production d'un lot pour l'horizon d'amortissement (défaut : slot Ethereum, 12 s)")
    g.add_argument("--usd-per-hour", type=float, default=0.0,
                   help="tarif horaire de la machine en USD : active la figure et les tables "
                        "de coût ($/preuve, $/tx) ; 0 = désactivé")
    g.add_argument("--no-plots", action="store_true", help="ne pas générer les figures")
    g.add_argument("--plot-only", type=str, default="",
                   help="régénérer figures + tables d'un run existant, ex. bench-out/20260730_101500")
    g.add_argument("--no-science-style", action="store_true", help="ne pas utiliser scienceplots")
    g.add_argument("--no-titles", action="store_true",
                   help="figures sans titre (le titre devient la légende de l'article)")
    g.add_argument("--big-font", action="store_true", help="polices agrandies (slides, impression)")
    g.add_argument("--quiet", action="store_true", help="affichage minimal")
    g.add_argument("--dry-run", action="store_true", help="afficher les commandes sans les exécuter")

    args = p.parse_args()

    # --- régénération de figures et tables uniquement --- #
    if args.plot_only:
        outdir = os.path.abspath(args.plot_only)
        manifest_path = os.path.join(outdir, "manifest.json")
        if not os.path.isfile(manifest_path):
            raise SystemExit(f"[erreur] {manifest_path} introuvable")
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        # les tables d'analyse dépendent de ce paramètre : il suit la CLI de
        # régénération, y compris pour les manifests antérieurs
        manifest.setdefault("params", {})["batch_period_s"] = args.batch_period_s
        build_figures(outdir, manifest, args)
        provers = sorted({r["prover"] for r in manifest["aggregates"]["phases"] if r["phase"] == "prove" and r["prover"]})
        main_prover = args.main_prover if args.main_prover in provers else (provers[0] if provers else "")
        write_latex_tables(outdir, manifest, provers, main_prover, args.usd_per_hour)
        save_json(manifest_path, manifest)
        write_report(outdir, manifest, main_prover, args.usd_per_hour)
        print(f"\n>>> Figures régénérées dans {os.path.join(outdir, 'figs')}")
        print(f">>> Tables LaTeX dans {os.path.join(outdir, 'tables')}")
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

    write_latex_tables(outdir, manifest, provers, main_prover, args.usd_per_hour)
    write_report(outdir, manifest, main_prover, args.usd_per_hour)
    save_json(os.path.join(outdir, "manifest.json"), manifest)

    failed = [s for s in manifest["steps"] if not s.get("ok", True)]
    print(f"\n>>> Terminé en {manifest['total_wall_s']:.1f} s — sorties dans {outdir}")
    print("    - mesures brutes  : raw/steps.jsonl, raw/samples/*.csv")
    print("    - agrégats        : steps.csv, phases.csv, env_breakdown.csv")
    print("    - figures         : figs/*.png, figs/*.svg")
    print("    - tables LaTeX    : tables/*.tex (+ tables/cost_usd.csv)")
    print("    - synthèse        : report.md, manifest.json")
    if failed:
        print(f"    [!] {len(failed)} étape(s) en échec — voir report.md et logs/")


if __name__ == "__main__":
    main()