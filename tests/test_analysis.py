import json
import struct
from pathlib import Path
import pytest
from mcap.writer import Writer, CompressionType, IndexType
from mcap.reader import make_reader
from rosbag_timeline.analysis import Analysis, fuzzy
from rosbag_timeline.demo import generate

SCHEMA = b"builtin_interfaces/Time stamp\n================================================================================\nMSG: builtin_interfaces/Time\nint32 sec\nuint32 nanosec\n"
BASE = 1_800_000_000_000_000_000


def bag(path, rows, compression=CompressionType.NONE, indexed=True, chunked=True):
    with path.open("wb") as f:
        w = Writer(f, chunk_size=140, compression=compression, index_types=IndexType.ALL if indexed else IndexType.NONE,
                   use_chunking=chunked, use_statistics=indexed)
        w.start(profile="ros2")
        s = w.register_schema("demo/msg/T", "ros2msg", SCHEMA)
        c = w.register_channel("/custom", "cdr", s)
        for log, pub, stamp in rows:
            raw = b"\x00\x01\x00\x00" + struct.pack("<iI", *divmod(stamp, 10**9))
            w.add_message(c, log, raw, pub)
        w.finish()


@pytest.mark.parametrize("compression", list(CompressionType))
@pytest.mark.parametrize("indexed,chunked", [(True, True), (False, True), (True, False), (False, False)])
def test_compression_and_unindexed(tmp_path, compression, indexed, chunked):
    path = tmp_path / "bag.mcap"
    rows = [(BASE + 100 + i * 100, BASE + 90 + i * 100, BASE + 70 + i * 100) for i in range(20)]
    bag(path, rows, compression, indexed, chunked)
    a = Analysis(path, tmp_path / "cache", validate_crcs=True)
    view = a.view(BASE, BASE + 10000, [1])
    assert view["count"] == 20
    assert [(int(e["log"]), int(e["publish"]), int(e["stamp"])) for e in view["events"]] == rows
    a.close()


def test_exact_statistics_and_histogram_selection(tmp_path):
    path = tmp_path / "bag.mcap"
    rows = [(BASE+100,BASE+90,BASE+70), (BASE+200,BASE+180,BASE+220), (BASE+300,BASE+300,0), (BASE+400,0,BASE+370)]
    bag(path, rows)
    a = Analysis(path, tmp_path / "cache")
    stats = a.statistics(BASE, BASE+1000, [1], bins=8)["topics"][0]
    m = stats["metrics"]
    assert m["record_stamp"]["count"] == 3
    assert m["record_stamp"]["mean_ns"] == pytest.approx(40/3)
    assert m["record_stamp"]["negative"] == 1
    assert m["publish_stamp"]["mean_ns"] == -10
    assert m["record_period"]["mean_ns"] == 100
    assert m["stamp_period"]["count"] == 1  # invalid stamp breaks a pair
    assert m["stamp_period"]["mean_ns"] == 150
    assert m["publish_period"]["count"] == 2
    assert stats["publish_equals_record"] == 1
    assert stats["publish_missing_zero"] == 1
    assert stats["stamp_status"]["zero_stamp"] == 1
    assert sum(m["record_stamp"]["histogram"]["counts"]) == 3
    v = a.view(BASE, BASE+1000, [1], selection=[-20,0], selection_topic=1)
    assert v["highlighted"] == 1
    assert [e["log"] for e in v["events"] if e["highlight"]] == [str(BASE+200)]
    p = a.view(BASE, BASE+1000, [1], metric="stamp_period", selection=[150,151])
    assert p["highlighted"] == 1
    a.close()


def test_laziness_cache_and_density(tmp_path):
    path = generate(tmp_path / "demo.mcap", 15000, chunk_size=8000)
    a = Analysis(path, tmp_path / "cache")
    assert a.source.units_read == 0
    start = a.source.start
    a.view(start, start+100000000, [1])
    first_read = a.source.units_read
    assert 0 < first_read < len(a.source.units)
    assert {r[0] for r in a.db.execute("SELECT DISTINCT tid FROM messages")} == {1}
    a.view(start, start+100000000, [1])
    assert a.source.units_read == first_read
    allview = a.view(a.source.start, a.source.end, [1,2,3], limit=20, bins=100)
    assert allview["mode"] == "density"
    assert len(allview["density"]) <= 300
    assert sum(d["count"] for d in allview["density"]) == 15000
    hi = a.view(a.source.start, a.source.end, [1,2,3], limit=1, bins=100, selection=[-10**8,0], selection_topic=1)
    assert sum(d["highlighted"] for d in hi["density"]) == hi["highlighted"] > 0
    assert not any(d["highlighted"] for d in hi["density"] if d["topic"] != 1)
    a.close()
    b = Analysis(path, tmp_path / "cache")
    b.view(start, start+100000000, [1])
    assert b.source.units_read == 0
    b.close()


def test_split_files_merge_topic_periods_and_order(tmp_path):
    bags = tmp_path / "bags"
    bags.mkdir()
    bag(bags / "part_1.mcap", [(BASE+10,BASE+9,BASE+8)])
    bag(bags / "part_2.mcap", [(BASE+20,BASE+19,BASE+18)])
    a = Analysis(bags, tmp_path / "cache")
    assert len(a.topics) == 1
    assert a.statistics(BASE, BASE+100, [1])["topics"][0]["metrics"]["record_period"]["mean_ns"] == 10
    assert len({e["id"] for e in a.view(BASE, BASE+100, [1])["events"]}) == 2
    a.close()


def test_range_end_exclusive_out_of_order_stamps(tmp_path):
    path = tmp_path / "bag.mcap"
    bag(path, [(BASE+200,BASE+190,BASE+150), (BASE+100,BASE+90,BASE+300)])
    a = Analysis(path, tmp_path / "cache")
    v = a.view(BASE+100, BASE+200, [1])
    assert v["count"] == 1
    assert int(v["events"][0]["stamp"]) == BASE+300
    assert a.statistics(BASE,BASE+300,[1])["topics"][0]["metrics"]["stamp_period"]["mean_ns"] == -150
    a.close()


def test_file_changed_rejected_and_cache_invalidated(tmp_path):
    path = tmp_path / "bag.mcap"
    bag(path, [(BASE,BASE,BASE)])
    a = Analysis(path,tmp_path / "cache")
    old = a.cache
    bag(path, [(BASE+10,BASE,BASE)])
    with pytest.raises(ValueError, match="changed"):
        a.view(BASE,BASE+100,[1])
    a.close()
    b = Analysis(path,tmp_path / "cache")
    assert b.cache != old
    b.close()


def test_zero_stamp_option_and_no_topics(tmp_path):
    path = tmp_path / "bag.mcap"
    bag(path, [(10,8,0)])
    a = Analysis(path,tmp_path / "cache",include_zero=True)
    assert a.statistics(0,20,[1])["topics"][0]["metrics"]["record_stamp"]["mean_ns"] == 10
    assert a.view(0,20,[])["count"] == 0
    a.close()


def test_node_ambiguity_and_fuzzy_search(tmp_path):
    path = generate(tmp_path / "demo.mcap", 50)
    a = Analysis(path,tmp_path / "cache",nodes={"topics":{"/robot/lidar/points":{"publishers":["/a", "/b"]},"/robot/imu/value":{"publishers":["/imu"]}}})
    assert a.topics[1]["publisher"].startswith("? multiple:")
    assert a.select(node="imu") == [2]
    assert a.select(query="PointCloud2") == [1]
    assert fuzzy("rbt pts", "/robot/lidar/points sensor_msgs/msg/PointCloud2")
    a.close()


def test_chunk_limit(tmp_path):
    path = generate(tmp_path / "big.mcap", 3, payload_bytes=2**20)
    a = Analysis(path,tmp_path / "cache",max_chunk_mb=0)
    with pytest.raises(ValueError, match="max-chunk-mb"):
        a.view(a.source.start,a.source.end,[1])
    a.close()


def test_crc_verification_not_bypassed_by_unvalidated_cache(tmp_path):
    path = tmp_path / "bag.mcap"
    bag(path, [(BASE+10,BASE+9,BASE+8)])
    a = Analysis(path,tmp_path/'cache')
    unit = a.source.units[0]
    a.close()
    raw = bytearray(path.read_bytes())
    # Last byte in an uncompressed chunk is message payload, not record framing.
    raw[unit.offset + unit.length - 1] ^= 1
    path.write_bytes(raw)
    a = Analysis(path,tmp_path/'cache')
    a.view(BASE,BASE+100,[1])
    unverified_cache = a.cache
    a.close()
    b = Analysis(path,tmp_path/'cache',validate_crcs=True)
    assert b.cache != unverified_cache
    with pytest.raises(ValueError, match='CRC mismatch'):
        b.view(BASE,BASE+100,[1])
    b.close()
