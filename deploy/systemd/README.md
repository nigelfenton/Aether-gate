# Running Aether-gate as a service (Linux / Raspberry Pi)

These systemd units run the gate **unattended** — surviving terminal/SSH
close, restarting on a crash, and (critically) stopping it with a **catchable
SIGTERM** so the gate can shut down *gracefully*.

## Why a service, not the launcher

The Setup UI (`aether_gate.setup`, port 8730) starts a gate as a **child
process**. When the launcher or its terminal/SSH session dies, the child is
**hard-killed** — it never runs its shutdown, so for an IC-9700 the **0x05
disconnect never goes out** and the radio keeps a phantom RS-BA1 session that
blocks the next start ("came up then jumped to the other radio after 2–3 s").

systemd fixes this: it *owns* the process, stops it with `SIGTERM` (which
`__main__.py` turns into the same graceful path as Ctrl-C → `adapter.close()` →
0x05), waits `TimeoutStopSec` for the disconnect to flush, and only then
escalates to SIGKILL. A clean `systemctl stop`/`restart` therefore releases the
radio cleanly. (A hard *crash* still can't send 0x05 — but the gate's own
open-failure path then detects the phantom, cleans up, and stops, and
`Restart=on-failure` retries after the radio's stale window.)

## Units

| Unit | Purpose | Port |
|------|---------|------|
| `aether-gate-7300.service` | IC-7300 USB CI-V + USB-audio bridge, RX/control only | ctl :8731 |
| `aether-gate-9700.service` | Always-on IC-9700 LAN bridge | ctl :8732 |
| `kenwood-gate.service` | Kenwood CAT + SDR-spectrum bridge | ctl :8734 |
| `aether-gate-setup.service` | First-boot Setup UI (interactive) | :8730 |

## Install

Each unit has `EDIT THESE` markers — set the radio IP/credentials, this host's
LAN `--ip`, and AE's `--ae` IP before installing.

For the IC-7300 unit, stop `wfweb` / `wfwebrtc` first if they normally own the
same USB serial/audio devices. The IC-7300 adapter is intentionally RX/control
only: it does not send CI-V PTT and it keeps RTS/DTR low on open and close.

```sh
sudo cp aether-gate-9700.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aether-gate-9700
journalctl -u aether-gate-9700 -f          # watch it connect
sudo systemctl stop aether-gate-9700       # graceful — sends 0x05, releases the radio
```

## Verifying graceful stop (the point of Blocker #2)

```sh
sudo systemctl start aether-gate-9700      # connects, holds
sudo systemctl stop  aether-gate-9700      # SIGTERM -> graceful "bye" in the log
sudo systemctl start aether-gate-9700      # should reconnect cleanly, NO phantom wait
```
Watch `journalctl -u aether-gate-9700`: a graceful stop logs `bye`; the restart
should reach `[civ] stream healthy` without the `connect failed … (authed=True)`
phantom error.

> One host can only advertise on `:4992` once — run at most one gate per IP, or
> give each a distinct `--ip`/`--port` (the Pi5 uses eth0 for the always-on gate
> and wlan0 for launcher-started ones).
