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
