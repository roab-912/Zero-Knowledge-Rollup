#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
optimize_batch_partition.py

Vaut-il mieux prouver un batch de N transactions dans UN gros circuit rempli de
transactions vides, ou le DÉCOUPER en plusieurs circuits plus petits ?

On ne dispose que d'un jeu fini de tailles de circuit (les puissances de deux
1..8192). Pour N = 1050 on peut donc :

  * décomposition binaire : 1024 + 16 + 8 + 2   -> 4 preuves, 0 padding
  * circuit supérieur     : un seul 2048        -> 1 preuve, 998 tx vides

Le script calcule l'optimum exact (programmation dynamique) et le compare à
ces deux heuristiques. Il produit trois figures :

  01_time_vs_n          coût réel : optimum contre heuristiques, par prover
  02_decomposition_map  pour chaque N, les tailles que retient l'optimum
  03_critical_rho       la règle de décision, indépendante de la machine

Domaine : tailles de circuit = puissances de deux de 1 à 8192, et N parcourant
*toutes* les valeurs entières de 1 à 8192.

Pourquoi on ne mesure PAS chaque N un par un
--------------------------------------------
Le temps de preuve d'un circuit de taille k ne dépend que de k, pas du nombre de
transactions réelles qu'on y met : les transactions vides traversent exactement
les mêmes contraintes. Le coût d'un plan est donc *exactement* additif :

    T(S) = sum_{k in S} c(k) + |S| * alpha

où alpha est le surcoût fixe payé par preuve (vérification, soumission). On
mesure donc les |K| coûts atomiques une fois (`measure` ou `import`), puis on
compose exactement pour tout N (`analyze`).

Utilisation
-----------
    # 1. modèle de coût, depuis un run de bench existant
    python scripts/bench/optimize_batch_partition.py import \
        --from bench-out/20260825_120620

    # ... ou par mesure directe des circuits présents
    python scripts/bench/optimize_batch_partition.py measure --repeat 10

    # 2. les trois figures
    python scripts/bench/optimize_batch_partition.py analyze
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

try:  # consoles cp1252 : ne pas casser sur les accents
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

# --------------------------------------------------------------------------- #
# Constantes et valeurs par défaut
# --------------------------------------------------------------------------- #

# scripts/bench/<ce fichier> -> racine du dépôt (deux niveaux au-dessus).
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
DEFAULT_CIRCUITS_DIR = os.path.join(REPO_ROOT, "circuits")
# Le jeu de circuits disponibles s'arrête à 8192 : au-delà, `single` devrait
# répéter le plus grand circuit, ce qui sort du compromis qu'on étudie. On
# balaie donc exactement 1..8192, toutes les valeurs entières de N.
DEFAULT_NMAX = 8192
DEFAULT_MAX_SIZE = 8192

PROVERS = ("rapidsnark", "snarkjs")

# Les façons de placer les transactions du batch dans des circuits. `opt` est
# l'optimum exact calculé par programmation dynamique ; les deux autres sont
# les heuristiques de référence auxquelles on le compare.
STRATEGIES = ("opt", "binary", "single")
BASELINES = ("binary", "single")
STRATEGY_LABELS = {
    "opt": "Optimal partition (DP)",
    "binary": "Binary decomposition",
    "single": "Single padded circuit",
}
# Palette Okabe-Ito : discriminable par les daltonismes courants et en niveaux
# de gris. Chaque stratégie porte en plus un style de trait propre, pour rester
# lisible sur une impression noir et blanc.
STRATEGY_COLORS = {
    "opt": "#0072B2",
    "binary": "#D55E00",
    "single": "#009E73",
}
STRATEGY_LINESTYLES = {
    "opt": "-",
    "binary": "--",
    "single": "-.",
}
# L'optimum coïncide souvent avec une baseline : il est dessiné en dernier pour
# rester visible. La légende, elle, garde l'ordre de STRATEGIES.
DRAW_ORDER = ("single", "binary", "opt")

ACCENT = "#0072B2"
ACCENT_ALT = "#D55E00"

# Figure 2 superpose les provers : la couleur y porte le prover, le style de
# trait restant celui de la stratégie (cf. make_time_figure).
PROVER_COLORS = {
    "rapidsnark": "#0072B2",
    "snarkjs": "#CC79A7",
}

# Largeurs de colonne usuelles, en pouces (IEEE / ACM double colonne).
SINGLE_COLUMN_IN = 3.5
DOUBLE_COLUMN_IN = 7.16

Plan = Counter  # {taille_circuit: multiplicité}


# --------------------------------------------------------------------------- #
# Utilitaires de plan
# --------------------------------------------------------------------------- #


def plan_m(plan: Plan) -> int:
    """Nombre de circuits (donc de preuves) du plan."""
    return sum(plan.values())


def plan_capacity(plan: Plan) -> int:
    """Nombre total de slots de transaction ouverts par le plan."""
    return sum(k * c for k, c in plan.items())


def plan_str(plan: Plan) -> str:
    """'1024+32' — forme compacte, tailles décroissantes."""
    parts = []
    for k in sorted(plan, reverse=True):
        c = plan[k]
        parts.append(f"{k}" if c == 1 else f"{c}x{k}")
    return "+".join(parts) if parts else "-"


def parse_plan(spec: str) -> Plan:
    """'1024+16+8+2' ou '2x512+32' -> Counter. Inverse de plan_str."""
    plan: Plan = Counter()
    for token in spec.replace(" ", "").split("+"):
        if not token or token == "-":
            continue
        if "x" in token.lower():
            c, _, k = token.lower().partition("x")
            plan[int(k)] += int(c)
        else:
            plan[int(token)] += 1
    return plan


# --------------------------------------------------------------------------- #
# Modèle de coût
# --------------------------------------------------------------------------- #


@dataclass
class SizeCost:
    """Coûts mesurés pour un circuit d'une taille donnée."""

    size: int
    witness_s: float = 0.0
    prove_s: float = 0.0
    verify_s: float = 0.0
    prove_std_s: float = 0.0
    verify_std_s: float = 0.0
    n_rep: int = 0

    def to_dict(self) -> Dict[str, float]:
        return {
            "witness_s": self.witness_s,
            "prove_s": self.prove_s,
            "verify_s": self.verify_s,
            "prove_std_s": self.prove_std_s,
            "verify_std_s": self.verify_std_s,
            "n_rep": self.n_rep,
        }


@dataclass
class CostModel:
    """Coûts atomiques par prover et par taille de circuit."""

    provers: Dict[str, Dict[int, SizeCost]] = field(default_factory=dict)
    meta: Dict[str, object] = field(default_factory=dict)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        payload = {
            "meta": self.meta,
            "provers": {
                p: {str(k): c.to_dict() for k, c in sorted(sizes.items())}
                for p, sizes in self.provers.items()
            },
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    @staticmethod
    def load(path: str) -> "CostModel":
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        model = CostModel(meta=payload.get("meta", {}))
        for prover, sizes in payload.get("provers", {}).items():
            model.provers[prover] = {
                int(k): SizeCost(size=int(k), **{kk: vv for kk, vv in v.items()})
                for k, v in sizes.items()
            }
        return model

    def sizes(self, prover: str) -> List[int]:
        return sorted(self.provers[prover])

    def available_provers(self) -> List[str]:
        return [p for p in PROVERS if p in self.provers] + [
            p for p in sorted(self.provers) if p not in PROVERS
        ]


@dataclass
class Objective:
    """Temps imputé à une preuve : T(S) = sum c(k_i) + m * alpha."""

    prover: str
    include_witness: bool = True
    include_verify: bool = True
    overhead_s: float = 0.0  # surcoût fixe par preuve, hors vérification

    def seconds(self, cost: SizeCost) -> float:
        """Temps mur imputé à une preuve de cette taille."""
        t = cost.prove_s
        if self.include_witness:
            t += cost.witness_s
        if self.include_verify:
            t += cost.verify_s
        return t + self.overhead_s

    def alpha(self, model: CostModel) -> float:
        """Part du coût unitaire indépendante de la taille du circuit.

        Vérification + surcoût de soumission : c'est ce qu'on paie *par preuve*,
        et donc ce qui pénalise les décompositions fines. Avec l'intercept `a`
        de l'ajustement affine, c'est ce qui forme rho = (a + alpha)/b.
        """
        verif = 0.0
        if self.include_verify:
            costs = model.provers[self.prover]
            vals = [c.verify_s for c in costs.values() if c.verify_s > 0]
            verif = statistics.mean(vals) if vals else 0.0
        return verif + self.overhead_s


def build_cost_table(model: CostModel, obj: Objective) -> Dict[int, float]:
    """Temps d'une preuve, par taille de circuit."""
    return {k: obj.seconds(c) for k, c in model.provers[obj.prover].items()}


# --------------------------------------------------------------------------- #
# Ajustement affine c(k) = a + b*k
# --------------------------------------------------------------------------- #


@dataclass
class AffineFit:
    a: float  # surcoût fixe de preuve (s)
    b: float  # coût marginal par transaction (s/tx)
    r2: float

    def rho(self, alpha: float) -> float:
        """Prix d'une preuve supplémentaire, en transactions vides."""
        return (self.a + alpha) / self.b if self.b > 0 else float("inf")

    def predict(self, k: float) -> float:
        return self.a + self.b * k


def fit_affine(sizes: Sequence[int], values: Sequence[float]) -> AffineFit:
    """Moindres carrés ordinaires sur c(k) = a + b*k (sans dépendance numpy)."""
    n = len(sizes)
    if n < 2:
        return AffineFit(a=values[0] if values else 0.0, b=0.0, r2=0.0)
    mx = statistics.mean(sizes)
    my = statistics.mean(values)
    sxx = sum((x - mx) ** 2 for x in sizes)
    sxy = sum((x - mx) * (y - my) for x, y in zip(sizes, values))
    b = sxy / sxx if sxx > 0 else 0.0
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in values)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(sizes, values))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return AffineFit(a=a, b=b, r2=r2)


# --------------------------------------------------------------------------- #
# Algorithme : programmation dynamique exacte
# --------------------------------------------------------------------------- #


def solve_dp(nmax: int, cost: Dict[int, float]) -> Tuple[List[float], List[int]]:
    """Optimum pour tout n de 0 à nmax.

    f[n] = coût minimal d'un multi-ensemble de circuits couvrant AU MOINS n
    transactions ; choice[n] = une taille de circuit optimale à utiliser en
    premier. Complexité O(nmax * |K|), mémoire O(nmax).

    Exactitude : J est additive et invariante par permutation, donc pour un
    multi-ensemble optimal S couvrant n, retirer n'importe quel k de S laisse un
    multi-ensemble couvrant au moins n-k, d'où la récurrence avec clamp à 0
    (sur-couvrir est gratuit : c'est exactement le padding).
    """
    sizes = sorted(cost)
    f = [0.0] + [math.inf] * nmax
    choice = [0] * (nmax + 1)
    for n in range(1, nmax + 1):
        best = math.inf
        best_k = 0
        for k in sizes:
            prev = f[n - k] if n > k else 0.0
            cand = cost[k] + prev
            if cand < best:
                best = cand
                best_k = k
        f[n] = best
        choice[n] = best_k
    return f, choice


def reconstruct(n: int, choice: Sequence[int]) -> Plan:
    """Plan optimal pour n, à partir de la table de décisions."""
    plan: Plan = Counter()
    cur = n
    while cur > 0:
        k = choice[cur]
        if k <= 0:  # sécurité : ne devrait pas arriver
            break
        plan[k] += 1
        cur = max(0, cur - k)
    return plan


# --------------------------------------------------------------------------- #
# Les stratégies de placement
# --------------------------------------------------------------------------- #


def plan_single(n: int, sizes: Sequence[int]) -> Plan:
    """Un seul circuit, le plus petit qui contienne n, complété par du padding.

    Au-delà de la plus grande taille disponible, on répète cette taille : c'est
    la généralisation naturelle de « une preuve, remplie de vide ».
    """
    for k in sorted(sizes):
        if k >= n:
            return Counter({k: 1})
    kmax = max(sizes)
    return Counter({kmax: math.ceil(n / kmax)})


def plan_binary(n: int, sizes: Sequence[int]) -> Plan:
    """Décomposition gloutonne par tailles décroissantes.

    Sur K = puissances de deux, c'est exactement l'écriture binaire de n (avec
    répétition de la plus grande taille pour la partie haute), c'est-à-dire la
    stratégie « 1024 + 16 + 8 + 2 » de l'énoncé. Le reliquat final est arrondi à
    la plus petite taille disponible qui le couvre.
    """
    plan: Plan = Counter()
    rest = n
    kmin = min(sizes)
    for k in sorted(sizes, reverse=True):
        if rest <= 0:
            break
        if k <= rest:
            c = rest // k
            plan[k] += c
            rest -= c * k
    if rest > 0:  # reliquat plus petit que le plus petit circuit
        plan[kmin] += 1
    return plan


PLANNERS = {"binary": plan_binary, "single": plan_single}


def plan_cost(plan: Plan, cost: Dict[int, float]) -> float:
    return sum(cost[k] * c for k, c in plan.items())


# --------------------------------------------------------------------------- #
# Acquisition des coûts atomiques
# --------------------------------------------------------------------------- #


def discover_sizes(circuits_dir: str) -> List[int]:
    """Tailles de circuit prêtes à l'emploi (zkey + wasm présents)."""
    out: List[int] = []
    if not os.path.isdir(circuits_dir):
        return out
    for entry in sorted(os.listdir(circuits_dir)):
        if not entry.isdigit():
            continue
        d = os.path.join(circuits_dir, entry)
        zkey = os.path.join(d, "circuit_final.zkey")
        wasm = os.path.join(d, "circuit_js", "circuit.wasm")
        if os.path.isfile(zkey) and os.path.isfile(wasm):
            out.append(int(entry))
    return sorted(out)


def _run(cmd: Sequence[str], cwd: str) -> Tuple[int, float]:
    """Exécute une commande, retourne (code retour, durée mur)."""
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True)
        rc = proc.returncode
    except FileNotFoundError:
        rc = 127
    return rc, time.perf_counter() - t0


def _timeit(cmd: Sequence[str], cwd: str, repeat: int, warmup: bool, sleep_s: float,
            label: str) -> Tuple[List[float], bool]:
    """Chronomètre `repeat` exécutions ; retourne (durées, toutes réussies)."""
    ok = True
    if warmup:
        rc, _ = _run(cmd, cwd)
        ok = ok and rc == 0
    times: List[float] = []
    for i in range(repeat):
        rc, dt = _run(cmd, cwd)
        ok = ok and rc == 0
        times.append(dt)
        print(f"      {label}: {dt:8.4f} s  ({i + 1}/{repeat})"
              f"{'' if rc == 0 else f'  [échec rc={rc}]'}")
        if sleep_s > 0:
            time.sleep(sleep_s)
    return times, ok


def measure_costs(args: argparse.Namespace) -> CostModel:
    """Mesure witness / preuve / vérification pour chaque taille disponible."""
    circuits_dir = os.path.abspath(args.circuits_dir)
    sizes = (
        [int(s) for s in args.sizes.split(",")] if args.sizes else discover_sizes(circuits_dir)
    )
    if not sizes:
        raise SystemExit(
            f"aucun circuit exploitable dans {circuits_dir} "
            f"(il faut circuit_final.zkey et circuit_js/circuit.wasm)"
        )

    provers = PROVERS if args.prover == "both" else (args.prover,)
    model = CostModel(
        meta={
            "source": "measure",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "host": platform.node(),
            "platform": platform.platform(),
            "user": getpass.getuser(),
            "repeat": args.repeat,
            "warmup": args.warmup,
            "circuits_dir": circuits_dir,
            "sizes": sizes,
        }
    )
    for p in provers:
        model.provers[p] = {}

    for k in sizes:
        print(f"\n[taille {k}]")
        rel = f"./circuits/{k}"
        wtns_cmd = [
            args.snarkjs, "wtns", "calculate",
            f"{rel}/circuit_js/circuit.wasm", f"{rel}/input.json", f"{rel}/witness.wtns",
        ]
        wt, _ = _timeit(wtns_cmd, REPO_ROOT, args.repeat, args.warmup, args.sleep, "witness")

        verify_cmd = [
            args.snarkjs, "groth16", "verify",
            f"{rel}/verification_key.json", f"{rel}/public.json", f"{rel}/proof.json",
        ]

        for p in provers:
            if p == "snarkjs":
                prove_cmd = [
                    args.snarkjs, "groth16", "prove",
                    f"{rel}/circuit_final.zkey", f"{rel}/witness.wtns",
                    f"{rel}/proof.json", f"{rel}/public.json",
                ]
            else:
                base = args.docker_mount_prefix.rstrip("/")
                prove_cmd = [
                    "docker", "exec", args.docker_container, args.docker_prover_path,
                    f"{base}/{k}/circuit_final.zkey", f"{base}/{k}/witness.wtns",
                    f"{base}/{k}/proof.json", f"{base}/{k}/public.json",
                ]
            pt, pok = _timeit(prove_cmd, REPO_ROOT, args.repeat, args.warmup, args.sleep,
                              f"prove/{p}")
            vt, _ = _timeit(verify_cmd, REPO_ROOT, args.repeat, args.warmup, args.sleep,
                            "verify")
            if not pok:
                print(f"      [attention] au moins une exécution de {p} a échoué pour k={k}")
            model.provers[p][k] = SizeCost(
                size=k,
                witness_s=statistics.median(wt) if wt else 0.0,
                prove_s=statistics.median(pt) if pt else 0.0,
                verify_s=statistics.median(vt) if vt else 0.0,
                prove_std_s=statistics.pstdev(pt) if len(pt) > 1 else 0.0,
                verify_std_s=statistics.pstdev(vt) if len(vt) > 1 else 0.0,
                n_rep=args.repeat,
            )
    return model


def import_phases_csv(path: str) -> CostModel:
    """Convertit un run à `phases.csv` (measure_zk_resources.py) en modèle de coût.

    Une ligne par (n, phase, prover), les phases utiles étant `prove` et
    `verify`. La phase `prove` agrège déjà tous ses steps : pour rapidsnark
    `witness_rapidsnark` + `prove_rapidsnark`, pour snarkjs le `fullprove` qui
    calcule aussi le témoin. Le témoin est donc compris dans `prove_s` pour les
    deux provers, et `witness_s` reste à zéro pour ne pas le compter deux fois.

    La phase `env` (ptau, compilation, setup zkey) est ignorée : c'est un coût
    de mise en place payé une fois par taille de circuit, pas par preuve.
    """
    csv_path = os.path.join(path, "phases.csv")
    with open(csv_path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    def cell(r: Dict[str, str], key: str) -> float:
        try:
            return float(r.get(key) or 0.0)
        except ValueError:
            return 0.0

    model = CostModel(
        meta={
            "source": "import/phases.csv",
            "from": os.path.abspath(path),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "note": "prove_s inclut le calcul du témoin (phase prove de phases.csv)",
        }
    )
    for r in rows:
        prover, phase = r.get("prover") or "", r.get("phase") or ""
        if not prover or phase not in ("prove", "verify"):
            continue
        if (r.get("ok") or "").strip().lower() not in ("true", "1"):
            print(f"  [attention] phase {phase}/{prover} en échec pour n={r['n']}")
        k = int(r["n"])
        sizes = model.provers.setdefault(prover, {})
        c = sizes.setdefault(k, SizeCost(size=k))
        if phase == "prove":
            c.prove_s = cell(r, "wall_s_mean")
            c.prove_std_s = cell(r, "wall_s_stdev")
            c.n_rep = int(cell(r, "n_reps"))
        else:
            c.verify_s = cell(r, "wall_s_mean")
            c.verify_std_s = cell(r, "wall_s_stdev")
    if not model.provers:
        raise SystemExit(f"aucune phase prove/verify exploitable dans {csv_path}")
    return model


def import_run(path: str) -> CostModel:
    """Importe un run de bench, quel que soit son format de sortie."""
    if os.path.isfile(os.path.join(path, "phases.csv")):
        return import_phases_csv(path)
    return import_bench_out(path)


def import_bench_out(path: str) -> CostModel:
    """Convertit un run de generate_proofs.py (bench-out/<ts>) en modèle de coût.

    Les timings de ce script incluent déjà le calcul du témoin dans la mesure du
    prover : on les impute donc entièrement à `prove_s` et on laisse witness_s à
    zéro, pour ne pas compter deux fois.
    """
    raw = os.path.join(path, "raw") if os.path.isdir(os.path.join(path, "raw")) else path

    def load(name: str) -> List[dict]:
        p = os.path.join(raw, name)
        if not os.path.isfile(p):
            return []
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)

    files = {
        "rapidsnark": "rapidsnark_timings.json",
        "snarkjs": "snarkjs_timings.json",
    }
    verify_rows = {int(r["n"]): r for r in load("snarkjs_verify_timings.json")}

    model = CostModel(
        meta={
            "source": "import",
            "from": os.path.abspath(path),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "note": "prove_s inclut le calcul du témoin (mesure de generate_proofs.py)",
        }
    )
    for prover, fname in files.items():
        rows = load(fname)
        if not rows:
            continue
        model.provers[prover] = {}
        for r in rows:
            n = int(r["n"])
            timings = r.get("timings") or []
            v = verify_rows.get(n, {})
            vt = v.get("timings") or []
            model.provers[prover][n] = SizeCost(
                size=n,
                witness_s=0.0,
                prove_s=float(r.get("mean", 0.0)),
                verify_s=float(v.get("mean", 0.0)),
                prove_std_s=statistics.pstdev(timings) if len(timings) > 1 else 0.0,
                verify_std_s=statistics.pstdev(vt) if len(vt) > 1 else 0.0,
                n_rep=len(timings),
            )
    if not model.provers:
        raise SystemExit(f"aucun fichier de timings exploitable sous {raw}")
    return model


# --------------------------------------------------------------------------- #
# Balayage N = 1..nmax
# --------------------------------------------------------------------------- #


def sweep(nmax: int, cost: Dict[int, float],
          choice: Sequence[int]) -> List[Dict[str, object]]:
    """Une ligne par N : pour chaque stratégie, nombre de circuits, temps, padding.

    `choice` est la table de décisions de solve_dp, qui donne l'optimum exact
    pour tout N en O(1) par reconstruction.
    """
    sizes = sorted(cost)
    rows: List[Dict[str, object]] = []
    for n in range(1, nmax + 1):
        row: Dict[str, object] = {"n": n}
        plans = {"opt": reconstruct(n, choice)}
        for name in BASELINES:
            plans[name] = PLANNERS[name](n, sizes)
        for name in STRATEGIES:
            plan = plans[name]
            cap = plan_capacity(plan)
            row[f"{name}_circuits"] = plan_m(plan)
            row[f"{name}_time_s"] = plan_cost(plan, cost)
            row[f"{name}_padding"] = cap - n
            row[f"{name}_padding_ratio"] = (cap - n) / cap if cap else 0.0
            row[f"{name}_plan"] = plan_str(plan)
        # Gain de l'optimum sur chaque heuristique : > 0 = l'heuristique perd.
        c_opt = float(row["opt_time_s"])
        for name in BASELINES:
            base = float(row[f"{name}_time_s"])
            row[f"gain_vs_{name}_pct"] = (base - c_opt) / base * 100.0 if base > 0 else 0.0
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# Diagramme de phase : la loi générale, indépendante de la machine
# --------------------------------------------------------------------------- #


def phase_diagram(
    sizes: Sequence[int], n_grid: Sequence[int], rho_grid: Sequence[float]
) -> Tuple[List[List[int]], List[List[float]]]:
    """Diagramme de phase exact du compromis, en unités de transaction.

    Sous le modèle affine c(k) = a + b*k, minimiser J revient à minimiser
    m*rho + capacité (tout divisé par b), avec rho = (a + alpha)/b. Le coût
    unitaire d'un circuit de taille k vaut donc simplement `rho + k` : le
    diagramme obtenu est indépendant de la machine, et chaque prover s'y place
    par son rho mesuré.

    Retourne (m*, gain relatif de l'optimum sur le circuit unique rempli),
    indexés [ligne = rho, colonne = N].
    """
    nmax = max(n_grid)
    m_mat: List[List[int]] = []
    gain_mat: List[List[float]] = []
    for rho in rho_grid:
        cost = {k: rho + k for k in sizes}
        _, choice = solve_dp(nmax, cost)
        m_row, g_row = [], []
        for n in n_grid:
            plan = reconstruct(n, choice)
            m_row.append(plan_m(plan))
            c_opt = plan_cost(plan, cost)
            c_single = plan_cost(plan_single(n, sizes), cost)
            g_row.append((c_single - c_opt) / c_single * 100.0 if c_single > 0 else 0.0)
        m_mat.append(m_row)
        gain_mat.append(g_row)
    return m_mat, gain_mat


def critical_rho(
    n_grid: Sequence[int], rho_grid: Sequence[float], m_mat: Sequence[Sequence[int]]
) -> List[Optional[float]]:
    """rho*(N) : plus petit rho pour lequel une seule preuve redevient optimale.

    Découper n'est rentable que si rho < rho*(N) : au-delà, la preuve
    supplémentaire coûte plus cher que le padding qu'elle évite.
    """
    out: List[Optional[float]] = []
    for j in range(len(n_grid)):
        val: Optional[float] = None
        for i, rho in enumerate(rho_grid):
            if m_mat[i][j] <= 1:
                val = rho
                break
        out.append(val)
    return out


def write_sweep_csv(path: str, rows: List[Dict[str, object]]) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #


@dataclass
class FigureStyle:
    """Paramètres d'export des figures, pensés pour l'insertion dans un article.

    `width_in` est la largeur *finale* de la figure dans le document : on
    dessine à la taille de publication plutôt que de réduire après coup, pour
    que la taille de police du graphique soit exactement celle demandée
    (typiquement 8–9 pt pour une colonne de 3.5 pouces).
    """

    formats: Tuple[str, ...] = ("png", "pdf")
    dpi: int = 600
    width_in: float = SINGLE_COLUMN_IN
    font_size: float = 8.0
    titles: bool = False  # dans un article, le titre est la légende (caption)


FIG = FigureStyle()


def _figsize(ratio: float = 0.72) -> Tuple[float, float]:
    w = FIG.width_in
    return (w, w * ratio)


def _setup_mpl():
    """rcParams de qualité publication (vecteur, polices à empattement, ticks internes)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    try:
        import scienceplots  # noqa: F401  (enregistre le style "science")

        plt.style.use(["science"])
    except Exception:
        plt.style.use("default")

    s = FIG.font_size
    matplotlib.rcParams.update({
        # Polices : serif pour s'accorder au corps de texte d'un article, et
        # mathtext STIX pour que $N$ et $m$ aient le même dessin que dans le
        # LaTeX environnant. usetex reste désactivé : le script doit tourner
        # sans distribution TeX installée.
        "text.usetex": False,
        "mathtext.fontset": "stix",
        "font.family": "serif",
        "font.serif": ["Times New Roman", "Nimbus Roman", "STIXGeneral",
                       "DejaVu Serif"],
        "font.size": s,
        "axes.titlesize": s + 1,
        "axes.labelsize": s,
        "xtick.labelsize": s - 1,
        "ytick.labelsize": s - 1,
        "legend.fontsize": s - 1,
        # Cadre et graduations
        "axes.linewidth": 0.6,
        "axes.grid": False,
        "xtick.direction": "in",
        "ytick.direction": "in",
        "xtick.top": True,
        "ytick.right": True,
        "xtick.minor.visible": True,
        "ytick.minor.visible": True,
        "xtick.major.width": 0.6,
        "ytick.major.width": 0.6,
        "xtick.minor.width": 0.4,
        "ytick.minor.width": 0.4,
        "xtick.major.size": 3.0,
        "ytick.major.size": 3.0,
        "xtick.minor.size": 1.6,
        "ytick.minor.size": 1.6,
        # Traits et légende
        "lines.linewidth": 1.0,
        "lines.markersize": 3.0,
        "legend.frameon": True,
        "legend.framealpha": 0.92,
        "legend.edgecolor": "0.8",
        "legend.fancybox": False,
        "legend.borderpad": 0.35,
        "legend.handlelength": 2.2,
        "legend.labelspacing": 0.3,
        # Export : polices vectorielles éditables dans le PDF (type 42), texte
        # non converti en courbes, marges serrées.
        "figure.dpi": 150,
        "savefig.dpi": FIG.dpi,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.02,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "svg.fonttype": "none",
        "agg.path.chunksize": 10_000,
    })
    return plt


def _finish(ax, title: str = "", above: bool = False, **legend_kw) -> None:
    """Grille, légende et titre optionnel, de façon homogène sur les deux figures."""
    ax.grid(True, which="major", ls="--", lw=0.35, alpha=0.6)
    ax.grid(True, which="minor", ls=":", lw=0.25, alpha=0.35)
    ax.set_axisbelow(True)
    # Légende toujours dans l'ordre de STRATEGIES, quel que soit l'ordre de tracé.
    handles, labels = ax.get_legend_handles_labels()
    wanted = [STRATEGY_LABELS[s] for s in STRATEGIES]

    def rank(label: str) -> int:  # les libellés lissés portent un suffixe
        for j, w in enumerate(wanted):
            if label.startswith(w):
                return j
        return len(wanted)

    order = sorted(range(len(labels)), key=lambda i: rank(labels[i]))
    handles = [handles[i] for i in order]
    labels = [labels[i] for i in order]
    if above:
        # Au-delà de deux entrées, une légende interne recouvre les courbes quelle
        # que soit sa position : elle sort du cadre, ancrée aux coordonnées de
        # l'axe, et bbox_inches="tight" la réintègre à l'export.
        ax.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.5, 1.01),
                  ncol=2, frameon=False, borderaxespad=0.0,
                  columnspacing=1.4, handletextpad=0.6)
    else:
        ax.legend(handles, labels, **legend_kw)
    if title and FIG.titles:
        ax.set_title(title)


def _save(plt, fig, outdir: str, name: str) -> None:
    for ext in FIG.formats:
        fig.savefig(os.path.join(outdir, f"{name}.{ext}"), dpi=FIG.dpi)
    plt.close(fig)
    print(f"    figure : {name}." + "/.".join(FIG.formats))


def _moving_average(ys: Sequence[float], window: int) -> List[float]:
    """Moyenne glissante causale, fenêtre tronquée au début de la série."""
    if window <= 1:
        return list(ys)
    from collections import deque

    out: List[float] = []
    buf: deque = deque()
    acc = 0.0
    for y in ys:
        buf.append(y)
        acc += y
        if len(buf) > window:
            acc -= buf.popleft()
        out.append(acc / len(buf))
    return out


# Séries en dents de scie par construction, qu'il faut lisser pour rester
# lisibles. La décision dépend de la grandeur tracée, pas seulement de la
# stratégie : m*(N) saute d'un N au suivant alors que le temps de l'optimum,
# lui, est croissant en N (couvrir plus ne peut pas coûter moins).
# `single` est monotone dans les deux cas — sa capacité ne décroît jamais — et
# les micro-inversions de son temps mesuré (le coût de k=4 dépasse celui de
# k=8 de 2 ms chez rapidsnark) sont du bruit de mesure, pas des dents de scie.
JAGGED_CIRCUITS = ("opt", "binary")
JAGGED_TIME = ("binary",)


def _plot_series(ax, ns, ys, label, color, ls, window, jagged, steps=False) -> None:
    """Trace une série, en la lissant si elle est en dents de scie.

    Pour la décomposition binaire, m(N) = popcount(N) saute entre 1 et 13 d'un N
    au suivant : tracer les 8192 valeurs brutes donne un aplat illisible. Les
    valeurs exactes restent dessinées en trait fin (c'est l'information, pas du
    bruit de mesure), et une moyenne glissante porte la tendance lisible.
    """
    style = dict(color=color, ls=ls)
    if steps:
        style["drawstyle"] = "steps-post"
    if not jagged or window <= 1:
        ax.plot(ns, ys, lw=1.0, zorder=3, label=label, **style)
        return
    ax.plot(ns, ys, lw=0.2, alpha=0.22, zorder=1,
            **{k: v for k, v in style.items() if k != "ls"})
    ax.plot(ns, _moving_average(ys, window), lw=1.1, zorder=2, label=label, **style)


def make_time_figure(outdir: str, rows_by_prover: Dict[str, List[Dict[str, object]]],
                     logx: bool = True, window: int = 101) -> None:
    """Figure 2 : N -> temps total, tous provers sur le même panneau.

    Deux clés de lecture indépendantes : la couleur porte le prover, le style
    de trait porte la stratégie — le même style de trait que dans la figure 1,
    pour que « pointillé = décomposition binaire » reste vrai d'une figure à
    l'autre. Les deux provers étant séparés d'un facteur ~5 en temps absolu,
    ils forment deux bandes nettes sur l'axe logarithmique, et la comparaison
    des stratégies se lit à l'intérieur de chaque bande.
    """
    plt = _setup_mpl()
    fig, ax = plt.subplots(figsize=_figsize())
    for prover, rows in rows_by_prover.items():
        ns = [int(r["n"]) for r in rows]
        for s in DRAW_ORDER:
            _plot_series(ax, ns, [float(r[f"{s}_time_s"]) for r in rows],
                         f"{prover} — {s}",
                         PROVER_COLORS.get(prover, ACCENT), STRATEGY_LINESTYLES[s],
                         window, s in JAGGED_TIME)
    ax.set_xlabel(r"Batch size $N$ (transactions)")
    ax.set_ylabel("Total proving time (s)")
    if logx:
        ax.set_xscale("log")
        ax.set_yscale("log")
    _finish(ax, title="Time to prove $N$ transactions",
            above=len(rows_by_prover) > 1, ncol=len(rows_by_prover),
            loc="upper left")
    _save(plt, fig, outdir, "01_time_vs_n")


def make_decomposition_map(outdir: str, rows: List[Dict[str, object]],
                           prover: str, logx: bool = True) -> None:
    """Figure 4 : quelles tailles de circuit l'optimum retient, pour chaque N.

    Un point par circuit du plan optimal ; l'aire du marqueur porte la
    multiplicité. C'est la réponse littérale à « pour ce N, quelle
    décomposition ». Lisible parce que m* reste petit : quelques circuits au
    plus, jamais l'écriture binaire complète.
    """
    plt = _setup_mpl()
    xs: List[int] = []
    ys: List[int] = []
    mult: List[float] = []
    for r in rows:
        n = int(r["n"])
        for k, c in parse_plan(str(r["opt_plan"])).items():
            xs.append(n)
            ys.append(k)
            mult.append(c)
    fig, ax = plt.subplots(figsize=_figsize())
    sc = ax.scatter(xs, ys, s=[2.0 * m for m in mult], c=mult, cmap="viridis",
                    vmin=1, vmax=max(mult) if mult else 1, linewidths=0,
                    rasterized=True)
    ax.set_xlabel(r"Batch size $N$ (transactions)")
    ax.set_ylabel(r"Circuit size $k$ used by the optimum")
    if logx:
        ax.set_xscale("log")
    ax.set_yscale("log", base=2)
    ax.grid(True, which="major", ls="--", lw=0.35, alpha=0.6)
    ax.set_axisbelow(True)
    if max(mult) > 1:
        cb = fig.colorbar(sc, ax=ax, pad=0.02,
                          ticks=list(range(1, int(max(mult)) + 1)))
        cb.set_label("Multiplicity", fontsize=FIG.font_size)
        cb.outline.set_linewidth(0.6)
    if FIG.titles:
        ax.set_title(f"Optimal decomposition of a batch of $N$ ({prover})")
    _save(plt, fig, outdir, f"02_decomposition_map_{prover}")


def make_critical_rho_figure(outdir: str, sizes: Sequence[int],
                             measured_rho: Dict[str, float], nmax: int,
                             n_points: int = 90, rho_points: int = 70) -> Dict[str, object]:
    """Figure 3 : rho*(N), le seuil au-delà duquel une seule preuve redevient optimale.

    C'est la règle de décision sous forme machine-indépendante. Sous le modèle
    affine, tout l'optimum ne dépend que de rho = (a+alpha)/b, le prix d'une
    preuve exprimé en transactions vides. Un prover est donc une simple
    horizontale : partout où la courbe passe au-dessus de sa ligne, découper le
    batch est rentable ; partout où elle passe en dessous, un seul circuit padé
    est optimal.
    """
    plt = _setup_mpl()
    import matplotlib.patheffects as pe

    n_grid = sorted({max(1, round(10 ** (i / (n_points - 1) * math.log10(nmax))))
                     for i in range(n_points)})
    rho_grid = [10 ** (i / (rho_points - 1) * 4.0) for i in range(rho_points)]  # 1 -> 1e4
    m_mat, _ = phase_diagram(sizes, n_grid, rho_grid)
    crit = critical_rho(n_grid, rho_grid, m_mat)

    xs = [n for n, c in zip(n_grid, crit) if c is not None]
    ys = [c for c in crit if c is not None]
    if xs:
        fig, ax = plt.subplots(figsize=_figsize())
        ax.step(xs, ys, where="post", lw=1.2, color=ACCENT)
        ax.fill_between(xs, ys, 1.0, step="post", alpha=0.12, color=ACCENT, lw=0)
        # Les deux régimes sont nommés dans la zone qu'ils désignent ; liseré
        # blanc pour rester lisibles là où ils croisent l'escalier.
        halo = [pe.withStroke(linewidth=2.0, foreground="white")]
        ax.text(0.5, 0.10, "partitioning pays", fontsize=FIG.font_size - 1,
                color=ACCENT, transform=ax.transAxes, ha="center",
                zorder=6, path_effects=halo)
        ax.text(0.04, 0.95, "padding one circuit pays", fontsize=FIG.font_size - 1,
                color="0.25", transform=ax.transAxes, va="top",
                zorder=6, path_effects=halo)
        for p, r in sorted(measured_rho.items(), key=lambda t: t[1]):
            ax.axhline(r, ls="--", lw=0.9, color=PROVER_COLORS.get(p, ACCENT_ALT))
            ax.text(xs[-1], r * 1.08, f"{p} ($\\rho={r:.0f}$)", ha="right",
                    fontsize=FIG.font_size - 1.5,
                    color=PROVER_COLORS.get(p, ACCENT_ALT),
                    zorder=6, path_effects=halo)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel(r"Batch size $N$ (transactions)")
        ax.set_ylabel(r"Critical overhead $\rho^*(N)$ (tx-equiv.)")
        ax.grid(True, which="major", ls="--", lw=0.35, alpha=0.6)
        ax.set_axisbelow(True)
        if FIG.titles:
            ax.set_title("When is it worth splitting a batch into several proofs?")
        _save(plt, fig, outdir, "03_critical_rho")

    return {
        "n_grid": n_grid,
        "critical_rho_tx": {n: c for n, c in zip(n_grid, crit)},
        "measured_rho_tx": measured_rho,
    }


# --------------------------------------------------------------------------- #
# Commandes
# --------------------------------------------------------------------------- #


def cmd_measure(args: argparse.Namespace) -> None:
    model = measure_costs(args)
    model.save(args.out)
    print(f"\n>>> modèle de coût écrit dans {args.out}")


def cmd_import(args: argparse.Namespace) -> None:
    model = import_run(getattr(args, "from"))
    model.save(args.out)
    for p in model.available_provers():
        print(f"  {p:<12} {len(model.provers[p])} tailles : {model.sizes(p)}")
    print(f"\n>>> modèle de coût écrit dans {args.out}")


def configure_figures(args: argparse.Namespace) -> None:
    """Applique les options d'export de figures passées en ligne de commande."""
    global FIG
    width = {"single": SINGLE_COLUMN_IN, "double": DOUBLE_COLUMN_IN}.get(
        args.fig_width, None
    )
    if width is None:  # largeur explicite en pouces
        width = float(args.fig_width)
    FIG = FigureStyle(
        formats=tuple(dict.fromkeys(args.fig_format)),
        dpi=args.fig_dpi,
        width_in=width,
        font_size=args.fig_font_size,
        titles=args.fig_titles,
    )


def cmd_analyze(args: argparse.Namespace) -> None:
    configure_figures(args)
    model = CostModel.load(args.costs)
    provers = model.available_provers() if args.prover == "both" else [args.prover]
    os.makedirs(args.outdir, exist_ok=True)
    rows_by_prover: Dict[str, List[Dict[str, object]]] = {}
    measured_rho: Dict[str, float] = {}
    all_sizes: List[int] = []

    for prover in provers:
        if prover not in model.provers:
            print(f"[ignoré] aucun coût mesuré pour {prover}")
            continue
        obj = Objective(
            prover=prover,
            include_witness=not args.no_witness,
            include_verify=not args.no_verify,
            overhead_s=args.overhead_per_proof,
        )
        cost = build_cost_table(model, obj)
        cost = {k: v for k, v in cost.items()
                if k <= args.max_size and (k & (k - 1)) == 0}
        if not cost:
            raise SystemExit(
                f"aucune taille de circuit <= {args.max_size} dans {args.costs}"
            )
        sizes = sorted(cost)
        all_sizes = sizes
        print(f"\n=== {prover} ===")
        print(f"  tailles de circuit : {sizes}")
        print("  coût d'une preuve  : " +
              ", ".join(f"{k}:{cost[k]:.3f}s" for k in sizes))

        # Ajustement affine et rho : le prix d'une preuve, en transactions
        # vides. C'est le seul paramètre dont dépend l'optimum sous ce modèle,
        # et c'est par lui que le prover se place dans le diagramme de phase.
        # L'ajustement porte sur le coût de PREUVE seul : obj.seconds() inclut
        # déjà la vérification, donc ajuster dessus mettrait alpha dans
        # l'intercept a, et rho = (a + alpha)/b le compterait deux fois.
        alpha = obj.alpha(model)
        fit = fit_affine(
            sizes, [obj.seconds(model.provers[prover][k]) - alpha for k in sizes]
        )
        rho = fit.rho(alpha)
        measured_rho[prover] = rho
        print(f"  c(k) = a + b*k : a={fit.a:.3f}s  b={fit.b * 1e3:.4g}ms/tx  "
              f"R²={fit.r2:.4f}")
        print(f"  rho = (a+alpha)/b = {rho:.0f} tx  "
              f"(une preuve de plus coûte autant que {rho:.0f} slots vides)")

        _, choice = solve_dp(args.nmax, cost)
        rows = sweep(args.nmax, cost, choice)
        rows_by_prover[prover] = rows
        csv_path = os.path.join(args.outdir, f"sweep_{prover}.csv")
        write_sweep_csv(csv_path, rows)
        print(f"  balayage N=1..{args.nmax} : {os.path.relpath(csv_path, REPO_ROOT)}")

        # Repères de lecture : l'optimum est le seul plafond honnête, on dit
        # donc sur quelle part du domaine chaque heuristique s'en écarte.
        ms = Counter(int(r["opt_circuits"]) for r in rows)
        print("  m* de l'optimum : " + ", ".join(
            f"m={m} sur {c} N ({c / len(rows) * 100:.1f} %)" for m, c in sorted(ms.items())))
        both = sum(1 for r in rows
                   if all(float(r[f"gain_vs_{b}_pct"]) > 1e-9 for b in BASELINES))
        print(f"  optimum strictement meilleur que les DEUX heuristiques sur "
              f"{both}/{len(rows)} N ({both / len(rows) * 100:.1f} %)")
        for b in BASELINES:
            g = [float(r[f"gain_vs_{b}_pct"]) for r in rows]
            beat = sum(1 for x in g if x > 1e-9)
            top = max(range(len(g)), key=lambda i: g[i])
            print(f"    vs {b:<7} : bat sur {beat}/{len(rows)} N, "
                  f"gain max {g[top]:.1f} % à N={rows[top]['n']}")
        for n in args.show_n:
            if 1 <= n <= args.nmax:
                r = rows[n - 1]
                print(f"    N={n:<6} opt {r['opt_plan']:<20} m={r['opt_circuits']:<2} "
                      f"{float(r['opt_time_s']):7.2f}s | "
                      f"bin {r['binary_plan']:<26} {float(r['binary_time_s']):7.2f}s | "
                      f"sgl {r['single_plan']:<8} {float(r['single_time_s']):7.2f}s")

    if not rows_by_prover:
        raise SystemExit(f"aucun prover exploitable dans {args.costs}")

    if not args.no_figures:
        print("\n=== figures ===")
        logx = not args.linear_x
        make_time_figure(args.outdir, rows_by_prover, logx=logx, window=args.smooth)
        for prover, rows in rows_by_prover.items():
            make_decomposition_map(args.outdir, rows, prover, logx=logx)
        if not args.no_phase:
            phase = make_critical_rho_figure(args.outdir, all_sizes, measured_rho,
                                             nmax=args.nmax)
            path = os.path.join(args.outdir, "critical_rho.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(phase, f, indent=2, default=str)
            print(f"    seuils rho*(N) : {os.path.relpath(path, REPO_ROOT)}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def add_objective_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("fonction de coût")
    g.add_argument("--no-witness", action="store_true",
                   help="exclut le calcul du témoin du coût de preuve")
    g.add_argument("--no-verify", action="store_true",
                   help="exclut la vérification (régime prover seul, sans règlement)")
    g.add_argument("--overhead-per-proof", type=float, default=0.0, metavar="S",
                   help="surcoût fixe supplémentaire par preuve, en secondes "
                        "(soumission, agrégation, latence réseau)")


def add_figure_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group(
        "figures (qualité publication)",
        "Les figures sont dessinées à leur taille finale dans l'article : la police "
        "vue par le lecteur est exactement --fig-font-size, sans réduction ultérieure.",
    )
    g.add_argument("--fig-format", nargs="+", default=["png", "pdf"], metavar="EXT",
                   help="formats d'export (défaut : png pdf ; svg, eps possibles)")
    g.add_argument("--fig-dpi", type=int, default=600,
                   help="résolution du PNG (défaut 600 : seuil usuel des éditeurs)")
    g.add_argument("--fig-width", default="single", metavar="W",
                   help="largeur finale : 'single' (3.5 in), 'double' (7.16 in) "
                        "ou une valeur en pouces")
    g.add_argument("--fig-font-size", type=float, default=8.0, metavar="PT",
                   help="taille de police de base, en points (défaut 8)")
    g.add_argument("--fig-titles", action="store_true",
                   help="grave le titre dans la figure (par défaut il n'est "
                        "que dans la légende LaTeX, comme le veut l'usage)")
    g.add_argument("--linear-x", action="store_true",
                   help="axe des abscisses linéaire (défaut : logarithmique)")
    g.add_argument("--smooth", type=int, default=101, metavar="W",
                   help="fenêtre de la moyenne glissante appliquée aux courbes en "
                        "dents de scie (défaut 101 ; 1 = valeurs brutes seules)")


def add_exec_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("exécution")
    g.add_argument("--snarkjs", default="snarkjs")
    g.add_argument("--circuits-dir", default=DEFAULT_CIRCUITS_DIR)
    g.add_argument("--docker-container", default="debian_rapidsnark")
    g.add_argument("--docker-prover-path", default="mnt/projet/rapidsnark/package/bin/prover")
    g.add_argument("--docker-mount-prefix", default="mnt/projet")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="optimize_batch_partition.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # -- measure ----------------------------------------------------------- #
    m = sub.add_parser("measure", help="mesure les coûts atomiques par taille de circuit")
    m.add_argument("--out", default="bench-out/partition/costs.json")
    m.add_argument("--sizes", default="", help="liste explicite, ex. 1,2,4,8 (défaut : auto)")
    m.add_argument("--prover", choices=("rapidsnark", "snarkjs", "both"), default="both")
    m.add_argument("--repeat", type=int, default=10)
    m.add_argument("--sleep", type=float, default=0.0, help="pause entre exécutions (s)")
    m.add_argument("--warmup", action="store_true", default=True)
    m.add_argument("--no-warmup", dest="warmup", action="store_false")
    add_exec_args(m)
    m.set_defaults(func=cmd_measure)

    # -- import ------------------------------------------------------------ #
    i = sub.add_parser("import", help="convertit un run bench-out/<ts> en modèle de coût")
    i.add_argument("--from", required=True,
                   help="dossier bench-out/<timestamp> ; format phases.csv "
                        "(measure_zk_resources.py) ou raw/*_timings.json "
                        "(generate_proofs.py), détecté automatiquement")
    i.add_argument("--out", default="bench-out/partition/costs.json")
    i.set_defaults(func=cmd_import)

    # -- analyze ----------------------------------------------------------- #
    a = sub.add_parser("analyze", help="balayage N=1..nmax et les deux figures")
    a.add_argument("--costs", default="bench-out/partition/costs.json")
    a.add_argument("--outdir", default="bench-out/partition")
    a.add_argument("--nmax", type=int, default=DEFAULT_NMAX,
                   help=f"N balayé de 1 à NMAX, toutes les valeurs entières "
                        f"(défaut {DEFAULT_NMAX})")
    a.add_argument("--max-size", type=int, default=DEFAULT_MAX_SIZE, metavar="K",
                   help=f"plus grand circuit autorisé ; seules les puissances de "
                        f"deux sont retenues (défaut {DEFAULT_MAX_SIZE})")
    a.add_argument("--prover", choices=("rapidsnark", "snarkjs", "both"), default="both")
    a.add_argument("--show-n", type=int, nargs="+", default=[1050, 5000, 10000],
                   metavar="N", help="valeurs de N détaillées dans la sortie console")
    a.add_argument("--no-figures", action="store_true")
    a.add_argument("--no-phase", action="store_true",
                   help="saute le diagramme de phase (le plus long : une DP "
                        "complète par valeur de rho de la grille)")
    add_figure_args(a)
    add_objective_args(a)
    a.set_defaults(func=cmd_analyze)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
