#
# Aether-gate — hamlib rigctld client tests (mock daemon, no real rig).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""Verify the Rigctld client against a MOCK rigctld TCP server.

rigctld's short-command replies are the fiddly bit (value line vs "RPRT n",
multi-line mode replies), so this exercises the parsing without a real radio.

Run:  python -m aether_gate.tests.test_hamlib
"""
import socket
import threading
import sys


class MockRigctld:
    """A tiny rigctld stand-in: answers the short single-char commands the
    Rigctld client sends, mimicking hamlib's on-the-wire reply shapes."""

    def __init__(self):
        self.freq = 14074000
        self.mode = "USB"
        self.ptt = 0
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._run = True
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while self._run:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        buf = b""
        conn.settimeout(2.0)
        while self._run:
            try:
                d = conn.recv(256)
            except OSError:
                break
            if not d:
                break
            buf += d
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                conn.sendall(self._reply(line.decode().strip()).encode())

    def _reply(self, line):
        parts = line.split()
        c = parts[0] if parts else ""
        if c == "f":                       # get_freq
            return f"{self.freq}\n"
        if c == "F":                       # set_freq
            self.freq = int(parts[1]); return "RPRT 0\n"
        if c == "m":                       # get_mode -> "MODE\nPASSBAND"
            return f"{self.mode}\n2400\n"
        if c == "M":                       # set_mode
            self.mode = parts[1]; return "RPRT 0\n"
        if c == "t":                       # get_ptt
            return f"{self.ptt}\n"
        if c == "T":                       # set_ptt
            self.ptt = int(parts[1]); return "RPRT 0\n"
        if c == "l" and len(parts) > 1 and parts[1] == "STRENGTH":
            return "-12\n"                 # S-meter dB rel S9
        if c == "_":                       # model name
            return "Kenwood TS-2000\n"
        return "RPRT -1\n"

    def stop(self):
        self._run = False
        try: self._srv.close()
        except OSError: pass


def test_hamlib_roundtrip():
    from aether_gate.adapters.hamlib.rigctld import Rigctld
    mock = MockRigctld()
    try:
        rc = Rigctld("127.0.0.1", mock.port, timeout=2.0)
        assert rc.connect(), "connect failed"

        assert rc.get_freq_hz() == 14074000, rc.get_freq_hz()
        assert rc.set_freq_hz(7040000) is True
        assert rc.get_freq_hz() == 7040000

        assert rc.get_mode() == "USB", rc.get_mode()
        assert rc.set_mode("LSB") is True
        assert rc.get_mode() == "LSB"
        # AE mode maps through hamlib names both ways
        assert rc.set_mode("CW") is True
        assert rc.get_mode() == "CW"

        assert rc.get_ptt() is False
        assert rc.set_ptt(True) is True
        assert rc.get_ptt() is True
        rc.set_ptt(False)

        assert rc.get_smeter_db() == -12, rc.get_smeter_db()
        assert "TS-2000" in (rc.model_name() or "")

        rc.close()
        print("ok  hamlib: rigctld round-trip (freq/mode/ptt/smeter/model)")
    finally:
        mock.stop()


def test_kenwood_adapter_constructs():
    from aether_gate.adapters import KenwoodAdapter, available
    assert "kenwood" in available()
    a = KenwoodAdapter(model="TS-2000")
    assert a.provides == "iq"
    assert a.capabilities.model == "FLEX-6700"      # TS-2000 has 2m
    # TX is opt-in (default RX-only): no PTT is wired, so the gate must not
    # advertise tx_capable unless explicitly enabled. See PR #5 / issue #3.
    assert a.capabilities.tx_capable is False
    assert KenwoodAdapter(model="TS-2000", enable_tx=True).capabilities.tx_capable is True
    assert "2m" in a.capabilities.bands and "20m" in a.capabilities.bands
    d = a.diagnostics()
    assert d["presented_as"] == "FLEX-6700"
    assert d["link"]["hamlib_model"] == 2014
    print("ok  kenwood: adapter constructs + advertises TS-2000 caps")


def main():
    for t in (test_hamlib_roundtrip, test_kenwood_adapter_constructs):
        try:
            t()
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            sys.exit(1)
    print("\nall hamlib/kenwood tests passed")


if __name__ == "__main__":
    main()
