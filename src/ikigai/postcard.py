"""The postcard primitives this package needs — nothing more.

Postcard (https://postcard.jamesmunns.com) is a non-self-describing binary
codec: the byte stream carries no field names or type tags, so encode and
decode must mirror the Rust type layout field-for-field. The subset used by
the ikigai wire protocol:

- ``u8``: one byte, verbatim.
- ``bool``: one byte, ``0x00`` / ``0x01``.
- ``u16``/``u32``/``u64``/``usize``: unsigned LEB128 varint — 7 bits per
  byte, least-significant group first, high bit = continuation.
- ``Option<T>``: ``0x00`` for ``None``, ``0x01`` + payload for ``Some``.
- ``String`` / ``Vec<u8>``: varint length + bytes (a ``Vec<u8>`` is a
  sequence of ``u8``, which is byte-for-byte identical to a byte string).
- sequences (``Vec<T>``, ``BTreeSet<T>``): varint length + elements.
- maps (``BTreeMap<K, V>``): varint length + key/value pairs. Rust's
  ``BTreeMap`` iterates in key order, so the canonical encoding sorts keys
  (for ``String`` keys: lexicographic over UTF-8 bytes).
- structs and tuples: fields in declaration order, no framing.
- enums: varint ``u32`` discriminant = the variant's declaration INDEX
  (``#[repr(u8)]`` values do not participate), then the payload.
"""

from __future__ import annotations

__all__ = ["DecodeError", "Reader", "encode_varint"]


class DecodeError(ValueError):
    """A byte stream that does not parse as the expected postcard layout."""


def encode_varint(n: int) -> bytes:
    """Unsigned LEB128."""
    if n < 0:
        raise ValueError("postcard varints are unsigned")
    out = bytearray()
    while True:
        group = n & 0x7F
        n >>= 7
        if n:
            out.append(group | 0x80)
        else:
            out.append(group)
            return bytes(out)


class Reader:
    """A cursor over one postcard message. Every read is bounds-checked."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def take(self, n: int) -> bytes:
        end = self._pos + n
        if end > len(self._data):
            raise DecodeError(
                f"truncated message: wanted {n} bytes at offset {self._pos}, "
                f"have {len(self._data) - self._pos}"
            )
        chunk = self._data[self._pos : end]
        self._pos = end
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def bool(self) -> bool:
        b = self.u8()
        if b == 0:
            return False
        if b == 1:
            return True
        raise DecodeError(f"invalid bool byte 0x{b:02x}")

    def varint(self, max_bits: int = 64) -> int:
        n = 0
        shift = 0
        while True:
            b = self.u8()
            n |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
            if shift >= max_bits + 7:
                raise DecodeError("varint too long")
        if n >= 1 << max_bits:
            raise DecodeError(f"varint exceeds u{max_bits}")
        return n

    def byte_string(self) -> bytes:
        return self.take(self.varint())

    def string(self) -> str:
        raw = self.byte_string()
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise DecodeError(f"invalid utf-8 in string: {e}") from e

    def option(self) -> bool:
        """Read an Option tag; True means a payload follows."""
        b = self.u8()
        if b == 0:
            return False
        if b == 1:
            return True
        raise DecodeError(f"invalid Option tag 0x{b:02x}")

    def remainder(self) -> bytes:
        """Consume and return everything left. For payloads whose layout this
        side cannot know (an unknown enum variant from a newer peer) — the
        codec is non-self-describing, so skipping is all-or-nothing."""
        return self.take(len(self._data) - self._pos)

    def finish(self) -> None:
        """Assert the message was consumed exactly."""
        if self._pos != len(self._data):
            raise DecodeError(f"{len(self._data) - self._pos} trailing bytes after message")
