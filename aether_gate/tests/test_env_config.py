#
# Aether-gate — AETHER_GATE_* environment-default tests (no hardware, no network).
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
"""apply_env_defaults() lets every flag also arrive as AETHER_GATE_<DEST>, so a
container or a systemd unit can configure the gate without a hand-edited command
line (and without a password on one).

The properties that matter, pinned here:
  * CLI beats env beats built-in default -- nothing existing changes behaviour;
  * argparse's type= still runs, so --port arrives as an int, not "5992";
  * store_true flags read as booleans, and "0"/"false"/"no"/"off" mean OFF --
    otherwise AETHER_GATE_RX_ONLY=0 would switch transmit-disable ON;
  * the variable follows argparse's DEST, not the flag spelling. --pass is
    dest="pw", so it is AETHER_GATE_PW. That mismatch is the single easiest
    thing to get wrong when writing a compose file, so it is pinned.

A synthetic parser is used so the test is hermetic and does not depend on the
gate's full flag list; it mirrors the real flags it names. os.environ is never
mutated -- the helper takes the mapping as an argument.

Run:  python -m aether_gate.tests.test_env_config
"""
import argparse
import sys

from aether_gate.__main__ import apply_env_defaults, wants_setup_ui


def _parser():
    ap = argparse.ArgumentParser(prog="aether-gate-test", add_help=False)
    ap.add_argument("--radio-ip", default=None)
    ap.add_argument("--port", type=int, default=4992)
    ap.add_argument("--width-khz", type=float, default=3.0)
    ap.add_argument("--pass", dest="pw", default=None)     # dest != flag
    ap.add_argument("--rx-only", action="store_true")
    ap.add_argument("--station", default="aether-gate 1")
    return ap


def _parse(env, argv=()):
    return apply_env_defaults(_parser(), env).parse_args(list(argv))


def test_env_supplies_a_value():
    a = _parse({"AETHER_GATE_RADIO_IP": "172.17.0.97"})
    assert a.radio_ip == "172.17.0.97"
    print("ok  env: AETHER_GATE_RADIO_IP supplies --radio-ip")


def test_unset_env_leaves_defaults_alone():
    a = _parse({})
    assert a.radio_ip is None and a.port == 4992 and a.station == "aether-gate 1"
    assert a.rx_only is False
    print("ok  env: no env -> built-in defaults untouched")


def test_cli_beats_env():
    a = _parse({"AETHER_GATE_PORT": "4992"}, ["--port", "5992"])
    assert a.port == 5992, "an explicit flag must always win over the environment"
    print("ok  env: CLI flag beats env var")


def test_type_coercion_still_runs():
    """argparse applies type= to a string default -- so --port must come back an
    int. If this ever regresses, port comparisons and formatting break subtly."""
    a = _parse({"AETHER_GATE_PORT": "5992", "AETHER_GATE_WIDTH_KHZ": "2.5"})
    assert a.port == 5992 and isinstance(a.port, int), f"port={a.port!r}"
    assert a.width_khz == 2.5 and isinstance(a.width_khz, float), f"w={a.width_khz!r}"
    print("ok  env: type= still applied (int/float, not str)")


def test_store_true_truthy_and_falsey():
    for on in ("1", "true", "TRUE", "yes", "on"):
        assert _parse({"AETHER_GATE_RX_ONLY": on}).rx_only is True, on
    for off in ("0", "false", "FALSE", "no", "off", "", "   "):
        assert _parse({"AETHER_GATE_RX_ONLY": off}).rx_only is False, repr(off)
    print("ok  env: store_true reads booleans; 0/false/no/off/empty are OFF")


def test_variable_follows_dest_not_flag_spelling():
    """--pass is dest="pw", so the variable is AETHER_GATE_PW. AETHER_GATE_PASS
    is NOT a thing -- pinned because it is the easiest compose-file mistake."""
    a = _parse({"AETHER_GATE_PW": "s3cret"})
    assert a.pw == "s3cret"
    b = _parse({"AETHER_GATE_PASS": "s3cret"})
    assert b.pw is None, "AETHER_GATE_PASS must NOT work -- the dest is pw"
    print("ok  env: follows argparse dest (--pass -> AETHER_GATE_PW)")


def test_bare_launch_opens_setup():
    assert wants_setup_ui([], {}) is True
    print("ok  setup: bare launch with no env opens the Setup page")


def test_env_configured_bare_launch_starts_the_gate():
    """The whole point of env config: a container passes no argv. Without this,
    `python -m aether_gate` with AETHER_GATE_* set would open the Setup page and
    never bring up the radio it was configured for."""
    assert wants_setup_ui([], {"AETHER_GATE_ADAPTER": "icom9700"}) is False
    print("ok  setup: AETHER_GATE_ADAPTER suppresses the page (containers)")


def test_setup_flag_always_wins():
    assert wants_setup_ui(["--setup"], {"AETHER_GATE_ADAPTER": "icom9700"}) is True
    print("ok  setup: --setup forces the page even when env selects an adapter")


def test_args_still_bypass_setup():
    assert wants_setup_ui(["--adapter", "sim"], {}) is False
    print("ok  setup: ordinary CLI args still start the gate")


def main():
    tests = [test_env_supplies_a_value, test_unset_env_leaves_defaults_alone,
             test_cli_beats_env, test_type_coercion_still_runs,
             test_store_true_truthy_and_falsey,
             test_variable_follows_dest_not_flag_spelling,
             test_bare_launch_opens_setup,
             test_env_configured_bare_launch_starts_the_gate,
             test_setup_flag_always_wins, test_args_still_bypass_setup]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            return 1
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            return 2
    print(f"\nall {len(tests)} env-default tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
