#
# Aether-gate — busy-refusal test (no hardware, no AE; loopback sockets only).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""A real radio serves one GUI client. serve() runs the whole session inside
handle() (returns only on disconnect), so a SECOND connection must NOT be left
hanging in the listen() backlog — the gate accepts it and closes it at once,
turning AE's silent connect-hang into an instant clean disconnect.

This test drives the real serve() loop over loopback:
  1. client A connects -> becomes the incumbent, gets the V/H handshake, stays up
  2. client B connects while A holds the slot -> gets closed promptly (recv == b"")
  3. client A is still alive (its handshake bytes are intact, socket not closed)
  4. A disconnects, THEN client C connects -> now accepted (slot freed)

Run:  python -m aether_gate.tests.test_busy_refuse
Exits non-zero on first failure.
"""
import socket
import sys
import threading
import time


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _make_radio(port):
    from aether_gate.core import Radio
    from aether_gate.adapters import SimAdapter
    a = SimAdapter(pattern="carrier", model="FLEX-6700")
    # bind loopback so serve() listens on 127.0.0.1:<port>
    return Radio("127.0.0.1", None, adapter=a, port=port)


def _recv_ready(sock, want=1, timeout=2.0):
    """Read up to `want` bytes (or until close) within timeout. Returns bytes read."""
    sock.settimeout(timeout)
    got = b""
    try:
        while len(got) < want:
            chunk = sock.recv(want - len(got))
            if not chunk:               # peer closed
                break
            got += chunk
    except socket.timeout:
        pass
    return got


def _recv_closed(sock, timeout=2.0):
    """True if the peer closes the socket (recv returns b"") within timeout."""
    sock.settimeout(timeout)
    try:
        # A refused client is accepted then immediately closed -> recv returns b"".
        return sock.recv(64) == b""
    except socket.timeout:
        return False
    except OSError:
        return True


def test_second_client_refused_first_survives():
    port = _free_port()
    r = _make_radio(port)
    t = threading.Thread(target=r.serve, daemon=True)
    t.start()
    # let serve() bind+listen
    time.sleep(0.3)

    a = b_ = c = None
    try:
        # --- client A: the incumbent ---
        a = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        # Drain A's full V/H handshake so nothing is left mid-buffer, and prove
        # A is the served client (a refused client never gets a V line).
        hs = _recv_ready(a, want=len(b"V3.3.28.0\nH00000000\n"), timeout=2.0)
        assert hs[:1] == b"V", f"incumbent expected V-handshake, got {hs!r}"
        assert b"\nH" in hs or hs.count(b"\n") >= 1, f"incumbent handshake incomplete: {hs!r}"

        # --- client B: connects while A holds the slot -> must be closed FAST ---
        # A refused client is accepted-then-closed, so recv returns b"" almost
        # immediately (well under a second). A HANG (the bug) would time out.
        b_ = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        assert _recv_closed(b_, timeout=1.5), "second client was NOT refused (hung/served)"

        # --- A must STILL be the incumbent: check BEFORE any teardown races ---
        # keepalive line must not raise, and A must not have been closed.
        a.sendall(b"C1|ping\n")
        assert not _recv_closed(a, timeout=0.4), "incumbent was dropped when 2nd client connected"
        print("ok  busy: 2nd client refused fast, incumbent survived")

        # --- free the slot, then C should be accepted ---
        a.close(); a = None
        time.sleep(0.5)                 # let serve()'s finally: clear self.conn
        c = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        hs = _recv_ready(c, want=1, timeout=2.0)
        assert hs[:1] == b"V", f"post-release client expected V-handshake, got {hs!r}"
        print("ok  busy: slot freed on disconnect, next client accepted")
    finally:
        for s in (a, b_, c):
            if s is not None:
                try: s.close()
                except OSError: pass
        # Stop serve() and unblock its accept() with a throwaway connection, then
        # let the thread wind down before the interpreter finalizes — otherwise a
        # daemon thread mid-log() can deadlock on the stdout lock at shutdown.
        r.run = False
        try:
            k = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            k.close()
        except OSError:
            pass
        t.join(timeout=2.0)
        time.sleep(0.1)


def main():
    tests = [test_second_client_refused_first_survives]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} busy-refusal test(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
