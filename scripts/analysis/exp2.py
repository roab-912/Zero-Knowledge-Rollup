#!/usr/bin/env python3
"""Exp 2: calibrate throughput on selected sizes; validate on unused sizes.

python scripts/analysis/exp2.py --train-max 1024
python scripts/analysis/exp2.py --train-max 512 --power 1.5 --outdir bench-out/exp2_512
python scripts/analysis/exp2.py --design logspace --calibration-count 6 --train-max 4096
python scripts/analysis/exp2.py --design manual --train-max 4096 --calibration-sizes 1 16 256 1024 4096
python scripts/analysis/exp2.py --compare-designs --outdir bench-out/exp2/designs
python scripts/analysis/exp2.py --combine-designs --train-max 4096 --outdir bench-out/exp2

Fits elapsed time, then derives throughput and its elasticity analytically.
The held-out sizes NEVER participate in fitting or model selection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares

if __package__:
    from .exp1 import COLORS as PROVER_COLORS
    from .exp1 import ROOT, load_series, log_slope, write_csv
else:
    from exp1 import COLORS as PROVER_COLORS
    from exp1 import ROOT, load_series, log_slope, write_csv

MODELS = ("affine", "superlinear")
COLORS = {"affine": "#0072B2", "superlinear": "#CC79A7"}


@dataclass
class TimeModel:
    name: str
    t0: float
    b: float
    a: float
    power: float
    train_log_rmse: float

    def time(self, n):
        n = np.asarray(n, dtype=float)
        return self.t0 + self.b * n + self.a * n ** self.power

    def throughput(self, n):
        return np.asarray(n, dtype=float) / self.time(n)

    def elasticity(self, n):
        n = np.asarray(n, dtype=float)
        return (self.t0 - (self.power - 1) * self.a * n ** self.power) / self.time(n)

    def intrinsic_optimum(self):
        # No positive superlinear term: no isolated finite maximum in this family.
        if self.a <= 0 or self.t0 <= 0:
            return None
        return (self.t0 / ((self.power - 1) * self.a)) ** (1 / self.power)


def fit_time(n, t, name, power=2.):
    """Constrained least squares of log(time); each calibration size weighs equally.

    N and time are normalized on CALIBRATION data only. Exponent is prescribed,
    not estimated from or chosen using the held-out results.
    """
    n, t = np.asarray(n, dtype=float), np.asarray(t, dtype=float)
    if name not in MODELS or not np.isfinite(power) or power <= 1:
        raise ValueError("Unknown model or invalid superlinear exponent")
    if len(n) < 4 or len(n) != len(t) or np.any(~np.isfinite(n)) or np.any(~np.isfinite(t)) or np.any(n <= 0) or np.any(t <= 0):
        raise ValueError("At least four positive, finite calibration points are required")
    scale_n, scale_t = float(max(n)), float(max(t))
    x, y = n / scale_n, t / scale_t
    columns = [np.ones(len(n)), x]
    if name == "superlinear":
        columns.append(x ** power)
    design = np.column_stack(columns)

    def residual(c):
        return np.log(np.maximum(design @ c, 1e-300)) - np.log(y)

    starts = ([.1, .9], [.9, .1]) if name == "affine" else ([.1, .8, .1], [.1, .1, .8], [.8, .1, .1])
    fits = [least_squares(residual, start, bounds=(0., np.inf), max_nfev=4000,
                          ftol=1e-12, xtol=1e-12, gtol=1e-12) for start in starts]
    valid = [f for f in fits if f.success and np.all(np.isfinite(f.x))]
    if not valid:
        raise ValueError(f"Fit failed for {name}")
    best = min(valid, key=lambda f: np.sum(f.fun ** 2))
    # Numerical zero is defined on normalized contributions at the last training size.
    c = np.where(best.x < 1e-10, 0., best.x)
    model = TimeModel(name, float(c[0] * scale_t), float(c[1] * scale_t / scale_n),
                      float(c[2] * scale_t / scale_n ** power) if len(c) == 3 else 0.,
                      power, 0.)
    model.train_log_rmse = float(np.sqrt(np.mean(np.log(model.time(n) / t) ** 2)))
    return model


def calibration_mask(n, train_max, design="prefix", count=6, sizes=None):
    """Select from available sizes, never from observed times or errors."""
    n = np.asarray(n)
    eligible = n[n <= train_max]
    if design == "manual":
        if not sizes or len(set(sizes)) != len(sizes) or not set(sizes).issubset(set(eligible)):
            raise ValueError("Manual sizes must be unique, present in the dataset and <= train-max")
        chosen = np.array(sorted(sizes))
    elif design == "prefix":
        chosen = eligible
    elif design in ("logspace", "large"):
        if not 4 <= count <= len(eligible):
            raise ValueError("Calibration count must be between 4 and the number of eligible sizes")
        if design == "large":
            chosen = np.concatenate((eligible[:1], eligible[-(count-1):]))
        else:
            # Choose nearest remaining log-spaced anchors, keeping endpoints.
            targets = np.linspace(np.log(eligible[0]), np.log(eligible[-1]), count)
            indices = {0, len(eligible)-1}
            for target in targets[1:-1]:
                available = [i for i in range(len(eligible)) if i not in indices]
                indices.add(min(available, key=lambda i: abs(np.log(eligible[i]) - target)))
            chosen = eligible[sorted(indices)]
    else:
        raise ValueError("Unknown calibration design")
    mask = np.isin(n, chosen)
    if mask.sum() < 4 or (~mask).sum() < 1:
        raise ValueError("Need >=4 training sizes and >=1 held-out size")
    return mask


def train_mask(s, train_max):
    return s.get("calibration_mask", s["n"] <= train_max)


def calibrate(series, train_max, power):
    mask = train_mask(series, train_max)
    if mask.sum() < 4 or (~mask).sum() < 1:
        raise ValueError("Need >=4 training sizes and >=1 held-out size")
    return {name: fit_time(series["n"][mask], series["t"][mask], name, power)
            for name in MODELS}


def optimum_comparison(s, model, tolerance=.05):
    """Compare choices on the SAME tested grid; report continuous prediction separately."""
    n, observed = s["n"], s["throughput"]
    pred_i = int(np.argmax(model.throughput(n)))
    obs_i = int(np.argmax(observed))
    n0 = model.intrinsic_optimum()
    continuous = (float(np.clip(n0, n[0], n[-1])) if n0 is not None else
                  float(n[0] if model.t0 == 0 else n[-1]))
    integers = np.unique([int(np.floor(continuous)), int(np.ceil(continuous))])
    integer_best = int(integers[np.argmax(model.throughput(integers))])
    retained = float(observed[pred_i] / observed[obs_i])
    acceptable = observed / observed[obs_i] >= 1 - tolerance
    return dict(
        phase=s["phase"], prover=s["prover"], model=model.name,
        intrinsic_N0=n0, continuous_best_in_range=continuous,
        integer_best_in_range=integer_best, predicted_best_tested_N=int(n[pred_i]),
        observed_best_tested_N=int(n[obs_i]),
        predicted_choice_at_upper_boundary=bool(pred_i == len(n) - 1),
        observed_choice_at_upper_boundary=bool(obs_i == len(n) - 1),
        choice_ratio=float(n[pred_i] / n[obs_i]),
        observed_throughput_at_predicted_choice=float(observed[pred_i]),
        observed_best_throughput=float(observed[obs_i]),
        throughput_regret_pct=float(100 * (1 - retained)),
        throughput_retained_pct=100 * retained,
        tolerance_pct=100 * tolerance,
        recommendation_within_tolerance=bool(acceptable[pred_i]),
        observed_near_optimal_sizes=" ".join(str(int(size)) for size in n[acceptable]),
    )


def evaluate(s, models, train_max, tolerance=.05):
    points, scores, optima = [], [], []
    n, actual = s["n"], s["throughput"]
    training = train_mask(s, train_max)
    lower, upper = min(n[training]), max(n[training])
    inside = (n >= lower) & (n <= upper)
    # Same finite-difference reference as Exp 1, computed only for evaluation.
    empirical_gamma = log_slope(n, actual)
    for name, model in models.items():
        predicted, gamma = model.throughput(n), model.elasticity(n)
        fd_gamma = log_slope(n, predicted)
        for i, size in enumerate(n):
            points.append(dict(phase=s["phase"], prover=s["prover"], model=name,
                               n=int(size), split="train" if training[i] else "validation",
                               validation_kind="" if training[i] else "interpolation" if inside[i] else "extrapolation",
                               time_observed_s=float(s["t"][i]), time_predicted_s=float(model.time(size)),
                               throughput_observed=float(actual[i]), throughput_predicted=float(predicted[i]),
                               throughput_error_pct=float(100 * (predicted[i] / actual[i] - 1)),
                               gamma_observed_fd=float(empirical_gamma[i]), gamma_predicted_analytic=float(gamma[i]),
                               gamma_predicted_fd=float(fd_gamma[i]),
                               gamma_error=float(gamma[i] - empirical_gamma[i]),
                               gamma_error_same_estimator=float(fd_gamma[i] - empirical_gamma[i])))
        for split, mask in (("train", training), ("validation", ~training),
                            ("interpolation", ~training & inside), ("extrapolation", ~training & ~inside)):
            if not mask.any():
                continue
            err, err_fd = gamma[mask] - empirical_gamma[mask], fd_gamma[mask] - empirical_gamma[mask]
            relative_error = np.abs(predicted[mask] / actual[mask] - 1)
            scores.append(dict(phase=s["phase"], prover=s["prover"], model=name, split=split,
                               n_sizes=int(mask.sum()),
                               throughput_MAPE_pct=float(100 * np.mean(np.abs(predicted[mask] / actual[mask] - 1))),
                               throughput_max_error_pct=float(100 * np.max(relative_error)),
                               tolerance_pct=100 * tolerance,
                               throughput_within_tolerance_pct=float(100 * np.mean(relative_error <= tolerance)),
                               throughput_log_RMSE=float(np.sqrt(np.mean(np.log(predicted[mask] / actual[mask]) ** 2))),
                               gamma_MAE=float(np.mean(np.abs(err))), gamma_RMSE=float(np.sqrt(np.mean(err ** 2))),
                               gamma_same_estimator_MAE=float(np.mean(np.abs(err_fd)))))
        optima.append(optimum_comparison(s, model, tolerance))
    return points, scores, optima


def evaluate_intervals(s, models, train_max):
    """Compare secant elasticities on identical adjacent size intervals.

    Mixed intervals use one calibration endpoint: they are NOT fully held out.
    All measured values here are evaluation references, never fitting inputs.
    """
    n, actual = s["n"], s["throughput"]
    training = train_mask(s, train_max)
    lower, upper = min(n[training]), max(n[training])
    inside = (n >= lower) & (n <= upper)
    dlog_n = np.diff(np.log(n))
    observed_log_gain = np.diff(np.log(actual))
    rows, scores = [], []
    for name, model in models.items():
        predicted_log_gain = np.diff(np.log(model.throughput(n)))
        model_rows = []
        for i in range(len(n) - 1):
            count = int(training[i]) + int(training[i + 1])
            split = {0: "heldout", 1: "mixed", 2: "train"}[count]
            log_error = float(predicted_log_gain[i] - observed_log_gain[i])
            model_rows.append(dict(
                phase=s["phase"], prover=s["prover"], model=name,
                n_left=int(n[i]), n_right=int(n[i + 1]), split=split,
                left_is_calibration=bool(training[i]), right_is_calibration=bool(training[i + 1]),
                validation_kind="" if split == "train" else
                    "interpolation" if inside[i] and inside[i + 1] else "extrapolation",
                gain_observed=float(np.exp(observed_log_gain[i])),
                gain_predicted=float(np.exp(predicted_log_gain[i])),
                gain_error_pct=float(100 * np.expm1(log_error)),
                gain_log_error=log_error,
                gamma_observed_interval=float(observed_log_gain[i] / dlog_n[i]),
                gamma_predicted_interval=float(predicted_log_gain[i] / dlog_n[i]),
                gamma_interval_error=float(log_error / dlog_n[i]),
            ))
        rows.extend(model_rows)
        for split in ("train", "mixed", "heldout"):
            for kind in ("all", "interpolation", "extrapolation"):
                selected = [r for r in model_rows if r["split"] == split and
                            (kind == "all" or r["validation_kind"] == kind)]
                if not selected:
                    continue
                scores.append(dict(
                    phase=s["phase"], prover=s["prover"], model=name,
                    split=split, validation_kind=kind, n_intervals=len(selected),
                    gain_MAPE_pct=float(np.mean([abs(r["gain_error_pct"]) for r in selected])),
                    gain_log_RMSE=float(np.sqrt(np.mean([r["gain_log_error"] ** 2 for r in selected]))),
                    gamma_interval_MAE=float(np.mean([abs(r["gamma_interval_error"]) for r in selected])),
                ))
    return rows, scores


def plotted_models(models):
    """Merge only numerically equivalent affine fits; retain both CSV models."""
    affine, power = models.get("affine"), models.get("superlinear")
    merged = (affine is not None and power is not None and affine.a == power.a == 0
              and np.allclose([affine.t0, affine.b], [power.t0, power.b], rtol=1e-6, atol=0))
    for name, model in models.items():
        if name == "superlinear" and merged:
            continue
        label = ("Affine (power term = 0)" if merged else "Affine") if name == "affine" else \
            f"Affine + power (p={model.power:g})"
        yield name, model, label


def figures(series, fitted, args):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 8, "axes.grid": True, "grid.alpha": .22})
    for kind in ("throughput", "elasticity"):
        fig, axes = plt.subplots(2, 2, figsize=(8.5, 6.63), layout="constrained")
        for i, (s, models) in enumerate(zip(series, fitted)):
            ax = axes.flat[i]
            n, actual = s["n"], s["throughput"]
            mask = train_mask(s, args.train_max)
            dense = np.geomspace(n[0], n[-1], 400)
            ax.set_title(f"({chr(97+i)}) " + (f"{s['prover']}/{s['phase']}".strip("/")), loc="left")
            y = actual if kind == "throughput" else log_slope(n, actual)
            ax.plot(n, y, color=".55", linewidth=.8)
            ax.scatter(n[mask], y[mask], color=".2", s=18, label="Calibration data", zorder=3)
            ax.scatter(n[~mask], y[~mask], facecolors="white", edgecolors=".2", s=25,
                       label="Held-out data", zorder=3)
            for name, model, label in plotted_models(models):
                y = model.throughput(dense) if kind == "throughput" else model.elasticity(dense)
                ax.plot(dense, y, color=COLORS[name], linestyle="-" if name == "affine" else "--",
                        label=label)
            ax.axvline(max(n[mask]), color=".4", linestyle=":", linewidth=1)
            ax.set_xscale("log", base=2)
            ax.set_xlabel(r"Batch size $N$ [tx]")
            if kind == "throughput":
                ax.set_yscale("log")
                ax.set_ylabel(r"$\Lambda_p=N/T_p$ [tx/s]")
            elif kind == "elasticity":
                ax.set_ylabel(r"$\gamma_{\Lambda,p}=d\ln\Lambda_p/d\ln N$")
                lo, hi = ax.get_ylim()
                lo, hi = min(lo, -.1), max(hi, .1)
                ax.set_ylim(lo, hi)
                ax.axhspan(0, hi, color="#009E73", alpha=.08)
                ax.axhspan(lo, 0, color="#E69F00", alpha=.1)
                ax.axhline(0, color=".4", linestyle=":")
        for ax in list(axes.flat)[len(series):]:
            ax.set_visible(False)
        legend_entries = {}
        for ax in axes.flat:
            if ax.get_visible():
                handles, labels = ax.get_legend_handles_labels()
                for handle, label in zip(handles, labels):
                    legend_entries.setdefault(label, handle)
        fig.legend(list(legend_entries.values()), list(legend_entries), loc="outside lower center", ncol=2)
        title = {"throughput": "Throughput: calibration and extrapolation",
                 "elasticity": "Throughput elasticity: empirical differences and analytic prediction"}[kind]
        fig.suptitle(title + f"\nDesign: {args.design}; filled points: calibration; open points: validation", fontsize=9)
        number = {"throughput": "01", "elasticity": "02"}[kind]
        for ext in args.fig_format:
            fig.savefig(args.outdir / f"{number}_{kind}.{ext}", dpi=args.fig_dpi, bbox_inches="tight")
        plt.close(fig)
    interval_figures(series, fitted, args)
    # Retire the superseded export only after its replacement was saved successfully.
    for ext in ("png", "pdf", "svg"):
        (args.outdir / f"03_errors.{ext}").unlink(missing_ok=True)


def interval_figures(series, fitted, args):
    """Plot the CSV interval estimates and their signed residuals in a 2x2 layout."""
    import matplotlib.pyplot as plt

    # Two series per page preserves the panel ratio, including opt-in verification.
    for start in range(0, len(series), 2):
        fig, axes = plt.subplots(2, 2, figsize=(8.5, 6.63), layout="constrained",
                                 sharex="col", sharey="row")
        for col, (s, models) in enumerate(zip(series[start:start + 2], fitted[start:start + 2])):
            top, bottom = axes[:, col]
            rows, _ = evaluate_intervals(s, models, args.train_max)
            reference = [r for r in rows if r["model"] == next(iter(models))]
            left = np.array([r["n_left"] for r in reference], dtype=float)
            right = np.array([r["n_right"] for r in reference], dtype=float)
            middle = np.sqrt(left * right)
            observed = np.array([r["gamma_observed_interval"] for r in reference])
            roles = np.array([r["split"] for r in reference])
            label = f"{s['prover']}/{s['phase']}".strip("/")
            top.set_title(f"({chr(97 + col)}) {label}: interval elasticity", loc="left")
            bottom.set_title(f"({chr(99 + col)}) {label}: prediction error", loc="left")
            top.plot(middle, observed, color=".5", linewidth=1, label="Observed interval elasticity")
            for role, marker, face, legend in (
                ("train", "o", ".2", "Both endpoints in calibration"),
                ("mixed", "^", "white", "One held-out endpoint"),
                ("heldout", "s", "white", "Both endpoints held out"),
            ):
                chosen = roles == role
                if not chosen.any():
                    continue
                top.scatter(middle[chosen], observed[chosen], marker=marker,
                            facecolors=face, edgecolors=".2", s=25, zorder=4, label=legend)
            for name, _, legend in plotted_models(models):
                selected = [r for r in rows if r["model"] == name]
                predicted = np.array([r["gamma_predicted_interval"] for r in selected])
                error = np.array([r["gamma_interval_error"] for r in selected])
                style = "-" if name == "affine" else "--"
                top.plot(middle, predicted, color=COLORS[name], linestyle=style,
                         linewidth=1.5, label=legend)
                bottom.plot(middle, error, color=COLORS[name], linestyle=style, linewidth=1.5)
                for role, marker in (("train", "o"), ("mixed", "^"), ("heldout", "s")):
                    chosen = roles == role
                    bottom.scatter(middle[chosen], error[chosen], marker=marker, s=20,
                                   edgecolors=COLORS[name],
                                   facecolors=COLORS[name] if role == "train" else "white", zorder=3)
            for ax in (top, bottom):
                ax.axvline(max(s["n"][train_mask(s, args.train_max)]), color=".4", linestyle=":", linewidth=1)
                ax.axhline(0, color=".4", linestyle=":", linewidth=.8)
                ax.set_xscale("log", base=2)
            bottom.set_xlabel(r"Interval midpoint $\sqrt{N_iN_j}$ [tx]")
        if len(series[start:start + 2]) == 1:
            for ax in axes[:, 1]:
                ax.set_visible(False)
        axes[0, 0].set_ylabel(r"$\bar\gamma_{\Lambda,p}=\ln(\Lambda_j/\Lambda_i)/\ln(N_j/N_i)$")
        axes[1, 0].set_ylabel(r"$\bar\gamma_{\mathrm{predicted}}-\bar\gamma_{\mathrm{observed}}$")
        lo, hi = axes[0, 0].get_ylim()
        lo, hi = min(lo, -.1), max(hi, .1)
        axes[0, 0].set_ylim(lo, hi)
        for ax in axes[0]:
            if ax.get_visible():
                ax.axhspan(0, hi, color="#009E73", alpha=.08)
                ax.axhspan(lo, 0, color="#E69F00", alpha=.1)
        legend_entries = {}
        for ax in axes[0]:
            if ax.get_visible():
                handles, labels = ax.get_legend_handles_labels()
                for handle, label in zip(handles, labels):
                    legend_entries.setdefault(label, handle)
        fig.legend(list(legend_entries.values()), list(legend_entries),
                   loc="outside lower center", ncol=2, fontsize=7)
        fig.suptitle("Throughput elasticity: comparison on identical intervals\n"
                     f"Design: {args.design}; interval estimates connected at geometric midpoints", fontsize=9)
        suffix = "" if start == 0 else f"_{start // 2 + 1:02d}"
        for ext in args.fig_format:
            fig.savefig(args.outdir / f"03_interval_elasticity{suffix}.{ext}",
                        dpi=args.fig_dpi, bbox_inches="tight")
        plt.close(fig)


def combined_figures(cases, args):
    """Columns are sampling designs; rows are estimates and signed errors."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    plt.rcParams.update({"font.size": 8, "axes.grid": True, "grid.alpha": .22})
    phases = list(dict.fromkeys(s["phase"] for _, result in cases for s in result["series"]))
    for phase in phases:
        for kind in ("throughput", "elasticity", "interval_elasticity"):
            fig, axes = plt.subplots(2, 2, figsize=(8.5, 8.5), layout="constrained",
                                     sharex="col", sharey="row")
            legend = {}
            model_legend = {}
            role_legend = {}
            for col, (config, result) in enumerate(cases):
                top, bottom = axes[:, col]
                top.set_title(f"({chr(97 + col)}) {config.design}: {kind.replace('_', ' ')}", loc="left")
                bottom.set_title(f"({chr(99 + col)}) {config.design}: prediction error", loc="left")
                boundaries = set()
                for s, models in zip(result["series"], result["fitted"]):
                    if s["phase"] != phase:
                        continue
                    n = s["n"]
                    training = train_mask(s, config.train_max)
                    boundaries.add(float(max(n[training])))
                    prover = s["prover"] or phase
                    color = PROVER_COLORS.get(s["prover"], "#555555")
                    legend[prover] = Line2D([], [], color=color, linewidth=2)
                    visible_models = list(plotted_models(models))
                    if kind == "interval_elasticity":
                        rows = result["intervals"]
                        reference = [r for r in rows if r["phase"] == phase and r["prover"] == s["prover"]
                                     and r["model"] == visible_models[0][0]]
                        x = np.sqrt(np.array([r["n_left"] for r in reference], dtype=float) *
                                    np.array([r["n_right"] for r in reference], dtype=float))
                        observed = np.array([r["gamma_observed_interval"] for r in reference])
                        roles = np.array([r["split"] for r in reference])
                        markers = (("train", "o", True, "Both endpoints in calibration"),
                                   ("mixed", "^", False, "One held-out endpoint"),
                                   ("heldout", "s", False, "Both endpoints held out"))
                    else:
                        x = n
                        observed = s["throughput"] if kind == "throughput" else log_slope(n, s["throughput"])
                        roles = np.where(training, "train", "heldout")
                        markers = (("train", "o", True, "Calibration size"),
                                   ("heldout", "o", False, "Held-out size"))
                    top.plot(x, observed, color=color, linestyle=":", linewidth=1.1)
                    for role, marker, filled, label in markers:
                        chosen = roles == role
                        if not chosen.any():
                            continue
                        top.scatter(x[chosen], observed[chosen], marker=marker, s=24,
                                    facecolors=color if filled else "white", edgecolors=color, zorder=4)
                        role_legend[label] = Line2D([], [], linestyle="none", marker=marker,
                                                    markerfacecolor=".3" if filled else "white", color=".3")
                    for name, model, model_label in visible_models:
                        style = "-" if name == "affine" else "--"
                        model_legend[model_label] = Line2D([], [], color=".3", linestyle=style)
                        if kind == "interval_elasticity":
                            selected = [r for r in rows if r["phase"] == phase and
                                        r["prover"] == s["prover"] and r["model"] == name]
                            predicted = np.array([r["gamma_predicted_interval"] for r in selected])
                            error = np.array([r["gamma_interval_error"] for r in selected])
                            model_x = x
                        else:
                            model_x = np.geomspace(n[0], n[-1], 400)
                            if kind == "throughput":
                                predicted = model.throughput(model_x)
                                error = 100 * (model.throughput(n) / observed - 1)
                            else:
                                predicted = model.elasticity(model_x)
                                error = model.elasticity(n) - observed
                        top.plot(model_x, predicted, color=color, linestyle=style, linewidth=1.6)
                        bottom.plot(x, error, color=color, linestyle=style, linewidth=1.4)
                        for role, marker, filled, _ in markers:
                            chosen = roles == role
                            bottom.scatter(x[chosen], error[chosen], marker=marker, s=20,
                                           facecolors=color if filled else "white", edgecolors=color, zorder=3)
                for ax in (top, bottom):
                    ax.set_xscale("log", base=2)
                    for boundary in sorted(boundaries):
                        ax.axvline(boundary, color=".4", linestyle="-.", linewidth=.8)
                bottom.axhline(0, color=".4", linestyle=":", linewidth=.8)
                bottom.set_xlabel(r"Interval midpoint $\sqrt{N_iN_j}$ [tx]" if kind == "interval_elasticity"
                                  else r"Batch size $N$ [tx]")
            if kind == "throughput":
                axes[0, 0].set_yscale("log")
                axes[0, 0].set_ylabel(r"$\Lambda_p=N/T_p$ [tx/s]")
                axes[1, 0].set_ylabel("Throughput error: 100 (predicted / observed - 1) [%]")
            else:
                axes[0, 0].set_ylabel(r"$\bar\gamma_{\Lambda,p}=\Delta\ln\Lambda_p/\Delta\ln N$"
                                      if kind == "interval_elasticity" else r"$\gamma_{\Lambda,p}$")
                axes[1, 0].set_ylabel("Elasticity error: predicted - observed")
                lo, hi = axes[0, 0].get_ylim()
                lo, hi = min(lo, -.1), max(hi, .1)
                axes[0, 0].set_ylim(lo, hi)
                for ax in axes[0]:
                    ax.axhspan(0, hi, color="#009E73", alpha=.08)
                    ax.axhspan(lo, 0, color="#E69F00", alpha=.1)
                    ax.axhline(0, color=".4", linestyle=":", linewidth=.8)
            legend["Observed"] = Line2D([], [], color=".3", linestyle=":")
            legend.update(model_legend)
            legend.update(role_legend)
            legend["Largest calibration size"] = Line2D([], [], color=".4", linestyle="-.")
            fig.legend(list(legend.values()), list(legend), loc="outside lower center", ncol=3, fontsize=7)
            title = {"throughput": "Throughput: observed and predicted",
                     "elasticity": "Throughput elasticity: empirical differences vs analytic prediction",
                     "interval_elasticity": "Throughput elasticity: comparison on identical intervals"}[kind]
            fig.suptitle(f"{title}\nPhase: {phase}; logspace and large calibration designs", fontsize=9)
            number = {"throughput": "01", "elasticity": "02", "interval_elasticity": "03"}[kind]
            suffix = "" if phase == "prove" else f"_{phase}"
            for ext in args.fig_format:
                fig.savefig(args.outdir / f"{number}_{kind}{suffix}.{ext}", dpi=args.fig_dpi)
            plt.close(fig)


def combine_designs(args):
    """Keep numeric exports per design and place their combined figures in the parent."""
    cases = []
    for design in ("logspace", "large"):
        config = argparse.Namespace(**vars(args))
        config.design, config.calibration_sizes = design, None
        config.outdir = args.outdir / design
        config.no_figures = True
        cases.append((config, run(config)))
    if not args.no_figures:
        combined_figures(cases, args)
    lines = ["# Exp 2 — comparaison logspace / large", "",
             "```powershell",
             f'python scripts/analysis/exp2.py --combine-designs --train-max {args.train_max} '
             f'--calibration-count {args.calibration_count} --power {args.power:g} '
             f'--tolerance {args.tolerance:g} --phases {" ".join(args.phases)} '
             f'--from "{args.run_dir}" --outdir "{args.outdir}"', "```", "",
             "Chaque figure carrée contient quatre panneaux (2 × 2) : logspace à gauche, large à droite ; "
             "valeurs observées et prédites en haut, erreurs signées en bas. Les axes partagent "
             "les mêmes échelles par ligne. Couleurs reprises d'Exp 1 : bleu (#0072B2) pour "
             "rapidsnark ; rose (#CC79A7) pour snarkjs.", "",
             "- `01_throughput` : débit et erreur relative en pourcentage.",
             "- `02_elasticity` : dérivée analytique prédite face aux différences finies observées ; "
             "en bas, leur différence sans dimension. Cette comparaison utilise deux estimateurs différents.",
             "- `03_interval_elasticity` : élasticités sur les mêmes intervalles, puis différence "
             "prédite moins observée. Les points sont reliés à leur milieu géométrique, sans lissage.", "",
             "Courbes pointillées : mesures ; trait plein : modèle affine ; tirets : modèle avec puissance "
             "s'il est distinct. Quand le modèle avec puissance se réduit au même affine, une seule "
             "courbe est affichée par prover, avec la légende « Affine (power term = 0) ».", "",
             "Pour 01 et 02, rond plein : taille en calibration ; rond vide : taille exclue. "
             "Pour 03, rond plein : deux extrémités en calibration ; triangle vide : une extrémité "
             "exclue ; carré vide : deux extrémités exclues. Seuls les marqueurs présents sont "
             "affichés dans la légende. Le trait vertical mixte indique la plus grande taille "
             "de calibration. Vert/orange en haut : hausse/baisse du débit. En bas, zéro est "
             "l'erreur idéale ; les erreurs d'élasticité ne sont pas des pourcentages.", "",
             "Les données, paramètres et scores restent dans [logspace](logspace/README.md) et "
             "[large](large/README.md). Les ajustements restent indépendants pour chaque plan "
             "et chaque prover. Aucun point exclu ne participe à l'ajustement. Les scores moyens "
             "des deux plans ne portent pas forcément sur les mêmes tailles de validation.", ""]
    (args.outdir / "README.md").write_text("\n".join(lines), encoding="utf-8")
    if not args.no_figures:
        for config, _ in cases:
            for stem in ("01_throughput", "02_elasticity", "03_interval_elasticity", "03_interval_elasticity_02", "03_errors"):
                for ext in ("png", "pdf", "svg"):
                    (config.outdir / f"{stem}.{ext}").unlink(missing_ok=True)


def report(args, scores, optima, series, fitted, interval_scores):
    lines = ["# Exp 2 — extrapolation du débit", "",
             f"Source : `{args.run_dir.resolve()}`. Plan de calibration : {args.design}, plafond N={args.train_max}. "
             f"Validation : toutes les tailles non sélectionnées. Exposant fixé a priori : p={args.power:g}.", "",
             "```powershell", f"python scripts/analysis/exp2.py --train-max {args.train_max} --design {args.design} --calibration-count {args.calibration_count} --power {args.power:g} --phases {' '.join(args.phases)}" +
             (" --calibration-sizes " + " ".join(map(str, args.calibration_sizes)) if args.calibration_sizes else "") +
             f' --tolerance {args.tolerance:g} --from "{args.run_dir}" --outdir "{args.outdir}"', "```", "",
             "Tailles retenues : " + "; ".join(f"{s['prover']}/{s['phase']} : {s['n'][train_mask(s, args.train_max)].tolist()}" for s in series), "",
             "Les tailles sont sélectionnées sans consulter leurs temps : prefix prend toutes les tailles "
             "jusqu'au plafond ; logspace répartit un budget de tailles sur l'échelle logarithmique ; "
             "large conserve la plus petite et les plus grandes tailles éligibles ; manual prend la liste fournie. "
             "Une taille exclue à l'intérieur du domaine de calibration teste l'interpolation ; "
             "hors de ce domaine, elle teste l'extrapolation. metrics.csv distingue les deux.", "",
             "Les mesures sont chargées comme dans Exp 1 : même moyenne des temps par taille, "
             "même regroupement des répétitions verify. Le débit est celui d'une phase isolée, "
             "pas celui du rollup complet. CPU et RAM ne participent pas à cet ajustement.", "",
             "## Modèles issus des formes du papier", "",
             "- Affine : T(N)=t0+bN ; Lambda=N/T ; gamma_Lambda=t0/T.",
             "- Affine avec terme superlinéaire : T(N)=t0+bN+aN^p ; "
             "gamma_Lambda=[t0-(p-1)aN^p]/T.", "",
             "Les coefficients sont non négatifs. On ajuste log(T_prédit)-log(T_mesuré), "
             "avec un poids égal par taille, uniquement sur la calibration. Cela équivaut "
             "à minimiser les écarts logarithmiques du débit. Les erreurs de mesure "
             "ne servent pas de poids. Les contributions normalisées <1e-10 sont mises à zéro.", "",
             "Ces formes reprennent W=W0+wN et W=W0+wN+aN^p du papier en les appliquant "
             "au temps de phase. Sous capacité constante, leurs coefficients seraient ceux "
             "du travail divisés par la capacité. Ici, ce sont des modèles empiriques de temps : "
             "ni W ni R_phys ne sont identifiés séparément.", "",
             "Les deux modèles sont présentés sans sélection sur les tailles de validation. "
             "Modifier p ou la coupure après avoir vu la validation constitue une exploration, "
             "pas une nouvelle validation indépendante. L'affine sert de référence sans "
             "optimum fini intrinsèque ; le terme superlinéaire permet de tester une dégradation.", "",
             "## Résultats hors calibration", "",
             f"Tolérance pratique choisie : {100 * args.tolerance:g} %. Ce seuil est configurable "
             "avec --tolerance et ne représente pas un niveau de confiance statistique.", "",
             "| Phase | Modèle | Domaine | Tailles | Erreur débit MAPE (%) | Erreur maximale (%) | Tailles dans la tolérance (%) |",
             "|---|---|---|---:|---:|---:|---:|"]
    for r in scores:
        if r["split"] in ("interpolation", "extrapolation"):
            lines.append(f"| {r['prover']}/{r['phase']} | {r['model']} | {r['split']} | {r['n_sizes']} | "
                         f"{r['throughput_MAPE_pct']:.2f} | {r['throughput_max_error_pct']:.2f} | "
                         f"{r['throughput_within_tolerance_pct']:.1f} |")
    lines += ["", "## Fidélité des gains et de l'élasticité", "",
              "La comparaison principale de l'élasticité utilise désormais les mêmes intervalles "
              "[Ni, Nj] entre tailles adjacentes pour le modèle et les mesures :", "",
              "- Facteur de gain : G = Lambda(Nj)/Lambda(Ni) ; G > 1 signifie un débit croissant.",
              "- Élasticité moyenne sur l'intervalle : gamma_bar = ln(G)/ln(Nj/Ni). "
              "C'est une pente logarithmique, pas la dérivée ponctuelle à une extrémité.",
              "- Erreur de gain (%) : 100 × (G_prédit/G_observé - 1). Le dénominateur est "
              "le facteur G, pas G-1 : la mesure reste définie sur un plateau.",
              "- Erreur d'élasticité : |gamma_bar_prédit - gamma_bar_observé|, sans dimension. "
              "On évite un pourcentage relatif à une élasticité proche de zéro.", "",
              "Ces deux erreurs décrivent le même écart : ln(G_prédit/G_observé) = "
              "(gamma_bar_prédit - gamma_bar_observé) × ln(Nj/Ni). Elles ne constituent "
              "pas deux validations indépendantes. Un biais multiplicatif constant du débit "
              "s'annule dans les gains : il faut aussi conserver les erreurs de débit ci-dessus.", "",
              "mixed : un seul point de l'intervalle est hors calibration ; heldout : les deux "
              "le sont. Un intervalle est classé en extrapolation dès qu'une extrémité sort "
              "du domaine de calibration. Les intervalles voisins partagent des mesures, "
              "donc leurs erreurs ne sont pas indépendantes.", "",
              "| Phase | Modèle | Points utilisés | Domaine | Intervalles | Erreur gain MAPE (%) | Erreur élasticité MAE |",
              "|---|---|---|---|---:|---:|---:|"]
    for r in interval_scores:
        if r["split"] != "train" and r["validation_kind"] != "all":
            lines.append(f"| {r['prover']}/{r['phase']} | {r['model']} | {r['split']} | "
                         f"{r['validation_kind']} | {r['n_intervals']} | {r['gain_MAPE_pct']:.2f} | "
                         f"{r['gamma_interval_MAE']:.4f} |")
    lines += ["", "Pour comparer logspace et large, utiliser exactement les mêmes tailles ou "
              "intervalles réservés. Les moyennes globales ne portent pas nécessairement sur "
              "les mêmes points. Par exemple, avec les plans logspace et large plafonnés à 4096 "
              "sur le dataset initial, l'intervalle [4096, 8192] permet une comparaison commune : "
              "son point gauche est utilisé en calibration, son point droit en validation."]
    collapsed = [f"{s['prover']}/{s['phase']}".strip("/") for s, models in zip(series, fitted)
                 if models["superlinear"].a == 0]
    if collapsed:
        lines += ["", "Pour " + ", ".join(collapsed) + ", le coefficient superlinéaire "
                  "ajusté est nul à la précision numérique retenue. Les deux modèles "
                  "se réduisent à une forme affine. Lorsque leurs coefficients affines coïncident "
                  "à une tolérance relative de 1e-6 (sans tolérance absolue), les figures 01, 02 et 03 "
                  "affichent une seule courbe, intitulée « Affine (power term = 0) ». Les deux "
                  "ajustements restent exportés dans les CSV et analysis.json. La calibration "
                  "n'identifie pas de terme provoquant "
                  "un retournement futur. Cela n'établit pas son absence aux grandes tailles."]
    lines += ["", "L'élasticité de référence est estimée par différences finies logarithmiques "
              "sur le dataset complet, comme dans Exp 1. Aux extrémités, la différence est "
              "unilatérale. Autour de la coupure, une pente peut utiliser des points des deux "
              "domaines, mais ces pentes ne sont jamais utilisées pour ajuster le modèle. "
              "La référence n'est pas une vérité exacte : elle dépend du bruit et de la grille.", "",
              "La figure 02 et les anciens champs gamma_MAE/gamma_RMSE conservent la comparaison "
              "de la dérivée analytique du modèle avec cette référence discrète. "
              "`gamma_same_estimator_MAE` applique aussi les différences finies aux prédictions "
              "pour comparer avec le même estimateur. Les erreurs d'élasticité sont absolues "
              "(sans dimension), car une erreur relative près de zéro serait trompeuse.", "",
              "## Taille optimale", "",
              "Si t0>0 et a>0 : N0=[t0/((p-1)a)]^(1/p). Sinon aucun optimum intérieur "
              "isolé n'est identifié par cette formule. Pour le modèle affine à t0>0, "
              "le débit est croissant. Si les termes fixe et superlinéaire sont nuls, "
              "le débit est constant et l'optimum n'est pas unique.", "",
              "La comparaison porte sur le même ensemble discret de tailles testées, "
              "de 1 au maximum du dataset. Le maximum observé est choisi après calibration "
              "uniquement pour évaluer la prédiction. Le regret est la perte de débit "
              "observée au choix prédit, relativement au meilleur débit observé. "
              "Les optima continus et entiers intermédiaires sont des prédictions non "
              "validées par des circuits de ces tailles. Le domaine testé n'est pas N_max "
              "physique ou protocolaire ; aucune contrainte RAM, release ou gas n'est imposée.", "",
              "Le critère principal pour la recommandation est le regret : "
              "100 × [1 - Lambda_observée(N_recommandé)/max_N Lambda_observée(N)]. "
              "La recommandation satisfait la tolérance si ce regret ne la dépasse pas. "
              "Toutes les tailles conservant au moins (1-tolérance) du meilleur débit observé "
              "sont exportées dans observed_near_optimal_sizes. Cette évaluation est rétrospective "
              "sur toute la grille, qui inclut les tailles de calibration ; elle ne remplace pas "
              "la validation sur des points exclus ou sur un nouveau run.", "",
              "| Phase | Modèle | N prédit (grille) | N observé (grille) | Regret (%) | Dans la tolérance |",
              "|---|---|---:|---:|---:|---|"]
    for r in optima:
        lines.append(f"| {r['prover']}/{r['phase']} | {r['model']} | {r['predicted_best_tested_N']} | "
                     f"{r['observed_best_tested_N']} | {r['throughput_regret_pct']:.2f} | "
                     f"{'oui' if r['recommendation_within_tolerance'] else 'non'} |")
    lines += ["", "Un maximum à la borne supérieure ne démontre pas l'existence d'un optimum "
              "fini. Un N0 extrapolé loin de la calibration est particulièrement fragile : "
              "un très petit coefficient superlinéaire peut produire un optimum très lointain. "
              "Un bon ajustement local ne garantit pas une bonne extrapolation.", "",
              "## Fichiers et portée", "",
              "- `01_throughput`, `02_elasticity` : figures PNG/PDF en grille 2 × 2.",
              "- `03_interval_elasticity` remplace `03_errors` : en haut, élasticités observées et "
              "prédites sur les mêmes intervalles ; en bas, erreur signée (prédite moins observée). "
              "Chaque colonne correspond à un prover. Les valeurs sont placées au milieu géométrique "
              "de chaque intervalle et reliées par des lignes pour faciliter la lecture, sans lissage "
              "ni modification des estimations. Elles ne représentent pas une dérivée ponctuelle. "
              "Rond plein : deux extrémités en calibration ; triangle vide : "
              "une extrémité hors calibration ; carré vide : les deux hors calibration. "
              "Le trait vertical indique la plus grande taille de calibration. Le fond vert/orange "
              "en haut indique un gain/une perte de débit ; en bas, la proximité de zéro indique "
              "une prédiction fidèle. La grille 2 × 2 conserve le ratio des panneaux ; une page "
              "supplémentaire est exportée si la vérification est activée.",
              "- `predictions.csv` : valeurs mesurées/prédites et erreurs par taille.",
              "- `metrics.csv` : scores distincts calibration/validation/interpolation/extrapolation.",
              "- `interval_predictions.csv` : gains et élasticités comparés sur les mêmes intervalles.",
              "- `interval_metrics.csv` : erreurs par rôle des extrémités et domaine de validation.",
              "- `calibration_plan.csv` : tailles à mesurer et coût historique indicatif de collecte.",
              "- `optima.csv` : optima et regret sur les tailles testées.",
              "- `analysis.json` : paramètres ajustés, unités, configuration et empreintes des sources.", "",
              "Cette première expérience quantifie une précision prédictive sur un run. "
              "Elle ne prouve pas la véracité universelle du modèle. Aucun intervalle de "
              "confiance n'est estimé ici. Pour tester la robustesse, varier la coupure "
              "(--train-max), conserver les sorties dans des dossiers distincts et, ensuite, "
              "valider sur des runs indépendants. Le bruit sur les élasticités et les différences "
              "faibles entre meilleurs débits demandent une analyse des répétitions. "
              "Avec une seule taille de validation, le score est une erreur ponctuelle, "
              "pas une estimation de précision sur une plage. Pour comparer des coupures, "
              "comparer aussi les erreurs aux mêmes tailles réservées à la validation.", ""]
    if getattr(args, "combine_designs", False):
        start = lines.index("## Fichiers et portée") + 2
        end = next(i for i in range(start, len(lines)) if lines[i].startswith("- `predictions.csv`"))
        lines[start:end] = ["Les figures 01, 02 et 03 sont regroupées dans le dossier parent : "
                            "[figures comparatives](../README.md). Colonnes : logspace/large ; "
                            "lignes : valeurs/erreurs ; couleurs : rapidsnark/snarkjs.", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="run_dir", type=Path, default=ROOT / "bench-out/20260825_120620")
    parser.add_argument("--outdir", type=Path, default=ROOT / "bench-out/exp2")
    parser.add_argument("--train-max", type=int, default=1024)
    parser.add_argument("--design", choices=["prefix", "logspace", "large", "manual"], default="prefix")
    parser.add_argument("--calibration-count", type=int, default=6)
    parser.add_argument("--calibration-sizes", nargs="+", type=int)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--compare-designs", action="store_true", help="Compare prefix and sparse designs; reserve largest size")
    modes.add_argument("--combine-designs", action="store_true", help="Combine logspace/large and both provers in 2x2 figures")
    parser.add_argument("--power", type=float, default=2., help="Superlinear exponent fixed BEFORE validation")
    parser.add_argument("--tolerance", type=float, default=.05,
                        help="Relative throughput error / optimum loss tolerated (default: 0.05 = 5%%)")
    parser.add_argument("--phases", nargs="+", choices=["prove", "verify"], default=["prove"])
    parser.add_argument("--fig-format", nargs="+", choices=["png", "pdf", "svg"], default=["png", "pdf"])
    parser.add_argument("--fig-dpi", type=int, default=300)
    parser.add_argument("--no-figures", action="store_true")
    args = parser.parse_args(argv)
    if args.train_max < 1 or not np.isfinite(args.power) or not 1 < args.power <= 8 or args.fig_dpi < 1:
        parser.error("Require train-max >= 1, 1 < power <= 8, fig-dpi >= 1")
    if args.design != "manual" and args.calibration_sizes:
        parser.error("--calibration-sizes requires --design manual")
    if not np.isfinite(args.tolerance) or not 0 <= args.tolerance < 1:
        parser.error("Require 0 <= tolerance < 1")
    try:
        if args.combine_designs:
            combine_designs(args)
        elif args.compare_designs:
            compare_designs(args)
        else:
            run(args)
    except ValueError as exc:
        parser.error(str(exc))
    return 0


def run(args):
    series = load_series(args.run_dir, args.phases)
    for s in series:
        s["calibration_mask"] = calibration_mask(s["n"], args.train_max, args.design,
                                                 args.calibration_count, args.calibration_sizes)
    fitted = [calibrate(s, args.train_max, args.power) for s in series]
    points, scores, optima = [], [], []
    intervals, interval_scores = [], []
    for s, models in zip(series, fitted):
        p, m, o = evaluate(s, models, args.train_max, args.tolerance)
        edges, edge_scores = evaluate_intervals(s, models, args.train_max)
        intervals.extend(edges)
        interval_scores.extend(edge_scores)
        points.extend(p)
        scores.extend(m)
        optima.extend(o)
    args.outdir.mkdir(parents=True, exist_ok=True)
    plan = [dict(phase=s["phase"], prover=s["prover"], n=int(s["n"][i]),
                 historical_n_reps=int(s["n_reps"][i]),
                 historical_execution_s=float(s["t"][i] * s["n_reps"][i]))
            for s in series for i in np.flatnonzero(s["calibration_mask"])]
    write_csv(args.outdir / "calibration_plan.csv", plan)
    for name, rows in (("predictions", points), ("metrics", scores), ("optima", optima),
                       ("interval_predictions", intervals), ("interval_metrics", interval_scores)):
        write_csv(args.outdir / f"{name}.csv", rows)
    metadata = dict(source=str(args.run_dir.resolve()), train_max=args.train_max, power=args.power,
                    design=args.design, calibration_count=args.calibration_count,
                    tolerance=args.tolerance,
                    primary_elasticity_comparison="log throughput gains on identical adjacent size intervals",
                    selection="size coordinates only; no measured values used",
                    phases=args.phases, objective="unweighted least squares in log time",
                    coefficient_units={"t0": "s", "b": "s/tx", "a": "s/tx^p"},
                    model_selection="none; both prespecified models reported",
                    source_sha256={name: hashlib.sha256((args.run_dir / name).read_bytes()).hexdigest()
                                   for name in ("phases.csv", "steps.csv", "manifest.json")},
                    fits=[dict(phase=s["phase"], prover=s["prover"],
                               train_sizes=s["n"][s["calibration_mask"]].tolist(),
                               validation_sizes=s["n"][~s["calibration_mask"]].tolist(),
                               models={name: asdict(model) for name, model in models.items()})
                          for s, models in zip(series, fitted)])
    (args.outdir / "analysis.json").write_text(json.dumps(metadata, indent=2, allow_nan=False), encoding="utf-8")
    (args.outdir / "README.md").write_text(report(args, scores, optima, series, fitted, interval_scores), encoding="utf-8")
    if not args.no_figures:
        figures(series, fitted, args)
    print(f"Exp 2: {len(series)} series, design={args.design}, cap={args.train_max} -> {args.outdir}")
    for row in scores:
        if row["split"] == "validation":
            print(f"{row['prover']}/{row['phase']} {row['model']}: "
                  f"MAPE={row['throughput_MAPE_pct']:.2f}%, "
                  f"within {100 * args.tolerance:g}% tolerance={row['throughput_within_tolerance_pct']:.1f}% of sizes")
    for row in optima:
        print(f"{row['prover']}/{row['phase']} {row['model']}: "
              f"recommended N={row['predicted_best_tested_N']}, regret={row['throughput_regret_pct']:.2f}%, "
              f"within tolerance={row['recommendation_within_tolerance']}")
    return dict(series=series, fitted=fitted, scores=scores, points=points, optima=optima, plan=plan,
                intervals=intervals, interval_scores=interval_scores)


def compare_designs(args):
    """A fixed, descriptive comparison; never select a winner automatically."""
    series = load_series(args.run_dir, args.phases)
    common = sorted(set.intersection(*(set(s["n"].tolist()) for s in series)))
    if len(common) < 5:
        raise ValueError("Design comparison needs at least five common sizes")
    target = common[-1]
    upper = common[-2]
    prefix_cap = min(args.train_max, upper)
    cases = [("prefix_local", "prefix", prefix_cap), ("prefix_wide", "prefix", upper),
             ("logspace", "logspace", upper), ("large", "large", upper)]
    rows = []
    for label, design, cap in cases:
        config = argparse.Namespace(**vars(args))
        config.design, config.train_max = design, cap
        config.calibration_sizes = None
        config.outdir = args.outdir / label
        result = run(config)
        for p in result["points"]:
            if p["n"] != target:
                continue
            s = next(s for s in result["series"] if (s["phase"], s["prover"]) == (p["phase"], p["prover"]))
            mask = s["calibration_mask"]
            o = next(o for o in result["optima"] if (o["phase"], o["prover"], o["model"]) == (p["phase"], p["prover"], p["model"]))
            rows.append(dict(design=label, prover=p["prover"], phase=p["phase"], model=p["model"],
                             calibration_sizes=" ".join(map(str, s["n"][mask])), n_calibration=int(mask.sum()),
                             historical_execution_s=float(np.sum(s["t"][mask] * s["n_reps"][mask])),
                             common_test_N=target, throughput_error_pct=abs(p["throughput_error_pct"]),
                             gamma_error=abs(p["gamma_error"]), predicted_best_N=o["predicted_best_tested_N"],
                             regret_pct=o["throughput_regret_pct"]))
    write_csv(args.outdir / "design_comparison.csv", rows)
    lines = ["# Plans de calibration réduits", "", f"Comparaison commune sur N={target}, toujours exclu de la calibration. "
             "Les tailles sélectionnées ne dépendent pas des temps mesurés. Cette étude est exploratoire sur un run déjà examiné.", "",
             "| Plan | Prover | Modèle | Nb tailles | Erreur débit (%) | Erreur élasticité | N prédit | Regret (%) |",
             "|---|---|---|---:|---:|---:|---:|---:|"]
    for r in rows:
        lines.append(f"| {r['design']} | {r['prover']} | {r['model']} | {r['n_calibration']} | "
                     f"{r['throughput_error_pct']:.2f} | {r['gamma_error']:.4f} | {r['predicted_best_N']} | {r['regret_pct']:.2f} |")
    lines += ["", "## Plans exacts", ""]
    for label, _, _ in cases:
        example = next(r for r in rows if r["design"] == label)
        lines.append(f"- {label} : {example['calibration_sizes']} ({example['n_calibration']} tailles).")
    lines += ["", "Exemple de reproduction de l'analyse réduite :", "", "```powershell",
              f"python scripts/analysis/exp2.py --design logspace --calibration-count {args.calibration_count} --train-max {upper} --outdir bench-out/exp2/logspace",
              "```", "", "Le collecteur accepte déjà --sizes : utiliser les tailles de calibration_plan.csv "
              "pour un futur benchmark réduit. La taille de test finale doit rester séparée de la calibration.", "",
              "Chaque sous-dossier contient le plan exact, les figures et les erreurs séparées "
              "en interpolation et extrapolation. Le coût historique somme les durées moyennes "
              "multipliées par les répétitions : il exclut compilation, setup, pauses et vérification. "
              "Moins de tailles n'implique donc pas toujours un benchmark plus court.", "",
              "## Autres démarches utiles", "",
              "- Mesure séquentielle : ajouter la taille suivante seulement après avoir évalué la prédiction précédente ; "
              "conserver une taille finale ou un nouveau run pour une validation indépendante.",
              "- Densifier autour d'un retournement suspecté avec --design manual, sans sélectionner ces tailles "
              "sur les erreurs du test final. De nouveaux circuits peuvent être nécessaires.",
              "- Bootstrap des répétitions de calibration pour quantifier la dispersion des coefficients et optima ; "
              "cela n'évalue pas à lui seul l'erreur de forme du modèle (non implémenté ici).",
              "- Comparer des exposants fixés à l'avance avec --power dans une validation interne à la calibration ; "
              "ne pas retenir celui qui maximise le score sur le test final. Une interpolation par spline peut "
              "servir de référence entre points, mais ne garantit pas l'extrapolation (non implémentée ici).", ""]
    (args.outdir / "README.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
