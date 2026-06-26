#!/usr/bin/env python3
"""
Analyze rpmeta prediction logs against actual Copr build times.

Fetches prediction logs from the Copr backend server (via SSH), queries the
Copr API for real build durations, and produces a report showing prediction
accuracy at various "powerful builder" thresholds.

Usage:
    python misc/rpmeta_analysis.py --host root@copr-be.aws.fedoraproject.org
    python misc/rpmeta_analysis.py --log-dir /tmp/rpmeta-logs/

Dependencies: pip install click aiohttp matplotlib
"""

import asyncio
import gzip
import json
import os
import subprocess
from pathlib import Path

import aiohttp
import click
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


LOG_PATH_PREFIX = "/var/log/copr-backend/rpmeta-predictions.log"
DEFAULT_COPR_URL = "https://copr.fedorainfracloud.org"
DEFAULT_CONCURRENCY = 50
DEFAULT_THRESHOLD = 120


def fetch_logs_ssh(host):
    """Pull all rpmeta prediction log data from *host* via SSH."""
    cmd = (
        f"[ -f {LOG_PATH_PREFIX} ] && cat {LOG_PATH_PREFIX}; "
        f"for f in {LOG_PATH_PREFIX}-*.gz; do "
        f'  [ -f "$f" ] && zcat "$f"; '
        f"done"
    )
    result = subprocess.run(
        ["ssh", host, cmd],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def read_logs_local(log_dir):
    """Read prediction logs from a local directory."""
    lines = []
    log_dir = Path(log_dir)
    for path in sorted(log_dir.iterdir()):
        if path.name.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                lines.append(fh.read())
        elif "rpmeta-predictions" in path.name:
            lines.append(path.read_text(encoding="utf-8"))

    return "\n".join(lines)


def parse_predictions(raw_text):
    # Each line is a JSON object. Old logs have {build_id, prediction,
    # recommends_powerful, has_powerful_tag}. New logs additionally contain
    # {chroot, arch, package_name, package_version}. We just load whatever
    # keys are present and handle the difference downstream.
    records = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def load_cache(cache_path):
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_cache(cache_path, cache):
    if cache_path:
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)


async def fetch_build_time(session, sem, copr_url, build_id, chroot=None):
    if chroot:
        # the correct format
        url = f"{copr_url}/api_3/build-chroot/?build_id={build_id}&chrootname={chroot}"
        cache_key = f"{build_id}:{chroot}"
    else:
        # the missing data format :/
        url = f"{copr_url}/api_3/build/{build_id}"
        cache_key = str(build_id)

    async with sem:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return cache_key, None

                data = await resp.json()

        except (aiohttp.ClientError, asyncio.TimeoutError):
            return cache_key, None

    started = data.get("started_on")
    ended = data.get("ended_on")
    if started and ended:
        return cache_key, (ended - started) / 60.0

    return cache_key, None


async def fetch_all_build_times(predictions, copr_url, concurrency, cache_path):
    cache = load_cache(cache_path)
    sem = asyncio.Semaphore(concurrency)

    to_fetch = []
    for rec in predictions:
        bid = rec["build_id"]
        chroot = rec.get("chroot")
        cache_key = f"{bid}:{chroot}" if chroot else str(bid)
        if cache_key not in cache:
            to_fetch.append((bid, chroot))

    if to_fetch:
        click.echo(
            f"Fetching {len(to_fetch)} build times from {copr_url} "
            f"(concurrency={concurrency})..."
        )
        connector = aiohttp.TCPConnector(limit=concurrency)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = [
                fetch_build_time(session, sem, copr_url, bid, chroot)
                for bid, chroot in to_fetch
            ]
            done = 0
            total = len(tasks)
            for coro in asyncio.as_completed(tasks):
                key, minutes = await coro
                if minutes is not None:
                    cache[key] = minutes

                done += 1
                if done % 500 == 0 or done == total:
                    click.echo(f"  {done}/{total}")

        save_cache(cache_path, cache)
    else:
        click.echo("All build times already cached.")

    results = []
    for rec in predictions:
        bid = rec["build_id"]
        chroot = rec.get("chroot")
        cache_key = f"{bid}:{chroot}" if chroot else str(bid)
        actual = cache.get(cache_key)
        results.append({**rec, "actual_minutes": actual})

    return results


def classify(predicted, actual, threshold):
    pred_positive = predicted >= threshold
    actual_positive = actual >= threshold
    if pred_positive and actual_positive:
        return "TP"
    if pred_positive and not actual_positive:
        return "FP"
    if not pred_positive and not actual_positive:
        return "TN"
    return "FN"


def compute_metrics(data, threshold):
    tp = fp = tn = fn = 0
    for rec in data:
        cat = classify(rec["prediction"], rec["actual_minutes"], threshold)
        if cat == "TP":
            tp += 1
        elif cat == "FP":
            fp += 1
        elif cat == "TN":
            tn += 1
        else:
            fn += 1

    total = tp + fp + tn + fn
    accuracy = (tp + tn) / total if total else 0
    precision = tp / (tp + fp) if (tp + fp) else 0
    recall = tp / (tp + fn) if (tp + fn) else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0
    return {
        "threshold": threshold,
        "TP": tp,
        "FP": fp,
        "TN": tn,
        "FN": fn,
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def threshold_sweep(data, step=10, max_threshold=240):
    thresholds = list(range(step, max_threshold + 1, step))
    return [compute_metrics(data, t) for t in thresholds]


def print_report(data, current_threshold):
    click.echo("\n" + "=" * 72)
    click.echo(f"  rpmeta prediction analysis  --  {len(data)} builds with data")
    click.echo("=" * 72)

    predictions = [r["prediction"] for r in data]
    actuals = [r["actual_minutes"] for r in data]
    click.echo(
        f"\n  Predicted range : {min(predictions):.0f} - {max(predictions):.0f} min"
    )
    click.echo(f"  Actual range    : {min(actuals):.0f} - {max(actuals):.0f} min")

    m = compute_metrics(data, current_threshold)
    click.echo(f"\n--- Current threshold: {current_threshold} min ---")
    click.echo(f"  TP={m['TP']:5d}  FP={m['FP']:5d}")
    click.echo(f"  FN={m['FN']:5d}  TN={m['TN']:5d}")
    click.echo(f"  Accuracy  = {m['accuracy']:.4f}")
    click.echo(f"  Precision = {m['precision']:.4f}")
    click.echo(f"  Recall    = {m['recall']:.4f}")
    click.echo(f"  F1        = {m['f1']:.4f}")

    sweep = threshold_sweep(data)
    click.echo(
        f"\n{'Thresh':>6} {'TP':>6} {'FP':>6} {'TN':>6} {'FN':>6}"
        f" {'Acc':>7} {'Prec':>7} {'Recall':>7} {'F1':>7}"
    )
    click.echo("-" * 72)
    for s in sweep:
        click.echo(
            f"{s['threshold']:>6d} {s['TP']:>6d} {s['FP']:>6d}"
            f" {s['TN']:>6d} {s['FN']:>6d}"
            f" {s['accuracy']:>7.4f} {s['precision']:>7.4f}"
            f" {s['recall']:>7.4f} {s['f1']:>7.4f}"
        )

    best = max(sweep, key=lambda s: s["f1"])
    click.echo(f"\n  Best F1 = {best['f1']:.4f} at threshold = {best['threshold']} min")

    return sweep


def make_plots(data, sweep, current_threshold, output_dir):
    os.makedirs(output_dir, exist_ok=True)

    predictions = [r["prediction"] for r in data]
    actuals = [r["actual_minutes"] for r in data]

    fig, ax = plt.subplots(figsize=(8, 8))
    ax.scatter(actuals, predictions, alpha=0.3, s=8)
    lim = max(max(actuals), max(predictions)) * 1.05
    ax.plot([0, lim], [0, lim], "r--", linewidth=1, label="perfect")
    ax.axhline(
        current_threshold,
        color="orange",
        linestyle=":",
        label=f"threshold={current_threshold}",
    )
    ax.axvline(current_threshold, color="orange", linestyle=":")
    ax.set_xlabel("Actual build time (min)")
    ax.set_ylabel("Predicted build time (min)")
    ax.set_title("rpmeta: Predicted vs Actual Build Time")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, "scatter_predicted_vs_actual.png"), dpi=150)
    plt.close(fig)

    thresholds = [s["threshold"] for s in sweep]
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(thresholds, [s["precision"] for s in sweep], "o-", label="Precision")
    ax.plot(thresholds, [s["recall"] for s in sweep], "s-", label="Recall")
    ax.plot(thresholds, [s["f1"] for s in sweep], "^-", label="F1")
    ax.axvline(
        current_threshold,
        color="gray",
        linestyle="--",
        label=f"current={current_threshold}",
    )
    ax.set_xlabel("Threshold (min)")
    ax.set_ylabel("Score")
    ax.set_title("Precision / Recall / F1 vs Threshold")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, "threshold_sweep.png"), dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.hist(actuals, bins=80, edgecolor="black", alpha=0.7)
    ax.axvline(
        current_threshold,
        color="red",
        linestyle="--",
        label=f"threshold={current_threshold}",
    )
    best = max(sweep, key=lambda s: s["f1"])
    if best["threshold"] != current_threshold:
        ax.axvline(
            best["threshold"],
            color="green",
            linestyle="--",
            label=f"best F1={best['threshold']}",
        )
    ax.set_xlabel("Actual build time (min)")
    ax.set_ylabel("Count")
    ax.set_title("Distribution of Actual Build Times")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, "actual_time_histogram.png"), dpi=150)
    plt.close(fig)

    m = compute_metrics(data, current_threshold)
    matrix = [[m["TP"], m["FN"]], [m["FP"], m["TN"]]]
    fig, ax = plt.subplots(figsize=(5, 5))
    im = ax.imshow(matrix, cmap="Blues")
    labels = [["TP", "FN"], ["FP", "TN"]]
    for i in range(2):
        for j in range(2):
            ax.text(
                j,
                i,
                f"{labels[i][j]}\n{matrix[i][j]}",
                ha="center",
                va="center",
                fontsize=14,
            )
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["Predicted +", "Predicted -"])
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["Actual +", "Actual -"])
    ax.set_title(f"Confusion Matrix (threshold={current_threshold} min)")
    fig.colorbar(im)
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, "confusion_matrix.png"), dpi=150)
    plt.close(fig)

    click.echo(f"\nPlots saved to {output_dir}/")


@click.command()
@click.option(
    "--host",
    default=None,
    help="SSH host to pull logs from (e.g. copr-be.aws.fedoraproject.org)",
)
@click.option(
    "--log-dir",
    default=None,
    type=click.Path(exists=True),
    help="Local directory with already-downloaded log files",
)
@click.option(
    "--copr-url", default=DEFAULT_COPR_URL, show_default=True, help="Copr instance URL"
)
@click.option(
    "--concurrency",
    default=DEFAULT_CONCURRENCY,
    show_default=True,
    help="Max parallel API requests",
)
@click.option(
    "--cache-file",
    default="rpmeta-build-cache.json",
    show_default=True,
    help="Path to build data cache",
)
@click.option(
    "--output-dir",
    default="rpmeta-analysis-output",
    show_default=True,
    help="Directory for plots",
)
@click.option(
    "--current-threshold",
    default=DEFAULT_THRESHOLD,
    show_default=True,
    help="Currently configured threshold in minutes",
)
def main(
    host, log_dir, copr_url, concurrency, cache_file, output_dir, current_threshold
):
    """Analyze rpmeta prediction logs against actual Copr build times."""
    if not host and not log_dir:
        raise click.UsageError("Either --host or --log-dir is required.")

    if host:
        click.echo(f"Fetching logs from {host}...")
        raw = fetch_logs_ssh(host)
    else:
        click.echo(f"Reading logs from {log_dir}...")
        raw = read_logs_local(log_dir)

    predictions = parse_predictions(raw)
    click.echo(f"Parsed {len(predictions)} prediction records.")
    if not predictions:
        raise click.ClickException("No prediction records found.")

    # Old log format lacks "chroot", so multiple chroots of the same build
    # produce duplicate entries with identical build_id and no way to tell
    # which chroot each prediction belonged to. We keep only the first
    # occurrence per (build_id, chroot) -- for old data that means one entry
    # per build, matched against the overall build wall-clock time (imprecise).
    # New logs include "chroot", so each entry is unique and gets matched
    # against per-chroot timing from the API which is precise.
    seen = set()
    unique = []
    for rec in predictions:
        key = (rec["build_id"], rec.get("chroot"))
        if key not in seen:
            seen.add(key)
            unique.append(rec)

    predictions = unique
    click.echo(f"After dedup: {len(predictions)} unique prediction entries.")

    enriched = asyncio.run(
        fetch_all_build_times(predictions, copr_url, concurrency, cache_file)
    )

    with_data = [r for r in enriched if r["actual_minutes"] is not None]
    without_data = len(enriched) - len(with_data)
    click.echo(
        f"Builds with timing data: {len(with_data)}, " f"without (null): {without_data}"
    )

    if not with_data:
        raise click.ClickException("No builds with actual timing data available.")

    sweep = print_report(with_data, current_threshold)
    make_plots(with_data, sweep, current_threshold, output_dir)


if __name__ == "__main__":
    main()
