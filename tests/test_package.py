import ikigai


def test_protocol_version_is_pinned():
    # v6 is the ikigai-wire era this package mirrors (the hello exchange).
    assert ikigai.PROTOCOL_VERSION == 6
