#
# Aether-gate - IC-9700 LAN control-stream handler (auth state machine).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
# Ported from github.com/w5jwp/SDR9700 (GPL-3.0) by Justin W5JWP: UdpHandler.cpp/.h.
# Attribution preserved. FAITHFUL PORT: the discovery -> login -> token ->
# capabilities -> conninfo -> status sequence, the innerseq/tokrequest handling,
# and the 60 s token renewal are matched to SDR9700 exactly.
#
"""Control-stream auth for the IC-9700 LAN protocol.

Ports SDR9700's UdpHandler. Runs on the shared threaded UdpBase so the
ping(500ms) / idle(100ms) / retransmit(100ms) cadence flows continuously through
the whole auth dialog (armed at "I am here", like the reference's init/dataReceived).

Sequence, straight from UdpHandler.cpp:
    are-you-there (0x03)  -->  I-am-here (0x04)  --> arm ping+idle, reply 0x06
    I-am-ready (0x06)     -->  sendLogin()
    login_response (0x60) -->  match tokrequest, token=..., sendToken(0x02),
                               tokenTimer(60s), isAuthenticated=true
    token renewal reply   -->  0x00 renew OK (+ sendRequestStream if !streamOpened);
                               0xffffffff -> re-login on the same session
    capabilities packet   -->  parse radios; if 1 radio, setCurrentRadio ->
                               sendRequestStream()
    status (0x50)         -->  civPort/audioPort (big-endian) or disc=1 kick
"""
import random
import socket
import struct
import threading
import time

from .udpbase import (UdpBase, TOKEN_RENEWAL, AREYOUTHERE_PERIOD, CONTROL_SIZE,
                      PING_SIZE, TOKEN_SIZE, STATUS_SIZE, LOGIN_RESPONSE_SIZE,
                      LOGIN_SIZE, CONNINFO_SIZE, CAPABILITIES_SIZE, RADIO_CAP_SIZE)
from .obfuscation import obfuscate

# Reference constants (UdpHandler.cpp anonymous namespace).
_LOGIN_ERROR_INVALID_CREDENTIALS = 0xFEFFFFFF
_AREYOUTHERE_LIMIT = 20            # sendAreYouThere gives up at counter==20


class Ic9700Handler(UdpBase):
    def __init__(self, local_ip, radio_ip, radio_port, username, password,
                 name="aether-gate"):
        super().__init__(local_ip, radio_ip, radio_port, bind_port=0, name="ctrl")
        self.username = username
        self.password = password
        # compName = clientName[:8] + "-" + APP_NAME (reference); simplified to
        # a bounded latin-1 client name (radio only cares it fits its field).
        self.client_name = (name or "aether-gate")[:16]

        # tokRequest: RANDOM per login (UdpHandler::sendLogin uses
        # QRandomGenerator::generate()). Must NOT be deterministic across runs or
        # the radio collides it with our own dead sessions.
        self._tok_request = (random.getrandbits(16) & 0xFFFF) or 1
        self.token = 0
        self.mac = b"\x00" * 6
        self.use_guid = False
        self._dev_name = b"IC-9700"

        # --- seam attributes icom9700.py / civ construction rely on ----------
        self.civ_port = None
        self.audio_port = None
        self.civ_local_port = 0
        self.audio_local_port = 0
        self._civ_sock = None
        self._audio_sock = None
        self._fail = None
        self.radio_disconnected = False   # radio sent status disc=1 (kicked us)

        self.authenticated = threading.Event()
        self.stream_ready = threading.Event()
        self.on_civ_ports = None          # callback(civ_port, audio_port)

        # token renewal cadence
        self._last_token_renewal = 0.0
        self._token_on = False

        self._stream_opened = False
        self.on_data = None               # UdpBase reader defers to _dispatch_subclass

    # ==================================================================== #
    #  public seam: connect()                                               #
    # ==================================================================== #
    def connect(self, timeout=10.0):
        """UdpHandler::init + run discovery/auth; return True once the radio has
        handed us the civ/audio ports (stream_ready)."""
        self.init()                        # UdpBase::init(0): mono clock + retransmit
        self._areyouthere_on = True        # areYouThereTimer->start(AREYOUTHERE_PERIOD)
        self._last_areyouthere = 0.0
        self.start()                       # reader + cadence threads
        return self.stream_ready.wait(timeout)

    def stop(self):
        # UdpHandler::shutdown: if authenticated, send a token REMOVAL
        # (sendToken 0x01) and wait briefly for the disconnect ack, then the
        # base sends the 0x05 control and closes the socket.
        if self.authenticated.is_set():
            try:
                self._send_token(0x01)
                time.sleep(0.3)
            except Exception:
                pass
        super().stop()

    @property
    def streamOpened(self):
        return self._stream_opened

    # ==================================================================== #
    #  are-you-there (UdpHandler::sendAreYouThere)                          #
    # ==================================================================== #
    def _on_areyouthere_tick(self):
        if self.areyouthere_counter == _AREYOUTHERE_LIMIT:
            print(f"[{self.name}] radio not responding (are-you-there x"
                  f"{_AREYOUTHERE_LIMIT})", flush=True)
            self._fail = "radio not responding"
            self._areyouthere_on = False
            return
        self.areyouthere_counter += 1
        self.send_control(False, 0x03, 0x00)

    # ==================================================================== #
    #  semantic dispatch (UdpHandler::dataReceived switch by length)        #
    # ==================================================================== #
    def _dispatch_subclass(self, r):
        length = len(r)
        typ = struct.unpack("<H", r[4:6])[0]

        if length == CONTROL_SIZE:
            if typ == 0x04:
                # "I am here" - arm ping + idle once (reference guards on the
                # are-you-there timer still being active).
                if self._areyouthere_on:
                    self._areyouthere_on = False
                    self._ping_on = True
                    self._idle_on = True
                    now = time.monotonic()
                    self._last_ping = now
                    self._last_idle = now
            elif typ == 0x06:
                # "I am ready" -> send login.
                self._send_login()
            return

        if length == PING_SIZE:
            # Ping reply latency accounting lives in the reference's status
            # message building; not needed for the transport, so skipped here.
            return

        if length == TOKEN_SIZE:
            self._on_token(r)
            return

        if length == STATUS_SIZE:
            self._on_status(r)
            return

        if length == LOGIN_RESPONSE_SIZE:
            self._on_login_response(r)
            return

        if length == CONNINFO_SIZE:
            self._on_conninfo(r)
            return

        # default: capabilities (0x42 + N*0x66)
        if length >= CAPABILITIES_SIZE:
            self._on_capabilities(r)
            return

    # ---- token (0x40) ----
    def _on_token(self, r):
        reqreply = r[0x14]
        reqtype = r[0x15]
        typ = struct.unpack("<H", r[4:6])[0]
        if reqtype == 0x05 and reqreply == 0x02 and typ != 0x01:
            resp = struct.unpack("<I", r[0x30:0x34])[0]
            if resp == 0x00000000:
                # Token renewal successful - restart the 60 s timer; open the
                # stream if not yet open.
                self._token_on = True
                self._last_token_renewal = time.monotonic()
                if not self._stream_opened:
                    self._send_request_stream()
            elif resp == 0xFFFFFFFF:
                # Radio rejected token renewal -> perform login on this session.
                self.remote_id = struct.unpack("<I", r[8:12])[0]
                self._tok_request = struct.unpack("<H", r[0x1a:0x1c])[0]
                self.token = struct.unpack("<I", r[0x1c:0x20])[0]
                self._stream_opened = False
                self.stream_ready.clear()
                self._send_login()
        elif reqtype == 0x01 and reqreply == 0x02 and typ != 0x01:
            self.radio_disconnected_ack = True   # token removal acknowledged

    # ---- status (0x50) ----
    def _on_status(self, r):
        typ = struct.unpack("<H", r[4:6])[0]
        if typ == 0x01:
            return
        err = struct.unpack("<I", r[0x30:0x34])[0]
        disc = r[0x40]
        if err == 0xFFFFFFFF and not self._stream_opened:
            self._fail = "connection failed (try rebooting the radio)"
            return
        if err == 0x00000000 and disc == 0x01:
            # Radio-initiated disconnect (kicked us).
            self.radio_disconnected = True
            self.authenticated.clear()
            self.stream_ready.clear()
            self._stream_opened = False
            return
        # Otherwise: the assigned civ/audio ports (big-endian on the wire).
        civ_port = struct.unpack(">H", r[0x42:0x44])[0]
        audio_port = struct.unpack(">H", r[0x46:0x48])[0]
        if civ_port == 0 or audio_port == 0:
            self._fail = "radio returned invalid UDP stream ports"
            self._stream_opened = False
            return
        self.civ_port = civ_port
        self.audio_port = audio_port
        if not self._stream_opened:
            self._stream_opened = True
            if not self.stream_ready.is_set():
                self.stream_ready.set()
                if self.on_civ_ports:
                    try:
                        self.on_civ_ports(self.civ_port, self.audio_port)
                    except Exception:
                        pass

    # ---- login response (0x60) ----
    def _on_login_response(self, r):
        typ = struct.unpack("<H", r[4:6])[0]
        if typ == 0x01:
            return
        err = struct.unpack("<I", r[0x30:0x34])[0]
        if err == _LOGIN_ERROR_INVALID_CREDENTIALS:
            self._fail = "invalid username/password"
            return
        if not self.authenticated.is_set():
            # Reference: only authenticate when the reply's tokrequest matches
            # the one we sent (guards against a reply meant for a stale login).
            got_tr = struct.unpack("<H", r[0x1a:0x1c])[0]
            if got_tr != self._tok_request:
                return
            self.token = struct.unpack("<I", r[0x1c:0x20])[0]
            self._send_token(0x02)             # token confirm
            self._token_on = True              # tokenTimer->start(TOKEN_RENEWAL)
            self._last_token_renewal = time.monotonic()
            self.authenticated.set()
            self.is_authenticated = True

    # ---- conninfo (0x90) ----
    def _on_conninfo(self, r):
        # Per-radio availability status. The reference matches by MAC/GUID and,
        # for a single radio, either connects or reports "in use". We already
        # drive the stream request off capabilities; here we just surface busy.
        busy = struct.unpack("<I", r[0x60:0x64])[0] if len(r) >= 0x64 else 0
        if busy:
            print("[ctrl] radio reports BUSY (in use by another client)", flush=True)

    # ---- capabilities (0x42 + N*0x66) ----
    def _on_capabilities(self, r):
        length = len(r)
        cap_bytes = length - CAPABILITIES_SIZE
        if cap_bytes <= 0 or cap_bytes % RADIO_CAP_SIZE != 0:
            return
        if self.stream_ready.is_set():
            return
        # First radio_cap block begins at CAPABILITIES_SIZE (0x42).
        base = CAPABILITIES_SIZE
        # radio_cap layout: guid/mac union @0x00 (mac at +0x0a), name @0x10 (32),
        # commoncap @0x07. commoncap == 0x8010 -> use MAC, else GUID.
        cc = struct.unpack("<H", r[base + 0x07:base + 0x09])[0]
        self.use_guid = (cc != 0x8010)
        self.mac = bytes(r[base + 0x0a:base + 0x10])
        self._dev_name = bytes(r[base + 0x10:base + 0x30]).split(b"\x00")[0]
        # setCurrentRadio(0) -> reserve local ports -> sendRequestStream.
        self._send_request_stream()

    # ==================================================================== #
    #  auth packet builders                                                 #
    # ==================================================================== #
    def _send_login(self):
        """UdpHandler::sendLogin. tokRequest randomised per call."""
        self._tok_request = (random.getrandbits(16) & 0xFFFF) or 1
        b = bytearray(LOGIN_SIZE)
        struct.pack_into("<IHHII", b, 0, LOGIN_SIZE, 0x0000, 0x0000,
                         self.my_id, self.remote_id)
        struct.pack_into(">I", b, 0x10, LOGIN_SIZE - 0x10)     # payloadsize (BE)
        b[0x14] = 0x01                                          # requestreply
        b[0x15] = 0x00                                          # requesttype login
        struct.pack_into(">H", b, 0x16, self.auth_seq & 0xFFFF)  # innerseq (BE)
        self.auth_seq += 1
        struct.pack_into("<H", b, 0x1a, self._tok_request)     # tokrequest (LE)
        b[0x40:0x50] = obfuscate(self.username)                # username (encoded)
        b[0x50:0x60] = obfuscate(self.password)                # password (encoded)
        nm = self.client_name.encode("latin-1")[:16]
        b[0x60:0x60 + len(nm)] = nm                            # name
        self.send_tracked(bytes(b))

    def _send_token(self, magic):
        """UdpHandler::sendToken(magic). Same shape, different requesttype:
        0x02 = confirm, 0x05 = renewal, 0x01 = removal (at shutdown)."""
        b = bytearray(TOKEN_SIZE)
        struct.pack_into("<IHHII", b, 0, TOKEN_SIZE, 0x0000, 0x0000,
                         self.my_id, self.remote_id)
        struct.pack_into(">I", b, 0x10, TOKEN_SIZE - 0x10)     # payloadsize (BE)
        b[0x14] = 0x01                                          # requestreply
        b[0x15] = magic & 0xFF                                  # requesttype
        struct.pack_into(">H", b, 0x16, self.auth_seq & 0xFFFF)  # innerseq (BE)
        self.auth_seq += 1
        struct.pack_into("<H", b, 0x1a, self._tok_request)     # tokrequest (LE)
        # resetcap @0x24 is BIG-endian on the wire (qToBigEndian(0x0798)).
        struct.pack_into(">H", b, 0x24, 0x0798)                # resetcap (BE)
        struct.pack_into("<I", b, 0x1c, self.token)            # token (LE)
        self.send_tracked(bytes(b))

    def _send_request_stream(self):
        """UdpHandler::sendRequestStream (conninfo_packet, requesttype 0x03).
        Reserves the local civ/audio ports first (setCurrentRadio reserves them
        via bound QUdpSockets); we bind two sockets and keep them for civ/audio."""
        if self.civ_local_port == 0 or self.audio_local_port == 0:
            cs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            cs.bind((self.local_ip, 0))
            as_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            as_.bind((self.local_ip, 0))
            self.civ_local_port = cs.getsockname()[1]
            self.audio_local_port = as_.getsockname()[1]
            self._civ_sock = cs
            self._audio_sock = as_

        b = bytearray(CONNINFO_SIZE)
        struct.pack_into("<IHHII", b, 0, CONNINFO_SIZE, 0x0000, 0x0000,
                         self.my_id, self.remote_id)
        struct.pack_into(">I", b, 0x10, CONNINFO_SIZE - 0x10)  # payloadsize (BE)
        b[0x14] = 0x01                                          # requestreply
        b[0x15] = 0x03                                          # requesttype stream req
        struct.pack_into(">H", b, 0x16, self.auth_seq & 0xFFFF)  # innerseq (BE)
        self.auth_seq += 1
        struct.pack_into("<H", b, 0x1a, self._tok_request)     # tokrequest (LE)
        struct.pack_into("<I", b, 0x1c, self.token)            # token (LE)
        if not self.use_guid:
            struct.pack_into("<H", b, 0x27, 0x8010)            # commoncap (native LE)
            b[0x2a:0x30] = self.mac                            # macaddress
        # name @0x40 (32) = devName from capabilities.
        dn = self._dev_name[:32]
        b[0x40:0x40 + len(dn)] = dn
        # requested-streams union @0x60.
        b[0x60:0x70] = obfuscate(self.username)               # username (encoded)
        b[0x70] = 0x01                                          # rxenable
        b[0x71] = 0x00                                          # txenable (RX only)
        b[0x72] = 0x04                                          # rxcodec (LPCM16)
        b[0x73] = 0x00                                          # txcodec
        struct.pack_into(">I", b, 0x74, 48000)                 # rxsample (BE)
        struct.pack_into(">I", b, 0x78, 0)                     # txsample (BE)
        struct.pack_into(">I", b, 0x7c, self.civ_local_port)   # civport (BE)
        struct.pack_into(">I", b, 0x80, self.audio_local_port) # audioport (BE)
        struct.pack_into(">I", b, 0x84, 0)                     # txbuffer (BE)
        b[0x88] = 0x01                                          # convert
        self.send_tracked(bytes(b))

    # ==================================================================== #
    #  token-renewal cadence (tokenTimer -> sendToken(0x05) every 60 s)     #
    # ==================================================================== #
    def _on_tick(self, now):
        if self._token_on and self.authenticated.is_set():
            if now - self._last_token_renewal >= TOKEN_RENEWAL:
                try:
                    self._send_token(0x05)
                except Exception:
                    pass
                self._last_token_renewal = now
