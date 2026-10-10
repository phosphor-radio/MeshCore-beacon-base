import pytest

from beacon_base.link import MAX_FRAME_SIZE, CommandError, CompanionLink, FrameDecoder, LinkError, encode_frame


def frame(payload: bytes) -> bytes:
    return b">" + len(payload).to_bytes(2, "little") + payload


def test_encode_frame():
    assert encode_frame(b"\x16\x03") == b"<\x02\x00\x16\x03"
    with pytest.raises(ValueError):
        encode_frame(b"")
    with pytest.raises(ValueError):
        encode_frame(bytes(MAX_FRAME_SIZE + 1))
    assert len(encode_frame(bytes(MAX_FRAME_SIZE))) == MAX_FRAME_SIZE + 3


def test_decodes_back_to_back_frames():
    d = FrameDecoder()
    assert d.feed(frame(b"\x00") + frame(b"\x05abc")) == [b"\x00", b"\x05abc"]
    assert not d.pending and d.discarded == 0


def test_decodes_one_byte_at_a_time():
    d = FrameDecoder()
    wire = frame(b"\x1b" + bytes(range(40))) + frame(b"\x0a")
    out = []
    for b in wire:
        out += d.feed(bytes([b]))
    assert out == [b"\x1b" + bytes(range(40)), b"\x0a"]


def test_skips_garbage_between_frames():
    d = FrameDecoder()
    assert d.feed(b"boot: ESP32 ready\r\n" + frame(b"\x0a") + b"\xff\xfe" + frame(b"\x83")) == [b"\x0a", b"\x83"]
    assert d.discarded > 0


def test_false_start_marker_does_not_swallow_the_real_frame():
    # a '>' in debug text, followed by bytes that look like a plausible length but an impossible code
    d = FrameDecoder()
    assert d.feed(b"a > b\x04\x00" + b"\x20xyz" + frame(b"\x0a")) == [b"\x0a"]


def test_rejects_zero_and_oversized_lengths():
    d = FrameDecoder()
    assert d.feed(b">\x00\x00\x0a") == []
    assert d.feed(b">" + (MAX_FRAME_SIZE + 1).to_bytes(2, "little") + b"\x0a" + frame(b"\x0a")) == [b"\x0a"]


def test_max_size_frame():
    d = FrameDecoder()
    assert d.feed(frame(b"\x1b" + bytes(MAX_FRAME_SIZE - 1))) == [b"\x1b" + bytes(MAX_FRAME_SIZE - 1)]


def test_partial_frame_waits_then_completes():
    d = FrameDecoder()
    wire = frame(b"\x05hello")
    assert d.feed(wire[:5]) == [] and d.pending
    assert d.feed(wire[5:]) == [b"\x05hello"]


def test_abandon_partial_recovers_following_frame():
    d = FrameDecoder()
    # a torn frame claims 50 bytes, then a complete frame follows inside what it would have swallowed
    d.feed(b">\x32\x00\x05" + frame(b"\x0a"))
    assert d.pending
    assert d.abandon_partial() == [b"\x0a"]
    assert not d.pending


class ScriptedTransport:
    """Replays queued byte chunks, one per read, and records writes."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.written = b""
        self.closed = False

    def read(self, timeout):
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data):
        self.written += data

    def close(self):
        self.closed = True


def test_request_returns_expected_reply_and_routes_pushes():
    pushes = []
    t = ScriptedTransport([frame(b"\x83") + frame(b"\x0a")])
    link = CompanionLink(t)
    link.on_push = pushes.append
    assert link.request(b"\x0a", [10]) == b"\x0a"
    assert pushes == [b"\x83"]
    assert t.written == b"<\x01\x00\x0a"


def test_request_skips_unexpected_replies():
    link = CompanionLink(ScriptedTransport([frame(b"\x05zz") + frame(b"\x0a")]))
    assert link.request(b"\x0a", [10]) == b"\x0a"
    assert link.stray_frames == 1


def test_request_raises_on_error_reply():
    link = CompanionLink(ScriptedTransport([frame(b"\x01\x02")]))
    with pytest.raises(CommandError) as e:
        link.request(b"\x1f\x01", [18])
    assert e.value.code == 2


def test_request_times_out():
    link = CompanionLink(ScriptedTransport([]), command_timeout=0.3)
    with pytest.raises(LinkError):
        link.request(b"\x16\x03", [13])


def test_serial_transport_turns_termios_errors_into_link_errors(monkeypatch):
    """A port that vanishes mid-write raises termios.error from flush(), which is not an OSError."""
    import termios

    from beacon_base.link import SerialTransport

    class Gone:
        def write(self, data):
            return len(data)

        def flush(self):
            raise termios.error(5, "Input/output error")

        def close(self):
            raise termios.error(5, "Input/output error")

    t = SerialTransport.__new__(SerialTransport)
    t._port = Gone()
    t._errors = (OSError, termios.error)
    with pytest.raises(LinkError):
        t.write(b"x")
    t.close()  # closing a dead port must not raise


def test_request_until_collects_a_stream_and_routes_pushes():
    pushes = []
    t = ScriptedTransport([frame(b"\x02\x01\x00\x00\x00") + frame(b"\x83") + frame(b"\x03a") + frame(b"\x03b") + frame(b"\x04\x00\x00\x00\x00")])
    link = CompanionLink(t)
    link.on_push = pushes.append
    assert link.request_until(b"\x04", 4) == [b"\x02\x01\x00\x00\x00", b"\x03a", b"\x03b"]
    assert pushes == [b"\x83"]


def test_request_until_error_and_stall():
    with pytest.raises(CommandError):
        CompanionLink(ScriptedTransport([frame(b"\x01\x04")])).request_until(b"\x04", 4)
    with pytest.raises(LinkError):
        CompanionLink(ScriptedTransport([frame(b"\x02\x01\x00\x00\x00")]), command_timeout=0.3).request_until(b"\x04", 4)
