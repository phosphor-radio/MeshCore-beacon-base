"""The executor's state machine against a scripted link and an injected clock: no threads, no pty, every timing is exact."""

import struct

import pytest

from beacon_base import companion, remote
from beacon_base.config import RemoteConfig
from beacon_base.executor import CLAIM_INTERVAL, RemoteExecutor
from beacon_base.link import CommandError
from beacon_base.remote import JobOutcome, RemoteJob

RKEY = bytes(range(1, 33))
OTHER = bytes(range(101, 133))
CFG = RemoteConfig(min_timeout_s=8.0, max_timeout_s=30.0, timeout_factor=1.5, max_job_s=100.0, step_gap_s=0.0)


def sent_frame(flooded=True, timeout_ms=2000):
    return bytes([companion.RESP_SENT, int(flooded)]) + bytes(4) + struct.pack("<I", timeout_ms)


def login_result(admin=True, perms=3, key=RKEY):
    return companion.parse_login(bytes([companion.PUSH_LOGIN_SUCCESS, int(admin)]) + key[:6] + struct.pack("<I", 1790000000) + bytes([perms, 11]))


def job(kind="name", op="get", params=None, password=None, pubkey=RKEY, job_id=1):
    return RemoteJob(
        id=job_id, repeater_prefix=RKEY[:8], pubkey=pubkey, name="ridge", lat=47.5, lon=-122.25, kind=kind, op=op,
        params=remote.validate(kind, op, params), password=password, created_at=0.0,
    )


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class StubLink:
    """Answers the companion commands the executor sends, and records them."""

    def __init__(self, contact_known=True):
        self.sent: list[bytes] = []
        self.contact_known = contact_known
        self.errors: dict[int, CommandError] = {}  # command code -> raise this
        self.timeout_ms = 2000
        self.flooded = True

    def request(self, payload, expect):
        self.sent.append(bytes(payload))
        op = payload[0]
        if op in self.errors:
            raise self.errors[op]
        if op == companion.CMD_GET_CONTACT_BY_KEY:
            if not self.contact_known:
                raise CommandError(2)
            return companion.build_contact(companion.RESP_CONTACT, bytes(payload[1:33]), 2, "x")
        if op in (companion.CMD_SEND_LOGIN, companion.CMD_SEND_TXT_MSG):
            return sent_frame(self.flooded, self.timeout_ms)
        return bytes([companion.RESP_OK])

    def ops(self):
        return [p[0] for p in self.sent]

    def commands(self):
        """The CLI texts sent, with their tags."""
        return [bytes(p[13:]).decode() for p in self.sent if p[0] == companion.CMD_SEND_TXT_MSG]


class Harness:
    def __init__(self, clock_ready=None, contact_known=True, cfg=CFG):
        self.clock = Clock()
        self.queue: list[RemoteJob] = []
        self.finished: list[tuple[RemoteJob, JobOutcome]] = []
        self.link = StubLink(contact_known)
        self.claims = 0
        self.ex = RemoteExecutor(cfg, self._claim, lambda j, o: self.finished.append((j, o)), clock_ready or (lambda link: None), 0x42, self.clock)

    def _claim(self):
        self.claims += 1
        return self.queue.pop(0) if self.queue else None

    def poll(self):
        self.ex.poll(self.link)

    def advance(self, seconds):
        """Let time pass. Two polls: the first notices a timeout and schedules the resend, the second can transmit it."""
        self.clock.t += seconds
        self.poll()
        self.poll()

    def run_to_login(self, j):
        self.queue.append(j)
        self.poll()

    def reply(self, text, key=RKEY, tag=None, txt_type=companion.TXT_TYPE_CLI_DATA):
        """The repeater's answer to the command in flight, tagged like the one sent unless told otherwise."""
        if tag is None:
            tag = companion.split_tag(self.link.commands()[-1])[0]
        body = text if tag == "" else f"{tag}|{text}"
        msg = companion.parse_contact_message(companion.build_contact_message(key, body, txt_type=txt_type))
        taken = self.ex.offer_message(msg)
        self.poll()
        return taken

    def login(self, **kw):
        self.ex.offer_login(login_result(**kw))
        self.poll()

    @property
    def outcome(self):
        assert len(self.finished) == 1, self.finished
        return self.finished[0][1]


def test_a_get_goes_contact_login_command_reply():
    h = Harness()
    h.run_to_login(job("name", "get"))
    assert h.ex.busy and h.link.ops() == [companion.CMD_GET_CONTACT_BY_KEY, companion.CMD_SEND_LOGIN]
    assert h.link.sent[1][33:] == b""  # an empty password: the ACL login
    h.login()
    assert h.link.ops()[-1] == companion.CMD_SEND_TXT_MSG and h.link.commands()[0].endswith("|get name")
    assert h.reply("> ridge")
    out = h.outcome
    assert out.ok and out.result["values"] == {"name": "ridge"} and out.result["round_trips"] == 1 and out.result["flooded"]
    assert out.result["login"] == {"permissions": 3, "admin": True, "repeater_time": 1790000000}
    assert not h.ex.busy and h.ex.describe() == "idle"


def test_a_set_runs_its_steps_in_order_with_distinct_tags():
    h = Harness()
    h.run_to_login(job("beacon.window", "set", {"seconds": 5}))
    h.login()
    tags = []
    for answer in ("OK", "> 5 secs", "> 0"):
        tags.append(companion.split_tag(h.link.commands()[-1])[0])
        h.reply(answer)
    assert h.link.commands()[0].endswith("|beacon.window 5") and h.link.commands()[1].endswith("|beacon.window") and h.link.commands()[2].endswith("|get advert.interval")
    assert len(set(tags)) == 3
    out = h.outcome
    assert out.ok and out.result["verified"] and out.result["notes"] == [remote.ZERO_HOP_WARNING] and out.result["round_trips"] == 3


def test_a_refused_set_does_not_send_the_read_back():
    h = Harness()
    h.run_to_login(job("beacon.window", "set", {"seconds": 5}))
    h.login()
    h.reply("Err - window must be 1-3600 secs")
    assert h.outcome.code == "rejected" and len(h.link.commands()) == 1


def test_the_contact_is_added_once_per_connection():
    h = Harness(contact_known=False)
    h.run_to_login(job("name", "get", job_id=1))
    assert h.link.ops()[:2] == [companion.CMD_GET_CONTACT_BY_KEY, companion.CMD_ADD_UPDATE_CONTACT]
    assert len(h.link.sent[1]) == 148 and h.link.sent[1][1:33] == RKEY
    h.login()
    h.reply("> ridge")
    h.link.sent.clear()
    h.run_to_login(job("name", "get", job_id=2))
    assert companion.CMD_GET_CONTACT_BY_KEY not in h.link.ops() and companion.CMD_ADD_UPDATE_CONTACT not in h.link.ops()
    h.ex.new_connection()  # the companion may have rebooted
    h.finished.clear()
    h.ex.abort()
    h.link.sent.clear()
    h.run_to_login(job("name", "get", job_id=3))
    assert h.link.ops()[0] == companion.CMD_GET_CONTACT_BY_KEY


def test_a_logged_in_repeater_is_not_logged_in_again_on_the_same_connection():
    h = Harness()
    h.run_to_login(job("name", "get", job_id=1))
    h.login()
    h.reply("> ridge")
    h.link.sent.clear()
    h.run_to_login(job("beacon.window", "get", job_id=2))
    assert h.link.ops() == [companion.CMD_SEND_TXT_MSG]  # straight to the command


def test_a_password_always_logs_in():
    h = Harness()
    h.run_to_login(job("name", "get", job_id=1))
    h.login()
    h.reply("> ridge")
    h.link.sent.clear()
    h.run_to_login(job("name", "get", password="hunter2", job_id=2))
    assert h.link.ops() == [companion.CMD_SEND_LOGIN] and h.link.sent[0][33:] == b"hunter2"


def test_a_guest_login_with_no_password_asks_for_one():
    h = Harness()
    h.run_to_login(job("name", "get"))
    h.login(admin=False, perms=0)
    out = h.outcome
    assert out.code == "needs_password" and out.message == remote.ERROR_TEXT["needs_password"] and not h.link.commands()
    assert RKEY not in h.ex.logged_in


def test_a_guest_login_with_a_password_is_not_admin():
    h = Harness()
    h.run_to_login(job("name", "get", password="guestpw"))
    h.login(admin=False, perms=0)
    assert h.outcome.code == "not_admin" and "permission 0" in h.outcome.message


@pytest.mark.parametrize("admin, perms", [(True, 2), (False, 3), (True, 1)])
def test_only_admin_flag_and_admin_permission_together_count(admin, perms):
    h = Harness()
    h.run_to_login(job("name", "get"))
    h.login(admin=admin, perms=perms)
    assert h.outcome.code == "needs_password" and not h.link.commands()


def test_a_login_from_another_repeater_is_ignored():
    h = Harness()
    h.run_to_login(job("name", "get"))
    h.login(key=OTHER)
    assert h.ex.busy and not h.link.commands()


def test_no_answer_to_the_login_sends_it_again_on_the_same_route():
    h = Harness()
    h.run_to_login(job("name", "get"))
    h.advance(7.0)  # inside the 8 s minimum wait: nothing happens
    assert h.link.ops().count(companion.CMD_SEND_LOGIN) == 1
    h.advance(2.0)  # past it (timeout 2 s x 1.5 = 3 s, raised to the 8 s minimum)
    assert h.link.ops().count(companion.CMD_SEND_LOGIN) == 2 and companion.CMD_RESET_PATH not in h.link.ops()
    h.login()
    h.reply("> ridge")
    out = h.outcome
    assert out.ok and out.result["retries"] == 1 and out.result["route_resets"] == 0


def test_a_second_unanswered_login_resets_the_route_and_a_third_gives_up():
    h = Harness()
    h.run_to_login(job("name", "get"))
    h.advance(9.0)  # try 2, same route
    assert companion.CMD_RESET_PATH not in h.link.ops()
    h.advance(9.0)  # try 3: reset the route, then log in again
    assert h.link.ops()[-2:] == [companion.CMD_RESET_PATH, companion.CMD_SEND_LOGIN]
    assert not h.finished
    h.advance(9.0)
    out = h.outcome
    assert out.code == "no_reply" and h.link.ops().count(companion.CMD_RESET_PATH) == 1 and h.link.ops().count(companion.CMD_SEND_LOGIN) == 3
    assert out.result["retries"] == 2 and out.result["route_resets"] == 1 and not h.ex.busy


def test_the_wait_follows_the_companion_s_estimate_between_the_limits():
    h = Harness()
    h.link.timeout_ms = 10_000  # x1.5 = 15 s
    h.run_to_login(job("name", "get"))
    h.advance(14.0)
    assert h.ex.busy and h.link.ops().count(companion.CMD_SEND_LOGIN) == 1
    h.advance(2.0)
    assert h.link.ops().count(companion.CMD_SEND_LOGIN) == 2
    h2 = Harness()
    h2.link.timeout_ms = 120_000  # capped at 30 s
    h2.run_to_login(job("name", "get"))
    h2.advance(29.0)
    assert h2.link.ops().count(companion.CMD_SEND_LOGIN) == 1
    h2.advance(2.0)
    assert h2.link.ops().count(companion.CMD_SEND_LOGIN) == 2


def test_a_lost_reply_is_sent_again_on_the_same_route_without_a_new_login():
    h = Harness()
    h.run_to_login(job("name", "get"))
    h.login()
    first = h.link.commands()[0]
    h.advance(9.0)  # no reply
    ops = h.link.ops()
    assert companion.CMD_RESET_PATH not in ops and ops.count(companion.CMD_SEND_LOGIN) == 1 and len(h.link.commands()) == 2
    second = h.link.commands()[1]
    assert second.split("|", 1)[1] == first.split("|", 1)[1] and second.split("|")[0] != first.split("|")[0]  # same command, new tag
    h.reply("> ridge")
    assert h.outcome.ok and h.outcome.result["round_trips"] == 1 and h.outcome.result["retries"] == 1


def test_two_lost_replies_reset_the_route_log_in_again_and_send_a_last_time():
    h = Harness()
    h.run_to_login(job("name", "get"))
    h.login()
    h.advance(9.0)  # try 2
    h.advance(9.0)  # try 3: reset, log in
    assert h.link.ops()[-2:] == [companion.CMD_RESET_PATH, companion.CMD_SEND_LOGIN] and RKEY not in h.ex.logged_in
    assert len(h.link.commands()) == 2
    h.login()
    assert len(h.link.commands()) == 3  # the last try
    h.reply("> ridge")
    out = h.outcome
    assert out.ok and out.result["route_resets"] == 1 and out.result["retries"] == 2


def test_the_last_try_failing_ends_the_job_without_another_login():
    h = Harness()
    h.run_to_login(job("name", "get"))
    h.login()
    h.advance(9.0)
    h.advance(9.0)
    h.login()
    h.advance(9.0)
    assert h.outcome.code == "no_reply" and h.link.ops().count(companion.CMD_SEND_LOGIN) == 2 and len(h.link.commands()) == 3


def test_a_late_reply_to_an_earlier_try_still_answers_the_step():
    h = Harness()
    h.run_to_login(job("name", "get"))
    h.login()
    first_tag = companion.split_tag(h.link.commands()[0])[0]
    h.advance(9.0)  # the command went out again
    assert len(h.link.commands()) == 2
    assert h.reply("> ridge", tag=first_tag)  # the first try's answer, after all
    assert h.outcome.result["values"] == {"name": "ridge"}
    second_tag = companion.split_tag(h.link.commands()[1])[0]
    assert not h.reply("> ridge", tag=second_tag)  # the second try's answer, with no job any more: left for the session


def test_a_reply_to_an_earlier_step_is_ignored_for_the_next_one():
    h = Harness()
    h.run_to_login(job("beacon.window", "set", {"seconds": 5}))
    h.login()
    old_tag = companion.split_tag(h.link.commands()[0])[0]
    h.reply("OK")  # the first step is answered; the read-back is now in flight
    assert len(h.link.commands()) == 2
    assert h.reply("OK", tag=old_tag)  # a duplicate of the first answer arrives
    assert h.ex.stats["stale_replies"] == 1 and len(h.link.commands()) == 2 and h.ex.busy
    h.reply("> 5 secs")
    h.reply("> 0")
    assert h.outcome.ok


def test_a_late_login_answer_during_the_wait_to_resend_is_taken():
    h = Harness()
    h.run_to_login(job("name", "get"))
    h.clock.t += 9.0
    h.poll()  # timed out; the resend waits for the next poll
    h.login()  # the first login's answer turns up now
    assert h.link.ops().count(companion.CMD_SEND_LOGIN) == 1 and h.link.commands()  # logged in, no second login, the command is out
    h.reply("> ridge")
    assert h.outcome.ok


def test_a_reply_with_no_tag_is_taken_for_a_repeater_that_does_not_reflect_it():
    h = Harness()
    h.run_to_login(job("name", "get"))
    h.login()
    h.reply("> ridge", tag="")
    assert h.outcome.ok and h.ex.stats["untagged_replies"] == 1


def test_only_cli_replies_from_the_job_s_repeater_are_taken_from_the_session():
    h = Harness()
    h.run_to_login(job("name", "get"))
    assert h.reply("> early", tag="00")  # a CLI reply from the repeater, though none is expected yet: taken and counted as late
    assert h.ex.stats["stale_replies"] == 1 and h.ex.busy
    h.login()
    assert not h.reply("hello", txt_type=companion.TXT_TYPE_PLAIN, tag="")  # a person's message, not a CLI reply
    assert not h.reply("> other", key=OTHER, tag="00")  # another repeater
    assert h.ex.busy


def test_one_job_at_a_time_and_the_queue_is_looked_at_once_a_second():
    h = Harness()
    h.poll()
    h.poll()
    assert h.claims == 1  # the empty queue was looked at once
    h.clock.t += CLAIM_INTERVAL + 0.1
    h.queue += [job("name", "get", job_id=1), job("name", "get", job_id=2)]
    h.poll()
    assert h.claims == 2 and h.ex.job.id == 1
    h.poll()
    assert h.claims == 2  # busy: does not claim
    h.login()
    h.reply("> a")
    assert len(h.finished) == 1 and not h.ex.busy
    h.poll()  # a finished job is followed at once by a look at the queue
    assert h.ex.job.id == 2


def test_a_repeater_whose_key_is_not_known_fails_with_no_key():
    h = Harness()
    h.run_to_login(job("name", "get", pubkey=None))
    assert h.outcome.code == "no_key" and not h.link.sent


def test_an_untrusted_clock_fails_the_job_before_anything_is_sent():
    h = Harness(clock_ready=lambda link: "clock_untrusted")
    h.run_to_login(job("name", "get"))
    assert h.outcome.code == "clock_untrusted" and not h.link.sent


def test_tampered_parameters_are_refused_again_at_the_executor():
    h = Harness()
    bad = job("name", "get")
    object.__setattr__(bad, "kind", "reboot")  # what someone with write access to the database could try
    h.run_to_login(bad)
    assert h.outcome.code == "bad_value" and not h.link.sent
    h2 = Harness()
    evil = RemoteJob(2, RKEY[:8], RKEY, "x", 0.0, 0.0, "beacon.channel", "set", {"hex": "00" * 16}, None, 0.0)
    h2.run_to_login(evil)
    assert h2.outcome.code == "bad_value" and not h2.link.sent


def test_a_password_that_is_too_long_is_refused_by_the_executor_too():
    h = Harness()
    h.run_to_login(job("name", "get", password="x" * 16))
    assert h.outcome.code == "bad_value" and not h.link.sent


def test_the_companion_refusing_the_contact_or_the_send_is_reported():
    h = Harness(contact_known=False)
    h.link.errors[companion.CMD_ADD_UPDATE_CONTACT] = CommandError(3)
    h.run_to_login(job("name", "get"))
    assert h.outcome.code == "contact_failed" and "table full" in h.outcome.message
    h = Harness()
    h.link.errors[companion.CMD_SEND_LOGIN] = CommandError(3)
    h.run_to_login(job("name", "get"))
    assert h.outcome.code == "companion_refused"
    h = Harness()
    h.run_to_login(job("name", "get"))
    h.link.errors[companion.CMD_SEND_TXT_MSG] = CommandError(1)
    h.login()
    assert h.outcome.code == "companion_refused"


def test_losing_the_link_fails_the_job_as_interrupted_and_forgets_the_connection():
    h = Harness()
    h.run_to_login(job("name", "get"))
    h.login()
    h.ex.abort()
    out = h.outcome
    assert out.code == "interrupted" and out.result["replies"] == [] and not h.ex.busy
    assert not h.ex.logged_in and not h.ex.contacts
    h.ex.abort()  # nothing running: harmless
    assert len(h.finished) == 1


def test_a_job_has_a_ceiling_whatever_step_it_is_on():
    h = Harness()
    h.link.timeout_ms = 120_000
    h.run_to_login(job("name", "get"))
    h.clock.t += 101.0
    h.poll()
    assert h.outcome.code == "no_reply" and "100 s" in h.outcome.message


def test_get_all_carries_on_past_an_unsupported_item():
    h = Harness()
    h.run_to_login(job("all", "get"))
    h.login()
    answers = ["> ridge", "> 47.5", "> -122.25", "> 60", "> 48", "Unknown command", "Unknown command", "Unknown command", "Unknown command"]
    for a in answers:
        h.reply(a)
    out = h.outcome
    assert out.ok and out.result["values"]["name"] == "ridge" and out.result["values"]["location"] == {"lat": 47.5, "lon": -122.25}
    assert set(out.result["errors"]) == {"beacon.window", "beacon.names", "beacon.name_refresh", "beacon.channel"}


def test_the_channel_reply_is_compared_with_the_base_s_hash():
    h = Harness()
    h.run_to_login(job("beacon.channel", "get"))
    h.login()
    h.reply("> set, hash 42")
    assert h.outcome.result["values"]["beacon.channel"] == {"set": True, "hash": 0x42, "matches_base": True}


# --- pacing ---------------------------------------------------------------------------------------------------------------

PACED = RemoteConfig(min_timeout_s=8.0, max_timeout_s=30.0, timeout_factor=1.5, max_job_s=100.0, step_gap_s=3.0)


def test_the_first_command_waits_out_the_gap_after_the_login_answer():
    h = Harness(cfg=PACED)
    h.run_to_login(job("name", "get"))
    assert h.link.ops()[-1] == companion.CMD_SEND_LOGIN  # nothing was heard yet, so the login goes out at once
    h.login()
    assert not h.link.commands() and h.ex.describe().endswith("(pacing)")
    h.advance(2.9)
    assert not h.link.commands()
    h.advance(0.2)
    assert len(h.link.commands()) == 1 and h.ex.describe().endswith("(reply)")


def test_every_command_waits_the_gap_after_the_reply_before_it():
    h = Harness(cfg=PACED)
    h.run_to_login(job("beacon.window", "set", {"seconds": 5}))
    h.login()
    h.advance(3.1)
    sent_at = []
    for answer in ("OK", "> 5 secs"):
        sent_at.append(h.clock.t)
        h.reply(answer)
        count = len(h.link.commands())
        h.advance(2.9)
        assert len(h.link.commands()) == count, "transmitted inside the gap"
        h.advance(0.2)
        assert len(h.link.commands()) == count + 1
    h.reply("> 0")
    assert h.outcome.ok and len(h.link.commands()) == 3


def test_a_job_that_follows_another_waits_for_the_gap_since_the_last_reply():
    h = Harness(cfg=PACED)
    h.queue += [job("name", "get", job_id=1)]
    h.poll()
    h.login()
    h.advance(3.1)
    h.reply("> ridge")
    assert len(h.finished) == 1
    h.queue.append(job("beacon.window", "get", job_id=2))
    h.link.sent.clear()
    h.poll()  # claimed at once, but the repeater's reply to the last job was just heard
    assert h.ex.busy and not h.link.commands() and not h.link.ops().count(companion.CMD_SEND_TXT_MSG)
    h.advance(3.1)
    assert len(h.link.commands()) == 1


def test_a_timeout_also_starts_the_gap_before_the_resend():
    h = Harness(cfg=PACED)
    h.run_to_login(job("name", "get"))
    h.login()
    h.advance(3.1)
    assert len(h.link.commands()) == 1
    h.advance(8.5)  # past the 8 s wait: the resend is scheduled, not sent
    assert len(h.link.commands()) == 1
    h.advance(3.1)
    assert len(h.link.commands()) == 2


def test_a_late_reply_during_the_gap_extends_it():
    h = Harness(cfg=PACED)
    h.run_to_login(job("beacon.window", "set", {"seconds": 5}))
    h.login()
    h.advance(3.1)
    first_tag = companion.split_tag(h.link.commands()[0])[0]
    h.reply("OK")  # the first step is answered; the next waits 3 s
    h.advance(2.0)
    assert len(h.link.commands()) == 1
    h.reply("OK", tag=first_tag)  # an echo of the same reply comes in: the quiet time restarts
    h.advance(2.0)
    assert len(h.link.commands()) == 1
    h.advance(1.2)
    assert len(h.link.commands()) == 2


def test_the_reply_deadline_runs_from_the_transmission_not_from_the_gap():
    h = Harness(cfg=PACED)
    h.run_to_login(job("name", "get"))
    h.login()
    h.advance(3.1)  # the command goes out here
    h.advance(7.0)  # 7 s after it: inside the 8 s wait
    assert len(h.link.commands()) == 1 and h.ex.describe().endswith("(reply)")


def test_with_no_gap_the_next_command_follows_the_reply_at_once():
    h = Harness()
    h.run_to_login(job("beacon.window", "set", {"seconds": 5}))
    h.login()
    h.reply("OK")
    assert len(h.link.commands()) == 2
