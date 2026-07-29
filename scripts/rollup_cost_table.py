#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rollup_cost_table.py
====================

Génère un tableau comparatif (LaTeX) du coût en GAS d'une transaction monétaire
selon le taux d'adoption p du ZK rollup, de 1 % à 100 %.

Modèle
------
Le rollup publie ses batchs sur L1 à un COÛT FIXE de G_BATCH gas par batch,
avec une taille de batch UNIQUE de N_B transactions. Le coût L1 amorti par
transaction sur le rollup est donc constant :

    G_rollup = G_BATCH / N_B                         (gas / tx)

À un taux d'adoption p (fraction du trafic monétaire qui migre vers le rollup),
le coût MOYEN d'une transaction monétaire dans l'écosystème mélange le rollup
(pour la fraction p) et Ethereum L1 (pour la fraction 1-p) :

    C(p) = (1 - p) * G_L1 + p * G_rollup             (gas / tx)

On rapporte :
  - C(p)                : coût moyen par tx (gas)
  - réduction           : 1 - C(p)/G_L1
  - facteur G_L1 / C(p) : « x fois moins cher » que le tout-L1

Paramètres par défaut (modifiables en CLI)
------------------------------------------
  G_BATCH = 240000 gas      (coût fixe de publication + vérification d'un batch)
  N_B     = 8192  tx        (taille de batch unique du rollup)
  G_L1    = 21000 gas       (transfert ETH natif standard, = g_tx du papier)
  débit   = 1750  tx/s      (capacité du rollup, pour information/latence)

Sorties
-------
  - stdout : tableau lisible + bloc LaTeX
  - <outdir>/tab_rollup_cost.tex
"""

import argparse
import os
import sys


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--g-batch", type=float, default=240_000.0,
                    help="Coût fixe en gas d'un batch (défaut: 240000)")
    ap.add_argument("--batch-size", type=int, default=8192,
                    help="Taille de batch unique du rollup (défaut: 8192)")
    ap.add_argument("--g-l1", type=float, default=21_000.0,
                    help="Coût gas d'une tx monétaire sur L1 (défaut: 21000)")
    ap.add_argument("--throughput", type=float, default=1750.0,
                    help="Débit du rollup en tx/s (défaut: 1750)")
    ap.add_argument("--l1-throughput", type=float, default=30.0,
                    help="Débit d'Ethereum L1 en tx/s (25-35 ; défaut: 30)")
    ap.add_argument("--percents", type=str,
                    default="1,10,20,30,40,50,60,70,80,90,100",
                    help="Taux d'adoption p (%%) séparés par des virgules.")
    ap.add_argument("--outdir", default=".", help="Dossier de sortie du .tex")
    return ap.parse_args()


def compute_rows(percents, g_batch, batch_size, g_l1, lam_l1, lam_rollup):
    g_rollup = g_batch / batch_size
    rows = []
    for pct in percents:
        p = pct / 100.0
        # Coût gas moyen par tx
        c = (1.0 - p) * g_l1 + p * g_rollup
        reduction = 1.0 - c / g_l1 if g_l1 else None
        factor = g_l1 / c if c else None
        # Temps de traitement moyen par tx : service time 1/débit, mélangé.
        # T(p) = (1-p)/lam_l1 + p/lam_rollup ; baseline = 1/lam_l1.
        # Réduction = 1 - T(p)*lam_l1 = p * (1 - lam_l1/lam_rollup).
        t_reduction = p * (1.0 - lam_l1 / lam_rollup) if lam_rollup else None
        rows.append({
            "pct": pct,
            "C_gas": c,
            "reduction": reduction,
            "factor": factor,
            "t_reduction": t_reduction,
        })
    return rows, g_rollup


# --------------------------------------------------------------------------- #
# Formatage                                                                     #
# --------------------------------------------------------------------------- #
def fmt_gas(x):
    """Gas lisible : 2 décimales si < 100, sinon entier à séparateur milliers."""
    if x < 100:
        return f"{x:,.2f}".replace(",", r"{,}")
    return f"{x:,.0f}".replace(",", r"{,}")


def fmt_factor(x):
    if x is None:
        return "--"
    if x >= 100:
        return rf"{x:,.0f}\times".replace(",", r"{,}")
    if x >= 10:
        return rf"{x:.1f}\times"
    return rf"{x:.3f}\times"


def latex_table(rows, g_rollup, g_batch, batch_size, g_l1, throughput,
                l1_throughput):
    speedup = throughput / l1_throughput if l1_throughput else float("nan")
    head = [
        r"\begin{table}[t]",
        r"\centering",
        r"\caption{Ecosystem-average per-transaction gas cost $C(p) = "
        r"(1-p)\,G_{L1} + p\,G_{\mathrm{rollup}}$ and processing-time reduction "
        rf"vs.\ rollup adoption $p$. Rollup: ${fmt_gas(g_batch)}$ gas per batch "
        rf"of $N_b = {batch_size}$ ($G_{{\mathrm{{rollup}}}} \approx "
        rf"{g_rollup:.1f}$ gas/tx, ${throughput:.0f}$ tx/s). L1 baseline: native "
        rf"ETH transfer (${fmt_gas(g_l1)}$ gas, ${l1_throughput:.0f}$ tx/s), "
        rf"i.e.\ ${speedup:.0f}\times$ slower.}}",
        r"\label{tab:rollup_cost}",
        r"\renewcommand{\arraystretch}{1.2}",
        r"\begin{tabular}{|c|c|c|c|}",
        r"\hline",
        r"\textbf{Adoption $p$} & \textbf{$C(p)$ (gas/tx)} & "
        r"\textbf{Cost reduction} & \textbf{Time reduction} \\",
        r"\hline",
    ]
    body = []
    for r in rows:
        body.append(
            f"{r['pct']:g}\\% & "
            f"${fmt_gas(r['C_gas'])}$ & "
            f"{r['reduction']*100:.2f}\\% & "
            f"{r['t_reduction']*100:.2f}\\% \\\\ \\hline"
        )
    tail = [r"\end{tabular}", r"\end{table}"]
    return "\n".join(head + body + tail)


def main():
    args = parse_args()
    try:
        percents = [float(x) for x in args.percents.split(",") if x.strip()]
    except ValueError:
        sys.exit("--percents doit être une liste de nombres séparés par ','")

    rows, g_rollup = compute_rows(percents, args.g_batch, args.batch_size,
                                  args.g_l1, args.l1_throughput,
                                  args.throughput)

    os.makedirs(args.outdir, exist_ok=True)
    tex = latex_table(rows, g_rollup, args.g_batch, args.batch_size,
                      args.g_l1, args.throughput, args.l1_throughput)
    tex_path = os.path.join(args.outdir, "tab_rollup_cost.tex")
    with open(tex_path, "w", encoding="utf-8") as f:
        f.write(tex + "\n")

    # Récapitulatif console
    print("=" * 60)
    print("  Coût gas par transaction monétaire vs adoption du rollup")
    print("=" * 60)
    print(f"  G_batch (fixe/batch)   : {args.g_batch:,.0f} gas")
    print(f"  N_b (taille de batch)  : {args.batch_size}")
    print(f"  G_rollup (amorti/tx)   : {g_rollup:.4f} gas")
    print(f"  G_L1 (baseline ETH)    : {args.g_l1:,.0f} gas")
    print(f"  Débit rollup           : {args.throughput:,.0f} tx/s "
          f"(batch plein en {args.batch_size/args.throughput:.2f} s)")
    print(f"  Débit L1               : {args.l1_throughput:,.0f} tx/s "
          f"(x{args.throughput/args.l1_throughput:.0f} plus lent)")
    print("-" * 60)
    print(f"  {'p':>5} | {'C(p) gas/tx':>14} | {'réd. coût':>10} | "
          f"{'réd. temps':>10}")
    print("-" * 60)
    for r in rows:
        print(f"  {r['pct']:>4.0f}% | {r['C_gas']:>14,.2f} | "
              f"{r['reduction']*100:>9.2f}% | {r['t_reduction']*100:>9.2f}%")
    print("-" * 60)
    print(f"  LaTeX -> {tex_path}\n")
    print("----- tab:rollup_cost -----\n")
    print(tex)


if __name__ == "__main__":
    main()
