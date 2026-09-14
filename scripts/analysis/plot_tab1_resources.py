#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
plot_tab1_resources.py

Builds publication-quality figures from the LaTeX table
`tables/tab1_resources.tex` produced by measure_zk_resources.py.

The table is parsed directly (no re-run of the benchmark needed), so the
figure and the table in the paper can never drift apart.

Outputs (PDF for LaTeX inclusion + PNG for quick preview), in <bench-run>/figs/:
    fig_tab1_resources.{pdf,png}        4-panel summary (time / RAM / cores / I/O)
    fig_tab1_time.{pdf,png}             time vs batch size (standalone)
    fig_tab1_ram.{pdf,png}              peak RAM vs batch size (standalone)
    fig_tab1_setup_breakdown.{pdf,png}  setup split into its three sub-steps
    tab1_resources.csv                  tidy version of the parsed table

Usage:
    python scripts/analysis/plot_tab1_resources.py
    python scripts/analysis/plot_tab1_resources.py --table bench-out/<run>/tables/tab1_resources.tex
    python scripts/analysis/plot_tab1_resources.py --outdir figs --bigfont
"""

import argparse
import csv
import os
import re
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, LogLocator

DEFAULT_TABLE = "bench-out/20260825_120620/tables/tab1_resources.tex"

# Canonical phase names, in plotting order, with their style.
# Keys are the (normalised) labels found in the LaTeX table.
PHASES = [
    ("Setup (one-off)",                 "Setup (one-off)",         "#0072B2", "o", "-"),
    ("Phase 1 (powers of tau)",         "Phase 1 (powers of tau)", "#56B4E9", "^", "--"),
    ("Circuit compilation (circom)",    "Circuit compilation",     "#009E73", "s", "--"),
    ("Phase 2 (circuit-specific zkey)", "Phase 2 (zkey)",          "#CC79A7", "D", "--"),
    ("Proof generation (snarkjs)",      "Proving (snarkjs)",       "#D55E00", "v", "-"),
    ("Proof generation (rapidsnark)",   "Proving (rapidsnark)",    "#E69F00", "P", "-"),
    ("Verification",                    "Verification",            "#000000", "X", "-"),
]
STYLE = {k: (lab, c, m, ls) for k, lab, c, m, ls in PHASES}
ORDER = [k for k, *_ in PHASES]

# Phases shown on the "top level" panels (setup sub-steps excluded).
TOP_LEVEL = [
    "Setup (one-off)",
    "Proof generation (snarkjs)",
    "Proof generation (rapidsnark)",
    "Verification",
]
SETUP_SUB = [
    "Phase 1 (powers of tau)",
    "Circuit compilation (circom)",
    "Phase 2 (circuit-specific zkey)",
]

_UNITS = {"B": 1.0, "KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40}
GIB = 2**30


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _clean(cell):
    """Strip LaTeX decorations from a table cell."""
    s = cell.strip()
    s = s.replace(r"\quad", " ").replace(r"\,", " ").replace("~", " ")
    s = re.sub(r"\\textbf\{(.*?)\}", r"\1", s)
    s = re.sub(r"\$(.*?)\$", r"\1", s)
    s = s.replace("{", "").replace("}", "")
    return " ".join(s.split())


def _to_bytes(s):
    """'1.27 GiB' -> float bytes. Returns None if unparsable."""
    m = re.match(r"^([\d.]+)\s*(B|KiB|MiB|GiB|TiB)$", s)
    if not m:
        return None
    return float(m.group(1)) * _UNITS[m.group(2)]


def parse_table(path):
    """Parse tab1_resources.tex into a list of dicts (one per data row)."""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    rows = []
    current_nb = None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("%") or "&" not in line:
            continue

        body = line.split("\\\\")[0]
        cells = [_clean(c) for c in body.split("&")]
        if len(cells) != 6:
            continue
        if "Time" in cells[2]:
            continue  # header row

        if cells[0]:
            try:
                current_nb = int(cells[0])
            except ValueError:
                continue
        if current_nb is None:
            continue

        phase = cells[1]
        if phase not in STYLE:
            continue

        io = cells[5].split("/")
        rows.append({
            "n_b": current_nb,
            "phase": phase,
            "time_s": float(cells[2]),
            "cores": float(cells[3]),
            "peak_ram_b": _to_bytes(cells[4]),
            "io_read_b": _to_bytes(io[0].strip()) if len(io) == 2 else None,
            "io_write_b": _to_bytes(io[1].strip()) if len(io) == 2 else None,
        })

    if not rows:
        sys.exit("No data row parsed from %s - has the table format changed?" % path)
    return rows


def series(rows, phase, field, scale=1.0):
    """(x, y) sorted by batch size, for one phase and one column."""
    pts = [(r["n_b"], r[field]) for r in rows
           if r["phase"] == phase and r[field] is not None]
    pts.sort()
    return [p[0] for p in pts], [p[1] / scale for p in pts]


# --------------------------------------------------------------------------- #
# Style
# --------------------------------------------------------------------------- #
def setup_style(bigfont=False):
    try:
        import scienceplots  # noqa: F401
        plt.style.use(["science", "no-latex"])
    except Exception:
        plt.style.use("seaborn-v0_8-whitegrid")
    mpl.rcParams["text.usetex"] = False
    mpl.rcParams["mathtext.fontset"] = "dejavusans"
    mpl.rcParams["font.family"] = "DejaVu Sans"
    mpl.rcParams["figure.dpi"] = 120
    mpl.rcParams["savefig.dpi"] = 300
    mpl.rcParams["savefig.bbox"] = "tight"
    mpl.rcParams["axes.grid"] = True
    mpl.rcParams["grid.alpha"] = 0.3
    mpl.rcParams["grid.linestyle"] = ":"
    mpl.rcParams["legend.frameon"] = True
    mpl.rcParams["legend.framealpha"] = 0.9

    if bigfont:
        sizes = dict(base=18, title=20, label=18, tick=15, legend=14)
    else:
        sizes = dict(base=9, title=10, label=9, tick=8, legend=7.5)
    mpl.rcParams["font.size"] = sizes["base"]
    mpl.rcParams["axes.titlesize"] = sizes["title"]
    mpl.rcParams["axes.labelsize"] = sizes["label"]
    mpl.rcParams["xtick.labelsize"] = sizes["tick"]
    mpl.rcParams["ytick.labelsize"] = sizes["tick"]
    mpl.rcParams["legend.fontsize"] = sizes["legend"]
    mpl.rcParams["lines.linewidth"] = 2.0 if bigfont else 1.3
    mpl.rcParams["lines.markersize"] = 8 if bigfont else 4.5


def pow2_axis(ax, xs):
    ax.set_xscale("log", base=2)
    ax.set_xlabel(r"Batch size $N_b$ (transactions)")
    ax.set_xticks(sorted(set(xs)))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: "%d" % int(v)))
    ax.tick_params(axis="x", rotation=45)
    ax.minorticks_off()


def logy(ax):
    ax.set_yscale("log")
    ax.yaxis.set_major_locator(LogLocator(base=10.0, numticks=12))


def plot_phases(ax, rows, phases, field, scale=1.0):
    xs_all = []
    for ph in phases:
        lab, color, marker, ls = STYLE[ph]
        x, y = series(rows, ph, field, scale)
        if not x:
            continue
        xs_all += x
        ax.plot(x, y, label=lab, color=color, marker=marker, linestyle=ls)
    pow2_axis(ax, xs_all)


def save(fig, outdir, name):
    for ext in ("pdf", "png"):
        p = os.path.join(outdir, "%s.%s" % (name, ext))
        fig.savefig(p)
        print("    wrote %s" % p)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #
def fig_summary(rows, outdir, bigfont):
    figsize = (16, 12) if bigfont else (7.2, 5.8)
    fig, axes = plt.subplots(2, 2, figsize=figsize)
    (ax_t, ax_r), (ax_c, ax_io) = axes

    # (a) wall-clock time
    plot_phases(ax_t, rows, ORDER, "time_s")
    logy(ax_t)
    ax_t.set_ylabel("Wall-clock time (s)")
    ax_t.set_title("(a) Time")

    # (b) peak resident memory
    plot_phases(ax_r, rows, ORDER, "peak_ram_b", GIB)
    logy(ax_r)
    ax_r.set_ylabel("Peak RAM (GiB)")
    ax_r.set_title("(b) Peak resident memory")

    # (c) mean busy cores - linear, it saturates
    plot_phases(ax_c, rows, ORDER, "cores")
    ax_c.set_ylabel("Mean busy cores")
    ax_c.set_title("(c) Parallelism")

    # (d) I/O volume, read + written
    xs_all = []
    for ph in TOP_LEVEL:
        lab, color, marker, ls = STYLE[ph]
        x, yr = series(rows, ph, "io_read_b", GIB)
        _, yw = series(rows, ph, "io_write_b", GIB)
        if not x:
            continue
        xs_all += x
        ax_io.plot(x, [a + b for a, b in zip(yr, yw)],
                   label=lab, color=color, marker=marker, linestyle=ls)
    pow2_axis(ax_io, xs_all)
    logy(ax_io)
    ax_io.set_ylabel("I/O read + written (GiB)")
    ax_io.set_title("(d) Syscall-level I/O")

    handles, labels = ax_t.get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center",
               ncol=3 if bigfont else 4, bbox_to_anchor=(0.5, -0.07))
    fig.tight_layout()
    save(fig, outdir, "fig_tab1_resources")


def fig_time(rows, outdir, bigfont):
    fig, ax = plt.subplots(figsize=(10, 7) if bigfont else (4.4, 3.2))
    plot_phases(ax, rows, TOP_LEVEL, "time_s")
    logy(ax)
    ax.set_ylabel("Wall-clock time (s)")
    ax.legend(loc="upper left")
    fig.tight_layout()
    save(fig, outdir, "fig_tab1_time")


def fig_ram(rows, outdir, bigfont):
    fig, ax = plt.subplots(figsize=(10, 7) if bigfont else (4.4, 3.2))
    plot_phases(ax, rows, TOP_LEVEL, "peak_ram_b", GIB)
    logy(ax)
    ax.set_ylabel("Peak RAM (GiB)")
    ax.legend(loc="upper left")
    fig.tight_layout()
    save(fig, outdir, "fig_tab1_ram")


def fig_setup_breakdown(rows, outdir, bigfont):
    """Stacked share of the three setup sub-steps in the total setup time."""
    fig, ax = plt.subplots(figsize=(10, 7) if bigfont else (4.4, 3.2))
    xs, tot = series(rows, "Setup (one-off)", "time_s")
    totals = dict(zip(xs, tot))

    shares, labels, colors = [], [], []
    for ph in SETUP_SUB:
        lab, color, _, _ = STYLE[ph]
        x, y = series(rows, ph, "time_s")
        shares.append([100.0 * v / totals[n] for n, v in zip(x, y)])
        labels.append(lab)
        colors.append(color)

    ax.stackplot(xs, *shares, labels=labels, colors=colors, alpha=0.85,
                 edgecolor="white", linewidth=0.4)
    pow2_axis(ax, xs)
    ax.set_ylim(0, 100)
    ax.set_ylabel("Share of setup wall-clock time (%)")
    ax.legend(loc="lower left")
    ax.grid(False)
    fig.tight_layout()
    save(fig, outdir, "fig_tab1_setup_breakdown")


def dump_csv(rows, outdir):
    path = os.path.join(outdir, "tab1_resources.csv")
    fields = ["n_b", "phase", "time_s", "cores", "peak_ram_b",
              "io_read_b", "io_write_b"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in sorted(rows, key=lambda r: (r["n_b"], ORDER.index(r["phase"]))):
            w.writerow(r)
    print("    wrote %s" % path)


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table", default=DEFAULT_TABLE,
                    help="path to tab1_resources.tex (default: %s)" % DEFAULT_TABLE)
    ap.add_argument("--outdir", default=None,
                    help="output directory (default: <bench-run>/figs)")
    ap.add_argument("--bigfont", action="store_true",
                    help="presentation typography instead of paper column size")
    args = ap.parse_args()

    if not os.path.isfile(args.table):
        sys.exit("Table not found: %s" % args.table)

    outdir = args.outdir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(args.table))), "figs")
    os.makedirs(outdir, exist_ok=True)

    print("\n>>> Table : %s" % args.table)
    print(">>> Output: %s\n" % outdir)

    rows = parse_table(args.table)
    nbs = sorted({r["n_b"] for r in rows})
    print("    parsed %d rows, batch sizes %d..%d (%d points)\n"
          % (len(rows), nbs[0], nbs[-1], len(nbs)))

    setup_style(args.bigfont)
    fig_summary(rows, outdir, args.bigfont)
    fig_time(rows, outdir, args.bigfont)
    fig_ram(rows, outdir, args.bigfont)
    fig_setup_breakdown(rows, outdir, args.bigfont)
    dump_csv(rows, outdir)
    print("\nDone.")


if __name__ == "__main__":
    main()
