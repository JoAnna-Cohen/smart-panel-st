"""OAuth 2.0 authorization server + account links for the Schema Connector.

Each time someone links a Leviton account from the SmartThings app we create
a *link* (stable id) holding their encrypted Leviton credentials, whether
breaker control is allowed, and the SmartThings callback credentials.
Access / refresh tokens issued to SmartThings point at the link id, so
rotating tokens never orphans the callback credentials or cached data.

Files in DATA_DIR (all chmod 600):
  links.json            link_id -> {creds (encrypted), allow_control, ...}
  snapshots.json        link_id -> last fetched device snapshot
  energy.json           link_id -> running Wh totals per device (energy.py)
  auth_codes.json       code -> {link_id, created_at}
  access_tokens.json    token -> {link_id, created_at, expires_in}
  refresh_tokens.json   token -> {link_id, created_at}
  pending_logins.json   id -> encrypted email/password awaiting a 2FA code
"""

import logging
import secrets
import time

from .crypto import CredentialCipher
from .storage import TokenStore

_LOGGER = logging.getLogger(__name__)

AUTH_CODE_TTL = 600  # 10 minutes
PENDING_LOGIN_TTL = 600  # 10 minutes to type the 2FA code
ACCESS_TOKEN_TTL = 86400  # 24 hours


def _new_id() -> str:
    return secrets.token_urlsafe(32)


class AuthManager:
    def __init__(self, data_dir: str = ".", cipher: CredentialCipher | None = None):
        self._cipher = cipher or CredentialCipher()
        self._links = TokenStore(f"{data_dir}/links.json")
        self._snapshots = TokenStore(f"{data_dir}/snapshots.json")
        self._energy = TokenStore(f"{data_dir}/energy.json")
        self._auth_codes = TokenStore(f"{data_dir}/auth_codes.json")
        self._access_tokens = TokenStore(f"{data_dir}/access_tokens.json")
        self._refresh_tokens = TokenStore(f"{data_dir}/refresh_tokens.json")
        self._pending = TokenStore(f"{data_dir}/pending_logins.json")

    # ------------------------------------------------------------------
    # 2FA: remember email/password between the two login form steps
    # ------------------------------------------------------------------

    def start_pending_login(self, email: str, password: str) -> str:
        now = time.time()
        self._pending.delete_where(
            lambda _k, v: now - v.get("created_at", 0) > PENDING_LOGIN_TTL
        )
        pending_id = _new_id()
        self._pending.set(
            pending_id,
            {
                "creds": self._cipher.encrypt({"email": email, "password": password}),
                "created_at": now,
            },
        )
        return pending_id

    def get_pending_login(self, pending_id: str) -> dict | None:
        entry = self._pending.get(pending_id) if pending_id else None
        if not entry or time.time() - entry["created_at"] > PENDING_LOGIN_TTL:
            return None
        return self._cipher.decrypt(entry["creds"])

    def drop_pending_login(self, pending_id: str):
        if pending_id:
            self._pending.delete(pending_id)

    # ------------------------------------------------------------------
    # Links
    # ------------------------------------------------------------------

    def create_link(self, creds: dict, allow_control: bool) -> str:
        link_id = _new_id()
        self._links.set(
            link_id,
            {
                "creds": self._cipher.encrypt(creds),
                "allow_control": bool(allow_control),
                "needs_reauth": False,
                "created_at": time.time(),
            },
        )
        _LOGGER.info("Created link %s*** (control=%s)", link_id[:6], allow_control)
        return link_id

    def get_link(self, link_id: str) -> dict | None:
        return self._links.get(link_id)

    def all_links(self) -> dict:
        return self._links.all()

    def get_creds(self, link: dict) -> dict | None:
        return self._cipher.decrypt(link["creds"])

    def save_creds(self, link_id: str, creds: dict):
        """Persist creds if the Leviton token changed (re-login happened)."""

        def _apply(link):
            if link is None:
                return None
            old = self._cipher.decrypt(link["creds"]) or {}
            if old.get("token") != creds.get("token") or old.get("userid") != creds.get("userid"):
                link["creds"] = self._cipher.encrypt(creds)
            return link

        self._links.update(link_id, _apply)

    def mark_needs_reauth(self, link_id: str):
        """Leviton login can't be recovered (password changed / 2FA needed)."""
        _LOGGER.warning("Link %s*** needs re-authentication", link_id[:6])

        def _apply(link):
            if link is not None:
                link["needs_reauth"] = True
            return link

        self._links.update(link_id, _apply)

    def set_callback(self, link_id: str, callback: dict):
        def _apply(link):
            if link is not None:
                link["callback"] = callback
            return link

        self._links.update(link_id, _apply)

    # ------------------------------------------------------------------
    # Snapshot cache
    # ------------------------------------------------------------------

    def get_snapshot(self, link_id: str) -> dict | None:
        return self._snapshots.get(link_id)

    def save_snapshot(self, link_id: str, snapshot: dict):
        self._snapshots.set(link_id, snapshot)

    def update_energy(self, link_id: str, fn):
        """Atomically advance a link's energy totals; fn(old_state) -> new_state."""
        return self._energy.update(link_id, fn, default={})

    # ------------------------------------------------------------------
    # OAuth codes and tokens
    # ------------------------------------------------------------------

    def create_auth_code(self, link_id: str) -> str:
        now = time.time()
        self._auth_codes.delete_where(
            lambda _k, v: now - v.get("created_at", 0) > AUTH_CODE_TTL
        )
        code = _new_id()
        self._auth_codes.set(code, {"link_id": link_id, "created_at": now})
        return code

    def _issue_tokens(self, link_id: str) -> dict:
        access_token = _new_id()
        refresh_token = _new_id()
        now = time.time()
        self._access_tokens.set(
            access_token,
            {"link_id": link_id, "created_at": now, "expires_in": ACCESS_TOKEN_TTL},
        )
        self._refresh_tokens.set(refresh_token, {"link_id": link_id, "created_at": now})
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "scope": "",
        }

    def exchange_code(self, code: str) -> dict | None:
        data = self._auth_codes.get(code) if code else None
        if not data:
            _LOGGER.warning("exchange_code: unknown code")
            return None
        self._auth_codes.delete(code)
        if time.time() - data["created_at"] > AUTH_CODE_TTL:
            _LOGGER.warning("exchange_code: code expired")
            return None
        if not self._links.get(data["link_id"]):
            return None
        return self._issue_tokens(data["link_id"])

    def refresh_access_token(self, refresh_token: str) -> dict | None:
        data = self._refresh_tokens.get(refresh_token) if refresh_token else None
        if not data:
            _LOGGER.warning("refresh_access_token: unknown refresh token")
            return None
        link_id = data["link_id"]
        link = self._links.get(link_id)
        if not link or link.get("needs_reauth"):
            # invalid_grant tells SmartThings the user must link again.
            return None
        self._refresh_tokens.delete(refresh_token)
        # Expired access tokens for this link are no longer needed.
        now = time.time()
        self._access_tokens.delete_where(
            lambda _k, v: v.get("link_id") == link_id
            and now - v["created_at"] > v["expires_in"]
        )
        return self._issue_tokens(link_id)

    def link_for_token(self, access_token: str) -> tuple[str | None, dict | None, str | None]:
        """Return (link_id, link, None), or (None, None, st-schema error enum)."""
        data = self._access_tokens.get(access_token) if access_token else None
        if not data:
            return None, None, "INVALID-TOKEN"
        if time.time() - data["created_at"] > data["expires_in"]:
            return None, None, "TOKEN-EXPIRED"
        link = self._links.get(data["link_id"])
        if not link or link.get("needs_reauth"):
            return None, None, "INVALID-TOKEN"
        return data["link_id"], link, None

    def link_id_for_token(self, access_token: str) -> str | None:
        """Like link_for_token but ignores expiry (used for integrationDeleted)."""
        data = self._access_tokens.get(access_token) if access_token else None
        return data["link_id"] if data else None

    # ------------------------------------------------------------------
    # Revocation
    # ------------------------------------------------------------------

    def revoke_link(self, link_id: str):
        """Remove a link and every token, code and cache entry that points at it."""
        same = lambda _k, v: v.get("link_id") == link_id  # noqa: E731
        self._access_tokens.delete_where(same)
        self._refresh_tokens.delete_where(same)
        self._auth_codes.delete_where(same)
        self._snapshots.delete(link_id)
        self._energy.delete(link_id)
        self._links.delete(link_id)
        _LOGGER.info("Revoked link %s***", link_id[:6])
