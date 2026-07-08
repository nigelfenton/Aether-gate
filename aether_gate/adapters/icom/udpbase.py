#
# Aether-gate — IC-9700 LAN UDP base: reliability layer + timers.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
# Ported from github.com/w5jwp/SDR9700 (GPL-3.0) by Justin W5JWP: UdpBase.cpp/.h. Attribution preserved.
#
"""Shared UDP transport for the IC-9700 LAN protocol (control / CI-V / audio streams).

Provides the bits the radio insists on or it drops you: session ids, a background
RX reader thread, the are-you-there/0x06 discovery, a tracked send-sequence with a
TX history buffer, ping/idle/retransmit timer threads, and the missing-seq tracker.

The KEY thing a serial probe gets wrong (and why login goes unanswered): the radio
needs the ping(500ms)/idle(100ms)/retransmit(100ms) cadence running CONTINUOUSLY,
on their own clocks, throughout the auth dialog. This class runs them as threads.
"""
import socket
import struct
import threading
import time

PING_PERIOD = 0.500
IDLE_PERIOD = 0.100
RETRANSMIT_PERIOD = 0.100
AREYOUTHERE_PERIOD = 0.500
CONTROL_SIZE = 0x10
PING_SIZE = 0x15
MAX_MISSING = 50
BUFSIZE = 500


def _now_ms():
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


class UdpBase:
    def __init__(self, local_ip, radio_ip, radio_port, bind_port=0, name="base"):
        self.local_ip = local_ip
        self.radio_ip = radio_ip
        self.radio_port = radio_port
        self.name = name
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((local_ip, bind_port))
        self.sock.settimeout(0.05)
        self.local_port = self.sock.getsockname()[1]
        o = local_ip.split(".")
        self.my_id = ((int(o[2]) << 24) | (int(o[3]) << 16) | (self.local_port & 0xFFFF))
        self.remote_id = 0

        self._send_seq = 1               # tracked-packet counter (starts at 1)
        self._ping_seq = 0
        self._lock = threading.Lock()
        self._tx_hist = {}               # seq -> (bytes, time)
        self._rx_last = None             # last radio->us seq seen (single int, wraps at 0x10000)
        self._rx_missing = {}            # seq -> retry count
        self._run = False
        self._connected = False          # got I-am-here
        self._t_reader = None
        self._t_timers = None
        self._last_ping = 0.0
        self._last_ayt = 0.0
        self._ayt_count = 0              # discovery retries (0x03) before i-am-here
        self.n_lost = 0                  # radio asked us to retransmit (loss signal)
        self._last_idle = 0.0
        self._last_retx = 0.0
        # Instrumentation for the "deaf scope" investigation: counters let a
        # diagnostic (or a test) measure the sustained tracked-packet rate and
        # whether stalls correlate with retransmit-request bursts, instead of
        # guessing. Cheap ints; read via diagnostics()/counters().
        self.n_sent = 0                  # tracked packets we've sent
        self.n_retx_req = 0              # retransmit REQUESTS we've sent to the radio
        self.n_rx_clears = 0             # times the RX seq tracker reset (gap/rollover)
        self.n_rx_dgrams = 0             # ALL datagrams received from the radio
        self.last_rx_at = 0.0            # monotonic time of the last radio datagram
        # subclasses set this to a callable(packet_bytes) for non-control payloads
        self.on_data = None

    # --- low-level send ---------------------------------------------------
    def _send(self, data):
        try:
            self.sock.sendto(data, (self.radio_ip, self.radio_port))
        except (OSError, AttributeError):      # AttributeError: sock closed to None by stop()
            pass

    def _control(self, typ, seq=0, ln=CONTROL_SIZE):
        return struct.pack("<IHHII", ln, typ, seq, self.my_id, self.remote_id)

    def send_control(self, typ, seq=0):
        self._send(self._control(typ, seq))

    def send_tracked(self, buf):
        """Send a tracked packet: stamp the next send_seq into [6:8], keep a copy."""
        with self._lock:
            seq = self._send_seq
            b = bytearray(buf)
            struct.pack_into("<H", b, 6, seq & 0xFFFF)
            if seq == 0:                 # rollover clears the window
                self._tx_hist.clear()
            if len(self._tx_hist) > BUFSIZE:
                self._tx_hist.pop(next(iter(self._tx_hist)))
            self._tx_hist[seq] = (bytes(b), time.monotonic())
            self._send_seq = (self._send_seq + 1) & 0xFFFF
            self.n_sent += 1
        self._send(bytes(b))
        return seq

    def send_idle(self):
        # idle keepalive — TRACKED (SDR9700 wires idleTimer to sendControl(true,0,0))
        self.send_tracked(self._control(0x00, 0))

    def send_ping(self):
        p = bytearray(PING_SIZE)
        struct.pack_into("<IHHII", p, 0, PING_SIZE, 0x07, self._ping_seq, self.my_id, self.remote_id)
        p[0x10] = 0x00
        struct.pack_into("<I", p, 0x11, _now_ms())
        self._ping_seq = (self._ping_seq + 1) & 0xFFFF
        self._send(bytes(p))

    # --- discovery --------------------------------------------------------
    def start(self):
        self._run = True
        self._t_reader = threading.Thread(target=self._reader, daemon=True)
        self._t_timers = threading.Thread(target=self._timer_loop, daemon=True)
        self._t_reader.start()
        self._t_timers.start()
        self.send_control(0x03)          # are-you-there

    def stop(self):
        self._run = False
        try:
            self.send_control(0x05, 0x00)  # disconnect/idle close
        except Exception:
            pass
        # Close the socket fd. Without this each reconnect leaked the civ/audio
        # sockets (they were left bound + unread — visible as orphaned sockets
        # with a stuck Recv-Q). The reader thread already exited on _run=False.
        try:
            if self.sock is not None:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    # --- timer thread (the cadence the radio needs) -----------------------
    def _timer_loop(self):
        while self._run:
            now = time.monotonic()
            if not self._connected:
                # ARE-YOU-THERE RETRY (transport-audit find): SDR9700 retries the
                # 0x03 discovery every 500 ms until i-am-here arrives, then stops
                # the timer (it is a discovery RETRY, not a keepalive). We used to
                # send it exactly once in start() — a lost datagram meant the
                # connection hung forever. Retry like the reference; after 20
                # tries log loudly and keep probing at a slower 2 s (a headless
                # service should keep trying, not give up like the GUI app).
                period = AREYOUTHERE_PERIOD if self._ayt_count < 20 else 2.0
                if now - self._last_ayt >= period:
                    self.send_control(0x03)
                    self._last_ayt = now
                    self._ayt_count += 1
                    if self._ayt_count == 20:
                        print(f"[{self.name}] radio not answering are-you-there "
                              f"after 20 tries - still probing every 2s", flush=True)
            else:
                if now - self._last_ping >= PING_PERIOD:
                    self.send_ping(); self._last_ping = now
                if now - self._last_idle >= IDLE_PERIOD:
                    self.send_idle(); self._last_idle = now
                if now - self._last_retx >= RETRANSMIT_PERIOD:
                    self._send_retransmit_requests(); self._last_retx = now
            self._on_tick(now)               # subclass hook (open-retry, watchdogs)
            time.sleep(0.02)

    def _on_tick(self, now):
        """Per-20ms hook for subclasses (Ic9700Civ open-retry-until-data, audio
        silence watchdog). Default: nothing."""

    def _send_retransmit_requests(self):
        with self._lock:
            if not self._rx_missing:
                return
            # Session is clearly behind (a burst of losses): asking for all of it
            # back piles traffic onto an already-struggling scope stream — the
            # amplifier behind the "deaf scope" loop. Drop the backlog and let it
            # resync instead. (Lost waterfall rows are realtime — a re-sent row is
            # stale by the time it arrives, so there's nothing to gain.)
            if len(self._rx_missing) > MAX_MISSING:
                self._rx_missing.clear(); self._rx_last = None
                self.n_rx_clears += 1
                return
            ranges = b""
            drop = []
            for seq, cnt in list(self._rx_missing.items()):
                if seq == 0:
                    continue
                if cnt < 4:
                    ranges += struct.pack("<HH", seq, seq)
                    self._rx_missing[seq] = cnt + 1
                else:
                    drop.append(seq)
            for s in drop:
                self._rx_missing.pop(s, None)
        if ranges:
            ln = CONTROL_SIZE + len(ranges)
            pkt = struct.pack("<IHHII", ln, 0x01, 0, self.my_id, self.remote_id) + ranges
            self._send(pkt)
            self.n_retx_req += 1

    # --- reader thread ----------------------------------------------------
    def _reader(self):
        while self._run:
            try:
                d = self.sock.recvfrom(4096)[0]
            except socket.timeout:
                continue
            except (OSError, AttributeError):  # closed / set to None by stop()
                break
            # Ground-truth instrumentation for the deaf-scope stall: count EVERY
            # datagram the radio sends us + when the last one arrived. If these
            # keep climbing through a scope stall, the radio is still talking and
            # the gate is dropping/ignoring scope frames; if they freeze, the
            # radio itself went silent. (No packet-capture tool on the Pi.)
            self.n_rx_dgrams += 1
            self.last_rx_at = time.monotonic()
            if len(d) < CONTROL_SIZE:
                continue
            typ = struct.unpack("<H", d[4:6])[0]
            self._handle(d, typ)

    def _handle(self, d, typ):
        # I-am-here -> capture remote id, send our 0x06, go connected
        if typ == 0x04:
            self.remote_id = struct.unpack("<I", d[8:12])[0]
            self.send_control(0x06, 0x01)
            self._connected = True
            self._on_iamhere()
            return
        if typ == 0x06:
            self._on_iamready()
            return
        if typ == 0x07:                          # ping request -> echo as reply
            if len(d) >= PING_SIZE and d[0x10] == 0x00:
                r = bytearray(d); r[0x10] = 0x01
                struct.pack_into("<I", r, 8, self.my_id)
                struct.pack_into("<I", r, 12, self.remote_id)
                self._send(bytes(r))
            return
        if typ == 0x01:                          # radio asks us to retransmit
            self._answer_retransmit(d)
            return
        if typ == 0x00:                          # idle/data marker — track seq if non-zero
            seq = struct.unpack("<H", d[6:8])[0]
            if seq != 0:
                self._track_rx(seq)
            # a len>0x10 type-0 packet carries tracked payload -> hand up
            if len(d) > CONTROL_SIZE and self.on_data:
                self.on_data(d)
            return
        # any other tracked reply (login/token/status/caps) -> hand up
        if self.on_data:
            self.on_data(d)

    def _answer_retransmit(self, d):
        # Every retransmit the radio asks for = a packet of OURS it lost; count it
        # (SDR9700 tracks packetsLost -> congestion %). We don't keep much history
        # pre-auth; reply "not available" (type 0x00, that seq) for unknown seqs.
        if len(d) == CONTROL_SIZE:
            rseq = struct.unpack("<H", d[6:8])[0]
            self.n_lost += 1
            with self._lock:
                hit = self._tx_hist.get(rseq)
            if hit:
                self._send(hit[0])
            else:
                self.send_control(0x00, rseq)
        else:
            pl = d[CONTROL_SIZE:]
            for i in range(0, len(pl) - 3, 4):
                first, last = struct.unpack("<HH", pl[i:i+4])
                for sq in range(first, (last + 1) & 0x1FFFF):
                    sq &= 0xFFFF
                    self.n_lost += 1
                    with self._lock:
                        hit = self._tx_hist.get(sq)
                    if hit:
                        self._send(hit[0])
                    elif sq != 0xFFFF:
                        self.send_control(0x00, sq)

    @staticmethod
    def _seq_delta(a, b):
        """Signed distance a-b on the 16-bit wrapping seq space, in (-32768,32767].
        Positive = a is ahead of b. Used everywhere so rollover (0xFFFF->0) is
        handled consistently — the old code mixed this with raw int min()/max(),
        which mis-judged gaps across the wrap and triggered spurious resets."""
        d = (a - b) & 0xFFFF
        return d if d < 0x8000 else d - 0x10000

    def _track_rx(self, seq):
        with self._lock:
            if self._rx_last is None:
                self._rx_last = seq
                return
            delta = self._seq_delta(seq, self._rx_last)
            if delta == 0:
                return                          # duplicate of the last seq — ignore
            # A jump bigger than the miss window (either direction) = the stream
            # resynced / rolled far / we fell behind: reset cleanly rather than
            # enqueue a storm of phantom "missing" seqs (which would feed the
            # retransmit-request loop that wedges the scope). Count the reset so
            # a diagnostic can see how often the tracker is thrashing.
            if abs(delta) > MAX_MISSING:
                self._rx_missing.clear()
                self._rx_last = seq
                self.n_rx_clears += 1
                return
            if delta < 0:
                # a late / reordered packet still inside the window: it fills one
                # of the gaps we were tracking. Clear just that seq; do NOT rewind
                # _rx_last (we're still ahead) and do NOT reset the whole set.
                self._rx_missing.pop(seq, None)
                return
            # forward gap of >1 => the in-between seqs are genuinely missing.
            f = (self._rx_last + 1) & 0xFFFF
            while f != seq:
                self._rx_missing.setdefault(f, 0)
                f = (f + 1) & 0xFFFF
            self._rx_missing.pop(seq, None)     # this seq arrived (maybe late)
            self._rx_last = seq

    # --- hooks for subclasses --------------------------------------------
    def _on_iamhere(self):
        pass

    def _on_iamready(self):
        pass
