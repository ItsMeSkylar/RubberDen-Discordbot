"""Integration tests for the FastAPI HTTP endpoints in main.py.

conftest.py sets the required env vars before this module is imported.
The shutil.which patch below prevents the ffmpeg SystemExit from firing
when main.py is first imported.
"""

import sys
from concurrent.futures import Future as ConcurrentFuture
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Patch the ffmpeg startup check so importing main.py doesn't raise SystemExit
# in environments that don't have ffmpeg in PATH.
if "main" not in sys.modules:
    with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
        import main
else:
    import main  # already imported (e.g. running full test suite)

import scripts.DiscordScripts as DS

_TOKEN = "test-secret"
_AUTH = {"X-Internal-Token": _TOKEN}


def _done_future(result=None) -> ConcurrentFuture:
    """Return an already-resolved concurrent.futures.Future."""
    fut: ConcurrentFuture = ConcurrentFuture()
    fut.set_result(result)
    return fut


def _error_future(exc: Exception) -> ConcurrentFuture:
    """Return a concurrent.futures.Future pre-loaded with an exception."""
    fut: ConcurrentFuture = ConcurrentFuture()
    fut.set_exception(exc)
    return fut


def _rtcs_ok(coro, _loop):
    """Side-effect for patching asyncio.run_coroutine_threadsafe to succeed.

    Closes the coroutine immediately to prevent 'coroutine was never awaited'
    warnings, then returns an already-resolved future.
    """
    coro.close()
    return _done_future()


def _rtcs_error(exc: Exception):
    def _side_effect(coro, _loop):
        coro.close()
        return _error_future(exc)
    return _side_effect


@pytest.fixture(autouse=True)
def isolate_bot_loop():
    """Save and restore BOT_LOOP around each test for isolation."""
    saved = DS.BOT_LOOP
    yield
    DS._set_bot_loop(saved)


@pytest.fixture()
def http():
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c


# ─────────────────────────────────────────────────────
# Auth — shared across all three endpoints
# ─────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/post-schedule", "/notify-session-expired", "/notify-failure"])
def test_missing_token_returns_401(http, path):
    resp = http.post(path, json={})
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["/post-schedule", "/notify-session-expired", "/notify-failure"])
def test_wrong_token_returns_401(http, path):
    resp = http.post(path, json={}, headers={"X-Internal-Token": "wrong"})
    assert resp.status_code == 401


# ─────────────────────────────────────────────────────
# 503 when bot not ready
# ─────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/post-schedule", "/notify-session-expired", "/notify-failure"])
def test_bot_not_ready_returns_503(http, path):
    DS._set_bot_loop(None)
    resp = http.post(path, json={}, headers=_AUTH)
    assert resp.status_code == 503
    assert resp.json() == {"ok": False, "error": "bot not ready yet"}


# ─────────────────────────────────────────────────────
# Success paths
# ─────────────────────────────────────────────────────

def test_post_schedule_success(http):
    DS._set_bot_loop(MagicMock())
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_rtcs_ok):
        resp = http.post("/post-schedule", json={"files": [], "header": "hi"}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_notify_session_expired_success(http):
    DS._set_bot_loop(MagicMock())
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_rtcs_ok):
        resp = http.post("/notify-session-expired", json={"site": "mysite"}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_notify_failure_success(http):
    DS._set_bot_loop(MagicMock())
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_rtcs_ok):
        resp = http.post(
            "/notify-failure",
            json={"error": "oops", "site": "x", "entry_id": "1"},
            headers=_AUTH,
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ─────────────────────────────────────────────────────
# Error propagation
# ─────────────────────────────────────────────────────

def test_post_schedule_internal_error_returns_ok_false(http):
    DS._set_bot_loop(MagicMock())
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_rtcs_error(RuntimeError("boom"))):
        resp = http.post("/post-schedule", json={"files": []}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_notify_failure_internal_error_returns_ok_false(http):
    DS._set_bot_loop(MagicMock())
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_rtcs_error(RuntimeError("discord down"))):
        resp = http.post("/notify-failure", json={}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


# ─────────────────────────────────────────────────────
# Payload validation
# ─────────────────────────────────────────────────────

def test_post_schedule_too_many_files_rejected(http):
    DS._set_bot_loop(MagicMock())
    files = [{"filename": f"img{i}.png"} for i in range(11)]  # max is 10
    resp = http.post("/post-schedule", json={"files": files}, headers=_AUTH)
    assert resp.status_code == 422


def test_post_schedule_file_missing_path_rejected(http):
    DS._set_bot_loop(MagicMock())
    resp = http.post("/post-schedule", json={"files": [{"description": "no path"}]}, headers=_AUTH)
    assert resp.status_code == 422
