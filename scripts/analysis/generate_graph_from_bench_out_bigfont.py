#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
plot_zksnark_bench_bigfont.py

Scientific visualization of zk-SNARK benchmarking results.

Same plots as generate_graph_from_bench_out.py, but with much larger
fonts for titles, axis labels and legends (presentation / poster friendly).

Features:
- Loads JSON outputs from benchmarking script
- Generates multiple analytical plots:
    * Latency vs circuit size
    * Throughput vs circuit size
    * Time per transaction vs circuit size
- Uses scienceplots style for publication-quality figures
- Log-scale visualization for asymptotic behavior analysis
"""

import json
import os
import matplotlib.pyplot as plt
import matplotlib as mpl
import scienceplots

# ---------- Configuration ----------
INPUT_DIR = "bench-out/20260301_113715/raw"

BASE_DIR = os.path.dirname(INPUT_DIR)
OUTPUT_DIR = os.path.join(BASE_DIR, "graph_bigfont")

os.makedirs(OUTPUT_DIR, exist_ok=True)

print(f"\n>>> Input directory : {INPUT_DIR}")
print(f">>> Output directory: {OUTPUT_DIR}")

# ---------- Style ----------
plt.style.use(["science"])
mpl.rcParams["text.usetex"] = False
mpl.rcParams["mathtext.fontset"] = "dejavusans"
mpl.rcParams["font.family"] = "DejaVu Sans"

# ---------- Big fonts ----------
# Much larger typography for titles, axis names and legend.
mpl.rcParams["font.size"] = 20          # base font size
mpl.rcParams["axes.titlesize"] = 28     # plot title
mpl.rcParams["axes.labelsize"] = 24     # x / y axis labels
mpl.rcParams["xtick.labelsize"] = 18    # x tick labels
mpl.rcParams["ytick.labelsize"] = 18    # y tick labels
mpl.rcParams["legend.fontsize"] = 20    # legend text
mpl.rcParams["legend.title_fontsize"] = 22
mpl.rcParams["lines.linewidth"] = 2.5   # thicker lines to match
mpl.rcParams["lines.markersize"] = 9    # bigger markers

# ---------- Load data ----------
def load_json(path):
    with open(path, "r") as f:
        return json.load(f)

rapidsnark = load_json(os.path.join(INPUT_DIR, "rapidsnark_timings.json"))
snarkjs = load_json(os.path.join(INPUT_DIR, "snarkjs_timings.json"))
verify = load_json(os.path.join(INPUT_DIR, "snarkjs_verify_timings.json"))

# ---------- Extract ----------
def extract(results):
    n = [r["n"] for r in results]
    mean = [r["mean"] for r in results]
    thr = [r.get("thr_mean", None) for r in results]
    tpt = [r["mean"] / r["n"] for r in results]  # time per tx
    return n, mean, thr, tpt

n_r, mean_r, thr_r, tpt_r = extract(rapidsnark)
n_s, mean_s, thr_s, tpt_s = extract(snarkjs)
n_v, mean_v, _, _ = extract(verify)

# ---------- Scaling Efficiency ----------
def compute_scaling_efficiency(n, thr):
    thr0 = thr[0]
    return [t / (ni * thr0) for ni, t in zip(n, thr)]

eff_r = compute_scaling_efficiency(n_r, thr_r)
eff_s = compute_scaling_efficiency(n_s, thr_s)

# ==========================================================
# 1. Latency vs Circuit Size
# ==========================================================
plt.figure(figsize=(12, 8))

plt.plot(n_r, mean_r, marker="o", label="Rapidsnark (Prover)")
plt.plot(n_s, mean_s, marker="^", label="SnarkJS (Prover)")
plt.plot(n_v, mean_v, marker="s", label="SnarkJS (Verifier)")

plt.xscale("log", base=2)
plt.yscale("log")

plt.xlabel("Circuit Size (number of transactions)")
plt.ylabel("Execution Time (seconds)")
plt.title("Latency Scaling of zk-SNARK Operations")

plt.legend()
plt.grid(True, which="both", ls="--", lw=0.5)
plt.tight_layout()

plt.savefig(os.path.join(OUTPUT_DIR, "latency_vs_circuit_size.png"), dpi=300)
plt.savefig(os.path.join(OUTPUT_DIR, "latency_vs_circuit_size.svg"))

# ==========================================================
# 2. Throughput vs Circuit Size
# ==========================================================
plt.figure(figsize=(12, 8))

plt.plot(n_r, thr_r, marker="o", label="Rapidsnark Throughput")
plt.plot(n_s, thr_s, marker="^", label="SnarkJS Throughput")

plt.xscale("log", base=2)

plt.xlabel("Circuit Size (number of transactions)")
plt.ylabel("Throughput (transactions per second)")
plt.title("Throughput Scaling with Circuit Size")

plt.legend()
plt.grid(True, which="both", ls="--", lw=0.5)
plt.tight_layout()

plt.savefig(os.path.join(OUTPUT_DIR, "throughput_vs_circuit_size.png"), dpi=300)
plt.savefig(os.path.join(OUTPUT_DIR, "throughput_vs_circuit_size.svg"))

# ==========================================================
# 3. Time per Transaction (Amortized Cost)
# ==========================================================
plt.figure(figsize=(12, 8))

plt.plot(n_r, tpt_r, marker="o", label="Rapidsnark")
plt.plot(n_s, tpt_s, marker="^", label="SnarkJS")

plt.xscale("log", base=2)
plt.yscale("log")

plt.xlabel("Circuit Size (number of transactions)")
plt.ylabel("Time per Transaction (seconds)")
plt.title("Amortized Cost per Transaction")

plt.legend()
plt.grid(True, which="both", ls="--", lw=0.5)
plt.tight_layout()

plt.savefig(os.path.join(OUTPUT_DIR, "time_per_tx_vs_circuit_size.png"), dpi=300)
plt.savefig(os.path.join(OUTPUT_DIR, "time_per_tx_vs_circuit_size.svg"))

# ==========================================================
# 4. Scaling Efficiency (Plateau Detection)
# ==========================================================
plt.figure(figsize=(12, 8))

plt.plot(n_r, eff_r, marker="o", label="Rapidsnark Efficiency")
plt.plot(n_s, eff_s, marker="^", label="SnarkJS Efficiency")

plt.xscale("log", base=2)

plt.axhline(1.0, linestyle="--", linewidth=1, label="Ideal linear scaling")

plt.xlabel("Circuit Size (number of transactions)")
plt.ylabel("Scaling Efficiency")
plt.title("Scaling Efficiency and Saturation Detection")

plt.legend()
plt.grid(True, which="both", ls="--", lw=0.5)
plt.tight_layout()

plt.savefig(os.path.join(OUTPUT_DIR, "scaling_efficiency.png"), dpi=300)
plt.savefig(os.path.join(OUTPUT_DIR, "scaling_efficiency.svg"))

# ==========================================================
# Show all
# ==========================================================
plt.show()

print(f"\n>>> Figures saved in: {OUTPUT_DIR}")
