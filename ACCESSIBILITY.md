# Accessibility

What `ikigai-python` emits that people read (error messages, a plain-text description face, a few
console lines, and the HTML of its examples), the rules that output follows, how it is tested,
and how to report a barrier. It is not a conformance claim. Adapted from the
[ACCESSIBILITY.md template](https://github.com/mgifford/ACCESSIBILITY.md) (MIT).

| Field | Value |
|---|---|
| Target | WCAG 2.2 Level AA for the HTML this repository's examples write; the published package writes text only |
| Status | Evaluation in progress. No conformance claim. |
| Owner | the maintainers of this repository |
| Report a barrier | [a GitHub issue](https://github.com/ikigai-rs/ikigai-python/issues/new?labels=accessibility) with the `accessibility` label; anything security-sensitive through [private vulnerability reporting](https://github.com/ikigai-rs/ikigai-python/security/advisories/new) |
| Last reviewed | 2026-10-31 |

## What it emits that people read

The published package (`src/ikigai/`):

- **Errors.** The typed failures in `wire.py` (`DeniedError`, `NotFoundError`, `ConflictError`,
  and the rest), whose `display()` prints the error as the Rust kernel's `Display` does; the
  QUIC errors in `quic.py` (`CertSetError`, `PinMismatchError`, `ClientRefusedError`,
  `NotEnrolledError`); `AuthError` in `auth.py`, which names the broken rule by a stable code and
  never holds the credential; and the protocol, decode and template errors (`ProtocolError`,
  `DecodeError` in `postcard.py`, `TemplateError` in `template.py`).
- **A plain-text description.** A served endpoint's Meta face, asked for as `text/plain`
  (`description_text` in `serve.py`): its id and title, its verbs, one line per input
  (`input name [source] (optional): summary`), its argument groups, and its outputs.
- **Console lines.** `serve` writes one line to stderr when it refuses a client that sent no
  version hello, naming both protocol versions and ending `Update the client.`;
  `python -m ikigai.demo` writes two, what it serves and where, and a command to try.
- **In a notebook**, results show as ordinary Python values; a `Representation`'s `repr` is its
  media type, its size and its expiry, never its body. The package defines no rich (HTML)
  display.
- **HTML helpers, not HTML.** `html.py` escapes text and attributes and checks link targets for a
  caller that writes HTML; it writes no page itself.

The examples (`examples/`, not shipped in the wheel):

- `examples/tictactoe_app.py` serves a page around the tic-tac-toe game: the document shell (its
  `page` function), the vendored `static/host.css` and `static/ttt.css`, and `static/conflict.js`,
  which puts a refused move's sentence into the board's status region as text.
- `examples/fasthtml_app.py` serves a page of forms and HTML fragments; `examples/litestar_app.py`
  and `examples/falcon_app.py` answer in plain text and JSON.

Not this repository's: the game's own views (`view:game`, the board, the status region) and the
stylesheets, which come from the ikigai tutorial's templates and are vendored or resolved as they
are; FastHTML's own page markup; and the text of a failure a remote kernel sends, which this
package carries unchanged.

## The rules that output follows

- **A refusal reads as the Rust kernel prints it.** `display()` gives the same words for the same
  failure in every implementation, and an `AuthError` names the rule that was broken.
- **A QUIC refusal names what to look at.** A missing or unusable certificate file names its path
  and what it should hold (`server.crt is missing: it should hold …`); a pin mismatch names the
  door, both fingerprints, and that nothing was sent to it.
- **The tic-tac-toe page shell** has `<html lang="en">`, a viewport meta, a `<title>` and one
  `<h1>` naming the game, one `<main>` holding the game and a `<nav aria-label="Games">` of links
  with a heading; its stylesheet (`host.css`) is light by default and dark under
  `prefers-color-scheme: dark`, and its comment states that text and links clear 4.5:1 and rules
  3:1.
- **A refused move is announced as text.** `conflict.js` writes the `409` answer into the status
  region with `textContent`, never as HTML, and the board is left as it was.
- **No color is written** anywhere in the package or its console lines.

The page shell's `lang`, `main` and `nav` and the stylesheet's contrast are followed by reading;
the tests below pin the rest.

## How it is tested

- `tests/test_failure_classes.py` (`test_kind_class_transient_and_display`, one case per row) pins
  every typed error's `display()` against `tests/vectors/failure_class_vectors.json`, a verbatim
  copy of the vectors the Rust kernel's vocabulary crate commits;
  `tests/test_tictactoe_app.py` (`test_a_refusal_is_said_as_the_rust_kernel_displays_it`) pins the
  same forms where the example shows them.
- `tests/test_quic.py` (`test_a_missing_file_names_itself`,
  `test_a_key_from_another_pair_is_refused_before_dialing`) pins two QUIC refusals' words;
  `tests/test_auth.py` (`test_no_credential_in_repr_or_errors`) pins that no credential reaches an
  error.
- `tests/test_serve.py` (`test_meta_faces`), `tests/test_families.py` and
  `tests/test_serve_shape.py` pin lines of the plain-text description.
- `tests/test_html.py` pins the escaping and link rules against the shared HTML vectors;
  `tests/test_tictactoe_app.py` pins that the page loads `conflict.js` and that it writes with
  `textContent` (`test_the_page_loads_the_script_that_swaps_a_409_into_the_status_region`), that a
  hostile mark is escaped, and, when a `ttt-host` binary is present, that the page and its files
  are the host's byte for byte.
- **No automated accessibility checker, browser or assistive technology has been used on this
  output.** No test pins the page shell's `lang`, `main` or `nav`, and the stylesheet's contrast
  is a comment, not a measurement made here.

## Known limitations

Found by reading the source on 2026-10-31, not by testing with people or assistive technology:

- The FastHTML example's page (`examples/fasthtml_app.py`) passes no `htmlkw`, so FastHTML writes
  `<html>` with no `lang`; its inputs are named only by a `placeholder`, with no `<label>`; and
  the region a result is swapped into is not a live region, so a screen reader is not told a
  result arrived.
- Decode and protocol errors are fragments that do not say what to do (`varint too long`,
  `invalid Option tag 0x…`); they are meant for implementors, but they reach whoever calls the
  client.
- The plain-text description writes an input with no summary as a line ending in a bare colon.

## Contributing

Read this file before changing any output listed above: an error's words, the description face,
a console line, or an example's page. Keep meaning in words, keep `display()` equal to the shared
vectors, and keep the page shell's `lang`, `main` and headings. Say in the pull request what was
tested, with what, and what was not.

## Reporting a barrier

[Open an issue](https://github.com/ikigai-rs/ikigai-python/issues/new?labels=accessibility) with
the `accessibility` label (the link sets it when GitHub lets you add labels; otherwise a
maintainer adds it): what you were doing, what happened, and your browser, terminal or notebook
and any assistive technology, if you are comfortable sharing them. Anything security-sensitive
goes through
[private vulnerability reporting](https://github.com/ikigai-rs/ikigai-python/security/advisories/new).
**We acknowledge a report within 7 days**, and promise nothing further.

## Change history

| Date | Change |
|---|---|
| 2026-10-31 | First version. |
