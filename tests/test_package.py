import ikigai


def test_protocol_version_is_pinned():
    # v7 is the ikigai-wire era this package mirrors (typed errors on the
    # wire; hello required).
    assert ikigai.PROTOCOL_VERSION == 7
