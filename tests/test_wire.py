import pytest

from beacon_base import wire


def _obs(d):
    return wire.Observation(bytes.fromhex(d["beacon_id"]), d["counter"], d["rssi"], d["snr"], d["batt_mv"])


def test_constants_match_firmware(golden):
    c = golden["constants"]
    assert wire.REPORT_DATA_TYPE == c["data_type"]
    assert wire.REPORT_VERSION == golden["version"]
    assert wire.ID_LEN == c["id_len"]
    assert wire.HEADER_LEN == c["header_len"]
    assert wire.ENTRY_LEN == c["entry_len"]
    assert wire.MAX_ENTRIES == c["max_entries"]
    assert wire.MAX_GROUP_DATA_LENGTH == c["max_group_data_length"]


def test_decodes_golden_reports(golden):
    for case in golden["reports"]:
        report = wire.decode_report(bytes.fromhex(case["hex"]))
        assert report.repeater_id.hex() == golden["repeater_id"], case["name"]
        assert list(report.observations) == [_obs(o) for o in case["observations"]], case["name"]


def test_encodes_golden_reports(golden):
    key = bytes.fromhex(golden["repeater_key"])
    for case in golden["reports"]:
        encoded = wire.encode_report(key, [_obs(o) for o in case["observations"]])
        assert encoded.hex() == case["hex"], case["name"]


def test_decode_only_cases(golden):
    for case in golden["decode_only"]:
        data = bytes.fromhex(case["hex"])
        if case["expect_count"] < 0:
            with pytest.raises(wire.WireError):
                wire.decode_report(data)
        else:
            assert len(wire.decode_report(data).observations) == case["expect_count"], case["name"]


def test_snr_is_quarter_db():
    assert wire.Observation(bytes(8), 1, -90, -21, 3700).snr == -5.25


def test_encode_rejects_bad_input():
    o = wire.Observation(bytes(8), 1, 0, 0, 0)
    with pytest.raises(ValueError):
        wire.encode_report(bytes(32), [])
    with pytest.raises(ValueError):
        wire.encode_report(bytes(32), [o] * (wire.MAX_ENTRIES + 1))
    with pytest.raises(ValueError):
        wire.encode_report(bytes(4), [o])
    with pytest.raises(ValueError):
        wire.encode_report(bytes(32), [wire.Observation(bytes(7), 1, 0, 0, 0)])
