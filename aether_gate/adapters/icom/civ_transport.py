#
# Aether-gate - IC-9700 LAN CI-V stream transport (open + watchdog + scope parse).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
# Ported from github.com/w5jwp/SDR9700 (GPL-3.0) by Justin W5JWP: UdpCivData.cpp/.h.
# Attribution preserved. FAITHFUL PORT of the CI-V data channel TRANSPORT half:
# sendOpenClose, the 2 s watchdog that re-opens the stream on stall, the
# openclose/civ data-packet wire formats, and the '27 00 00 ... FD' scope-frame
# extraction (incl. the splitWaterfall subframe split). The CI-V command building
# for freq/mode and the freq/mode state tracking are NOT here - they live in the
# adapter subclass (_Ic9700Stream in icom9700.py), which overrides on_data.
#
"""CI-V data stream transport for the IC-9700 LAN protocol.

Ports SDR9700's UdpCivData. On the radio-assigned CI-V port it:
  1. runs the are-you-there / 0x06 handshake (UdpBase),
  2. on "I am ready" (0x06) sends the data-stream OPEN (openclose magic 0x04)
     and arms startCivDataTimer to re-send it every 100 ms until frames flow,
  3. runs a 500 ms watchdog: >2 s with no CI-V data -> re-arm the 100 ms open
     retries (sendOpenClose(false)) until data resumes (the reference's exact
     stall-recovery - no re-login, no session churn),
  4. parses the '27 00 00 ... FD' scope waveform frames and (when splitWaterfall)
     splits the 490-byte payload into 11 subframes.

Subclasses set self.on_data (the reference's `emit receive`) to consume the
CI-V/scope payloads; this transport calls it for every accepted CI-V datagram.
"""
import struct

from .udpbase import (UdpBase, WATCHDOG_PERIOD, CONTROL_SIZE, DATA_SIZE)

# CI-V controller address for frames WE originate (FE FE <radio> E0 ...).
CONTROLLER_CIV = 0xE0

# openclose start-retry period (UdpCivData startCivDataTimer->start(100)).
_START_CIV_RETRY = 0.100
# watchdog: >2 s with no CI-V data -> request a data (re)start.
_WATCHDOG_STALL_S = 2.0

# Scope conversion (ScopeAdapter mapping, kept with the transport so latest_dbm
# is produced here as the seam requires: -130 .. -10 dBm over raw 0..159).
SCOPE_MIN_DBM = -130.0
SCOPE_MAX_DBM = -10.0
SCOPE_SPAN = SCOPE_MAX_DBM - SCOPE_MIN_DBM

# splitWaterfall constants (UdpCivData.cpp).
_WF_PAYLOAD_BYTES = 490
_WF_DIVISIONS = 11
_WF_DIVISION_BYTES = 50
_WF_FIRST_DATA_OFFSET = 12

# scope-frame layout after the "27 00 00" marker (matches ours/civ.py):
#   marker+2 = pos; seq1 mode/bounds header = d[pos+3 : pos+3+12];
#   waveform pixels begin at pos+3+12 = marker+17.
_SCOPE_HDR_AFTER_MARKER = 17
_SCOPE_BOUNDS_START = 5


def _byte_to_dbm(b):
    if b > 159:
        b = 159
    return SCOPE_MIN_DBM + (b / 159.0) * SCOPE_SPAN


class UdpCivData(UdpBase):
    """Port of SDR9700 UdpCivData - the CI-V data channel transport.

    Construct with the control handler's pre-reserved civ socket (the radio
    streams to the civ_local_port advertised in conninfo). Call start()."""

    def __init__(self, local_ip, radio_ip, civ_port, sock, split_wf=False,
                 civ_addr=0xA2, name="civ"):
        # Adopt the pre-bound reservation socket rather than binding a new port.
        self.local_ip = local_ip
        self.radio_ip = radio_ip
        self.radio_port = civ_port
        self.name = name
        self.sock = sock
        self.sock.settimeout(0.05)
        self.local_port = sock.getsockname()[1]

        # Bring up UdpBase state without re-binding (mirror __init__ minus socket).
        self._init_ids(local_ip)
        self.send_seq = 1
        self.send_seq_b = 0
        self.auth_seq = 0x30
        self.ping_send_seq = 0
        self.is_authenticated = False

        import threading
        self._lock = threading.Lock()
        self._tx_seq_buf = {}
        self._rx_seq_buf = {}
        self._rx_missing = {}
        self.packets_sent = 0
        self.packets_lost = 0
        self._mono_start = None
        self._last_received_ms = 0
        self._last_ping_sent_ms = 0
        self._ping_have_sync = False
        self._ping_radio_base = 0
        self._ping_local_base = 0
        self._ping_baseline_valid = False
        self._ping_lateness_ms = 0
        self._ping_baseline_ms = 0
        self._t_reader = None
        self._t_timers = None
        self._run = False
        self._retransmit_on = False
        self._ping_on = False
        self._idle_on = False
        self._areyouthere_on = False
        self._last_retransmit = 0.0
        self._last_ping = 0.0
        self._last_idle = 0.0
        self._last_areyouthere = 0.0
        self.areyouthere_counter = 0
        self.n_sent = 0
        self.n_retx_req = 0
        self.n_rx_clears = 0
        self.n_rx_dgrams = 0
        self.last_rx_at = 0.0
        self.on_data = self._on_civ

        # --- CIV-specific -----------------------------------------------------
        self.civ_addr = civ_addr          # radio's CI-V address (frames -> radio)
        self.split_waterfall = split_wf
        self._watchdog_alerted = False    # m_watchdogAlerted
        self._close_sent = False          # m_closeSent
        self._start_civ_on = False        # startCivDataTimer active?
        self._last_start_civ = 0.0
        self._watchdog_on = False
        self._last_watchdog = 0.0
        self._ready_seen = False

        # --- seam attributes the adapter + engine read ------------------------
        self.frames = 0                   # scope-frame counter (increments per frame)
        self.latest_dbm = None            # scope sweep as dBm list
        self.freq_hz = None               # adapter fills via its on_data override
        self.mode = None
        self.smeter_raw = None
        # scope diagnostics (kept from ours/civ.py)
        self.first_raw = None
        self.bounds_raw = None
        self.max_byte = 0
        self.best_raw = None
        self.dgram_lens = {}
        self.samples = []
        self.n_fb = 0
        self.n_fa = 0
        self.scope_only = False

    # ==================================================================== #
    #  start(): init + handshake + arm the CIV timers                       #
    # ==================================================================== #
    def start(self):
        """UdpCivData ctor sequence: init, send are-you-there, arm ping/idle/
        areyouthere/watchdog, then start the reader + cadence threads."""
        self.init()                       # UdpBase::init(localPort): mono + retransmit
        self.send_control(False, 0x03, 0x00)   # kick discovery immediately
        self._ping_on = True
        self._idle_on = True
        self._areyouthere_on = True
        self._watchdog_on = True
        now_reset = 0.0
        self._last_ping = now_reset
        self._last_idle = now_reset
        self._last_areyouthere = now_reset
        self._last_watchdog = now_reset
        super().start()

    def stop(self):
        # UdpCivData::closeStream: send the data-stream CLOSE (openclose magic
        # 0x00) before the base 0x05 disconnect, once.
        if not self._close_sent:
            self._close_sent = True
            self._start_civ_on = False
            try:
                self._send_openclose(opening=False)
            except Exception:
                pass
        super().stop()

    # UdpCivData ties areYouThereTimer to a plain sendControl(false,0x03,0).
    def _on_areyouthere_tick(self):
        self.send_control(False, 0x03, 0x00)

    # ==================================================================== #
    #  discovery hooks                                                      #
    # ==================================================================== #
    def _on_iamready(self):
        """0x06 'I am ready' -> open the CI-V data stream and arm the 100 ms
        open-retry (UdpCivData::dataReceived control 0x06 branch)."""
        self._ready_seen = True
        self._send_openclose(opening=True)
        self._start_civ_on = True
        self._last_start_civ = 0.0

    # ==================================================================== #
    #  cadence hook: startCivDataTimer + watchdog                           #
    # ==================================================================== #
    def _on_tick(self, now):
        # startCivDataTimer: re-send the data-stream open every 100 ms until the
        # first frame arrives (reference stops it once valid CI-V data flows).
        if self._start_civ_on and now - self._last_start_civ >= _START_CIV_RETRY:
            self._last_start_civ = now
            try:
                self._send_openclose(opening=False)
            except Exception:
                pass
        # watchdog (WATCHDOG_PERIOD = 500 ms).
        if self._watchdog_on and now - self._last_watchdog >= WATCHDOG_PERIOD:
            self._last_watchdog = now
            self._watchdog()

    def _watchdog(self):
        """UdpCivData::watchdog. >2 s since the last accepted CI-V datagram ->
        (re-)arm the 100 ms open retries; reset the alert once data resumes."""
        if self._ms_since_last_received() > int(_WATCHDOG_STALL_S * 1000):
            if not self._watchdog_alerted:
                self._start_civ_on = True
                self._last_start_civ = 0.0
                self._watchdog_alerted = True
        else:
            self._watchdog_alerted = False

    # ==================================================================== #
    #  0x06 in the CIV subclass captures remoteId + opens (dataReceived)    #
    # ==================================================================== #
    def _dispatch_subclass(self, r):
        length = len(r)
        typ = struct.unpack("<H", r[4:6])[0]

        if length == CONTROL_SIZE:
            if typ == 0x04:
                # "I am here" - stop the are-you-there timer (reference).
                self._areyouthere_on = False
            elif typ == 0x06:
                # "I am ready" - capture remoteId, open the stream + arm retry.
                self.remote_id = struct.unpack("<I", r[8:12])[0]
                self._on_iamready()
            return

        # default: a CI-V / scope datagram (len > 21 in the reference).
        if length > 21:
            self._on_civ_datagram(r)

    def _on_civ_datagram(self, r):
        """UdpCivData::dataReceived default branch: validate the data_packet
        header, stop the start-retry, mark received, then extract/emit the
        scope frame (splitting the waterfall payload when split_waterfall)."""
        typ = struct.unpack("<H", r[4:6])[0]
        if typ == 0x01:
            return
        hdr_len = struct.unpack("<I", r[0:4])[0]
        if hdr_len != len(r):
            return                        # mismatched length -> drop
        datalen = struct.unpack("<H", r[0x11:0x13])[0]
        if datalen + DATA_SIZE != hdr_len:
            return                        # mismatched payload length -> drop

        # Valid CI-V data: stop the start-retry, mark the session alive.
        self._start_civ_on = False
        self._mark_packet_received()

        marker = r.find(b"\x27\x00\x00")
        pos = marker + 2 if marker >= 0 else -1
        length = -1
        if pos >= 0:
            fd = r[pos:].find(b"\xfd")
            length = fd if fd >= 0 else -1

        if self.split_waterfall and pos >= 6 and length >= 490:
            self._emit_split_waterfall(r, pos, length)
        else:
            # Reference: emit receive(r.mid(0x15)) - the CI-V payload after the
            # data header. on_data both extracts the scope (default _on_civ) AND
            # runs the adapter's semantic parse (its override, which calls the
            # inherited _on_civ so scope extraction still happens exactly once).
            if self.on_data:
                self.on_data(bytes(r[0x15:]))

    def _emit_split_waterfall(self, r, pos, length):
        """UdpCivData splitWaterfall: split the 490-byte payload into 11
        subframes, each a synthesised 9-byte CI-V header + a 50-byte slice."""
        if length != _WF_PAYLOAD_BYTES:
            return
        num = _WF_DIVISIONS
        div = _WF_DIVISION_BYTES
        split_pos = _WF_FIRST_DATA_OFFSET
        for i in range(num):
            wf = bytearray(r[pos - 6:pos - 6 + 9])
            tens = (i + 1) // 10
            units = (i + 1) - (10 * tens)
            wf[7] = units | (tens << 4)
            tens = num // 10
            units = num - (10 * tens)
            wf[8] = units | (tens << 4)
            if i == 0:
                wf += r[pos + 3:pos + 3 + split_pos]
            else:
                start = (pos + split_pos + 3) + ((i - 1) * div)
                wf += r[start:start + div]
            if i < num - 1:
                wf += b"\xfd"
            if self.on_data:
                self.on_data(bytes(wf))

    # ==================================================================== #
    #  scope-frame extraction (produces latest_dbm / frames)                #
    # ==================================================================== #
    def _extract_scope(self, d):
        self.dgram_lens[len(d)] = self.dgram_lens.get(len(d), 0) + 1
        if self.dgram_lens[len(d)] <= 2:
            self.samples.append((len(d), bytes(d).hex()))
        m = d.find(b"\x27\x00\x00")
        if m < 0:
            return
        end = d.find(b"\xfd", m)
        if end < 0:
            return
        if self.first_raw is None:
            self.first_raw = bytes(d[m:end + 1]).hex()
            self.bounds_raw = bytes(
                d[m + _SCOPE_BOUNDS_START:m + _SCOPE_HDR_AFTER_MARKER]).hex()
        pixels = d[m + _SCOPE_HDR_AFTER_MARKER:end]
        if pixels:
            self.latest_dbm = [_byte_to_dbm(x) for x in pixels]
            self.frames += 1
            peak = max(pixels)
            if peak > self.max_byte:
                self.max_byte = peak
                self.best_raw = bytes(d[m:end + 1]).hex()

    # ==================================================================== #
    #  outbound CI-V frames (UdpCivData::send + sendOpenClose)              #
    # ==================================================================== #
    def _send_openclose(self, opening):
        """UdpCivData::sendOpenClose. openclose_packet: data=0x01c0, sendseq (BE)
        from sendSeqB, magic 0x04 (open) / 0x00 (close). sendSeqB++ after."""
        magic = 0x04 if opening else 0x00
        b = bytearray(0x16)
        struct.pack_into("<IHHII", b, 0, 0x16, 0x0000, 0x0000,
                         self.my_id, self.remote_id)
        struct.pack_into("<H", b, 0x10, 0x01C0)               # data
        struct.pack_into(">H", b, 0x13, self.send_seq_b & 0xFFFF)  # sendseq (BE)
        b[0x15] = magic                                        # magic
        self.send_seq_b = (self.send_seq_b + 1) & 0xFFFF
        self.send_tracked(bytes(b))

    def _send_civ(self, civ_cmd):
        """Build a CI-V frame from the command bytes and send it over the data
        channel. SEAM: the adapter (_Ic9700Stream) calls _send_civ(bytes([...]))
        with just the CI-V command (e.g. 25 00 <bcd>); this wraps it as
        FE FE <civ_addr> E0 <cmd...> FD and then applies UdpCivData::send()'s
        data_packet header (reply=0xC1, datalen LE, sendseq BE from sendSeqB)."""
        frame = (bytes([0xFE, 0xFE, self.civ_addr, CONTROLLER_CIV])
                 + bytes(civ_cmd) + bytes([0xFD]))
        self._send_civ_frame(frame)

    def _send_civ_frame(self, civ_frame):
        """UdpCivData::send(d) proper: wrap a COMPLETE CI-V frame in the
        data_packet header (reply=0xC1, datalen LE, sendseq BE), sendSeqB++."""
        d = bytes(civ_frame)
        hdr = bytearray(0x15)
        struct.pack_into("<IHHII", hdr, 0, 0x15 + len(d), 0x0000, 0x0000,
                         self.my_id, self.remote_id)
        hdr[0x10] = 0xC1                                       # reply
        struct.pack_into("<H", hdr, 0x11, len(d))             # datalen (LE)
        struct.pack_into(">H", hdr, 0x13, self.send_seq_b & 0xFFFF)  # sendseq (BE)
        self.send_seq_b = (self.send_seq_b + 1) & 0xFFFF
        self.send_tracked(bytes(hdr) + d)

    # --- default on_data (base scope consumer = ours/civ.py Ic9700Civ._on_civ)
    def _on_civ(self, d):
        """Default CI-V consumer: extract the scope frame from the payload.
        The adapter's on_data override calls this (super()._on_civ or _on_civ)
        so scope extraction still runs exactly once even when overridden."""
        self._extract_scope(d)
