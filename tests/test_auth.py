"""``ikigai.auth`` and ``ikigai.base64`` against the shared vectors (ledger #1861).

``tests/vectors/auth_vectors.json`` is a VERBATIM copy of ikigai-http's
``tests/vectors/auth_vectors.json`` at commit ffadc89 (ikigai-http PR 19). To pick up new
vectors, copy that file again, unedited; it stays ``eol=lf`` through ``.gitattributes``.
"""

import json
import pickle
from pathlib import Path

import pytest

from ikigai import auth, base64

PATH = Path(__file__).resolve().parent / "vectors" / "auth_vectors.json"
VECTORS = json.loads(PATH.read_text(encoding="utf-8"))


def cases(section):
    return [pytest.param(c, id=c["name"]) for c in VECTORS[section]]


def test_every_section_is_run():
    assert set(VECTORS) == {
        "about",
        "version",
        "basic",
        "basic_refusals",
        "bearer",
        "bearer_refusals",
        "scheme",
        "base64",
        "base64_refusals",
    }


@pytest.mark.parametrize("case", cases("basic"))
def test_basic(case):
    built = auth.basic(case["user"], case["password"])
    assert built.as_str() == case["expect"]
    assert built.credentials() == case["expect"].removeprefix("Basic ")


@pytest.mark.parametrize("case", cases("basic_refusals"))
def test_basic_refusals(case):
    with pytest.raises(auth.AuthError) as e:
        auth.basic(case["user"], case["password"])
    assert e.value.code == case["error"]


@pytest.mark.parametrize("case", cases("bearer"))
def test_bearer(case):
    assert auth.bearer(case["token"]).as_str() == case["expect"]


@pytest.mark.parametrize("case", cases("bearer_refusals"))
def test_bearer_refusals(case):
    with pytest.raises(auth.AuthError) as e:
        auth.bearer(case["token"])
    assert e.value.code == case["error"]


@pytest.mark.parametrize("case", cases("scheme"))
def test_scheme(case):
    got = auth.scheme(case["value"])
    want = case["expect"]
    if want is None:
        assert got is None
    else:
        assert got is not None
        found, credentials = got
        assert (found.kind, found.name, credentials) == (
            want["scheme"],
            want["name"],
            want["credentials"],
        )


def unhex(case):
    return bytes.fromhex(case["hex"]) if "hex" in case else case["text"].encode()


@pytest.mark.parametrize("case", cases("base64"))
def test_base64(case):
    data = unhex(case)
    assert base64.encode(data) == case["encoded"]
    assert base64.decode(case["encoded"]) == data
    assert base64.decode(case["encoded"].encode()) == data


@pytest.mark.parametrize("case", cases("base64_refusals"))
def test_base64_refusals(case):
    with pytest.raises(base64.Base64Error) as e:
        base64.decode(case["encoded"])
    assert e.value.code == case["error"]


def test_nonzero_pad_bits_are_refused_where_the_stdlib_is_not():
    import base64 as std

    assert std.b64decode("Zh==", validate=True) == b"f"
    with pytest.raises(base64.Base64Error):
        base64.decode("Zh==")


def test_non_ascii_text_is_refused():
    with pytest.raises(base64.Base64Error):
        base64.decode("Zm9é")


def test_no_credential_in_repr_or_errors():
    secret = "hunter2-s3cret"
    built = auth.basic("user", secret)
    for shown in (repr(built), str(built), f"{built}", f"{[built]}"):
        assert secret not in shown
        assert built.credentials() not in shown
    assert repr(built) == "Authorization(Basic <redacted>)"
    with pytest.raises(TypeError):
        pickle.dumps(built)
    for call in (lambda: auth.basic("a:b", secret), lambda: auth.basic("a", secret + "\n")):
        with pytest.raises(auth.AuthError) as e:
            call()
        assert secret not in repr(e.value) + str(e.value)
    with pytest.raises(auth.AuthError) as e:
        auth.bearer("zq9 xw")
    assert "zq9" not in repr(e.value)
    with pytest.raises(base64.Base64Error) as e:
        base64.decode("Zh==")
    assert "Zh" not in repr(e.value) and e.value.__cause__ is None
