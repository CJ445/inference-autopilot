#!/usr/bin/env python3
"""Record the README demos: drive the real `aiops` in a pseudo-terminal, write asciinema casts and
render GIFs into docs/media/. Everything is the SIMULATION: nothing real is touched. The TUI demos
use a throwaway HOME, a profile on a spare port and a workload-free control plane.

Needs: pexpect, the built TUI, `aiops` on PATH, and `agg` (https://github.com/asciinema/agg).
Usage: scripts/record_demos.py [name ...]      (default: all)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pexpect
import pyte

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "docs" / "media"
COLS, ROWS = 120, 32
PORT = 18080


class Recorder:
    """A pty session whose output is written, timestamped, as an asciinema v2 cast."""

    def __init__(self, path, env, cwd):
        self.f = open(path, "w")
        self.f.write(json.dumps({"version": 2, "width": COLS, "height": ROWS,
                                 "env": {"TERM": "xterm-256color"}}) + "\n")
        self.t0 = time.monotonic()
        self.env, self.cwd = env, cwd
        self.child = None
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.Stream(self.screen)

    def emit(self, text):
        self.stream.feed(text)
        self.f.write(json.dumps([round(time.monotonic() - self.t0, 3), "o", text]) + "\n")

    def type(self, text, delay=0.045):
        for ch in text:
            self.emit(ch)
            time.sleep(delay)

    def run(self, cmd):
        self.emit("\x1b[1;32m$\x1b[0m ")
        self.type(cmd)
        time.sleep(0.4)
        self.emit("\r\n")
        self.child = pexpect.spawn("/bin/bash", ["-c", cmd], env=self.env, cwd=self.cwd,
                                   dimensions=(ROWS, COLS), encoding="utf-8", timeout=120)
        return self

    def wait_for(self, pattern, timeout=60):
        """Pump output into the cast until `pattern` appears."""
        end = time.monotonic() + timeout
        buf = ""
        while time.monotonic() < end:
            try:
                chunk = self.child.read_nonblocking(65536, timeout=0.2)
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                if pattern is None:
                    return buf
                raise RuntimeError(f"ended before {pattern!r}; screen:\n"
                                   + "\n".join(self.screen.display))
            self.emit(chunk)
            buf += chunk
            if pattern and pattern in " ".join(self.screen.display):
                return buf
        raise RuntimeError(f"timed out waiting for {pattern!r}; screen:\n"
                           + "\n".join(self.screen.display))

    def pause(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                self.emit(self.child.read_nonblocking(65536, timeout=0.1))
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                return

    def key(self, k):
        self.child.send(k)

    def finish(self):
        self.pause(1.5)
        if self.child and self.child.isalive():
            self.child.terminate(force=True)
        self.f.close()


def _env(home):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL")}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), TERM="xterm-256color",
               COLORTERM="truecolor", PS1="$ ", PATH=f"{Path.home()}/.local/bin:{env['PATH']}")
    return env


def demo_cli(name, flag):
    def record(work, cast):
        r = Recorder(cast, _env(work / "home"), work / "cwd").run(f"aiops demo {flag}")
        r.wait_for(None)
        r.finish()
    return record


def profile(work):
    text = (ROOT / "deploy/profiles/docker-real-gpu.toml").read_text()
    path = work / "home" / ".config" / "aiops" / "aiops.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text.replace("port = 8080", f"port = {PORT}"))


def demo_tui_recovery(work, cast):
    profile(work)
    r = Recorder(cast, _env(work / "home"), work / "cwd").run("aiops")
    r.wait_for("Overview", 90)
    r.pause(2.5)
    r.key("3")                                   # Lab
    r.pause(1.5)
    r.key("\r")                                  # run "Model becomes unresponsive"
    r.wait_for("needs your decision", 120)
    r.pause(3)
    r.key("a")                                   # open the request
    r.pause(3)
    r.key("\r")                                  # confirm exactly that action
    r.wait_for("INCIDENT RESOLVED", 120)
    r.pause(4)                                   # end on the resolved screen, not on the shell
    r.finish()


def demo_tui_palette(work, cast):
    profile(work)
    r = Recorder(cast, _env(work / "home"), work / "cwd").run("aiops")
    r.wait_for("Overview", 90)
    r.pause(2)
    r.key("\x10")                                # Ctrl+P
    r.pause(3)
    r.key("\x1b")
    r.pause(0.8)
    r.key("?")
    r.pause(3.5)
    r.key("\x1b")
    r.pause(0.8)
    for space in "5":
        r.key(space)
        r.pause(3)
    r.finish()


DEMOS = {
    "demo-approve": demo_cli("demo-approve", "--approve"),
    "demo-reject": demo_cli("demo-reject", "--reject"),
    "tui-recovery": demo_tui_recovery,
    "tui-tour": demo_tui_palette,
}


def main(names):
    agg = shutil.which("agg") or str(Path.home() / ".local/bin/agg")
    MEDIA.mkdir(parents=True, exist_ok=True)
    for name in names or DEMOS:
        work = Path(tempfile.mkdtemp(prefix="aiops-rec-"))
        (work / "home").mkdir()
        (work / "cwd").mkdir()
        cast = MEDIA / f"{name}.cast"
        print(f"recording {name} ...", flush=True)
        try:
            DEMOS[name](work, cast)
        finally:
            subprocess.run(["aiops", "stop", "--config",
                            str(work / "home/.config/aiops/aiops.toml")],
                           capture_output=True, env=_env(work / "home"))
            shutil.rmtree(work, ignore_errors=True)
        subprocess.run([agg, "--font-size", "16", "--idle-time-limit", "2", "--last-frame-duration",
                        "3", str(cast), str(MEDIA / f"{name}.gif")], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cast.unlink()
        print(f"  -> docs/media/{name}.gif", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
