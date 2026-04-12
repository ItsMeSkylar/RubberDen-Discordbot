"""Integration tests for the FastAPI HTTP endpoints.

conftest.py sets the required env vars before this module is imported.
The shutil.which patch below prevents the ffmpeg SystemExit from firing
when config.py is first imported.
"""

import asyncio
import sys
from concurrent.futures import Future as ConcurrentFuture
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Patch the ffmpeg startup check so importing config.py doesn't raise SystemExit
# in environments that don't have ffmpeg in PATH.
if "main" not in sys.modules:
    with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
        import main
else:
    import main  # already imported (e.g. running full test suite)

from services import api
import services.discord_scripts as DS

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


def _rtcs_ok():
    """Return a side-effect for patching asyncio.run_coroutine_threadsafe to succeed."""
    def _side_effect(coro, _loop):
        coro.close()
        return _done_future()
    return _side_effect


def _rtcs_error(exc: Exception):
    """Return a side-effect for patching asyncio.run_coroutine_threadsafe to fail."""
    def _side_effect(coro, _loop):
        coro.close()
        return _error_future(exc)
    return _side_effect


@pytest.fixture(autouse=True)
def isolate_bot_state():
    """Save and restore bot globals around each test for isolation."""
    saved_loop = DS.BOT_LOOP
    saved_thread = DS._http_thread
    yield
    DS._set_bot_loop(saved_loop)
    DS._http_thread = saved_thread


@pytest.fixture(autouse=True)
def reset_limiter():
    """Clear rate-limit counters before each test so tests don't interfere."""
    api._limiter.reset()
    yield


@pytest.fixture()
def http():
    with TestClient(api.app, raise_server_exceptions=False) as c:
        yield c


# ─────────────────────────────────────────────────────
# Auth — shared across all three endpoints
# ─────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/post-schedule", "/notify-session-expired", "/notify-failure", "/notify-pending"])
def test_missing_token_returns_401(http, path):
    resp = http.post(path, json={})
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["/post-schedule", "/notify-session-expired", "/notify-failure", "/notify-pending"])
def test_wrong_token_returns_401(http, path):
    resp = http.post(path, json={}, headers={"X-Internal-Token": "wrong"})
    assert resp.status_code == 401


# ─────────────────────────────────────────────────────
# 503 when bot not ready
# ─────────────────────────────────────────────────────

@pytest.mark.parametrize("path,body", [
    ("/post-schedule", {"header": "hi"}),
    ("/notify-session-expired", {}),
    ("/notify-failure", {}),
    ("/notify-pending", {"publish_url": "https://rubberden.com/publish?id=abc"}),
])
def test_bot_not_ready_returns_503(http, path, body):
    DS._set_bot_loop(None)
    resp = http.post(path, json=body, headers=_AUTH)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "bot not ready yet"


# ─────────────────────────────────────────────────────
# Success paths
# ─────────────────────────────────────────────────────

def test_post_schedule_success(http):
    DS._set_bot_loop(MagicMock())
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_rtcs_ok()):
        resp = http.post("/post-schedule", json={"files": [], "header": "hi"}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_notify_session_expired_success(http):
    DS._set_bot_loop(MagicMock())
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_rtcs_ok()):
        resp = http.post("/notify-session-expired", json={"site": "mysite"}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_notify_failure_success(http):
    DS._set_bot_loop(MagicMock())
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_rtcs_ok()):
        resp = http.post(
            "/notify-failure",
            json={"error": "oops", "site": "x", "entry_id": "1"},
            headers=_AUTH,
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_notify_pending_success(http):
    DS._set_bot_loop(MagicMock())
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_rtcs_ok()):
        resp = http.post(
            "/notify-pending",
            json={"site": "patreon", "title": "My Post", "publish_url": "https://rubberden.com/publish?id=abc"},
            headers=_AUTH,
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_notify_pending_reminder_success(http):
    DS._set_bot_loop(MagicMock())
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_rtcs_ok()):
        resp = http.post(
            "/notify-pending",
            json={"site": "twitter", "title": "My Tweet", "publish_url": "https://rubberden.com/publish?id=xyz", "reminder": True},
            headers=_AUTH,
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_notify_pending_failure_alert_success(http):
    DS._set_bot_loop(MagicMock())
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_rtcs_ok()):
        resp = http.post(
            "/notify-pending",
            json={"site": "patreon", "title": "My Post", "publish_url": "https://rubberden.com/publish?id=abc", "failed": True, "error": "Post button not found"},
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
        resp = http.post("/post-schedule", json={"files": [], "header": "hi"}, headers=_AUTH)
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


def test_post_schedule_empty_files_and_no_header_rejected(http):
    resp = http.post("/post-schedule", json={"files": []}, headers=_AUTH)
    assert resp.status_code == 422


# ─────────────────────────────────────────────────────
# Health / ready endpoints  (fix 2)
# ─────────────────────────────────────────────────────

def test_health_always_returns_200(http):
    resp = http.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_ready_returns_503_when_bot_not_ready(http):
    DS._set_bot_loop(None)
    resp = http.get("/ready")
    assert resp.status_code == 503


def test_ready_returns_200_when_bot_ready(http):
    with patch.object(main.client, "is_ready", return_value=True), \
         patch.object(type(main.client), "latency", new_callable=lambda: property(lambda self: 0.1)):
        resp = http.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_ready_returns_503_when_latency_is_nan(http):
    """The math.isnan(latency) guard should trigger 503 even when is_ready() is True."""
    with patch.object(main.client, "is_ready", return_value=True), \
         patch.object(type(main.client), "latency", new_callable=lambda: property(lambda self: float("nan"))):
        resp = http.get("/ready")
    assert resp.status_code == 503


# ─────────────────────────────────────────────────────
# Missing error-path coverage  (fix 3)
# ─────────────────────────────────────────────────────

def test_notify_session_expired_internal_error_returns_ok_false(http):
    DS._set_bot_loop(MagicMock())
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_rtcs_error(RuntimeError("discord down"))):
        resp = http.post("/notify-session-expired", json={}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


# ─────────────────────────────────────────────────────
# Rate limiting  (fix 4)
# ─────────────────────────────────────────────────────

def test_post_schedule_rate_limited_after_10_requests(http):
    DS._set_bot_loop(MagicMock())
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_rtcs_ok()):
        for _ in range(10):
            assert http.post("/post-schedule", json={"files": [], "header": "hi"}, headers=_AUTH).status_code == 200
        resp = http.post("/post-schedule", json={"files": [], "header": "hi"}, headers=_AUTH)
    assert resp.status_code == 429


def test_notify_rate_limited_after_20_requests(http):
    DS._set_bot_loop(MagicMock())
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_rtcs_ok()):
        for _ in range(20):
            assert http.post("/notify-failure", json={}, headers=_AUTH).status_code == 200
        resp = http.post("/notify-failure", json={}, headers=_AUTH)
    assert resp.status_code == 429


# ─────────────────────────────────────────────────────
# Timeout handling  (fix 5)
# ─────────────────────────────────────────────────────

@pytest.mark.parametrize("path,body", [
    ("/post-schedule", {"files": [], "header": "hi"}),
    ("/notify-session-expired", {}),
    ("/notify-failure", {}),
    ("/notify-pending", {"publish_url": "https://rubberden.com/publish?id=abc"}),
])
def test_endpoint_timeout_returns_ok_false(http, path, body):
    DS._set_bot_loop(MagicMock())
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_rtcs_error(asyncio.TimeoutError())):
        resp = http.post(path, json=body, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
