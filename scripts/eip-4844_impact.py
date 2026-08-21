#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eip4844_figure.py
=================

Génère une figure prête pour publication illustrant l'impact de l'EIP-4844
(hard fork Dencun, 13 mars 2024, passage du `calldata` aux `blobs`) sur
l'économie des rollups Layer-2 d'Ethereum.

La figure ne montre **que la comparaison Layer-1 / Layer-2 agrégé** : les
rollups individuels sont fusionnés en un unique agrégat, le détail par chaîne
n'étant pas l'objet de la démonstration.

Panneaux
--------
  (a) Coût médian d'une transaction, L2 agrégé vs Ethereum L1 (USD, échelle
      log) — la rupture structurelle au moment de Dencun est l'effet principal
      à démontrer. L'enveloppe grisée donne l'étendue (min-max) entre rollups,
      comme mesure de dispersion, sans nommer les chaînes.
  (b) Débit agrégé L2 vs L1 (transactions/jour, moyenne mobile 7 j) — montre
      que la baisse de coût s'accompagne d'une adoption accrue.
  (c) Coût médian moyen 30 j avant vs 30 j après Dencun, pour l'agrégat L2 et
      pour L1, avec le pourcentage de réduction annoté.

Agrégation L2
-------------
Le coût L2 agrégé est, pour chaque date, la moyenne des coûts médians par
rollup **pondérée par le nombre de transactions du jour** (`txcount`) :

    cost_L2(t) = somme_c txcount_c(t) * cost_c(t) / somme_c txcount_c(t)

Un rollup n'entre dans l'agrégat un jour donné que si ses deux métriques sont
disponibles ; le débit L2 agrégé est la somme simple des `txcount`.

Sources de données
------------------
Par défaut : API publique de growthepie.xyz (agrégateur open-source de
métriques L2, données on-chain).

    https://api.growthepie.com/v1/export/<metric>.json

Le script met les données en cache dans un CSV. **Archivez ce CSV avec votre
article** : c'est ce qui rend la figure reproductible par un relecteur.

Format CSV attendu (long format), si vous fournissez vos propres données :

    date,chain,metric,value
    2024-03-01,arbitrum,txcosts_median_usd,0.0921
    2024-03-01,arbitrum,txcount,1204551
    ...

Usage
-----
    pip install pandas matplotlib numpy

    python eip4844_figure.py --fetch          # télécharge + met en cache le CSV
    python eip4844_figure.py                  # trace depuis le cache
    python eip4844_figure.py --csv data.csv   # vos propres données
    python eip4844_figure.py --demo           # données SYNTHÉTIQUES (test only)
    python eip4844_figure.py --lang fr        # légendes en français
    python eip4844_figure.py --no-band        # masque l'enveloppe de dispersion

AVERTISSEMENT : le mode `--demo` produit des données artificielles destinées
uniquement à vérifier le rendu graphique. Ne publiez jamais cette figure sans
avoir régénéré les données via `--fetch` ou votre propre source.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Évènements de référence
DENCUN = pd.Timestamp("2024-03-13")   # EIP-4844 : introduction des blobs
PECTRA = pd.Timestamp("2025-05-07")   # EIP-7691 : cible blobs 3 -> 6 par bloc

# Rollups entrant dans l'agrégat L2 : clé API growthepie -> nom d'affichage.
# Ils ne sont plus tracés individuellement ; les noms servent au journal et à
# la note de méthode accompagnant la figure.
CHAINS = {
    "arbitrum":   "Arbitrum One",
    "optimism":   "OP Mainnet",
    "base":       "Base",
    "zksync_era": "zkSync Era",
    "linea":      "Linea",
}
L1_KEY = "ethereum"

FEE_METRIC = "txcosts_median_usd"
TX_METRIC = "txcount"

# Palette Okabe-Ito (sûre pour le daltonisme, lisible en niveaux de gris)
COLORS = ["#0072B2", "#E69F00", "#009E73", "#D55E00", "#CC79A7", "#56B4E9"]
GREY = "#3F3F3F"
L2_COLOR = COLORS[0]
L1_COLOR = GREY
BAND_COLOR = "#9FB6C6"

# API growthepie (public, sans authentification, <= 10 requetes/minute).
# NB : v1/fundamentals.json ne couvre que les 90 derniers jours ; pour disposer
# de l'historique complet encadrant Dencun il faut interroger v1/export/{metric}.json.
API_BASE = "https://api.growthepie.com/v1"
EXPORT_METRICS = ("txcosts", "txcount")   # -> v1/export/<metric>.json
RATE_LIMIT_SLEEP = 7.0                    # secondes entre deux appels
CACHE = Path("data/l2_fundamentals.csv")

WINDOW = 30            # jours de part et d'autre de Dencun (panneau c)
SMOOTH = 7             # fenêtre de lissage (moyenne mobile), en jours

LABELS = {
    "en": {
        "fee_y":    "Median transaction cost (USD)",
        "tx_y":     "Transactions per day",
        "bar_y":    "Median cost (USD)",
        "before":   f"{WINDOW} d before",
        "after":    f"{WINDOW} d after",
        "dencun":   "Dencun\n(EIP-4844)",
        "pectra":   "Pectra\n(EIP-7691)",
        "l2":       "Aggregate L2",
        "l1":       "Ethereum L1",
        "band":     "L2 range (min-max)",
        "a":        "(a) Transaction cost: calldata to blob regime shift",
        "b":        "(b) Layer-2 vs Layer-1 throughput",
        "c":        f"(c) Cost reduction across the Dencun fork ({WINDOW}-day means)",
    },
    "fr": {
        "fee_y":    "Coût médian par transaction (USD)",
        "tx_y":     "Transactions par jour",
        "bar_y":    "Coût médian (USD)",
        "before":   f"{WINDOW} j avant",
        "after":    f"{WINDOW} j après",
        "dencun":   "Dencun\n(EIP-4844)",
        "pectra":   "Pectra\n(EIP-7691)",
        "l2":       "Agrégat L2",
        "l1":       "Ethereum L1",
        "band":     "Étendue L2 (min-max)",
        "a":        "(a) Coût de transaction : bascule du calldata vers les blobs",
        "b":        "(b) Débit Layer-2 vs Layer-1",
        "c":        f"(c) Réduction du coût autour de Dencun (moyennes sur {WINDOW} j)",
    },
}


# ---------------------------------------------------------------------------
# Style publication
# ---------------------------------------------------------------------------

def set_style(base_size: float = 9.0) -> None:
    """Style sobre, vectoriel, compatible LaTeX (pdflatex / Type-42)."""
    mpl.rcParams.update({
        "font.family":        "serif",
        "font.serif":         ["Times New Roman", "Nimbus Roman", "DejaVu Serif"],
        "mathtext.fontset":   "stix",
        "font.size":          base_size,
        "axes.titlesize":     base_size,
        "axes.labelsize":     base_size,
        "xtick.labelsize":    base_size - 1,
        "ytick.labelsize":    base_size - 1,
        "legend.fontsize":    base_size - 1.5,
        "axes.linewidth":     0.7,
        "axes.edgecolor":     GREY,
        "axes.spines.top":    False,
        "axes.spines.right":  False,
        "axes.titlelocation": "left",
        "axes.titlepad":      6,
        "xtick.direction":    "out",
        "ytick.direction":    "out",
        "xtick.major.width":  0.7,
        "ytick.major.width":  0.7,
        "lines.linewidth":    1.3,
        "grid.color":         "#D9D9D9",
        "grid.linewidth":     0.5,
        "legend.frameon":     False,
        "figure.dpi":         120,
        "savefig.dpi":        600,
        "savefig.bbox":       "tight",
        "savefig.pad_inches": 0.05,
        "pdf.fonttype":       42,   # polices intégrées, éditables (exigé par bcp d'éditeurs)
        "ps.fonttype":        42,
    })


def usd_fmt(x, _pos=None) -> str:
    if x >= 1:
        return f"${x:,.0f}"
    if x >= 0.01:
        return f"${x:.2f}"
    return f"${x:.3f}"


def si_fmt(x, _pos=None) -> str:
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if x >= div:
            return f"{x/div:g}{suf}"
    return f"{x:g}"


# ---------------------------------------------------------------------------
# Données
# ---------------------------------------------------------------------------

def _get_json(url: str, timeout: int = 300):
    """GET JSON avec messages d'erreur exploitables (l'API evolue regulierement)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "eip4844-figure/2.0 (academic research)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                payload = gzip.decompress(payload)
            return json.loads(payload)
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"HTTP {exc.code} sur {url}\n"
            "L'endpoint a probablement change. Verifiez la documentation courante :\n"
            "  https://docs.growthepie.com/api-reference/api\n"
            "puis ajustez API_BASE / EXPORT_METRICS, ou fournissez vos donnees via --csv."
        ) from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Echec reseau sur {url} : {exc.reason}") from exc


def fetch(out: Path = CACHE, base: str = API_BASE) -> pd.DataFrame:
    """Recupere l'historique complet des metriques utiles et met en cache un CSV."""
    frames = []
    for i, metric in enumerate(EXPORT_METRICS):
        url = f"{base}/export/{metric}.json"
        print(f"[fetch] {url} ...", file=sys.stderr)
        raw = _get_json(url)
        # Le payload est soit une liste de lignes, soit un objet enveloppant "data".
        rows = raw.get("data", raw) if isinstance(raw, dict) else raw
        df = pd.DataFrame(rows)
        missing = {"metric_key", "origin_key", "date", "value"} - set(df.columns)
        if missing:
            raise SystemExit(
                f"Schema inattendu pour {url} : colonnes manquantes {sorted(missing)}.\n"
                f"Colonnes recues : {sorted(df.columns)}"
            )
        frames.append(df)
        if i < len(EXPORT_METRICS) - 1:
            time.sleep(RATE_LIMIT_SLEEP)

    df = pd.concat(frames, ignore_index=True)

    wanted_metrics = {FEE_METRIC, TX_METRIC}
    available = set(df["metric_key"].unique())
    if not wanted_metrics <= available:
        raise SystemExit(
            f"metric_key introuvable(s) : {sorted(wanted_metrics - available)}\n"
            f"Cles disponibles : {sorted(available)}\n"
            "Ajustez FEE_METRIC / TX_METRIC en haut du script."
        )

    wanted_chains = set(CHAINS) | {L1_KEY}
    absent = wanted_chains - set(df["origin_key"].unique())
    if absent:
        print(f"[warn] origin_key sans donnees, ignore(s) : {sorted(absent)}", file=sys.stderr)

    df = df[df["metric_key"].isin(wanted_metrics) & df["origin_key"].isin(wanted_chains)].copy()
    df = df.rename(columns={"origin_key": "chain", "metric_key": "metric"})
    df = df[["date", "chain", "metric", "value"]]
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.tz_localize(None).dt.normalize()
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna().drop_duplicates(["date", "chain", "metric"])

    out.parent.mkdir(parents=True, exist_ok=True)
    df.sort_values(["metric", "chain", "date"]).to_csv(out, index=False)
    span = f"{df['date'].min():%Y-%m-%d} -> {df['date'].max():%Y-%m-%d}"
    print(f"[fetch] {len(df):,} lignes ({span}) -> {out}", file=sys.stderr)
    if df["date"].min() > DENCUN - pd.Timedelta(days=WINDOW):
        print("[warn] L'historique ne couvre pas les 30 j precedant Dencun : "
              "le panneau (c) sera incomplet.", file=sys.stderr)
    return df


def load(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(
            f"Cache introuvable : {path}\n"
            "Lancez d'abord `python eip4844_figure.py --fetch`, "
            "ou fournissez vos données avec `--csv`, ou testez le rendu avec `--demo`."
        )
    df = pd.read_csv(path, parse_dates=["date"])
    return df


def demo_data(seed: int = 7) -> pd.DataFrame:
    """DONNÉES SYNTHÉTIQUES — vérification du rendu uniquement, jamais publiables."""
    rng = np.random.default_rng(seed)
    days = pd.date_range("2023-09-01", "2026-06-30", freq="D")
    t = (days - DENCUN).days.to_numpy(dtype=float)
    rows = []

    pre = {"arbitrum": 0.21, "optimism": 0.28, "base": 0.19,
           "zksync_era": 0.34, "linea": 0.31}
    drop = {"arbitrum": 0.045, "optimism": 0.035, "base": 0.030,
            "zksync_era": 0.060, "linea": 0.055}

    for chain in CHAINS:
        step = np.where(t < 0, pre[chain], pre[chain] * drop[chain])
        # remontée progressive post-Pectra (saturation de l'espace blob)
        creep = 1 + 1.8 / (1 + np.exp(-(t - 480) / 90))
        fee = step * creep * np.exp(rng.normal(0, 0.28, t.size))
        rows.append(pd.DataFrame({"date": days, "chain": chain,
                                  "metric": FEE_METRIC, "value": fee}))
        base_tx = {"arbitrum": 1.1e6, "optimism": 6e5, "base": 9e5,
                   "zksync_era": 4e5, "linea": 2e5}[chain]
        growth = np.exp(np.clip(t, -400, None) / 900) * (1 + 1.4 / (1 + np.exp(-t / 45)))
        tx = base_tx * growth * np.exp(rng.normal(0, 0.12, t.size))
        rows.append(pd.DataFrame({"date": days, "chain": chain,
                                  "metric": TX_METRIC, "value": tx}))

    rows.append(pd.DataFrame({"date": days, "chain": L1_KEY, "metric": TX_METRIC,
                              "value": 1.15e6 * np.exp(rng.normal(0, 0.05, t.size))}))
    rows.append(pd.DataFrame({"date": days, "chain": L1_KEY, "metric": FEE_METRIC,
                              "value": 4.2 * np.exp(rng.normal(0, 0.35, t.size))}))
    return pd.concat(rows, ignore_index=True)


def pivot(df: pd.DataFrame, metric: str) -> pd.DataFrame:
    return (df[df["metric"] == metric]
            .pivot_table(index="date", columns="chain", values="value", aggfunc="mean")
            .sort_index())


def aggregate(df: pd.DataFrame) -> pd.DataFrame:
    """Réduit les rollups à un unique agrégat L2, aligné sur les séries L1.

    Renvoie un DataFrame indexé par date, colonnes :
      l2_fee    coût médian L2, moyenne pondérée par txcount
      l1_fee    coût médian L1
      l2_tx     somme des txcount L2
      l1_tx     txcount L1
      l2_lo     plus faible coût médian observé parmi les rollups (dispersion)
      l2_hi     plus fort coût médian observé parmi les rollups (dispersion)
      n_chains  nombre de rollups contribuant à l'agrégat ce jour-là
    """
    fees, txs = pivot(df, FEE_METRIC), pivot(df, TX_METRIC)
    idx = fees.index.union(txs.index)
    fees, txs = fees.reindex(idx), txs.reindex(idx)

    cols = [c for c in CHAINS if c in fees.columns and c in txs.columns]
    if not cols:
        raise SystemExit("Aucun rollup ne dispose des deux métriques ; agrégat impossible.")
    dropped = [CHAINS[c] for c in CHAINS if c not in cols]
    if dropped:
        print(f"[warn] exclus de l'agregat des couts (metrique manquante) : {dropped}",
              file=sys.stderr)

    f, w = fees[cols], txs[cols]
    # Un rollup ne compte un jour donné que si coût ET débit sont disponibles.
    mask = f.notna() & w.notna()
    f, w = f.where(mask), w.where(mask)

    out = pd.DataFrame(index=idx)
    out["l2_fee"] = (f * w).sum(axis=1, min_count=1) / w.sum(axis=1, min_count=1)
    out["l2_tx"] = txs[[c for c in CHAINS if c in txs.columns]].sum(axis=1, min_count=1)
    out["l2_lo"] = f.min(axis=1)
    out["l2_hi"] = f.max(axis=1)
    out["n_chains"] = mask.sum(axis=1)
    out["l1_fee"] = fees[L1_KEY] if L1_KEY in fees.columns else np.nan
    out["l1_tx"] = txs[L1_KEY] if L1_KEY in txs.columns else np.nan
    return out


# ---------------------------------------------------------------------------
# Figure
# ---------------------------------------------------------------------------

def mark_forks(ax, lab, show_pectra=True, annotate=True) -> None:
    ax.axvline(DENCUN, color="#B3202C", lw=1.0, ls="-", zorder=1)
    if show_pectra:
        ax.axvline(PECTRA, color=GREY, lw=0.8, ls=(0, (4, 2)), zorder=1)
    if not annotate:
        return
    ax.annotate(lab["dencun"], xy=(DENCUN, 1.0), xycoords=("data", "axes fraction"),
                xytext=(-4, -2), textcoords="offset points",
                ha="right", va="top", color="#B3202C", fontsize=7.5, linespacing=1.15)
    if show_pectra:
        ax.annotate(lab["pectra"], xy=(PECTRA, 1.0), xycoords=("data", "axes fraction"),
                    xytext=(4, -2), textcoords="offset points",
                    ha="left", va="top", color=GREY, fontsize=7.5, linespacing=1.15)


def make_figure(df: pd.DataFrame, lang: str = "en", start: str | None = None,
                band: bool = True):
    lab = LABELS[lang]
    agg = aggregate(df)
    if start:
        agg = agg.loc[start:]

    def roll_med(s):
        return s.rolling(SMOOTH, min_periods=max(1, SMOOTH // 2)).median()

    def roll_avg(s):
        return s.rolling(SMOOTH, min_periods=3).mean()

    fig, axes = plt.subplots(3, 1, figsize=(7.0, 8.0),
                             gridspec_kw={"height_ratios": [1.15, 0.95, 0.9], "hspace": 0.62})
    ax_a, ax_b, ax_c = axes

    # ---- (a) coût médian par transaction : L2 agrégé vs L1 --------------
    if band:
        lo, hi = roll_med(agg["l2_lo"]), roll_med(agg["l2_hi"])
        ok = lo.notna() & hi.notna()
        ax_a.fill_between(agg.index[ok], lo[ok], hi[ok], color=BAND_COLOR,
                          alpha=0.35, lw=0, zorder=1, label=lab["band"])
    fee2 = roll_med(agg["l2_fee"])
    ax_a.plot(fee2.index, fee2.values, color=L2_COLOR, label=lab["l2"], zorder=3)
    fee1 = roll_med(agg["l1_fee"])
    if fee1.notna().any():
        ax_a.plot(fee1.index, fee1.values, color=L1_COLOR, ls=(0, (4, 2)),
                  label=lab["l1"], zorder=3)
    ax_a.set_yscale("log")
    ax_a.set_ylabel(lab["fee_y"])
    ax_a.set_title(lab["a"])
    ax_a.yaxis.set_major_formatter(FuncFormatter(usd_fmt))
    ax_a.grid(axis="y", which="major", zorder=0)
    ylo, yhi = ax_a.get_ylim()
    ax_a.set_ylim(ylo * 0.8, yhi * 3.2)        # marge haute pour les annotations de fork
    mark_forks(ax_a, lab)
    ax_a.legend(loc="lower left", ncol=1, handlelength=1.8)

    # ---- (b) débit L2 agrégé vs L1 -------------------------------------
    tx2 = roll_avg(agg["l2_tx"])
    ax_b.plot(tx2.index, tx2.values, color=L2_COLOR, label=lab["l2"])
    tx1 = roll_avg(agg["l1_tx"])
    if tx1.notna().any():
        ax_b.plot(tx1.index, tx1.values, color=L1_COLOR, ls=(0, (4, 2)), label=lab["l1"])
    ax_b.set_yscale("log")
    ax_b.set_ylabel(lab["tx_y"])
    ax_b.set_title(lab["b"])
    ax_b.yaxis.set_major_formatter(FuncFormatter(si_fmt))
    ax_b.grid(axis="y", which="major", zorder=0)
    mark_forks(ax_b, lab, annotate=False)
    ax_b.legend(loc="upper left", ncol=1, handlelength=1.8)

    # ---- (c) avant / après Dencun --------------------------------------
    pre = agg.loc[DENCUN - pd.Timedelta(days=WINDOW):DENCUN - pd.Timedelta(days=1)]
    post = agg.loc[DENCUN + pd.Timedelta(days=1):DENCUN + pd.Timedelta(days=WINDOW)]
    groups = [(name, col) for name, col in ((lab["l2"], "l2_fee"), (lab["l1"], "l1_fee"))
              if np.isfinite(pre[col].mean()) and np.isfinite(post[col].mean())]
    if not groups:
        raise SystemExit("Fenêtre autour de Dencun vide : le panneau (c) ne peut être tracé.")

    names = [n for n, _ in groups]
    before = [float(pre[c].mean()) for _, c in groups]
    after = [float(post[c].mean()) for _, c in groups]

    x = np.arange(len(groups))
    w = 0.30
    ax_c.bar(x - w / 2, before, w, label=lab["before"],
             color=BAND_COLOR, edgecolor=GREY, linewidth=0.5)
    ax_c.bar(x + w / 2, after, w, label=lab["after"],
             color=L2_COLOR, edgecolor=GREY, linewidth=0.5)
    for xi, b, a in zip(x, before, after):
        red = 100 * (1 - a / b)
        ax_c.annotate(f"−{red:.1f} %", xy=(xi, max(b, a)),
                      xytext=(0, 3), textcoords="offset points",
                      ha="center", va="bottom", fontsize=7.5, color="#B3202C")
    ax_c.set_yscale("log")
    ax_c.set_xticks(x, names)
    ax_c.set_xlim(-0.6, len(groups) - 0.4)
    ax_c.set_ylabel(lab["bar_y"])
    ax_c.set_title(lab["c"])
    ax_c.yaxis.set_major_formatter(FuncFormatter(usd_fmt))
    ax_c.grid(axis="y", which="major", zorder=0)
    ax_c.set_ylim(top=max(before) * 12)
    ax_c.legend(loc="upper right", ncol=2, handlelength=1.4)

    for ax in axes:
        ax.set_axisbelow(True)

    # Table récapitulative (à reporter dans le texte de l'article)
    summary = pd.DataFrame({
        "mean_before_usd": before,
        "mean_after_usd": after,
        "reduction_pct": [100 * (1 - a / b) for a, b in zip(after, before)],
    }, index=names)
    if len(groups) == 2:   # facteur de coût L1/L2, avant et après le fork
        summary["l1_over_l2_before"] = before[1] / before[0]
        summary["l1_over_l2_after"] = after[1] / after[0]

    n = agg.loc[DENCUN - pd.Timedelta(days=WINDOW):DENCUN + pd.Timedelta(days=WINDOW),
                "n_chains"]
    if len(n):
        print(f"[info] rollups agreges autour de Dencun : "
              f"{int(n.min())}-{int(n.max())} chaines/jour", file=sys.stderr)

    return fig, summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fetch", action="store_true", help="télécharger les données et écrire le cache CSV")
    p.add_argument("--csv", type=Path, default=CACHE, help="CSV source (long format)")
    p.add_argument("--demo", action="store_true", help="données SYNTHÉTIQUES (test du rendu)")
    p.add_argument("--lang", choices=["en", "fr"], default="en")
    p.add_argument("--start", default="2023-09-01", help="date de début de l'axe temporel")
    p.add_argument("--out", default="fig_eip4844_l2", help="préfixe des fichiers de sortie")
    p.add_argument("--formats", default="pdf,png", help="formats séparés par des virgules")
    p.add_argument("--no-band", dest="band", action="store_false",
                   help="masquer l'enveloppe min-max des rollups au panneau (a)")
    args = p.parse_args()

    if args.demo:
        print("\n*** MODE DEMO : données synthétiques, NE PAS PUBLIER ***\n", file=sys.stderr)
        df = demo_data()
    elif args.fetch:
        df = fetch(out=args.csv)
    else:
        df = load(args.csv)

    set_style()
    fig, summary = make_figure(df, lang=args.lang, start=args.start, band=args.band)

    for fmt in [f.strip() for f in args.formats.split(",") if f.strip()]:
        path = f"{args.out}.{fmt}"
        fig.savefig(path)
        print(f"[write] {path}", file=sys.stderr)

    print("\n" + summary.round(4).to_string())
    summary.round(4).to_csv(f"{args.out}_summary.csv")
    print(f"\n[write] {args.out}_summary.csv", file=sys.stderr)


if __name__ == "__main__":
    main()
