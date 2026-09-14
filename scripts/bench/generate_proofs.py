#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_proofs_bench.py

Benchmark generation & verification de preuves avec:
 - rapidsnark (via docker call)
 - snarkjs

Caractéristiques:
 - options pour calculer médiane / écart-type (désactivées par défaut)
 - calcul du throughput optionnel
 - sauvegarde CSV/JSON/figures
 - affichage console dynamique selon les métriques effectivement calculées
"""

import time
import statistics
import os
import json
import csv
from typing import Callable, List, Dict, Any
from datetime import datetime
import platform
import getpass

import matplotlib as mpl
import matplotlib.pyplot as plt
import scienceplots

# ---------- Commandes de génération / vérification ----------


def generate_proofs_rapidsnark(size: int) -> None:
    os.system(
        f"snarkjs wtns calculate ./circuits/{size}/circuit_js/circuit.wasm "
        f"./circuits/{size}/input.json ./circuits/{size}/witness.wtns"
    )
    os.system(
        f"docker exec debian_rapidsnark "
        f"mnt/projet/rapidsnark/package/bin/prover "
        f"mnt/projet/{size}/circuit_final.zkey "
        f"mnt/projet/{size}/witness.wtns "
        f"mnt/projet/{size}/proof.json "
        f"mnt/projet/{size}/public.json"
    )

def generate_proofs_snarkjs(size: int) -> None:
    os.system(
        f"snarkjs groth16 fullprove "
        f"./circuits/{size}/input.json "
        f"./circuits/{size}/circuit_js/circuit.wasm "
        f"./circuits/{size}/circuit_final.zkey "
        f"./circuits/{size}/proof.json "
        f"./circuits/{size}/public.json"
    )

def verify_proofs(size: int) -> None:
    os.system(
        f"snarkjs groth16 verify "
        f"./circuits/{size}/verification_key.json "
        f"./circuits/{size}/public.json "
        f"./circuits/{size}/proof.json"
    )


# ---------- Paramètres ----------
attempts = 25
sizes = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192]
sleep_between = 5.0
warmup = True

# ---------- I/O utilitaires ----------
def make_outdir(root: str = "./bench-out") -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(root, ts)
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(os.path.join(outdir, "figs"), exist_ok=True)
    os.makedirs(os.path.join(outdir, "raw"), exist_ok=True)
    return outdir

def save_csv(path: str, rows: List[Dict[str, object]], fieldnames: List[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

def save_json(path: str, obj: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

# ---------- Benchmark ----------
def benchmark(
    sizes: List[int],
    attempts: int,
    fn: Callable[[int], None],
    label: str,
    sleep_between: float = 1.0,
    warmup: bool = True,
    compute_thr: bool = True,
    compute_median: bool = False,
    compute_stdev: bool = False,
) -> List[Dict[str, object]]:
    """
    Retourne une liste de dicts par taille. Les clés 'median' et 'stdev' ne sont
    présentes que si demandées. Les clés thr_* ne sont présentes que si calculées.
    """
    results: List[Dict[str, object]] = []
    for n in sizes:
        timings: List[float] = []

        if warmup:
            try:
                fn(n)
            except Exception:
                # ignore errors during warmup
                pass

        for i in range(attempts):
            start = time.perf_counter()
            fn(n)
            end = time.perf_counter()
            dt = end - start
            timings.append(dt)
            print(f"{label}: exécution pour n={n} en {dt:.6f} s ; essai {i+1}/{attempts}")
            if sleep_between > 0:
                time.sleep(sleep_between)

        mean_t = statistics.mean(timings)
        med_t = statistics.median(timings) if compute_median else None
        std_t = statistics.pstdev(timings) if (compute_stdev and len(timings) > 1) else None

        result: Dict[str, Any] = {"n": n, "mean": mean_t, "timings": timings}

        if compute_median:
            result["median"] = med_t
        if compute_stdev:
            result["stdev"] = std_t

        if compute_thr:
            result["thr_mean"] = n / mean_t if mean_t > 0 else float("inf")
            if compute_median and (med_t is not None) and med_t > 0:
                result["thr_median"] = n / med_t
            # else: don't include thr_median

        results.append(result)

    return results

# ---------- Utilitaires d'inspection des résultats ----------
def _detect_result_flags(results: List[Dict[str, object]]) -> Dict[str, bool]:
    """Détecte si 'median', 'stdev', 'thr_mean' et 'thr_median' sont présentes/valables."""
    if not results:
        return {"median": False, "stdev": False, "thr_mean": False, "thr_median": False}
    median_present = any(r.get("median") is not None for r in results)
    stdev_present = any(r.get("stdev") is not None for r in results)
    thr_mean_present = any(("thr_mean" in r) and (r.get("thr_mean") is not None) for r in results)
    thr_median_present = any(("thr_median" in r) and (r.get("thr_median") is not None) for r in results)
    return {
        "median": median_present,
        "stdev": stdev_present,
        "thr_mean": thr_mean_present,
        "thr_median": thr_median_present,
    }

# ---------- Exécution ----------
outdir = make_outdir()

results_rapidsnark = benchmark(
    sizes,
    attempts,
    generate_proofs_rapidsnark,
    "Rapidsnark",
    sleep_between=sleep_between,
    warmup=warmup,
    compute_thr=True,
    compute_median=False,
    compute_stdev=False,
)

results_snarkjs = benchmark(
    sizes,
    attempts,
    generate_proofs_snarkjs,
    "SnarkJS",
    sleep_between=sleep_between,
    warmup=warmup,
    compute_thr=True,
    compute_median=False,
    compute_stdev=False,
)

results_snarkjs_verify = benchmark(
    sizes,
    attempts,
    verify_proofs,
    "SnarkJS Verify",
    sleep_between=sleep_between,
    warmup=False,
    compute_thr=False,
    compute_median=False,
    compute_stdev=False,
)

# ---------- Affichages console ----------
def print_table(title: str, results: List[Dict[str, object]]) -> None:
    flags = _detect_result_flags(results)
    median_present = flags["median"]
    stdev_present = flags["stdev"]
    thr_present = flags["thr_mean"] or flags["thr_median"]

    print(f"\n{title}")

    # Header construction dynamique
    header_cols = ["Taille", "Moyenne (s)"]
    if median_present:
        header_cols.append("Médiane (s)")
    if stdev_present:
        header_cols.append("Écart-type (s)")
    if thr_present:
        if flags["thr_mean"]:
            header_cols.append("Débit moy. (tx/s)")
        if flags["thr_median"]:
            header_cols.append("Débit méd. (tx/s)")

    # print header line
    print(" | ".join(header_cols))
    print("-" * max(40, len(" | ".join(header_cols)) + 5))

    for r in results:
        pieces = [f"{r['n']:5d}", f"{r['mean']:12.6f}"]
        if median_present:
            med = r.get("median")
            pieces.append(f"{med:12.6f}" if (med is not None) else " " * 12)
        if stdev_present:
            st = r.get("stdev")
            pieces.append(f"{st:10.6f}" if (st is not None) else " " * 10)
        if thr_present:
            if flags["thr_mean"]:
                pieces.append(f"{r.get('thr_mean', float('nan')):16.6f}" if r.get("thr_mean") is not None else " " * 16)
            if flags["thr_median"]:
                pieces.append(f"{r.get('thr_median', float('nan')):16.6f}" if r.get("thr_median") is not None else " " * 16)
        print(" | ".join(pieces))

print_table("Rapidsnark — Génération de preuves", results_rapidsnark)
print_table("SnarkJS — Génération de preuves", results_snarkjs)
print_table("SnarkJS — Vérification des preuves", results_snarkjs_verify)

# ---------- Sauvegardes (CSV/JSON) ----------
# Timings bruts
save_json(os.path.join(outdir, "raw", "rapidsnark_timings.json"), results_rapidsnark)
save_json(os.path.join(outdir, "raw", "snarkjs_timings.json"), results_snarkjs)
save_json(os.path.join(outdir, "raw", "snarkjs_verify_timings.json"), results_snarkjs_verify)

# Agrégats CSV dynamiques
def to_rows(results: List[Dict[str, object]]) -> List[Dict[str, object]]:
    rows = []
    for r in results:
        row: Dict[str, object] = {"n": r["n"], "mean_s": r["mean"]}
        if "median" in r:
            row["median_s"] = r.get("median")
        if "stdev" in r:
            row["stdev_s"] = r.get("stdev")
        if "thr_mean" in r:
            row["throughput_mean_tx_per_s"] = r.get("thr_mean")
        if "thr_median" in r:
            row["throughput_median_tx_per_s"] = r.get("thr_median")
        rows.append(row)
    return rows

def csv_fieldnames_from_rows(rows: List[Dict[str, object]]) -> List[str]:
    if not rows:
        return ["n", "mean_s"]
    keys = set()
    for r in rows:
        keys.update(r.keys())
    ordered = []
    for k in ["n", "mean_s", "median_s", "stdev_s", "throughput_mean_tx_per_s", "throughput_median_tx_per_s"]:
        if k in keys:
            ordered.append(k)
    return ordered

# Save CSVs using dynamic fields
rows_rapids = to_rows(results_rapidsnark)
fields_rapids = csv_fieldnames_from_rows(rows_rapids)
save_csv(os.path.join(outdir, "rapidsnark_generate_summary.csv"), rows_rapids, fields_rapids)

rows_snarkjs = to_rows(results_snarkjs)
fields_snarkjs = csv_fieldnames_from_rows(rows_snarkjs)
save_csv(os.path.join(outdir, "snarkjs_generate_summary.csv"), rows_snarkjs, fields_snarkjs)

rows_verify = to_rows(results_snarkjs_verify)
fields_verify = csv_fieldnames_from_rows(rows_verify)
save_csv(os.path.join(outdir, "snarkjs_verify_summary.csv"), rows_verify, fields_verify)


# Manifest / contexte d’exécution
manifest = {
    "timestamp": datetime.now().isoformat(),
    "outdir": outdir,
    "params": {
        "attempts": attempts,
        "sizes": sizes,
        "sleep_between_s": sleep_between,
        "warmup": warmup,
    },
    "env": {
        "user": getpass.getuser(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
    },
    "artifacts": {
        "figures": {},
        "csv": {
            "rapidsnark_generate_summary": os.path.basename(os.path.join(outdir, "rapidsnark_generate_summary.csv")),
            "snarkjs_generate_summary": os.path.basename(os.path.join(outdir, "snarkjs_generate_summary.csv")),
            "snarkjs_verify_summary": os.path.basename(os.path.join(outdir, "snarkjs_verify_summary.csv")),
        },
        "raw_json": {
            "rapidsnark_timings": "raw/rapidsnark_timings.json",
            "snarkjs_timings": "raw/snarkjs_timings.json",
            "snarkjs_verify_timings": "raw/snarkjs_verify_timings.json",
        },
    },
}

# ---------- Graphiques ----------
plt.style.use(["science"])
mpl.rcParams["text.usetex"] = False
mpl.rcParams["mathtext.fontset"] = "dejavusans"
mpl.rcParams["font.family"] = "DejaVu Sans"

plt.figure(figsize=(10, 6))

# Moyennes (toujours)
plt.plot([r["n"] for r in results_rapidsnark], [r["mean"] for r in results_rapidsnark], marker="o", label="Rapidsnark — moyenne")
plt.plot([r["n"] for r in results_snarkjs], [r["mean"] for r in results_snarkjs], marker="^", label="SnarkJS — moyenne")
plt.plot([r["n"] for r in results_snarkjs_verify], [r["mean"] for r in results_snarkjs_verify], marker="s", label="SnarkJS Verify — moyenne")

# Médianes (uniquement si présentes)
flags_rapids = _detect_result_flags(results_rapidsnark)
flags_snarkjs = _detect_result_flags(results_snarkjs)
flags_verify = _detect_result_flags(results_snarkjs_verify)

if flags_rapids["median"]:
    plt.plot([r["n"] for r in results_rapidsnark], [r["median"] for r in results_rapidsnark], linestyle="--", marker="o", label="Rapidsnark — médiane")
if flags_snarkjs["median"]:
    plt.plot([r["n"] for r in results_snarkjs], [r["median"] for r in results_snarkjs], linestyle="--", marker="^", label="SnarkJS — médiane")
if flags_verify["median"]:
    plt.plot([r["n"] for r in results_snarkjs_verify], [r["median"] for r in results_snarkjs_verify], linestyle="--", marker="s", label="SnarkJS Verify — médiane")

plt.xscale("log", base=2)
plt.xlabel("Taille du batch (transactions)")
plt.ylabel("Temps (s)")
plt.title("Rapidsnark vs SnarkJS — moyennes et (éventuelles) statistiques")
plt.legend()
plt.grid(True, which="both", ls="--", lw=0.5)
plt.tight_layout()

fig_png = os.path.join(outdir, "figs", "rapidsnark_vs_snarkjs.png")
fig_svg = os.path.join(outdir, "figs", "rapidsnark_vs_snarkjs.svg")
plt.savefig(fig_png, dpi=200)
plt.savefig(fig_svg)
manifest["artifacts"]["figures"]["summary_png"] = os.path.relpath(fig_png, outdir)
manifest["artifacts"]["figures"]["summary_svg"] = os.path.relpath(fig_svg, outdir)

plt.show()

# Sauvegarde du manifest
save_json(os.path.join(outdir, "manifest.json"), manifest)

print(f"\n>>> Sorties enregistrées dans: {outdir}")
print(f"    - CSV: {fields_rapids}, {fields_snarkjs}, {fields_verify}")
print("    - JSON bruts: raw/*.json")
print("    - Figures: figs/*.png, *.svg")
print("    - Manifest: manifest.json")

print("\n>>> Fin du script")