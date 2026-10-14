"""The beacon-web entry point: arguments, warnings, and failing with a message rather than a traceback."""

import logging

import pytest

pytest.importorskip("uvicorn")
import uvicorn  # noqa: E402

from beacon_base import web  # noqa: E402


@pytest.fixture
def config(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[web]\nport = 9001\n')
    return str(p)


@pytest.fixture
def served(monkeypatch):
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    return calls


def test_defaults_to_loopback_and_the_configured_port(config, served, caplog):
    caplog.set_level(logging.INFO)
    assert web.main(["-c", config]) == 0
    assert (served[0]["host"], served[0]["port"], served[0]["server_header"]) == ("127.0.0.1", 9001, False)
    assert "read-only" in caplog.text and "can reach" not in caplog.text


def test_arguments_override_the_config_and_a_lan_address_is_warned_about(config, served, caplog):
    caplog.set_level(logging.INFO)
    assert web.main(["-c", config, "--host", "0.0.0.0", "--port", "8123"]) == 0
    assert (served[0]["host"], served[0]["port"]) == ("0.0.0.0", 8123)
    assert "anyone on the network can read the map" in caplog.text


def test_the_warning_for_a_lan_address_mentions_the_token_when_there_is_one(config, served, caplog, monkeypatch):
    monkeypatch.setenv("BEACON_WEB_TOKEN", "a-token-of-sufficient-length")
    web.main(["-c", config, "--host", "192.168.1.5"])
    assert "operator token travels in clear" in caplog.text


def test_a_taken_port_is_a_message_not_a_traceback(config, monkeypatch, capsys):
    def busy(app, **kw):
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(uvicorn, "run", busy)
    assert web.main(["-c", config]) == 2
    assert "cannot listen on 127.0.0.1:9001" in capsys.readouterr().err


def test_a_bad_config_is_a_message(tmp_path, served, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("[web]\nport = 0\n")
    assert web.main(["-c", str(bad)]) == 2
    assert "web.port" in capsys.readouterr().err and served == []


def test_without_the_web_extra_it_says_what_to_install(config, monkeypatch, capsys):
    import builtins

    real = builtins.__import__

    def refuse(name, *a, **kw):
        if name == "uvicorn":
            raise ModuleNotFoundError("No module named 'uvicorn'", name="uvicorn")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", refuse)
    assert web.main(["-c", config]) == 2
    assert "pip install 'beacon-base[web]'" in capsys.readouterr().err


def test_check_reads_the_configuration_and_opens_nothing(config, served, tmp_path, capsys):
    assert web.main(["-c", config, "--check"]) == 0
    assert "configuration ok" in capsys.readouterr().out
    assert served == [] and not (tmp_path / "beacon.db").exists()  # no server, and no database created as a side effect
    bad = tmp_path / "bad.toml"
    bad.write_text("[web]\nport = 0\n")
    assert web.main(["-c", str(bad), "--check"]) == 2
