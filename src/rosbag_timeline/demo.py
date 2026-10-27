"""Deterministic, valid MCAP fixture for exploration and reproducible benchmarks."""
import json
import struct
from pathlib import Path
from mcap.writer import Writer, CompressionType

HEADER = "\n" + "=" * 80 + "\nMSG: std_msgs/Header\nbuiltin_interfaces/Time stamp\nstring frame_id\n" + "=" * 80 + "\nMSG: builtin_interfaces/Time\nint32 sec\nuint32 nanosec\n"
POINTCLOUD = "std_msgs/Header header\nuint32 height\nuint32 width\nsensor_msgs/PointField[] fields\nbool is_bigendian\nuint32 point_step\nuint32 row_step\nuint8[] data\nbool is_dense\n" + HEADER + "=" * 80 + "\nMSG: sensor_msgs/PointField\nuint8 INT8=1\nuint8 UINT8=2\nuint8 INT16=3\nuint8 UINT16=4\nuint8 INT32=5\nuint8 UINT32=6\nuint8 FLOAT32=7\nuint8 FLOAT64=8\nstring name\nuint32 offset\nuint8 datatype\nuint32 count\n"
STAMPED = "std_msgs/Header header\nfloat64 value\n" + HEADER


class Cdr:
    def __init__(self):
        self.data = bytearray(b"\x00\x01\x00\x00")

    def pack(self, fmt, *values, alignment=4):
        self.data.extend(b"\x00" * (-(len(self.data) - 4) % alignment))
        self.data.extend(struct.pack("<" + fmt, *values))

    def string(self, text):
        data = text.encode() + b"\x00"
        self.pack("I", len(data))
        self.data.extend(data)


def pointcloud_template(payload_bytes=0):
    c = Cdr()
    c.pack("iI", 0, 0)
    c.string("lidar")
    count = payload_bytes // 12
    c.pack("II", 1, count)
    c.pack("I", 3)
    for name, offset in (("x", 0), ("y", 4), ("z", 8)):
        c.string(name)
        c.pack("I", offset)
        c.pack("B", 7, alignment=1)
        c.pack("I", 1)
    c.pack("B", 0, alignment=1)
    c.pack("III", 12, count * 12, count * 12)
    c.data.extend(b"\x00" * (count * 12))
    c.pack("B", 1, alignment=1)
    return bytes(c.data)


def generate(path, messages=20000, payload_bytes=0, compression="zstd", chunk_size=2**20):
    path = Path(path)
    template = pointcloud_template(payload_bytes)
    value = Cdr()
    value.pack("iI", 0, 0)
    value.string("imu")
    value.pack("d", 1.0, alignment=8)
    value_template = bytes(value.data)
    base = 1_800_000_000_000_000_000
    with path.open("wb") as f:
        writer = Writer(f, compression={"none": CompressionType.NONE, "zstd": CompressionType.ZSTD, "lz4": CompressionType.LZ4}[compression], chunk_size=chunk_size)
        writer.start(profile="ros2", library="rosbag-timeline demo")
        schemas = [writer.register_schema("sensor_msgs/msg/PointCloud2", "ros2msg", POINTCLOUD.encode()),
                   writer.register_schema("demo_msgs/msg/StampedValue", "ros2msg", STAMPED.encode()),
                   writer.register_schema("std_msgs/msg/UInt32", "ros2msg", b"uint32 data\n")]
        names = ["/robot/lidar/points", "/robot/imu/value", "/robot/controller/counter"]
        channels = [writer.register_channel(n, "cdr", s) for n, s in zip(names, schemas)]
        for i in range(messages):
            topic = i % 3
            log = base + i * 1_000_000
            delay = 2_000_000 + (i % 19) * 120_000 + (60_000_000 if i % 1000 > 960 else 0)
            pub = log - delay
            if i % 151 == 0:
                pub = log
            stamp = pub - 8_000_000 - (i % 11) * 270_000
            if i % 97 == 0:
                stamp = log + 15_000_000
            if i % 211 == 0:
                stamp = 0
            raw = template if topic == 0 else value_template
            if topic == 2:
                raw = b"\x00\x01\x00\x00" + struct.pack("<I", i)
            else:
                raw = raw[:4] + struct.pack("<iI", *divmod(stamp, 1_000_000_000)) + raw[12:]
            writer.add_message(channels[topic], log, raw, pub, sequence=i)
        writer.finish()
    nodes = {"recorder": "/bag_recorder", "topics": {
        names[0]: {"publishers": ["/lidar_driver"], "subscribers": ["/localization", "/bag_recorder"]},
        names[1]: {"publishers": ["/imu_driver"], "subscribers": ["/localization", "/bag_recorder"]},
        names[2]: {"publishers": ["/controller"], "subscribers": ["/bag_recorder"]}}}
    path.with_suffix(".nodes.json").write_text(json.dumps(nodes, indent=2))
    return path
