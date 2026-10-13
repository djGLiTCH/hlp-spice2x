"""The poll, resolve and publish loop.

Reads the game's lights from spice2x, turns them into a frame through the
profile, and publishes that frame to the board. Frames are only sent when
something changed. While nothing does, the claim on the board's lights is
renewed several times per keepalive timeout, which holds the takeover without
republishing an identical frame.

SPDX-FileCopyrightText: (C) 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
import secrets
import time

from . import hlp, profile as profile_module

# the rate when none was asked for and the board states none
DEFAULT_FPS = 60.0
# The highest rate streamed unless --fps asks for more. hlp-bench measured 250
# frames/s on a 46-LED board at three reports per frame, and half of that leaves
# room for a longer frame, a slow poll or a retry.
MAX_STREAM_FPS = 120.0
# the name page 1 shows other applications while the bridge holds the lights
CLAIM_NAME = b'hlp-spice2x'
# How long a frame's acknowledgement is waited for. The board answers nothing for
# about 200 ms while it writes its settings flash, and a shorter wait takes such a
# write for a lost frame.
ACK_TIMEOUT = 0.25
# how often page 1 is read on a board that cannot send STATE_CHANGED
STATE_POLL_SECONDS = 5.0
# how long to leave a board whose pages could not be re-read before asking again
REFRESH_RETRY_SECONDS = 5.0
# how often a board that left the bus is looked for
RECONNECT_SECONDS = 1.0
# how many housekeeping requests in a row go unanswered before the board is taken as lost
SILENT_LIMIT = 3
# The order a frame's entries are staged in. Entries apply in order, so where
# two reach the same LED the later one wins. A kind of light goes first, then the
# controls by name, then single lights. Raw LED runs go after all of them.
ENTRY_PASSES = (hlp.ADDRESSING_KIND, hlp.ADDRESSING_ACTION, hlp.ADDRESSING_LIGHT)


class FrameResult:
    """What became of one published frame.

    Truthy when the frame was acknowledged, so a caller can read it as a yes
    or no.
    """

    def __init__(self, acknowledged: bool, holder=None):
        """Record how the board answered.

        :param acknowledged: whether the frame's last report was answered with status 0
        :param holder: the holder as a status 4 reply showed it, where the
            board refused the frame after a change of holder
        """
        self.acknowledged = acknowledged
        self.holder = holder

    def __bool__(self) -> bool:
        """Report whether the frame was acknowledged."""
        return self.acknowledged


def send_frame(device, frame, staging, clear_first=False) -> FrameResult:
    """Stage a resolved frame and publish it.

    The staged frame is kept while the takeover lasts, so a control that drops
    out of the frame keeps its last colour unless it is unstaged. When the set
    of targets shrinks the caller asks for a CLEAR first, which unstages every
    pixel. An unstaged pixel is off in whole-frame takeover and shows the
    on-board animation in overlay.

    The passes are emitted in a fixed order that must be kept. They all write
    into one staged frame, where the last write to an LED wins. A kind of light
    and a control name reach every light carrying them. A target naming one
    light or a raw LED run is staged after them, so it wins.

    Every report but the last is sent with NO_REPLY. The last carries
    COMMIT_AFTER, so it stages, publishes and is the frame's one
    acknowledgement. If no reply comes the report is sent once more, and
    after that the frame is taken as not shown.

    :param device: an opened HostLightingDevice
    :param frame: a resolved frame
    :param staging: how each target reaches the board's lights, from staging_for
    :param clear_first: whether to clear the staged frame before staging this one
    :return: what became of the frame, falsy if it went unacknowledged or was refused
    :raises hlp.HostLightingRejected: if the board refuses the frame with a
        status other than 4
    """
    # the reports already waiting are read before the frame's first report goes out
    device.drain()
    entries, runs = {}, []
    for target, colour in frame.items():
        # Which light an index names is known only against a board. A target that
        # reached here unresolved is a wiring mistake, and staging is sent without
        # replies, so it is raised here.
        if target not in staging:
            raise ValueError(f"target {target} was never resolved against a board")
        if staging[target] is None:
            continue  # held back, since the board cannot honour it as it stands
        operation, white = staging[target]
        pixel_format = hlp.PIXEL_RGBW if white else hlp.PIXEL_RGB
        pixel = hlp.subtractive_white(colour) if white else tuple(colour[:3])
        if operation[0] == 'run':
            runs.append((operation[1], operation[2], pixel, pixel_format))
        elif operation[0] == 'entry':
            entries.setdefault((operation[1], pixel_format), []).append((operation[2], pixel))
        else:
            raise ValueError(f"staging carries an unknown operation: {operation[0]!r}")

    requests = []
    for addressing in ENTRY_PASSES:
        for pixel_format in (hlp.PIXEL_RGB, hlp.PIXEL_RGBW):
            requests.extend((hlp.CMD_STAGE, payload) for payload in hlp.entry_payloads(
                addressing, entries.get((addressing, pixel_format), []), pixel_format))
    for first, count, pixel, pixel_format in runs:
        requests.extend((hlp.CMD_STAGE, payload)
                        for payload in hlp.run_payloads(first, count, pixel, pixel_format))
    if clear_first or not requests:
        # a frame with nothing in it still has to be published, and CLEAR carries the commit
        requests.insert(0, (hlp.CMD_CLEAR, b''))

    for command, payload in requests[:-1]:
        device.send(command, payload, hlp.FLAG_NO_REPLY)
    command, payload = requests[-1]
    for _ in range(2):
        try:
            reply = device.request(command, payload, hlp.FLAG_COMMIT_AFTER, timeout=ACK_TIMEOUT)
        except hlp.HostLightingTimeout:
            continue
        if reply[2] == hlp.STATUS_OK:
            return FrameResult(True)
        refusal = device.rejected(command, reply)
        if reply[2] == hlp.STATUS_CLAIMED:
            return FrameResult(False, refusal.holder)
        raise refusal
    return FrameResult(False)


def addressing_used(profile) -> set:
    """List the addressing modes a profile's targets are staged by.

    :param profile: a loaded profile
    :return: the ADDRESSING_* modes the board has to offer for it
    """
    modes = set()
    for target, _ in profile.values():
        if target[0] == 'button':
            modes.add(target[1][0])
        elif target[0] == 'light':
            modes.add(hlp.ADDRESSING_LIGHT)
        else:
            modes.add(hlp.ADDRESSING_LED_RUN)
    return modes


def validate_targets(profile, caps) -> tuple:
    """Report profile controls that this board cannot light.

    Staging is sent without replies in the main loop, so a control the board
    cannot resolve is skipped without a message. Each one is checked once at
    startup and reported.

    The Lights page says what STAGE will reach, and the Controls page splits
    the rest. A control it leaves out is not on the board, and one it lists is
    wired but has no LED.

    :param profile: a loaded profile
    :param caps: the board's capabilities
    :return: (controls the board has no LED for, controls the board does not have)
    """
    controls = sorted({target[1] for target, _ in profile.values() if target[0] == 'button'})
    unlit = [control for control in controls if not caps.lights_of(control)]
    absent = [control for control in unlit if caps.lacks_control(control)]
    return ([hlp.control_name(control) for control in unlit if control not in absent],
            [hlp.control_name(control) for control in absent])


def light_operation(caps, control, index) -> tuple:
    """Resolve one per-light profile target against the board's Lights page.

    :param caps: the board's capabilities
    :param control: the control being indexed into
    :param index: which of that control's lights, in the order the page lists them
    :return: the staging operation that colours that light
    :raises profile_module.ProfileError: if this board cannot offer that light
    """
    name = hlp.control_name(control)
    records = caps.lights_of(control)
    if index >= len(records):
        raise profile_module.ProfileError(
            f"{name}[{index}] is out of range: this board gives {name} {len(records)} "
            f"light{'' if len(records) == 1 else 's'}")
    return ('entry', hlp.ADDRESSING_LIGHT, records[index]['ordinal'])


def staging_for(profile, caps, problems=None) -> dict:
    """Work out how each of the profile's targets reaches the board's lights.

    Built at connect and again whenever the board says its configuration
    changed, never per frame.

    At startup a profile the board cannot honour raises. A caller re-resolving
    after a configuration change passes `problems`, and the entries the board
    cannot honour are held back and described there.

    :param profile: a loaded profile
    :param caps: the board's capabilities
    :param problems: if given, entries the board cannot honour are described into
        this list and staged as nothing
    :return: mapping of target to (staging operation, whether to send white),
        or to None for an entry held back
    :raises profile_module.ProfileError: if the board cannot honour the profile
        and no `problems` list was given
    """
    staging = {}
    # Whether a target carries white is decided per target. Two entries may share
    # a target, and resolve_frame merges them into the wider of the two colours,
    # so a per-entry decision would follow whichever entry the profile lists last.
    wide = {target for target, colour in profile.values() if len(colour) > 3}
    for target, _ in profile.values():
        # a white component only survives where the chain has an emitter for it
        white = target in wide and caps.host_white
        if target[0] == 'light':
            try:
                staging[target] = (light_operation(caps, target[1], target[2]), white)
            except profile_module.ProfileError as error:
                if problems is None:
                    raise
                problems.append(str(error))
                staging[target] = None
        elif target[0] == 'range':
            # An LED run reaching past the addressable limit is an invalid
            # argument that stages none of it, and it is sent without a reply.
            # The run is held to the limit here.
            count = min(target[2], caps.led_limit - target[1])
            staging[target] = (('run', target[1], count), white) if count > 0 else None
        else:
            staging[target] = (('entry',) + tuple(target[1]), white)
    return staging


def white_warnings(profile, caps) -> list:
    """Say, once at startup, where a profile's white component cannot be used.

    :param profile: a loaded profile
    :param caps: the board's capabilities
    :return: warnings to report, empty when the profile asks for no white
    """
    carrying = [target for target, colour in profile.values() if len(colour) > 3]
    if not carrying or caps.host_white:
        return []
    asking = ('1 profile entry asks' if len(carrying) == 1
              else f"{len(carrying)} profile entries ask")
    if not caps.white_channel:
        chain = hlp.LED_FORMAT_NAMES.get(caps.summary.get('colour_format'), 'unknown')
        return [f"{asking} for a white component, but this "
                f"board's LED chain is {chain} and has no white emitter to drive it, so the "
                f"white is dropped and the colour is sent as RGB"]
    return [f"{asking} for a white component, but this board's firmware does not list the "
            f"RGBW pixel format, so the white is dropped and the colour is sent as RGB"]


def ranges_past_the_board(profile, caps) -> list:
    """Find raw ranges that reach past the last LED this board has.

    The load-time check bounds a range by what the wire can carry, which is all
    that can be known without a board. The board reports its own extent, and
    LEDs past it can be staged but are never shown.

    These are reported as a warning and the rest of each range is staged.

    :param profile: a loaded profile
    :param caps: the board's capabilities
    :return: the (start, count) pairs that reach past the board, in order
    """
    return sorted({(target[1], target[2]) for target, _ in profile.values()
                   if target[0] == 'range' and target[1] + target[2] > caps.led_extent})


def restage(profile, caps, report) -> dict:
    """Re-resolve a profile against a board whose configuration has changed.

    The entries that no longer fit the board are held back and reported once.
    The rest of the profile carries on.

    :param profile: a loaded profile
    :param caps: the board's capabilities, already refreshed
    :param report: callable used for progress output
    :return: the staging to use from here on
    """
    problems = []
    restaged = staging_for(profile, caps, problems)
    for problem in problems:
        report(f"warning: after the board's configuration changed, {problem}")
    return restaged


def resolve_stream_rate(fps, caps) -> tuple:
    """Decide how fast to stream, and why.

    A board that renders slower than the cap is matched, since frames it never
    renders use the USB bus for nothing.

    A board that renders faster is held to the cap, which leaves room for a
    slow poll or a retry.

    :param fps: the rate asked for, or None if nobody asked
    :param caps: the board's capabilities
    :return: (the rate to stream at, why it is that rate)
    """
    if fps is not None:
        return fps, 'asked for with --fps'
    if not caps.connected:
        return DEFAULT_FPS, 'no board to ask'
    if not caps.render_hz:
        return DEFAULT_FPS, 'the board did not state a render rate'
    if caps.render_hz <= MAX_STREAM_FPS:
        return float(caps.render_hz), "matching the board's render rate"
    return MAX_STREAM_FPS, f"capped below the board's {caps.render_hz} Hz"


def frame_stats(frames: int, misses: int, started: float) -> str:
    """Summarise a run as frames published, rate and unacknowledged frames."""
    elapsed = max(time.monotonic() - started, 1e-6)
    return (f"{frames} frames in {elapsed:.1f}s ({frames / elapsed:.1f}/s published), "
            f"{misses} frames unacknowledged")


def held_by_another(events, token) -> bool:
    """Say whether any of the events received shows a holder other than this application.

    :param events: event reports, oldest first
    :param token: this application's claim token
    :return: True if the claim was unheld or another application's at any of them
    """
    for event in events:
        try:
            state, _ = hlp.decode_state_changed(event)
        except hlp.HostLightingError:
            continue
        if state['holder_token'] != token:
            return True
    return False


def latest_state(events):
    """Pick the newest board state out of the events received.

    A STATE_CHANGED event carries the whole of page 1 and the state token as
    they stood when it was sent, so only the last one matters.

    :param events: event reports, oldest first
    :return: (the page 1 record, the state token), or None if there was none
    """
    for event in reversed(events):
        try:
            return hlp.decode_state_changed(event)
        except hlp.HostLightingError:
            continue  # an event this bridge does not know
    return None


class LightsClaim:
    """The bridge's claim on a board's lights.

    Applications sharing a board coordinate through CLAIM. The claim is a lease
    that only the holder's own CLAIM renews. When it lapses or another
    application takes it over, the takeover ends and the CONFIGURE settings
    return to their defaults. The claim is renewed several times per keepalive
    timeout, and the settings are sent again whenever it is taken again.
    """

    def __init__(self, device, overlay, timeout_ms, brightness, report):
        """Describe the claim and the settings that go with it. Nothing is sent yet.

        :param device: an opened HostLightingDevice
        :param overlay: overlay onto the board's animations instead of taking the whole frame
        :param timeout_ms: the keepalive timeout to ask for
        :param brightness: scale the bridge's pixels by the board's brightness
        :param report: callable used for progress output
        """
        self.device = device
        self.settings = {'overlay': overlay, 'keepalive_ms': timeout_ms, 'brightness': brightness}
        self.report = report
        # random and non-zero, so the board can tell this application from another
        self.token = secrets.randbits(32) or 1
        self.held = False
        self.configured = False
        self.applied = {}
        self.interval = timeout_ms / 3000.0
        self.due = 0.0

    def take(self, force: bool, wait: bool = False) -> None:
        """Claim the board's lights, then send the settings.

        :param force: take the claim over from an application that holds it
        :param wait: where another application holds the claim, report it and
            leave the renewals to take the claim when the board is unclaimed
        :raises hlp.HostLightingError: if another application holds the claim and
            it was neither to be taken over nor waited for
        """
        holder = self.device.claim(hlp.CLAIM_TAKE_OVER if force else hlp.CLAIM_TAKE,
                                   self.token, CLAIM_NAME)
        if holder['holder_token'] != self.token and wait:
            self.report(f"'{holder['holder_name']}' holds the board's lights. Waiting "
                        f"for it to give them up")
            self.due = time.monotonic() + self.interval
            return
        if holder['holder_token'] != self.token:
            raise hlp.HostLightingError(
                f"'{holder['holder_name']}' holds this board's lights. Close it, or pass "
                f"--force to take the lights over from it")
        self.held = True
        self.configure()

    def configure(self) -> None:
        """Send CONFIGURE, and pace the renewals by the timeout the board applied.

        The timeout is the claim's lease as well as the takeover's keepalive, and
        the lease must be renewed at least twice within it. A renewal is sent
        every third of it.

        A caller sends this straight after a CLAIM. A shorter timeout applies
        to the lease already running, which lapses if its last renewal is older.
        """
        self.configured = False
        self.applied = self.device.configure(**self.settings)
        self.configured = True
        self.interval = self.applied['keepalive_ms'] / 3000.0
        self.due = time.monotonic() + self.interval

    def renew(self) -> bool:
        """Renew the claim, or ask for it again while another application holds it.

        One CLAIM answers every case:

        * Held and unchanged, it renews the lease and, with KEEPALIVE, the takeover.
        * Lapsed, it takes the claim again under a new generation.
        * Taken over, it changes nothing and shows the holder.
        * While waiting, it takes the claim as soon as the board is unclaimed.

        :return: True when the claim came back under a new generation, which
            ended the takeover and cleared the staged frame, so the whole frame
            has to be staged again
        """
        generation = self.device.generation
        self.due = time.monotonic() + self.interval
        holder = self.device.claim(hlp.CLAIM_TAKE, self.token, CLAIM_NAME,
                                   hlp.FLAG_KEEPALIVE if self.held else 0)
        if holder['holder_token'] != self.token:
            if self.held:
                self.report(f"'{holder['holder_name']}' took the board's lights over. Waiting "
                            f"for it to give them up")
            self.held = False
            return False
        if not self.held:
            self.report("the board is unclaimed again, lights claimed")
        self.held = True
        retaken = self.device.generation != generation
        if retaken or not self.configured:
            self.configure()
        return retaken

    def give_up(self) -> None:
        """Give the claim up, which ends the takeover and restores the on-board animations.

        Nothing is sent while another application holds the claim. A board that
        has gone away, or does not answer, restores its animations itself when
        the keepalive timeout runs out.
        """
        if not self.held:
            return
        self.held = False
        try:
            holder = self.device.claim(hlp.CLAIM_GIVE_UP, self.token)
        except hlp.HostLightingError:
            return
        if not holder['holder_token']:
            self.report("claim given up, on-board animations restored")


def start_board(device, profile, settings, force, report, returning=False) -> tuple:
    """Run the start-up against a board: handshake, claim, settings, subscription and staging.

    :param device: an opened HostLightingDevice
    :param profile: a loaded profile
    :param settings: (overlay, keepalive timeout in ms, apply board brightness)
    :param force: take the board's lights over from an application that holds them
    :param report: callable used for progress output
    :param returning: the board left the bus mid-run and is back, so another
        holder is waited for whatever `force` says, and an entry the board
        cannot honour is held back
    :return: (the board's capabilities, the claim, the staging)
    :raises hlp.HostLightingIncompatible: if the board's firmware cannot honour the profile
    :raises profile_module.ProfileError: if the board itself cannot honour the profile
    """
    caps = hlp.negotiate(device, addressing_used(profile))
    label, firmware = (device.read_identity() if hlp.PAGE_IDENTITY in caps.hello['pages']
                       else ('?', '?'))
    report(f"board: {label} ({firmware})")
    report(caps.summary_line())
    # the claim comes before CONFIGURE and before anything is staged
    claim = LightsClaim(device, *settings, report)
    try:
        claim.take(force and not returning, wait=returning)
        if claim.held:
            report(f"takeover: {'overlay' if claim.applied['overlay'] else 'whole frame'}, "
                   f"{claim.applied['keepalive_ms']} ms keepalive, "
                   f"{'applying' if claim.applied['brightness_applied'] else 'ignoring'} "
                   f"board brightness")
        if caps.events:
            device.subscribe(1 << hlp.EVENT_STATE_CHANGED)
        # a change before the subscription raises no event, so page 1 is read once after it
        _, token = device.read_record(hlp.PAGE_STATE, hlp.STATE_GROUPS)
        if token != caps.token:
            caps.refresh(device)
        unmapped, absent = validate_targets(profile, caps)
        if absent:
            report(f"warning: {len(absent)} mapped control(s) are not on this board: "
                   f"{', '.join(absent)}")
        if unmapped:
            report(f"warning: {len(unmapped)} mapped control(s) have no LED on this "
                   f"board: {', '.join(unmapped)}")
        problems = [] if returning else None
        staging = staging_for(profile, caps, problems)
        for problem in problems or []:
            report(f"warning: {problem}")
        over = ranges_past_the_board(profile, caps)
        if over:
            report(f"warning: {len(over)} raw range(s) reach past this board's "
                   f"{caps.led_extent} LEDs and light nothing beyond it: "
                   + ', '.join(f"[{start}, {count}]" for start, count in over))
        for warning in white_warnings(profile, caps):
            report(f"warning: {warning}")
        several = sorted(hlp.control_name(target[1]) for target in staging
                         if target[0] == 'button' and target[1][0] == hlp.ADDRESSING_ACTION
                         and len(caps.lights_of(target[1])) > 1)
        if several:
            report(f"controls the board gives more than one light, all of which will be "
                   f"lit: {', '.join(several)}")
    except BaseException:
        claim.give_up()
        raise
    return caps, claim, staging


def lost_line(error) -> str:
    """Say how a board was lost: it stopped answering, or it left the bus.

    :param error: the HostLightingDisconnected that ended the session
    :return: the line to report
    """
    if isinstance(error, hlp.HostLightingUnanswered):
        return "board stopped answering"
    return f"board disconnected ({error})"


def close_quietly(device) -> None:
    """Close a device that may already have left the bus."""
    try:
        device.close()
    except OSError:
        pass


def run(connection, profile, device=None, fps=None, timeout_ms=2000,
        overlay=False, full_brightness=False, force=False, report=print, reopen=None):
    """Poll spice2x and stream frames until interrupted or the game closes.

    :param connection: an open SpiceConnection
    :param profile: a loaded profile
    :param device: an opened HostLightingDevice, or None for a dry run
    :param fps: poll rate, or None to resolve one after the handshake
    :param timeout_ms: keepalive timeout sent in CONFIGURE
    :param overlay: overlay onto the board's animations instead of taking the whole frame
    :param full_brightness: ignore the board's brightness setting
    :param force: take the board's lights over from an application that holds them
    :param report: callable used for progress output
    :param reopen: callable taking a board ID and returning that board opened
        again, or None while it is off the bus. Without one a disconnect ends the run.
    :raises hlp.HostLightingIncompatible: if the board's firmware cannot honour the profile
    :raises profile_module.ProfileError: if the board itself cannot honour the profile
    """
    frames = misses = 0
    started = time.monotonic()
    caps = hlp.HostLightingCapabilities.absent()
    staging = {}
    claim = None
    settings = (overlay, timeout_ms, not full_brightness)
    # the device opened here when the board returned, which this function closes
    reopened = None

    try:
        if device is not None:
            caps, claim, staging = start_board(device, profile, settings, force, report)

        # resolved after the handshake, since the board's render rate is unknown before it
        fps, why = resolve_stream_rate(fps, caps)
        report(f"streaming at {fps:g} fps ({why})")
        period = 1.0 / fps
        last_frame = None
        reported_unmapped = False
        shown = clear_next = False
        refresh_due = None
        complaints = set()
        away = False
        silent = 0
        started = next_tick = poll_due = retry_due = time.monotonic()

        while True:
            states = connection.lights_read()
            if not reported_unmapped:
                missing = sorted(set(states) - set(profile))
                if missing:
                    report(f"{len(missing)} lights not in the profile, ignoring: "
                           f"{', '.join(missing[:6])}{' ...' if len(missing) > 6 else ''}")
                reported_unmapped = True

            frame = profile_module.resolve_frame(states, profile)
            now = time.monotonic()

            try:
                if away:
                    found = None
                    if now >= retry_due:
                        retry_due = now + RECONNECT_SECONDS
                        try:
                            found = reopen(caps.hello['board_id'])
                        except OSError:
                            pass  # the bus could not be enumerated, so it is tried again
                    if found is not None:
                        try:
                            caps, claim, staging = start_board(found, profile, settings, force,
                                                               report, returning=True)
                        except hlp.HostLightingIncompatible:
                            close_quietly(found)
                            raise
                        except hlp.HostLightingError:
                            close_quietly(found)  # not answering yet, or gone again
                        except BaseException:
                            close_quietly(found)
                            raise
                        else:
                            # a board back on the bus holds no staged frame
                            device = reopened = found
                            away = False
                            last_frame = None
                            shown = clear_next = False
                            refresh_due = None
                            silent = 0
                            poll_due = now
                elif device is not None:
                    again = False
                    # Housekeeping runs on wall-clock time, so a static game screen,
                    # which produces no new frames, still keeps the claim and still
                    # notices the board changing.
                    try:
                        seen = None
                        if caps.events:
                            events = device.take_events()
                            seen = latest_state(events)
                            if claim.held and held_by_another(events, claim.token):
                                # The generation has seven values, so a claim that
                                # changed holder and came back can carry the one held
                                # here. Its settings and its frame are gone either way.
                                claim.configured = False
                                claim.due = now
                                again = True
                        # The renewal goes before any request. A read under a generation
                        # that is no longer current is refused, and CLAIM is not.
                        if now >= claim.due:
                            again = claim.renew() or again
                            silent = 0
                        if not caps.events and now >= poll_due:
                            poll_due = now + STATE_POLL_SECONDS
                            seen = device.read_record(hlp.PAGE_STATE, hlp.STATE_GROUPS)
                        if seen is not None:
                            state, token = seen
                            if token != caps.token and refresh_due is None:
                                refresh_due = now
                            if claim.held and state['holder_token'] != claim.token:
                                claim.due = now  # the holder has changed
                            elif not claim.held and not state['holder_token']:
                                claim.due = now  # the board is unclaimed again
                            elif claim.held and shown and not state['takeover_live']:
                                # the takeover ended, and the staged frame went with it
                                again = True
                        if now >= claim.due:
                            again = claim.renew() or again
                            silent = 0
                        if refresh_due is not None and now >= refresh_due:
                            try:
                                # A light's ordinal is what an index addresses, so
                                # the pages are read again before anything more is
                                # staged by ordinal.
                                changed = caps.refresh(device)
                            except hlp.HostLightingDisconnected:
                                raise
                            except hlp.HostLightingError as error:
                                # the pages can still be changing after the token moved
                                report(f"warning: could not re-read the board's configuration "
                                       f"({error}), retrying in {REFRESH_RETRY_SECONDS:g}s")
                                refresh_due = now + REFRESH_RETRY_SECONDS
                            else:
                                refresh_due = None
                                if changed:
                                    staging = restage(profile, caps, report)
                                    again = True
                                    ranges = any(target[0] == 'range' for target, _ in profile.values())
                                    report("board configuration changed, controls re-resolved"
                                           + (". Check the profile's raw ranges" if ranges else ""))
                    except hlp.HostLightingDisconnected:
                        raise
                    except hlp.HostLightingTimeout:
                        # The next tick asks again. A static game screen sends no
                        # frame to show a lost board, so the unanswered requests
                        # are counted here.
                        silent += 1
                        if silent >= SILENT_LIMIT:
                            raise hlp.HostLightingUnanswered("no reply to the renewals") from None
                    except hlp.HostLightingError as error:
                        if getattr(error, 'status', None) == hlp.STATUS_CLAIMED:
                            # Refused under a generation that is no longer current, so
                            # the holder has changed. The next renewal shows the holder.
                            claim.due = now
                        elif str(error) not in complaints:
                            # reported once, since the request is repeated every tick it keeps failing
                            complaints.add(str(error))
                            report(f"warning: the board's state could not be read ({error}). "
                                   f"The run carries on")
                    if again:
                        last_frame = None
                        shown = False
                        clear_next = True

                if frame != last_frame and not away:
                    # a target that has left the frame must be cleared, or its last
                    # colour stays staged on the board and republishes every commit
                    departed = bool(set(last_frame) - set(frame)) if last_frame else False
                    if device is None:
                        report(f"frame:{' [clear]' if departed else ''} "
                               f"{profile_module.format_frame(frame)}")
                        last_frame = frame
                        frames += 1
                    elif claim.held:
                        result = send_frame(device, frame, staging, clear_first=departed or clear_next)
                        if result:
                            last_frame = frame
                            shown = True
                            clear_next = False
                            frames += 1
                        elif result.holder is not None:
                            claim.due = time.monotonic()  # refused after a change of holder
                        else:
                            # Not shown, so the next frame is staged whole. The board
                            # is lost only if it does not answer HELLO either. A
                            # reply of any status is an answer.
                            misses += 1
                            last_frame = None
                            clear_next = True
                            try:
                                device.request(hlp.CMD_HELLO)
                            except hlp.HostLightingTimeout:
                                raise hlp.HostLightingUnanswered("no reply to HELLO") from None
            except hlp.HostLightingDisconnected as error:
                if reopen is None:
                    raise
                # The board left the bus, as it does when another host reboots it,
                # or stopped answering. Nothing is given up on it.
                report(f"{lost_line(error)}. Waiting for it to return")
                close_quietly(device)
                claim = reopened = None
                away = True
                retry_due = now + RECONNECT_SECONDS

            next_tick += period
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()  # fell behind, so the schedule starts again from now

    except KeyboardInterrupt:
        report("")
        report(frame_stats(frames, misses, started))
    except hlp.HostLightingDisconnected as error:
        # reported as the board, so the user does not look for a fault in spice2x
        report("")
        report(lost_line(error))
        report(frame_stats(frames, misses, started))
        claim = None
    except (ConnectionError, OSError) as error:
        # The game exiting closes the API socket, which is a normal ending. The
        # claim is still given up in the finally.
        report("")
        report(f"spice2x connection lost ({error})")
        report(frame_stats(frames, misses, started))
    finally:
        if claim is not None:
            claim.give_up()
        if reopened is not None:
            close_quietly(reopened)
