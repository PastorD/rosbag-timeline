"""Command line entry point."""
import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import __version__
from .analysis import Analysis


def parser():
    p = argparse.ArgumentParser(prog="rosbag-timeline", description="Inspect MCAP message stamps, publish times, and recording times without ROS.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("view", "info", "index", "stats"):
        cmd = sub.add_parser(name)
        cmd.add_argument("bag", help="MCAP file or directory of split MCAP files")
        cmd.add_argument("--cache-dir", help="disk timing cache (default ~/.cache/rosbag-timeline)")
        cmd.add_argument("--stamp", action="append", default=[], metavar="TOPIC=PATH", help="stamp override, e.g. /tf=transforms[0].header.stamp; use none to disable")
        cmd.add_argument("--nodes", type=Path, help="JSON graph snapshot or explicit topic mapping")
        cmd.add_argument("--include-zero-stamps", action="store_true", help="treat a zero stamp as valid (e.g. simulation epoch)")
        cmd.add_argument("--max-chunk-mb", type=int, default=512)
        cmd.add_argument("--validate-crcs", action="store_true", help="validate chunk CRCs (touches all chunk bytes)")
        if name in ("index", "stats"):
            cmd.add_argument("--start", default="0", help="seconds after earliest bag recording time; inclusive")
            cmd.add_argument("--end", help="seconds after earliest bag recording time; exclusive")
            cmd.add_argument("--topic", action="append", help="exact topic name; repeat to select multiple")
            cmd.add_argument("--search", default="", help="fuzzy subsequence search anywhere in topic name/type")
            cmd.add_argument("--node", default="", help="fuzzy publisher/recorder node search")
        if name == "view":
            cmd.add_argument("--port", type=int, default=8765)
            cmd.add_argument("--no-browser", action="store_true")
        if name == "stats":
            cmd.add_argument("-o", "--output", type=Path, required=True)
            cmd.add_argument("--bins", type=int, default=80)
            cmd.add_argument("--format", choices=["png", "svg", "pdf"], action="append", dest="formats")
            cmd.add_argument("--json-only", action="store_true", help="JSON + CSV only; do not generate images")
    demo = sub.add_parser("demo", help="generate a deterministic MCAP and node-map example")
    demo.add_argument("output", type=Path)
    demo.add_argument("--messages", type=int, default=20000)
    demo.add_argument("--payload-bytes", type=int, default=0, help="point-cloud payload size per lidar message")
    demo.add_argument("--compression", choices=["none", "zstd", "lz4"], default="zstd")
    graph = sub.add_parser("capture-graph", help="optional ROS-side node/endpoint snapshot")
    graph.add_argument("output", type=Path)
    graph.add_argument("--wait", type=float, default=3.0)
    graph.add_argument("--recorder", default="/bag_recorder")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.command == "demo":
            from .demo import generate
            if args.messages < 1 or args.payload_bytes < 0:
                raise ValueError("messages must be positive; payload bytes must be nonnegative")
            generate(args.output, args.messages, args.payload_bytes, args.compression)
            print(f"Created {args.output} and {args.output.with_suffix('.nodes.json')}")
            return
        if args.command == "capture-graph":
            from .graph import capture
            capture(args.output, args.wait, args.recorder)
            print(f"Saved {args.output}")
            return
        stamps = {}
        for item in args.stamp:
            topic, sep, field = item.partition("=")
            if not sep or not topic or not field:
                raise ValueError("--stamp must be TOPIC=FIELD.PATH")
            stamps[topic] = field
        nodes = json.loads(args.nodes.read_text()) if args.nodes else None
        a = Analysis(args.bag, args.cache_dir, stamps, nodes, args.include_zero_stamps, args.max_chunk_mb, args.validate_crcs)
        try:
            if args.command == "view":
                from .server import serve
                serve(a, args.port, not args.no_browser)
            elif args.command == "info":
                print(json.dumps(a.catalog(), indent=2))
            else:
                start = a.source.start + int(Decimal(args.start) * 1_000_000_000)
                end = a.source.start + int(Decimal(args.end) * 1_000_000_000) if args.end else a.source.end
                if not 0 <= start < end <= 2**63 - 1:
                    raise ValueError("time window must satisfy 0 <= start < end within signed int64 nanoseconds")
                tids = a.select(query=args.search, node=args.node)
                if args.topic:
                    unknown = set(args.topic) - {t["name"] for t in a.topics.values()}
                    if unknown:
                        raise ValueError("unknown topics: " + ", ".join(sorted(unknown)))
                    tids = [t for t in tids if a.topics[t]["name"] in args.topic]
                if not tids:
                    raise ValueError("no topics match these filters")
                if args.command == "index":
                    a.ensure(start, end, tids)
                    count = a.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                    print(f"Timing index contains {count:,} messages: {a.cache}")
                else:
                    from .report import save_report
                    result = a.statistics(start, end, tids, args.bins)
                    save_report(result, args.output, () if args.json_only else args.formats or ("png",))
                    print(f"Statistics for {len(result['topics'])} topic(s) saved to {args.output}")
        finally:
            a.close()
    except (ValueError, OSError, InvalidOperation) as exc:
        p.exit(2, f"error: {exc}\n")
