"""Schema-directed CDR1 cursor: read one Time, never deserialize a message.

Offsets are relative to the start of the CDR payload, AFTER encapsulation.
Skipping primitive arrays is O(1); variable-size struct sequences require walking
their element layouts. Nothing past the selected timestamp is inspected.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from rosbags.typesys import get_types_from_msg, get_types_from_idl

TIME = "builtin_interfaces/msg/Time"
SIZES = {"bool": 1, "byte": 1, "char": 1, "int8": 1, "uint8": 1,
         "int16": 2, "uint16": 2, "int32": 4, "uint32": 4, "float32": 4,
         "int64": 8, "uint64": 8, "float64": 8, "float128": 16}
TOKEN = re.compile(r"([A-Za-z_][A-Za-z_0-9]*)(?:\[([0-9]+)\])?$")


class StampError(ValueError):
    pass


def parse_schema(name: str, encoding: str, data: bytes):
    text = data.decode("utf-8")
    if encoding == "ros2msg":
        return get_types_from_msg(text, name)
    if encoding == "ros2idl":
        # rosbag2 concatenates dependency IDLs with ==== / IDL: separators.
        parts = re.split(r"(?m)^={10,}\s*\nIDL:[^\n]*\n", text)
        types = {}
        for part in parts:
            part = re.sub(r"(?m)^IDL:[^\n]*\n", "", part)
            if part.strip():
                types.update(get_types_from_idl(part))
        return types
    raise StampError(f"unsupported schema encoding: {encoding or '(empty)'}")


class Cursor:
    def __init__(self, data):
        if len(data) < 4 or bytes(data[:2]) not in (b"\x00\x00", b"\x00\x01"):
            raise StampError("unsupported CDR encapsulation (only plain CDR1 BE/LE)")
        self.data = data
        self.endian = "<" if data[1] == 1 else ">"
        self.pos = 0

    def align(self, n):
        self.pos = (self.pos + n - 1) & ~(n - 1)

    def advance(self, n):
        self.pos += n
        if self.pos + 4 > len(self.data):
            raise StampError("truncated CDR field")

    def uint32(self):
        self.align(4)
        self.advance(4)
        return struct.unpack_from(self.endian + "I", self.data, self.pos)[0]

    def time(self):
        self.align(4)
        self.advance(8)
        sec, nano = struct.unpack_from(self.endian + "iI", self.data, self.pos - 4)
        if nano >= 1_000_000_000:
            raise StampError("nanosec is outside [0, 1e9)")
        return sec * 1_000_000_000 + nano


@dataclass
class StampExtractor:
    types: dict
    typename: str
    path: str | None
    candidates: list[str]
    error: str | None = None
    disabled: bool = False

    @classmethod
    def build(cls, schema, override=None):
        if schema is None:
            return cls({}, "", None, [], "missing schema")
        try:
            types = parse_schema(schema.name, schema.encoding, schema.data)
            name = schema.name
            if name not in types and name.count("/") == 1:
                name = name.replace("/", "/msg/")
            obj = cls(types, name, None, [])
            obj.candidates = obj.discover(name)
            if override == "none":
                obj.disabled = True
                return obj
            if override:
                obj.validate_path(name, override)
                obj.path = override
            elif "header.stamp" in obj.candidates:
                obj.path = "header.stamp"
            elif "stamp" in obj.candidates:
                obj.path = "stamp"
            elif len(obj.candidates) == 1 and "[" not in obj.candidates[0]:
                obj.path = obj.candidates[0]
            return obj
        except Exception as exc:
            return cls({}, schema.name, None, [], str(exc))

    def discover(self, name, prefix="", seen=()):
        if name in seen or len(seen) > 24:
            return []
        result = []
        for field, desc in self.types.get(name, ([], []))[1]:
            path = prefix + field
            kind, arg = desc
            if kind == 2:
                if arg == TIME:
                    result.append(path)
                else:
                    result.extend(self.discover(arg, path + ".", seen + (name,)))
            elif kind in (3, 4) and arg[0][0] == 2:
                subtype = arg[0][1]
                if subtype == TIME:
                    result.append(path + "[0]")
                else:
                    result.extend(self.discover(subtype, path + "[0].", seen + (name,)))
        return result

    def validate_path(self, name, path):
        for i, token in enumerate(path.split(".")):
            match = TOKEN.fullmatch(token)
            if not match:
                raise StampError(f"invalid stamp path: {path}")
            field, index = match.groups()
            desc = dict(self.types[name][1]).get(field)
            if desc is None:
                raise StampError(f"field {field} not found in {name}")
            kind, arg = desc
            if index is not None:
                if kind not in (3, 4):
                    raise StampError(f"{field} is not an array")
                if kind == 3 and int(index) >= arg[1]:
                    raise StampError("array index outside fixed bound")
                kind, arg = arg[0]
            if kind != 2:
                raise StampError("stamp path must end in builtin_interfaces/Time")
            name = arg
        if name != TIME:
            raise StampError("stamp path must end in builtin_interfaces/Time")

    def skip(self, c, desc, depth=0):
        if depth > 64:
            raise StampError("schema recursion limit")
        kind, arg = desc
        if kind == 1:
            base = arg[0]
            if base == "string":
                length = c.uint32()
                c.advance(length)
            elif base in SIZES:
                size = SIZES[base]
                c.align(min(size, 8))
                c.advance(size)
            else:
                raise StampError(f"cannot skip CDR primitive {base}")
        elif kind == 2:
            if arg not in self.types:
                raise StampError(f"schema dependency missing: {arg}")
            for _, field in self.types[arg][1]:
                self.skip(c, field, depth + 1)
        elif kind in (3, 4):
            element, count = arg
            if kind == 4:
                count = c.uint32()
            self.skip_array(c, element, count, depth)
        else:
            raise StampError(f"unknown schema node {kind}")

    def skip_array(self, c, desc, count, depth=0):
        if not count:
            return
        kind, arg = desc
        if kind == 1 and arg[0] in SIZES:
            size = SIZES[arg[0]]
            c.align(min(size, 8))
            c.advance(size * count)
        else:
            if count > len(c.data):
                raise StampError("invalid sequence length")
            for _ in range(count):
                self.skip(c, desc, depth + 1)

    def locate(self, c, name, tokens):
        match = TOKEN.fullmatch(tokens[0])
        field, index = match.groups()
        for key, desc in self.types[name][1]:
            if key != field:
                self.skip(c, desc)
                continue
            kind, arg = desc
            if index is not None:
                element, count = arg
                if kind == 4:
                    count = c.uint32()
                index = int(index)
                if index >= count:
                    raise StampError("selected stamp array element is absent")
                self.skip_array(c, element, index)
                kind, arg = element
            if len(tokens) == 1:
                return c.time()
            return self.locate(c, arg, tokens[1:])
        raise StampError("stamp field not found")

    def extract(self, data):
        if self.disabled:
            return None, "disabled_stamp"
        if self.error:
            return None, "schema_error"
        if self.path is None:
            return None, "ambiguous_stamp" if self.candidates else "no_stamp"
        try:
            value = self.locate(Cursor(data), self.typename, self.path.split("."))
            return value, "zero_stamp" if value == 0 else "ok"
        except (ValueError, KeyError, IndexError, struct.error) as exc:
            return None, "decode_error"
