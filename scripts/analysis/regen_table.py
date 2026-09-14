#!/usr/bin/env python3
"""Regenerate Table III (migration table) and all derived quantities of Sec. VI
from measured market parameters, using the raw Rapidsnark benchmark means.

Usage: edit MEASURED below with the June-2025 window values, then run:
    python3 regen_table3.py
Outputs LaTeX rows for Table III plus every number quoted in the text
(break-even, ratio 15.3/N_b, lambda_crit, prover utilization, crossover table,
abstract percentages).
"""
import math

# ----------------------------------------------------------------------------
# 1) MEASURED window parameters  (placeholders = May 2025 values; REPLACE with
#    the June 2025 measurements)
MEASURED = dict(
    lambda_eth = 10,        # tx/s, native ETH + ERC-20 transfers
    P_gas      = 1.249,      # gwei, median base fee
    P_blob     = 1.0e-9,     # gwei/byte, median blob base fee
    eth_usd    = 2500.0,     # period average
    g_avg      = 21_000.0,  # measured avg gas/tx (Sigma gasUsed / Sigma txCount)
)

# ----------------------------------------------------------------------------
# 2) Model constants (paper, post-revision)
G_TX      = 21_000          # native transfer, protocol constant
G_SET     = 321_000         # settlement tx: 300k verification+submission + 21k intrinsic
S0_DA     = 512             # bytes, per-batch overhead
S_TX_DA   = 16              # bytes, compressed state diff
RHO       = 170             # gwei/s, prover hardware cost
DELTA_B   = 300             # s, publication interval
NB_MAX    = 8192
B_BLOB    = 131_072         # bytes per blob
N_TGT     = 6               # Pectra blob target (uniform over June 2025)
T_BLOCK   = 12
G_LIMIT   = 36_000_000      # gas limit over the window
G_TARGET  = 18_000_000      # EIP-1559 target

# Rapidsnark mean proving times (s) from the 100-trial benchmark
RAPIDSNARK = {1:0.19823, 2:0.19887, 4:0.20154, 8:0.20533, 16:0.21225,
              32:0.22510, 64:0.24246, 128:0.27673, 256:0.34933, 512:0.48453,
              1024:0.78969, 2048:1.34111, 4096:2.43658, 8192:4.67257}

def t_proof(n):
    """Log-log interpolation of measured Rapidsnark means."""
    ns = sorted(RAPIDSNARK)
    if n <= ns[0]: return RAPIDSNARK[ns[0]]
    if n >= ns[-1]:
        k = math.log(RAPIDSNARK[ns[-1]]/RAPIDSNARK[ns[-2]]) / math.log(ns[-1]/ns[-2])
        return RAPIDSNARK[ns[-1]] * (n/ns[-1])**k
    lo = max(x for x in ns if x <= n); hi = min(x for x in ns if x >= n)
    if lo == hi: return RAPIDSNARK[lo]
    k = math.log(RAPIDSNARK[hi]/RAPIDSNARK[lo]) / math.log(hi/lo)
    return RAPIDSNARK[lo] * (n/lo)**k

def main(m=MEASURED):
    gwei_usd = m["eth_usd"] * 1e-9
    F_L1 = G_TX * m["P_gas"]                       # gwei
    print(f"=== L1 baseline ===")
    print(f"F_L1 = {F_L1:,.0f} gwei = {F_L1*gwei_usd:.4f} USD")
    print(f"L1 theoretical bound: N_b={G_LIMIT/G_TX:.0f}, TPS={G_LIMIT/G_TX/T_BLOCK:.1f}")
    print(f"L1 practical (g_avg={m['g_avg']:.0f}): N_b={G_TARGET/m['g_avg']:.0f}, "
          f"TPS={G_TARGET/m['g_avg']/T_BLOCK:.1f}")

    print(f"\n=== Derived constants ===")
    print(f"break-even: N_b > g_set/g_tx = {G_SET/G_TX:.1f}  -> batch of {math.floor(G_SET/G_TX)+1}")
    lam_crit = N_TGT * B_BLOB / (S_TX_DA * T_BLOCK)
    print(f"lambda_crit = {lam_crit:.0f} tx/s (n_tgt={N_TGT})  "
          f"= {lam_crit/m['lambda_eth']:.0f}x the monetary workload")
    print(f"DA ceiling = 4096*alpha TPS ; proving ceiling = "
          f"{NB_MAX/t_proof(NB_MAX):.0f} TPS ; alpha* = "
          f"{(NB_MAX/t_proof(NB_MAX))*S_TX_DA*T_BLOCK/(N_TGT*B_BLOB):.2f}")

    print(f"\n=== Table III rows (LaTeX) ===")
    rows = []
    for pct in (1, 10, 25, 50, 75, 100):
        p = pct/100
        lam = p * m["lambda_eth"]
        Nb = min(int(lam*DELTA_B), NB_MAX)
        if Nb < 1: Nb = 1
        cL1 = (G_SET*m["P_gas"] + S0_DA*m["P_blob"])/Nb + S_TX_DA*m["P_blob"]
        c_prove = RHO * t_proof(Nb)/Nb
        r = cL1 + c_prove
        red = (1 - r/F_L1)*100
        blobs = lam * S_TX_DA * T_BLOCK / B_BLOB
        usd = r*gwei_usd
        if usd >= 1e-3:
            usd_s = f"{usd:.3f}"
        else:
            mant, expo = f"{usd:.1e}".split("e")
            usd_s = f"${mant}\\cdot10^{{{int(expo)}}}$"
        rows.append((pct, lam, Nb, cL1, r, usd, red, blobs))
        print(f"{pct}\\% & {lam:.1f} & {Nb:,} & {cL1:,.0f} & {r:,.0f} & "
              f"{usd_s} & {red:.1f}\\% & ${blobs:.1e}$ \\\\ \\hline"
              .replace(",", "{,}"))

    print(f"\n=== Text values ===")
    _, _, Nb1, _, r1, _, red1, _ = rows[0]
    _, _, Nb100, _, r100, _, red100, blobs100 = rows[-1]
    print(f"abstract/intro: reductions {red1:.0f}%--{red100:.1f}%  "
          f"(x{F_L1/r100:.0f} at p=100%, x{F_L1/r1:.1f} at p=1%)")
    print(f"ratio check: g_set/(Nb*g_tx) at p=100%: {G_SET/(Nb100*G_TX):.4f} "
          f"vs r/F = {r100/F_L1:.4f}")
    print(f"T_proof(N_b(1)={Nb100}) = {t_proof(Nb100):.2f} s  "
          f"-> prover utilization {t_proof(Nb100)/DELTA_B*100:.2f}% of Delta_b")
    print(f"blob demand at p=100%: {blobs100:.3f} blobs/block "
          f"= {blobs100/N_TGT*100:.2f}% of target")

    print(f"\n=== Crossover table N*(alpha), n_tgt={N_TGT} ===")
    for alpha in (1.0, 0.5, 0.43, 0.2, 0.1, 0.05, 0.01):
        R = alpha * N_TGT * B_BLOB / T_BLOCK
        cross, prev = None, None
        for n in range(1, 200001):
            d = (S0_DA + S_TX_DA*n)/R - t_proof(n)
            if prev is not None and prev < 0 <= d:
                cross = n; break
            prev = d
        regime = ("proof-bound everywhere" if cross is None and prev < 0 else
                  "DA-bound everywhere" if cross is None else f"N* = {cross}")
        print(f"alpha={alpha:5.2f}  R_DA={R:9.0f} B/s  {regime}")

if __name__ == "__main__":
    main()