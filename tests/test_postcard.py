import pytest

from ikigai.postcard import DecodeError, Reader, encode_varint


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        (0, b"\x00"),
        (1, b"\x01"),
        (127, b"\x7f"),
        (128, b"\x80\x01"),
        (300, b"\xac\x02"),
        (16384, b"\x80\x80\x01"),
        (2**32 - 1, b"\xff\xff\xff\xff\x0f"),
        (2**64 - 1, b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01"),
    ],
)
def test_varint_edges(value, encoded):
    assert encode_varint(value) == encoded
    assert Reader(encoded).varint() == value


def test_varint_rejects_negative():
    with pytest.raises(ValueError):
        encode_varint(-1)


def test_varint_rejects_overflow():
    # 2^64 does not fit a u64.
    with pytest.raises(DecodeError):
        Reader(b"\x80\x80\x80\x80\x80\x80\x80\x80\x80\x02").varint()


def test_truncated_read_is_loud():
    r = Reader(b"\x05ab")
    with pytest.raises(DecodeError, match="truncated"):
        r.byte_string()


def test_string_round_trip():
    payload = "héllo".encode()
    r = Reader(encode_varint(len(payload)) + payload)
    assert r.string() == "héllo"
    r.finish()


def test_invalid_utf8_is_a_decode_error():
    with pytest.raises(DecodeError, match="utf-8"):
        Reader(b"\x02\xff\xfe").string()


def test_bool_and_option_tags_are_strict():
    with pytest.raises(DecodeError):
        Reader(b"\x02").bool()
    with pytest.raises(DecodeError):
        Reader(b"\x07").option()


def test_trailing_bytes_are_rejected():
    r = Reader(b"\x00\x00")
    r.u8()
    with pytest.raises(DecodeError, match="trailing"):
        r.finish()


@pytest.mark.parametrize("bits", [16, 32, 64])
def test_varint_rejects_more_than_the_rust_integer_byte_limit(bits):
    # postcard 1.1.3 de/deserializer.rs bounds reads by ceil(bits / 7),
    # even when all payload groups are zero and the value itself fits.
    limit = (bits + 6) // 7
    with pytest.raises(DecodeError):
        Reader(b"\x80" * limit + b"\x00").varint(bits)


@pytest.mark.parametrize("bits", [16, 32, 64])
def test_varint_rejects_continuation_at_the_rust_integer_byte_limit(bits):
    limit = (bits + 6) // 7
    reader = Reader(b"\x80" * limit + b"\x02")
    with pytest.raises(DecodeError):
        reader.varint(bits)
    # The next field must not be consumed while looking for a terminator.
    assert reader.u8() == 2


@pytest.mark.parametrize("bits", [16, 32, 64])
def test_varint_accepts_nonminimal_encodings_within_the_rust_byte_limit(bits):
    # Rust accepts these; decode/encode normalizes them rather than preserving bytes.
    limit = (bits + 6) // 7
    assert Reader(b"\x80" * (limit - 1) + b"\x00").varint(bits) == 0


@pytest.mark.parametrize("bits", [16, 32, 64])
def test_varint_maximum_and_one_past_for_each_width(bits):
    maximum = (1 << bits) - 1
    assert Reader(encode_varint(maximum)).varint(bits) == maximum
    with pytest.raises(DecodeError):
        Reader(encode_varint(maximum + 1)).varint(bits)


@pytest.mark.parametrize("encoded", [b"", b"\x80", b"\xff\xff\xff\xff"])
def test_truncated_varint_is_refused(encoded):
    with pytest.raises(DecodeError, match="truncated"):
        Reader(encoded).varint()


@pytest.mark.parametrize("size", [0, 127, 128, 1 << 20])
def test_byte_string_round_trip_at_length_boundaries(size):
    data = b"x" * size
    encoded = encode_varint(size) + data
    reader = Reader(encoded)
    assert reader.byte_string() == data
    reader.finish()
