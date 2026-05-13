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


def _str_to_keystrokes(s: str) -> list[list[dict[str, str]]]:
    """Each element of the outer list is a single send-key call (one
    chord: all keys pressed simultaneously). Uppercase letters and
    shift-decorated chars produce 2-key chords (shift + base)."""
    out: list[list[dict[str, str]]] = []
    for c in s:
        if c.isalpha():
            if c.isupper():
                out.append([
                    {"type": "qcode", "data": "shift"},
                    {"type": "qcode", "data": c.lower()},
                ])
            else:
                out.append([{"type": "qcode", "data": c}])
        elif c.isdigit():
            out.append([{"type": "qcode", "data": c}])
        elif c in _ASCII_TO_QEMU_KEY:
            out.append([{"type": "qcode", "data": _ASCII_TO_QEMU_KEY[c]}])
        elif c in SHIFT:
            out.append([
                {"type": "qcode", "data": "shift"},
                {"type": "qcode", "data": SHIFT[c]},
            ])
        # Else: silently skip (extend map as needed).
    return out


SHIFT: dict[str, str] = {
    '!': '1', '@': '2', '#': '3', '$': '4', '%': '5',
    '^': '6', '&': '7', '*': '8', '(': '9', ')': '0',
    '_': 'minus', '+': 'equal',
    '{': 'bracket_left', '}': 'bracket_right',
    ':': 'semicolon', '"': 'apostrophe',
    '<': 'comma', '>': 'dot', '?': 'slash',
    '|': 'backslash', '~': 'grave_accent',
}


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
    results = []
    if isinstance(keys, str):
        chords = _str_to_keystrokes(keys)
    else:
        chords = [[{"type": "qcode", "data": k}] for k in keys]
    for chord in chords:
        r = await _qmp(
            sess, "send-key",
            keys=chord,
            **({"hold-time": hold_ms} if hold_ms else {}),
        )
        results.append(r)
    return {"chords_sent": len(chords)}


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
async def qemu_attach_scratch_disk(session_id: str, host_path: str,
                                   size_mb: int = 4,
                                   device_name: str = "scratch0") -> dict[str, Any]:
    """Hot-attach an empty raw disk to a running guest.

    The disk is created at host_path (host-visible) with the specified
    size and attached as an IDE drive. From inside the guest, write to
    /dev/hd1c (or equivalent) with `dd`. After detach, the host file
    contains the written bytes at offset 0.

    Use case: file extraction. Run this, then qemu_sendkeys
    `dd if=/path/to/guest/file of=/dev/hd1c bs=512`, then qemu_kill the
    session, and read host_path on the host.
    """
    sess = SESSIONS.get(session_id)
    if not sess:
        raise ValueError(f"unknown session_id: {session_id}")
    # Create the empty disk on the host.
    import subprocess as sp
    sp.run(["truncate", "-s", f"{size_mb}M", host_path], check=True)
    # Add via QMP: blockdev-add + device_add.
    await _qmp(sess, "blockdev-add",
               **{"driver": "file", "filename": host_path,
                  "node-name": device_name + "-file"})
    await _qmp(sess, "blockdev-add",
               **{"driver": "raw", "file": device_name + "-file",
                  "node-name": device_name + "-raw"})
    await _qmp(sess, "device_add",
               **{"driver": "ide-hd",
                  "drive": device_name + "-raw",
                  "id": device_name})
    return {"device_id": device_name, "host_path": host_path,
            "size_mb": size_mb}


@mcp.tool()
async def qemu_extract_fs_file(session_id: str, guest_path: str,
                               host_path: str) -> dict[str, Any]:
    """Extract a file from the running guest's filesystem to the host.

    Implementation uses a scratch-disk approach:
      1. Hot-attach an empty raw disk via QMP blockdev-add + device_add.
      2. Have the guest `dd if=GUEST_PATH of=/dev/hd1c bs=512` (caller
         must drive this via qemu_sendkeys + qemu_sendkeys('sync\\n')).
      3. Detach disk.
      4. Read host_path; the file's bytes are at offset 0.

    This function does steps 1 and 3 + the host-side bookkeeping. The
    guest-side `dd` is the CALLER'S RESPONSIBILITY (this MCP server
    cannot anticipate the guest's device-naming convention; e.g.
    /dev/hd1c on V7/x86, /dev/hdb on Linux, etc.).
    """
    raise NotImplementedError(
        "qemu_extract_fs_file: use qemu_attach_scratch_disk + "
        "qemu_sendkeys('dd if=PATH of=/dev/hd1c bs=512\\n') + qemu_kill, "
        "then read the scratch host_path on the host side.")


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


# ---------------------------------------------------------------------------
# Tool 8 (optional): qemu_screen_text - OCR-free text scrape via VGA peek
# ---------------------------------------------------------------------------

@mcp.tool()
async def qemu_screen_text(session_id: str) -> dict[str, Any]:
    """Return the current VGA text-mode screen as a Python string.

    For guests in 80x25 text mode (which V7/x86, MS-DOS, MINIX, etc.
    use), this is much faster and more reliable than screendump+OCR.
    Uses QMP `human-monitor-command` to issue `info vga` then parses
    the framebuffer dump. Heuristic; falls back to None if not text mode.

    Phase Q stage 4. Useful for batch automation: poll this until you
    see `# ` (shell prompt) or `BOOT [hd(0,0)unix]:` before sending
    the next command.
    """
    sess = SESSIONS.get(session_id)
    if not sess:
        raise ValueError(f"unknown session_id: {session_id}")
    # QEMU's `info vga` doesn't dump the framebuffer; we use `xp` (eXamine
    # Physical) to read VGA text RAM at 0xb8000. Each cell is 2 bytes:
    # (ASCII char, attribute byte).
    res = await _qmp(
        sess, "human-monitor-command",
        **{"command-line": "xp /4000bx 0xb8000"})
    if isinstance(res, str):
        out = res
    else:
        out = res.get("return", "")
    # Parse hex dump lines like "00000000: 0x20 0x07 0x20 0x07 ..."
    chars: list[str] = []
    for line in out.splitlines():
        if ":" not in line:
            continue
        _, _, payload = line.partition(":")
        bytes_ = [p for p in payload.split() if p.startswith("0x")]
        # Every even-indexed byte is an ASCII char; every odd is the attr.
        for i, b in enumerate(bytes_):
            if i % 2 != 0:
                continue
            v = int(b, 16)
            chars.append(chr(v) if 32 <= v < 127 else (".\n"[v == 0] if v in (0, 10) else "."))
    # Reshape into 25 lines of 80 chars each.
    text = "".join(chars[:80 * 25])
    rows = ["".join(chars[i:i+80]).rstrip() for i in range(0, 80*25, 80)]
    full = "\n".join(rows)
    return {"text": full, "rows": rows, "raw_chars": len(chars)}


@mcp.tool()
async def qemu_wait_for(session_id: str, pattern: str,
                        timeout_s: int = 60,
                        poll_interval_s: float = 0.5) -> dict[str, Any]:
    """Poll the VGA text mode screen until `pattern` appears, or timeout.

    Returns when the pattern is observed; raises TimeoutError otherwise.
    Useful for synchronising automation: wait_for('# ') before sending
    the next shell command, wait_for('login:') to know the OS booted, etc.

    Phase Q stage 4.
    """
    import time
    sess = SESSIONS.get(session_id)
    if not sess:
        raise ValueError(f"unknown session_id: {session_id}")
    start = time.time()
    while True:
        scr = await qemu_screen_text(session_id)
        if pattern in scr["text"]:
            return {"matched": True, "elapsed_s": round(time.time() - start, 2),
                    "screen": scr["text"]}
        if time.time() - start > timeout_s:
            return {"matched": False, "elapsed_s": timeout_s,
                    "screen": scr["text"]}
        await asyncio.sleep(poll_interval_s)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
