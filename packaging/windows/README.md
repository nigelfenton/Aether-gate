# Windows installer

`Aether-gate-Setup-<version>.exe`: Next, Next, Finish, then a **Start-menu icon, "Aether-gate Setup"**, that
opens the Setup page in the browser. No Python, no git, no command prompt.

Built by [`.github/workflows/windows-installer.yml`](../../.github/workflows/windows-installer.yml) and attached to
every release. To build it yourself, on Windows:

```bat
python -m pip install numpy pyinstaller
python packaging\windows\build.py
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVersion=0.5.1 packaging\windows\installer.iss
```

The installer lands in `dist\`.

| File | What it does |
|---|---|
| `launcher.py` | The program's entry point. Bare: opens the Setup page (like `python -m aether_gate`). With arguments: runs the gate. It drops the `-u -m aether_gate` the Setup page puts in front of each gate it starts, so `setup.py` needs no Windows branch. |
| `build.py` | PyInstaller **folder** build (`dist\AetherGate\`), not one-file. A one-file exe unpacks itself to `%TEMP%` on every start, and the Setup page starts a second copy for each gate. |
| `installer.iss` | Inno Setup script. Firewall rule for `AetherGate.exe` on **private** networks (on by default), start with Windows (off), desktop icon (off). |

## What the installer asks, and why

- **Firewall (recommended):** without the rule, the first start pops a Windows Firewall prompt. Pressing
  "Cancel" there silently hides the radio from AetherSDR's chooser. The rule is for private (home) networks only.
- **Start with Windows:** starts the Setup page quietly at logon, so a profile saved with *connect on launch* comes
  back after a reboot.
- Saved radios and the setup PIN live in `%USERPROFILE%\.aether-gate`. Upgrades and uninstalls leave them alone.

## Antivirus and signing

The installer is **not code-signed**, so it has no reputation: SmartScreen warns, and a
reputation-based antivirus may refuse it outright. **Norton blocked it on the first real install
(2026-09-22)** before it could run.

Until it is signed:

- the **portable zip** (`Aether-gate-<ver>-windows-portable.zip`, built alongside the installer) is
  the fallback — unzip and run, no installer and no admin;
- every artifact ships a `.sha256`;
- false positives are worth reporting to the vendor (Norton/Symantec and Microsoft both take
  submissions), which is how an unsigned installer eventually gets whitelisted.

Signing is the real fix, in rough order of fit: **SignPath Foundation** (free for open-source
projects), **Azure Trusted Signing** (~$10/month, needs a verified business identity), or a
traditional OV certificate (~$200–400/year).

## Phase 1 limits

- **Radios:** Icom LAN (IC-9700, IC-705 and the rest of the Icom LAN family) and the sim. **Not yet:** hamlib CAT
  radios (Kenwood/Yaesu need `rigctld.exe`) and SDR dongles (SoapySDR). The Setup page's *Known info* page says
  what is missing.
- **Unsigned:** Windows SmartScreen shows *"Windows protected your PC"* the first time. Click **More info → Run
  anyway**. That goes away once the installer is code-signed.
- **Updates:** the page tells you when a new release is out. Update by running the new installer; the in-page
  one-click update is for the Pi and source installs.
- **AetherSDR on the same PC:** set **Network / advanced → Port** to **5992**, so the gate does not collide with
  AetherSDR's own port 4992.
