"""Headless image and machine-readable statistics reports."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from .analysis import METRICS


def save_report(result, output, formats=("png",)):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "statistics.json").write_text(json.dumps(result, indent=2))
    with (output / "summary.csv").open("w", newline="") as f:
        fields = ["topic", "type", "metric", "count", "mean_ns", "std_ns", "min_ns", "max_ns", "negative", "zero"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for topic in result["topics"]:
            for metric, stats in topic["metrics"].items():
                writer.writerow({"topic": topic["topic"], "type": topic["type"], "metric": metric,
                                 **{k: stats[k] for k in fields[3:]}})
    if not formats:
        return
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise ValueError('Image export requires: pip install "rosbag-timeline[stats]"') from exc
    for topic in result["topics"]:
        fig, axs = plt.subplots(2, 3, figsize=(15, 8), layout="constrained")
        fig.suptitle(f"{topic['topic']}  ·  {topic['message_count']:,} messages\n{topic['type']}", fontsize=14)
        for ax, (metric, label) in zip(axs.flat, METRICS.items()):
            stats = topic["metrics"][metric]
            hist = stats["histogram"]
            ax.set_title(label, fontsize=11)
            ax.set_xlabel("milliseconds")
            ax.set_ylabel("messages / pairs")
            ax.grid(axis="y", alpha=0.2)
            if stats["count"]:
                edges = [int(e) / 1e6 for e in hist["edges_ns"]]
                widths = [b - a for a, b in zip(edges, edges[1:])]
                if stats["min_ns"] == stats["max_ns"]:
                    center = int(stats["min_ns"]) / 1e6
                    pad = max(abs(center) * .05, .001)
                    ax.vlines(center, 0, stats["count"], color="#257b8a", linewidth=3)
                    ax.set_xlim(center-pad, center+pad)
                    ax.set_ylim(0, stats["count"] * 1.12)
                else:
                    ax.bar(edges[:-1], hist["counts"], width=widths, align="edge", color="#257b8a", linewidth=0)
                ax.text(0.98, 0.96, f"n={stats['count']:,}\nmean {stats['mean_ns']/1e6:.4g} ms\nσ {stats['std_ns']/1e6:.4g} ms\nnegative {stats['negative']:,}",
                        ha="right", va="top", transform=ax.transAxes, fontsize=9,
                        bbox={"facecolor": "white", "alpha": .85, "edgecolor": "none", "pad": 3})
            else:
                ax.text(0.5, 0.5, "No valid timestamp pairs", ha="center", transform=ax.transAxes)
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", topic["topic"]).strip("_")[:70] or "topic"
        digest = hashlib.sha256((topic["topic"] + topic["type"]).encode()).hexdigest()[:8]
        for fmt in formats:
            fig.savefig(output / f"{name}-{digest}.{fmt}", dpi=150)
        plt.close(fig)
