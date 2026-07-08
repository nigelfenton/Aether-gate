#
# Aether-gate — IC-9700 LAN control-stream handler (auth state machine).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
# Ported from github.com/w5jwp/SDR9700 (GPL-3.0) by Justin W5JWP: UdpHandler.cpp. Attribution preserved.
#
"""Control-stream auth: discovery -> login -> token -> capabilities -> conninfo,
yielding the radio-assigned civ/audio ports. Built on the threaded UdpBase so the
ping/idle/retransmit cadence runs throughout (the serial-probe bug fix)."""
import random
import socket
import struct
import threading
import time

from .udpbase import UdpBase
from .obfuscation import obfuscate


class Ic9700Handler(UdpBase):
    def __init__(self, local_ip, radio_ip, radio_port, username, password, name="aether-gate"):
        super().__init__(local_ip, radio_ip, radio_port, bind_port=0, name="ctrl")
        self.username = username
        self.password = password
        self.client_name = name[:16]
        self._auth_seq = 0x30
        # TRANSPORT-AUDIT FIND (the sneakiest one): the token-request id MUST be
        # RANDOM PER LOGIN — SDR9700: tokRequest = QRandomGenerator::generate()
        # in sendLogin(). Ours was id(self)&0xFFFF, which is effectively CONSTANT
        # across process restarts (deterministic CPython heap) — every session
        # presented the SAME tokrequest, colliding in the radio's token table
        # with its own dead predecessors: renewals then "OK" against stale
        # entries while the real stream lease dies at ~90 s, and the radio
        # reports "busy (another client)" that is really our own last ghost.
        self._tok_request = (random.getrandbits(16) & 0xFFFF) or 1
        self.token = 0
        self.mac = b"\x00" * 6
        self.use_guid = False
        self.civ_port = None
        self.audio_port = None
        self.civ_local_port = 0
        self.audio_local_port = 0
        self.authenticated = threading.Event()
        self.stream_ready = threading.Event()
        self.radio_disconnected = False  # radio sent status disc=1 (kicked us)
        self.on_civ_ports = None         # callback(civ_port, audio_port)
        self.on_data = self._on_control_data

    # discovery hooks ------------------------------------------------------
    def _on_iamready(self):
        # radio acked our 0x06 -> send login (first tracked packet, seq=1)
        self._send_login()

    # auth packets ---------------------------------------------------------
    def _send_login(self):
        b = bytearray(0x80)
        struct.pack_into("<IHHII", b, 0, 0x80, 0x00, 0, self.my_id, self.remote_id)
        struct.pack_into(">I", b, 0x10, 0x70)
        b[0x14] = 0x01                   # requestreply = request
        b[0x15] = 0x00                   # requesttype = login
        struct.pack_into(">H", b, 0x16, self._auth_seq); self._auth_seq += 1
        struct.pack_into("<H", b, 0x1a, self._tok_request)
        b[0x40:0x50] = obfuscate(self.username)
        b[0x50:0x60] = obfuscate(self.password)
        nm = self.client_name.encode("latin-1"); b[0x60:0x60 + len(nm)] = nm
        self.send_tracked(bytes(b))

    def _send_token(self, reqtype):
        # One packet shape, two uses (SDR9700 UdpHandler::sendToken):
        #   0x02 = token CONFIRM (once, during login)
        #   0x05 = token RENEWAL (every 60 s, forever)
        b = bytearray(0x40)
        struct.pack_into("<IHHII", b, 0, 0x40, 0x00, 0, self.my_id, self.remote_id)
        struct.pack_into(">I", b, 0x10, 0x30)
        b[0x14] = 0x01                   # requestreply = request
        b[0x15] = reqtype & 0xFF         # requesttype
        struct.pack_into(">H", b, 0x16, self._auth_seq); self._auth_seq += 1
        struct.pack_into("<H", b, 0x1a, self._tok_request)
        struct.pack_into("<I", b, 0x1c, self.token)
        # resetcap is BIG-endian on the wire — byte-level diff vs SDR9700's
        # captured renewal proved it: reference sends 07 98 (qToBigEndian),
        # our old "<H" sent 98 07 = 0x9807, a completely different flags value
        # in EVERY token confirm + renewal since the port was written.
        struct.pack_into(">H", b, 0x24, 0x0798)     # resetcap (BE)
        self.send_tracked(bytes(b))

    def _send_token_confirm(self):
        self._send_token(0x02)

    # THE DEAF-SCOPE ROOT CAUSE (found 2026-07-08 by auditing SDR9700's
    # UdpHandler.cpp after Nigel ran the reference app side-by-side and it never
    # froze): the radio's session TOKEN EXPIRES after ~90 s. When it does, the
    # radio silently stops the privileged streams — the scope (and audio) die
    # while ping/idle keep flowing, which is exactly the "deaf scope" we chased
    # for two days. The ~2650-frame "cap" was never a frame count: it's
    # ~90 s x 29.5 fps. SDR9700 never freezes because it RENEWS the token every
    # 60 s (TOKEN_RENEWAL=60000, sendToken(0x05)); we confirmed once at login
    # and never again. Renew like the reference and the scope never pauses.
    def _token_renewal_loop(self):
        last = time.monotonic()
        while self._run:
            time.sleep(1.0)
            if not self._run:
                break
            if self.authenticated.is_set() and time.monotonic() - last >= 60.0:
                try:
                    self._send_token(0x05)
                    print("[ctrl] token renewal sent", flush=True)
                except Exception as e:
                    print(f"[ctrl] token renewal send failed: {e}", flush=True)
                last = time.monotonic()

    def _start_token_renewal(self):
        if getattr(self, "_renew_thread", None) is None:
            self._renew_thread = threading.Thread(target=self._token_renewal_loop,
                                                  daemon=True, name="ic9700-token-renew")
            self._renew_thread.start()

    def stop(self):
        # Transport-audit find: SDR9700 sends a token REMOVAL (sendToken(0x01))
        # before the 0x05 disconnect and waits briefly for the ack — proper
        # session hygiene so the radio releases the login slot immediately
        # instead of aging out a phantom session (the authed=True wedge that
        # blocked reconnects all night is exactly what un-removed tokens cause).
        if self.authenticated.is_set():
            try:
                self._send_token(0x01)
                time.sleep(0.3)          # give the ack/flush a moment
            except Exception:
                pass
        super().stop()                   # 0x05 disconnect + close the socket

    def _send_conninfo(self):
        # reserve civ/audio local ports (bind temp sockets)
        cs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); cs.bind((self.local_ip, 0))
        as_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); as_.bind((self.local_ip, 0))
        self.civ_local_port = cs.getsockname()[1]
        self.audio_local_port = as_.getsockname()[1]
        self._civ_sock = cs; self._audio_sock = as_   # keep them bound; civ.py adopts cs
        b = bytearray(0x90)
        struct.pack_into("<IHHII", b, 0, 0x90, 0x00, 0, self.my_id, self.remote_id)
        struct.pack_into(">I", b, 0x10, 0x80)
        b[0x14] = 0x01                   # request
        b[0x15] = 0x03                   # requesttype = stream request
        struct.pack_into(">H", b, 0x16, self._auth_seq); self._auth_seq += 1
        struct.pack_into("<H", b, 0x1a, self._tok_request)
        struct.pack_into("<I", b, 0x1c, self.token)
        if not self.use_guid:
            struct.pack_into("<H", b, 0x27, 0x8010)         # commoncap (native LE)
            b[0x2a:0x30] = self.mac
        nm = b"IC-9700"                  # devName from capabilities (set in _parse_caps)
        b[0x40:0x40 + len(self._dev_name)] = self._dev_name[:32]
        # requested_streams union @0x60: username, rxenable, txenable, codecs, samples, ports
        b[0x60:0x70] = obfuscate(self.username)
        b[0x70] = 0x01                   # rxenable
        b[0x71] = 0x00                   # txenable (RX-only first)
        b[0x72] = 0x04                   # rxcodec (LPCM16)
        b[0x73] = 0x00                   # txcodec
        struct.pack_into(">I", b, 0x74, 48000)              # rxsample
        struct.pack_into(">I", b, 0x78, 0)                  # txsample
        struct.pack_into(">I", b, 0x7c, self.civ_local_port)
        struct.pack_into(">I", b, 0x80, self.audio_local_port)
        struct.pack_into(">I", b, 0x84, 0)                  # txbuffer
        b[0x88] = 0x01                   # convert
        self.send_tracked(bytes(b))

    # control-stream replies (dispatch by exact packet length, like SDR9700) ----
    def _on_control_data(self, d):
        ln = len(d)
        if ln < 0x16:
            return

        # LOGIN_RESPONSE (0x60): read token, send token-confirm, mark authenticated
        if ln == 0x60:
            err = struct.unpack("<I", d[0x30:0x34])[0]
            if err == 0xFEFFFFFF:
                self._fail = "bad credentials"; return
            if not self.authenticated.is_set():
                # Transport-audit: the reference validates that the reply's
                # tokrequest matches the one WE sent (a reply meant for a stale
                # login attempt must not authenticate this one).
                got_tr = struct.unpack("<H", d[0x1a:0x1c])[0]
                if got_tr != self._tok_request:
                    print(f"[ctrl] login reply tokrequest mismatch "
                          f"(sent 0x{self._tok_request:04x}, got 0x{got_tr:04x}) "
                          f"- ignoring stale reply", flush=True)
                    return
                self.token = struct.unpack("<I", d[0x1c:0x20])[0]
                self._send_token_confirm()
                self.authenticated.set()
                self._start_token_renewal()   # keep the token alive (60 s cadence)
            return

        # CONNINFO (0x90): radio's per-radio availability status (name / busy /
        # in-use-by-computer). We don't surface a UI for it; recognize + log the
        # busy flag so "another client holds the radio" is at least visible.
        if ln == 0x90:
            busy = d[0x60] if len(d) > 0x60 else 0
            if busy:
                print("[ctrl] radio reports BUSY (in use by another client)", flush=True)
            return

        # TOKEN packet (0x40): reply to our confirm (0x02) or RENEWAL (0x05).
        # requestreply@0x14 == 0x02 marks a radio reply; response@0x30:
        #   0x00000000 = accepted; 0xFFFFFFFF = REJECTED (token dead — the scope
        #   is about to stop; the adapter's watchdog will recycle the session).
        if ln == 0x40:
            reply, reqtype = d[0x14], d[0x15]
            if reply == 0x02 and reqtype == 0x05:
                resp = struct.unpack("<I", d[0x30:0x34])[0]
                if resp == 0:
                    print("[ctrl] token renewal OK", flush=True)
                elif resp == 0xFFFFFFFF:
                    # Transport-audit find: SDR9700 does NOT wait to die here — it
                    # RE-LOGS IN on the same session (UdpHandler.cpp "Radio
                    # rejected token renewal, performing login"): adopt the ids
                    # from the rejection packet, drop stream state, send login.
                    # Far cheaper than the full session recycle the watchdog
                    # would eventually do.
                    print("[ctrl] token renewal REJECTED -> re-logging in "
                          "(reference behaviour)", flush=True)
                    self.remote_id = struct.unpack("<I", d[0x08:0x0c])[0]
                    self._tok_request = struct.unpack("<H", d[0x1a:0x1c])[0]
                    self.token = struct.unpack("<I", d[0x1c:0x20])[0]
                    self.authenticated.clear()
                    self._send_login()
                else:
                    print(f"[ctrl] token renewal: unexpected response 0x{resp:08x}", flush=True)
            elif reply == 0x02 and reqtype == 0x01:
                # ack of our token REMOVAL (sent at shutdown) — nothing to do,
                # but recognizing it keeps the dispatch honest.
                print("[ctrl] token removal acknowledged", flush=True)
            return

        # CAPABILITIES (0x42 + N*0x66; 1 radio = 0xa8): parse MAC/name, request stream
        if ln >= 0x42 + 0x66 and (ln - 0x42) % 0x66 == 0:
            if not self.stream_ready.is_set():
                self._parse_caps(d)
                self._send_conninfo()
            return

        # STATUS (0x50): the assigned civ/audio ports (big-endian), OR a
        # radio-initiated DISCONNECT (disc=0x01 — timeout, another client took
        # the slot, front-panel change...). Transport-audit find: we used to
        # ignore the disconnect entirely and sit orphaned with stale streams.
        if ln == 0x50:
            err = struct.unpack("<I", d[0x30:0x34])[0]
            disc = d[0x40]
            if err == 0 and disc == 0x01:
                print("[ctrl] RADIO DISCONNECTED US (status disc=1) - flagging "
                      "for session recycle", flush=True)
                self.radio_disconnected = True
                self.authenticated.clear()
                self.stream_ready.clear()
                return
            if err == 0 and not disc:
                self.civ_port = struct.unpack(">H", d[0x42:0x44])[0]
                self.audio_port = struct.unpack(">H", d[0x46:0x48])[0]
                if self.civ_port and not self.stream_ready.is_set():
                    self.stream_ready.set()
                    if self.on_civ_ports:
                        self.on_civ_ports(self.civ_port, self.audio_port)
            return

    def _parse_caps(self, d):
        # first radio_cap block at 0x42; mac@0x0a within it, name@0x10 (32)
        base = 0x42
        self.mac = bytes(d[base + 0x0a:base + 0x10])
        self._dev_name = bytes(d[base + 0x10:base + 0x30]).split(b"\x00")[0]
        # commoncap is a native quint16 in the packed struct -> little-endian on the
        # LE wire (bytes 10 80 -> 0x8010). SDR9700 compares == 0x8010 to use MAC.
        cc = struct.unpack("<H", d[base + 0x07:base + 0x09])[0]
        self.use_guid = (cc != 0x8010)

    # public ---------------------------------------------------------------
    _dev_name = b"IC-9700"
    _fail = None

    def connect(self, timeout=8.0):
        """Run discovery+auth; return True when civ port is known."""
        self.start()
        return self.stream_ready.wait(timeout)
