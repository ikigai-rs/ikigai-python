# Changelog

## Unreleased

- `ikigai.html.percent_decode` (text) and `percent_decode_bytes`: strict percent decoders
  mirroring `ikigai_fn::html` 0.9.0. Each `%XX` decodes exactly once in either hex case, `+` is
  a space only when asked, and `%2B` stays a plus. A malformed escape raises
  `PercentDecodeError` (kind `malformed_escape`, with its input byte offset) from both; decoded
  bytes that are not UTF-8 raise it (kind `invalid_utf8`) from the text form only.
  `tests/vectors/html_vectors.json` is ikigai-fn's file at v0.9.0, verbatim, and every
  `percent_decode` case and the `url_segment` round trip in both plus modes run (ledger #2837).
