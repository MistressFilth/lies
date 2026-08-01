from unittest import mock

import pytest

from lies.etl.normalize.pandoc_daemon import PandocDaemon


def test_daemon_starts_on_first_convert(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []
    fake_proc = mock.Mock()
    fake_proc.stdin = mock.Mock()
    fake_proc.stdout = mock.Mock()
    fake_proc.stdout.read.return_value = b"# ok"
    fake_proc.poll.return_value = None
    def fake_popen(*a, **kw):
        started.append(True)
        return fake_proc
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    d = PandocDaemon()
    out = d.convert(b"<h1>x</h1>", "html")
    assert out == b"# ok"
    assert started == [True]
    fake_proc.stdin.write.assert_called_once()
    fake_proc.stdin.close.assert_called_once()


def test_daemon_restarts_on_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    crash = mock.Mock()
    crash.poll.return_value = 1
    crash.stdin = mock.Mock()
    crash.stdout = mock.Mock()
    crash.stdout.read.return_value = b""

    ok = mock.Mock()
    ok.poll.return_value = None
    ok.stdin = mock.Mock()
    ok.stdout = mock.Mock()
    ok.stdout.read.return_value = b"good"

    popen_calls: list[mock.Mock] = []
    def fake_popen(*a, **kw):
        proc = crash if not popen_calls else ok
        popen_calls.append(proc)
        return proc
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    d = PandocDaemon()
    d.convert(b"x", "html")
    assert len(popen_calls) == 2


def test_daemon_shutdown_terminates(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_proc = mock.Mock()
    fake_proc.poll.return_value = None
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: fake_proc)
    d = PandocDaemon()
    d._proc = fake_proc
    d.shutdown()
    fake_proc.terminate.assert_called_once()
