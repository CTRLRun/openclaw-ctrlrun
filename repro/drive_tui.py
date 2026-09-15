"""Drive OpenClaw's TUI through a pty so the agent turn has an approval route.

`openclaw agent` from a shell has no turnSourceChannel, so the host answers
plugin.approval.request with decision: null and reports "no approval route". The TUI
connects as an approval-capable client, so a turn started inside it can hold an approval
open for a person to answer.
"""

from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import time

PROMPT = "read the file /tmp/ctrlrun-probe.txt"
LOG = "/tmp/tui.log"


def main() -> int:
    master, slave = pty.openpty()
    os.environ["CTRLRUN_OPENCLAW_TOKEN"] = "probe-token-local"
    os.environ["TERM"] = "xterm-256color"
    proc = subprocess.Popen(
        ["openclaw", "--profile", "gap", "tui", "--token", "gap-token"],
        stdin=slave, stdout=slave, stderr=slave,
        close_fds=True,
    )
    os.close(slave)

    out = open(LOG, "wb")
    deadline = time.time() + 25
    seen = b""

    # Let the TUI connect and draw before typing into it.
    while time.time() < deadline:
        r, _, _ = select.select([master], [], [], 0.5)
        if r:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            out.write(chunk); out.flush()
            seen += chunk
        if b"\x1b[" in seen and len(seen) > 2000:
            break

    print(f"[tui] connected, {len(seen)} bytes drawn", flush=True)
    time.sleep(3)

    os.write(master, PROMPT.encode())
    time.sleep(1.0)
    os.write(master, b"\r")
    print("[tui] prompt sent", flush=True)

    # Stay alive so the session, and its approval route, persist.
    end = time.time() + 240
    while time.time() < end and proc.poll() is None:
        r, _, _ = select.select([master], [], [], 1.0)
        if r:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            out.write(chunk); out.flush()
    proc.terminate()
    out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
