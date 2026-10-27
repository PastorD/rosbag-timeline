import struct
import tracemalloc
import numpy as np
import pytest
from mcap.records import Schema
from rosbags.typesys import get_typestore, get_types_from_msg, Stores
from rosbag_timeline.stamps import StampExtractor, parse_schema
from rosbag_timeline.demo import POINTCLOUD, pointcloud_template

TIME = "\n" + "=" * 80 + "\nMSG: builtin_interfaces/Time\nint32 sec\nuint32 nanosec\n"


def make(definition, typename="example/msg/Test", override=None):
    definition += TIME
    store = get_typestore(Stores.EMPTY)
    store.register(get_types_from_msg(definition, typename))
    extractor = StampExtractor.build(Schema(1, definition.encode(), "ros2msg", typename), override)
    return store, extractor


@pytest.mark.parametrize("little", [True, False])
@pytest.mark.parametrize("nano", [0, 1, 999999999])
@pytest.mark.parametrize("sec", [-1, 0, 1800000000])
def test_endian_precision_signed_time(little, nano, sec):
    st, ex = make("uint8 first\nstring label\nfloat64[] before\nbuiltin_interfaces/Time stamp\nuint8[] tail\n")
    T = st.types
    msg = T["example/msg/Test"](7, "abcde", np.array([1.0, 2.0]), T["builtin_interfaces/msg/Time"](sec, nano), np.zeros(8, dtype=np.uint8))
    raw = st.serialize_cdr(msg, "example/msg/Test", little_endian=little)
    value, status = ex.extract(raw)
    assert value == sec * 10**9 + nano
    assert status == ("zero_stamp" if value == 0 else "ok")


def test_header_reads_only_prefix():
    ex = StampExtractor.build(Schema(1, POINTCLOUD.encode(), "ros2msg", "sensor_msgs/msg/PointCloud2"))
    # Deliberately omit every byte after header.stamp. Full deserialization fails;
    # timestamp extraction must still work without touching fields or point data.
    assert ex.extract(b"\x00\x01\x00\x00" + struct.pack("<iI", 123, 456)) == (123000000456, "ok")


def test_large_primitive_array_before_stamp_has_bounded_allocations():
    _, ex = make("uint8[] payload\nbuiltin_interfaces/Time stamp\n")
    size = 24 * 2**20
    raw = b"\x00\x01\x00\x00" + struct.pack("<I", size) + bytes(size) + struct.pack("<iI", 77, 9)
    tracemalloc.start()
    assert ex.extract(memoryview(raw)) == (77000000009, "ok")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 100000


def test_custom_nested_stamp_and_struct_array():
    definition = "example/Item[] items\nbuiltin_interfaces/Time stamp\n" + "=" * 80 + "\nMSG: example/Item\nstring name\nuint16[3] codes\nbuiltin_interfaces/Time stamp\n"
    st, ex = make(definition)
    T = st.types
    Time = T["builtin_interfaces/msg/Time"]
    msg = T["example/msg/Test"]([T["example/msg/Item"]("odd", np.array([1, 2, 3], dtype=np.uint16), Time(3, 4)), T["example/msg/Item"]("other", np.array([4, 5, 6], dtype=np.uint16), Time(4, 5))], Time(20, 6))
    raw = st.serialize_cdr(msg, "example/msg/Test")
    assert ex.extract(raw) == (20000000006, "ok")
    ex2 = make(definition, override="items[1].stamp")[1]
    assert ex2.extract(raw) == (4000000005, "ok")
    assert make(definition, override="items[2].stamp")[1].extract(raw)[1] == "decode_error"


def test_multi_stamp_is_explicit():
    _, ex = make("builtin_interfaces/Time a\nbuiltin_interfaces/Time b\n")
    assert ex.path is None
    assert ex.extract(b"")[1] == "ambiguous_stamp"


def test_one_array_stamp_not_silently_first():
    _, ex = make("builtin_interfaces/Time[] stamps\n")
    assert ex.path is None
    assert ex.candidates == ["stamps[0]"]


def test_ros2idl_dependency_bundle():
    definition = "IDL: example/msg/Test\nmodule example { module msg { struct Test { sequence<octet> payload; builtin_interfaces::msg::Time stamp; }; }; };\n" + "=" * 80 + "\nIDL: builtin_interfaces/msg/Time\nmodule builtin_interfaces { module msg { struct Time { long sec; unsigned long nanosec; }; }; };\n"
    ex = StampExtractor.build(Schema(1, definition.encode(), "ros2idl", "example/msg/Test"))
    assert ex.error is None
    raw = b"\x00\x01\x00\x00" + struct.pack("<I", 3) + b"abc\x00" + struct.pack("<iI", 55, 99)
    assert ex.extract(raw) == (55000000099, "ok")


@pytest.mark.parametrize("prefix", [b"\x00\x07\x00\x00", b"\x00\x03\x00\x00", b"\x00"])
def test_unsupported_encapsulation_is_not_a_timestamp(prefix):
    _, ex = make("builtin_interfaces/Time stamp\n")
    assert ex.extract(prefix + bytes(8))[1] == "decode_error"


def test_invalid_time_truncation_and_overrides():
    _, ex = make("builtin_interfaces/Time stamp\n")
    assert ex.extract(b"\x00\x01\x00\x00" + struct.pack("<iI", 1, 10**9))[1] == "decode_error"
    assert ex.extract(b"\x00\x01\x00\x00")[1] == "decode_error"
    assert make("builtin_interfaces/Time stamp\n", override="missing")[1].error
    assert make("builtin_interfaces/Time stamp\n", override="none")[1].path is None
    assert make("builtin_interfaces/Time stamp\n", override="none")[1].extract(b"")[1] == "disabled_stamp"


def test_demo_pointcloud_is_valid_full_cdr():
    store = get_typestore(Stores.EMPTY)
    store.register(get_types_from_msg(POINTCLOUD, "sensor_msgs/msg/PointCloud2"))
    msg = store.deserialize_cdr(pointcloud_template(12000), "sensor_msgs/msg/PointCloud2")
    assert len(msg.data) == 12000
    assert [x.name for x in msg.fields] == ["x", "y", "z"]
