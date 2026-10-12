"""Time-weighted process-tree RSS statistics; no third-party dependencies.

Each observation represents the interval up to the midpoints of its neighbours,
clipped to the command window. Quantiles use the inverse weighted empirical CDF.
The same intervals can be concatenated across sequential steps of a phase.
"""
import math

MEMORY_METRICS = (
    "rss_min_bytes", "rss_mean_bytes", "rss_median_bytes", "rss_p05_bytes",
    "rss_p25_bytes", "rss_p75_bytes", "rss_p95_bytes", "rss_p99_bytes",
    "rss_stdev_bytes", "rss_integral_byte_s", "rss_sampled_peak_bytes",
)


def summarize_memory(intervals):
    """Summarize (RSS bytes, duration seconds) cells, including observed zeros."""
    if not intervals:
        return dict.fromkeys(MEMORY_METRICS)
    if any(not math.isfinite(v) or not math.isfinite(w) or v < 0 or w <= 0
           for v, w in intervals):
        raise ValueError("RSS and interval duration must be finite and nonnegative/positive")
    duration = sum(w for _, w in intervals)
    integral = sum(v * w for v, w in intervals)
    mean = integral / duration
    ordered = sorted(intervals)

    def quantile(q):
        cumulative = 0.0
        for value, weight in ordered:
            cumulative += weight
            if cumulative >= q * duration:
                return value
        return ordered[-1][0]

    result = dict(rss_min_bytes=ordered[0][0], rss_mean_bytes=mean,
                  rss_median_bytes=quantile(.5),
                  rss_stdev_bytes=math.sqrt(sum(w * (v - mean) ** 2 for v, w in intervals) / duration),
                  rss_integral_byte_s=integral, rss_sampled_peak_bytes=ordered[-1][0])
    for p in (5, 25, 75, 95, 99):
        result[f"rss_p{p:02d}_bytes"] = quantile(p / 100)
    return result


def memory_intervals(samples, start_s, end_s):
    """Return weighted cells and sampling diagnostics; do not invent missing RAM.

    Input triples: (time since sampler start, RSS bytes, observed process count).
    Samples outside the window bracket its boundaries. No-live-process samples
    can represent startup/exit zeros, but an entirely unobserved run is missing.
    """
    points = sorted(samples)
    if any(not all(math.isfinite(v) for v in p) or p[1] < 0 or p[2] < 0 for p in points):
        raise ValueError("Invalid memory sample")
    if any(b[0] <= a[0] for a, b in zip(points, points[1:])):
        raise ValueError("Sample times must be distinct")
    quality = dict(rss_valid_samples=0, rss_observed_duration_s=0.0,
                   rss_observed_fraction=0.0, rss_max_sample_gap_s=None,
                   rss_measurement_status="unavailable")
    if end_s <= start_s or len(points) < 2:
        return [], quality
    selected = []
    gaps = [b[0] - a[0] for a, b in zip(points, points[1:])
            if b[0] > start_s and a[0] < end_s]
    quality["rss_max_sample_gap_s"] = max(gaps) if gaps else None
    for i, (t, value, count) in enumerate(points):
        left = (points[i-1][0] + t) / 2 if i else start_s
        right = (t + points[i+1][0]) / 2 if i + 1 < len(points) else end_s
        weight = min(end_s, right) - max(start_s, left)
        if weight > 0:
            selected.append((value, weight))
            if count > 0 and value > 0:
                quality["rss_valid_samples"] += 1
                quality["rss_observed_duration_s"] += weight
    quality["rss_observed_fraction"] = quality["rss_observed_duration_s"] / (end_s - start_s)
    n = quality["rss_valid_samples"]
    quality["rss_measurement_status"] = "ok" if n >= 2 else "sparse" if n else "unavailable"
    return (selected if n else []), quality
