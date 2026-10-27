"""Reproducible benchmark, separate from unit tests. Uses a synthetic bag.

python -m rosbag_timeline demo million.mcap --messages 1000000
python tests/benchmark.py million.mcap bench-cache > benchmark.json
"""
import json
import resource
import sys
import time
from pathlib import Path
from rosbag_timeline.analysis import Analysis

result = {"bag": Path(sys.argv[1]).name, "bag_bytes": Path(sys.argv[1]).stat().st_size,
          "description": "Synthetic MCAP, three topics, one million small messages; timings are machine-specific."}
t = time.perf_counter()
a = Analysis(sys.argv[1], sys.argv[2])
result["catalog_seconds"] = time.perf_counter() - t
result["chunk_count"] = len(a.source.units)
start, end = a.source.start, a.source.end
t = time.perf_counter()
partial = a.view(start, start + 10**9, [1])
result["cold_one_second_one_topic_seconds"] = time.perf_counter() - t
result["partial_messages"] = partial["count"]
result["chunks_read_for_partial"] = a.source.units_read
t = time.perf_counter()
a.ensure(start, end, list(a.topics))
result["remaining_index_seconds"] = time.perf_counter() - t
result["indexed_messages"] = a.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
t = time.perf_counter()
view = a.view(start, end, list(a.topics))
result["warm_full_density_seconds"] = time.perf_counter() - t
result["density_bin_count"] = len(view["density"])
result["density_total"] = sum(b["count"] for b in view["density"])
t = time.perf_counter()
stats = a.statistics(start, end, list(a.topics))
result["six_metrics_two_pass_statistics_seconds"] = time.perf_counter() - t
result["stats_messages"] = sum(t["message_count"] for t in stats["topics"])
a.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
result["cache_bytes"] = a.cache.stat().st_size
result["peak_process_rss_mib_linux"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
a.close()
assert result["indexed_messages"] == result["density_total"] == result["stats_messages"]
print(json.dumps(result, indent=2))
