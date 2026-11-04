#!/usr/bin/env python3
"""Exp 3: measured stage dominance and crossover brackets, without fitting.

python scripts/analysis/exp3.py --from bench-out/20260825_120620
python scripts/analysis/exp3.py --from bench-out/my_run --outdir bench-out/exp3/my_run

Elapsed stage times are descriptive. N/max(T_stage) is an ideal pipeline
bound requiring independent stages/resources; N/sum(T_stage) is a sequential
estimate. Neither is measured sustained rollup throughput.
"""
from __future__ import annotations

import argparse
import csv
from itertools import combinations
import json
from pathlib import Path

import numpy as np

if __package__:
    from .exp1 import COLORS as PROVER_COLORS
    from .exp1 import ROOT, number, write_csv
else:
    from exp1 import COLORS as PROVER_COLORS
    from exp1 import ROOT, number, write_csv

LABELS = {
    "batch_build": "Batch construction",
    "compute_batch": "Batch execution (Executor)",
    "input_prepare": "Circuit input preparation",
    "state_hash": "State hashes (before + after)",
    "da_encode": "DA serialization + compression",
    "da_persist": "DA persistence",
    "snapshot_persist": "Snapshot persistence",
    "witness": "Witness calculation",
    "proof": "Proof generation only",
    "witness_and_proof": "Witness + proof (fullprove)",
    "verify": "Local verification (snarkjs)",
}
METRICS = ("wall_s", "cpu_total_s", "rss_peak_bytes", "io_read_bytes", "io_write_bytes")
LOCAL_STAGES = set(LABELS) - {"witness", "proof", "witness_and_proof", "verify"}


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def successful(row):
    return str(row.get("ok", "")).lower() in ("true", "1")


def stage_name(row):
    step = row["step"]
    if row["phase"] == "verify":
        return "verify"
    for prefix, stage in (("fullprove_", "witness_and_proof"),
                          ("witness_", "witness"), ("prove_", "proof")):
        if step.startswith(prefix):
            return stage
    raise ValueError(f"Unknown recurring step: {step}")


def load_measurements(run_dir):
    """Discard whole failed proving repetitions, not just their failed steps."""
    raw = read_csv(run_dir / "steps.csv")
    manifest_path = run_dir / "manifest.json"
    sampler_available = True
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        sampler_available = manifest.get("env", {}).get("sampler_backend") != "none"
    reps = {}
    rejected = []
    for row in raw:
        if row["phase"] not in ("prove", "verify"):
            continue
        n = int(row["size"])
        if n < 1:
            raise ValueError("Batch sizes must be positive")
        key = (n, row.get("prover", ""), row["rep"], row["phase"])
        reps.setdefault(key, []).append(row)
    observations = []
    for (n, prover, rep, phase), rows in reps.items():
        names = [stage_name(r) for r in rows]
        valid_layout = (phase == "verify" or names == ["witness_and_proof"]
                        or sorted(names) == ["proof", "witness"])
        if not all(successful(r) for r in rows) or not valid_layout:
            rejected.append(dict(n=n, prover=prover, rep=rep, phase=phase))
            continue
        # Group verifier substeps within one repetition, if present.
        stage_groups = [(stage, [r for r in rows if stage_name(r) == stage])
                        for stage in dict.fromkeys(names)]
        if phase == "prove":
            # Sum within repetitions before computing SD: witness/proof
            # covariance must be preserved, as in Exp 1's proving phase.
            stage_groups.append(("proving_total", rows))
        for stage, members in stage_groups:
            item = dict(n=n, prover=prover, rep=rep, stage=stage,
                        timing_scope="command_including_startup",
                        resource_scope="|".join(sorted({r.get("scope", "process") for r in members})))
            for metric in METRICS:
                values = [number(r, metric) for r in members]
                item[metric] = float((np.max if metric == "rss_peak_bytes" else np.sum)(values))
                if not sampler_available and metric != "wall_s":
                    # rusage can still supply CPU on Unix, but an absent
                    # sampler cannot substantiate zero RAM/disk work.
                    if metric != "cpu_total_s" or item[metric] == 0:
                        item[metric] = np.nan
            observations.append(item)
    local_path = run_dir / "local_steps.csv"
    if local_path.exists():
        local = read_csv(local_path)
        local_reps = {}
        for row in local:
            if int(row["size"]) < 1:
                raise ValueError("Batch sizes must be positive")
            local_reps.setdefault((int(row["size"]), row["rep"]), []).append(row)
        for (n, rep), rows in local_reps.items():
            if not all(successful(r) for r in rows):
                rejected.append(dict(n=n, prover="", rep=rep, phase="local"))
                continue
            stages = [r["stage"] for r in rows]
            expected = LOCAL_STAGES
            if set(stages) != expected or len(stages) != len(expected):
                raise ValueError(f"Incomplete/duplicate local stages: N={n}, rep={rep}")
            for row in rows:
                observations.append(dict(n=n, prover="", rep=rep, stage=row["stage"],
                                         timing_scope=row["timing_scope"], resource_scope="operation_cpu_only",
                                         **{m: number(row, m) for m in METRICS}))
    if not observations:
        raise ValueError("No successful recurring measurements")
    buckets = {}
    for row in observations:
        if not np.isfinite(row["wall_s"]) or row["wall_s"] <= 0:
            raise ValueError(f"Invalid duration (dry runs cannot be analyzed): {row}")
        key = row["n"], row["prover"], row["stage"]
        buckets.setdefault(key, []).append(row)
    result = []
    for (n, prover, stage), rows in sorted(buckets.items()):
        if len({r["rep"] for r in rows}) != len(rows):
            raise ValueError(f"Duplicate stage repetition: {n}, {prover}, {stage}")
        scopes = {(r["timing_scope"], r["resource_scope"]) for r in rows}
        if len(scopes) != 1:
            raise ValueError(f"Mixed measurement scopes: {n}, {prover}, {stage}")
        timing, resource = scopes.pop()
        row = dict(n=n, prover=prover, stage=stage, n_reps=len(rows),
                   timing_scope=timing, resource_scope=resource)
        for metric in METRICS:
            values = [r[metric] for r in rows]
            # Missing counters stay missing, rather than turning into zero work.
            for suffix, reducer in (("mean", np.mean), ("stdev", np.std), ("max", np.max)):
                row[f"{metric}_{suffix}"] = float(reducer(values))
        result.append(row)
    return result, rejected


def build_pipelines(rows, tie_rtol=1e-6, release_period=None):
    backends = sorted({r["prover"] for r in rows if r["prover"]}) or ["local_only"]
    pipelines = []
    for backend in backends:
        selected = [r for r in rows if r["prover"] in ("", backend)]
        stages = [s for s in LABELS if any(r["stage"] == s for r in selected)]
        if backend != "local_only" and ("verify" not in stages or not (
                "witness_and_proof" in stages or {"witness", "proof"}.issubset(stages))):
            raise ValueError(f"Incomplete recurring workflow for {backend}: {stages}")
        lookup = {(r["n"], r["stage"]): r for r in selected}
        all_sizes = sorted({r["n"] for r in selected})
        sizes = [n for n in all_sizes if all((n, s) in lookup for s in stages)]
        if not sizes:
            raise ValueError(f"No complete size grid for {backend}")
        if "witness_and_proof" in stages and ("proof" in stages or "witness" in stages):
            raise ValueError("Cannot mix fullprove and split-prove protocols in one pipeline")
        times = np.array([[lookup[n, s]["wall_s_mean"] for s in stages] for n in sizes])
        maximum = times.max(axis=1)
        dominant = [tuple(s for s, t in zip(stages, ts) if np.isclose(t, mx, rtol=tie_rtol, atol=0))
                    for ts, mx in zip(times, maximum)]
        pipelines.append(dict(backend=backend, stages=stages, n=np.array(sizes),
                              times=times, maximum=maximum, total=times.sum(axis=1),
                              dominant=dominant, lookup=lookup,
                              excluded_sizes=sorted(set(all_sizes) - set(sizes)),
                              release_period=release_period))
    return pipelines


def crossover_brackets(pipeline, tie_rtol=1e-6):
    """Report sign reversals on the measured grid; a tangency is not a switch.

    Zero runs (ties) are bridged only when strict dominance actually reverses.
    No interpolated crossing location is claimed.
    """
    out = []
    stages, times, ns = pipeline["stages"], pipeline["times"], pipeline["n"]
    for a, b in combinations(range(len(stages)), 2):
        sign = np.sign(times[:, a] - times[:, b])
        sign[np.isclose(times[:, a], times[:, b], rtol=tie_rtol, atol=0)] = 0
        previous = None
        for i, current in enumerate(sign):
            if current == 0:
                continue
            if previous is not None and current != sign[previous]:
                before, after = ((stages[a], stages[b]) if sign[previous] > 0
                                 else (stages[b], stages[a]))
                dominant = pipeline["dominant"]
                effective = (dominant[previous] == (before,) and dominant[i] == (after,)
                             and all(set(d).issubset({before, after}) for d in dominant[previous:i + 1]))
                out.append(dict(prover=pipeline["backend"], stage_before=before, stage_after=after,
                                N_low=int(ns[previous]), N_high=int(ns[i]),
                                kind="dominant_stage_switch" if effective else "pairwise_crossing",
                                uncertainty="measured bracket; no fitted crossing or confidence interval"))
            previous = i
    return out


def export(pipelines, outdir, tie_rtol):
    summary, transitions, metrics = [], [], []
    for p in pipelines:
        for i, n in enumerate(p["n"]):
            maximum, total = float(p["maximum"][i]), float(p["total"][i])
            interval = max(maximum, p["release_period"] or 0)
            summary.append(dict(prover=p["backend"], n=int(n),
                                dominant_stages="|".join(p["dominant"][i]),
                                T_max_stage_s=maximum, T_sum_stages_s=total,
                                ideal_pipeline_bound_tx_s=float(n / maximum),
                                sequential_estimate_tx_s=float(n / total),
                                release_period_s=p["release_period"],
                                ideal_bound_with_release_tx_s=float(n / interval)))
            for j, stage in enumerate(p["stages"]):
                row = p["lookup"][int(n), stage]
                metrics.append(dict(pipeline=p["backend"], **row,
                                    normalized_time=float(p["times"][i, j] / maximum),
                                    equivalent_stage_rate_tx_s=float(n / p["times"][i, j])))
        transitions.extend(crossover_brackets(p, tie_rtol))
    write_csv(outdir / "stage_metrics.csv", metrics)
    write_csv(outdir / "dominance.csv", summary)
    # Keep a header even when no transition has been observed.
    fields = ["prover", "stage_before", "stage_after", "N_low", "N_high", "kind", "uncertainty"]
    with (outdir / "transitions.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(transitions)
    return transitions


def proving_comparison(pipelines, outdir, tie_rtol):
    """Separate diagnostic matching Exp 1's witness+proof phase definition."""
    rows, transitions, comparisons = [], [], []
    for p in pipelines:
        if not all((int(n), stage) in p["lookup"] for n in p["n"]
                   for stage in ("proving_total", "verify")):
            continue
        times = np.array([[p["lookup"][int(n), s]["wall_s_mean"]
                           for s in ("proving_total", "verify")] for n in p["n"]])
        maximum = times.max(axis=1)
        dominant = [tuple(s for s, t in zip(("proving_total", "verify"), ts)
                          if np.isclose(t, mx, rtol=tie_rtol, atol=0))
                    for ts, mx in zip(times, maximum)]
        diagnostic = dict(p, stages=["proving_total", "verify"], times=times,
                          maximum=maximum, dominant=dominant)
        comparisons.append(diagnostic)
        transitions.extend(crossover_brackets(diagnostic, tie_rtol))
        for n, (proving, verify) in zip(p["n"], times):
            rows.append(dict(prover=p["backend"], n=int(n),
                             T_witness_plus_proof_s=float(proving), T_verify_s=float(verify),
                             proving_stdev_s=p["lookup"][int(n), "proving_total"]["wall_s_stdev"],
                             ratio_proving_to_verify=float(proving / verify)))
    write_csv(outdir / "proving_comparison.csv", rows)
    if comparisons:
        fields = ["prover", "stage_before", "stage_after", "N_low", "N_high", "kind", "uncertainty"]
        with (outdir / "proving_transitions.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(transitions)
    return comparisons, transitions


def figures(pipelines, outdir, font_size=8, width=8.5, comparisons=(), tie_rtol=1e-6,
            dpi=300, formats=("png", "pdf")):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": font_size, "axes.grid": True, "grid.alpha": .22,
                         "savefig.bbox": "tight"})
    from matplotlib.colors import LinearSegmentedColormap
    colors = dict(zip(LABELS, ("#555555", "#D55E00", "#E69F00", "#56B4E9",
                               "#882255", "#44AA99", "#999933")))

    def stage_style(stage, backend):
        if stage == "verify":
            return dict(color=PROVER_COLORS[""], linestyle="--", marker="o", markersize=3)
        if stage in ("proof", "witness", "witness_and_proof", "proving_total"):
            return dict(color=PROVER_COLORS.get(backend, "#555555"),
                        linestyle=":" if stage == "witness" else "-",
                        marker="^" if stage == "witness" else "o", markersize=3)
        return dict(color=colors[stage], linestyle="-", marker="o", markersize=3)

    def stage_label(stage, backend):
        if stage in ("proof", "witness", "witness_and_proof", "proving_total"):
            label = {"proof": "proof", "witness": "witness",
                     "witness_and_proof": "proof (fullprove)", "proving_total": "witness + proof"}[stage]
            return f"{backend}/{label}"
        return "verify (local)" if stage == "verify" else LABELS[stage]

    def save(fig, name, title, note, legend=True):
        if legend:
            entries = {}
            for ax in fig.axes:
                handles, labels = ax.get_legend_handles_labels()
                for handle, label in zip(handles, labels):
                    entries.setdefault(label, handle)
            if entries:
                fig.legend(list(entries.values()), list(entries), loc="outside lower center",
                           ncol=min(3, len(entries)), fontsize="small")
        fig.suptitle(f"{title}\n{note}", fontsize=font_size)
        for ext in formats:
            fig.savefig(outdir / f"{name}.{ext}", dpi=dpi)
        plt.close(fig)

    fig, axes = plt.subplots(1, len(pipelines), squeeze=False,
                             figsize=(width, width * .48), layout="constrained")
    for index, (ax, p) in enumerate(zip(axes.flat, pipelines)):
        for stage in p["stages"]:
            data = [p["lookup"][int(n), stage] for n in p["n"]]
            t = np.array([r["wall_s_mean"] for r in data])
            sd = np.array([r["wall_s_stdev"] for r in data])
            style = stage_style(stage, p["backend"])
            ax.plot(p["n"], t, label=stage_label(stage, p["backend"]), **style)
            ax.fill_between(p["n"], np.where(t - sd > 0, t - sd, np.nan), t + sd,
                            color=style["color"], alpha=.12)
        ax.plot(p["n"], p["maximum"], color="0.35", linestyle="--", linewidth=1.3,
                label="Maximum stage mean")
        ax.set(yscale="log", xlabel=r"Batch size $N$ [tx]", ylabel=r"$T_p(N)$ [s]")
        ax.set_title(f"({chr(97 + index)}) {p['backend']}", loc="left")
        ax.set_xscale("log", base=2)
    save(fig, "01_stage_times", "Measured stage times and dominant-stage envelope",
         "Time shading: +/- 1 SD; ZK commands include startup, local operations exclude it")

    fig, axes = plt.subplots(len(pipelines), 1, squeeze=False,
                             figsize=(width, width * .39 * len(pipelines)), layout="constrained")
    dominance_cmap = LinearSegmentedColormap.from_list("exp1_dominance", ["#FFFFFF", PROVER_COLORS[""]])
    for index, (ax, p) in enumerate(zip(axes.flat, pipelines)):
        image = ax.imshow((p["times"] / p["maximum"][:, None]).T, aspect="auto",
                          vmin=0, vmax=1, cmap=dominance_cmap, interpolation="nearest")
        ax.set_xticks(range(len(p["n"])), [str(n) for n in p["n"]])
        ax.set_yticks(range(len(p["stages"])), [LABELS[s] for s in p["stages"]])
        ax.set(xlabel=r"Batch size $N$ [tx]")
        ax.set_title(f"({chr(97 + index)}) {p['backend']}", loc="left")
        ax.grid(False)
        for i, dominant in enumerate(p["dominant"]):
            for stage in dominant:
                ax.text(i, p["stages"].index(stage), "*", ha="center", va="center", color="white")
        fig.colorbar(image, ax=ax, label=r"$T_p(N)/\max_q T_q(N)$")
    save(fig, "02_stage_dominance", "Measured stage dominance",
         "Stars identify the dominant stage(s); normalized elapsed time, not physical resource utilization", legend=False)

    fig, axes = plt.subplots(1, len(pipelines), squeeze=False,
                             figsize=(width, width * .48), layout="constrained")
    for index, (ax, p) in enumerate(zip(axes.flat, pipelines)):
        color = PROVER_COLORS.get(p["backend"], "#555555")
        ax.plot(p["n"], p["n"] / p["maximum"], marker="o", markersize=3, color=color,
                linestyle="-", label=f"{p['backend']}/ideal pipeline")
        ax.plot(p["n"], p["n"] / p["total"], marker="o", markersize=3, color=color,
                linestyle="--", label=f"{p['backend']}/sequential")
        if p["release_period"] is not None:
            ax.plot(p["n"], p["n"] / np.maximum(p["maximum"], p["release_period"]),
                    ":", color=color, label=f"{p['backend']}/with release interval")
        ax.set(xlabel=r"Batch size $N$ [tx]", ylabel=r"Derived rate [tx/s]", yscale="log")
        ax.set_title(f"({chr(97 + index)}) {p['backend']}", loc="left")
        ax.set_xscale("log", base=2)
    save(fig, "03_derived_rates", "Ideal pipeline bound and sequential rate estimate",
         r"$N/\max_p T_p$ versus $N/\sum_p T_p$; neither is measured sustained rollup throughput")

    for p in pipelines:
        fig, axes = plt.subplots(2, 2, figsize=(width, width * .78), layout="constrained")
        for index, (ax, (metric, title, ylabel)) in enumerate(zip(axes.flat, [
            ("cpu_total_s", "Total CPU work", r"$W_{\mathrm{CPU},p}(N)$ [core s]"),
            ("rss_peak_bytes", "Peak memory occupancy", r"$M_p^{\mathrm{peak}}(N)$ [bytes]"),
            ("io_read_bytes", "Disk reads", r"$W_{\mathrm{IO,read},p}(N)$ [bytes]"),
            ("io_write_bytes", "Disk writes", r"$W_{\mathrm{IO,write},p}(N)$ [bytes]")])):
            for stage in p["stages"]:
                data = [p["lookup"][int(n), stage] for n in p["n"]]
                # Docker's system-wide counters cannot identify stage CPU/RAM/IO.
                values = np.array([r[f"{metric}_mean"]
                                   if "system" not in r["resource_scope"].split("|") else np.nan for r in data])
                if np.any(np.isfinite(values)):
                    style = stage_style(stage, p["backend"])
                    ax.plot(p["n"], values, label=stage_label(stage, p["backend"]), **style)
                    if metric == "cpu_total_s":
                        sd = np.array([r[f"{metric}_stdev"] for r in data])
                        ax.fill_between(p["n"], np.where(values - sd > 0, values - sd, np.nan),
                                        values + sd, color=style["color"], alpha=.12)
            ax.set(xlabel=r"Batch size $N$ [tx]", ylabel=ylabel)
            ax.set_title(f"({chr(97 + index)}) {title}", loc="left")
            ax.set_xscale("log", base=2)
            # Log axes match Exp 1 for positive measurements. Keep genuine
            # sampled zeros visible rather than replacing them by epsilon.
            positive = any(np.any(line.get_ydata() > 0) for line in ax.lines)
            zero = any(np.any(line.get_ydata() == 0) for line in ax.lines)
            if zero or not positive:
                ax.set_yscale("symlog", linthresh=1 if metric != "cpu_total_s" else .001)
                ax.set_ylim(bottom=0)
                if not positive:
                    ax.set_ylim(0, 1)
            else:
                ax.set_yscale("log")
        save(fig, f"04_resources_{p['backend']}", f"CPU work, memory occupancy and I/O — {p['backend']}",
             "CPU shading: +/- 1 SD; local operations: CPU only; Docker system counters omitted")

    if comparisons:
        fig, axes = plt.subplots(1, len(comparisons), squeeze=False,
                                 figsize=(width, width * .48), layout="constrained")
        for index, (ax, p) in enumerate(zip(axes.flat, comparisons)):
            split = all((int(n), "proof") in p["lookup"] for n in p["n"])
            stages = ("proof", "witness", "verify") if split else ("witness_and_proof", "verify")
            for stage in stages:
                if not all((int(n), stage) in p["lookup"] for n in p["n"]):
                    continue
                data = [p["lookup"][int(n), stage] for n in p["n"]]
                t = np.array([r["wall_s_mean"] for r in data])
                sd = np.array([r["wall_s_stdev"] for r in data])
                style = stage_style(stage, p["backend"])
                ax.plot(p["n"], t, label=stage_label(stage, p["backend"]), **style)
                ax.fill_between(p["n"], np.where(t - sd > 0, t - sd, np.nan), t + sd, color=style["color"], alpha=.12)
            ax.set(xlabel=r"Batch size $N$ [tx]", ylabel=r"$T_p(N)$ [s]", yscale="log")
            ax.set_title(f"({chr(97 + index)}) {p['backend']}", loc="left")
            ax.set_xscale("log", base=2)
            diagnostic = [p]
            if split:
                proof_times = np.array([[p["lookup"][int(n), s]["wall_s_mean"]
                                         for s in ("proof", "verify")] for n in p["n"]])
                proof_dominant = [tuple(s for s, t in zip(("proof", "verify"), ts)
                                        if np.isclose(t, max(ts), rtol=tie_rtol, atol=0))
                                  for ts in proof_times]
                diagnostic = [dict(p, stages=["proof", "verify"], times=proof_times,
                                   dominant=proof_dominant)]
            for bracket_index, comparison in enumerate(diagnostic):
                for crossing in crossover_brackets(comparison, tie_rtol):
                    if "verify" in (crossing["stage_before"], crossing["stage_after"]):
                        ax.axvspan(crossing["N_low"], crossing["N_high"],
                                   color=PROVER_COLORS.get(p["backend"], "#555555"),
                                   alpha=.08 if bracket_index == 0 else .12)
        save(fig, "05_proving_vs_verify", "Proof generation, witness calculation and local verification",
             "Time shading: +/- 1 SD; vertical bands: measured crossing brackets")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="run_dir", type=Path, default=ROOT / "bench-out/20260825_120620")
    parser.add_argument("--outdir", type=Path, default=ROOT / "bench-out/exp3")
    parser.add_argument("--release-period-s", type=float)
    parser.add_argument("--tie-rtol", type=float, default=1e-6)
    parser.add_argument("--fig-font-size", type=float, default=8)
    parser.add_argument("--fig-width", type=float, default=8.5)
    parser.add_argument("--fig-dpi", type=int, default=300)
    parser.add_argument("--fig-format", nargs="+", choices=["png", "pdf", "svg"], default=["png", "pdf"])
    args = parser.parse_args()
    if args.outdir.resolve() == args.run_dir.resolve():
        parser.error("Output must differ from the source dataset directory")
    for key in ("release_period_s", "fig_font_size", "fig_width"):
        value = getattr(args, key)
        if value is not None and (not np.isfinite(value) or value <= 0):
            parser.error(f"{key} must be positive and finite")
    if not np.isfinite(args.tie_rtol) or not 0 <= args.tie_rtol < 1:
        parser.error("--tie-rtol must be in [0, 1)")
    if args.fig_dpi < 1:
        parser.error("--fig-dpi must be positive")
    rows, rejected = load_measurements(args.run_dir)
    pipelines = build_pipelines(rows, args.tie_rtol, args.release_period_s)
    args.outdir.mkdir(parents=True, exist_ok=True)
    transitions = export(pipelines, args.outdir, args.tie_rtol)
    comparisons, proving_transitions = proving_comparison(pipelines, args.outdir, args.tie_rtol)
    figures(pipelines, args.outdir, args.fig_font_size, args.fig_width, comparisons, args.tie_rtol,
            args.fig_dpi, args.fig_format)
    manifest = dict(source=str(args.run_dir.resolve()), tie_rtol=args.tie_rtol,
                    release_period_s=args.release_period_s, rejected_repetitions=rejected,
                    figure_style=dict(reference="exp1/First Result", font_size=args.fig_font_size,
                                      width_inches=args.fig_width, dpi=args.fig_dpi, formats=args.fig_format),
                    pipelines=[dict(prover=p["backend"], stages=p["stages"],
                                    sizes=p["n"].tolist(), excluded_sizes=p["excluded_sizes"])
                               for p in pipelines], transitions=transitions,
                    proving_phase_transitions=proving_transitions)
    (args.outdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    missing_local = not any(r["stage"] == "compute_batch" for r in rows)
    readme = f"""# Experiment 3: observed stage dominance

Source: `{args.run_dir.resolve()}`. Setup is excluded. Each prover is a separate
pipeline; verification is kept with its source backend. Successful repetitions
are aggregated with population SD. Failed/incomplete proving repetitions are
discarded together. Sizes missing any selected stage are excluded (manifest).

`01_stage_times` compares mean durations with one SD. `02_stage_dominance`
shows T_stage/max(T_stage), with stars for the largest mean(s).
`stage_metrics.csv`, `dominance.csv`, and `transitions.csv` give the underlying
values. Transitions are brackets between measured sizes, without interpolation,
confidence intervals or a claim about the first integer crossing. Pairwise
crossings below another stage's envelope are distinguished from dominant-stage
switches. The tie tolerance is numerical, not a statistical significance test.

`05_proving_vs_verify` displays proof, witness and verification separately
when the source contains split measurements. The proof curve excludes witness
calculation. Legacy fullprove stays combined and is labeled proof (fullprove);
no witness curve is inferred from that combined duration.
`proving_comparison.csv` separately compares the complete witness+proof phase
(same definition as Exp 1) with verification.
`proving_transitions.csv` contains these phase crossings. The proving SD is
computed after summing steps within each repetition, preserving covariance.
The combined phase is excluded from the decomposed stage envelope to avoid
counting both a total and its components as independent stages.

`03_derived_rates` compares N/max(T_stage), an ideal pipeline bound requiring
independent stage allocations and overlap, with N/sum(T_stage), a sequential
estimate. Shared CPU/IO work can invalidate the independent-stage assumption.
The current ZKRollup holds a lock over compute_batch, including settlement;
it does not implement the ideal pipeline. Release intervals are optional,
explicit assumptions, never inferred from setup or inclusion latency.

ZK command timings include process/Node/Docker startup. Local timers run inside
a worker after imports and fixture initialization. The worker uses real
Executor, State, Batch, Transaction and DataAvailability classes and the shared
input writer. The workload has N independent transfers and 2N accounts.
Executor logging remains part of its measured cost. Account count, compression,
logging and filesystem caching affect results. These local operations are
isolated microbenchmarks, not a live trace or the same batch as a legacy run.
Very short local CPU timings may be zero at the platform clock resolution
(notably on Windows); this does not establish absence of CPU work.

`04_resources_*` shows CPU work and available sampled RSS/IO, separately from
elapsed times. Local operation memory/IO are unavailable, not zero. Their
worker's process metrics including imports/startup remain in source steps.csv.
Docker system-scope counters are retained in CSV but omitted from resource
plots because they include other host processes. RAM is occupancy, not work
converted to seconds. No CPU, disk or network capacity is calibrated here.

Local verification is not L1 verification time. L1 submission, RPC, inclusion,
blob propagation, KZG and finality are unmeasured. Use live instrumentation or
the existing settlement benchmarks for those; inclusion latency alone does
not establish a service capacity. Synthetic SNARK and local workloads must
match, and allocations must be controlled, before testing physical resource
bottleneck predictions from the paper.

Local rollup stages absent from this dataset: **{missing_local}**.
Legacy fullprove stays explicitly combined; witness/proof are never invented.
"""
    (args.outdir / "README.md").write_text(readme, encoding="utf-8")
    print(f"Experiment 3: {args.outdir.resolve()}")
    for row in transitions:
        if row["kind"] == "dominant_stage_switch":
            print(f"  {row['prover']}: {row['stage_before']} -> {row['stage_after']} "
                  f"between N={row['N_low']} and N={row['N_high']}")


if __name__ == "__main__":
    main()

# python3 scripts/analysis/exp3.py --from bench-out/TON_NOUVEAU_RUN