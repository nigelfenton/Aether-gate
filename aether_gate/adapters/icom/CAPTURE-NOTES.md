# IC-9700 working handshake — captured from SDR9700 (2026-06-30)

Captured by patching a copy of SDR9700 (linux-aether /srv/build/SDR9700-cap) with
hexdumps + a hardcoded auto-connect, run headless (QT_QPA_PLATFORM=offscreen) against
the live radio at 10.0.0.7:50001. SDR9700 authenticated fully: token granted, FTTH,
civPort=50002 audioPort=50003, CI-V stream up.

## The auth sequence (tracked seq assigned by sendTrackedPacket, login_packet built with seq=0):
  TRACKED seq=1 len=128  -> LOGIN     (the 0x80 login packet)
  TRACKED seq=2 len=64   -> TOKEN     (0x40 confirm, reqtype=0x02)
  TRACKED seq=3 len=144  -> CONNINFO  (0x90 stream request)
  (NO idle packets are interleaved between these three — idles only fill later gaps)

## KEY FIX vs my probe: I was MISSING THE TOKEN STEP entirely.
Flow: are-you-there -> I-am-here -> (we send 0x06) -> radio 0x06 -> LOGIN ->
  radio LOGIN_RESPONSE (carries a token) -> **we send TOKEN confirm (reqtype=0x02,
  echo that token + tokreq)** -> radio grants -> CONNINFO -> civ/audio ports.
The "no login response" was because I never sent the token, so the radio never advanced.

## LOGIN packet (0x80) — verified byte layout (seq overwritten to 1 by sendTracked):
  80000000 0000 0000  <sentid LE> <rcvdid LE>  00000070(BE paylsz)
  01(reqreply) 00(reqtype=login) 0030(BE innerseq) 0000 <tokreq LE> 00000000(token)
  ...zeros... @0x40 username(obf) @0x50 password(obf) @0x60 "SDR9700"\0...
  username obf "nigel"    = 7425373328 00...  (MATCHES our obfuscate())
  password obf "<redacted>" = <redacted> ...  (MATCHED the live capture)

## TOKEN packet (0x40) — verified:
  40000000 0000 0000 <sentid> <rcvdid>  00000030(BE paylsz)
  01(reqreply) 02(reqtype=CONFIRM) 0031(BE innerseq = login innerseq+1) 0000
  <tokreq LE, echoed from login> <token LE, echoed from LOGIN_RESPONSE @0x1c>
  0000(authstartid) 00000 0798(@0x24 resetcap) 0000(commoncap)... rest zeros

## TODO next: capture the CONNINFO (0x90) bytes the same way (add hexdump to
  sendRequestStream), and the LOGIN_RESPONSE parse (where the token comes from).
  Then build handler.py with: login -> on response read token@0x1c -> send token
  confirm -> on grant send conninfo -> read STATUS civport@0x42 BE -> open CI-V.

## Cleanup: /srv/build/SDR9700-cap is a PATCHED copy (hexdumps + hardcoded creds in
  tryAutoConnect). Delete it when done; the pristine reference is /srv/build/SDR9700.
