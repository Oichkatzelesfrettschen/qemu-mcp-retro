# Installing qemu-mcp-retro

## Quick install (venv-based)

```sh
git clone https://github.com/Oichkatzelesfrettschen/qemu-mcp-retro
cd qemu-mcp-retro
python3 -m venv .venv
source .venv/bin/activate
pip install fastmcp 'qemu.qmp'
deactivate
```

## Register with Claude Code

Either edit `~/.claude.json` directly and add under `mcpServers`:

```json
"qemu-retro": {
  "command": "/abs/path/to/qemu-mcp-retro/.venv/bin/python3",
  "args": ["-m", "qemu_mcp_retro"]
}
```

Or use the CLI:

```sh
claude mcp add qemu-retro \
    /home/$USER/Github/Tools/qemu-mcp-retro/.venv/bin/python3 \
    -m qemu_mcp_retro
```

## Verified compatibility

- Python 3.14 (Arch Linux default 2026)
- fastmcp 3.2.4
- qemu.qmp 0.0.3
- qemu-system-i386 11.0.0

## Smoke test (no Claude needed)

```sh
source .venv/bin/activate
python3 -c "
import asyncio
from qemu_mcp_retro.server import qemu_boot, qemu_sendkeys, qemu_screendump_png, qemu_kill

async def main():
    r = await qemu_boot(
        machine='pc', cpu='486', memory_mb=16,
        disks=['/path/to/v7x86-32/tests/golden/hd0.img'],
        serial='null', display='none', extra_args=['-snapshot'])
    await asyncio.sleep(4)
    await qemu_sendkeys(r['session_id'], '\n')
    await asyncio.sleep(8)
    await qemu_sendkeys(r['session_id'], 'ls\n')
    await asyncio.sleep(3)
    await qemu_screendump_png(r['session_id'], '/tmp/v7x86-via-mcp.png')
    await qemu_kill(r['session_id'], 'hard')

asyncio.run(main())
"
```

Verified output: see `evidence/01-v7x86-boot-via-mcp.png` — the V7/x86
single-user shell with working `ls` command, driven entirely through the
MCP tool API.

## Tools currently functional

| Tool | Status | Notes |
| --- | --- | --- |
| `qemu_boot` | working | i386, IDE disks, QMP socket auto-wired |
| `qemu_sendkeys` | working | single-RPC key array via QMP `send-key` |
| `qemu_screendump_png` | working | native `format=png` (QEMU >= 6.0) |
| `qemu_diff_disk_sectors` | working | `qemu-img compare` or sector-range diff |
| `qemu_qmp` | working | raw QMP passthrough |
| `qemu_kill` | working | hard mode validated; graceful needs guest cooperation |
| `qemu_extract_fs_file` | stub | hot-plug scratch disk strategy not yet wired |
| `qemu_serial_expect` | stub | needs `-serial unix:` plumbing |
