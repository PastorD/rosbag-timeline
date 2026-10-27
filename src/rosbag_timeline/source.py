"""Chunk-pruned MCAP access; memoryviews avoid per-message payload copies."""
from __future__ import annotations

import io
import mmap
import struct
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from mcap.data_stream import ReadDataStream
from mcap.reader import make_reader
from mcap.records import Schema, Channel

MAGIC = b"\x89MCAP0\r\n"


@dataclass
class Unit:
    file: int
    offset: int
    length: int
    start: int
    end: int
    channels: set[int] | None
    chunk: bool = True


def records(data):
    pos = 0
    while pos < len(data):
        if pos + 9 > len(data):
            raise ValueError("truncated MCAP record header")
        op, length = struct.unpack_from("<BQ", data, pos)
        end = pos + 9 + length
        if end > len(data):
            raise ValueError("MCAP record exceeds its container")
        yield op, pos, pos + 9, end
        pos = end


class Source:
    def __init__(self, path, max_chunk_mb=512, validate_crcs=False):
        path = Path(path).resolve()
        self.paths = sorted(path.glob("*.mcap")) if path.is_dir() else [path]
        if not self.paths or any(not p.is_file() for p in self.paths):
            raise ValueError("provide an MCAP file or directory containing *.mcap files")
        self.max_chunk = int(max_chunk_mb * 1024 * 1024)
        self.validate_crcs = validate_crcs
        self.schemas = {}
        self.channels = {}
        self.units = []
        self.warnings = []
        self.decompressed_bytes = 0
        self.units_read = 0
        for fid, path in enumerate(self.paths):
            with path.open("rb") as stream:
                summary = make_reader(stream).get_summary()
            if summary and summary.chunk_indexes and summary.channels:
                self.schemas.update({(fid, k): v for k, v in summary.schemas.items()})
                self.channels.update({(fid, k): v for k, v in summary.channels.items()})
                for chunk in summary.chunk_indexes:
                    self.units.append(Unit(fid, chunk.chunk_start_offset, chunk.chunk_length,
                                           chunk.message_start_time, chunk.message_end_time,
                                           set(chunk.message_index_offsets) or None))
            else:
                self.warnings.append(f"{path.name}: no usable chunk summary; startup scans records to discover schemas and time bounds.")
                self.discover(fid)
        self.units.sort(key=lambda u: (u.start, u.file, u.offset))
        self.start = min((x.start for x in self.units), default=0)
        self.end = max((x.end for x in self.units), default=self.start) + 1
        self.channel_keys = sorted(self.channels)
        self.cid = {key: i + 1 for i, key in enumerate(self.channel_keys)}
        self._stats = [p.stat() for p in self.paths]

    def unchanged(self):
        return all((p.stat().st_size, p.stat().st_mtime_ns) == (s.st_size, s.st_mtime_ns)
                   for p, s in zip(self.paths, self._stats))

    @contextmanager
    def buffer(self, unit):
        with self.paths[unit.file].open("rb") as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                raw = memoryview(mm)[unit.offset:unit.offset + unit.length]
                data = None
                try:
                    if unit.chunk:
                        if len(raw) < 49 or raw[0] != 6:
                            raise ValueError("invalid MCAP chunk header")
                        size, crc, n = struct.unpack_from("<QII", raw, 25)
                        if size > self.max_chunk:
                            raise ValueError(f"chunk is {size / 2**20:.1f} MiB; raise --max-chunk-mb explicitly")
                        compression = bytes(raw[41:41 + n]).decode()
                        offset = 41 + n
                        length = struct.unpack_from("<Q", raw, offset)[0]
                        offset += 8
                        if offset + length != len(raw):
                            raise ValueError("invalid MCAP chunk data length")
                        encoded = raw[offset:]
                        try:
                            if not compression:
                                data = encoded[:]
                            elif compression == "zstd":
                                import zstandard
                                with zstandard.ZstdDecompressor().stream_reader(encoded) as reader:
                                    decoded = reader.read(size + 1)
                                if len(decoded) != size:
                                    raise ValueError("Zstd chunk size mismatch")
                                data = memoryview(decoded)
                                self.decompressed_bytes += len(data)
                            elif compression == "lz4":
                                import lz4.frame
                                # Bounded streaming output; reject a misleading chunk size.
                                d = lz4.frame.LZ4FrameDecompressor()
                                decoded = d.decompress(encoded, max_length=size + 1)
                                if len(decoded) > size or not d.eof:
                                    raise ValueError("LZ4 chunk exceeds declared size")
                                data = memoryview(decoded)
                                self.decompressed_bytes += len(data)
                            else:
                                raise ValueError(f"unsupported MCAP compression: {compression}")
                        finally:
                            encoded.release()
                        if len(data) != size:
                            raise ValueError("decompressed chunk size mismatch")
                        if self.validate_crcs and crc and zlib.crc32(data) != crc:
                            raise ValueError("MCAP chunk CRC mismatch")
                    else:
                        data = raw[:]
                    self.units_read += 1
                    yield data
                finally:
                    if data is not None:
                        data.release()
                    raw.release()

    def metadata_record(self, fid, op, data, begin, end):
        if op in (3, 4):
            stream = ReadDataStream(io.BytesIO(bytes(data[begin:end])))
            value = Schema.read(stream) if op == 3 else Channel.read(stream)
            target = self.schemas if op == 3 else self.channels
            target[(fid, value.id)] = value

    def discover(self, fid):
        """Fallback without keeping message payloads or a per-message Python list."""
        with self.paths[fid].open("rb") as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                if mm[:8] != MAGIC or mm[-8:] != MAGIC:
                    raise ValueError("not a complete MCAP file")
                pos = 8
                group_start = None
                group_end = None
                group_min = 2**63 - 1
                group_max = 0
                group_channels = set()

                def flush():
                    nonlocal group_start, group_end, group_min, group_max, group_channels
                    if group_start is not None:
                        self.units.append(Unit(fid, group_start, group_end - group_start,
                                               group_min, group_max, group_channels, False))
                    group_start, group_end, group_min, group_max, group_channels = None, None, 2**63 - 1, 0, set()

                while pos < len(mm) - 8:
                    op, length = struct.unpack_from("<BQ", mm, pos)
                    end = pos + 9 + length
                    if end > len(mm) - 8:
                        raise ValueError("truncated MCAP data section")
                    if op == 15 or op == 2:
                        break
                    if op == 6:
                        flush()
                        start, last = struct.unpack_from("<QQ", mm, pos + 9)
                        unit = Unit(fid, pos, end - pos, start, last, set())
                        with self.buffer(unit) as data:
                            for inner_op, _, begin, stop in records(data):
                                self.metadata_record(fid, inner_op, data, begin, stop)
                                if inner_op == 5:
                                    unit.channels.add(struct.unpack_from("<H", data, begin)[0])
                        self.units.append(unit)
                    elif op == 5:
                        ch, _, log, _ = struct.unpack_from("<HIQQ", mm, pos + 9)
                        if group_start is None:
                            group_start = pos
                        group_end = end
                        group_min, group_max = min(group_min, log), max(group_max, log)
                        group_channels.add(ch)
                        if group_end - group_start > 8 * 2**20:
                            flush()
                    else:
                        flush()
                        self.metadata_record(fid, op, mm, pos + 9, end)
                    pos = end
                flush()

    def timing_rows(self, unit, wanted, extractors):
        with self.buffer(unit) as data:
            for op, offset, begin, end in records(data):
                if op != 5:
                    continue
                if end - begin < 22:
                    raise ValueError("truncated MCAP Message record")
                channel, seq, log, pub = struct.unpack_from("<HIQQ", data, begin)
                if channel not in wanted:
                    continue
                cid = self.cid[(unit.file, channel)]
                if log > 2**63 - 1 or pub > 2**63 - 1:
                    raise ValueError("MCAP timestamp exceeds signed int64 range")
                channel_info = self.channels[(unit.file, channel)]
                if channel_info.message_encoding == "cdr":
                    payload = data[begin + 22:end]
                    try:
                        stamp, status = extractors[cid].extract(payload)
                    finally:
                        payload.release()
                else:
                    stamp, status = None, "unsupported_encoding"
                key = f"{unit.file}:{unit.offset}:{offset}"
                yield (key, cid, log, pub, stamp, status, seq)
