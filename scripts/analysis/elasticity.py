#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
elasticity.py

Loi d'amortissement du batching : quand augmenter N fait-il baisser le coût
moyen par transaction, et quand devient-ce contre-productif ?

Les quatre grandeurs de la loi, pour une phase x (prove, verify, env) :

    W_x(N)      travail total pour un batch de N transactions
    W̄_x(N)      = W_x(N) / N            travail moyen par transaction
    Λ_x(N)      = N / T_x(N)            débit, en transactions par seconde
                = R_x^phys / W̄_x(N)

    γ_x(N)      = d ln W_x / d ln N     élasticité du TRAVAIL
    ξ_x(N)      = 1 - γ_x(N)            élasticité d'AMORTISSEMENT
                = - d ln W̄_x / d ln N
                = + d ln Λ_x / d ln N

Les trois régimes, lus sur γ :

    γ < 1   sous-linéaire   ξ > 0   W̄ baisse, Λ monte : le batching paie
    γ = 1   linéaire        ξ = 0   W̄ et Λ constants : plus aucun gain
    γ > 1   super-linéaire  ξ < 0   W̄ monte, Λ baisse : le batching nuit

Cas affine classique W = W_0 + w·N : ξ(N) = W_0 / (W_0 + w·N), qui décroît de
1 vers 0, et Λ converge vers R^phys / w. L'amortissement est fort au début,
puis s'épuise — le script superpose cette prédiction aux valeurs mesurées.

p, la taille de batch retenue
-----------------------------
La loi seule ne donne AUCUN optimum : W̄(N) = w + (W_0+v)/N décroît strictement,
donc « le meilleur N » serait l'infini, et ρ n'est qu'un genou. Un optimum
suppose une borne supérieure. Le script en retient quatre, toutes mesurables
sur le run, et p est la première atteinte :

    N_eps    = ρ/ε                      au-delà, le gain sur W̄ est < ε
    N_super  : γ(N) franchit 1          au-delà, W̄ REMONTE
    N_mem    : R_0 + r·N ≤ M            le prover doit tenir en RAM
    N_tau    : W_0 + v + w·N ≤ τ        prouver un batch par slot L1, sinon
                                        la file diverge

        p = 2^floor(log2( min(N_eps, N_super, N_mem, N_tau, N_circuits) ))

l'arrondi parce que les circuits n'existent qu'en puissances de deux, et le
plafond N_circuits parce qu'on ne peut pas prouver plus grand que le plus grand
circuit compilé. Ce que la figure 03 montre n'est pas seulement la valeur de p,
c'est LAQUELLE des bornes mord : c'est elle qu'il faut desserrer pour agrandir
le batch. τ et M sont lus par défaut dans le manifest.json du run
(`params.batch_period_s` et `env.mem_total_gb`), donc ce sont les budgets du
banc lui-même, pas des hypothèses ajoutées après coup.

Figures
-------
    01_elasticity     W, W̄, Λ et les deux élasticités γ, ξ
    02_optimal_batch  l'effondrement sur ξ = 1/(1 + N/ρ), et ρ par prover
    03_batch_size_p   p, et la contrainte qui le fixe

Pourquoi γ ne dépend pas du choix d'unité
-----------------------------------------
γ est une dérivée logarithmique : multiplier W par une constante ne la change
pas. Mesurer le travail en secondes de mur, en secondes CPU ou en contraintes
donne donc la MÊME élasticité, tant que le facteur de conversion R^phys ne
dépend pas de N. Le script prend le temps de mur par défaut (les identités
Λ = N/T et ξ = d ln Λ / d ln N sont alors exactes) et vérifie, quand on lui
demande les secondes CPU, que R^phys = CPU/mur reste bien plat.

Utilisation
-----------
    python scripts/analysis/elasticity.py --from bench-out/20260825_120620
    python scripts/analysis/elasticity.py --from bench-out/20260825_120620 \
        --work cpu --phases prove verify env

    # p sous d'autres budgets : slot de 4 s, machine de 8 Go, circuits jusqu'à
    # 2^16, et on s'arrête quand il ne reste que 2 % de surcoût
    python scripts/analysis/elasticity.py --from bench-out/20260825_120620 \
        --deadline 4 --ram-budget 8 --max-circuit 65536 --p-tolerance 0.02
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

try:  # consoles cp1252 : ne pas casser sur les accents
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

# Le prover porte la couleur, la phase porte le style de trait : c'est la même
# convention que les figures de optimize_batch_partition.py, pour qu'un lecteur
# qui passe d'une figure à l'autre garde ses repères.
PROVER_COLORS = {
    "rapidsnark": "#0072B2",
    "snarkjs": "#CC79A7",
    "": "#009E73",  # phases sans prover (env)
}
PHASE_LINESTYLES = {"prove": "-", "verify": "--", "env": "-."}

# Okabe-Ito, discriminables en niveaux de gris comme en daltonisme courant.
SUBLINEAR_C = "#009E73"
SUPERLINEAR_C = "#D55E00"
# Gris de l'axe ξ : assez sombre pour rester lisible, assez neutre pour ne pas
# se confondre avec une série.
XI_AXIS_C = "0.35"

# Chaque borne sur N porte une forme et un tiret qui lui sont propres : dans
# la figure 03 la couleur est déjà prise par le prover, et il faut pouvoir dire
# QUELLE borne mord sans lire la légende deux fois.
LIMIT_MARKERS = {"eps": "s", "super": "^", "mem": "D", "deadline": "v"}
LIMIT_LINESTYLES = {
    "eps": (0, (4, 2)),
    "super": (0, (1, 1.5)),
    "mem": (0, (6, 2, 1, 2)),
    "deadline": (0, (3, 1, 1, 1)),
}

SINGLE_COLUMN_IN = 3.5
DOUBLE_COLUMN_IN = 7.16


# --------------------------------------------------------------------------- #
# Lecture du run
# --------------------------------------------------------------------------- #


@dataclass
class Series:
    """Une phase mesurée, échantillonnée sur les N du run."""

    phase: str
    prover: str
    ns: List[int]
    work: List[float]   # W_x(N), dans l'unité choisie
    wall: List[float]   # T_x(N), toujours en secondes de mur
    cpu: List[float]    # secondes CPU, pour contrôler R^phys
    rss: List[float]    # pic de RSS en octets, pour le plafond mémoire

    @property
    def name(self) -> str:
        return f"{self.prover}/{self.phase}" if self.prover else self.phase

    @property
    def label(self) -> str:
        """Le libellé de légende : aussi court que possible sans ambiguïté.

        `prove` est la phase qui distingue les provers, donc son libellé se
        réduit au nom du prover. Les phases sans prover (verify fusionnée, env)
        portent simplement leur nom.
        """
        if not self.prover:
            return self.phase
        return self.prover if self.phase == "prove" else f"{self.prover} — {self.phase}"


def load_phases(run_dir: str, work_metric: str,
                phases: Sequence[str]) -> List[Series]:
    """Charge phases.csv et regroupe par (phase, prover).

    On ne garde que les lignes marquées ok : une mesure ratée fausserait une
    dérivée logarithmique bien plus qu'une moyenne, puisque γ se lit sur deux
    points voisins.
    """
    path = os.path.join(run_dir, "phases.csv")
    if not os.path.isfile(path):
        raise SystemExit(f"introuvable : {path}")
    col = {"wall": "wall_s_mean", "cpu": "cpu_total_s_mean"}[work_metric]

    buckets: Dict[Tuple[str, str], Dict[int, Tuple[float, ...]]] = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if str(r.get("ok", "True")).lower() not in ("true", "1"):
                continue
            if r["phase"] not in phases:
                continue
            try:
                n = int(r["n"])
                w = float(r[col])
                wall = float(r["wall_s_mean"])
                cpu = float(r["cpu_total_s_mean"])
            except (KeyError, ValueError):
                continue
            if n <= 0 or w <= 0 or wall <= 0:
                continue
            # Le RSS n'est qu'une contrainte d'exploitation : une colonne
            # absente ne doit pas faire perdre la mesure de temps.
            try:
                rss = float(r.get("rss_peak_bytes_mean") or 0.0)
            except ValueError:
                rss = 0.0
            buckets.setdefault((r["phase"], r["prover"]), {})[n] = (w, wall, cpu, rss)

    out: List[Series] = []
    for (phase, prover), by_n in sorted(buckets.items()):
        ns = sorted(by_n)
        if len(ns) < 3:  # deux points ne donnent qu'une seule pente
            print(f"[ignoré] {prover or '-'}/{phase} : {len(ns)} point(s)")
            continue
        out.append(Series(
            phase=phase, prover=prover, ns=ns,
            work=[by_n[n][0] for n in ns],
            wall=[by_n[n][1] for n in ns],
            cpu=[by_n[n][2] for n in ns],
            rss=[by_n[n][3] for n in ns],
        ))
    if not out:
        raise SystemExit(f"aucune série exploitable dans {path}")
    return out


def merge_phase(series: List[Series], phase: str) -> List[Series]:
    """Fusionne en une seule série les mesures d'une phase commune aux provers.

    La vérification Groth16 ne dépend pas du prover qui a produit la preuve :
    c'est le même couplage sur la même courbe, avec la même clé. Mesurée deux
    fois, elle donne deux séries qui se superposent — sur ce run, 2.3 % d'écart
    maximum entre provers, là où l'écart-type de mesure intra-série vaut 2.9 %.
    Les garder séparées ferait croire à deux régimes là où il n'y en a qu'un,
    et doublerait une courbe dans chaque panneau pour rien.

    La fusion moyenne les deux mesures point par point : ce sont deux
    échantillons indépendants de la même grandeur, pas deux grandeurs.
    """
    targets = [s for s in series if s.phase == phase and s.prover]
    if len(targets) < 2:
        return series

    common = sorted(set.intersection(*(set(s.ns) for s in targets)))
    if not common:
        return series

    def mean_at(getter, n: int) -> float:
        vals = [getter(s)[s.ns.index(n)] for s in targets]
        return statistics.mean(vals)

    spread = max(
        (max(s.work[s.ns.index(n)] for s in targets)
         - min(s.work[s.ns.index(n)] for s in targets)) / mean_at(lambda s: s.work, n)
        for n in common
    )
    names = ", ".join(s.prover for s in targets)
    print(f"  [fusion] {phase} : {names} moyennés en une série "
          f"(écart max entre provers {spread * 100:.1f} %)")

    merged = Series(
        phase=phase, prover="", ns=common,
        work=[mean_at(lambda s: s.work, n) for n in common],
        wall=[mean_at(lambda s: s.wall, n) for n in common],
        cpu=[mean_at(lambda s: s.cpu, n) for n in common],
        rss=[mean_at(lambda s: s.rss, n) for n in common],
    )
    kept = [s for s in series if s not in targets]
    kept.append(merged)
    # L'ordre de tracé suit l'ordre des phases demandées, puis le prover.
    kept.sort(key=lambda s: (s.phase != "prove", s.phase, s.prover))
    return kept


# --------------------------------------------------------------------------- #
# Les grandeurs de la loi
# --------------------------------------------------------------------------- #


def log_slope(xs: Sequence[float], ys: Sequence[float]) -> List[float]:
    """d ln y / d ln x, par différences centrées en échelle logarithmique.

    Les N du run doublent à chaque point : ils sont donc régulièrement espacés
    en ln N, ce qui est exactement le cas où la différence centrée est le bon
    estimateur — elle est d'ordre 2 et ne privilégie aucun côté. Aux deux
    extrémités on retombe sur une différence latérale, d'ordre 1 : la valeur de
    γ au dernier point est donc un peu moins fiable que les autres.
    """
    n = len(xs)
    lx = [math.log(x) for x in xs]
    ly = [math.log(y) for y in ys]
    out: List[float] = []
    for i in range(n):
        a, b = (0, 1) if i == 0 else (n - 2, n - 1) if i == n - 1 else (i - 1, i + 1)
        dx = lx[b] - lx[a]
        out.append((ly[b] - ly[a]) / dx if dx else 0.0)
    return out


@dataclass
class AffineFit:
    """W(N) = W0 + w·N, le « cas classique » de la loi."""

    w0: float
    w: float
    r2: float

    def gamma(self, n: float) -> float:
        """γ(N) = w·N / (W0 + w·N), la prédiction du modèle affine."""
        den = self.w0 + self.w * n
        return self.w * n / den if den > 0 else 0.0

    def xi(self, n: float) -> float:
        """ξ(N) = W0 / (W0 + w·N) : fort au début, puis s'épuise."""
        return 1.0 - self.gamma(n)


def fit_affine(ns: Sequence[int], ws: Sequence[float]) -> AffineFit:
    """Moindres carrés ordinaires sur W = W0 + w·N."""
    mx = statistics.mean(ns)
    my = statistics.mean(ws)
    sxx = sum((x - mx) ** 2 for x in ns)
    sxy = sum((x - mx) * (y - my) for x, y in zip(ns, ws))
    w = sxy / sxx if sxx > 0 else 0.0
    w0 = my - w * mx
    ss_tot = sum((y - my) ** 2 for y in ws)
    ss_res = sum((y - (w0 + w * x)) ** 2 for x, y in zip(ns, ws))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return AffineFit(w0=w0, w=w, r2=r2)


@dataclass
class Elasticity:
    """Tout ce que la loi dit d'une série, prêt à tracer."""

    series: Series
    wbar: List[float]       # W̄(N) = W(N)/N
    throughput: List[float]  # Λ(N) = N/T(N)
    gamma: List[float]       # γ(N), mesuré
    xi: List[float]          # ξ(N) = 1 - γ(N)
    fit: AffineFit

    @property
    def regime(self) -> str:
        g = self.gamma[-1]
        if g > 1.02:
            return "super-linéaire"
        if g < 0.98:
            return "sous-linéaire"
        return "linéaire"

    def crossing_n(self) -> Optional[float]:
        """Le N où γ franchit 1, par interpolation en ln N entre deux points."""
        ns, g = self.series.ns, self.gamma
        for i in range(len(ns) - 1):
            if (g[i] - 1.0) * (g[i + 1] - 1.0) < 0:
                t = (1.0 - g[i]) / (g[i + 1] - g[i])
                return math.exp(math.log(ns[i])
                                + t * (math.log(ns[i + 1]) - math.log(ns[i])))
        return None


def analyse(s: Series) -> Elasticity:
    wbar = [w / n for w, n in zip(s.work, s.ns)]
    thr = [n / t for n, t in zip(s.ns, s.wall)]
    gamma = log_slope(s.ns, s.work)
    return Elasticity(
        series=s,
        wbar=wbar,
        throughput=thr,
        gamma=gamma,
        xi=[1.0 - g for g in gamma],
        fit=fit_affine(s.ns, s.work),
    )


def parallelism_spread(s: Series) -> Tuple[float, float]:
    """(min, max) de R^phys = CPU/mur sur la série.

    Λ = R^phys / W̄ ne tient que si ce rapport est constant en N. Quand il ne
    l'est pas, ξ et d ln Λ / d ln N se séparent, et il faut le dire plutôt que
    de tracer les deux comme s'ils coïncidaient.
    """
    r = [c / w for c, w in zip(s.cpu, s.wall) if w > 0]
    return (min(r), max(r)) if r else (0.0, 0.0)



@dataclass
class Amortization:
    """Le coût d'UNE preuve, et son unique paramètre d'échelle rho.

    On amortit le coût d'une preuve complète : l'intercept de la preuve ET la
    vérification, qui se paie une fois par preuve quelle que soit la taille du
    circuit. D'où W_total(N) = (W_0 + v) + w·N, et donc

        xi(N) = (W_0 + v) / ((W_0 + v) + w·N) = rho / (rho + N)
        W_bar(N) / w = 1 + rho / N

    Tout ne dépend donc que de rho = (W_0 + v)/w, exprimé en transactions :
    c'est le nombre de transactions dont le coût marginal égale le coût fixe
    d'une preuve. C'est exactement le rho que calcule optimize_batch_partition,
    par un tout autre chemin — ici une dérivée logarithmique, là-bas le prix
    d'une preuve supplémentaire en transactions vides.
    """

    prover: str
    ns: List[int]
    work: List[float]   # W_prove(N) + W_verify(N)
    xi: List[float]     # mesuré, par différences centrées
    fit: AffineFit
    rss: List[float]        # pic de RSS de la preuve, en octets
    rss_fit: AffineFit      # RSS = R_0 + r·N, pour le plafond mémoire

    @property
    def rho(self) -> float:
        return self.fit.w0 / self.fit.w if self.fit.w > 0 else float("inf")

    def n_for(self, eps: float) -> float:
        """Le N qui amène W_bar à moins de eps du plancher w : N = rho/eps."""
        return self.rho / eps

    def law(self, n: float) -> float:
        """La courbe maîtresse xi = rho/(rho+N)."""
        return self.rho / (self.rho + n)


def build_amortization(els: Sequence[Elasticity]) -> List[Amortization]:
    """Assemble, par prover, le coût d'une preuve complète : prove + verify.

    La vérification est constante en N : c'est du coût fixe, donc du coût
    amortissable, et l'ignorer sous-estimerait rho. Si aucune série verify
    n'est disponible, on retombe sur la preuve seule — rho est alors le rho
    du prover seul, sans le règlement.
    """
    ver = next((e for e in els if e.series.phase == "verify"), None)
    out: List[Amortization] = []
    for e in els:
        if e.series.phase != "prove":
            continue
        ns = e.series.ns
        work = list(e.series.work)
        if ver is not None:
            by_n = dict(zip(ver.series.ns, ver.series.work))
            if all(n in by_n for n in ns):
                work = [w + by_n[n] for w, n in zip(work, ns)]
        # Le RSS retenu est celui de la PREUVE seule : c'est le processus qui
        # dimensionne la machine. La vérification tient dans quelques dizaines
        # de Mo et tourne de toute façon ailleurs, sur L1.
        out.append(Amortization(
            prover=e.series.prover, ns=ns, work=work,
            xi=[1.0 - g for g in log_slope(ns, work)],
            fit=fit_affine(ns, work),
            rss=list(e.series.rss),
            rss_fit=fit_affine(ns, e.series.rss),
        ))
    return out


def run_defaults(run_dir: str) -> Dict[str, float]:
    """Les budgets que le run se donne déjà à lui-même, lus dans manifest.json.

    Deux d'entre eux n'ont pas à être inventés en ligne de commande : la
    période de batch (`batch_period_s`, le slot L1 sur lequel le rollup se
    cale) et la mémoire de la machine qui a produit les mesures
    (`env.mem_total_gb`). Les prendre ici garantit que la contrainte tracée est
    celle du banc, et non une hypothèse ajoutée après coup.
    """
    out: Dict[str, float] = {}
    path = os.path.join(run_dir, "manifest.json")
    try:
        with open(path, encoding="utf-8") as f:
            man = json.load(f)
    except (OSError, ValueError):
        return out
    tau = man.get("params", {}).get("batch_period_s")
    mem = man.get("env", {}).get("mem_total_gb")
    if isinstance(tau, (int, float)) and tau > 0:
        out["deadline_s"] = float(tau)
    if isinstance(mem, (int, float)) and mem > 0:
        out["ram_gb"] = float(mem)
    return out


# --------------------------------------------------------------------------- #
# p : la taille de batch retenue, et les contraintes qui la fixent
# --------------------------------------------------------------------------- #
#
# Pourquoi il faut des contraintes pour parler d'optimum
# ------------------------------------------------------
# Sur le seul coût, il n'y a PAS d'optimum intérieur : W_bar(N) = w + (W_0+v)/N
# décroît strictement, donc « le meilleur N » serait l'infini. Le rho de la
# section précédente donne un GENOU, pas un minimum. Ce qui borne N par le
# haut, ce sont quatre faits mesurables de ce run :
#
#   (1) amortissement épuisé   au-delà de N = rho/eps, ce que l'on gagne encore
#                              sur W_bar est inférieur à eps : on paie de la
#                              latence et de la mémoire pour rien.
#   (2) super-linéarité        là où gamma franchit 1, W_bar REMONTE. C'est la
#                              seule borne qui soit un vrai maximum de gain.
#   (3) mémoire                le prover doit tenir dans la RAM de la machine :
#                              RSS(N) = R_0 + r*N <= M.
#   (4) échéance de preuve     le rollup poste un batch par slot L1 de période
#                              tau. En régime permanent il faut prouver plus
#                              vite qu'on ne remplit : W_0 + v + w*N <= tau,
#                              sans quoi la file diverge.
#
# p est donc la PREMIÈRE de ces bornes atteinte, arrondie à la puissance de deux
# inférieure — les circuits n'existent qu'en puissances de deux — et plafonnée
# par le plus grand circuit réellement construit :
#
#       p = 2^floor(log2( min(N_eps, N_super, N_mem, N_tau, N_circuits) ))
#
# Le résultat utile n'est pas seulement la valeur : c'est de savoir LAQUELLE des
# bornes mord, parce que c'est elle qui dit quoi améliorer pour aller plus loin.
# Un p fixé par (4) demande un prover plus rapide ; fixé par (1), rien ne sert
# d'aller plus loin ; fixé par les circuits, c'est l'implémentation et non la
# physique qui limite.


@dataclass
class Budget:
    """Ce que l'exploitation impose au batch, en plus de la loi d'amortissement."""

    eps: float           # tolérance sur W_bar : au-delà de rho/eps, gain < eps
    ram_bytes: float     # mémoire utilisable par le prover
    deadline_s: float    # temps de preuve maximal (période de slot L1)
    max_circuit: int     # plus grand circuit réellement construit


@dataclass
class Limit:
    """Une borne supérieure sur N, et d'où elle vient."""

    key: str
    label: str           # libellé court, pour la figure
    n: Optional[float]   # None : la borne ne mord jamais sur ce domaine
    detail: str          # la valeur du budget, pour le rapport console


@dataclass
class Optimum:
    """La réponse pour un prover : p, et la borne qui l'a fixé."""

    prover: str
    am: Amortization
    limits: List[Limit]
    binding: Limit       # la borne atteinte en premier
    n_allowed: float     # min des bornes, AVANT arrondi et plafond circuits
    p: int               # la puissance de deux retenue
    capped_by_circuits: bool

    def xi_at_p(self) -> float:
        """Ce qu'il reste d'amortissement en p : rho/(rho+p)."""
        return self.am.law(self.p)

    def work_at_p(self) -> float:
        return self.am.fit.w0 + self.am.fit.w * self.p

    def rss_at_p(self) -> float:
        return self.am.rss_fit.w0 + self.am.rss_fit.w * self.p


def pow2_at_most(x: float) -> int:
    """La plus grande puissance de deux <= x, au moins 1."""
    return 1 if x < 2 else 1 << int(math.floor(math.log2(x)))


def cross_below(ns: Sequence[int], ys: Sequence[float],
                level: float = 0.0) -> Optional[float]:
    """Le N où y descend sous `level`, interpolé en ln N. None si jamais.

    On cherche le PREMIER franchissement descendant : une fois que xi est passé
    sous zéro, le batching a cessé de payer, et un retour au-dessus plus loin
    serait du bruit, pas un second régime.
    """
    for i in range(len(ns) - 1):
        if ys[i] > level >= ys[i + 1]:
            t = (ys[i] - level) / (ys[i] - ys[i + 1])
            return math.exp(math.log(ns[i])
                            + t * (math.log(ns[i + 1]) - math.log(ns[i])))
    return None


def _n_for_budget(fit: AffineFit, budget: float) -> Optional[float]:
    """Le plus grand N tel que fit.w0 + fit.w*N <= budget.

    Une pente nulle ou négative veut dire que la grandeur ne croît pas avec N :
    la borne ne mord alors jamais, et on renvoie None plutôt qu'un infini qui
    se propagerait dans les comparaisons.
    """
    if fit.w <= 0:
        return None
    return max((budget - fit.w0) / fit.w, 0.0)


def solve_optimum(a: Amortization, b: Budget) -> Optimum:
    """Les quatre bornes, leur minimum, et p."""
    limits: List[Limit] = [
        Limit("eps", r"amortization  $\xi \leq \varepsilon$",
              a.n_for(b.eps), f"xi <= {b.eps:.0%} du plancher"),
        Limit("super", r"superlinear  $\gamma > 1$",
              cross_below(a.ns, a.xi, 0.0), "gamma franchit 1"),
        Limit("mem", "memory", _n_for_budget(a.rss_fit, b.ram_bytes),
              f"RSS <= {b.ram_bytes / 1e9:.0f} GB"),
        Limit("deadline", "proving deadline",
              _n_for_budget(a.fit, b.deadline_s),
              f"prove+verify <= {b.deadline_s:.0f} s"),
    ]

    live = [l for l in limits if l.n is not None]
    binding = min(live, key=lambda l: l.n) if live else limits[0]
    n_allowed = binding.n if live else float("inf")

    return Optimum(prover=a.prover, am=a, limits=limits, binding=binding,
                   n_allowed=n_allowed,
                   p=pow2_at_most(min(n_allowed, float(b.max_circuit))),
                   capped_by_circuits=b.max_circuit < n_allowed)


# --------------------------------------------------------------------------- #
# Style des figures
# --------------------------------------------------------------------------- #


@dataclass
class FigureStyle:
    formats: Tuple[str, ...] = ("png", "pdf")
    dpi: int = 600
    width_in: float = DOUBLE_COLUMN_IN
    font_size: float = 8.0
    titles: bool = False


FIG = FigureStyle()


def _setup_mpl():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    try:
        import scienceplots  # noqa: F401
        plt.style.use(["science"])
    except Exception:
        plt.style.use("default")
    plt.rcParams.update({
        # mathtext STIX : $N$ et $\gamma$ ont le même dessin que dans le LaTeX
        # environnant, sans imposer une installation TeX pour lancer le script.
        "text.usetex": False,
        "mathtext.fontset": "stix",
        "font.family": "serif",
        "font.size": FIG.font_size,
        "axes.labelsize": FIG.font_size,
        "axes.titlesize": FIG.font_size,
        "legend.fontsize": FIG.font_size - 1,
        "xtick.labelsize": FIG.font_size - 1,
        "ytick.labelsize": FIG.font_size - 1,
        "axes.linewidth": 0.6,
        "lines.linewidth": 1.0,
        "legend.frameon": False,
        "savefig.dpi": FIG.dpi,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.02,
    })
    return plt


def _grid(ax) -> None:
    ax.grid(True, which="major", ls="--", lw=0.35, alpha=0.6)
    ax.grid(True, which="minor", ls=":", lw=0.25, alpha=0.3)
    ax.set_axisbelow(True)


def _style_of(e: Elasticity) -> Dict[str, object]:
    s = e.series
    return {
        "color": PROVER_COLORS.get(s.prover, SUBLINEAR_C),
        "ls": PHASE_LINESTYLES.get(s.phase, "-"),
    }


def _save(plt, fig, outdir: str, name: str) -> None:
    for ext in FIG.formats:
        fig.savefig(os.path.join(outdir, f"{name}.{ext}"), dpi=FIG.dpi)
    plt.close(fig)
    print(f"    figure : {name}." + "/.".join(FIG.formats))


# --------------------------------------------------------------------------- #
# La figure
# --------------------------------------------------------------------------- #


def make_figure(outdir: str, els: Sequence[Elasticity], show_fit: bool) -> None:
    """Les quatre panneaux de la loi, dans l'ordre où elle s'énonce.

    (a) W(N) -> (b) W̄(N) = W/N -> (c) Λ(N) = N/T, puis (d) γ et ξ, qui sont
    les pentes logarithmiques des trois premiers. Un lecteur peut donc vérifier
    à l'oeil : là où (b) descend, (c) monte, et (d) est sous la ligne γ = 1.
    """
    plt = _setup_mpl()
    fig, axes = plt.subplots(2, 2, figsize=(FIG.width_in, FIG.width_in * 0.62))
    (ax_w, ax_wbar), (ax_thr, ax_g) = axes

    # -- (a) travail total ------------------------------------------------- #
    for e in els:
        st = _style_of(e)
        ax_w.plot(e.series.ns, e.series.work, label=e.series.label, **st)
        if show_fit:
            ax_w.plot(e.series.ns, [e.fit.w0 + e.fit.w * n for n in e.series.ns],
                      lw=0.6, alpha=0.55, color=st["color"], ls=":")
    ax_w.set_xscale("log")
    ax_w.set_yscale("log")
    ax_w.set_ylabel(r"$W_x(N)$  (s)")
    ax_w.set_title("(a) total work", loc="left")
    _grid(ax_w)

    # -- (b) travail moyen par transaction --------------------------------- #
    for e in els:
        ax_wbar.plot(e.series.ns, e.wbar, **_style_of(e))
    ax_wbar.set_xscale("log")
    ax_wbar.set_yscale("log")
    ax_wbar.set_ylabel(r"$\bar{W}_x(N) = W_x(N)/N$  (s/tx)")
    ax_wbar.set_title("(b) work per transaction", loc="left")
    _grid(ax_wbar)

    # -- (c) débit --------------------------------------------------------- #
    for e in els:
        st = _style_of(e)
        ax_thr.plot(e.series.ns, e.throughput, **st)
        # Asymptote Λ -> R/w du cas affine : la limite que le batching vise.
        if show_fit and e.fit.w > 0:
            ax_thr.axhline(1.0 / e.fit.w, lw=0.6, alpha=0.5,
                           color=st["color"], ls=":")
    ax_thr.set_xscale("log")
    ax_thr.set_yscale("log")
    ax_thr.set_xlabel(r"Batch size $N$ (transactions)")
    ax_thr.set_ylabel(r"$\Lambda_x(N) = N / T_x(N)$  (tx/s)")
    ax_thr.set_title("(c) throughput", loc="left")
    _grid(ax_thr)

    # -- (d) les deux élasticités ------------------------------------------ #
    # Les bandes disent le verdict avant même de lire les courbes : sous la
    # ligne le batching amortit, au-dessus il se retourne contre lui-même.
    ax_g.axhspan(-0.6, 1.0, color=SUBLINEAR_C, alpha=0.07, lw=0, zorder=0)
    ax_g.axhspan(1.0, 1.6, color=SUPERLINEAR_C, alpha=0.09, lw=0, zorder=0)
    ax_g.axhline(1.0, ls=":", lw=1.0, color="0.25", zorder=4)
    # Pas de prédiction affine ici, contrairement aux panneaux (a) et (c) :
    # elle doublerait chaque courbe pour presque rien, puisqu'elle colle à
    # la mesure sur les séries prove et n'a aucun sens sur verify (w ~ 0, R^2 =
    # 0.01). L'ajustement se juge en (a), directement contre W.
    #
    # À noter tout de même, et c'est dans le rapport console : le modèle
    # affine donne gamma = wN/(W0 + wN), qui est < 1 pour tout N. Un gamma
    # mesuré au-dessus de 1 n'est donc pas un point du modèle affine, c'est un
    # écart à ce modèle.
    for e in els:
        ax_g.plot(e.series.ns, e.gamma, marker="o", ms=1.8, zorder=3,
                  **_style_of(e))
    ax_g.set_xscale("log")
    ax_g.set_ylim(-0.6, 1.6)
    ax_g.set_yticks([0.0, 0.5, 1.0, 1.5])
    ax_g.set_xlabel(r"Batch size $N$ (transactions)")
    ax_g.set_ylabel(r"$\gamma_x(N) = \mathrm{d}\ln W_x / \mathrm{d}\ln N$")
    ax_g.set_title("(d) work and amortization elasticity", loc="left")
    _grid(ax_g)

    # ξ = 1 - γ n'est pas une seconde mesure, c'est une définition : tracer
    # une deuxième courbe dessinerait deux fois la même information. On donne
    # donc la seconde graduation qui la lit sur le même trait.
    #
    # Cet axe est INVERSÉ par construction — γ monte quand ξ descend — et rien
    # ne le signale de soi-même : un lecteur voit une courbe montante et croit
    # que les deux élasticités vont dans le même sens. D'où les trois repères
    # ci-dessous : l'inversion écrite dans le libellé, toute la graduation
    # grisée pour qu'elle se lise comme une échelle étrangère à l'axe gauche,
    # et l'équivalence des deux seuils posée sur la ligne qui les porte.
    ax_xi = ax_g.secondary_yaxis(
        "right", functions=(lambda g: 1.0 - g, lambda x: 1.0 - x))
    ax_xi.set_ylabel(r"$\xi_x(N) = 1 - \gamma_x(N)$  (axis reversed)",
                     color=XI_AXIS_C)
    ax_xi.set_yticks([-0.5, 0.0, 0.5, 1.0])
    ax_xi.tick_params(axis="y", colors=XI_AXIS_C)
    for sp in ax_xi.spines.values():
        sp.set_color(XI_AXIS_C)

    # Posé juste sous la ligne seuil : c'est le point où les deux lectures se
    # rejoignent, et donc la clé de l'inversion.
    ax_g.text(0.035, 0.705, r"$\gamma = 1 \Leftrightarrow \xi = 0$",
              transform=ax_g.transAxes, fontsize=FIG.font_size - 2,
              color="0.25", va="top", zorder=5)

    ax_g.text(0.04, 0.10, "sublinear: batching amortizes",
              transform=ax_g.transAxes, fontsize=FIG.font_size - 2,
              color=SUBLINEAR_C, va="bottom", zorder=5)
    ax_g.text(0.04, 0.93, "superlinear: batching backfires",
              transform=ax_g.transAxes, fontsize=FIG.font_size - 2,
              color=SUPERLINEAR_C, va="top", zorder=5)

    handles, labels = ax_w.get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=min(4, len(labels)),
               frameon=False, bbox_to_anchor=(0.5, -0.04),
               columnspacing=1.6, handletextpad=0.6)
    if FIG.titles:
        fig.suptitle("Batch amortization law")
    fig.tight_layout()
    _save(plt, fig, outdir, "01_elasticity")


def make_optimum_figure(outdir: str, ams: Sequence[Amortization],
                       tolerances: Sequence[float], nmax: int) -> None:
    """Figure 2 : où placer N pour chaque prover.

    (a) L'effondrement. Si xi ne dépend que de rho, alors tracer xi contre le N
    RÉDUIT x = N/rho doit superposer toutes les implémentations sur la même
    courbe 1/(1+x). C'est le test de la loi, pas son illustration : deux
    provers séparés d'un facteur 5 en temps absolu et 3.7 en rho doivent
    tomber l'un sur l'autre. Les écarts à la courbe sont alors le signal —
    c'est là que se voit le décrochage super-linéaire de snarkjs.

    (b) La réponse en N réels. Pour chaque prover, la barre est le domaine
    effectivement mesuré ; les marqueurs sont rho (amortissement à moitié
    consommé) et les cibles N = rho/eps. Un marqueur PLEIN est dans le domaine
    mesuré, un marqueur CREUX est une extrapolation de la loi affine au-delà
    des données — distinction qui porte toute la conclusion, puisque l'optimum
    de rapidsnark tombe hors du domaine et celui de snarkjs dedans.
    """
    plt = _setup_mpl()
    fig, (ax_c, ax_n) = plt.subplots(
        1, 2, figsize=(FIG.width_in, FIG.width_in * 0.40))

    # -- (a) effondrement sur la courbe maîtresse -------------------------- #
    xs = [10 ** (-3 + i * 4.8 / 299) for i in range(300)]
    ax_c.plot(xs, [1.0 / (1.0 + x) for x in xs], lw=1.1, color="0.25",
              zorder=2, label=r"$\xi = 1/(1 + N/\rho)$")
    ax_c.axhline(0.0, ls="-", lw=0.6, color="0.45", zorder=1)
    for eps in tolerances:
        ax_c.axvline(1.0 / eps, ls=":", lw=0.8, color="0.55", zorder=1)
        ax_c.text(1.0 / eps, 0.62, rf"$\bar{{W}}$ within {eps:.0%}",
                  fontsize=FIG.font_size - 2, color="0.45", rotation=90,
                  ha="right", va="center", zorder=2)
    for a in ams:
        ax_c.plot([n / a.rho for n in a.ns], a.xi, marker="o", ms=2.4, lw=0.7,
                  color=PROVER_COLORS.get(a.prover, SUBLINEAR_C),
                  label=f"{a.prover}  ($\\rho$ = {a.rho:,.0f})", zorder=3)
    ax_c.set_xscale("log")
    ax_c.set_ylim(-0.25, 1.12)
    ax_c.set_xlabel(r"Reduced batch size  $N / \rho$")
    ax_c.set_ylabel(r"Amortization elasticity  $\xi$")
    ax_c.set_title("(a) one curve for every prover", loc="left")
    _grid(ax_c)
    ax_c.legend(loc="lower left", fontsize=FIG.font_size - 2)

    # -- (b) les cibles, en N réels ---------------------------------------- #
    # Marqueur par tolérance : la forme porte le critère, le remplissage dit
    # si la valeur est mesurée ou extrapolée.
    shapes = ["s", "D", "^", "v"]
    hi = max(max(a.n_for(min(tolerances)) for a in ams), nmax) * 1.6
    for i, a in enumerate(ams):
        y = len(ams) - 1 - i
        color = PROVER_COLORS.get(a.prover, SUBLINEAR_C)
        ax_n.plot([1, nmax], [y, y], lw=5.0, color=color, alpha=0.30,
                  solid_capstyle="butt", zorder=2)
        if hi > nmax:  # la zone que la loi affine prolonge sans données
            ax_n.plot([nmax, hi], [y, y], lw=5.0, color=color, alpha=0.16,
                      solid_capstyle="butt", zorder=1)
        ax_n.plot([a.rho], [y], marker="o", ms=4.2, color=color, zorder=4,
                  mfc=color if a.rho <= nmax else "white", mew=0.9)
        for j, eps in enumerate(sorted(tolerances, reverse=True)):
            n_t = a.n_for(eps)
            ax_n.plot([n_t], [y], marker=shapes[j % len(shapes)], ms=4.2,
                      color=color, zorder=4, mew=0.9,
                      mfc=color if n_t <= nmax else "white")
        ax_n.text(1.25, y + 0.26, a.prover, fontsize=FIG.font_size - 1,
                  color=color, va="bottom", ha="left", zorder=5)
    ax_n.axvline(nmax, ls="--", lw=0.9, color="0.3", zorder=3)
    ax_n.text(nmax * 0.88, len(ams) - 0.36, f"measured up to $N$ = {nmax:,}",
              fontsize=FIG.font_size - 2, color="0.3", ha="right", va="top")
    ax_n.set_xscale("log")
    ax_n.set_xlim(1, hi)
    # La bande sous la derniere barre est reservee a la legende des marqueurs.
    ax_n.set_ylim(-1.55, len(ams) - 0.28)
    ax_n.set_yticks([])
    ax_n.set_xlabel(r"Batch size $N$ (transactions)")
    ax_n.set_title("(b) where to set $N$", loc="left")
    ax_n.grid(True, axis="x", which="major", ls="--", lw=0.35, alpha=0.6)
    ax_n.set_axisbelow(True)

    marks = [plt.Line2D([], [], ls="none", marker="o", ms=4.2, color="0.35",
                        label=r"$\rho$  ($\xi = 1/2$)")]
    for j, eps in enumerate(sorted(tolerances, reverse=True)):
        marks.append(plt.Line2D([], [], ls="none", marker=shapes[j % len(shapes)],
                                ms=4.2, color="0.35",
                                label=r"$\bar{W}$ within " + f"{eps:.0%}"))
    marks.append(plt.Line2D([], [], ls="none", marker="o", ms=4.2, color="0.35",
                            mfc="white", mew=0.9, label="hollow = extrapolated"))
    ax_n.legend(handles=marks, loc="lower center", ncol=2,
                fontsize=FIG.font_size - 2, handletextpad=0.4,
                borderpad=0.3, columnspacing=1.2)

    if FIG.titles:
        fig.suptitle("Optimal batch size per prover")
    fig.tight_layout()
    _save(plt, fig, outdir, "02_optimal_batch")


def make_p_figure(outdir: str, opts: Sequence[Optimum], b: Budget,
                  nmax: int) -> None:
    """Figure 3 : d'où vient p, et ce qu'il coûte de s'arrêter là.

    (a) La courbe de décision. W_bar(N)/w part de 1 + rho/N et tombe vers 1 :
    c'est le SURCOÛT par transaction, en multiples du plancher w. Les traits
    verticaux sont les quatre bornes ; p est la première atteinte, arrondie à
    la puissance de deux inférieure, et l'étoile la pose sur la courbe. On lit
    donc d'un coup l'arbitrage : où l'on s'arrête, et combien de surcoût il
    reste à cet endroit.

    (b) Le budget, borne par borne. Une voie par prover, la zone pleine est le
    domaine admissible, la zone hachurée celle qu'au moins une borne interdit.
    Un marqueur CREUX est une borne extrapolée au-delà du domaine mesuré. Le
    marqueur le plus à gauche est celui qui mord : c'est lui qui fixe p, et
    donc lui qu'il faut desserrer pour agrandir le batch.
    """
    plt = _setup_mpl()
    fig, (ax_c, ax_b) = plt.subplots(
        1, 2, figsize=(FIG.width_in, FIG.width_in * 0.42))

    finite = [o.n_allowed for o in opts if math.isfinite(o.n_allowed)]
    hi = max(finite + [float(b.max_circuit), float(nmax)]) * 2.2
    # Le panneau (a) ne commence pas à N = 1. Entre 1 et quelques dizaines,
    # W_bar/w vaut des centaines : trois décades de verticale pour une région
    # où aucune borne ne se pose, et la zone de décision — celle du genou et
    # des bornes — se retrouverait écrasée sur le plancher. La chute complète
    # est déjà l'objet des figures 01 et 02.
    lo = 32.0
    xs = [10 ** (math.log10(lo) + i * math.log10(hi / lo) / 299)
          for i in range(300)]

    # -- (a) la courbe de décision ----------------------------------------- #
    for i, o in enumerate(opts):
        color = PROVER_COLORS.get(o.prover, SUBLINEAR_C)
        w = o.am.fit.w
        ax_c.plot(xs, [1.0 + o.am.rho / x for x in xs], lw=1.1, color=color,
                  zorder=3, label=f"{o.prover}  ($\\rho$ = {o.am.rho:,.0f})")
        if w > 0:  # les mesures, pour montrer que la loi n'est pas postulée
            ax_c.plot(o.am.ns, [wk / n / w for wk, n in zip(o.am.work, o.am.ns)],
                      ls="none", marker="o", ms=2.6, color=color, zorder=4)
        for lim in o.limits:
            if lim.n is None or not (0 < lim.n < hi):
                continue
            ax_c.axvline(lim.n, ls=LIMIT_LINESTYLES[lim.key], lw=0.7,
                         color=color, alpha=0.55, zorder=2)
        ax_c.plot([o.p], [1.0 + o.am.rho / o.p], marker="*", ms=9.0,
                  color=color, mec="white", mew=0.5, zorder=6)
        ax_c.annotate(f"$p$ = {o.p:,}", xy=(o.p, 1.0 + o.am.rho / o.p),
                      xytext=(5, 6 + 10 * i), textcoords="offset points",
                      fontsize=FIG.font_size - 2, color=color, zorder=6,
                      arrowprops=dict(arrowstyle="-", lw=0.5, color=color,
                                      shrinkA=0.0, shrinkB=2.0))

    # Le plafond du jeu de circuits n'est pas une borne physique : c'est ce que
    # ce dépôt a compilé. Il mérite donc un trait neutre, pas une couleur.
    ax_c.axvline(b.max_circuit, ls="-", lw=0.8, color="0.35", zorder=2)
    ax_c.text(b.max_circuit * 0.88, 0.04,
              f"largest circuit built ({b.max_circuit:,})",
              transform=ax_c.get_xaxis_transform(), rotation=90,
              fontsize=FIG.font_size - 2, color="0.35", ha="right", va="bottom")
    # Le plancher w : l'asymptote que le batching vise et n'atteint jamais.
    ax_c.axhline(1.0, ls=":", lw=0.8, color="0.45", zorder=1)
    ax_c.text(hi * 0.95, 1.008, "floor $w$", fontsize=FIG.font_size - 2,
              color="0.45", ha="right", va="bottom")

    ax_c.set_xscale("log")
    ax_c.set_yscale("log")
    ax_c.set_xlim(lo, hi)
    ax_c.set_ylim(0.93, 1.35 * max(1.0 + o.am.rho / lo for o in opts))
    ax_c.set_xlabel(r"Batch size $N$ (transactions)")
    ax_c.set_ylabel(r"$\bar{W}(N)\,/\,w$   (cost per tx, floor = 1)")
    ax_c.set_title("(a) where to stop, and what it still costs", loc="left")
    _grid(ax_c)
    ax_c.legend(loc="lower left", fontsize=FIG.font_size - 2)

    # -- (b) les bornes, une voie par prover -------------------------------- #
    for i, o in enumerate(opts):
        y = len(opts) - 1 - i
        color = PROVER_COLORS.get(o.prover, SUBLINEAR_C)
        stop = min(o.n_allowed, hi)
        left = lo
        # Deux teintes, deux statuts très différents : la teinte pleine est ce
        # que le dépôt peut réellement exécuter, la teinte pâle ce que les
        # bornes physiques autoriseraient si les circuits existaient.
        avail = min(stop, float(b.max_circuit))
        ax_b.plot([left, avail], [y, y], lw=6.0, color=color, alpha=0.32,
                  solid_capstyle="butt", zorder=2)
        if stop > avail:
            ax_b.plot([avail, stop], [y, y], lw=6.0, color=color, alpha=0.11,
                      solid_capstyle="butt", zorder=2)
        if stop < hi:
            ax_b.fill_between([stop, hi], y - 0.13, y + 0.13, facecolor="none",
                              hatch="////", edgecolor="0.6", lw=0.0, zorder=1)
        for lim in o.limits:
            if lim.n is None or not (0 < lim.n < hi):
                continue
            ax_b.plot([lim.n], [y], marker=LIMIT_MARKERS[lim.key], ms=4.4,
                      color=color, mew=0.9, zorder=4,
                      mfc=color if lim.n <= nmax else "white")
        ax_b.plot([o.p], [y], marker="*", ms=10.0, color=color, mec="white",
                  mew=0.6, zorder=5)
        why = "circuit set" if o.capped_by_circuits else o.binding.label
        ax_b.text(lo * 1.2, y + 0.20,
                  f"{o.prover}  —  $p$ = {o.p:,}  ({why})",
                  fontsize=FIG.font_size - 1, color=color, va="bottom",
                  ha="left", zorder=6)

    # Le domaine mesuré et le plus grand circuit construit coïncident sur ce
    # run ; un seul trait, avec les deux lectures, plutôt que deux superposés.
    ax_b.axvline(nmax, ls="--", lw=0.8, color="0.35", zorder=3)
    same = abs(math.log2(max(nmax, 1)) - math.log2(max(b.max_circuit, 1))) < 1e-9
    note = (f"measured, and largest circuit, $N$ = {nmax:,}" if same
            else f"measured up to $N$ = {nmax:,}")
    ax_b.text(nmax * 0.9, len(opts) - 0.42, note,
              fontsize=FIG.font_size - 2, color="0.35", ha="right", va="top")
    if not same:
        ax_b.axvline(b.max_circuit, ls="-", lw=0.8, color="0.35", zorder=3)
    ax_b.set_xscale("log")
    ax_b.set_xlim(lo, hi)
    # La bande sous la dernière voie est réservée à la légende des marqueurs.
    ax_b.set_ylim(-1.45, len(opts) - 0.25)
    ax_b.set_yticks([])
    ax_b.set_xlabel(r"Batch size $N$ (transactions)")
    ax_b.set_title("(b) which constraint binds", loc="left")
    ax_b.grid(True, axis="x", which="major", ls="--", lw=0.35, alpha=0.6)
    ax_b.set_axisbelow(True)

    marks = [plt.Line2D([], [], ls="none", marker="*", ms=9.0, color="0.35",
                        label=r"$p$ (retained)")]
    for j, lim in enumerate(opts[0].limits):
        ns_j = [o.limits[j].n for o in opts]
        shown = [n for n in ns_j if n is not None and n < hi]
        label = lim.label
        if not shown:
            finite_j = [n for n in ns_j if n is not None]
            label += (f"  (off scale, $\\geq$ {min(finite_j):,.0f})"
                      if finite_j else "  (never reached)")
        marks.append(plt.Line2D([], [], ls="none",
                                marker=LIMIT_MARKERS[lim.key], ms=4.4,
                                color="0.35", label=label))
    marks.append(plt.Line2D([], [], ls="none", marker="o", ms=4.4, color="0.35",
                            mfc="white", mew=0.9, label="hollow = extrapolated"))
    ax_b.legend(handles=marks, loc="lower center", ncol=2,
                fontsize=FIG.font_size - 2, handletextpad=0.4,
                borderpad=0.3, columnspacing=1.2)

    if FIG.titles:
        fig.suptitle("Retained batch size p and the constraints that fix it")
    fig.tight_layout()
    _save(plt, fig, outdir, "03_batch_size_p")


# --------------------------------------------------------------------------- #
# Sorties
# --------------------------------------------------------------------------- #


def write_csv(path: str, els: Sequence[Elasticity]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["phase", "prover", "n", "W_s", "Wbar_s_per_tx",
                    "throughput_tx_s", "gamma", "xi"])
        for e in els:
            s = e.series
            for i, n in enumerate(s.ns):
                w.writerow([s.phase, s.prover, n,
                            f"{s.work[i]:.6g}", f"{e.wbar[i]:.6g}",
                            f"{e.throughput[i]:.6g}",
                            f"{e.gamma[i]:.4f}", f"{e.xi[i]:.4f}"])


def report_optimum(ams: Sequence[Amortization], tolerances: Sequence[float],
                   nmax: int) -> None:
    """Dit, par prover, où placer N — et quand le run ne permet pas de le dire.

    Deux verdicts très différents sont possibles, et les confondre serait la
    faute la plus coûteuse de toute cette analyse :

      - la cible tombe DANS le domaine mesuré : on la lit sur les données ;
      - elle tombe au-delà : c'est une extrapolation de la loi affine, et le
        run ne contient tout simplement pas l'optimum du prover.
    """
    print("\n=== taille de batch optimale, par prover ===")
    print("  xi(N) = rho/(rho+N) et Wbar(N)/w = 1 + rho/N : tout ne dépend "
          "que de rho.")
    for a in ams:
        print(f"\n  {a.prover} : rho = {a.rho:,.0f} tx  "
              f"(W = {a.fit.w0:.4g} + {a.fit.w:.4g}·N, R² = {a.fit.r2:.4f})")
        print(f"    xi mesuré au bord du domaine (N={a.ns[-1]:,}) : "
              f"{a.xi[-1]:+.3f}")
        for eps in sorted(tolerances, reverse=True):
            n_t = a.n_for(eps)
            where = "mesuré" if n_t <= nmax else "EXTRAPOLÉ, hors domaine"
            print(f"    Wbar à {eps:.0%} du plancher w  ->  N = {n_t:,.0f}  "
                  f"[{where}]")
        if a.xi[-1] > 0.02:
            print(f"    -> l'amortissement paie encore au dernier point : "
                  f"l'optimum de ce prover n'est PAS dans ce run")
        else:
            i = min(range(len(a.ns)), key=lambda k: abs(a.xi[k]))
            print(f"    -> xi s'annule vers N = {a.ns[i]:,} : l'optimum est "
                  f"dans le domaine mesuré")


def report(els: Sequence[Elasticity], work_metric: str) -> None:
    for e in els:
        s = e.series
        print(f"\n=== {s.label} ===")
        print(f"  W = {e.fit.w0:.4g} + {e.fit.w:.4g}·N   (R² = {e.fit.r2:.4f})")
        if e.fit.w > 0:
            print(f"  débit asymptotique Λ(∞) = 1/w = {1.0 / e.fit.w:,.0f} tx/s "
                  f"(unité : {work_metric})")
        print(f"  γ mesuré : {e.gamma[0]:+.3f} à N={s.ns[0]}  ->  "
              f"{e.gamma[-1]:+.3f} à N={s.ns[-1]}   [{e.regime}]")
        print(f"  ξ mesuré : {e.xi[0]:+.3f} à N={s.ns[0]}  ->  "
              f"{e.xi[-1]:+.3f} à N={s.ns[-1]}")
        cross = e.crossing_n()
        if cross is not None:
            print(f"  γ franchit 1 vers N ≈ {cross:,.0f} : au-delà, agrandir le "
                  f"batch fait MONTER le coût moyen par transaction")
        gain = e.wbar[0] / e.wbar[-1] if e.wbar[-1] > 0 else float("inf")
        print(f"  coût moyen par tx : {e.wbar[0]:.4g} -> {e.wbar[-1]:.4g} s/tx "
              f"({gain:,.0f}x moins cher)")
        lo, hi = parallelism_spread(s)
        if lo > 0 and hi / lo > 1.25:
            print(f"  [!] R^phys = CPU/mur varie de {lo:.2f} à {hi:.2f} sur la "
                  f"série : ξ et d lnΛ/d lnN ne coïncident qu'approximativement")


def write_p_csv(path: str, opts: Sequence[Optimum], b: Budget) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["prover", "limit", "budget", "n_limit", "binding",
                    "n_allowed", "p", "capped_by_circuits",
                    "xi_at_p", "work_at_p_s", "rss_at_p_gb"])
        for o in opts:
            for lim in o.limits:
                w.writerow([
                    o.prover, lim.key, lim.detail,
                    "" if lim.n is None else f"{lim.n:.0f}",
                    int(lim is o.binding),
                    f"{o.n_allowed:.0f}" if math.isfinite(o.n_allowed) else "",
                    o.p, int(o.capped_by_circuits),
                    f"{o.xi_at_p():.4f}", f"{o.work_at_p():.4g}",
                    f"{o.rss_at_p() / 1e9:.3f}",
                ])


def report_p(opts: Sequence[Optimum], b: Budget, nmax: int) -> None:
    """Dit p, la borne qui l'a fixé, et ce qu'il reste sur la table.

    La distinction qui compte est la même que pour rho : une borne calculée
    DANS le domaine mesuré est une lecture, une borne au-delà est une
    extrapolation du modèle affine. On le marque à chaque ligne, parce qu'un p
    fixé par une extrapolation ne se défend pas de la même façon.
    """
    print("\n=== p : taille de batch retenue ===")
    print("  p = 2^floor(log2(min(bornes))), plafonné par le plus grand "
          "circuit construit")
    print(f"  budgets : xi <= {b.eps:.0%}, RSS <= {b.ram_bytes / 1e9:.0f} GB, "
          f"preuve <= {b.deadline_s:.0f} s, circuits <= {b.max_circuit:,}")
    for o in opts:
        print(f"\n  {o.prover} : p = {o.p:,} tx")
        for lim in o.limits:
            mark = " <-- mord" if lim is o.binding else ""
            if lim.n is None:
                print(f"    {lim.key:<9} {lim.detail:<28} N <= "
                      f"{'jamais atteint':>12}")
                continue
            where = "mesuré" if lim.n <= nmax else "extrapolé"
            print(f"    {lim.key:<9} {lim.detail:<28} N <= {lim.n:>12,.0f}  "
                  f"[{where}]{mark}")
        if o.capped_by_circuits:
            print(f"    -> les bornes autorisent N = {o.n_allowed:,.0f}, mais "
                  f"le jeu de circuits s'arrête à {b.max_circuit:,} : c'est "
                  f"l'implémentation, pas la physique, qui fixe p")
        else:
            print(f"    -> p est fixé par « {o.binding.key} » : c'est cette "
                  f"contrainte qu'il faut desserrer pour agrandir le batch")
        print(f"    en p : Wbar = {1.0 + o.am.rho / o.p:.2f}x le plancher w "
              f"({o.xi_at_p():.1%} de surcoût restant), preuve "
              f"{o.work_at_p():.2f} s, RSS {o.rss_at_p() / 1e9:.1f} GB")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="elasticity.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--from", dest="run_dir",
                   default=os.path.join(REPO_ROOT, "bench-out", "20260825_120620"),
                   help="dossier bench-out/<timestamp> contenant phases.csv")
    p.add_argument("--outdir", default=os.path.join(REPO_ROOT, "bench-out", "elasticity"))
    p.add_argument("--work", choices=("wall", "cpu"), default="wall",
                   help="grandeur prise pour W : secondes de mur (défaut, les "
                        "identités Λ=N/T et ξ=dlnΛ/dlnN sont alors exactes) ou "
                        "secondes CPU (le travail au sens propre)")
    p.add_argument("--phases", nargs="+", default=["prove", "verify"],
                   metavar="PHASE",
                   help="phases à analyser (défaut : prove verify ; 'env' "
                        "ajoute le setup, dont l'échelle écrase les autres)")
    p.add_argument("--tolerance", type=float, nargs="+", default=[0.10, 0.05],
                   metavar="EPS",
                   help="tolérances visées sur le coût moyen : N = rho/eps "
                        "amène Wbar à eps près de son plancher (défaut : "
                        "0.10 0.05)")
    g = p.add_argument_group(
        "p : taille de batch retenue",
        "Les bornes qui empêchent N de croître indéfiniment. Par défaut, "
        "l'échéance de preuve et la mémoire sont lues dans le manifest.json "
        "du run : ce sont les budgets du banc lui-même.")
    g.add_argument("--p-tolerance", type=float, default=0.05, metavar="EPS",
                   help="tolérance retenue pour la borne d'amortissement : "
                        "au-delà de N = rho/EPS le gain sur Wbar est < EPS "
                        "(défaut : 0.05)")
    g.add_argument("--ram-budget", type=float, default=None, metavar="GB",
                   help="mémoire utilisable par le prover (défaut : "
                        "env.mem_total_gb du manifeste, sinon 128)")
    g.add_argument("--deadline", type=float, default=None, metavar="S",
                   help="temps de preuve maximal : en régime permanent le "
                        "rollup doit prouver un batch par période de slot "
                        "(défaut : params.batch_period_s du manifeste, "
                        "sinon 12)")
    g.add_argument("--max-circuit", type=int, default=8192, metavar="N",
                   help="plus grand circuit réellement construit ; p ne peut "
                        "pas le dépasser (défaut : 8192)")

    p.add_argument("--no-merge-verify", action="store_true",
                   help="garde une série verify par prover au lieu de les "
                        "moyenner ; elles se superposent sous le bruit de "
                        "mesure, donc c'est surtout un contrôle")
    p.add_argument("--no-fit", action="store_true",
                   help="n'affiche pas la prédiction du modèle affine W0 + w·N")
    p.add_argument("--fig-format", nargs="+", default=["png", "pdf"], metavar="EXT")
    p.add_argument("--fig-dpi", type=int, default=600)
    p.add_argument("--fig-width", type=float, default=DOUBLE_COLUMN_IN,
                   metavar="IN", help="largeur finale en pouces (défaut 7.16)")
    p.add_argument("--fig-font-size", type=float, default=8.0, metavar="PT")
    p.add_argument("--fig-titles", action="store_true")
    p.add_argument("--no-figures", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    global FIG
    args = build_parser().parse_args(argv)
    FIG = FigureStyle(
        formats=tuple(dict.fromkeys(args.fig_format)),
        dpi=args.fig_dpi,
        width_in=args.fig_width,
        font_size=args.fig_font_size,
        titles=args.fig_titles,
    )

    series = load_phases(args.run_dir, args.work, args.phases)
    print(f"run     : {os.path.relpath(args.run_dir, REPO_ROOT)}")
    if not args.no_merge_verify:
        series = merge_phase(series, "verify")
    els = [analyse(s) for s in series]
    print(f"travail : {args.work}  ({len(els)} séries, "
          f"N = {series[0].ns[0]}..{series[0].ns[-1]})")
    report(els, args.work)

    nmax = max(s.ns[-1] for s in series)
    ams = build_amortization(els)
    if ams:
        report_optimum(ams, args.tolerance, nmax)

    # Les budgets du banc priment sur les valeurs codées en dur ; la ligne de
    # commande prime sur les deux.
    d = run_defaults(args.run_dir)
    budget = Budget(
        eps=args.p_tolerance,
        ram_bytes=(args.ram_budget if args.ram_budget is not None
                   else d.get("ram_gb", 128.0)) * 1e9,
        deadline_s=(args.deadline if args.deadline is not None
                    else d.get("deadline_s", 12.0)),
        max_circuit=args.max_circuit,
    )
    opts = [solve_optimum(a, budget) for a in ams]
    if opts:
        report_p(opts, budget, nmax)

    os.makedirs(args.outdir, exist_ok=True)
    csv_path = os.path.join(args.outdir, "elasticity.csv")
    write_csv(csv_path, els)
    print(f"\n  table : {os.path.relpath(csv_path, REPO_ROOT)}")
    if opts:
        p_csv = os.path.join(args.outdir, "batch_size_p.csv")
        write_p_csv(p_csv, opts, budget)
        print(f"  table : {os.path.relpath(p_csv, REPO_ROOT)}")

    if not args.no_figures:
        print("\n=== figures ===")
        make_figure(args.outdir, els, show_fit=not args.no_fit)
        if ams:
            make_optimum_figure(args.outdir, ams, args.tolerance, nmax)
        if opts:
            make_p_figure(args.outdir, opts, budget, nmax)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
