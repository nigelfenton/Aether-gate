# Privacy

**Aether-gate collects nothing about you, and sends nothing to us.** There is no telemetry, no analytics, no
account, no registration, and no "phone home" with usage data. Nobody, including the author, can see that you are
running it.

## What it talks to

| Connection | Why | Where it goes |
|---|---|---|
| **Your radio** | control and receive | your own radio, on your own network or a USB cable |
| **AetherSDR** | the gate presents the radio to it | the computer you run AetherSDR on |
| **A GitHub release check** (optional) | tells you when a newer version exists | `api.github.com` — see below |

Nothing else. The gate does not use the internet to work: a station with no internet at all runs it normally.

## The update check

On start-up, the gate asks GitHub whether a newer release exists, so the Setup page can say so. That request
carries what any HTTP request carries — your IP address and a user-agent string — and is made to GitHub, not to
us. It sends **no** information about you, your radio, or your station.

Turn it off with `AETHER_GATE_NO_UPDATE_CHECK=1`, or `--no-update-check`. Nothing else changes.

## What stays on your own machine

- **Saved radio profiles** and, where a radio needs one, its **network login** —
  `~/.aether-gate/profiles.json` (`%USERPROFILE%\.aether-gate\profiles.json` on Windows), written so that only
  your account can read it.
- **The Setup page's PIN**, stored as a PBKDF2 hash in the same folder — never the PIN itself.
- **Logs**, which stay in the terminal or your system's journal.

None of this leaves the machine. Uninstalling does not delete that folder; remove it yourself if you want the
saved radios and PIN gone.

## The Setup page and the control panel

Both are web pages served **by your own machine, on your own network**. The Setup page (`:8730`) requires a PIN,
refuses requests from other web sites, and never hands back a saved password. The per-radio control panel
(`:8731` and up) has no login: treat it as you would any device on your LAN, and do not expose either to the
internet.

## Downloads

Release files are hosted by **GitHub**, whose own privacy terms apply to downloading them. The project keeps no
download logs of its own beyond the counters GitHub shows publicly.

## Contact

Questions, or anything here that looks wrong:
[open an issue](https://github.com/nigelfenton/Aether-gate/issues).

*Aether-gate is a hobby project by Nigel Fenton (G0JKN), published under GPL-3.0-or-later.*
