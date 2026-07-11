#
# Aether-gate - IC-9700 LAN UDP base: reliability layer + timers.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
# Ported from github.com/w5jwp/SDR9700 (GPL-3.0) by Justin W5JWP: UdpBase.cpp/.h.
# Attribution preserved. This is a FAITHFUL PORT of the SDR9700 UdpBase engine:
# the seq/ack/retransmit/ping/idle logic and timer periods are matched exactly.
# Do not "improve" the cadences or the RX-seq bookkeeping - the whole point of
# the port is byte-and-behaviour fidelity with the known-working C++.
#
"""Shared UDP transport for the IC-9700 LAN protocol (control / CI-V / audio).

Ports SDR9700's UdpBase. A QUdpSocket + several QTimers in the reference become,
here, one background reader thread plus one 20 ms cadence thread that drives the
same periods measured off a monotonic clock:

    RETRANSMIT_PERIOD  100 ms   (UdpBase::init starts retransmitTimer)
    PING_PERIOD        500 ms   (subclass starts pingTimer)
    IDLE_PERIOD        100 ms   (subclass starts idleTimer; reset on every send)
    AREYOUTHERE_PERIOD 500 ms   (subclass starts areYouThereTimer)
    WATCHDOG_PERIOD    500 ms   (CIV subclass)

The reference keeps the RX-side reliability state in Qt containers under four
mutexes (udpMutex -> txBufferMutex -> rxBufferMutex -> missingMutex). Here a
single threading.Lock guards the equivalent dicts; the CPython GIL plus the
lock make the read-modify-write on those dicts atomic, which is all the C++
mutexes bought. The lock is NEVER held across a blocking socket send.
"""
import socket
import struct
import threading
import time

# --- timing / size constants (PacketTypes.h - THE authority) --------------
# Periods are milliseconds in the C++; kept in seconds here for time.monotonic().
PURGE_SECONDS = 10
TOKEN_RENEWAL = 60.000       # ms 60000 -> s
PING_PERIOD = 0.500          # ms 500
IDLE_PERIOD = 0.100          # ms 100
AREYOUTHERE_PERIOD = 0.500   # ms 500
WATCHDOG_PERIOD = 0.500      # ms 500
RETRANSMIT_PERIOD = 0.100    # ms 100
LOCK_PERIOD = 0.010          # ms 10
STALE_CONNECTION = 15
BUFSIZE = 500
MAX_MISSING = 50
AUDIO_PERIOD = 0.020         # ms 20

# Fixed packet lengths.
CONTROL_SIZE = 0x10
WATCHDOG_SIZE = 0x14
PING_SIZE = 0x15
OPENCLOSE_SIZE = 0x16
RETRANSMIT_RANGE_SIZE = 0x18
TOKEN_SIZE = 0x40
STATUS_SIZE = 0x50
LOGIN_RESPONSE_SIZE = 0x60
LOGIN_SIZE = 0x80
CONNINFO_SIZE = 0x90
CAPABILITIES_SIZE = 0x42
RADIO_CAP_SIZE = 0x66
CIV_SIZE = 0x15
AUDIO_SIZE = 0x18
DATA_SIZE = 0x15

_TICK = 0.020                # cadence-thread sleep; matches AUDIO_PERIOD granularity
_DAY_MS = 24 * 60 * 60 * 1000


def _elapsed_ms(mono_start):
    """ms since the monotonic base - the reference's QElapsedTimer::elapsed()."""
    return int((time.monotonic() - mono_start) * 1000)


def _seq16(x):
    return x & 0xFFFF


class UdpBase:
    """Port of SDR9700 UdpBase. Subclasses (handler / civ) create the sockets,
    set radioIP/port, then call init() and start()."""

    def __init__(self, local_ip, radio_ip, radio_port, bind_port=0, name="base"):
        self.local_ip = local_ip
        self.radio_ip = radio_ip
        self.radio_port = radio_port          # C++ `port`
        self.name = name

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((local_ip, bind_port))
        self.sock.settimeout(0.05)
        self.local_port = self.sock.getsockname()[1]

        # --- session ids (UdpBase::init: myId built from last-2 octets + port) ---
        self._init_ids(local_ip)

        # --- sequence state (mirrors UdpBase.h members) -----------------------
        self.send_seq = 1            # sendSeq (tracked-packet counter; starts 1)
        self.send_seq_b = 0          # sendSeqB (CIV/openclose data seq; BE on wire)
        self.auth_seq = 0x30         # authSeq (innerseq for auth packets)
        self.ping_send_seq = 0       # pingSendSeq
        self.is_authenticated = False

        # reliability buffers (reference: txSeqBuf / rxSeqBuf / rxMissing)
        self._lock = threading.Lock()
        self._tx_seq_buf = {}        # seq -> {"data": bytes, "time_ms": int, "retx": int}
        self._rx_seq_buf = {}        # seq -> received_at_ms
        self._rx_missing = {}        # seq -> request-count

        # congestion accounting
        self.packets_sent = 0
        self.packets_lost = 0

        # monotonic clock (QElapsedTimer mono) - set in init()
        self._mono_start = None
        self._last_received_ms = 0   # lastReceivedMs

        # ping/sync bookkeeping (per-instance, as the reference requires)
        self._last_ping_sent_ms = 0
        self._ping_have_sync = False
        self._ping_radio_base = 0
        self._ping_local_base = 0
        self._ping_baseline_valid = False
        self._ping_lateness_ms = 0
        self._ping_baseline_ms = 0

        # timer enable flags (subclasses turn these on to arm a QTimer equivalent)
        self._t_reader = None
        self._t_timers = None
        self._run = False

        # cadence bookkeeping (monotonic-time "last fired" per timer)
        self._retransmit_on = False
        self._ping_on = False
        self._idle_on = False
        self._areyouthere_on = False
        self._last_retransmit = 0.0
        self._last_ping = 0.0
        self._last_idle = 0.0
        self._last_areyouthere = 0.0

        # discovery
        self.areyouthere_counter = 0  # areYouThereCounter

        # --- Aether-gate seam: diagnostic counters + on_data hook -------------
        # (Kept for icom9700.py's diagnostics()/watchdog; not part of SDR9700.)
        self.n_sent = 0
        self.n_retx_req = 0
        self.n_rx_clears = 0
        self.n_rx_dgrams = 0
        self.last_rx_at = 0.0
        self.on_data = None          # callable(datagram_bytes) for non-control payloads

    def _init_ids(self, local_ip):
        o = local_ip.split(".")
        addr_hi = int(o[2]) & 0xFF
        addr_lo = int(o[3]) & 0xFF
        # myId = (addr>>8 & 0xff)<<24 | (addr & 0xff)<<16 | (localPort & 0xffff)
        # where addr is the 32-bit IPv4: >>8&0xff is the 3rd octet, &0xff the 4th.
        self.my_id = (addr_hi << 24) | (addr_lo << 16) | (self.local_port & 0xFFFF)
        self.remote_id = 0

    # ==================================================================== #
    #  init / lifecycle                                                     #
    # ==================================================================== #
    def init(self):
        """UdpBase::init(bindPort). Socket already bound in __init__; this arms
        the monotonic clock, seeds lastReceived, and enables the retransmit
        cadence (the reference starts retransmitTimer here unconditionally)."""
        self._mono_start = time.monotonic()
        self._mark_packet_received()
        self._retransmit_on = True

    def _elapsed_ms(self):
        if self._mono_start is None:
            return 0
        return _elapsed_ms(self._mono_start)

    def _ms_since_last_received(self):
        return self._elapsed_ms() - self._last_received_ms

    def _mark_packet_received(self):
        self._last_received_ms = self._elapsed_ms()

    def start(self):
        """Spin the reader + cadence threads. Subclasses arm their timers
        (ping/idle/areyouthere/watchdog) before or after calling this."""
        self._run = True
        self._t_reader = threading.Thread(target=self._reader, daemon=True,
                                           name=f"{self.name}-rx")
        self._t_timers = threading.Thread(target=self._cadence, daemon=True,
                                          name=f"{self.name}-timers")
        self._t_reader.start()
        self._t_timers.start()

    def stop(self):
        """~UdpBase(): send the 0x05 control (disconnect/idle-close) then close
        the socket. Reference does sendControl(false,0x05,0x00) in the dtor."""
        self._run = False
        try:
            self.send_control(False, 0x05, 0x00)
        except Exception:
            pass
        try:
            if self.sock is not None:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    # ==================================================================== #
    #  low-level send                                                       #
    # ==================================================================== #
    def _write(self, data):
        """udp->writeDatagram(data, radioIP, port). Never called under _lock."""
        try:
            self.sock.sendto(data, (self.radio_ip, self.radio_port))
            return len(data)
        except (OSError, AttributeError):     # closed / set None by stop()
            return -1

    def _control_bytes(self, typ, seq=0, ln=CONTROL_SIZE):
        return struct.pack("<IHHII", ln, typ, seq, self.my_id, self.remote_id)

    def send_control(self, tracked, typ, seq):
        """UdpBase::sendControl(bool tracked, quint8 type, quint16 seq).
        A zeroed control packet; untracked ones carry `seq` verbatim, tracked
        ones go through sendTrackedPacket (which stamps sendSeq)."""
        if self.sock is None:
            return
        if not tracked:
            self._write(self._control_bytes(typ, seq))
        else:
            # tracked: seq field is overwritten by sendTrackedPacket
            self.send_tracked(self._control_bytes(typ, 0))

    def send_tracked(self, buf):
        """UdpBase::sendTrackedPacket. Stamp sendSeq into [6:8], keep a copy in
        txSeqBuf for retransmit, purge old entries, bump sendSeq, then send and
        (per reference) restart the idle timer if it is active."""
        if self.sock is None:
            return None
        if len(buf) < CONTROL_SIZE:
            return None
        b = bytearray(buf)
        with self._lock:
            seq = self.send_seq
            struct.pack_into("<H", b, 6, _seq16(seq))
            if seq == 0:                       # rollover starts a new window
                self._tx_seq_buf.clear()
            if len(self._tx_seq_buf) > BUFSIZE:
                self._tx_seq_buf.pop(next(iter(self._tx_seq_buf)))
            self._tx_seq_buf[seq] = {"data": bytes(b),
                                     "time_ms": self._elapsed_ms(),
                                     "retx": 0}
            self.send_seq = _seq16(self.send_seq + 1)
            self.n_sent += 1
        self._purge_old_entries()
        ret = self._write(bytes(b))
        # Reference resets idleTimer (start(IDLE_PERIOD)) inside sendTrackedPacket
        # if it's active: any tracked traffic defers the next idle keepalive.
        if self._idle_on:
            self._last_idle = time.monotonic()
        if ret is not None and ret >= 0:
            self.packets_sent += 1
        return seq

    def send_ping(self):
        """UdpBase::sendPing. type=0x07, reply implicit 0, time = ms-since-start-
        of-day. Reference uses QTime::currentTime().msecsSinceStartOfDay()."""
        if self.sock is None:
            return
        p = bytearray(PING_SIZE)
        struct.pack_into("<IHHII", p, 0, PING_SIZE, 0x07, self.ping_send_seq,
                         self.my_id, self.remote_id)
        p[0x10] = 0x00                          # reply
        struct.pack_into("<I", p, 0x11, self._msecs_since_start_of_day())
        self._last_ping_sent_ms = self._elapsed_ms()
        self._write(bytes(p))

    @staticmethod
    def _msecs_since_start_of_day():
        lt = time.localtime()
        secs = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
        frac_ms = int((time.time() % 1) * 1000)
        return (secs * 1000 + frac_ms) & 0xFFFFFFFF

    # ==================================================================== #
    #  purge old buffer entries (UdpBase::purgeOldEntries)                  #
    # ==================================================================== #
    def _purge_old_entries(self):
        now = self._elapsed_ms()
        with self._lock:
            # txSeqBuf: drop entries older than PURGE_SECONDS (ordered by insert,
            # so stop at the first fresh one - reference breaks out of the loop).
            for seq in list(self._tx_seq_buf.keys()):
                if now - self._tx_seq_buf[seq]["time_ms"] > PURGE_SECONDS * 1000:
                    del self._tx_seq_buf[seq]
                else:
                    break
            # rxSeqBuf: same age-out.
            for seq in list(self._rx_seq_buf.keys()):
                if now - self._rx_seq_buf[seq] > PURGE_SECONDS * 1000:
                    del self._rx_seq_buf[seq]
                else:
                    break
            # rxMissing: bound it - if >50, drop the oldest 25 (reference literal).
            if len(self._rx_missing) > 50:
                for _ in range(25):
                    if not self._rx_missing:
                        break
                    self._rx_missing.pop(next(iter(self._rx_missing)))

    # ==================================================================== #
    #  cadence thread (QTimers -> monotonic checks)                         #
    # ==================================================================== #
    def _cadence(self):
        while self._run:
            now = time.monotonic()
            if self._retransmit_on and now - self._last_retransmit >= RETRANSMIT_PERIOD:
                self._send_retransmit_request()
                self._last_retransmit = now
            if self._areyouthere_on and now - self._last_areyouthere >= AREYOUTHERE_PERIOD:
                self._on_areyouthere_tick()
                self._last_areyouthere = now
            if self._ping_on and now - self._last_ping >= PING_PERIOD:
                self.send_ping()
                self._last_ping = now
            if self._idle_on and now - self._last_idle >= IDLE_PERIOD:
                # idleTimer -> sendControl(true, 0, 0): a TRACKED idle keepalive.
                self.send_control(True, 0, 0)
                self._last_idle = now
            self._on_tick(now)                  # subclass watchdog / open-retry hook
            time.sleep(_TICK)

    def _on_areyouthere_tick(self):
        """Default are-you-there behaviour (CIV subclass overrides in the
        reference with a plain sendControl(false,0x03,0); the handler subclass
        has its own counter/limit)."""
        self.send_control(False, 0x03, 0)

    def _on_tick(self, now):
        """Per-cadence hook for subclasses (CIV watchdog, open-retry)."""

    # ==================================================================== #
    #  retransmit REQUEST (UdpBase::sendRetransmitRequest)                  #
    # ==================================================================== #
    def _send_retransmit_request(self):
        with self._lock:
            if not self._rx_missing:
                return
            if len(self._rx_missing) > MAX_MISSING:
                # "Too many missing packets, flushing all buffers" - reference
                # clears rxMissing AND rxSeqBuf (its next RX re-seeds rxSeqBuf).
                self._rx_missing.clear()
                self._rx_seq_buf.clear()
                self.n_rx_clears += 1
                return
            missing = bytearray()
            for seq in list(self._rx_missing.keys()):
                if seq == 0:
                    continue                    # reference skips the 0 key
                cnt = self._rx_missing[seq]
                if cnt < 4:
                    # appendRetransmitSeqRange(first, last) with first==last==seq
                    # (single-seq range: two little-endian copies of the seq).
                    missing += struct.pack("<HH", seq, seq)
                    self._rx_missing[seq] = cnt + 1
                else:
                    # "No response for missing packet ... deleting"
                    del self._rx_missing[seq]
        if not missing:
            return
        # Build the control(type=0x01) request. Single-range (4 bytes) reference
        # packs the seq into the packet's own seq field; multi-range prepends a
        # 0x10-byte control header (with type 0x01) and grows len accordingly.
        if len(missing) == 4:
            first = struct.unpack("<H", missing[0:2])[0]
            pkt = struct.pack("<IHHII", CONTROL_SIZE, 0x01, first,
                              self.my_id, self.remote_id)
            self._write(pkt)
        else:
            ln = CONTROL_SIZE + len(missing)
            hdr = struct.pack("<IHHII", ln, 0x01, 0x0000, self.my_id, self.remote_id)
            self._write(hdr + bytes(missing))
        self.n_retx_req += 1

    # ==================================================================== #
    #  reader thread + dispatch                                             #
    # ==================================================================== #
    def _reader(self):
        while self._run:
            try:
                d = self.sock.recvfrom(4096)[0]
            except socket.timeout:
                continue
            except (OSError, AttributeError):
                break
            self.n_rx_dgrams += 1
            self.last_rx_at = time.monotonic()
            if len(d) < CONTROL_SIZE:
                continue
            # Subclasses (handler/civ) parse the datagram semantically first,
            # then defer to _data_received for the shared reliability pass -
            # exactly as UdpHandler::dataReceived / UdpCivData::dataReceived call
            # UdpBase::dataReceived(r) at the end of their switch.
            self._dispatch_subclass(d)
            self._data_received(d)

    def _dispatch_subclass(self, d):
        """Subclass semantic parse (overridden). Base does nothing; the handler
        and civ subclasses interpret login/token/status/scope here."""

    def _data_received(self, r):
        """UdpBase::dataReceived - the shared reliability pass. Handles:
          * control len==0x10 type==0x01: single-packet retransmit answer
          * control type==0x04: 'I am here' (remoteId capture + 0x06 reply)
          * ping type==0x07: reply=0 -> echo; reply=1 -> pingSendSeq bookkeeping
          * type==0x01 len!=0x10: multi-packet retransmit answer (flat seq list)
          * type==0x00 seq!=0: RX-seq tracking + missing-gap detection
        """
        length = len(r)
        received_at = self._elapsed_ms()

        # ---- length-specific handling (reference's outer switch) ----
        if length == CONTROL_SIZE:
            typ = struct.unpack("<H", r[4:6])[0]
            ln = struct.unpack("<I", r[0:4])[0]
            seq = struct.unpack("<H", r[6:8])[0]
            if typ == 0x01 and ln == 0x10:
                # Radio asks us to retransmit ONE packet (its seq is in `seq`).
                self.packets_lost += 1
                with self._lock:
                    hit = self._tx_seq_buf.get(seq)
                    if hit is not None:
                        hit["retx"] += 1
                        data = hit["data"]
                    else:
                        data = None
                if data is not None:
                    self._write(data)
                # (not found -> reference just logs; nothing sent)
            if typ == 0x04:
                # "I am here" during discovery.
                self.areyouthere_counter = 0
                self.remote_id = struct.unpack("<I", r[8:12])[0]
                self._areyouthere_on = False   # stop the are-you-there timer
                self.send_control(False, 0x06, 0x01)
            # typ == 0x06 handled purely in the shared seq path below.

        elif length == PING_SIZE:
            typ = struct.unpack("<H", r[4:6])[0]
            if typ == 0x07:
                reply = r[0x10]
                if reply == 0x00:
                    self._ping_reply_bookkeeping(r)
                    # Echo the ping back with reply=0x01, same seq + time.
                    p = bytearray(PING_SIZE)
                    p[0:PING_SIZE] = r[0:PING_SIZE]
                    struct.pack_into("<I", p, 0, PING_SIZE)
                    struct.pack_into("<H", p, 4, 0x07)
                    struct.pack_into("<I", p, 8, self.my_id)
                    struct.pack_into("<I", p, 12, self.remote_id)
                    p[0x10] = 0x01
                    # seq (6:8) and time (0x11:0x15) already copied from r.
                    self._write(bytes(p))
                elif reply == 0x01:
                    seq = struct.unpack("<H", r[6:8])[0]
                    if seq == self.ping_send_seq:
                        self.ping_send_seq = _seq16(self.ping_send_seq + 1)
            # ping path does not fall through to the seq tracker (reference
            # gates the shared block on len != PING_SIZE).
            return

        # ---- shared block: retransmit-list OR seq tracking ----
        typ = struct.unpack("<H", r[4:6])[0]
        ln = struct.unpack("<I", r[0:4])[0]
        seq = struct.unpack("<H", r[6:8])[0]

        if typ == 0x01 and ln != 0x10:
            # Multi-packet retransmit request: a FLAT LIST of 16-bit seqs starting
            # at 0x10 (reference: for i=0x10; i+1<len; i+=2). NOT (first,last).
            i = 0x10
            while i + 1 < length:
                s = r[i] | (r[i + 1] << 8)
                with self._lock:
                    hit = self._tx_seq_buf.get(s)
                    if hit is not None:
                        hit["retx"] += 1
                        data = hit["data"]
                    else:
                        data = None
                if data is None:
                    # Reference: sendControl(false, 0, seq) - "not available".
                    self.send_control(False, 0, s)
                else:
                    self._write(data)
                    self.packets_lost += 1
                i += 2
            return

        if ln != PING_SIZE and typ == 0x00 and seq != 0x00:
            self._track_rx_seq(seq, received_at)

    def _ping_reply_bookkeeping(self, r):
        """Maintain the per-stream radio-clock sync/lateness estimate exactly as
        UdpBase.cpp (normDay / signedDeltaDay / slow baseline). Diagnostic only -
        it never gates behaviour, but keeping it means the port is faithful."""
        radio_time = struct.unpack("<I", r[0x11:0x15])[0]
        local_now = self._elapsed_ms()
        radio_now = self._norm_day(radio_time)

        if not self._ping_have_sync:
            self._ping_have_sync = True
            self._ping_radio_base = radio_now
            self._ping_local_base = local_now

        predicted = self._norm_day(self._ping_radio_base +
                                   (local_now - self._ping_local_base))
        self._ping_lateness_ms = self._signed_delta_day(predicted, radio_now)

        if not self._ping_baseline_valid:
            self._ping_baseline_ms = self._ping_lateness_ms
            self._ping_baseline_valid = True
        else:
            dev = self._ping_lateness_ms - self._ping_baseline_ms
            if abs(dev) < 200:
                self._ping_baseline_ms = (self._ping_baseline_ms * 31 +
                                          self._ping_lateness_ms) // 32
                self._ping_baseline_ms = max(0, min(self._ping_baseline_ms, 2000))

    @staticmethod
    def _norm_day(ms):
        ms %= _DAY_MS
        if ms < 0:
            ms += _DAY_MS
        return ms

    def _signed_delta_day(self, a, b):
        d = self._norm_day(a) - self._norm_day(b)
        if d > _DAY_MS // 2:
            d -= _DAY_MS
        if d < -_DAY_MS // 2:
            d += _DAY_MS
        return d

    # ==================================================================== #
    #  RX-seq tracking (UdpBase::dataReceived, rxSeqBuf/rxMissing branch)   #
    # ==================================================================== #
    def _track_rx_seq(self, seq, received_at):
        """Faithful port of the reference's rxSeqBuf/rxMissing bookkeeping.

        rxSeqBuf is a MAP of seq -> receive-time (not a single 'last' int); every
        seq in a forward gap is inserted and any absent in-between seqs are added
        to rxMissing. This is the exact structure our from-spec Python replaced
        with one _rx_last int - the divergence that (per the deaf-scope notes)
        mis-judged gaps and drove spurious retransmit storms."""
        with self._lock:
            if not self._rx_seq_buf:
                self._rx_seq_buf[seq] = received_at
                return

            first_key = min(self._rx_seq_buf)
            last_key = max(self._rx_seq_buf)

            # Large-gap / rollback guard (reference uses signed qint16 math).
            gap = self._as_s16(seq - last_key)
            if seq < first_key or gap > self._as_s16(MAX_MISSING):
                # Reference "Large seq number gap detected": reset both buffers,
                # re-seed rxSeqBuf with this seq, drop the whole missing set.
                self._rx_seq_buf.clear()
                self._rx_seq_buf[seq] = received_at
                self._rx_missing.clear()
                self.n_rx_clears += 1
                return

            if seq not in self._rx_seq_buf:
                if seq > last_key + 1:
                    # 1+ missing packets: insert each seq from last_key+1..seq,
                    # marking every one except `seq` itself as missing.
                    f = last_key + 1
                    while f <= seq:
                        if len(self._rx_seq_buf) > BUFSIZE:
                            self._rx_seq_buf.pop(next(iter(self._rx_seq_buf)))
                        self._rx_seq_buf[f] = received_at
                        if f != seq and f not in self._rx_missing:
                            self._rx_missing[f] = 0
                        f += 1
                else:
                    if len(self._rx_seq_buf) > BUFSIZE:
                        self._rx_seq_buf.pop(next(iter(self._rx_seq_buf)))
                    self._rx_seq_buf[seq] = received_at
            else:
                # Duplicate/late arrival: if it was flagged missing, clear it.
                if seq in self._rx_missing:
                    del self._rx_missing[seq]

    @staticmethod
    def _as_s16(x):
        x &= 0xFFFF
        return x if x < 0x8000 else x - 0x10000

    # ==================================================================== #
    #  Aether-gate seam compatibility                                       #
    # ==================================================================== #
    # icom9700.py reads packets_sent/packets_lost via these names on the C++
    # side; the seam it actually needs is the counter attributes above plus the
    # on_data hook, both already present. Nothing further required here.
