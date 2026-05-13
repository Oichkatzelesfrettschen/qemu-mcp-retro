# qemu-mcp-retro

A Model Context Protocol server for driving **headless retro x86 OS work**
under `qemu-system-i386`. Designed for V7/x86, MINIX, ELKS, Coherent,
Xenix, and other vintage systems that don't have SSH or xdotool.

## Why not razr/qemu-mcp or Neanderthal/mcp-qemu-vm?

The existing 7 QEMU MCP servers on GitHub target modern Linux guests
with SSH + xdotool. None handle:

- `-cpu 486 -m 16` retro configurations
- Booting from a raw disk image with no init system
- Sending keystrokes via QMP `send-key` (the right primitive for
  pre-network-stack OSes)
- File extraction by attaching a scratch disk + driving the guest
- Byte-level disk-sector diffing for golden-image work

## Status

**Phase 1 scaffold.** Drop-in `mcp-server-qemu-retro.py` that you can
register in `~/.claude.json`. Tools below are stubs returning
NotImplementedError; each one is concrete and implementable in <50
lines once an actual QMP socket is wired.

## Planned tool surface (7 + 1 optional)

| Tool | Inputs | Outputs |
| --- | --- | --- |
| `qemu_boot` | machine, cpu, memory_mb, disks[], extra_args | session_id |
| `qemu_sendkeys` | session, keys (string or list), hold_ms=30 | ok / events |
| `qemu_screendump_png` | session, host_path | bytes_written + path |
| `qemu_extract_fs_file` | session, guest_path, host_path | sha256 + size |
| `qemu_diff_disk_sectors` | image_a, image_b, lba_range | offsets + bytes |
| `qemu_qmp` | session, command, args | structured QMP reply |
| `qemu_kill` | session, mode (graceful\|hard) | exit_status |
| `qemu_serial_expect` (optional) | session, pattern, send, timeout | match + output |

## Dependencies

```sh
pip install --user fastmcp 'qemu.qmp>=0.0.3'
sudo paru -S qemu-base   # qemu-system-i386, qemu-img
```

## Smoke-test target

The first concrete user of this server is the v7x86-32 reconstruction at
`~/Github/OS-Projects/v7x86-32/`. Once the QEMU MCP server is functional,
its `tests/qemu-boot-test.sh` can be replaced with an MCP-driven script
that runs in CI without expect.

## License

BSD-2-Clause.
