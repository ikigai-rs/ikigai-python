# Changelog

## Unreleased

- **The served floor conceals an existence-sensitive door** (ledger #2889). A refused caller
  of a door marked `existence_sensitive` now gets exactly the miss a name bound nowhere gets,
  `UnresolvedError` naming the target (the same bytes on the wire), never `DeniedError`, so it
  cannot learn that the name exists: the leak ikigai-core 0.2.2 closed in the kernel (ledger
  #2176). The rule is core's `Floor::conceals`, mirrored as `ikigai.serve.conceals`: a
  declared verb conceals when its own action is marked (an explicit action's flag replaces the
  flat one), an undeclared verb whenever anything is marked, and `Meta` when the caller passes
  the floor of no declared verb. `@endpoint` and the family verb decorators take
  `existence_sensitive=True`, which reaches the JSON Meta face, the contract digest and the
  Turtle (`ik:existenceSensitive true`). `tests/vectors/capability_vectors.json` is core main's
  file (ikigai-rs/ikigai-core PR 295), verbatim: its 23 `sensitive-*` denial rows, 12 of them
  concealed, failed nine ways here before this change.
- **CI `interop` job pins `IKIGAI_CLI_VERSION` 0.2.8**, the current cli, up from 0.1.67, so CI
  tests the door people run (ledger #2857; ikigai-deno made the same move). The whole suite
  under `--require-interop` passed unchanged locally against cli 0.2.3 with its `quic` feature
  and a `ttt-host` built at `TTT_HOST_REV`: no test asserted a 0.1.x behavior the 0.2 door
  changed. The `DIGESTS` comment in `tests/test_tictactoe_app.py` now names `TTT_HOST_REV` as
  the commit the vendored files match, not tutorial 89677bc, and counts four files, not three.
- **The tic-tac-toe parity test runs in CI** (ledger #2856). It drives ikigai-tutorial's
  `ttt-host`, not the `ikigai` cli, and no CI job had one, so it skipped there whatever the cli
  pin. The `interop` job now builds `ttt-host` at a pinned ikigai-tutorial commit
  (`TTT_HOST_REV`, cached by that commit and the runner OS) and names it as `TTT_HOST`, and
  under `--require-interop` a missing `ttt-host`, or one speaking another wire version, FAILS
  with a sentence saying how to build one. The host is found as the Deno face finds it:
  `$TTT_HOST`, then `~/.local/ttt-host/bin/ttt-host`, then `PATH`.
- **One stale assertion fixed, on the face's side.** Against `ttt-host` at that commit, the
  all-in-Python-but-the-middle test asserted that a second board read was answered from the
  host's cache. Since `ikigai-resolve` 0.1.64 a mount's answers are uncacheable in the host (a
  cut on the peer never crosses back), so a game over a peer store caches nothing there; the
  test now asserts the store is asked again, and the README and the app's docstring say so.
  The refused-play `Conflict`, the off-board text and the strict `{id}` route were already up
  to date.
- `ikigai.html.percent_decode` (text) and `percent_decode_bytes`: strict percent decoders
  mirroring `ikigai_fn::html` 0.9.0. Each `%XX` decodes exactly once in either hex case, `+` is
  a space only when asked, and `%2B` stays a plus. A malformed escape raises
  `PercentDecodeError` (kind `malformed_escape`, with its input byte offset) from both; decoded
  bytes that are not UTF-8 raise it (kind `invalid_utf8`) from the text form only.
  `tests/vectors/html_vectors.json` is ikigai-fn's file at v0.9.0, verbatim, and every
  `percent_decode` case and the `url_segment` round trip in both plus modes run (ledger #2837).
