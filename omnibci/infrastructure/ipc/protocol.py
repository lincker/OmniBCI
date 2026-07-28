#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2024-12-02 09:01
"""Length-prefixed IPC frame primitives.

Wire v1 remains exactly eight header bytes (unsigned big-endian version and
body length) followed by an UTF-8 body.  This module deliberately validates the
header before callers allocate/read a body: a peer-controlled length must never
turn into an unbounded ``readexactly`` request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final


# Event messages are small JSON envelopes.  One MiB leaves ample room for rich
# result payloads while bounding memory retained by a single hostile/corrupt
# frame.  This is a receiver limit as well as an encoder limit.
MAX_FRAME_BODY_BYTES: Final[int] = 1024 * 1024
_UINT32_MAX: Final[int] = (1 << 32) - 1


class FrameProtocolError(ValueError):
    """Base class for structurally invalid frame data."""


class InvalidFrameHeader(FrameProtocolError):
    """The fixed-width header has an invalid shape or field value."""


class FrameBodyTooLarge(FrameProtocolError):
    """The advertised or encoded body exceeds ``MAX_FRAME_BODY_BYTES``."""


def _uint32(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidFrameHeader(f"{field} must be an integer")
    if not 0 <= value <= _UINT32_MAX:
        raise InvalidFrameHeader(f"{field} must fit in an unsigned 32-bit integer")
    return value


class Frame:

    @dataclass(frozen=True, slots=True)
    class Header:
        """Immutable fixed-width frame header.

        Keeping the fields read-only is part of the framing invariant: callers
        cannot mutate ``body_size`` after a :class:`Frame` has paired it with a
        body and thereby put a different length on the wire.
        """

        version: int = 1
        body_size: int = 0

        HEADER_SIZE: ClassVar[int] = 8

        def __post_init__(self) -> None:
            version = _uint32(self.version, field="version")
            body_size = _uint32(self.body_size, field="body_size")
            if body_size > MAX_FRAME_BODY_BYTES:
                raise FrameBodyTooLarge(
                    f"frame body is {body_size} bytes; "
                    f"limit is {MAX_FRAME_BODY_BYTES}"
                )
            object.__setattr__(self, "version", version)
            object.__setattr__(self, "body_size", body_size)

        def to_bytes(self) -> bytes:
            version_bytes = self.version.to_bytes(length=4, byteorder="big")
            body_size_bytes = self.body_size.to_bytes(length=4, byteorder="big")
            return version_bytes + body_size_bytes

        @classmethod
        def from_bytes(cls, header_bytes) -> "Frame.Header":
            if not isinstance(header_bytes, (bytes, bytearray, memoryview)):
                raise InvalidFrameHeader("frame header must be bytes-like")
            raw = bytes(header_bytes)
            if len(raw) != cls.HEADER_SIZE:
                raise InvalidFrameHeader(
                    f"frame header must be exactly {cls.HEADER_SIZE} bytes; "
                    f"got {len(raw)}"
                )
            version = int.from_bytes(raw[:4], byteorder="big")
            body_size = int.from_bytes(raw[4:], byteorder="big")
            return cls(version, body_size)

    __slots__ = ("_body", "_body_bytes", "_header")

    def __init__(self, body: str, version: int = 1):
        if not isinstance(body, str):
            raise TypeError("frame body must be str")
        body_bytes = body.encode()
        self._header = Frame.Header(version, len(body_bytes))
        self._body = body
        self._body_bytes = body_bytes

    @property
    def header(self) -> "Frame.Header":
        return self._header

    @property
    def body(self) -> str:
        return self._body

    def to_bytes(self) -> bytes:
        # Frame and Header expose no mutable public fields, so the validated
        # header and the exact encoded bytes can never drift after construction.
        return self._header.to_bytes() + self._body_bytes
