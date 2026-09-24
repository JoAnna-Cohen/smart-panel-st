"""SmartThings callback (proactive state) client.

After linking, SmartThings sends a grantCallbackAccess interaction carrying a
one-time code and callback URLs. We exchange the code for callback tokens
here, then the background worker uses them to push fresh device state to
SmartThings' stateCallback URL without waiting to be asked.

https://developer.smartthings.com/docs/devices/cloud-connected/st-schema#callback-interactions
"""

import logging
import time
import uuid

import requests

_LOGGER = logging.getLogger(__name__)

TIMEOUT = 20


class CallbackAuthError(Exception):
    """SmartThings rejected our callback credentials."""


def _headers(interaction_type: str) -> dict:
    return {
        "schema": "st-schema",
        "version": "1.0",
        "interactionType": interaction_type,
        "requestId": str(uuid.uuid4()),
    }


def _token_request(urls: dict, interaction_type: str, callback_auth: dict) -> dict:
    resp = requests.post(
        urls["oauthToken"],
        json={
            "headers": _headers(interaction_type),
            "callbackAuthentication": callback_auth,
        },
        timeout=TIMEOUT,
    )
    body = resp.json() if resp.content else {}
    auth = body.get("callbackAuthentication") or {}
    if resp.status_code != 200 or not auth.get("accessToken"):
        raise CallbackAuthError(
            f"callback token request failed: HTTP {resp.status_code} {body.get('globalError')}"
        )
    return {
        "urls": urls,
        "access_token": auth["accessToken"],
        "refresh_token": auth.get("refreshToken"),
        "expires_at": time.time() + int(auth.get("expiresIn", 86400)),
    }


def exchange_code(grant: dict, urls: dict, client_id: str, client_secret: str) -> dict:
    """Trade the grantCallbackAccess code for callback tokens."""
    return _token_request(
        urls,
        "accessTokenRequest",
        {
            "grantType": "authorization_code",
            "code": grant.get("code"),
            "clientId": client_id or grant.get("clientId"),
            "clientSecret": client_secret,
        },
    )


def refresh(cb: dict, client_id: str, client_secret: str) -> dict:
    if not cb.get("refresh_token"):
        raise CallbackAuthError("no callback refresh token")
    # Refreshing is its own interaction type, not another accessTokenRequest.
    return _token_request(
        cb["urls"],
        "refreshAccessTokens",
        {
            "grantType": "refresh_token",
            "refreshToken": cb["refresh_token"],
            "clientId": client_id,
            "clientSecret": client_secret,
        },
    )


def push_state(cb: dict, device_state: list, client_id: str, client_secret: str) -> dict:
    """POST device states to SmartThings. Returns cb (refreshed if needed)."""
    if time.time() > cb.get("expires_at", 0) - 300:
        cb = refresh(cb, client_id, client_secret)

    def _post(token):
        return requests.post(
            cb["urls"]["stateCallback"],
            json={
                "headers": _headers("stateCallback"),
                "authentication": {"tokenType": "Bearer", "token": token},
                "deviceState": device_state,
            },
            timeout=TIMEOUT,
        )

    resp = _post(cb["access_token"])
    if resp.status_code == 401:
        cb = refresh(cb, client_id, client_secret)
        resp = _post(cb["access_token"])
    if resp.status_code >= 400:
        _LOGGER.warning("stateCallback returned HTTP %s: %s", resp.status_code, resp.text[:300])
        if resp.status_code in (401, 403):
            raise CallbackAuthError(f"stateCallback HTTP {resp.status_code}")
    return cb
