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
| Last reviewed | 2026-11-12 |

## What it emits that people read

The published package (`src/ikigai/`):

- **Errors.** The typed failures in `wire.py` (`DeniedError`, `NotFoundError`, `ConflictError`,
  and the rest), whose `display()` prints the error as the Rust kernel's `Display` does; the
  QUIC errors in `quic.py` (`CertSetError`, `PinMismatchError`, `ClientRefusedError`,
  `NotEnrolledError`); `AuthError` in `auth.py`, which names the broken rule by a stable code and
  never holds the credential; and the protocol, decode and template errors (`ProtocolError`,
  `DecodeError` in `postcard.py`, `TemplateError` in `template.py`); and `PercentDecodeError` in
  `html.py`, worded as `ikigai_fn::html`'s (`percent_decode: malformed percent-escape at byte 3`,
  `percent_decode: decoded bytes are not UTF-8`), with its kind and offset as attributes.
  `FormPairsError` uses the Rust form parser's words (`form_pairs: malformed
  percent-escape at byte 3`, `form_pairs: decoded bytes are not UTF-8 at byte 2`),
  with the decode kind, original-input byte offset and underlying error as attributes.
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
  caller that writes HTML, and decodes percent escapes strictly; it writes no page itself.

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
- **The FastHTML example** declares English with `<html lang="en">` on its index and catalog
  pages. Each form input has a visible, explicitly associated label and a unique id; each
  result target is a persistent `aria-live="polite"` region for incoming result fragments.
- **A refused move is announced as text.** `conflict.js` writes the `409` answer into the status
  region with `textContent`, never as HTML, and the board is left as it was.
- **No color is written** anywhere in the package or its console lines.

## How it is tested

Every named check has a result: Pass, Fail or Blocked. A Pass covers only the stated
assertions, not overall accessibility or WCAG conformance. Unrun checks remain Blocked.
Historical checks that CI does not repeat must be Blocked with their date.

The automated checks below ran on 2026-11-12 against unchanged `origin/main`, commit
`6902e3e55af9faec340aadd639739b46041be5df`, before this documentation update, using Python
3.12.3 and pytest 9.2.0. From an environment installed with
`python -m pip install -e '.[dev,examples,quic]'`, run:

```sh
python -m pytest -q tests/test_failure_classes.py tests/test_tictactoe_app.py tests/test_quic.py tests/test_auth.py tests/test_serve.py tests/test_families.py tests/test_serve_shape.py tests/test_html.py tests/test_examples_fasthtml.py -ra
```

Result: 721 passed, 15 skipped. All 15 skips require `ttt-host`; they are not Pass results.
These tests run in CI through `scripts/gates.sh`; the separate interop job supplies the Rust
binaries and runs the whole suite with `--require-interop`. A headless HTTP test client is
not a browser: it does not render CSS, execute JavaScript or exercise assistive technology.

| Check | Result | Evidence |
|---|---|---|
| Typed failure wording and example refusal text | Pass | Command above: `tests/test_failure_classes.py::test_kind_class_transient_and_display` checks shared `tests/vectors/failure_class_vectors.json`; `tests/test_tictactoe_app.py::test_a_refusal_is_said_as_the_rust_kernel_displays_it` checks displayed refusals. Checks wording parity, not whether people understand the errors. |
| QUIC refusal wording | Pass | Command above: `tests/test_quic.py::test_a_missing_file_names_itself` and `test_a_key_from_another_pair_is_refused_before_dialing`, aioquic 1.7.2. Checks those two refusals, not every transport error. |
| Credentials excluded from authentication errors | Pass | Command above: `tests/test_auth.py::test_no_credential_in_repr_or_errors`. Checks the tested credential in representations and errors, not usability of recovery instructions. |
| Plain-text description lines | Pass | Command above: `tests/test_serve.py::test_meta_faces`, `tests/test_families.py` and `tests/test_serve_shape.py`. Checks description content and shape; no terminal or screen reader evaluation. |
| HTML escaping, safe links and strict percent/form decoding | Pass | Command above: `tests/test_html.py`, including `test_percent_decode_error_words` and `test_form_pairs_error_words_and_source`, against shared HTML vectors. Checks helper behavior, error words, offsets and causes; does not evaluate rendered pages. |
| Refused-move script loading and text insertion | Pass | Command above: `tests/test_tictactoe_app.py::test_the_page_loads_the_script_that_swaps_a_409_into_the_status_region`. Checks script inclusion and `textContent` in source; does not execute it or verify an announcement. |
| Hostile game mark escaping and vendored file integrity | Pass | Command above: `tests/test_tictactoe_app.py::test_a_hostile_mark_is_escaped_and_never_expanded` and `test_the_vendored_files_are_the_books_byte_for_byte`. Checks escaping and pinned file hashes, not accessibility of the upstream game templates. |
| Page, views and static files match the running Rust host | Blocked | The 15 host-backed cases in `tests/test_tictactoe_app.py` skipped locally on 2026-11-12 because `ttt-host` was unavailable. CI's interop job runs these with the pinned host; no local parity result is claimed. |
| FastHTML page language, input labels, unique ids and persistent polite result regions | Pass | Command above: `tests/test_examples_fasthtml.py::test_pages_declare_english` and `test_each_form_input_has_a_visible_label_and_a_live_result`; python-fasthtml 0.14.23, Starlette 1.10.0 TestClient, HTTPX 0.28.1. Checks returned markup, not browser behavior, focus or live announcements. |
| Tic-tac-toe shell language, viewport, title, headings and landmarks | Blocked | Source reading of `examples/tictactoe_app.py::page` describes these rules; no dedicated shell accessibility assertion or rendered semantic evaluation was run. |
| Light/dark theme contrast (text and links 4.5:1, rules 3:1) | Blocked | `examples/static/host.css` states these ratios in a comment. No contrast measurement was run here; the comment and file hash are not contrast evidence. |
| Package/console output without color; demo text and notebook representation | Blocked | Described from source; no terminal or notebook accessibility session was available for this review. No claim about assistive-technology reading order or presentation. |
| Automated accessibility checker (axe or equivalent) | Blocked | No browser accessibility checker was configured or run for this review. The pytest assertions above are not an automated WCAG audit. |
| Browser rendering, reflow/zoom, focus and dynamic status announcements | Blocked | No browser evaluation was run for this review; HTTP test clients do not cover rendering or JavaScript. |
| Manual keyboard navigation | Blocked | No person performed a keyboard pass; no interactive browser session was available for this review. |
| Screen reader | Blocked | No screen reader session was available; none was used on this output for this review. |
| Screen magnifier | Blocked | No magnifier session was available; none was used on this output for this review. |
| Speech input | Blocked | No speech-input session was available; none was used on this output for this review. |
| Switch access | Blocked | No switch-access session was available; none was used on this output for this review. |
| Braille display | Blocked | No braille display was available; none was used on this output for this review. |
| Actionable decode/protocol error text | Fail | Source review on 2026-11-12: `src/ikigai/postcard.py` still raises `varint too long` and `invalid Option tag 0x…` without recovery instructions. This is the known wording limitation below, not a measured WCAG failure. |
| Description of an input without a summary | Fail | Source review on 2026-11-12: `src/ikigai/serve.py::_Door.description_text` interpolates an empty `arg.summary` after the colon. This is the known content limitation below, not a measured WCAG failure. |

## Known limitations

Found by reading the source on 2026-10-31 and confirmed by source review on 2026-11-12,
not by testing with people or assistive technology:

- Decode and protocol errors are fragments that do not say what to do (`varint too long`,
  `invalid Option tag 0x…`); they are meant for implementors, but they reach whoever calls the
  client.
- The plain-text description writes an input with no summary as a line ending in a bare colon.

## Contributing

Read this file before changing any output listed above: an error's words, the description face,
a console line, or an example's page. Keep meaning in words, keep `display()` equal to the shared
vectors, and keep the page shell's `lang`, `main` and headings. Say in the pull request what was
tested, with what, and what was not. In the pull request, give every named check a
`Check | Result | Evidence` row with Pass, Fail or Blocked and the test or command, tool and
version, or why it could not run. Keep unused assistive technologies Blocked. A historical
run CI does not repeat is Blocked with its date, never a current Pass.

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
| 2026-11-07 | FastHTML pages declare English; forms have explicit labels and polite live result regions, pinned by headless tests. |
| 2026-11-12 | Record Pass, Fail and Blocked results with current main test evidence and explicit browser and assistive-technology gaps. |
