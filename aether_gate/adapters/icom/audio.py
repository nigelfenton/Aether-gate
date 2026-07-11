#
# Aether-gate — IC-9700 LAN audio stream: the RS-BA1 RX audio session.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
# Ported from github.com/w5jwp/SDR9700 (GPL-3.0): UdpAudio.cpp + PacketTypes.h.
# Attribution preserved.
#
"""RX audio for the IC-9700 over its LAN interface.

The 9700's RX audio is a THIRD independent RS-BA1 session (alongside control and
CI-V/scope) — NOT just packets sent to a port we advertised. Like the CI-V stream
it runs the are-you-there / i-am-here / i-am-ready handshake on the audio port
(UdpBase.start does exactly that); once synced the radio streams 48 kHz LPCM16
mono audio in 0x18-header packets. We decode the PCM payload into a ring of float
samples for the adapter's get_audio(), which the engine forwards to AE.

Audio packet (0x18 = 24-byte header, then LPCM16 payload):
  0x00 len(u32 LE)  0x04 type(u16)  0x06 seq(u16)  0x08 sentid  0x0c rcvdid
  0x10 ident(u16)   0x12 sendseq(u16)  0x14 unused  0x16 datalen(u16 BE)
  0x18.. int16 LE PCM samples
(mirrors SDR9700 audio_packet; RX payload len = datagram_len - 0x18.)
"""
import struct
import threading

from .udpbase import UdpBase

AUDIO_HDR = 0x18                 # audio packet header size
RADIO_RATE = 48000              # the rxsample we request in conninfo (handler.py)
# Cap the decoded ring to a SMALL realtime window. Audio is live QSO audio, so
# low latency beats completeness: if the reader (AE) falls behind, drop the
# OLDEST samples to stay current rather than build a growing delay. ~0.3 s of
# 48 kHz mono is enough to smooth jitter without an audible lag.
_RING_MAX = int(RADIO_RATE * 0.3)


class Ic9700Audio(UdpBase):
    """RS-BA1 RX-audio session. Adopts the handler's reserved audio socket
    (bound at the audio_local_port advertised in conninfo)."""

    def __init__(self, local_ip, radio_ip, audio_port, sock):
        # Same pre-bound-socket adoption the CI-V stream uses (see Ic9700Civ):
        # the radio sends audio to the audio_local_port we advertised, so we must
        # keep listening on that exact socket, not bind a fresh one.
        # Adopt the pre-bound reservation socket + init the ported UdpBase state
        # WITHOUT re-binding (mirrors UdpCivData.__init__; the radio streams audio
        # to the exact port we advertised, so keep this socket).
        self.local_ip = local_ip
        self.radio_ip = radio_ip
        self.radio_port = audio_port
        self.name = "audio"
        self.sock = sock
        self.sock.settimeout(0.05)
        self.local_port = sock.getsockname()[1]
        # UdpBase state (ported attribute names).
        self._init_ids(local_ip)
        self.send_seq = 1
        self.send_seq_b = 0
        self.auth_seq = 0x30
        self.ping_send_seq = 0
        self.is_authenticated = False
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
        self._connected = False
        self.on_data = self._on_audio
        # audio-specific
        self._ring = bytearray()       # decoded int16 PCM bytes waiting for get_audio
        self._ring_lock = threading.Lock()
        self.audio_frames = 0          # count of audio datagrams parsed (diagnostics)
        self.audio_bytes = 0           # total PCM bytes received
        self.dropped = 0               # samples dropped when the ring overflowed

    def _on_iamready(self):
        # The audio session needs NO scope/openclose bring-up — being synced is
        # enough; the radio starts streaming audio to this port once ready.
        # (UdpBase already sent 0x06 and keeps the are-you-there/ping/idle
        # cadence via the timer thread.) Nothing to send here.
        pass

    def _on_tick(self, now):
        # AUDIO SILENCE WATCHDOG (transport-audit find): SDR9700 alerts after
        # 30 s without audio data (its UdpAudio watchdog; deliberately
        # conservative — no auto-recovery, audio restarts with the session).
        # Log once per silent episode so a dead audio stream is visible in the
        # journal instead of just sounding like a quiet band.
        if self._connected and self.audio_frames > 0:
            last = self.last_rx_at or now
            if now - last > 30.0:
                if not getattr(self, "_silence_alerted", False):
                    self._silence_alerted = True
                    print("[audio] no audio data for 30s - stream silent "
                          "(recovers with the session)", flush=True)
            else:
                self._silence_alerted = False

    def _on_audio(self, d):
        # Called by UdpBase._handle for type-0 tracked data. An audio datagram is
        # >= the 0x18 header; the PCM payload follows. Ignore short control frames.
        if len(d) < AUDIO_HDR:
            return
        declared = struct.unpack_from(">H", d, 0x16)[0]     # datalen (BE), like SDR9700
        payload = d[AUDIO_HDR:]
        if not payload:
            return
        # Trust the header length when it agrees; otherwise take the datagram tail
        # (fail-safe: never index past the buffer). Payload is int16 LE PCM.
        if declared and declared == len(payload):
            pcm = payload
        else:
            pcm = payload[:declared] if 0 < declared <= len(payload) else payload
        # keep an even byte count (whole int16 samples)
        if len(pcm) & 1:
            pcm = pcm[:-1]
        if not pcm:
            return
        with self._ring_lock:
            self._ring.extend(pcm)
            self.audio_frames += 1
            self.audio_bytes += len(pcm)
            if len(self._ring) > _RING_MAX * 2:            # ring is in BYTES (2/sample)
                over = len(self._ring) - _RING_MAX * 2
                del self._ring[:over]                       # drop oldest (stay realtime)
                self.dropped += over // 2

    def read_samples(self, n):
        """Pop up to n int16 samples as a list of floats in [-1, 1]. Returns fewer
        (or []) if the ring is short — the caller pads/upsamples as needed."""
        need = int(n) * 2
        with self._ring_lock:
            if not self._ring:
                return []
            take = self._ring[:need]
            del self._ring[:len(take)]
        vals = struct.unpack("<%dh" % (len(take) // 2), bytes(take))
        return [s / 32768.0 for s in vals]

    @property
    def ring_samples(self):
        with self._ring_lock:
            return len(self._ring) // 2
