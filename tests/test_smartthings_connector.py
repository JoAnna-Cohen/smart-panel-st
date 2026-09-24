"""Tests for the SmartThings Schema Connector (app.py / smartpanel_smartthings).

These don't need Home Assistant; run with:
    pytest tests/test_smartthings_connector.py
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REDIRECT = "https://c2c-us.smartthings.com/oauth/callback"


def _snapshot(on=True, can_remote_on=True, online=True):
    return {
        "fetched_at": 9e12,  # far future: always fresh
        "devices": {
            "panel-P1": {
                "kind": "panel", "panel_id": "P1", "name": "Main", "model": "LWHEM",
                "firmware": "2.1.0", "online": online, "power": 1500.0, "voltage": 240.2,
                "over_voltage": False, "under_voltage": False,
            },
            "breaker-B1": {
                "kind": "breaker", "breaker_id": "B1_raw", "panel_id": "P1", "panel_name": "Main",
                "name": "Kitchen", "position": 1, "poles": 1, "rating": 20, "model": "LB120",
                "firmware": "1.0", "online": online, "on": on, "can_remote_on": can_remote_on,
                "power": 300.0, "current": 2.5, "voltage": 120.1, "energy": None,
            },
            "ct-P1-7": {
                "kind": "ct", "panel_id": "P1", "name": "Main Grid", "model": "CT Clamp",
                "firmware": "2.1.0", "online": online, "power": 1500.0, "current": 12.5,
            },
        },
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test")
    monkeypatch.setenv("ST_CLIENT_ID", "cid")
    monkeypatch.setenv("ST_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ST_PROFILE_PANEL", "profile-panel")
    monkeypatch.setenv("ST_PROFILE_CT", "profile-ct")
    # Re-import so module-level config picks up this test's environment.
    import smartpanel_smartthings
    for mod in ["runtime", "connector"]:
        sys.modules.pop(f"smartpanel_smartthings.{mod}", None)
        smartpanel_smartthings.__dict__.pop(mod, None)
    sys.modules.pop("app", None)
    app_module = importlib.import_module("app")
    app_module.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    return app_module


def _link(env, allow_control=False, code_2fa=False):
    """Run the OAuth flow; returns the token response."""
    client = env.app.test_client()
    r = client.get("/oauth/authorize", query_string={"redirect_uri": REDIRECT, "state": "xyz"})
    assert r.status_code == 200

    form = {"email": "a@b.com", "password": "pw"}
    if allow_control:
        form["allow_control"] = "on"

    if code_2fa:
        with patch("smartpanel_smartthings.leviton.login") as login:
            login.side_effect = [env.leviton.TwoFactorRequired(), {"token": "lt", "userid": "u1"}]
            r = client.post("/oauth/authorize", data=form)
            assert b"verification code" in r.data
            r = client.post("/oauth/authorize", data={"step": "code", "code": "123456"})
            assert login.call_args.args == ("a@b.com", "pw", "123456")
    else:
        with patch("smartpanel_smartthings.leviton.login", return_value={"token": "lt", "userid": "u1"}):
            r = client.post("/oauth/authorize", data=form)

    assert r.status_code == 302
    loc = urlparse(r.headers["Location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == REDIRECT
    qs = parse_qs(loc.query)
    assert qs["state"] == ["xyz"]

    r = client.post(
        "/oauth/token",
        data={"grant_type": "authorization_code", "code": qs["code"][0]},
        headers={"Authorization": "Basic Y2lkOmNzZWNyZXQ="},  # cid:csecret
    )
    assert r.status_code == 200, r.data
    return r.get_json()


def _webhook(env, token, interaction, **extra):
    body = {
        "headers": {"schema": "st-schema", "version": "1.0", "interactionType": interaction, "requestId": "r1"},
        "authentication": {"tokenType": "Bearer", "token": token},
        **extra,
    }
    return env.app.test_client().post("/webhook", json=body).get_json()


def test_rejects_foreign_redirect(env):
    r = env.app.test_client().get("/oauth/authorize", query_string={"redirect_uri": "https://evil.example/cb"})
    assert r.status_code == 400


def test_token_endpoint_requires_client_secret(env):
    r = env.app.test_client().post(
        "/oauth/token", data={"grant_type": "authorization_code", "code": "x"},
        headers={"Authorization": "Basic Y2lkOndyb25n"},  # cid:wrong
    )
    assert r.status_code == 401


def test_credentials_encrypted_at_rest(env, tmp_path):
    _link(env)
    raw = (tmp_path / "links.json").read_text()
    assert "pw" not in raw and "a@b.com" not in raw and '"lt"' not in raw


def test_2fa_flow_and_discovery(env):
    tokens = _link(env, code_2fa=True)
    with patch("smartpanel_smartthings.leviton.fetch_snapshot") as fetch:
        fetch.return_value = (_snapshot(), {"email": "a@b.com", "password": "pw", "token": "lt2", "userid": "u1"})
        resp = _webhook(env, tokens["access_token"], "discoveryRequest")
    assert resp["headers"]["interactionType"] == "discoveryResponse"
    devices = {d["externalDeviceId"]: d for d in resp["devices"]}
    assert set(devices) == {"panel-P1", "breaker-B1", "ct-P1-7"}
    assert devices["breaker-B1"]["deviceHandlerType"] == "c2c-switch-power-energy"
    assert devices["panel-P1"]["deviceUniqueId"] == "profile-panel"
    assert devices["ct-P1-7"]["deviceUniqueId"] == "profile-ct"
    # refreshed Leviton token was persisted
    link_id, link, _ = env.auth_manager.link_for_token(tokens["access_token"])
    assert env.auth_manager.get_creds(link)["token"] == "lt2"


def test_state_refresh_uses_cache(env):
    tokens = _link(env)
    link_id, _, _ = env.auth_manager.link_for_token(tokens["access_token"])
    env.auth_manager.save_snapshot(link_id, _snapshot())
    with patch("smartpanel_smartthings.leviton.fetch_snapshot") as fetch:
        resp = _webhook(env, tokens["access_token"], "stateRefreshRequest",
                        devices=[{"externalDeviceId": "breaker-B1"}, {"externalDeviceId": "gone"}])
        fetch.assert_not_called()
    by_id = {d["externalDeviceId"]: d for d in resp["deviceState"]}
    states = {s["capability"]: s["value"] for s in by_id["breaker-B1"]["states"]}
    assert states == {
        "st.healthCheck": "online", "st.switch": "on", "st.powerMeter": 300.0,
        "st.voltageMeasurement": 120.1, "st.currentMeasurement": 2.5,
    }
    assert by_id["gone"]["deviceError"][0]["errorEnum"] == "DEVICE-DELETED"


def _command(env, token, command):
    return _webhook(env, token, "commandRequest", devices=[{
        "externalDeviceId": "breaker-B1",
        "commands": [{"component": "main", "capability": "st.switch", "command": command, "arguments": []}],
    }])["deviceState"][0]


def test_command_blocked_without_opt_in(env):
    tokens = _link(env, allow_control=False)
    link_id, _, _ = env.auth_manager.link_for_token(tokens["access_token"])
    env.auth_manager.save_snapshot(link_id, _snapshot())
    with patch("smartpanel_smartthings.leviton.set_breaker") as sb:
        result = _command(env, tokens["access_token"], "off")
        sb.assert_not_called()
    assert result["deviceError"][0]["errorEnum"] == "CAPABILITY-NOT-SUPPORTED"
    assert {"component": "main", "capability": "st.switch", "attribute": "switch", "value": "on"} in result["states"]


def test_command_with_opt_in(env):
    tokens = _link(env, allow_control=True)
    link_id, _, _ = env.auth_manager.link_for_token(tokens["access_token"])
    env.auth_manager.save_snapshot(link_id, _snapshot())
    with patch("smartpanel_smartthings.leviton.set_breaker", return_value=(True, {"token": "lt"})) as sb:
        result = _command(env, tokens["access_token"], "off")
    assert sb.call_args.args[1:] == ("B1_raw", False)
    assert "deviceError" not in result
    assert env.auth_manager.get_snapshot(link_id)["devices"]["breaker-B1"]["on"] is False


def test_gen1_breaker_cannot_turn_on(env):
    tokens = _link(env, allow_control=True)
    link_id, _, _ = env.auth_manager.link_for_token(tokens["access_token"])
    env.auth_manager.save_snapshot(link_id, _snapshot(on=False, can_remote_on=False))
    with patch("smartpanel_smartthings.leviton.set_breaker") as sb:
        result = _command(env, tokens["access_token"], "on")
        sb.assert_not_called()
    assert "reset it at the panel" in result["deviceError"][0]["detail"]


def test_leviton_auth_failure_forces_relink(env):
    tokens = _link(env)
    with patch("smartpanel_smartthings.leviton.fetch_snapshot", side_effect=env.leviton.LDATAAuthError("x")):
        resp = _webhook(env, tokens["access_token"], "discoveryRequest")
    assert resp["globalError"]["errorEnum"] == "INVALID-TOKEN"
    r = env.app.test_client().post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        headers={"Authorization": "Basic Y2lkOmNzZWNyZXQ="},
    )
    assert r.status_code == 400


def test_refresh_keeps_link_and_integration_deleted_revokes(env):
    tokens = _link(env)
    link_id, _, _ = env.auth_manager.link_for_token(tokens["access_token"])
    env.auth_manager.set_callback(link_id, {"access_token": "cbt"})
    r = env.app.test_client().post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        headers={"Authorization": "Basic Y2lkOmNzZWNyZXQ="},
    )
    new = r.get_json()
    new_link_id, link, _ = env.auth_manager.link_for_token(new["access_token"])
    assert new_link_id == link_id and link["callback"] == {"access_token": "cbt"}

    resp = _webhook(env, new["access_token"], "integrationDeleted")
    assert resp["headers"]["interactionType"] == "integrationDeletedResponse"
    assert env.auth_manager.all_links() == {}
    assert env.auth_manager._refresh_tokens.all() == {}
    assert env.auth_manager._access_tokens.all() == {}


def test_store_delete_does_not_clobber_other_process(tmp_path):
    from smartpanel_smartthings.storage import TokenStore
    a = TokenStore(str(tmp_path / "s.json"))
    b = TokenStore(str(tmp_path / "s.json"))  # e.g. another gunicorn worker
    a.set("x", 1)
    b.set("y", 2)
    a.delete("x")
    assert b.all() == {"y": 2}


def test_fetch_snapshot_maps_ldata_status():
    from smartpanel_smartthings import leviton

    status = {
        "panels": [{"id": "P1", "name": "Main", "model": "LWHEM", "firmware": "2.1", "connected": True,
                    "voltage": 240.0, "overVoltage": False, "underVoltage": False}],
        "P1totalPower": 812.34,
        "breakers": {
            "B1_A65E": {"id": "B1_A65E", "stable_id": "B1", "panel_id": "P1", "name": "", "position": 3,
                        "poles": 1, "rating": 15, "model": "LB115", "firmware": "1.2",
                        "state": "ManualON", "remoteState": "RemoteON", "canRemoteOn": False,
                        "power": 100.04, "current": 0.834, "voltage": 120.0, "consumption": 5.5},
        },
        "cts": {"7": {"id": "7", "panel_id": "P1", "name": "Grid", "power": 812.3, "current": 3.4}},
    }

    class FakeService:
        auth_token = "new-token"
        userid = "u1"
        _panel_has_hw_counters = {"P1": False}

        def status(self):
            return status

    with patch.object(leviton, "make_service", return_value=FakeService()):
        snap, creds = leviton.fetch_snapshot({"email": "e", "password": "p", "token": "old", "userid": "u1"})

    assert creds["token"] == "new-token"
    d = snap["devices"]
    assert d["panel-P1"]["power"] == 812.3
    b = d["breaker-B1"]
    assert (b["breaker_id"], b["name"], b["on"], b["can_remote_on"], b["energy"]) == (
        "B1_A65E", "Breaker 3", True, False, None)
    assert d["ct-P1-7"]["name"] == "Main Grid"


def test_ldata_service_loads_without_home_assistant():
    from smartpanel_smartthings import leviton
    assert "homeassistant" not in sys.modules
    assert hasattr(leviton.LDATAService, "status")


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = b"x"
        self.text = str(body)

    def json(self):
        return self._body


def test_callback_grant_and_worker_push(env, monkeypatch):
    monkeypatch.setattr(env.connector, "_cb_id", "st-cb-id")
    monkeypatch.setattr(env.connector, "_cb_secret", "st-cb-secret")
    tokens = _link(env)
    urls = {"oauthToken": "https://c2c-us.smartthings.com/oauth/token",
            "stateCallback": "https://c2c-us.smartthings.com/device/events"}

    posts = []

    def fake_post(url, json, timeout):
        posts.append((url, json))
        if url == urls["oauthToken"]:
            return _Resp(200, {"callbackAuthentication": {
                "accessToken": "cb-at", "refreshToken": "cb-rt", "expiresIn": 86400}})
        return _Resp(200, {})

    with patch("smartpanel_smartthings.callbacks.requests.post", side_effect=fake_post):
        resp = _webhook(env, tokens["access_token"], "grantCallbackAccess",
                        callbackAuthentication={"grantType": "authorization_code", "code": "one-time",
                                                "clientId": "st-cb-id"},
                        callbackUrls=urls)
        assert resp["headers"]["interactionType"] == "grantCallbackAccessResponse"
        grant = posts[0][1]["callbackAuthentication"]
        assert (grant["code"], grant["clientId"], grant["clientSecret"]) == ("one-time", "st-cb-id", "st-cb-secret")

        import worker
        with patch("smartpanel_smartthings.leviton.fetch_snapshot",
                   return_value=(_snapshot(), {"token": "lt", "userid": "u1"})):
            worker.push_all(env.auth_manager, env.connector)

    url, body = posts[-1]
    assert url == urls["stateCallback"]
    assert body["headers"]["interactionType"] == "stateCallback"
    assert body["authentication"]["token"] == "cb-at"
    assert {d["externalDeviceId"] for d in body["deviceState"]} == {"panel-P1", "breaker-B1", "ct-P1-7"}
