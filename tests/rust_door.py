"""A real Rust QUIC door on loopback, for the interop tests: ``ikigai serve quic://``.

Started from the installed ``ikigai`` binary on ``127.0.0.1`` port 0 (the door binds first and
names the port it got in its banner, so nothing picks a port by bind-and-release), with a
scratch cert set from ``ikigai cert generate``, a scratch config home and a scratch file
root, so it reads nothing of this machine's own configuration and writes nothing outside the
test's directory. Stopped by its PID.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

IKIGAI = shutil.which("ikigai")
BANNER = re.compile(r"serving on (quic://127\.0\.0\.1:\d+)")


def require_ikigai(request) -> str:
    """The ``ikigai`` binary, or skip; under ``--require-interop`` a missing one FAILS."""
    if IKIGAI is None:
        if request.config.getoption("--require-interop"):
            pytest.fail("--require-interop, and there is no `ikigai` on PATH")
        pytest.skip("no `ikigai` binary on PATH (the interop tests run against the Rust door)")
    return IKIGAI


def generate(ikigai: str, directory: Path) -> Path:
    """``ikigai cert generate --dir directory``: server.{crt,key} and client.{crt,key}."""
    done = subprocess.run(
        [ikigai, "cert", "generate", "--dir", str(directory)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if done.returncode != 0:
        pytest.fail(f"`ikigai cert generate` failed (built without `quic`?): {done.stderr}")
    return directory


class RustDoor:
    """``ikigai --config-home HOME --files FILES serve quic://127.0.0.1:0 --cert-dir CERTS``
    plus ``extra`` arguments. :attr:`url` is the door; :attr:`log` what it printed."""

    def __init__(self, ikigai: str, root: Path, certs: Path, *, home: Path | None = None, extra=()):
        self.home = home or root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        files = root / "files"
        files.mkdir(parents=True, exist_ok=True)
        argv = [
            ikigai,
            "--config-home",
            str(self.home),
            "--files",
            str(files),
            "serve",
            "quic://127.0.0.1:0",
            "--cert-dir",
            str(certs),
            *extra,
        ]
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.lines: list[str] = []
        found = threading.Event()
        self.url: str | None = None

        def drain():
            for line in self.process.stdout:
                self.lines.append(line.rstrip("\n"))
                m = BANNER.search(line)
                if m and self.url is None:
                    self.url = m.group(1)
                    found.set()
            found.set()

        threading.Thread(target=drain, daemon=True).start()
        deadline = time.monotonic() + 30
        while not found.wait(0.1):
            if time.monotonic() > deadline:
                break
        if self.url is None:
            self.stop()
            pytest.fail("the Rust door did not come up:\n" + self.log)

    @property
    def log(self) -> str:
        """Everything the door printed so far."""
        return "\n".join(self.lines)

    def stop(self) -> None:
        """Terminate the door by its PID and reap it."""
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
