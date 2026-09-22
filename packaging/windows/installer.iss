; Aether-gate — Windows installer (Inno Setup 6).
; Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
;
; Wraps dist\AetherGate (from packaging\windows\build.py) into
; Aether-gate-Setup-<version>.exe:
;   iscc /DAppVersion=0.5.1 packaging\windows\installer.iss
;
; For hams, not developers: Next, Next, Finish -> a Start-menu icon that opens
; the Setup page in the browser. The choices offered are the ones people
; otherwise trip over:
;   * a Windows Firewall rule for AetherGate.exe, PRIVATE networks only (on by
;     default). Without it the first start pops a firewall prompt, and a
;     "Cancel" there silently hides the radio from AetherSDR's chooser.
;   * start with Windows (off by default), so a saved "connect on launch"
;     radio comes up on its own after a reboot.
; Saved radios and the setup PIN live in %USERPROFILE%\.aether-gate and are
; left alone by both an upgrade and an uninstall.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6C1B9C1E-5F0A-4C8E-9E61-AE7A7E0A6A71}
AppName=Aether-gate
AppVersion={#AppVersion}
AppVerName=Aether-gate {#AppVersion}
AppPublisher=G0JKN
AppPublisherURL=https://github.com/nigelfenton/Aether-gate
AppSupportURL=https://github.com/nigelfenton/Aether-gate/issues
AppUpdatesURL=https://github.com/nigelfenton/Aether-gate/releases
DefaultDirName={autopf}\Aether-gate
DefaultGroupName=Aether-gate
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename=Aether-gate-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; per-machine: the firewall rule needs admin anyway
PrivilegesRequired=admin
UninstallDisplayIcon={app}\AetherGate.exe
CloseApplications=yes

[Tasks]
Name: "firewall"; Description: "Allow Aether-gate through Windows Firewall on private (home) networks — recommended, so AetherSDR can find your radio"
Name: "startup"; Description: "Start Aether-gate when Windows starts (brings up a saved ""connect on launch"" radio)"; Flags: unchecked
Name: "desktopicon"; Description: "Desktop icon"; Flags: unchecked

[Files]
Source: "..\..\dist\AetherGate\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Aether-gate Setup"; Filename: "{app}\AetherGate.exe"; WorkingDir: "{app}"; Comment: "Pick your radio and start it (opens in your browser)"
Name: "{group}\Aether-gate on GitHub"; Filename: "https://github.com/nigelfenton/Aether-gate"
Name: "{group}\Uninstall Aether-gate"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Aether-gate Setup"; Filename: "{app}\AetherGate.exe"; WorkingDir: "{app}"; Tasks: desktopicon
; at logon: quiet (no browser pop-up), window minimised; opening the Start-menu icon still shows the page
Name: "{userstartup}\Aether-gate"; Filename: "{app}\AetherGate.exe"; Parameters: "--setup --no-browser"; WorkingDir: "{app}"; Flags: runminimized; Tasks: startup

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Aether-gate"""; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Aether-gate"" dir=in action=allow program=""{app}\AetherGate.exe"" enable=yes profile=private"; Flags: runhidden; Tasks: firewall
Filename: "{app}\AetherGate.exe"; Description: "Open Aether-gate Setup now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Aether-gate"""; Flags: runhidden; RunOnceId: "DelFirewallRule"
