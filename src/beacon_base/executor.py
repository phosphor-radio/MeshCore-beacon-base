"""Runs remote repeater jobs from inside the companion session.

The session thread has to keep draining reports while a command waits minutes of airtime for an answer, so this is a small state
machine that the session loop drives, not a blocking call. It never touches the database: the session's handler hands it jobs
(``claim``) and takes the outcome (``finish``), the same split as reports (``docs/plan/repeater-remote.md``).

    claim -> contact -> login -> command -> wait for the tagged reply -> next command ... -> interpret -> finish

Pacing. The radio is half duplex and shared with every repeater in earshot, and a reply is rebroadcast by the repeaters that hear it,
each after a random delay. Sending the next command the moment a reply arrives puts it on the air while the echoes of that reply are
still going round, so the base waits ``remote.step_gap_s`` after anything it receives (a login answer, a reply, a late reply) before it
transmits again, and the same after a timeout. The wait is a time, not a sleep: reports keep draining meanwhile.

Retries. A step is tried up to three times. A timeout first sends the same login or command again on the same route (one packet,
and a late answer to the first try still counts); a second timeout resets the stored route, logs in again over a flood, and sends the
command a last time; a third gives up with ``no_reply``.

Everything is serialised: one job, one command in flight. The callbacks the session makes from deep inside a request (a login push)
or from the queue drain (a reply) only record what arrived; the work is done in ``poll`` from the main loop, where it is safe to
send commands.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from . import companion, remote
from .config import RemoteConfig
from .link import CommandError, CompanionLink
from .remote import JobOutcome, RemoteJob

log = logging.getLogger(__name__)

CLAIM_INTERVAL = 0.25  # seconds between looks at the job queue when it is empty (a read, no write lock)
ATTEMPTS = 3  # send; send again on the same route; reset the route, log in again and send
NOT_FOUND = 2  # the companion's ERR_CODE_NOT_FOUND


class _JobEnded(Exception):
    """The job was already failed while a send was being recovered; nothing more to do for it."""


class RemoteExecutor:
    def __init__(
        self,
        cfg: RemoteConfig,
        claim: Callable[[], RemoteJob | None],
        finish: Callable[[RemoteJob, JobOutcome], None],
        clock_ready: Callable[[CompanionLink], str | None],
        base_channel_hash: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        """claim() returns the next job or None; finish(job, outcome) records it; clock_ready(link) returns None when the companion's
        clock may be used for a login, else the error code to fail the job with."""
        self._cfg = cfg
        self._claim = claim
        self._finish = finish
        self._clock_ready = clock_ready
        self._base_hash = base_channel_hash
        self._now = clock
        self._wall = wall  # the base's wall clock: the time a clock sync sets, and what a clock read is compared with
        self._next_claim = 0.0
        self._quiet_until = 0.0  # nothing is transmitted before this: the previous exchange is still echoing round the mesh
        self._tag_n = 0
        self.contacts: set[bytes] = set()  # keys known to the companion on this connection
        self.logged_in: set[bytes] = set()  # keys that answered a login as admin on this connection
        self._events: list[tuple[str, object]] = []
        self.stats = {"jobs": 0, "failed": 0, "stale_replies": 0, "untagged_replies": 0, "retries": 0}
        self._reset_job()

    # --- state ----------------------------------------------------------------------------------------------------------

    def _reset_job(self) -> None:
        self.job: RemoteJob | None = None
        self._steps: list[remote.Step] = []
        self._replies: list[str] = []
        self._phase = "idle"  # idle, login (waiting for the answer), reply (waiting for the answer), gap (waiting to transmit)
        self._pending: str | None = None  # what to transmit when the gap ends: 'login' or 'step'
        self._attempt = 1
        self._relogin = False  # the login in progress follows a route reset, so it does not clear the attempt count
        self._deadline = 0.0
        self._started = 0.0
        self._step_tags: list[str] = []  # tags of the tries at the current step; a reply to any of them answers it
        self._sent: dict[int, str] = {}  # the command text as last sent for each step ('time {now}' has the time filled in)
        self._flooded = False
        self._round_trips = 0
        self._retries = 0
        self._route_resets = 0
        self._login: companion.LoginResult | None = None
        self._events.clear()

    @property
    def busy(self) -> bool:
        return self.job is not None

    def describe(self) -> str:
        """What the heartbeat reports."""
        if self.job is None:
            return "idle"
        phase = "pacing" if self._phase == "gap" else self._phase
        return f"job {self.job.id}: {self.job.op} {self.job.kind} on {self.job.repeater_prefix.hex()[:12]} ({phase})"

    def new_connection(self) -> None:
        """A fresh link: the companion may have rebooted, so what was known about logins and contacts is gone."""
        self.contacts.clear()
        self.logged_in.clear()

    # --- events from the session ----------------------------------------------------------------------------------------

    def _waiting_for_login(self) -> bool:
        return self._phase == "login" or (self._phase == "gap" and self._pending == "login")

    def offer_login(self, result: companion.LoginResult) -> None:
        if self._waiting_for_login():
            self._events.append(("login", result))

    def offer_message(self, message: companion.ContactMessage) -> bool:
        """True when the message is a CLI reply from the repeater the job is working on (so the session does not pass it on). Whether it
        answers the step in hand or is a late one is decided in poll."""
        if self.job is not None and self.job.pubkey and message.prefix == self.job.pubkey[:6] and message.is_cli_data:
            self._events.append(("message", message))
            return True
        return False

    # --- the loop -------------------------------------------------------------------------------------------------------

    def poll(self, link: CompanionLink) -> None:
        """Advance the job: take what arrived, transmit when the quiet time is over, check the deadlines, start the next job when idle."""
        now = self._now()
        if self.job is None:
            if now >= self._next_claim:
                job = self._claim()
                if job is None:
                    self._next_claim = now + CLAIM_INTERVAL
                else:
                    self._start(link, job, now)
            return
        events, self._events = self._events, []
        for kind, payload in events:
            if self.job is None:
                break
            if kind == "login":
                self._on_login(payload, now)  # type: ignore[arg-type]
            else:
                self._on_message(payload, now)  # type: ignore[arg-type]
        if self.job is None:
            return
        if now - self._started > self._cfg.max_job_s:
            self._fail("no_reply", f"the job ran for more than {self._cfg.max_job_s:g} s")
        elif self._phase == "gap" and now >= self._quiet_until:
            self._transmit(link, self._pending or "step", now)
        elif self._phase in ("login", "reply") and now > self._deadline:
            self._on_timeout(link, now)

    def abort(self, code: str = "interrupted") -> None:
        """The link was lost or ingest is stopping: the job cannot finish, and a command may or may not have been acted on."""
        if self.job is not None:
            self._fail(code)
        self.new_connection()

    # --- steps ----------------------------------------------------------------------------------------------------------

    def _start(self, link: CompanionLink, job: RemoteJob, now: float) -> None:
        self._reset_job()
        self.job, self._started = job, now
        self.stats["jobs"] += 1
        log.info("remote job %d: %s %s on %s", job.id, job.op, job.kind, job.repeater_prefix.hex())
        try:
            self._steps = remote.plan(job.kind, job.op, job.params)
            remote.validate_password(job.password)
        except remote.RemoteError as e:
            self._fail("bad_value", str(e))
            return
        if job.pubkey is None:
            self._fail("no_key")
            return
        code = self._clock_ready(link)
        if code:
            self._fail(code)
            return
        if not self._ensure_contact(link, job):
            return
        # a password always logs in; without one, a repeater that already took the base as an admin on this connection needs no login
        self._schedule(link, "login" if job.password is not None or job.pubkey not in self.logged_in else "step", now)

    def _ensure_contact(self, link: CompanionLink, job: RemoteJob) -> bool:
        key = job.pubkey
        assert key is not None
        if key in self.contacts:
            return True
        try:
            link.request(companion.get_contact_by_key(key), [companion.RESP_CONTACT])
        except CommandError as e:
            if e.code != NOT_FOUND:
                self._fail("contact_failed", f"the companion would not look up the repeater: {companion.describe_error(e.code)}")
                return False
            try:
                name = job.name or f"repeater {key[:3].hex()}"
                # no position: the contact would hand the base its own value back as if the repeater had advertised it
                link.request(companion.add_update_contact(key, companion.ADV_TYPE_REPEATER, name), [companion.RESP_OK])
            except CommandError as e2:
                self._fail("contact_failed", f"the companion would not take the repeater as a contact: {companion.describe_error(e2.code)}")
                return False
            log.info("added repeater %s to the companion's contacts", key[:8].hex())
        self.contacts.add(key)
        return True

    def _timeout(self, sent: companion.Sent) -> float:
        return min(max(sent.timeout_ms / 1000.0 * self._cfg.timeout_factor, self._cfg.min_timeout_s), self._cfg.max_timeout_s)

    def _heard(self, now: float) -> None:
        """Something came in (or a wait ran out): leave the air alone for the pacing gap."""
        self._quiet_until = max(self._quiet_until, now + self._cfg.step_gap_s)

    def _schedule(self, link: CompanionLink, action: str, now: float) -> None:
        """Transmit 'login' or 'step' now, or when the pacing gap since the last traffic is over."""
        if now < self._quiet_until:
            self._pending, self._phase = action, "gap"
            return
        self._transmit(link, action, now)

    def _transmit(self, link: CompanionLink, action: str, now: float) -> None:
        self._pending = None
        job = self.job
        assert job is not None and job.pubkey is not None
        try:
            if action == "login":
                sent = companion.parse_sent(self._request(link, job, companion.send_login(job.pubkey, job.password or "")))
                self._phase = "login"
            else:
                self._tag_n += 1
                tag = companion.make_tag(self._tag_n)
                index = len(self._replies)
                command = self._steps[index].command.replace("{now}", str(int(self._wall())))  # a resend carries the time of the resend
                self._sent[index] = command
                text = companion.tag_command(tag, command)
                sent = companion.parse_sent(self._request(link, job, companion.send_cli(job.pubkey, text)))
                self._step_tags.append(tag)
                self._phase = "reply"
        except _JobEnded:
            return
        except CommandError as e:
            self._fail("companion_refused", f"the companion would not send the {'login' if action == 'login' else 'command'}: {companion.describe_error(e.code)}")
            return
        self._flooded = self._flooded or sent.flooded
        self._deadline = now + self._timeout(sent)
        if action == "step" and self._steps[len(self._replies)].role == "noreply":
            # a reboot: the board resets before it can answer, so the companion having sent it is all there is to wait for. The ACL is
            # saved to flash only a few seconds after a login, so the next job logs in again rather than trust this one
            self._replies.append("")
            self._round_trips += 1
            self.logged_in.discard(job.pubkey)
            self._complete()

    def _request(self, link: CompanionLink, job: RemoteJob, frame: bytes) -> bytes:
        """Send a login or command. If the companion says it does not know the repeater, the contact was removed behind our back (someone
        cleaned its contact list, or it was reset): add it again and send once more."""
        try:
            return link.request(frame, [companion.RESP_SENT])
        except CommandError as e:
            if e.code != NOT_FOUND:
                raise
        log.info("the companion no longer has repeater %s as a contact; adding it again", job.repeater_prefix.hex())
        self.contacts.discard(job.pubkey)
        if not self._ensure_contact(link, job):
            raise _JobEnded()  # _ensure_contact has failed the job
        return link.request(frame, [companion.RESP_SENT])

    def _on_login(self, result: companion.LoginResult, now: float) -> None:
        job = self.job
        assert job is not None and job.pubkey is not None
        if result.prefix != job.pubkey[:6] or not self._waiting_for_login():
            return
        self._login = result
        self._heard(now)
        if result.admin:
            self.logged_in.add(job.pubkey)
            if not self._relogin:
                self._attempt = 1  # a first login that took a few tries does not count against the command
            self._relogin = False
            self._pending, self._phase = "step", "gap"  # the first command waits out the gap like any other
            return
        self.logged_in.discard(job.pubkey)
        if job.password is None:
            self._fail("needs_password", None)
        else:
            self._fail("not_admin", f"{remote.ERROR_TEXT['not_admin']} (permission {result.permissions})")

    def _on_message(self, message: companion.ContactMessage, now: float) -> None:
        self._heard(now)
        tag, text = companion.split_tag(message.text)
        if not self._step_tags or (tag is not None and tag not in self._step_tags):
            self.stats["stale_replies"] += 1  # the late answer to an earlier step, or to a command given up on
            log.debug("ignoring a reply tagged %s (waiting for %s)", tag, self._step_tags)
            return
        if tag is None:
            self.stats["untagged_replies"] += 1  # a repeater that does not reflect the tag: take the one reply that came
        self._replies.append(text)
        self._round_trips += 1
        self._attempt = 1
        self._step_tags = []
        step = self._steps[len(self._replies) - 1]
        if len(self._replies) >= len(self._steps) or remote.step_failed(step, text):
            self._complete()
        else:
            self._pending, self._phase = "step", "gap"

    def _on_timeout(self, link: CompanionLink, now: float) -> None:
        job = self.job
        assert job is not None and job.pubkey is not None
        if self._attempt >= ATTEMPTS:
            self._fail("no_reply")
            return
        action = "login" if self._phase == "login" else "step"
        self._attempt += 1
        self._retries += 1
        self.stats["retries"] += 1
        self._heard(now)
        if self._attempt < ATTEMPTS:
            log.info("remote job %d: no answer from %s (try %d of %d), sending again", job.id, job.repeater_prefix.hex(), self._attempt - 1, ATTEMPTS)
        else:
            log.info(
                "remote job %d: still no answer from %s, resetting its route, logging in again and trying a last time",
                job.id, job.repeater_prefix.hex(),
            )
            self._route_resets += 1
            self.logged_in.discard(job.pubkey)
            self._relogin, action = True, "login"
            try:
                link.request(companion.reset_path(job.pubkey), [companion.RESP_OK])
            except CommandError:
                pass  # not a contact any more; the login says so
        self._pending, self._phase = action, "gap"

    # --- the end --------------------------------------------------------------------------------------------------------

    def _shown(self) -> list[str]:
        return [self._sent.get(i, s.command) for i, s in enumerate(self._steps)]

    def _extras(self, result: dict) -> dict:
        result["round_trips"] = self._round_trips
        result["flooded"] = self._flooded
        result["retries"] = self._retries
        result["route_resets"] = self._route_resets
        result["seconds"] = round(self._now() - self._started, 1)
        if self._login is not None:
            result["login"] = {"permissions": self._login.permissions, "admin": self._login.admin, "repeater_time": self._login.server_time}
        return result

    def _complete(self) -> None:
        job = self.job
        assert job is not None
        outcome = remote.interpret(job.kind, job.op, job.params, self._replies, self._base_hash, self._wall(), self._shown())
        self._extras(outcome.result)
        self._done(job, outcome)

    def _fail(self, code: str, message: str | None = None) -> None:
        job = self.job
        assert job is not None
        result: dict = {"replies": [{"command": c, "reply": r} for c, r in zip(self._shown(), self._replies)]}
        self._extras(result)
        self._done(job, JobOutcome(False, code, message or remote.ERROR_TEXT.get(code, code), result))

    def _done(self, job: RemoteJob, outcome: JobOutcome) -> None:
        if not outcome.ok:
            self.stats["failed"] += 1
        log.info("remote job %d %s%s", job.id, "done" if outcome.ok else "failed", "" if outcome.ok else f" ({outcome.code}): {outcome.message}")
        self._reset_job()
        self._next_claim = 0.0  # look for the next job at once
        self._finish(job, outcome)
