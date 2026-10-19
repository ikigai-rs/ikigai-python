import ikigai


def test_protocol_version_is_pinned():
    # v9 is the ikigai-wire era this package mirrors (IssueWith, error tags 9-13),
    # backward compatible down to v7 (hello required).
    assert ikigai.PROTOCOL_VERSION == 9
    assert ikigai.MIN_PROTOCOL_VERSION == 7
