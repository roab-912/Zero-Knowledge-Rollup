#!/usr/bin/env python3
"""Analyse frozen calibration and independent validation; never run a prover.

See batch_theory.md. One result.json plus PNG/PDF figures per invocation.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.calibration import batch_calibration as bench
import numpy as np

SCOPES = ("proof_s", "proof_generation_s", "total_s", "local_verified_s", "verification_s")
LABELS = {"proof_s": "Proof only", "proof_generation_s": "Witness + proof",
          "total_s": "Preparation + witness + proof",
          "local_verified_s": "Local processing including verification",
          "verification_s": "Shared snarkjs verification"}
COLORS = {"rapidsnark": "#0072B2", "snarkjs": "#CC79A7", "verification": "#009E73"}


def portable_path(value):
    """Resolve recorded Docker /work paths on the host, without rewriting records."""
    path = Path(value)
    if path.exists():
        return path.resolve()
    normalized = str(value).replace("\\", "/")
    if normalized.startswith("/work/"):
        return ROOT / normalized[len("/work/"):]
    if "/bench-out/" in normalized:
        return ROOT / "bench-out" / normalized.split("/bench-out/", 1)[1]
    return path


def load_sources(validation_path, calibration_path=None, model_path=None):
    validation = bench.read_json(validation_path)
    if validation.get("status") != "complete" or validation["manifest"]["campaign"] != "validation":
        raise ValueError("a completed independent validation campaign is required")
    model = validation["model"]
    source = portable_path(calibration_path or model["calibration_directory"])
    source = source / "result.json" if source.is_dir() else source
    calibration = bench.read_json(source)
    frozen_path = portable_path(model_path or calibration["frozen"]["model_path"])
    frozen_file = bench.read_json(frozen_path)
    if frozen_file["model"] != model:
        raise ValueError("validation's embedded model differs from the frozen model")
    sha = bench.digest(frozen_path)
    if sha != calibration["frozen"]["model_sha256"] or sha != validation["manifest"]["model_sha256"]:
        raise ValueError("frozen model hash mismatch")
    if bench.calibration_digest(source.parent) != model["calibration_data_sha256"]:
        raise ValueError("calibration measurements changed after freezing")
    if calibration.get("status") != "complete":
        raise ValueError("calibration is incomplete")
    cm, vm = calibration["manifest"], validation["manifest"]
    if cm["campaign_id"] == vm["campaign_id"] or cm["config"]["seed"] == vm["config"]["seed"]:
        raise ValueError("calibration and validation must be separate campaigns with different seeds")
    if (bench.machine_signature(cm["environment"]) != bench.machine_signature(vm["environment"])
            or cm["artifacts"] != vm["artifacts"]):
        raise ValueError("calibration and validation configurations/artifacts differ")
    for path, document in ((source, calibration), (validation_path, validation)):
        records = bench.records(Path(path).parent, document["manifest"])
        if len(records) != len(document["manifest"]["plan"]):
            raise ValueError("campaign plan is incomplete")
        bench.check_pairs(records)
    provenance = {"calibration": {"path": str(source), "sha256": bench.digest(source)},
                  "model": {"path": str(frozen_path), "sha256": sha},
                  "validation": {"path": str(validation_path), "sha256": bench.digest(validation_path)}}
    return calibration, validation, model, provenance


def grouped_times(document, prover, scope):
    grouped = {}
    for row in document["trials"]:
        if row["warmup"] or row["prover"] != prover or row["status"] != "success":
            continue
        value = (row["total_s"] + row["verification_s"] if scope == "local_verified_s"
                 else row.get(scope))
        if value is not None and math.isfinite(value) and value > 0:
            grouped.setdefault(row["n"], []).append(value)
    return grouped


def quantities(model, ns, delta):
    n = np.asarray(ns, dtype=float)
    t, tps, xi = bench.evaluate(model, n)
    gamma = 1 - xi
    return {"time_s": t, "mean_cost_s_per_tx": t / n, "capacity_tps": tps,
            "gamma": gamma, "xi": xi,
            "fixed_cost_s_per_tx": model["alpha_s"] / n,
            "variable_cost_s_per_tx": (t - model["alpha_s"]) / n,
            "fixed_fraction": model["alpha_s"] / t,
            "deadline_utilization": t / delta,
            "deadline_headroom": 1 - t / delta,
            "scheduled_tps": n / np.maximum(delta, t),
            # At the max() kink no unique classical derivative exists.
            "scheduled_xi": [None if math.isclose(float(s), delta, rel_tol=1e-10)
                             else (1.0 if s < delta else float(x)) for s, x in zip(t, xi)]}


def ci(draws):
    return np.quantile(draws, [.025, .975], axis=0).tolist()


def optimum(ns, times, delta, mode="deadline"):
    allowed = [(int(n), float(n / (max(delta, t) if mode == "cadence" else t)))
               for n, t in zip(ns, times) if t <= delta]
    return max(allowed, key=lambda x: (x[1], -x[0]))[0] if allowed else None


def accuracy(rows, field):
    if not rows:
        return None
    measured = np.array([r["observed"][field] for r in rows])
    predicted = np.array([r["predicted"][field] for r in rows])
    relative = predicted / measured - 1
    return {"size_count": len(rows), "mape_percent": float(100 * np.mean(np.abs(relative))),
            "median_absolute_percentage_error": float(100 * np.median(np.abs(relative))),
            "max_absolute_percentage_error": float(100 * np.max(np.abs(relative))),
            "signed_mean_percentage_error": float(100 * np.mean(relative)),
            "rmse": float(np.sqrt(np.mean((predicted - measured) ** 2))),
            "mean_absolute_log_error": float(np.mean(np.abs(np.log(predicted / measured))))}


def analyse_scope(calibration, validation, frozen, prover, scope, grid, delta, mode, count, seed):
    training = grouped_times(calibration, prover, scope)
    training = {n: v for n, v in training.items() if n <= max(grid)}
    measured = grouped_times(validation, prover, scope)
    sizes = sorted(training)
    if len(sizes) < 4:
        return {"status": "insufficient_data", "reason": "fewer than four calibration sizes"}
    if scope in ("local_verified_s", "verification_s") and scope not in frozen["models"][prover]:
        model, candidates = bench.select_model(sizes, [np.mean(training[n]) for n in sizes])
        origin = "retrospective extension: selected using calibration only, after validation collection"
    else:
        fit = frozen["models"][prover][scope]
        if "model" not in fit:
            return {"status": "insufficient_data", "reason": "frozen fit unavailable"}
        model, candidates = fit["model"], fit["candidates"]
        origin = "frozen before independent validation; coefficients unchanged"
    predicted = quantities(model, grid, delta)
    rng = np.random.default_rng(seed)
    boot_models = []
    if all(len(v) >= 2 for v in training.values()):
        for _ in range(count):
            means = [np.mean(rng.choice(training[n], len(training[n]), replace=True)) for n in sizes]
            boot_models.append(bench.fit_candidate(sizes, means, model["family"], model["p"]))
    boot_quantities = [quantities(m, grid, delta) for m in boot_models]
    param_ci = {key: np.quantile([q[key] for q in boot_quantities], [.025, .975], axis=0)
                for key in ("time_s", "capacity_tps", "gamma", "xi")} if boot_models else {}
    failed_calibration = {r["n"] for r in calibration["trials"]
                          if r["prover"] == prover and r["status"] != "success"}
    admissible = [n for n, t in zip(grid, predicted["time_s"])
                  if t <= delta and n not in failed_calibration]
    rows, observed_draws = [], {}
    for i, n in enumerate(grid):
        pred = {k: (None if v[i] is None else float(v[i])) for k, v in predicted.items()}
        point = {"n": n, "held_out_size": n not in frozen["calibration_sizes"],
                 "extrapolation": n < min(sizes) or n > max(sizes), "predicted": pred,
                 "parameter_ci95": {k: v[:, i].tolist() for k, v in param_ci.items()},
                 "predicted_admissible": n in admissible, "observed": None}
        trials = [r for r in validation["trials"] if not r["warmup"] and r["prover"] == prover and r["n"] == n]
        point["attempts"] = len(trials)
        point["failures"] = [{"id": r["id"], "status": r["status"]} for r in trials if r["status"] != "success"]
        times = measured.get(n, [])
        if times:
            mean = float(np.mean(times))
            obs = {"time_s": mean, "mean_cost_s_per_tx": mean / n, "capacity_tps": n / mean,
                   "scheduled_tps": n / float(np.mean(np.maximum(delta, times))),
                   "repetitions": len(times), "max_time_s": max(times),
                   "deadline_violations": sum(t > delta for t in times),
                   "all_attempts_successful_within_deadline": len(times) == len(trials) and max(times) <= delta}
            if len(times) >= 2:
                sampled = rng.choice(times, size=(count, len(times)), replace=True)
                means = np.mean(sampled, axis=1)
                observed_draws[n] = {"capacity_tps": n / means,
                                     "scheduled_tps": n / np.mean(np.maximum(delta, sampled), axis=1)}
                obs["time_ci95"] = ci(means)
                obs["capacity_tps_ci95"] = ci(observed_draws[n]["capacity_tps"])
            point["observed"] = obs
            point["relative_time_error"] = pred["time_s"] / mean - 1
            point["relative_capacity_error"] = pred["capacity_tps"] / obs["capacity_tps"] - 1
        rows.append(point)
    by_n = {r["n"]: r for r in rows}
    secants = []
    for n in grid:
        if 2*n not in by_n or not by_n[n]["observed"] or not by_n[2*n]["observed"]:
            continue
        a, b = by_n[n], by_n[2*n]
        obs_xi = math.log2(b["observed"]["capacity_tps"] / a["observed"]["capacity_tps"])
        pred_xi = math.log2(b["predicted"]["capacity_tps"] / a["predicted"]["capacity_tps"])
        interval = ci(np.log2(observed_draws[2*n]["capacity_tps"] / observed_draws[n]["capacity_tps"])) if (
            n in observed_draws and 2*n in observed_draws) else None
        secants.append({"n": n, "next_n": 2*n, "plot_n": math.sqrt(2)*n,
                        "observed_xi": obs_xi, "predicted_xi": pred_xi,
                        "absolute_error": abs(pred_xi - obs_xi), "measurement_ci95": interval})
    selected_field = "scheduled_tps" if mode == "cadence" else "capacity_tps"
    recommendation = max(admissible, key=lambda n: (by_n[n]["predicted"][selected_field], -n)) if admissible else None
    observed_allowed = [r for r in rows if r["observed"] and r["observed"]["all_attempts_successful_within_deadline"]]
    best = max(observed_allowed, key=lambda r: (r["observed"][selected_field], -r["n"])) if observed_allowed else None
    regret = regret_ci = None
    if best and recommendation in {r["n"] for r in observed_allowed}:
        regret = 1 - by_n[recommendation]["observed"][selected_field] / best["observed"][selected_field]
        if all(r["n"] in observed_draws for r in observed_allowed):
            regret_ci = ci(1 - observed_draws[recommendation][selected_field] / np.max(
                [observed_draws[r["n"]][selected_field] for r in observed_allowed], axis=0))
    interior = None
    if model["alpha_s"] > 0 and model["c"] > 0:
        if model["family"] == "power":
            interior = (model["alpha_s"] / ((model["p"] - 1)*model["c"])) ** (1/model["p"])
        elif model["family"] == "nlogn":
            interior = model["alpha_s"] / model["c"]
        if interior is not None and (interior <= 1 or not math.isfinite(interior)):
            interior = None
    evaluated = [r for r in rows if r["observed"]]
    held_out = [r for r in evaluated if r["held_out_size"]]
    hist = {}
    for q in boot_quantities:
        valid = [(n, t) for n, t in zip(grid, q["time_s"]) if n not in failed_calibration]
        pick = optimum([v[0] for v in valid], [v[1] for v in valid], delta, mode)
        key = str(pick) if pick is not None else "none"
        hist[key] = hist.get(key, 0) + 1
    unconstrained_rows = [r for r in rows if r["n"] not in failed_calibration]
    unconstrained = max(unconstrained_rows, key=lambda r: (r["predicted"]["capacity_tps"], -r["n"])) if unconstrained_rows else None
    observed_successes = [r for r in rows if r["observed"] and not r["failures"]]
    observed_unconstrained = max(observed_successes, key=lambda r: r["observed"]["capacity_tps"]) if observed_successes else None
    near_optimal = [n for n in admissible if by_n[n]["predicted"][selected_field] >=
                    .98 * by_n[recommendation]["predicted"][selected_field]] if recommendation else []
    crossing = bool(interior and min(grid) < interior < max(grid) and
                    bench.evaluate(model, [interior*.999])[2][0] > 0 >
                    bench.evaluate(model, [interior*1.001])[2][0])
    if recommendation is None:
        kind = "no_admissible_size"
    elif mode == "cadence":
        kind = "cadence_limited_objective"
    elif unconstrained and recommendation != unconstrained["n"]:
        kind = "deadline_limited"
    elif crossing:
        kind = "predicted_interior_maximum"
    elif model["family"] == "affine" and model["alpha_s"] == 0:
        kind = "flat_predicted_capacity"
    else:
        kind = "best_at_tested_boundary"
    return {"status": "complete", "model": model, "model_origin": origin, "candidates": candidates,
            "optimum_kind": kind, "positive_to_negative_xi_crossing_in_tested_range": crossing,
            "optimal_batch_predicted_without_deadline": unconstrained["n"] if unconstrained else None,
            "optimal_batch_observed_without_deadline": observed_unconstrained["n"] if observed_unconstrained else None,
            "predicted_sizes_within_2_percent_of_best": near_optimal,
            "uncertainty_available": bool(boot_models) and all(n in observed_draws for n in grid),
            "uncertainty_missing_reason": None if boot_models and all(n in observed_draws for n in grid)
                else "at least two successful repetitions per size in each campaign are needed; single measurements cannot establish repeatability",
            "validation_scope": "new executions at known sizes" if not held_out else "new executions including held-out sizes",
            "calibration_sizes": sizes, "rows": rows, "elasticity_secants": secants,
            "continuous_capacity_maximum": interior,
            "affine_amortization_scale_alpha_over_beta": model["alpha_s"] / model["beta_s_per_tx"] if (
                model["family"] == "affine" and model["beta_s_per_tx"] > 0) else None,
            "max_admissible_batch_predicted": max(admissible) if admissible else None,
            "max_batch_all_observed_attempts_within_deadline": max((r["n"] for r in observed_allowed), default=None),
            "optimal_batch_predicted": recommendation, "optimal_batch_observed": best["n"] if best else None,
            "optimal_batch_bootstrap_counts": hist,
            "recommendation_loss": regret, "recommendation_loss_measurement_ci95": regret_ci,
            "recommendation_loss_missing_reason": None if regret is not None else "recommendation not empirically admissible or observations absent",
            "boundary_optimum": recommendation == max(grid),
            "time_budget_nonbinding_in_predictions": bool(np.all(predicted["time_s"] <= delta)),
            "all_validation_attempts_successful_within_deadline": len(observed_allowed) == len(grid),
            "metrics": {group: {key: accuracy(values, key) for key in ("time_s", "capacity_tps", "scheduled_tps")}
                        for group, values in (("all_sizes", evaluated), ("held_out_sizes", held_out))},
            "elasticity_secant_mae": float(np.mean([s["absolute_error"] for s in secants])) if secants else None}


def analyse_shared_verification(calibration, validation, frozen, grid, delta, mode, count, seed):
    calibration_rows = bench.shared_verification_rows(calibration["trials"], frozen["provers"])
    validation_rows = bench.shared_verification_rows(validation["trials"], frozen["provers"])
    scopes = {"verification_s": frozen["shared_verification"]} if "shared_verification" in frozen else {}
    derived_model = {**frozen, "models": {"verification": scopes}}
    result = analyse_scope({"trials": calibration_rows}, {"trials": validation_rows}, derived_model,
                           "verification", "verification_s", grid, delta, mode, count, seed)
    result["aggregation"] = "arithmetic mean across proof producers per matched (size, repetition, warmup); one round remains one sample; incomplete rounds excluded"
    result["proof_producers"] = frozen["provers"]
    return result


def phase_profile(validation, prover, grid):
    profile = []
    for n in grid:
        rows = [r for r in validation["trials"] if r["n"] == n and r["prover"] == prover
                and not r["warmup"] and r["status"] == "success"]
        if not rows:
            continue
        means = {phase: float(np.mean([r[phase + "_s"] for r in rows]))
                 for phase in ("preparation", "witness", "proof", "verification")}
        total = sum(means.values())
        memory = {}
        for phase in ("witness", "proof", "verification"):
            samples = [r["steps"][phase]["resources"].get("rss_peak_bytes") for r in rows]
            values = [x for x in samples if x is not None]
            memory[phase] = {"max_sampled_rss_bytes": max(values) if values else None,
                             "available_repetitions": len(values)}
        profile.append({"n": n, "mean_phase_s": means,
                        "phase_time_fraction": {k: v / total for k, v in means.items()},
                        "longest_phase": max(means, key=means.get), "memory": memory})
    return profile


def figures(report, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    plt.rcParams.update({"font.family": "DejaVu Serif", "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "savefig.dpi": 220, "pdf.fonttype": 42})
    grid = report["config"]["grid"]
    dense = np.geomspace(min(grid), max(grid), 300)
    paths = []

    def save(fig, name):
        for suffix in ("png", "pdf"):
            path = out / f"{name}.{suffix}"
            fig.savefig(path, bbox_inches="tight")
            paths.append(path.name)
        plt.close(fig)

    def axis(ax):
        ax.set_xscale("log", base=2)
        ticks = grid[::max(2, math.ceil(len(grid)/6))]
        if grid[-1] not in ticks:
            if len(ticks) > 1 and grid[-1] / ticks[-1] < 4:
                ticks.pop()
            ticks.append(grid[-1])
        ax.set_xticks(ticks, [str(n) for n in ticks])
        ax.set_xlabel("Batch size N")
        ax.grid(alpha=.2, linestyle="--")

    for scope, name in (("proof_generation_s", "01_elasticity"), ("local_verified_s", "03_local_processing"),
                        ("verification_s", "05_verification")):
        fig, axes = plt.subplots(2, 2, figsize=(11, 7.5))
        for ax in axes.flat:
            axis(ax)
        series = [(p, scope, scopes[scope], "-") for p, scopes in report["provers"].items()]
        if scope == "proof_generation_s":
            series.append(("verification", "verification_s", report["verification"], "--"))
        elif scope == "verification_s":
            series = [("verification", "verification_s", report["verification"], "--")]
        handles = []
        for prover, series_scope, result, style in series:
            if result["status"] != "complete":
                continue
            color = COLORS.get(prover, "#009E73")
            label = "Verification (snarkjs)" if series_scope == "verification_s" else prover
            handles.append(Line2D([], [], color=color, ls=style, label=label))
            q = quantities(result["model"], dense, report["config"]["delta_s"])
            for ax, field in zip(axes.flat, ("time_s", "mean_cost_s_per_tx", "capacity_tps", "gamma")):
                ax.plot(dense, q[field], color=color, ls=style, label=label)
                if field == "gamma":
                    continue
                for held, marker in ((False, "o"), (True, "s")):
                    rows = [r for r in result["rows"] if r["observed"] and r["held_out_size"] == held]
                    ax.scatter([r["n"] for r in rows], [r["observed"][field] for r in rows],
                               facecolors="white" if held else color, edgecolors=color, marker=marker, s=25, zorder=3)
                if field in ("time_s", "capacity_tps"):
                    rows = [r for r in result["rows"] if field in r["parameter_ci95"]]
                    ax.fill_between([r["n"] for r in rows], [r["parameter_ci95"][field][0] for r in rows],
                                    [r["parameter_ci95"][field][1] for r in rows], color=color, alpha=.12)
            secants = result["elasticity_secants"]
            ax = axes[1, 1]
            sx = [s["plot_n"] for s in secants]
            # Same secant operator for prediction and observation; analytic curve is separate.
            ax.plot(sx, [1-s["predicted_xi"] for s in secants], color=color, ls="none", marker="x", ms=4)
            ax.scatter(sx, [1-s["observed_xi"] for s in secants], color=color, s=14, zorder=3)
            for s in secants:
                if s["measurement_ci95"]:
                    lo, hi = s["measurement_ci95"]
                    ax.vlines(s["plot_n"], 1-hi, 1-lo, color=color, alpha=.55)
        for ax in (axes[0, 0], axes[0, 1], axes[1, 0]):
            ax.set_yscale("log")
        deadline = report["config"]["delta_s"]
        # A generous budget should not compress the measured curves on a log axis.
        if deadline <= axes[0, 0].get_ylim()[1] * 1.2:
            axes[0, 0].axhline(deadline, color=".4", ls=":")
        axes[0, 0].text(.02, .95, f"Deadline: {deadline:g} s", transform=axes[0, 0].transAxes,
                        va="top", color=".4", fontsize=8)
        for ax, title, ylabel in zip(axes.flat,
                ("(a) Batch service time", "(b) Amortized service cost", "(c) Processing capacity", "(d) Scaling elasticity"),
                ("T(N) [s]", "T(N)/N [s/tx]", "N/T(N) [tx/s]", r"$\gamma = d\ln T / d\ln N$")):
            ax.set_title(title, loc="left")
            ax.set_ylabel(ylabel)
        ax = axes[1, 1]
        lo, hi = ax.get_ylim()
        ax.set_ylim(min(-.2, lo), max(1.15, hi))
        ax.axhspan(ax.get_ylim()[0], 1, color="#009E73", alpha=.055)
        ax.axhspan(1, ax.get_ylim()[1], color="#D55E00", alpha=.07)
        ax.axhline(1, color=".4", ls=":")
        sec = ax.secondary_yaxis("right", functions=(lambda y: 1-y, lambda y: 1-y))
        sec.set_ylabel(r"$\xi = 1-\gamma$")
        handles += [Line2D([], [], color=".3", marker="o", ls="none", label="Independent validation"),
                    Line2D([], [], color=".3", marker="x", ls="none", label="Predicted secant (d)")]
        if any(r["held_out_size"] for scopes in report["provers"].values()
               for r in scopes[scope].get("rows", [])):
            handles.append(Line2D([], [], color=".3", marker="s", mfc="white", ls="none", label="Held-out size"))
        fig.legend(handles=handles, loc="lower center", ncol=3, fontsize=9)
        title = ("Witness + proof, and verification separately" if scope == "proof_generation_s" else LABELS[scope])
        title += " — model vs measurements"
        if any("retrospective" in result.get("model_origin", "") for _, _, result, _ in series):
            title += "\nIncludes retrospective calibration-only extension"
        fig.suptitle(title, fontsize=12)
        fig.tight_layout(rect=(0, .14 if len(handles) > 6 else .085, 1, .94))
        save(fig, name)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.1))
    for col, (prover, scopes) in enumerate(report["provers"].items()):
        if col >= 2:
            break
        result = scopes["proof_generation_s"]
        if result["status"] != "complete":
            continue
        rows = [r for r in result["rows"] if r["observed"]]
        color = COLORS.get(prover, "k")
        ax = axes[col]
        axis(ax)
        x = [r["n"] for r in rows]
        ax.plot(x, [r["predicted"]["capacity_tps"] for r in rows], color=color, label="Theory")
        ax.plot(x, [r["observed"]["capacity_tps"] for r in rows], "o--", color=color, label="Validation")
        for r in rows:
            if "capacity_tps_ci95" in r["observed"]:
                ax.vlines(r["n"], *r["observed"]["capacity_tps_ci95"], color=color)
        verification = report["verification"]
        if verification["status"] == "complete":
            vr = [r for r in verification["rows"] if r["observed"]]
            vx = [r["n"] for r in vr]
            ax.plot(vx, [r["predicted"]["capacity_tps"] for r in vr], color="#009E73", ls="--", label="Verification theory")
            ax.plot(vx, [r["observed"]["capacity_tps"] for r in vr], "o", color="#009E73", label="Verification validation")
            for r in vr:
                if "capacity_tps_ci95" in r["observed"]:
                    ax.vlines(r["n"], *r["observed"]["capacity_tps_ci95"], color="#009E73")
            ax.set_yscale("log")
        metric = result["metrics"]["all_sizes"]["capacity_tps"]
        ax.set_title(f"{prover} — generation MAPE: {metric['mape_percent']:.2f}%" if metric else prover)
        ax.set_ylabel("Equivalent stage capacity N/T [tx/s]")
        ax.legend(fontsize=8)
    fig.suptitle("Proof generation and verification — theory vs independent measurements")
    fig.tight_layout(rect=(0, 0, 1, .96))
    save(fig, "02_theory_vs_experiment")

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.1))
    phase_colors = ["#999999", "#E69F00", "#0072B2", "#009E73"]
    for ax, (prover, profile) in zip(axes, report["phase_profiles"].items()):
        bottom = np.zeros(len(profile))
        for phase, color in zip(("preparation", "witness", "proof", "verification"), phase_colors):
            values = np.array([r["mean_phase_s"][phase] for r in profile])
            ax.bar(range(len(profile)), values, bottom=bottom, color=color, label=phase)
            bottom += values
        ax.set_xticks(range(len(profile)), [r["n"] for r in profile], rotation=45)
        ax.set_title(prover)
        ax.set_xlabel("Batch size N")
        ax.set_ylabel("Mean sequential phase duration [s]")
    axes[0].legend(fontsize=8)
    fig.suptitle("Measured phase durations (not physical-resource bottleneck indices)")
    fig.tight_layout(rect=(0, 0, 1, .94))
    save(fig, "04_phase_durations")
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation", type=Path, required=True, help="completed validation result.json")
    parser.add_argument("--calibration", type=Path, help="override source location after moving a campaign")
    parser.add_argument("--model", type=Path, help="override frozen fit result.json location")
    parser.add_argument("--delta", type=float, default=300., help="maximum per-batch service time in seconds (default: 300)")
    parser.add_argument("--delta-mode", choices=("deadline", "cadence"), default="deadline")
    parser.add_argument("--max-batch", type=int, default=8192, help="experimental circuit bound (default: 8192)")
    parser.add_argument("--bootstrap", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20261007)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if not math.isfinite(args.delta) or args.delta <= 0 or args.bootstrap < 50 or args.seed < 0:
        parser.error("delta>0, bootstrap>=50 and seed>=0 required")
    if args.max_batch < 1 or args.max_batch & (args.max_batch-1):
        parser.error("max-batch must be a positive power of two")
    calibration, validation, frozen, provenance = load_sources(args.validation.resolve(), args.calibration, args.model)
    grid = [n for n in frozen["prediction_grid"] if n <= args.max_batch
            and frozen["artifacts"][str(n)]["supported"]]
    if not grid:
        parser.error("no supported circuit in the requested range")
    # Keep the original frozen fit and its full training range for uncertainty.
    if max(frozen["calibration_sizes"]) > max(grid):
        parser.error("max-batch cannot truncate the frozen calibration range; use a matching campaign")
    report = {"schema": 1, "created_utc": bench.utc_now(), "status": "complete", "sources": provenance,
              "config": {"delta_s": args.delta, "delta_mode": args.delta_mode, "max_batch": args.max_batch,
                         "grid": grid, "bootstrap": args.bootstrap, "seed": args.seed,
                         "objective": "N/T subject to T<=delta" if args.delta_mode == "deadline" else "N/max(delta,T) subject to T<=delta"},
              "method": {"service_model": "effective wall seconds; W/R identified, W and R not separately identified",
                         "gamma": "N*T'(N)/T(N)", "xi": "1-gamma = d ln(N/T)/d ln N",
                         "uncertainty": "pointwise 95% within-size bootstrap; parameters conditional on family/p; no model-selection or systematic uncertainty",
                         "measured_capacity": "N / arithmetic mean service time, successful non-warmup trials only",
                         "scheduled_throughput": "prediction N/max(delta,T); observed renewal estimate N/mean(max(delta,T_i)); not a timed scheduler benchmark",
                         "admissibility": "available circuits and calibration success, predicted mean time<=delta; observed criterion requires every recorded attempt successful within delta, not a guarantee",
                         "elasticity_validation": "compare logarithmic secants over [N,2N]; points at geometric midpoint; never compare secants as exact derivatives",
                         "accuracy": "equal weight per size; held-out sizes reported separately; MAPE is an error, not a probability of correctness",
                         "local_scope": "total_s + verification_s per trial; sum of measured intervals, not contiguous rollup end-to-end latency; origin is recorded per model",
                         "verification_scope": "snarkjs groth16 verify wall time; shared curve averages matched proof producers per repetition (not additional repetitions); individual diagnostics retained; N/T is equivalent transactions/s, not proofs/s or full rollup throughput",
                         "boundary": "maximum of tested supported grid, not physical or protocol ceiling"},
              "unidentified_theory": {"physical_resource_workloads_and_capacities": None,
                                      "protocol_budgets": None, "resource_bottleneck_B_and_H": None,
                                      "reason": "phase durations do not identify independent resource demands/capacities; DA, L1, queueing and full prototype execution unmeasured"},
              "analysis_source_sha256": bench.digest(__file__), "library_versions": bench.analysis_versions(),
              "provers": {}, "phase_profiles": {}}
    for pi, prover in enumerate(frozen["provers"]):
        report["provers"][prover] = {}
        for si, scope in enumerate(SCOPES):
            print(f"Analysing {prover}/{scope}...", flush=True)
            report["provers"][prover][scope] = analyse_scope(calibration, validation, frozen, prover, scope,
                grid, args.delta, args.delta_mode, args.bootstrap, args.seed + 100*pi + si)
        report["phase_profiles"][prover] = phase_profile(validation, prover, grid)
    report["verification"] = analyse_shared_verification(calibration, validation, frozen, grid,
        args.delta, args.delta_mode, args.bootstrap, args.seed + 1000)
    out = bench.output_directory("theory", args.out)
    report["figures"] = figures(report, out)
    from importlib.metadata import version
    report["library_versions"]["matplotlib"] = version("matplotlib")
    bench.save_result(out, report)
    print(f"Results: {out / 'result.json'}")
    for p, scopes in report["provers"].items():
        s = scopes["proof_generation_s"]
        if s["status"] == "complete":
            metric = s["metrics"]["all_sizes"]["capacity_tps"]
            print(f"{p}: predicted N*={s['optimal_batch_predicted']}, observed N*={s['optimal_batch_observed']}, validation TPS MAPE={metric['mape_percent'] if metric else None}; {s['optimum_kind']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
