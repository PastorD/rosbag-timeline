"""Persistent, lazily populated timing index and bounded-memory statistics."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from pathlib import Path

from .source import Source
from .stamps import StampExtractor

METRICS = {
    "publish_stamp": "Publish − stamp",
    "record_stamp": "Record − stamp",
    "record_publish": "Record − publish",
    "stamp_period": "Stamp inter-message period",
    "publish_period": "Publish inter-message period",
    "record_period": "Record inter-message period",
}


def fuzzy(query, text):
    text = text.casefold()
    for word in query.casefold().split():
        chars = iter(text)
        if not all(any(c == x for x in chars) for c in word):
            return False
    return True


class Moment:
    def __init__(self):
        self.n = 0
        self.mean = self.m2 = 0.0
        self.low = self.high = None
        self.negative = self.zero = 0

    def add(self, x):
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self.m2 += d * (x - self.mean)
        self.low = x if self.low is None else min(self.low, x)
        self.high = x if self.high is None else max(self.high, x)
        self.negative += x < 0
        self.zero += x == 0

    def result(self):
        return {"count": self.n, "mean_ns": self.mean if self.n else None,
                "std_ns": math.sqrt(max(0, self.m2 / self.n)) if self.n else None,
                "min_ns": str(self.low) if self.n else None,
                "max_ns": str(self.high) if self.n else None,
                "negative": self.negative, "zero": self.zero}


class Analysis:
    def __init__(self, path, cache_dir=None, stamps=None, nodes=None,
                 include_zero=False, max_chunk_mb=512, validate_crcs=False):
        self.source = Source(path, max_chunk_mb, validate_crcs)
        self.stamps = stamps or {}
        self.nodes = nodes or {}
        self.include_zero = include_zero
        self.recorder = self.nodes.get("recorder", "/bag_recorder")
        self.lock = threading.RLock()
        self.progress = {"done": 0, "total": 0, "active": False}
        self.topics = {}
        self.extractors = {}
        self.cid_to_tid = {}
        topic_lookup = {}
        for key in self.source.channel_keys:
            channel = self.source.channels[key]
            schema = self.source.schemas.get((key[0], channel.schema_id))
            cid = self.source.cid[key]
            extractor = StampExtractor.build(schema, self.stamps.get(channel.topic))
            self.extractors[cid] = extractor
            identity = (channel.topic, schema.name if schema else "(no schema)")
            tid = topic_lookup.setdefault(identity, len(topic_lookup) + 1)
            self.cid_to_tid[cid] = tid
            topic = self.topics.setdefault(tid, {"id": tid, "name": identity[0], "type": identity[1],
                                                 "channels": [], "stamps": [], "warnings": [],
                                                 "candidates": [], "publishers": []})
            topic["channels"].append(cid)
            topic["stamps"].append(extractor.path)
            topic["candidates"] += extractor.candidates
            if extractor.error:
                topic["warnings"].append(extractor.error)
            declared = self.nodes.get("topics", {}).get(channel.topic, {})
            publishers = declared.get("publishers", [])
            if isinstance(publishers, str):
                publishers = [publishers]
            # Only an explicit convention, not an assumption about ROS bag metadata.
            custom = self.nodes.get("channels", {}).get(f"{key[0]}:{key[1]}", {}).get("publisher")
            metadata_publisher = channel.metadata.get("rosbag_timeline.publisher")
            topic["publishers"] += [custom or metadata_publisher] if custom or metadata_publisher else publishers
        for topic in self.topics.values():
            for key in ("stamps", "candidates", "warnings", "publishers"):
                topic[key] = list(dict.fromkeys(topic[key]))
            pubs = topic["publishers"]
            topic["publisher"] = pubs[0] if len(pubs) == 1 else ("? multiple: " if pubs else "? unknown: ") + topic["name"]
        fingerprint = hashlib.sha256()
        fingerprint.update(json.dumps({"version": 2, "stamps": self.stamps,
                                       "validate_crcs": validate_crcs}, sort_keys=True).encode())
        for path in self.source.paths:
            st = path.stat()
            fingerprint.update(f"{path}:{st.st_size}:{st.st_mtime_ns}".encode())
            with path.open("rb") as f:
                fingerprint.update(f.read(65536))
                f.seek(max(0, st.st_size - 65536))
                fingerprint.update(f.read(65536))
        folder = Path(cache_dir) if cache_dir else Path.home() / ".cache" / "rosbag-timeline"
        folder.mkdir(parents=True, exist_ok=True)
        self.cache = folder / (fingerprint.hexdigest()[:24] + ".sqlite3")
        self.db = sqlite3.connect(self.cache, check_same_thread=False, timeout=60)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA cache_size=-32768")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
          id TEXT PRIMARY KEY, cid INTEGER, tid INTEGER, log INTEGER, pub INTEGER,
          stamp INTEGER, status TEXT, sequence INTEGER, fid INTEGER, unit INTEGER, off INTEGER);
        CREATE INDEX IF NOT EXISTS message_time ON messages(tid, log, fid, unit, off);
        CREATE TABLE IF NOT EXISTS coverage (fid INTEGER, unit INTEGER, cid INTEGER,
          PRIMARY KEY(fid, unit, cid)) WITHOUT ROWID;
        """)

    def close(self):
        self.db.close()

    def catalog(self):
        return {"files": [p.name for p in self.source.paths], "start_ns": str(self.source.start),
                "end_ns": str(self.source.end), "topics": list(self.topics.values()),
                "recorder": self.recorder, "metrics": METRICS, "include_zero_stamps": self.include_zero,
                "warnings": self.source.warnings + [
                    "Publish time is the MCAP field, not necessarily ROS-clock time. Equal publish/record times may be a recorder fallback.",
                    "Time filters select by recording time. Stamp and publish endpoints may lie outside the selected range.",
                    "Node arrows end at the bag recorder. A graph snapshot cannot recover other subscribers' receive times."]}

    def select(self, tids=None, query="", node=""):
        selected = []
        for tid, t in self.topics.items():
            if tids is not None and tid not in tids:
                continue
            if not fuzzy(query, t["name"] + " " + t["type"]):
                continue
            if not fuzzy(node, " ".join(t["publishers"] + [t["publisher"], self.recorder])):
                continue
            selected.append(tid)
        return selected

    def ensure(self, start, end, tids):
        if not self.source.unchanged():
            raise ValueError("bag changed while open; close the viewer and reopen the finalized bag")
        wanted_cids = {c for t in tids for c in self.topics[t]["channels"]}
        units = [u for u in self.source.units if u.end >= start and u.start < end]
        self.progress = {"done": 0, "total": len(units), "active": True}
        try:
            for n, unit in enumerate(units):
                channels = unit.channels if unit.channels is not None else {k[1] for k in self.source.channel_keys if k[0] == unit.file}
                local = {c for c in channels if self.source.cid.get((unit.file, c)) in wanted_cids}
                covered = {r[0] for r in self.db.execute("SELECT cid FROM coverage WHERE fid=? AND unit=?", (unit.file, unit.offset))}
                local = {c for c in local if self.source.cid[(unit.file, c)] not in covered}
                if local:
                    with self.db:
                        batch = []
                        for row in self.source.timing_rows(unit, local, self.extractors):
                            key, cid, log, pub, stamp, status, sequence = row
                            off = int(key.rsplit(":", 1)[1])
                            batch.append((key, cid, self.cid_to_tid[cid], log, pub, stamp, status, sequence, unit.file, unit.offset, off))
                            if len(batch) >= 4096:
                                self.db.executemany("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
                                batch.clear()
                        self.db.executemany("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch)
                        self.db.executemany("INSERT OR IGNORE INTO coverage VALUES (?,?,?)", [(unit.file, unit.offset, self.source.cid[(unit.file, c)]) for c in local])
                self.progress["done"] = n + 1
        finally:
            self.progress["active"] = False

    def stamp_sql(self):
        return "stamp" if self.include_zero else "NULLIF(stamp,0)"

    def base_sql(self, start, end, tids, metric):
        if metric not in METRICS:
            raise ValueError("unknown timing metric")
        where = "log>=? AND log<? AND tid IN (" + ",".join("?" for _ in tids) + ")"
        args = [start, end, *tids]
        s = self.stamp_sql()
        p = "NULLIF(pub,0)"
        expression = {"publish_stamp": f"{p}-{s}", "record_stamp": f"log-{s}",
                      "record_publish": f"log-{p}"}.get(metric)
        if expression:
            sql = f"SELECT *, ({expression}) AS value FROM messages WHERE {where}"
        else:
            source = {"stamp_period": s, "publish_period": p, "record_period": "log"}[metric]
            sql = f"SELECT *, ({source}-LAG({source}) OVER (PARTITION BY tid ORDER BY log,fid,unit,off)) AS value FROM messages WHERE {where}"
        return sql, args

    def view(self, start, end, tids, metric="record_stamp", selection=None, limit=20000, bins=600, selection_topic=None):
        with self.lock:
            self.ensure(start, end, tids)
            base, args = self.base_sql(start, end, tids, metric)
            cte = "WITH selected AS (" + base + ") "
            n = self.db.execute(cte + "SELECT COUNT(*) FROM selected", args).fetchone()[0]
            selected_expr = "0"
            if selection is not None:
                lo, hi = map(int, selection)
                if hi <= lo:
                    raise ValueError("invalid histogram selection")
                selected_expr = "(value>=? AND value<?)"
                extra = [lo, hi]
                if selection_topic is not None:
                    selected_expr = "(tid=? AND value>=? AND value<?)"
                    extra.insert(0, int(selection_topic))
            else:
                extra = []
            highlighted = self.db.execute(cte + f"SELECT COALESCE(SUM({selected_expr}),0) FROM selected", args + extra).fetchone()[0]
            out = {"count": n, "highlighted": highlighted, "origin_ns": str(start),
                   "mode": "events" if n <= limit else "density", "events": [], "density": []}
            if n <= limit:
                sql = cte + f"SELECT id,tid,log,pub,stamp,status,sequence,value,{selected_expr} FROM selected ORDER BY log,fid,unit,off"
                for key, tid, log, pub, stamp, status, seq, value, hi in self.db.execute(sql, args + extra):
                    # Absolute ns remain decimal strings. JS subtracts with BigInt before plotting.
                    out["events"].append({"id": key, "topic": tid, "log": str(log), "publish": str(pub),
                                           "stamp": str(stamp) if stamp is not None else None,
                                           "status": status, "sequence": seq, "value": str(value) if value is not None else None,
                                           "highlight": bool(hi)})
            else:
                bins = max(1, min(bins, max(1, 12000 // max(1, len(tids)))))
                width = max(1, (end - start + bins - 1) // bins)
                s = self.stamp_sql()
                sql = cte + f"""SELECT tid, (log-?)/? AS bin, COUNT(*), MIN(log), MAX(log),
                  MIN({s}), MAX({s}), MIN(NULLIF(pub,0)), MAX(NULLIF(pub,0)), COALESCE(SUM({selected_expr}),0)
                  FROM selected GROUP BY tid,bin ORDER BY tid,bin"""
                for row in self.db.execute(sql, args + [start, width] + extra):
                    tid, bucket, count, logmin, logmax, smin, smax, pmin, pmax, hi = row
                    out["density"].append({"topic": tid, "bin": bucket, "count": count, "highlighted": hi,
                        "log_min": str(logmin), "log_max": str(logmax),
                        "stamp_min": str(smin) if smin is not None else None,
                        "stamp_max": str(smax) if smax is not None else None,
                        "publish_min": str(pmin) if pmin is not None else None,
                        "publish_max": str(pmax) if pmax is not None else None})
            return out

    def iter_metrics(self, start, end, tid):
        previous = {"stamp": None, "publish": None, "record": None}
        for log, pub, stamp, status in self.db.execute("SELECT log,pub,stamp,status FROM messages WHERE tid=? AND log>=? AND log<? ORDER BY log,fid,unit,off", (tid, start, end)):
            s = stamp if self.include_zero or stamp != 0 else None
            p = pub if pub != 0 else None
            values = {"record_stamp": log - s if s is not None else None,
                      "publish_stamp": p - s if p is not None and s is not None else None,
                      "record_publish": log - p if p is not None else None}
            for key, current in (("stamp", s), ("publish", p), ("record", log)):
                prev = previous[key]
                values[key + "_period"] = current - prev if current is not None and prev is not None else None
                previous[key] = current
            yield values

    def statistics(self, start, end, tids, bins=80):
        bins = max(1, min(int(bins), 1000))
        with self.lock:
            self.ensure(start, end, tids)
            result = {"range": {"start_ns": str(start), "end_ns": str(end), "basis": "recording time; end exclusive"},
                      "include_zero_stamps": self.include_zero, "topics": [],
                      "notes": ["All selected messages are used. Population standard deviation; no sampling.",
                                "Periods use consecutive messages in recording order, within this selection; a missing/invalid timestamp breaks the pair.",
                                "Equal publish/record times are preserved but do not prove zero transport delay."]}
            for tid in tids:
                moments = {m: Moment() for m in METRICS}
                for values in self.iter_metrics(start, end, tid):
                    for key, value in values.items():
                        if value is not None:
                            moments[key].add(value)
                metrics = {}
                for key, moment in moments.items():
                    item = moment.result()
                    if moment.n:
                        width = max(1, (moment.high - moment.low + 1 + bins - 1) // bins)
                        actual = min(bins, (moment.high - moment.low) // width + 1)
                        edges = [moment.low + i * width for i in range(actual + 1)]
                        item["histogram"] = {"edges_ns": [str(x) for x in edges], "counts": [0] * actual}
                    else:
                        item["histogram"] = {"edges_ns": [], "counts": []}
                    metrics[key] = item
                for values in self.iter_metrics(start, end, tid):
                    for key, value in values.items():
                        if value is not None:
                            m = moments[key]
                            hist = metrics[key]["histogram"]
                            width = int(hist["edges_ns"][1]) - m.low
                            hist["counts"][(value - m.low) // width] += 1
                statuses = dict(self.db.execute("SELECT status,COUNT(*) FROM messages WHERE tid=? AND log>=? AND log<? GROUP BY status", (tid, start, end)))
                equal, missing_pub = self.db.execute("SELECT COALESCE(SUM(pub=log),0),COALESCE(SUM(pub=0),0) FROM messages WHERE tid=? AND log>=? AND log<?", (tid, start, end)).fetchone()
                result["topics"].append({"id": tid, "topic": self.topics[tid]["name"], "type": self.topics[tid]["type"],
                    "message_count": sum(statuses.values()), "stamp_status": statuses,
                    "publish_equals_record": equal, "publish_missing_zero": missing_pub, "metrics": metrics})
            return result
