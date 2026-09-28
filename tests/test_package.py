import ikigai


def test_protocol_version_is_pinned():
    # v8 is the ikigai-wire era this package mirrors (Conflict appended to the
    # typed errors), backward compatible down to v7 (hello required).
    assert ikigai.PROTOCOL_VERSION == 8
    assert ikigai.MIN_PROTOCOL_VERSION == 7
