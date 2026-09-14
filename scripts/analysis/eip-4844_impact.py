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
  (c) Coût médian moyen 30 j avant vs 30 j après chaque hard fork, en quatre
      colonnes : Dencun L2, Dencun L1, Pectra L2, Pectra L1, avec la variation
      relative annotée au-dessus de chaque paire de barres. Un groupe est omis
      si l'historique ne couvre pas l'une des deux fenêtres.

Sorties
-------
Chaque exécution écrit, pour chaque format demandé, la figure complète
(`<out>.<fmt>`) **et** les trois panneaux en images séparées
(`<out>_a.<fmt>`, `<out>_b.<fmt>`, `<out>_c.<fmt>`), plus le CSV récapitulatif
`<out>_summary.csv`.

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
import matplotlib.dates as mdates
from matplotlib.ticker import FuncFormatter

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Évènements de référence
DENCUN = pd.Timestamp("2024-03-13")   # EIP-4844 : introduction des blobs
PECTRA = pd.Timestamp("2025-05-07")   # EIP-7691 : cible blobs 3 -> 6 par bloc

# Forks compares dans le panneau (c), dans l'ordre d'affichage.
FORKS = (("dencun", DENCUN), ("pectra", PECTRA))

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
# Racine du depot : scripts/analysis/<ce fichier> -> remonte de deux niveaux.
ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "l2_fundamentals.csv"
DEFAULT_OUT = ROOT / "bench-out" / "eip4844" / "fig_eip4844_l2"

# Typographie : la figure est souvent reduite a la largeur d'une colonne dans
# un article a deux colonnes, ou un corps de 9 pt devient illisible.
BASE_FONT = 15.0       # texte courant de la figure (titres, axes, legendes)
ANN_FONT = 10.0        # annotations tracees dans l'aire du graphe

WINDOW = 30            # jours de part et d'autre de Dencun (panneau c)
SMOOTH = 7             # fenêtre de lissage (moyenne mobile), en jours

LABELS = {
    "en": {
        "fee_y":    "Median transaction cost (USD)",
        "tx_y":     "Transactions per day",
        # Variantes courtes : panneaux autonomes, plus plats, ou le libelle
        # complet de l'axe y depasserait la hauteur de la figure.
        "fee_y_short": "Cost per tx (USD)",
        "tx_y_short":  "Tx per day",
        "bar_y":    "Median cost (USD)",
        "before":   f"{WINDOW} d before",
        "after":    f"{WINDOW} d after",
        "dencun":   "Dencun\n(EIP-4844)",
        "pectra":   "Pectra\n(EIP-7691)",
        "dencun_short": "Dencun",
        "pectra_short": "Pectra",
        "l2":       "Aggregate L2",
        "l1":       "Ethereum L1",
        "band":     "L2 range (min-max)",
        "a":        "(a) Transaction cost: calldata to blob regime shift",
        "b":        "(b) Layer-2 vs Layer-1 throughput",
        "c":        f"(c) Cost reduction across the Dencun and Pectra forks ({WINDOW}-day means)",
    },
    "fr": {
        "fee_y":    "Coût médian par transaction (USD)",
        "tx_y":     "Transactions par jour",
        "fee_y_short": "Coût / tx (USD)",
        "tx_y_short":  "Tx / jour",
        "bar_y":    "Coût médian (USD)",
        "before":   f"{WINDOW} j avant",
        "after":    f"{WINDOW} j après",
        "dencun":   "Dencun\n(EIP-4844)",
        "pectra":   "Pectra\n(EIP-7691)",
        "dencun_short": "Dencun",
        "pectra_short": "Pectra",
        "l2":       "Agrégat L2",
        "l1":       "Ethereum L1",
        "band":     "Étendue L2 (min-max)",
        "a":        "(a) Coût de transaction : bascule du calldata vers les blobs",
        "b":        "(b) Débit Layer-2 vs Layer-1",
        "c":        f"(c) Réduction du coût autour de Dencun et Pectra (moyennes sur {WINDOW} j)",
    },
}


# ---------------------------------------------------------------------------
# Style publication
# ---------------------------------------------------------------------------

def set_style(base_size: float = BASE_FONT) -> None:
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

def _date_axis(ax) -> None:
    """Une seule graduation par annee, etiquetee `AAAA`.

    Le locator par defaut place une date par trimestre au format `AAAA-MM` :
    au-dela de ~10 pt les etiquettes se chevauchent. Les debuts d'annee
    suffisent a situer la chronologie, les deux forks etant reperes par leurs
    lignes verticales.
    """
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))


def _roll_med(s: pd.Series) -> pd.Series:
    return s.rolling(SMOOTH, min_periods=max(1, SMOOTH // 2)).median()


def _roll_avg(s: pd.Series) -> pd.Series:
    return s.rolling(SMOOTH, min_periods=3).mean()


def mark_forks(ax, lab, show_pectra=True, annotate=True) -> None:
    ax.axvline(DENCUN, color="#B3202C", lw=1.0, ls="-", zorder=1)
    if show_pectra:
        ax.axvline(PECTRA, color=GREY, lw=0.8, ls=(0, (4, 2)), zorder=1)
    if not annotate:
        return
    ax.annotate(lab["dencun"], xy=(DENCUN, 1.0), xycoords=("data", "axes fraction"),
                xytext=(-4, -2), textcoords="offset points",
                ha="right", va="top", color="#B3202C", fontsize=ANN_FONT, linespacing=1.15)
    if show_pectra:
        ax.annotate(lab["pectra"], xy=(PECTRA, 1.0), xycoords=("data", "axes fraction"),
                    xytext=(4, -2), textcoords="offset points",
                    ha="left", va="top", color=GREY, fontsize=ANN_FONT, linespacing=1.15)


def _panel_a(ax, agg, lab, band: bool = True, head: float = 3.2) -> None:
    """(a) Coût médian par transaction : L2 agrégé vs L1."""
    if band:
        lo, hi = _roll_med(agg["l2_lo"]), _roll_med(agg["l2_hi"])
        ok = lo.notna() & hi.notna()
        ax.fill_between(agg.index[ok], lo[ok], hi[ok], color=BAND_COLOR,
                        alpha=0.35, lw=0, zorder=1, label=lab["band"])
    fee2 = _roll_med(agg["l2_fee"])
    ax.plot(fee2.index, fee2.values, color=L2_COLOR, label=lab["l2"], zorder=3)
    fee1 = _roll_med(agg["l1_fee"])
    if fee1.notna().any():
        ax.plot(fee1.index, fee1.values, color=L1_COLOR, ls=(0, (4, 2)),
                label=lab["l1"], zorder=3)
    ax.set_yscale("log")
    ax.set_ylabel(lab["fee_y"])
    ax.set_title(lab["a"])
    ax.yaxis.set_major_formatter(FuncFormatter(usd_fmt))
    ax.grid(axis="y", which="major", zorder=0)
    _date_axis(ax)
    ylo, yhi = ax.get_ylim()
    ax.set_ylim(ylo * 0.8, yhi * head)       # marge haute pour les annotations de fork
    mark_forks(ax, lab)
    ax.legend(loc="lower left", ncol=1, handlelength=1.8)


def _panel_b(ax, agg, lab) -> None:
    """(b) Débit L2 agrégé vs L1."""
    tx2 = _roll_avg(agg["l2_tx"])
    ax.plot(tx2.index, tx2.values, color=L2_COLOR, label=lab["l2"])
    tx1 = _roll_avg(agg["l1_tx"])
    if tx1.notna().any():
        ax.plot(tx1.index, tx1.values, color=L1_COLOR, ls=(0, (4, 2)), label=lab["l1"])
    ax.set_yscale("log")
    ax.set_ylabel(lab["tx_y"])
    ax.set_title(lab["b"])
    ax.yaxis.set_major_formatter(FuncFormatter(si_fmt))
    ax.grid(axis="y", which="major", zorder=0)
    _date_axis(ax)
    mark_forks(ax, lab, annotate=False)
    ax.legend(loc="upper left", ncol=1, handlelength=1.8)


def _fork_groups(agg: pd.DataFrame, lab) -> list:
    """Moyennes `WINDOW` jours avant/après chaque fork, pour L2 puis L1.

    Renvoie au plus quatre groupes (Dencun L2, Dencun L1, Pectra L2, Pectra L1) ;
    un groupe est omis si l'une des deux fenêtres est vide — typiquement Pectra
    lorsque l'historique s'arrête trop tôt.
    """
    groups = []
    for fork_key, fork_date in FORKS:
        pre = agg.loc[fork_date - pd.Timedelta(days=WINDOW):fork_date - pd.Timedelta(days=1)]
        post = agg.loc[fork_date + pd.Timedelta(days=1):fork_date + pd.Timedelta(days=WINDOW)]
        for layer, col in (("l2", "l2_fee"), ("l1", "l1_fee")):
            if col not in agg.columns:
                continue
            b, a = pre[col].mean(), post[col].mean()
            if not (np.isfinite(b) and np.isfinite(a)):
                print(f"[warn] fenetre {fork_key}/{layer} incomplete, groupe omis du panneau (c)",
                      file=sys.stderr)
                continue
            short = lab[fork_key + "_short"]
            groups.append({
                "fork": fork_key,
                "layer": layer,
                "label": f"{lab[layer]}\n{short}",
                "key": f"{short} - {lab[layer]}",
                "before": float(b),
                "after": float(a),
            })
    return groups


def _panel_c(ax, agg, lab) -> pd.DataFrame:
    """(c) Coût médian avant/après fork : 4 colonnes (Dencun L2/L1, Pectra L2/L1)."""
    groups = _fork_groups(agg, lab)
    if not groups:
        raise SystemExit("Aucune fenêtre de fork exploitable : le panneau (c) ne peut être tracé.")

    x = np.arange(len(groups))
    before = [g["before"] for g in groups]
    after = [g["after"] for g in groups]
    w = 0.32

    ax.bar(x - w / 2, before, w, label=lab["before"],
           color=BAND_COLOR, edgecolor=GREY, linewidth=0.5, zorder=2)
    ax.bar(x + w / 2, after, w, label=lab["after"],
           color=L2_COLOR, edgecolor=GREY, linewidth=0.5, zorder=2)
    for xi, b, a in zip(x, before, after):
        delta = 100 * (1 - a / b)
        sign = "−" if delta >= 0 else "+"
        color = "#B3202C" if delta >= 0 else GREY
        ax.annotate(f"{sign}{abs(delta):.1f} %", xy=(xi, max(b, a)),
                    xytext=(0, 3), textcoords="offset points",
                    ha="center", va="bottom", fontsize=ANN_FONT, color=color)

    ax.set_yscale("log")
    ax.set_xticks(x, [g["label"] for g in groups])
    ax.set_xlim(-0.6, len(groups) - 0.4)
    ax.set_ylabel(lab["bar_y"])
    ax.set_title(lab["c"])
    ax.yaxis.set_major_formatter(FuncFormatter(usd_fmt))
    ax.grid(axis="y", which="major", zorder=0)
    ax.set_ylim(top=max(max(before), max(after)) * 12)

    # Séparateur entre blocs de forks ; le nom du fork figure déjà sur la
    # seconde ligne de chaque étiquette d'abscisse.
    for i in range(1, len(groups)):
        if groups[i]["fork"] != groups[i - 1]["fork"]:
            ax.axvline(i - 0.5, color="#BFBFBF", lw=0.7, ls=(0, (3, 3)), zorder=1)

    ax.legend(loc="upper right", ncol=2, handlelength=1.4)

    summary = pd.DataFrame({
        "fork": [g["fork"] for g in groups],
        "layer": [g["layer"] for g in groups],
        "mean_before_usd": before,
        "mean_after_usd": after,
        "reduction_pct": [100 * (1 - a / b) for b, a in zip(before, after)],
    }, index=[g["key"] for g in groups])

    # Facteur de coût L1/L2, avant et après chaque fork (si les deux couches existent)
    for key, _ in FORKS:
        pair = {g["layer"]: g for g in groups if g["fork"] == key}
        if {"l1", "l2"} <= set(pair):
            rows = summary["fork"] == key
            summary.loc[rows, "l1_over_l2_before"] = pair["l1"]["before"] / pair["l2"]["before"]
            summary.loc[rows, "l1_over_l2_after"] = pair["l1"]["after"] / pair["l2"]["after"]
    return summary


def make_figure(df: pd.DataFrame, lang: str = "en", start: str | None = None,
                band: bool = True):
    """Figure combinée (a, b, c) et une figure autonome par panneau.

    Renvoie `(fig, panels, summary)` où `panels` vaut {"a": fig_a, "b": fig_b, "c": fig_c}.
    """
    lab = LABELS[lang]
    agg = aggregate(df)
    if start:
        agg = agg.loc[start:]

    fig, axes = plt.subplots(3, 1, figsize=(7.0, 8.0),
                             gridspec_kw={"height_ratios": [1.15, 0.95, 0.9], "hspace": 0.62})
    ax_a, ax_b, ax_c = axes
    _panel_a(ax_a, agg, lab, band=band)
    _panel_b(ax_b, agg, lab)
    summary = _panel_c(ax_c, agg, lab)
    for ax in axes:
        ax.set_axisbelow(True)

    # Mêmes panneaux tracés isolément : une image exploitable par panneau.
    # Panneaux (a) et (b) volontairement plus plats : à largeur de colonne
    # constante, une hauteur réduite économise de la place dans l'article.
    draws = (
        ("a", lambda ax: _panel_a(ax, agg, lab, band=band, head=3.2), 2.15),
        ("b", lambda ax: _panel_b(ax, agg, lab), 1.85),
        ("c", lambda ax: _panel_c(ax, agg, lab), 2.9),
    )
    short_ylabel = {"a": lab["fee_y_short"], "b": lab["tx_y_short"]}
    panels = {}
    for key, draw, height in draws:
        f, ax = plt.subplots(figsize=(7.0, height))
        draw(ax)
        if key in short_ylabel:
            ax.set_ylabel(short_ylabel[key])
        ax.set_axisbelow(True)
        panels[key] = f

    n = agg.loc[DENCUN - pd.Timedelta(days=WINDOW):DENCUN + pd.Timedelta(days=WINDOW),
                "n_chains"]
    if len(n):
        print(f"[info] rollups agreges autour de Dencun : "
              f"{int(n.min())}-{int(n.max())} chaines/jour", file=sys.stderr)

    return fig, panels, summary


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
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="préfixe des fichiers de sortie")
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
    fig, panels, summary = make_figure(df, lang=args.lang, start=args.start, band=args.band)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    outputs = [(out, fig)] + [(out.with_name(f"{out.name}_{k}"), panels[k]) for k in sorted(panels)]
    for fmt in [f.strip() for f in args.formats.split(",") if f.strip()]:
        for stem, f in outputs:
            path = f"{stem}.{fmt}"
            f.savefig(path)
            print(f"[write] {path}", file=sys.stderr)

    print("\n" + summary.round(4).to_string())
    summary.round(4).to_csv(f"{out}_summary.csv")
    print(f"\n[write] {out}_summary.csv", file=sys.stderr)


if __name__ == "__main__":
    main()
