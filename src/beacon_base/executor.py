"""Runs remote repeater jobs from inside the companion session.

The session thread has to keep draining reports while a command waits minutes of airtime for an answer, so this is a small state
machine that the session loop drives, not a blocking call. It never touches the database: the session's handler hands it jobs
(``claim``) and takes the outcome (``finish``), the same split as reports (``docs/plan/repeater-remote.md``).

    claim -> contact -> login -> command -> wait for the tagged reply -> next command ... -> interpret -> finish
                           \\-> timeout: reset the route, log in again, send again, once; then fail ``no_reply``

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
ATTEMPTS = 2  # one try and one retry after the route is reset
NOT_FOUND = 2  # the companion's ERR_CODE_NOT_FOUND


class RemoteExecutor:
    def __init__(
        self,
        cfg: RemoteConfig,
        claim: Callable[[], RemoteJob | None],
        finish: Callable[[RemoteJob, JobOutcome], None],
        clock_ready: Callable[[CompanionLink], str | None],
        base_channel_hash: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """claim() returns the next job or None; finish(job, outcome) records it; clock_ready(link) returns None when the companion's
        clock may be used for a login, else the error code to fail the job with."""
        self._cfg = cfg
        self._claim = claim
        self._finish = finish
        self._clock_ready = clock_ready
        self._base_hash = base_channel_hash
        self._now = clock
        self._next_claim = 0.0
        self._tag_n = 0
        self.contacts: set[bytes] = set()  # keys known to the companion on this connection
        self.logged_in: set[bytes] = set()  # keys that answered a login as admin on this connection
        self._events: list[tuple[str, object]] = []
        self.stats = {"jobs": 0, "failed": 0, "stale_replies": 0, "untagged_replies": 0}
        self._reset_job()

    # --- state ----------------------------------------------------------------------------------------------------------

    def _reset_job(self) -> None:
        self.job: RemoteJob | None = None
        self._steps: list[remote.Step] = []
        self._replies: list[str] = []
        self._phase = "idle"  # idle, login, reply
        self._attempt = 1
        self._deadline = 0.0
        self._started = 0.0
        self._tag = ""
        self._flooded = False
        self._round_trips = 0
        self._login: companion.LoginResult | None = None
        self._events.clear()

    @property
    def busy(self) -> bool:
        return self.job is not None

    def describe(self) -> str:
        """What the heartbeat reports."""
        if self.job is None:
            return "idle"
        return f"job {self.job.id}: {self.job.op} {self.job.kind} on {self.job.repeater_prefix.hex()[:12]} ({self._phase})"

    def new_connection(self) -> None:
        """A fresh link: the companion may have rebooted, so what was known about logins and contacts is gone."""
        self.contacts.clear()
        self.logged_in.clear()

    # --- events from the session ----------------------------------------------------------------------------------------

    def offer_login(self, result: companion.LoginResult) -> None:
        if self._phase == "login":
            self._events.append(("login", result))

    def offer_message(self, message: companion.ContactMessage) -> bool:
        """True when the message is the reply to the job's command (so the session does not pass it on)."""
        if self._phase == "reply" and self.job is not None and self.job.pubkey and message.prefix == self.job.pubkey[:6] and message.is_cli_data:
            self._events.append(("message", message))
            return True
        return False

    # --- the loop -------------------------------------------------------------------------------------------------------

    def poll(self, link: CompanionLink) -> None:
        """Advance the job: take what arrived, check the deadlines, start the next job when idle."""
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
                self._on_login(link, payload, now)  # type: ignore[arg-type]
            else:
                self._on_message(link, payload, now)  # type: ignore[arg-type]
        if self.job is not None:
            if now - self._started > self._cfg.max_job_s:
                self._fail("no_reply", f"the job ran for more than {self._cfg.max_job_s:g} s")
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
        self._begin_login(link, now)

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
                link.request(companion.add_update_contact(key, companion.ADV_TYPE_REPEATER, name, job.lat, job.lon), [companion.RESP_OK])
            except CommandError as e2:
                self._fail("contact_failed", f"the companion would not take the repeater as a contact: {companion.describe_error(e2.code)}")
                return False
            log.info("added repeater %s to the companion's contacts", key[:8].hex())
        self.contacts.add(key)
        return True

    def _timeout(self, sent: companion.Sent) -> float:
        return min(max(sent.timeout_ms / 1000.0 * self._cfg.timeout_factor, self._cfg.min_timeout_s), self._cfg.max_timeout_s)

    def _begin_login(self, link: CompanionLink, now: float) -> None:
        job = self.job
        assert job is not None and job.pubkey is not None
        if job.password is None and job.pubkey in self.logged_in:
            self._send_step(link, now)  # the repeater has the base as an admin from earlier on this connection
            return
        try:
            sent = companion.parse_sent(link.request(companion.send_login(job.pubkey, job.password or ""), [companion.RESP_SENT]))
        except CommandError as e:
            self._fail("companion_refused", f"the companion would not send the login: {companion.describe_error(e.code)}")
            return
        self._flooded = self._flooded or sent.flooded
        self._phase, self._deadline = "login", now + self._timeout(sent)

    def _send_step(self, link: CompanionLink, now: float) -> None:
        job = self.job
        assert job is not None and job.pubkey is not None
        self._tag_n += 1
        self._tag = companion.make_tag(self._tag_n)
        text = companion.tag_command(self._tag, self._steps[len(self._replies)].command)
        try:
            sent = companion.parse_sent(link.request(companion.send_cli(job.pubkey, text), [companion.RESP_SENT]))
        except CommandError as e:
            self._fail("companion_refused", f"the companion would not send the command: {companion.describe_error(e.code)}")
            return
        self._flooded = self._flooded or sent.flooded
        self._phase, self._deadline = "reply", now + self._timeout(sent)

    def _on_login(self, link: CompanionLink, result: companion.LoginResult, now: float) -> None:
        job = self.job
        assert job is not None and job.pubkey is not None
        if result.prefix != job.pubkey[:6] or self._phase != "login":
            return
        self._login = result
        if result.admin:
            self.logged_in.add(job.pubkey)
            self._send_step(link, now)
            return
        self.logged_in.discard(job.pubkey)
        if job.password is None:
            self._fail("needs_password", None)
        else:
            self._fail("not_admin", f"{remote.ERROR_TEXT['not_admin']} (permission {result.permissions})")

    def _on_message(self, link: CompanionLink, message: companion.ContactMessage, now: float) -> None:
        tag, text = companion.split_tag(message.text)
        if tag is not None and tag != self._tag:
            self.stats["stale_replies"] += 1  # the late answer to a command that was already given up on
            log.debug("ignoring a reply tagged %s, waiting for %s", tag, self._tag)
            return
        if tag is None:
            self.stats["untagged_replies"] += 1  # a repeater that does not reflect the tag: take the one reply that came
        self._replies.append(text)
        self._round_trips += 1
        self._attempt = 1
        step = self._steps[len(self._replies) - 1]
        if len(self._replies) >= len(self._steps) or remote.step_failed(step, text):
            self._complete()
        else:
            self._send_step(link, now)

    def _on_timeout(self, link: CompanionLink, now: float) -> None:
        job = self.job
        assert job is not None and job.pubkey is not None
        if self._attempt >= ATTEMPTS:
            self._fail("no_reply")
            return
        self._attempt += 1
        log.info("remote job %d: no answer from %s, resetting its route and trying again", job.id, job.repeater_prefix.hex())
        self.logged_in.discard(job.pubkey)
        try:
            link.request(companion.reset_path(job.pubkey), [companion.RESP_OK])
        except CommandError:
            pass  # not a contact any more; the login below says so
        self._begin_login(link, now)

    # --- the end --------------------------------------------------------------------------------------------------------

    def _extras(self, result: dict) -> dict:
        result["round_trips"] = self._round_trips
        result["flooded"] = self._flooded
        result["seconds"] = round(self._now() - self._started, 1)
        if self._login is not None:
            result["login"] = {"permissions": self._login.permissions, "admin": self._login.admin, "repeater_time": self._login.server_time}
        return result

    def _complete(self) -> None:
        job = self.job
        assert job is not None
        outcome = remote.interpret(job.kind, job.op, job.params, self._replies, self._base_hash)
        self._extras(outcome.result)
        self._done(job, outcome)

    def _fail(self, code: str, message: str | None = None) -> None:
        job = self.job
        assert job is not None
        result: dict = {"replies": [{"command": s.command, "reply": r} for s, r in zip(self._steps, self._replies)]}
        self._extras(result)
        self._done(job, JobOutcome(False, code, message or remote.ERROR_TEXT.get(code, code), result))

    def _done(self, job: RemoteJob, outcome: JobOutcome) -> None:
        if not outcome.ok:
            self.stats["failed"] += 1
        log.info("remote job %d %s%s", job.id, "done" if outcome.ok else "failed", "" if outcome.ok else f" ({outcome.code}): {outcome.message}")
        self._reset_job()
        self._next_claim = 0.0  # look for the next job at once
        self._finish(job, outcome)
