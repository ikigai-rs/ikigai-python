import ikigai


def test_protocol_version_is_pinned():
    # v5 is the ikigai-wire era this package mirrors (core 0.1.48 TraceEvent.notes).
    assert ikigai.PROTOCOL_VERSION == 5
