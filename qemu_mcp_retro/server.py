"""qemu-mcp-retro - MCP server for headless retro x86 OS work.

Phase Q Stage 1 (this file): tool-surface skeleton with concrete
signatures, return types, and TODO blocks. Each tool returns a structured
NotImplementedError until wired to a real QMP session.

Run as MCP server (stdio transport):
    python3 -m qemu_mcp_retro

Register in ~/.claude.json:
    {
      "mcpServers": {
        "qemu-retro": {
          "command": "python3",
          "args": ["-m", "qemu_mcp_retro"]
        }
      }
    }

License: BSD-2-Clause.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from fastmcp import FastMCP
except ImportError as exc:
    raise SystemExit(
        "fastmcp not installed. Run: pip install --user fastmcp"
    ) from exc

# qemu.qmp is optional at scaffold time; tool stubs handle absence gracefully.
try:
    from qemu.qmp import QMPClient
    HAVE_QMP = True
except ImportError:
    HAVE_QMP = False


mcp = FastMCP("qemu-mcp-retro")


@dataclass
class Session:
    sid: str
    proc: subprocess.Popen
    qmp_sock: Path
    serial_sock: Path | None = None
    disks: list[str] = field(default_factory=list)
    started_at: float = 0.0


SESSIONS: dict[str, Session] = {}


# ---------------------------------------------------------------------------
# Tool 1: qemu_boot
# ---------------------------------------------------------------------------

@mcp.tool()
async def qemu_boot(
    machine: str = "pc",
    cpu: str = "486",
    memory_mb: int = 16,
    disks: list[str] | None = None,
    boot_order: str = "c",
    extra_args: list[str] | None = None,
    serial: str = "stdio",
    display: str = "none",
) -> dict[str, Any]:
    """Launch qemu-system-i386 with a QMP socket and return a session id.

    The launched QEMU exposes a QMP server at a UNIX socket whose path is
    returned. All other tools accept the session id and use the QMP
    connection from this session.

    Default machine is `-M pc -cpu 486 -m 16` (the canonical V7/x86
    profile). `disks` is a list of /path/to/image strings each attached
    as IDE.
    """
    sid = uuid.uuid4().hex[:12]
    tmp = Path(tempfile.mkdtemp(prefix=f"qemumcp-{sid}-"))
    qmp_sock = tmp / "qmp.sock"

    cmd: list[str] = [
        "qemu-system-i386",
        "-M", machine, "-cpu", cpu, "-m", str(memory_mb),
        "-boot", f"order={boot_order}",
        "-no-reboot", "-no-shutdown",
        "-display", display,
        "-qmp", f"unix:{qmp_sock},server,nowait",
        "-monitor", "null",
        "-serial", serial,
    ]
    for i, d in enumerate(disks or []):
        cmd += ["-drive", f"file={d},format=raw,if=ide,index={i},media=disk"]
    cmd += extra_args or []

    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Wait for the QMP socket to appear (max 5s).
    for _ in range(50):
        if qmp_sock.exists():
            break
        await asyncio.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError(
            f"qemu_boot: QMP socket did not appear: {qmp_sock}")

    sess = Session(sid=sid, proc=proc, qmp_sock=qmp_sock,
                   disks=list(disks or []), started_at=time.time())
    SESSIONS[sid] = sess
    return {"session_id": sid, "qmp_socket": str(qmp_sock),
            "pid": proc.pid, "cmd": cmd}


# ---------------------------------------------------------------------------
# Tool 2: qemu_sendkeys
# ---------------------------------------------------------------------------

# Minimal ASCII-to-QEMU-keycode map; expand as needed.
_ASCII_TO_QEMU_KEY: dict[str, str] = {
    " ": "spc", "\n": "ret", "\t": "tab",
    "-": "minus", "=": "equal",
    "[": "bracket_left", "]": "bracket_right",
    ";": "semicolon", "'": "apostrophe",
    ",": "comma", ".": "dot", "/": "slash",
    "\\": "backslash", "`": "grave_accent",
}


def _str_to_keys(s: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for c in s:
        if c.isalpha():
            if c.isupper():
                out.append({"type": "qcode", "data": f"shift-{c.lower()}"})
            else:
                out.append({"type": "qcode", "data": c})
        elif c.isdigit():
            out.append({"type": "qcode", "data": c})
        elif c in _ASCII_TO_QEMU_KEY:
            out.append({"type": "qcode", "data": _ASCII_TO_QEMU_KEY[c]})
        # Else: silently skip (extend map as needed).
    return out


@mcp.tool()
async def qemu_sendkeys(session_id: str, keys: str | list[str],
                        hold_ms: int = 30) -> dict[str, Any]:
    """Send keystrokes to the guest via QMP send-key.

    `keys` can be a string ("ls\\n") or an explicit list of QEMU
    key names (["l", "s", "ret"]).
    """
    sess = SESSIONS.get(session_id)
    if not sess:
        raise ValueError(f"unknown session_id: {session_id}")
    if isinstance(keys, str):
        key_arr = _str_to_keys(keys)
    else:
        key_arr = [{"type": "qcode", "data": k} for k in keys]
    return await _qmp(sess, "send-key",
                      keys=key_arr, **({"hold-time": hold_ms} if hold_ms else {}))


# ---------------------------------------------------------------------------
# Tool 3: qemu_screendump_png
# ---------------------------------------------------------------------------

@mcp.tool()
async def qemu_screendump_png(session_id: str, host_path: str) -> dict[str, Any]:
    """Dump the current guest framebuffer to a PNG file on the host.

    Uses QMP `screendump` with `format: png` if the QEMU build supports
    it; otherwise falls back to PPM and converts via ImageMagick.
    """
    sess = SESSIONS.get(session_id)
    if not sess:
        raise ValueError(f"unknown session_id: {session_id}")
    try:
        result = await _qmp(sess, "screendump",
                            **{"filename": host_path, "format": "png"})
        return {"path": host_path, "format": "png", "qmp": result}
    except Exception:
        # Fallback: ppm + convert
        ppm = host_path + ".ppm"
        await _qmp(sess, "screendump", filename=ppm)
        subprocess.run(["magick", ppm, host_path], check=True)
        os.unlink(ppm)
        return {"path": host_path, "format": "png-via-ppm"}


# ---------------------------------------------------------------------------
# Tool 4: qemu_extract_fs_file (stub: not yet implemented)
# ---------------------------------------------------------------------------

@mcp.tool()
async def qemu_extract_fs_file(session_id: str, guest_path: str,
                               host_path: str) -> dict[str, Any]:
    """Extract a file from the running guest's filesystem to the host.

    Strategy: hot-plug an empty raw disk via QMP `blockdev-add`, drive
    the guest (via serial) to `dd` the file onto that disk, detach the
    disk, then read the file out by parsing the V7 fs structures on the
    host side using tools/host-v7put / tools/aout/.

    Phase Q stage 2 (not implemented in scaffold).
    """
    raise NotImplementedError(
        "qemu_extract_fs_file: stage-2 implementation pending. "
        "Workaround: attach a second disk at boot time and have the guest "
        "write to it with `dd if=<file> of=/dev/hd1` then read host-side.")


# ---------------------------------------------------------------------------
# Tool 5: qemu_diff_disk_sectors
# ---------------------------------------------------------------------------

@mcp.tool()
async def qemu_diff_disk_sectors(image_a: str, image_b: str,
                                 lba_start: int = 0,
                                 lba_count: int = 0) -> dict[str, Any]:
    """Compare two raw disk images sector-by-sector and report diffs.

    If `lba_count == 0`, diffs the whole pair using `qemu-img compare`.
    Otherwise reads the named range from each and returns a list of
    (offset, byte_a, byte_b) tuples for the first 100 differing bytes.
    """
    if lba_count == 0:
        result = subprocess.run(
            ["qemu-img", "compare", image_a, image_b],
            capture_output=True, text=True)
        return {"qemu_img_exit": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip()}
    with open(image_a, "rb") as fa, open(image_b, "rb") as fb:
        fa.seek(lba_start * 512)
        fb.seek(lba_start * 512)
        nbytes = lba_count * 512
        a = fa.read(nbytes)
        b = fb.read(nbytes)
    diffs = [(i + lba_start * 512, a[i], b[i])
             for i in range(min(len(a), len(b)))
             if a[i] != b[i]][:100]
    return {"lba_start": lba_start, "lba_count": lba_count,
            "diffs": diffs, "total_diff_count": sum(1 for x, y in zip(a, b) if x != y)}


# ---------------------------------------------------------------------------
# Tool 6: qemu_qmp (raw passthrough)
# ---------------------------------------------------------------------------

@mcp.tool()
async def qemu_qmp(session_id: str, command: str,
                   args: dict[str, Any] | None = None) -> Any:
    """Raw QMP command passthrough. Escape hatch for anything not wrapped
    by a dedicated tool."""
    sess = SESSIONS.get(session_id)
    if not sess:
        raise ValueError(f"unknown session_id: {session_id}")
    return await _qmp(sess, command, **(args or {}))


# ---------------------------------------------------------------------------
# Tool 7: qemu_kill
# ---------------------------------------------------------------------------

@mcp.tool()
async def qemu_kill(session_id: str, mode: str = "graceful",
                    timeout_s: int = 10) -> dict[str, Any]:
    """Shut the QEMU session down.

    `mode=graceful` issues QMP `system_powerdown` then `quit`, waiting
    `timeout_s` for natural exit before SIGKILL.
    `mode=hard` skips the QMP shutdown and SIGKILLs immediately.
    """
    sess = SESSIONS.get(session_id)
    if not sess:
        raise ValueError(f"unknown session_id: {session_id}")
    if mode == "graceful":
        try:
            await _qmp(sess, "system_powerdown")
        except Exception:
            pass
        try:
            await _qmp(sess, "quit")
        except Exception:
            pass
        try:
            sess.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            sess.proc.kill()
    else:
        sess.proc.kill()
    code = sess.proc.poll()
    del SESSIONS[session_id]
    return {"session_id": session_id, "exit_code": code, "mode": mode}


# ---------------------------------------------------------------------------
# Internal: QMP client
# ---------------------------------------------------------------------------

async def _qmp(sess: Session, command: str, **kwargs: Any) -> Any:
    if not HAVE_QMP:
        raise RuntimeError(
            "qemu.qmp not installed. pip install --user 'qemu.qmp>=0.0.3'")
    client = QMPClient(name=f"qemu-mcp-retro-{sess.sid}")
    await client.connect(str(sess.qmp_sock))
    try:
        return await client.execute(command, arguments=kwargs or None)
    finally:
        await client.disconnect()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
