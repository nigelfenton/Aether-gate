# Aether-gate in Docker

A container is a good fit for the gate: it is a long-running background service with
awkward per-adapter dependencies, and it wants to come back after a reboot. This is the
NAS/Linux-box counterpart to the [Pi appliance](../PI_APPLIANCE.md).

```bash
cp gate.env.example gate.env && chmod 600 gate.env   # RS-BA1 login goes here
$EDITOR docker-compose.yml                           # radio IPs + this host's IP
docker compose up -d --build
```

The radio should appear in AetherSDR's chooser within a few seconds.

---

## Host networking is required, not preferred

Run with `network_mode: host` (compose) or `--network host` (`docker run`). Three separate
things break under bridge NAT:

1. **Discovery is a broadcast.** The gate advertises to `255.255.255.255:4992` once a
   second. A bridged container's broadcast does not reach your LAN, so the radio never
   appears in AE's chooser.
2. **AE unicasts back** to the address the advertisement carries. Behind NAT that address
   is a private container IP that AE cannot reach.
3. **The gate opens the VITA-49 stream** toward AE's *ephemeral* port. There is no fixed
   port to publish, so it cannot be mapped.

Two consequences worth stating plainly:

- **Linux hosts only.** Docker Desktop on macOS and Windows does not give a container the
  host's real broadcast domain, so `network_mode: host` will not do what you need there.
  Use the [Pi appliance](../PI_APPLIANCE.md) or run the gate natively.
- **Put the gate on the same subnet as AE and the radio.** Broadcast does not cross a
  router, and the unicast VITA-49 stream is routinely dropped by a firewall between
  subnets. Same L2 segment, and none of this is a problem.

### The macvlan alternative

`flex-sim`'s compose takes the other route: a **macvlan** network, giving the container its
own address on your LAN so it behaves as a genuinely distinct radio. That satisfies the same
three requirements and avoids a `:4992` collision when something else on the host already
wants that port.

Its trade-offs are the reason host networking is the default here: macvlan means claiming an
address your DHCP scope might later hand to something else, and — as flex-sim's compose notes
— **the Docker host itself cannot talk to a macvlan container**, so AE must run on a different
machine. Host networking with a distinct `AETHER_GATE_PORT` per radio keeps several gates on
one box with no address allocation at all.

Because the network stack is the host's, published ports do nothing — `EXPOSE` in the
Dockerfile is documentation. Set the ports through `AETHER_GATE_PORT` and
`AETHER_GATE_CTL_PORT` instead.

## Two image targets

The gate's dependencies are per-adapter, so the image mirrors the split that
`deploy/install-pi.sh --no-sdr` already makes:

| Target | Adds | Covers |
|---|---|---|
| **`lan`** (default) | numpy only | Icom LAN rigs (IC-9700, IC-705, IC-7610, IC-R8600, IC-905), `sim` |
| **`full`** | hamlib, SoapySDR, rtl-sdr-blog (source, pinned) | the above **+** Kenwood/Yaesu CAT and RTL/Airspy/SDRplay dongles |

```bash
docker build --target lan  -t aether-gate:lan  .    # small, quick
docker build --target full -t aether-gate:full .    # slower: source builds
```

If you only bridge an Icom over LAN, `lan` is all you need. `full` pins the same upstream
commits as the Pi installer — the apt `librtlsdr` does not drive an RTL-SDR V4 well, which
is why the blog fork is built from source in both places.

A dongle also needs the device passed in:

```bash
docker run --network host --device /dev/bus/usb --group-add plugdev aether-gate:full ...
```

## Configuration

Every command-line flag has an environment variable: `AETHER_GATE_` + the option name
upper-cased. Precedence is **CLI > environment > built-in default**.

| Variable | Flag | Note |
|---|---|---|
| `AETHER_GATE_ADAPTER` | `--adapter` | `icom9700` is the Icom **LAN** transport, any model |
| `AETHER_GATE_ICOM_MODEL` | `--icom-model` | `IC-705`, `IC-9700`, … — drives band coverage |
| `AETHER_GATE_RADIO_IP` | `--radio-ip` | the radio |
| `AETHER_GATE_IP` | `--ip` | **this host** — the address AE will be told to connect to |
| `AETHER_GATE_USER` / `AETHER_GATE_PW` | `--user` / `--pass` | RS-BA1 login (note: `PW`, not `PASS`) |
| `AETHER_GATE_SERIAL` | `--serial` | must be unique per gate |
| `AETHER_GATE_PORT` | `--port` | control/data; unique per gate on a host |
| `AETHER_GATE_CTL_PORT` | `--ctl-port` | control panel; unique per gate |
| `AETHER_GATE_RX_ONLY` | `--rx-only` | recommended for an unattended gate |

Two things that are easy to get wrong:

- The name follows the option's **destination**, not always its spelling. `--pass` is
  stored as `pw`, so it is **`AETHER_GATE_PW`**.
- On/off flags take `1/true/yes/on`; `0`, `false`, `no`, `off` and empty all mean **off**.
  `AETHER_GATE_RX_ONLY=0` therefore leaves transmit alone rather than enabling the lock.

Keep the login in `gate.env` (mode `600`) rather than in the compose file. A command line
is readable by every user on the box; a 0600 file is not. `gate.env` is gitignored and
excluded from the build context.

## Running more than one radio

One container per radio, each needing its own:

- **`AETHER_GATE_SERIAL`** — AE keys its chooser on the serial, so two gates sharing one
  will collide and you will see a single, flickering entry;
- **`AETHER_GATE_PORT`** and **`AETHER_GATE_CTL_PORT`** — they share the host's network
  namespace, so a duplicate simply fails to bind.

The shipped `docker-compose.yml` is a working two-radio example.

> **Known limitation.** The listener for AE's TX audio (`dax_tx`) is fixed at UDP `:4991`
> and is not derived from `AETHER_GATE_PORT`, so only the first container on a host binds
> it; the second logs that it could not and carries on. This affects **TX audio only** —
> receive, control, spectrum and waterfall are unaffected on both — and is moot entirely
> under `--rx-only`.

## Stopping cleanly

`docker stop` sends `SIGTERM`, which `__main__.py` turns into a graceful close:
`adapter.close()` sends the RS-BA1 `0x05` disconnect that releases the radio's session.
`stop_grace_period: 10s` gives that datagram time to leave, matching `TimeoutStopSec=10`
in the systemd units.

This is why the image uses an **exec-form `ENTRYPOINT`**: Python must be PID 1 to receive
the signal. It also means **`docker kill` is not a safe way to stop the gate** — the radio
keeps a phantom session that refuses the next login. If that happens, power-cycle the
radio or toggle *Menu > Set > Network > Network function* off for ~10 s and back on, then
start again.

> **Restart backoff.** Docker's restart delay starts around 100 ms, while the systemd
> units deliberately use `RestartSec=15` — a radio that is hammered with logins keeps
> resetting its own stale-session timer. The gate mitigates this by *stopping* rather than
> retrying when a connect fails, but if you see a restart loop, give the radio a minute.

## Transmit

The Icom LAN adapter has real, guarded PTT, and the engine arms TX whenever AE connects.
On a shared network that means any AE client that finds the gate can reach a live PTT
path. For a permanently-online container, set:

```yaml
AETHER_GATE_RX_ONLY: "1"
```

The gate then refuses PTT at the CI-V layer and reports `tx_capable=false`, so AE greys
its TX button instead of offering a control the gate would refuse.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Radio never appears in AE | not host networking, or gate and AE on different subnets |
| Appears, then no waterfall | VITA-49 blocked between subnets — put them on one segment |
| Two radios, one flickering entry | duplicate `AETHER_GATE_SERIAL` |
| Second container exits or logs a bind error | duplicate `AETHER_GATE_PORT` / `AETHER_GATE_CTL_PORT` |
| Login refused after an unclean stop | phantom RS-BA1 session — see *Stopping cleanly* |
| `unknown --icom-model` | not a LAN Icom; `--help` lists the known rows |
| Chooser shows `Icom` instead of `Icom IC-705` | **the station name contains a space** — see below |

> **Avoid spaces in `AETHER_GATE_STATION`.** The discovery advertisement is a flat list
> of space-delimited `key=value` pairs, so a station of `Icom IC-705` is read as
> `nickname=Icom` with `IC-705` left as an orphan token — AE then labels the radio just
> `Icom`. Use a hyphen (`Icom-IC-705`), which is what the gate's own default
> (`Icom-<model>`) and the systemd units already do.

The control panel (`AETHER_GATE_CTL_PORT`, default `8731`) shows what the gate sees from
the radio and is the first place to look. `docker compose logs -f` shows the rest.
