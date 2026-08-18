"""Command line entry point.

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import argparse
import sys

from . import bridge, hlp, profile as profile_module
from .spiceapi import SpiceApiError, SpiceConnection


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog='hlp-spice2x',
        description="Bridge spice2x cabinet lighting onto a GP2040-CE board.")
    parser.add_argument("--host", default="127.0.0.1", help="spice2x API host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=1337,
                        help="the port passed to spice2x's -api (1337 by convention, not a default)")
    parser.add_argument("--password", default="", help="spice2x API password, if -apipass was used")
    parser.add_argument("--profile", help="JSON profile mapping light names to controls")
    parser.add_argument("--list-lights", action="store_true",
                        help="print the lights spice2x reports and exit")
    parser.add_argument("--write-profile", metavar="FILE",
                        help="write a skeleton profile from the running game and exit")
    parser.add_argument("--board-id", default="",
                        help="hex prefix of the board ID, to pick one of several")
    parser.add_argument("--fps", type=float,
                        help="poll rate (default: the board's own render rate, capped at 60)")
    parser.add_argument("--timeout", type=int, default=2000,
                        help="takeover keepalive timeout in ms (default 2000)")
    parser.add_argument("--overlay", action="store_true",
                        help="overlay onto the board's animations instead of taking the whole frame")
    parser.add_argument("--full-brightness", action="store_true",
                        help="render at the colours the game asks for, ignoring the board's "
                             "brightness setting (useful when the board is configured dim)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print frames instead of driving a board")
    parser.add_argument("--force", action="store_true",
                        help="run even against a protocol major version this bridge does not "
                             "support, which is unsupported and for testing only")
    return parser


def main(argv=None) -> int:
    """Run the tool.

    :param argv: argument list, or None to read sys.argv
    :return: process exit status
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.fps is not None and args.fps <= 0:
        parser.error("--fps must be greater than zero")
    # the timeout travels as a 16-bit field, and the firmware floors it at 100 ms
    if not 100 <= args.timeout <= 0xFFFF:
        parser.error("--timeout must be between 100 and 65535 ms")

    try:
        if args.list_lights or args.write_profile:
            return inspect(args)
        if not args.profile:
            parser.error("--profile is required (build one with --write-profile)")
        return run_bridge(args)
    except (hlp.HostLightingError, profile_module.ProfileError, SpiceApiError) as error:
        print(error, file=sys.stderr)
        return 1
    except OSError as error:
        # not attributed to either side: an OSError here could be the socket or
        # hidapi, and guessing wrong sends the user looking in the wrong place
        print(f"error: {error}", file=sys.stderr)
        return 1


def inspect(args) -> int:
    """Handle --list-lights and --write-profile, which need no board."""
    connection = SpiceConnection(args.host, args.port, args.password)
    try:
        states = connection.lights_read()
        if not states:
            print("spice2x reported no lights - is a game running?", file=sys.stderr)
            return 1
        if args.write_profile:
            profile_module.write_skeleton(args.write_profile, states)
            print(f"wrote {args.write_profile} with {len(states)} lights")
            print("fill in each 'button' with a control name (Up Down Left Right B1-B4 L1 R1")
            print("L2 R2 S1 S2 L3 R3 A1 A2 PLED1-4 TURBO CASE, plus A3 A4 and E1-E12 where")
            print("the board reports them); leave it empty to ignore")
        else:
            print(f"{len(states)} lights:")
            for name, state in sorted(states.items()):
                print(f"  {state:5.2f}  {name}")
    finally:
        connection.close()
    return 0


def run_bridge(args) -> int:
    """Open the connections and run the bridge loop."""
    profile, warnings = profile_module.load(args.profile)
    for warning in warnings:
        print(f"warning: {warning}")

    connection = SpiceConnection(args.host, args.port, args.password)
    try:
        device = None if args.dry_run else hlp.open_device(args.board_id)
    except BaseException:
        connection.close()
        raise

    try:
        bridge.run(connection, profile, device=device, fps=args.fps, timeout_ms=args.timeout,
                   overlay=args.overlay, full_brightness=args.full_brightness,
                   force=args.force)
    finally:
        if device is not None:
            try:
                device.request_ok(hlp.CMD_RELEASE)
                print("released - on-board animations restored")
            except hlp.HostLightingError:
                pass
            device.close()
        connection.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
