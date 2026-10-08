#!/usr/bin/env python3
"""Exp 1 : grandeurs du modèle sur le dataset complet, sans calibration.

python scripts/analysis/exp1.py --from bench-out/20260825_120620

Une phase p n'est pas une ressource x. T_p désigne ici le temps écoulé
observé ; W_CPU,p est le temps CPU cumulé (secondes-cœur). Leur rapport
R_eff,p est une utilisation moyenne, pas une mesure de R_x^phys.
Les dérivées sont estimées sur les moyennes par taille, sans ajustement.
Voir README.md généré dans le dossier de sortie pour les limites du modèle.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
COLORS = {"rapidsnark": "#0072B2", "snarkjs": "#CC79A7", "": "#009E73"}
STYLES = {"prove": "-", "verify": "--", "env": "-."}


def log_slope(ns, values):
    """Dérivée logarithmique centrée, y compris sur une grille non uniforme.

    Les extrémités utilisent une différence unilatérale d'ordre 1.
    Une série non positive ne permet pas de calculer cette dérivée.
    """
    ns, values = np.asarray(ns, dtype=float), np.asarray(values, dtype=float)
    if len(ns) < 2 or np.any(~np.isfinite(values)) or np.any(values <= 0):
        return np.full(len(ns), np.nan)
    if np.any(ns <= 0) or np.any(np.diff(ns) <= 0):
        raise ValueError("Les tailles doivent être positives et strictement croissantes")
    return np.gradient(np.log(values), np.log(ns), edge_order=1)


def number(row, key):
    try:
        value = float(row[key])
        return value if np.isfinite(value) else np.nan
    except (KeyError, TypeError, ValueError):
        return np.nan


def pooled_verify_rows(run_dir):
    """Réunit les répétitions du même vérificateur, avant toute dérivée.

    L'identifiant de répétition reste (taille, prover, rep) : les rep=1
    des deux provers sont deux observations, pas deux steps à additionner.
    Les steps d'une répétition sont sommés, sauf le RSS (maximum).
    Comme le collecteur, on utilise l'écart-type population (ddof=0).
    """
    metrics = ("wall_s", "cpu_total_s", "rss_peak_bytes", "io_read_bytes", "io_write_bytes")
    reps = {}
    with (run_dir / "steps.csv").open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row["phase"] == "verify":
                key = (int(row["size"]), row.get("prover", ""), row["rep"])
                reps.setdefault(key, []).append(row)
    by_n = {}
    for (n, _, _), steps in reps.items():
        if not all(r.get("ok", "").lower() in ("true", "1") for r in steps):
            continue
        values = {m: (np.max if m == "rss_peak_bytes" else np.sum)(
            [number(r, m) for r in steps]) for m in metrics}
        if not np.isfinite(values["wall_s"]) or values["wall_s"] <= 0:
            raise ValueError(f"Temps de vérification invalide pour N={n}")
        by_n.setdefault(n, []).append(values)
    rows = []
    for n, observations in sorted(by_n.items()):
        row = dict(n=n, phase="verify", prover="", ok="True", n_reps=len(observations))
        for metric in metrics:
            values = [obs[metric] for obs in observations]
            row.update({f"{metric}_mean": np.mean(values),
                        f"{metric}_stdev": np.std(values, ddof=0),
                        f"{metric}_max": np.max(values)})
        rows.append(row)
    return rows


def load_series(run_dir, phases):
    buckets = {}
    with (run_dir / "phases.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["phase"] != "verify"]
    if "verify" in phases:
        rows.extend(pooled_verify_rows(run_dir))
    for row in rows:
        if row["phase"] not in phases or row.get("ok", "").lower() not in ("true", "1"):
            continue
        n, wall = number(row, "n"), number(row, "wall_s_mean")
        if not np.isfinite(n) or n <= 0 or n != int(n) or not np.isfinite(wall) or wall <= 0:
            raise ValueError(f"Taille ou temps invalide : {row}")
        key = row["phase"], row.get("prover", "")
        by_n = buckets.setdefault(key, {})
        if int(n) in by_n:
            raise ValueError(f"Mesure agrégée dupliquée : {key}, N={n}")
        by_n[int(n)] = row
    result = []
    for (phase, prover), by_n in sorted(buckets.items()):
        ns = sorted(by_n)
        rows = [by_n[n] for n in ns]
        arr = lambda key: np.array([number(r, key) for r in rows])
        n = np.array(ns)
        t, w = arr("wall_s_mean"), arr("cpu_total_s_mean")
        w = np.where(w > 0, w, np.nan)
        r_eff = w / t
        gamma_t, gamma_w = log_slope(n, t), log_slope(n, w)
        result.append(dict(
            phase=phase, prover=prover, n=n, t=t, w=w, r_eff=r_eff,
            t_per_tx=t / n, w_per_tx=w / n, throughput=n / t,
            gamma_t=gamma_t, gamma_w=gamma_w, xi_w=1 - gamma_w,
            gamma_ram=log_slope(n, arr("rss_peak_bytes_mean")),
            xi_ram=1 - log_slope(n, arr("rss_peak_bytes_mean")),
            eta=log_slope(n, n / t), eta_r=log_slope(n, r_eff),
            wall_sd=arr("wall_s_stdev"), cpu_sd=arr("cpu_total_s_stdev"),
            rss=arr("rss_peak_bytes_mean"), rss_max=arr("rss_peak_bytes_max"),
            rss_per_tx=arr("rss_peak_bytes_mean") / n,
            io_read=arr("io_read_bytes_mean"), io_write=arr("io_write_bytes_mean"),
            n_reps=arr("n_reps"),
        ))
    if not result:
        raise ValueError("Aucune série exploitable pour les phases demandées")
    return result


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({k: "" if isinstance(v, (float, np.floating)) and not np.isfinite(v)
                             else v for k, v in row.items()})


def export_metrics(series, outdir):
    columns = dict(n="n", T_phase_s="t", W_cpu_core_s="w",
                   T_per_tx_s="t_per_tx", W_cpu_per_tx_core_s="w_per_tx",
                   Lambda_phase_tx_s="throughput", R_eff_core_s_per_s="r_eff",
                   gamma_time="gamma_t", gamma_cpu="gamma_w", xi_cpu="xi_w",
                   gamma_ram="gamma_ram", xi_ram="xi_ram",
                   eta_throughput="eta", eta_R_eff="eta_r",
                   gamma_throughput="eta",
                   wall_stdev_s="wall_sd", cpu_stdev_core_s="cpu_sd",
                   rss_peak_mean_bytes="rss", rss_peak_max_bytes="rss_max",
                   rss_peak_mean_per_tx_bytes="rss_per_tx",
                   W_io_read_bytes="io_read", W_io_write_bytes="io_write", n_reps="n_reps")
    rows = []
    for s in series:
        for i in range(len(s["n"])):
            rows.append(dict(phase=s["phase"], prover=s["prover"],
                             **{col: s[key][i] for col, key in columns.items()}))
    write_csv(outdir / "exp1.csv", rows)


def observed_optima(series, deadline, ram_gb):
    """Optimum discret par phase dans le domaine mesuré ; aucune extrapolation."""
    rows = []
    for s in series:
        eligible = np.ones(len(s["n"]), dtype=bool)
        if deadline is not None:
            eligible &= s["t"] <= deadline
        if ram_gb is not None:
            eligible &= np.isfinite(s["rss_max"]) & (s["rss_max"] <= ram_gb * 1e9)
        candidates = np.flatnonzero(eligible)
        throughput = s["throughput"].copy()
        if deadline is not None:
            throughput = s["n"] / np.maximum(s["t"], deadline)
        best = candidates[np.argmax(throughput[candidates])] if len(candidates) else None
        rows.append(dict(
            phase=s["phase"], prover=s["prover"],
            criterion="N/Delta (phase admissible)" if deadline is not None else "N/T_phase",
            Delta_s=deadline, ram_budget_GB=ram_gb,
            admissible_measured_sizes=len(candidates),
            N_best_measured="" if best is None else int(s["n"][best]),
            throughput_tx_s="" if best is None else float(throughput[best]),
            at_largest_admissible="" if best is None else bool(best == candidates[-1]),
        ))
    return rows


def figures(series, outdir, args):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": args.fig_font_size, "axes.grid": True,
                         "grid.alpha": .22, "savefig.bbox": "tight"})

    def shade_regimes(ax, key):
        regimes = {
            "gamma_t": (1, True, "Time per tx decreases", "Time per tx increases"),
            "gamma_w": (1, True, "CPU work per tx decreases", "CPU work per tx increases"),
            "gamma_ram": (1, True, "Peak memory per tx decreases", "Peak memory per tx increases"),
            "xi_w": (0, False, "CPU work per tx decreases", "CPU work per tx increases"),
            "xi_ram": (0, False, "Peak memory per tx decreases", "Peak memory per tx increases"),
            "eta": (0, False, "Throughput increases", "Throughput decreases"),
            "comparison": (0, False, "Positive elasticity: gain", "Negative elasticity: loss"),
        }
        if key not in regimes:
            return False
        threshold, good_below, good_label, bad_label = regimes[key]
        lo, hi = ax.get_ylim()
        padding = .16 * max(hi - lo, .1)
        lo, hi = min(lo, threshold) - padding, max(hi, threshold) + padding
        ax.set_ylim(lo, hi)
        green, orange = "#009E73", "#E69F00"
        below_color, above_color = (green, orange) if good_below else (orange, green)
        ax.axhspan(lo, threshold, color=below_color, alpha=.08, zorder=0)
        ax.axhspan(threshold, hi, color=above_color, alpha=.10, zorder=0)
        ax.axhline(threshold, color="0.35", linestyle=":", linewidth=1.3)
        below_label, above_label = ((good_label, bad_label) if good_below
                                    else (bad_label, good_label))
        for y, label, color, va in [(lo, below_label, below_color, "bottom"),
                                     (hi, above_label, above_color, "top")]:
            ax.annotate(label, (.025, y), xycoords=ax.get_yaxis_transform(),
                        xytext=(0, 4 if va == "bottom" else -4),
                        textcoords="offset points", va=va, color=color,
                        fontsize=args.fig_font_size * .85)
        return True

    def panels(name, specs, note, reference=None, hidden_keys=()):
        fig, axes = plt.subplots(2, 2, figsize=(args.fig_width, args.fig_width * .78),
                                 layout="constrained")
        for index, (ax, (key, title, ylabel, log_y)) in enumerate(zip(axes.flat, specs)):
            ax.set_title(f"({chr(97 + index)}) {title}", loc="left")
            for s in series:
                label = f"{s['prover']}/{s['phase']}" if s["prover"] else s["phase"]
                y = s[key]
                y = np.where(y > 0, y, np.nan) if log_y else y
                ax.plot(s["n"], y, marker="o", markersize=3, label=label,
                        color=COLORS.get(s["prover"], "#555555"),
                        linestyle=STYLES.get(s["phase"], "-"))
                sd_key = {"t": "wall_sd", "w": "cpu_sd"}.get(key)
                if sd_key:
                    sd = s[sd_key]
                    lower = np.where(y - sd > 0, y - sd, np.nan)
                    ax.fill_between(s["n"], lower, y + sd, alpha=.12,
                                    color=COLORS.get(s["prover"], "#555555"))
            ax.set_xscale("log", base=2)
            if log_y:
                ax.set_yscale("log")
            ax.set(xlabel=r"Batch size $N$ [tx]", ylabel=ylabel)
            shaded = shade_regimes(ax, key)
            if name == "03_elasticities" and key in ("xi_w", "xi_ram", "eta"):
                # Gains increase upwards on the primary axis; the complementary
                # resource/time elasticity decreases upwards on the right.
                resource = r"\mathrm{CPU}" if key == "xi_w" else r"\mathrm{RAM}"
                right = ax.secondary_yaxis("right", functions=(lambda y: 1 - y,
                                                                 lambda y: 1 - y))
                right.set_ylabel(r"$\gamma_{T,p}=d\ln T_p/d\ln N=1-\gamma_{\Lambda,p}$"
                                 if key == "eta" else
                                 rf"$\gamma_{{{resource},p}}=1-\xi_{{{resource},p}}$")
                # Corresponding ticks make left 0 <-> right 1 and left 1 <-> right 0 explicit.
                right.set_yticks(1 - ax.get_yticks())
                right.tick_params(axis="y", colors="0.35")
                right.yaxis.label.set_color("0.35")
            if reference is not None and not shaded:
                ax.axhline(1 if key == "gamma_w" else reference,
                           color="0.4", linestyle=":", linewidth=1)
        handles, labels = axes.flat[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="outside lower center", ncol=min(3, len(labels)),
                   fontsize="small")
        fig.suptitle(note, fontsize=args.fig_font_size)
        # Resolve the original four-panel layout before hiding diagnostics.
        # Freeze it so the remaining axes keep their positions and aspect ratios.
        fig.canvas.draw()
        bounds = fig.get_tightbbox(fig.canvas.get_renderer()).padded(.1)
        fig.set_layout_engine("none")
        for ax, spec in zip(axes.flat, specs):
            if spec[0] in hidden_keys:
                ax.set_visible(False)
        for ext in args.fig_format:
            fig.savefig(outdir / f"{name}.{ext}", dpi=args.fig_dpi, bbox_inches=bounds)
        plt.close(fig)

    panels("01_times", [
        ("t", "Batch processing time", r"$T_p(N)$ [s]", True),
        ("t_per_tx", "Processing time per transaction", r"$T_p(N)/N$ [s/tx]", True),
        ("throughput", "Equivalent phase throughput", r"$\Lambda_p(N)=N/T_p(N)$ [tx/s]", True),
        ("eta", "Throughput elasticity", r"$\gamma_{\Lambda,p}(N)=d\ln\Lambda_p/d\ln N$", False),
    ], "Measured phase p: time, throughput and batching gain\nTime shading: +/- 1 SD; phase throughput is not system throughput")
    panels("02_work_resources", [
        ("w", "Total CPU work", r"$W_{\mathrm{CPU},p}(N)$ [core s]", True),
        ("w_per_tx", "Mean CPU work per transaction", r"$\bar W_{\mathrm{CPU},p}(N)=W_{\mathrm{CPU},p}(N)/N$ [core s/tx]", True),
        ("rss", "Peak memory occupancy", r"$M_p^{\mathrm{peak}}(N)$ [bytes]", True),
        ("rss_per_tx", "Mean peak memory per transaction", r"$M_p^{\mathrm{peak}}(N)/N$ [bytes/tx]", True),
    ], "CPU work and memory occupancy per phase p\nCPU shading: +/- 1 SD; memory: mean of repetition peaks")
    panels("03_elasticities", [
        ("xi_w", "CPU work and amortization", r"$\xi_{\mathrm{CPU},p}=1-\gamma_{\mathrm{CPU},p}$", False),
        ("xi_ram", "RAM occupancy and amortization", r"$\xi_{\mathrm{RAM},p}=1-\gamma_{\mathrm{RAM},p}$", False),
        ("eta", "Throughput and amortization", r"$\gamma_{\Lambda,p}=d\ln\Lambda_p/d\ln N$", False),
        ("eta_r", "CPU utilization correction", r"$d\ln R_{\mathrm{eff},p}/d\ln N$", False),
    ], "Resource amortization and throughput gain (dimensionless)\nHigher on the left axis = greater batching gain; right axes = 1 - left axes",
           reference=0, hidden_keys=("eta_r",))

    # Compare independently measured CPU work and phase wall time. Their
    # elasticities need not coincide when observed utilization varies with N.
    comparison = sorted(series, key=lambda s: (s["phase"] != "prove",
                                               s["phase"] == "env", s["prover"]))
    fig, axes = plt.subplots(2, 2, figsize=(args.fig_width, args.fig_width * .78),
                             layout="constrained")
    finite = np.concatenate([s[key][np.isfinite(s[key])]
                             for s in comparison for key in ("xi_w", "xi_ram", "eta")])
    limits = (min(0., finite.min()), max(0., finite.max())) if finite.size else (-.1, 1.)
    for index, ax in enumerate(axes.flat):
        ax.set_xscale("log", base=2)
        ax.set(xlabel=r"Batch size $N$ [tx]", ylabel="Elasticity (dimensionless)",
               ylim=limits)
        if index >= len(comparison):
            continue
        s = comparison[index]
        name = f"{s['prover']}/{s['phase']}" if s["prover"] else s["phase"]
        ax.set_title(f"({chr(97 + index)}) {name}", loc="left")
        ax.plot(s["n"], s["xi_w"], color="#0072B2", linestyle="-",
                marker="o", markersize=3,
                label=r"CPU amortization $\xi_{\mathrm{CPU},p}=1-\gamma_{\mathrm{CPU},p}$")
        ax.plot(s["n"], s["eta"], color="#CC79A7", linestyle="--",
                marker="s", markersize=3,
                label=r"Throughput elasticity $\gamma_{\Lambda,p}=d\ln\Lambda_p/d\ln N$")
        ax.plot(s["n"], s["xi_ram"], color="#009E73", linestyle="-.",
                marker="^", markersize=3,
                label=r"RAM occupancy amortization $\xi_{\mathrm{RAM},p}=1-\gamma_{\mathrm{RAM},p}$")
        shade_regimes(ax, "comparison")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncol=1, fontsize="small")
    fig.suptitle("CPU and RAM amortization versus phase throughput elasticity\n" +
                 r"$\gamma_{\Lambda,p}=d\ln\Lambda_p/d\ln N$; RAM occupancy amortization need not equal throughput elasticity",
                 fontsize=args.fig_font_size)
    fig.canvas.draw()
    bounds = fig.get_tightbbox(fig.canvas.get_renderer()).padded(.1)
    fig.set_layout_engine("none")
    for ax in list(axes.flat)[len(comparison):]:
        ax.set_visible(False)
    for ext in args.fig_format:
        fig.savefig(outdir / f"04_amortization_vs_throughput.{ext}",
                    dpi=args.fig_dpi, bbox_inches=bounds)
    plt.close(fig)


def report(series, manifest, args, optima):
    lines = ["# Exp 1 — dataset complet", "",
             f"Source : `{args.run_dir.resolve()}`.", "",
             "Reproduction : `python scripts/analysis/exp1.py --from bench-out/20260825_120620`. "
             "Ajouter `--phases prove verify env` pour inclure le setup ponctuel. "
             "Les options de cette exécution sont conservées dans `analysis.json`.", "",
             "Cette expérience décrit les mesures sur toutes les tailles disponibles. "
             "Elle ne calibre pas de modèle prédictif (Exp 2). Les phases prove restent "
             "séparées par prover. Une seule série verify regroupe, à chaque taille, "
             "les répétitions réussies du même vérificateur présentes dans steps.csv "
             "(20 par taille dans ce run). Les moyennes, écarts-types population "
             "(ddof=0) et maxima sont recalculés sur ces observations, puis les "
             "élasticités sur les moyennes regroupées. Les données brutes restent "
             "séparées et inchangées ; la fusion suppose des conditions comparables.", "",
             "## Correspondance avec le papier", "",
             "| Grandeur | Interprétation et unité |", "|---|---|",
             "| T_p(N) | Temps écoulé d'une phase p, en s ; candidat empirique à un temps de service, pas T_x d'une ressource isolée. |",
             "| W_CPU,p(N) | Temps CPU cumulé, en secondes-cœur (travail consommé). Ce n'est pas un nombre d'opérations. |",
             "| R_eff,p(N) | W_CPU,p / T_p, en secondes-cœur/s ; utilisation moyenne observée, pas capacité physique. |",
             "| W_CPU,p/N | Travail CPU moyen par transaction, en secondes-cœur/tx. |",
             "| Lambda_p(N) = N/T_p(N) | Débit équivalent de la phase isolée, pas débit mesuré du rollup en régime établi. |",
             "| gamma_CPU, xi_CPU | d ln W_CPU / d ln N et 1 - gamma_CPU. |",
             "| gamma_Lambda,p | d ln(N/T_p)/d ln N = xi_CPU + d ln R_eff/d ln N. |",
             "| W_IO | Volumes lus/écrits observés, en octets ; capacités de stockage non mesurées. |",
             "| M_p^peak(N) | Moyenne des pics RSS des répétitions ; occupation mémoire, ni travail cumulatif ni capacité en work/s. |", "",
             "L'indice p désigne une phase expérimentale ; x reste réservé aux "
             "ressources du modèle. Les notations Lambda_p, gamma_Lambda,p, R_eff,p et "
             "M_p^peak explicitent la correspondance expérimentale. Dans le papier, "
             "R_x^phys constant implique d ln T_x / d ln N = gamma_x, puis "
             "d ln Lambda_x / d ln N = xi_x. Pour nos phases, gamma_Lambda,p = xi_CPU,p "
             "+ d ln R_eff,p / d ln N : on distingue donc gamma_Lambda,p et xi_CPU,p. "
             "L'élasticité du temps reste exportée dans le CSV comme grandeur "
             "intermédiaire et apparaît en 03.C sur l'axe droit, associé à l'élasticité du débit sur l'axe gauche. "
             "Toutes les élasticités sont sans dimension.", "",
             "Les dérivées utilisent des différences finies logarithmiques centrées, "
             "unilatérales aux extrémités. Elles décrivent les moyennes mesurées et "
             "peuvent amplifier le bruit. Les écarts-types et nombres de répétitions "
             "sont exportés ; aucune incertitude de dérivée n'est estimée. Un passage "
             "ponctuel de gamma par 1 ne démontre ni plateau ni optimum global.", "",
             "## Figures et tables", "",
             "- `01_times` : temps T_p, temps/transaction, débit équivalent Lambda_p et élasticité du débit gamma_Lambda,p.",
             "- `02_work_resources` : travail CPU (A), travail/transaction (B), moyenne des pics mémoire par batch (C) et par transaction (D).",
             "Le panneau 02.D divise la moyenne des pics RSS par N : il décrit une occupation mémoire normalisée par transaction, pas une mesure de mémoire propre à chaque transaction ni un travail cumulatif.",
             "- `03_elasticities` : A amortissement CPU, B amortissement de l'occupation RAM, C élasticité du débit ; emplacement D vide. A et B : xi à gauche, gamma = 1 - xi à droite. C : gamma_Lambda,p à gauche, gamma_T,p = d ln T_p / d ln N = 1 - gamma_Lambda,p à droite. Les axes gauches croissent tous vers le haut : plus haut signifie un gain relatif plus grand, vert au-dessus de zéro et orange en dessous. Les axes droits sont inversés (0 à gauche = 1 à droite ; 1 à gauche = 0 à droite).",
             "Pour la RAM, gamma_RAM,p = d ln M_p^peak / d ln N et xi_RAM,p = -d ln(M_p^peak/N) / d ln N. Cet amortissement décrit la diminution de l'occupation mémoire normalisée par transaction ; il n'est pas automatiquement égal à l'élasticité du débit.",
             "- `04_amortization_vs_throughput` : comparaison de xi_CPU,p, xi_RAM,p et gamma_Lambda,p dans un panneau par prover et un pour verify ; mêmes échelles pour tous les panneaux. Le setup occupe le quatrième panneau si env est demandé.",
             "La grille 2 × 2 et les proportions des panneaux sont conservées ; les panneaux restants ne sont pas étirés.",
             "- `exp1.csv` : toutes les grandeurs par phase, prover et taille.",
             "- `observed_optima.csv` : maximum du débit par phase parmi les tailles mesurées admissibles.",
             "- `circuits.csv` : contraintes et tailles d'artefacts du manifeste (descripteurs, pas travail CPU ni volume DA publié).", "",
             "Les fonds des panneaux d'élasticité reprennent la convention de legacy : "
             "vert lorsque le coût par transaction diminue (gamma < 1 ou xi > 0), "
             "orange lorsqu'il augmente. Pour gamma_Lambda,p, vert signifie débit croissant "
             "(gamma_Lambda,p > 0), orange débit décroissant. La ligne pointillée marque le "
             "seuil de gain marginal nul. R_eff, son élasticité et les volumes "
             "d'E/S restent disponibles dans exp1.csv, mais ne sont plus "
             "représentés dans les figures principales.", "",
             "Dans la figure 04, la courbe bleue continue représente l'amortissement "
             "du travail CPU (1 - gamma_CPU,p), et la courbe rose discontinue "
             "l'élasticité du débit équivalent (d ln Lambda_p / d ln N). La courbe "
             "verte à tirets-points représente l'amortissement du pic mémoire "
             "(xi_RAM,p = 1 - gamma_RAM,p). Il ne s'agit pas de la moyenne temporelle "
             "de la mémoire et aucune égalité avec l'élasticité du débit n'est supposée. Le fond vert "
             "indique une élasticité positive : diminution du travail CPU/transaction "
             "pour xi, augmentation du débit pour gamma_Lambda,p ; l'orange indique l'inverse. "
             "L'écart eta - xi est la dérivée logarithmique de R_eff. Cette "
             "comparaison illustre les observations ; elle ne démontre pas "
             "indépendamment l'identité théorique. L'égalité du papier suppose "
             "une capacité de ressource constante et un débit associé à cette "
             "ressource, tandis que nos temps sont mesurés par phase.", "",
             "La notation gamma_Lambda,p remplace eta_p pour identifier explicitement "
             "la grandeur dérivée : le débit Lambda_p. Elle ne doit pas être confondue "
             "avec gamma_CPU,p, l'élasticité du travail. Le CSV exporte gamma_throughput "
             "et conserve eta_throughput comme alias pour compatibilité.", "",
             "## Constats sur ce run", ""]
    for s in series:
        finite = s["r_eff"][np.isfinite(s["r_eff"])]
        name = f"{s['prover']}/{s['phase']}".strip("/")
        span = f"{finite.min():.3f} à {finite.max():.3f}" if len(finite) else "indisponible"
        lines.append(f"- {name} : {len(s['n'])} tailles, N={s['n'][0]}…{s['n'][-1]} ; R_eff : {span} secondes-cœur/s.")
    env = manifest.get("env", {})
    lines += ["", f"Le manifeste annonce {env.get('cpu_count', '?')} CPU logiques et "
              f"{env.get('mem_total_gb', '?')} GB de RAM. Le nombre de CPU est une "
              "information de configuration, pas une calibration de capacité soutenable. "
              "Les limites d'affinité, de cgroup, la fréquence et la contention ne sont pas établies ici.", "",
              "## Optimum et contraintes", "",
              "Sans --deadline, le critère est N/T_p. Avec --deadline Delta, on "
              "retient les tailles où T_p <= Delta et maximise N/Delta. "
              "--ram-budget filtre sur le maximum des pics RSS des répétitions. "
              "Ces critères concernent une phase isolée et ne prouvent pas la "
              "faisabilité du système complet. Le budget RAM n'est pas automatiquement "
              "la RAM totale de la machine. Aucune extrapolation, interpolation de "
              "franchissement ou borne d'amortissement artificielle n'est utilisée.", ""]
    for row in optima:
        name = f"{row['prover']}/{row['phase']}".strip("/")
        lines.append(f"- {name} : N retenu = "
                     f"{row['N_best_measured'] or 'aucune taille admissible'}, critère {row['criterion']}.")
    lines += ["", "## Ce que le dataset ne permet pas d'identifier", "",
              "Le modèle suppose R_x^phys constant dans chaque configuration. "
              "R_eff varie avec N : les élasticités CPU et temporelle ne sont donc "
              "pas interchangeables. W_CPU/R_eff=T_p est une identité descriptive, "
              "pas une validation indépendante de T_x=W_x/R_x^phys.", "",
              "Les phases prove et verify consomment plusieurs ressources et peuvent "
              "partager la machine. Leur maximum ne donne pas automatiquement tau. "
              "On ne calcule donc pas B_x, H_x, les transitions de bottleneck, "
              "Lambda_svc du système ou N_max physique/protocolaire sans capacités "
              "calibrées, allocation des ressources, mesures concurrentes et limites K_x^prot. "
              "La vérification de ce run est locale : elle ne mesure pas le gas ou la latence L1. "
              "La phase env est un setup ponctuel et ne doit pas être ajoutée au coût récurrent "
              "de chaque batch sans une politique explicite d'amortissement.", "",
              "W_x,0 et W_x,v ne sont pas séparément identifiables à partir des seuls "
              "totaux sans hypothèse de forme ou expérience dédiée. Un ajustement "
              "affine/superlinéaire et sa validation relèvent de l'Exp 2.", "",
              "## Mesures à ajouter ultérieurement à measure_zk_resources.py", "",
              "Les données actuelles suffisent pour les figures ci-dessus ; le collecteur "
              "n'a pas été modifié pour cette expérience.", "",
              "- Enregistrer affinité CPU, quotas cgroup, nombre de threads, fréquence, "
              "RAM disponible et portée/source de chaque compteur (processus ou système).",
              "- Calibrer séparément les capacités CPU et I/O sous allocation fixe ; "
              "si W désigne des opérations, mesurer instructions/cycles et leur capacité correspondante.",
              "- Mesurer plusieurs batches concurrents : intervalle entre sorties, cadence "
              "de release, latence, contention et allocation par phase pour tester tau et Lambda_svc.",
              "- Mesurer les volumes réellement publiés, le gas de vérification et "
              "les capacités/limites protocolaires avec leurs unités.",
              "- Améliorer si nécessaire les compteurs E/S des processus courts : "
              "l'échantillonnage peut manquer une partie de l'activité ; zéro n'est pas "
              "une preuve d'absence d'E/S.", "",
              "Les anciens résultats elasticity sont conservés sous `legacy/`, "
              "uniquement comme archive ; leurs conclusions ne sont pas celles d'Exp 1.", ""]
    return "\n".join(lines)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="run_dir", type=Path,
                        default=ROOT / "bench-out/20260825_120620")
    parser.add_argument("--outdir", type=Path, default=ROOT / "bench-out/exp1")
    parser.add_argument("--phases", nargs="+", choices=["prove", "verify", "env"],
                        default=["prove", "verify"])
    parser.add_argument("--deadline", type=float, help="Budget Delta explicite en secondes")
    parser.add_argument("--ram-budget", type=float, help="Budget mémoire explicite en GB décimaux")
    parser.add_argument("--fig-format", nargs="+", choices=["png", "pdf", "svg"], default=["png", "pdf"])
    parser.add_argument("--fig-dpi", type=int, default=300)
    parser.add_argument("--fig-width", type=float, default=8.5)
    parser.add_argument("--fig-font-size", type=float, default=8)
    parser.add_argument("--no-figures", action="store_true")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    for key in ("deadline", "ram_budget", "fig_dpi", "fig_width", "fig_font_size"):
        value = getattr(args, key)
        if value is not None and (not np.isfinite(value) or value <= 0):
            parser.error(f"--{key.replace('_', '-')} doit être positif et fini")
    with (args.run_dir / "manifest.json").open(encoding="utf-8") as f:
        manifest = json.load(f)
    series = load_series(args.run_dir, args.phases)
    args.outdir.mkdir(parents=True, exist_ok=True)
    export_metrics(series, args.outdir)
    optima = observed_optima(series, args.deadline, args.ram_budget)
    write_csv(args.outdir / "observed_optima.csv", optima)
    circuits = [dict(n=int(n), constraints=c.get("constraints"),
                     r1cs_bytes=c.get("artifacts", {}).get("circuit.r1cs"),
                     proof_json_bytes=c.get("artifacts", {}).get("proof.json"))
                for n, c in sorted(manifest.get("circuits", {}).items(), key=lambda item: int(item[0]))]
    write_csv(args.outdir / "circuits.csv", circuits)
    (args.outdir / "README.md").write_text(report(series, manifest, args, optima), encoding="utf-8")
    (args.outdir / "analysis.json").write_text(json.dumps(dict(
        source=str(args.run_dir.resolve()), phases=args.phases,
        deadline_s=args.deadline, ram_budget_GB=args.ram_budget,
        derivative="numpy.gradient(log(y), log(N)), edge_order=1",
        physical_capacities_calibrated=False,
        verify_aggregation="successful repetitions from steps.csv pooled by N; population SD",
    ), indent=2), encoding="utf-8")
    if not args.no_figures:
        figures(series, args.outdir, args)
    print(f"Exp 1 : {len(series)} series, {sum(len(s['n']) for s in series)} points -> {args.outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
