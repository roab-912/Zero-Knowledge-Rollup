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

Le script compare ces deux stratégies, et rien d'autre. Le résultat n'est pas
qu'une stratégie gagne : AUCUNE DES DEUX NE DOMINE L'AUTRE.

  * `single` gagne dans le gros du domaine, parce qu'une preuve de plus coûte
    cher : la vérification est constante, indépendante de la taille du circuit.
  * `binary` gagne juste après chaque puissance de deux, là où `single` doit
    sauter à la taille supérieure et payer près de 50 % de padding.

Le vainqueur change donc des dizaines de fois quand N parcourt 1..8192, et la
frontière dépend du prover. Trois figures :

  01_time_vs_n          coût réel des deux stratégies, par prover
  02_padding            la part du batch remplie de transactions vides
  03_binary_vs_single   le rapport des deux temps : qui gagne, où, de combien

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

# Les deux façons de placer les transactions du batch dans des circuits.
# Aucune ne domine l'autre : c'est tout l'objet du script.
STRATEGIES = ("binary", "single")
STRATEGY_LABELS = {
    "binary": "Binary decomposition",
    "single": "Single padded circuit",
}
# Palette Okabe-Ito : discriminable par les daltonismes courants et en niveaux
# de gris. Chaque stratégie porte en plus un style de trait propre, pour rester
# lisible sur une impression noir et blanc.
STRATEGY_COLORS = {
    "binary": "#D55E00",
    "single": "#009E73",
}
STRATEGY_LINESTYLES = {
    "binary": "--",
    "single": "-",
}
# `binary` est en dents de scie et occupe une bande large : on la dessine en
# premier pour que l'escalier de `single` reste lisible par-dessus.
DRAW_ORDER = ("binary", "single")

ACCENT = "#0072B2"
ACCENT_ALT = "#D55E00"

# Les deux figures superposent les provers : la couleur y porte le prover, le
# style de trait restant celui de la stratégie (cf. make_time_figure).
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


def sweep(nmax: int, cost: Dict[int, float]) -> List[Dict[str, object]]:
    """Une ligne par N : pour chaque stratégie, nombre de circuits, temps, padding.

    Les deux dernières colonnes portent le résultat. `binary_vs_single_pct` est
    le gain de `binary` sur `single` : positif quand `binary` gagne, négatif
    quand `single` gagne. Il change de signe des dizaines de fois sur le
    domaine, et `winner` nomme le vainqueur pour chaque N.
    """
    sizes = sorted(cost)
    rows: List[Dict[str, object]] = []
    for n in range(1, nmax + 1):
        row: Dict[str, object] = {"n": n}
        for name in STRATEGIES:
            plan = PLANNERS[name](n, sizes)
            cap = plan_capacity(plan)
            row[f"{name}_circuits"] = plan_m(plan)
            row[f"{name}_time_s"] = plan_cost(plan, cost)
            row[f"{name}_padding"] = cap - n
            row[f"{name}_padding_ratio"] = (cap - n) / cap if cap else 0.0
            row[f"{name}_plan"] = plan_str(plan)
        t_bin = float(row["binary_time_s"])
        t_sgl = float(row["single_time_s"])
        row["binary_vs_single_pct"] = (
            (t_sgl - t_bin) / t_sgl * 100.0 if t_sgl > 0 else 0.0
        )
        row["winner"] = ("binary" if t_bin < t_sgl
                         else "single" if t_sgl < t_bin else "tie")
        rows.append(row)
    return rows


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


# `binary` est en dents de scie par construction : m(N) = popcount(N) saute
# entre 1 et 13 d'un N au suivant, et son temps suit. Tracer les 8192 valeurs
# brutes donne un aplat illisible, d'où la moyenne glissante de la figure 1.
# `single` est monotone — sa capacité ne décroît jamais — et les
# micro-inversions de son temps mesuré (le coût de k=4 dépasse celui de k=8 de
# 2 ms chez rapidsnark) sont du bruit de mesure, pas des dents de scie.
#
# Attention : le lissage est réservé à la figure 1, où l'on compare des ordres
# de grandeur. La figure 3 trace le rapport brut, car c'est justement dans les
# dents de scie que `binary` passe devant.
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
    """Figure 1 : N -> temps total, les deux stratégies et tous les provers.

    Deux clés de lecture indépendantes : la couleur porte le prover, le style
    de trait porte la stratégie (plein = `single`, tirets = `binary`). Les deux
    provers étant séparés d'un facteur ~5 en temps absolu, ils forment deux
    bandes nettes sur l'axe logarithmique, et la comparaison des stratégies se
    lit à l'intérieur de chaque bande.

    Cette figure donne les ordres de grandeur, pas le verdict : la moyenne
    glissante de `binary` masque les N où elle passe devant `single`. C'est la
    figure 3 qui tranche.
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


def make_padding_figure(outdir: str, rows: List[Dict[str, object]],
                        logx: bool = True) -> None:
    """Figure 2 : la part du batch occupée par des transactions vides.

    C'est le mécanisme derrière tout le reste. `single` doit arrondir N à la
    puissance de deux supérieure : son taux de padding retombe à 0 chaque fois
    que N tombe pile sur une taille de circuit, bondit à près de 50 % à la
    transaction suivante, puis redescend à mesure que le circuit se remplit.
    `binary`, sur des puissances de deux, couvre N exactement : zéro slot vide,
    partout. D'où les deux allures opposées, dents de scie contre plat.

    Figure prover-indépendante : le padding ne dépend que de N et du jeu de
    tailles. Ce qu'il COÛTE, lui, dépend du prover — c'est la figure 3.

    Ici la couleur porte la stratégie (il n'y a pas de dimension prover), alors
    que les figures 1 et 3 s'en servent pour le prover ; le style de trait,
    plein pour `single` et tireté pour `binary`, reste le repère commun.
    """
    plt = _setup_mpl()
    fig, ax = plt.subplots(figsize=_figsize())
    ns = [int(r["n"]) for r in rows]
    for name in DRAW_ORDER:
        ys = [float(r[f"{name}_padding_ratio"]) * 100.0 for r in rows]
        ax.plot(ns, ys, lw=1.0, color=STRATEGY_COLORS[name],
                ls=STRATEGY_LINESTYLES[name], label=STRATEGY_LABELS[name],
                zorder=3)
        if name == "single":  # l'aire, c'est littéralement le vide qu'on prouve
            ax.fill_between(ns, ys, 0.0, color=STRATEGY_COLORS[name],
                            alpha=0.15, lw=0, zorder=2)

    mean_pad = statistics.mean(
        float(r["single_padding_ratio"]) for r in rows) * 100.0
    ax.axhline(mean_pad, ls=":", lw=0.9, color="0.3", zorder=4)
    ax.text(ns[-1], mean_pad + 1.4, f"single: {mean_pad:.0f} % on average",
            fontsize=FIG.font_size - 1, color="0.3", ha="right", va="bottom",
            zorder=5)

    if logx:
        ax.set_xscale("log")
    # Un cran sous zéro : sinon la ligne plate de `binary` se confond avec
    # l'axe, et c'est précisément le résultat qu'on veut voir.
    ax.set_ylim(-2.5, 53.0)
    ax.set_yticks([0, 10, 20, 30, 40, 50])
    ax.set_xlabel(r"Batch size $N$ (transactions)")
    ax.set_ylabel("Empty slots in the batch (%)")
    # Légende hors cadre : à l'intérieur, elle recouvre le sommet des dents
    # entre N=9 et N=60, qui est justement ce qu'on veut lire.
    _finish(ax, title="How much of each batch is padding?", above=True, ncol=2)
    _save(plt, fig, outdir, "02_padding")


def make_ratio_figure(outdir: str, rows_by_prover: Dict[str, List[Dict[str, object]]],
                      logx: bool = True) -> None:
    """Figure 3 : le rapport des deux temps — la figure qui tranche.

    On trace T_binary / T_single. Au-dessus de 1, `single` gagne ; en dessous,
    `binary` gagne. La courbe traverse la ligne des dizaines de fois : elle
    plonge juste après chaque puissance de deux — là où `single` doit sauter à
    la taille supérieure et payer près de 50 % de padding — puis remonte à
    mesure que le batch remplit ce circuit, jusqu'au creux suivant.

    Aucun lissage ici, contrairement à la figure 1 : les plongeons SONT
    l'information. Une moyenne glissante les effacerait et donnerait
    l'illusion que `single` domine partout.
    """
    plt = _setup_mpl()
    fig, ax = plt.subplots(figsize=_figsize())
    lo, hi = 1.0, 1.0
    for prover, rows in rows_by_prover.items():
        ns = [int(r["n"]) for r in rows]
        ratio = [float(r["binary_time_s"]) / float(r["single_time_s"])
                 if float(r["single_time_s"]) > 0 else 1.0 for r in rows]
        lo, hi = min(lo, min(ratio)), max(hi, max(ratio))
        color = PROVER_COLORS.get(prover, ACCENT)
        ax.plot(ns, ratio, lw=0.5, color=color, label=prover, zorder=3)
        # Seule la partie sous la ligne est remplie : c'est la zone à montrer.
        ax.fill_between(ns, ratio, 1.0, where=[r < 1.0 for r in ratio],
                        color=color, alpha=0.25, lw=0, zorder=2)
    ax.axhline(1.0, ls=":", lw=1.0, color="0.2", zorder=4)

    # Les deux étiquettes sont ancrées à la ligne de décision, pas aux bords du
    # cadre : c'est elle qui sépare les deux régimes.
    ax.text(1.25, 1.10, "single wins", fontsize=FIG.font_size - 1, color="0.25",
            va="bottom", ha="left", zorder=5)
    ax.text(1.25, 0.91, "binary wins", fontsize=FIG.font_size - 1, color="0.25",
            va="top", ha="left", zorder=5)

    if logx:
        ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_ylim(lo * 0.85, hi * 1.3)
    # Sur une décennie incomplete, matplotlib ne graduerait que 10^0 : on force
    # des repères en facteurs, seule échelle qui parle ici ("2x plus lent").
    ticks = [t for t in (0.25, 0.5, 1, 2, 4, 8, 16)
             if lo * 0.85 <= t <= hi * 1.3]
    ax.set_yticks(ticks)
    ax.set_yticklabels([("%g" % t) if t < 1 else ("%g$\\times$" % t)
                        for t in ticks])
    ax.minorticks_off()
    ax.set_xlabel(r"Batch size $N$ (transactions)")
    ax.set_ylabel(r"$T_{\mathrm{binary}} / T_{\mathrm{single}}$")
    _finish(ax, title="Neither strategy dominates the other",
            above=len(rows_by_prover) > 1, ncol=len(rows_by_prover),
            loc="upper right")
    _save(plt, fig, outdir, "03_binary_vs_single")


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
        print(f"\n=== {prover} ===")
        print(f"  tailles de circuit : {sizes}")
        print("  coût d'une preuve  : " +
              ", ".join(f"{k}:{cost[k]:.3f}s" for k in sizes))

        # Ajustement affine et rho : le prix d'une preuve, en transactions
        # vides. C'est le chiffre qui explique l'arbitrage : `binary` ne gagne
        # que si les (m-1) preuves qu'elle ajoute, à rho chacune, coûtent moins
        # que le padding que `single` aurait payé.
        # L'ajustement porte sur le coût de PREUVE seul : obj.seconds() inclut
        # déjà la vérification, donc ajuster dessus mettrait alpha dans
        # l'intercept a, et rho = (a + alpha)/b le compterait deux fois.
        alpha = obj.alpha(model)
        fit = fit_affine(
            sizes, [obj.seconds(model.provers[prover][k]) - alpha for k in sizes]
        )
        rho = fit.rho(alpha)
        print(f"  c(k) = a + b*k : a={fit.a:.3f}s  b={fit.b * 1e3:.4g}ms/tx  "
              f"R²={fit.r2:.4f}")
        print(f"  rho = (a+alpha)/b = {rho:.0f} tx  "
              f"(une preuve de plus coûte autant que {rho:.0f} slots vides)")

        rows = sweep(args.nmax, cost)
        rows_by_prover[prover] = rows
        csv_path = os.path.join(args.outdir, f"sweep_{prover}.csv")
        write_sweep_csv(csv_path, rows)
        print(f"  balayage N=1..{args.nmax} : {os.path.relpath(csv_path, REPO_ROOT)}")

        # Le verdict : qui gagne, sur quelle part du domaine, et combien de
        # fois le vainqueur change quand N avance d'une seule transaction.
        nb = len(rows)
        wins = Counter(str(r["winner"]) for r in rows)
        print("  vainqueur : " + ", ".join(
            f"{w} sur {c} N ({c / nb * 100:.1f} %)"
            for w, c in wins.most_common()))
        flips = sum(1 for a, b in zip(rows, rows[1:])
                    if a["winner"] != b["winner"])
        verdict = ("-> aucune des deux stratégies ne domine l'autre"
                   if wins.get("binary") and wins.get("single")
                   else "(une seule stratégie gagne sur ce domaine)")
        print(f"  le vainqueur change {flips} fois sur N=1..{args.nmax} {verdict}")
        pads = [float(r["single_padding_ratio"]) for r in rows]
        print(f"  slots vides chez single : {statistics.mean(pads) * 100:.1f} % "
              f"en moyenne, jusqu'à {max(pads) * 100:.1f} % "
              f"(binary : 0 partout)")
        g = [float(r["binary_vs_single_pct"]) for r in rows]
        i_bin = max(range(nb), key=lambda i: g[i])
        i_sgl = min(range(nb), key=lambda i: g[i])
        if g[i_bin] > 1e-9:
            print(f"    binary au mieux : {g[i_bin]:+.1f} % à N={rows[i_bin]['n']} "
                  f"(juste après une puissance de deux)")
        else:
            print(f"    binary ne gagne sur aucun N <= {args.nmax}")
        r_sgl = rows[i_sgl]
        factor = (float(r_sgl["binary_time_s"]) / float(r_sgl["single_time_s"])
                  if float(r_sgl["single_time_s"]) > 0 else float("inf"))
        print(f"    single au mieux : binary est {factor:.1f}x plus lent "
              f"à N={r_sgl['n']} (juste avant une puissance de deux)")
        for n in args.show_n:
            if 1 <= n <= args.nmax:
                r = rows[n - 1]
                print(f"    N={n:<6} bin {r['binary_plan']:<26} "
                      f"m={r['binary_circuits']:<2} {float(r['binary_time_s']):7.2f}s | "
                      f"sgl {r['single_plan']:<8} {float(r['single_time_s']):7.2f}s "
                      f"(padding {r['single_padding']:<5}) -> {r['winner']}")

    if not rows_by_prover:
        raise SystemExit(f"aucun prover exploitable dans {args.costs}")

    if not args.no_figures:
        print("\n=== figures ===")
        logx = not args.linear_x
        make_time_figure(args.outdir, rows_by_prover, logx=logx, window=args.smooth)
        make_padding_figure(args.outdir, next(iter(rows_by_prover.values())),
                            logx=logx)
        make_ratio_figure(args.outdir, rows_by_prover, logx=logx)


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
    a = sub.add_parser("analyze",
                       help="balayage N=1..nmax, binary contre single, et les figures")
    a.add_argument("--costs", default="bench-out/partition/costs.json")
    a.add_argument("--outdir", default="bench-out/partition")
    a.add_argument("--nmax", type=int, default=DEFAULT_NMAX,
                   help=f"N balayé de 1 à NMAX, toutes les valeurs entières "
                        f"(défaut {DEFAULT_NMAX})")
    a.add_argument("--max-size", type=int, default=DEFAULT_MAX_SIZE, metavar="K",
                   help=f"plus grand circuit autorisé ; seules les puissances de "
                        f"deux sont retenues (défaut {DEFAULT_MAX_SIZE})")
    a.add_argument("--prover", choices=("rapidsnark", "snarkjs", "both"), default="both")
    a.add_argument("--show-n", type=int, nargs="+", default=[1050, 2049, 5000],
                   metavar="N", help="valeurs de N détaillées dans la sortie console "
                                     "(2049 : le premier N où binary passe devant)")
    a.add_argument("--no-figures", action="store_true")
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
