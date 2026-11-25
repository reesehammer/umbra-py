"""Optional API keys for the hosted ``umbra serve --public`` process.

Anonymous callers keep the per-IP token bucket. A key is a second bucket
with a higher sustained rate and a larger burst, so a short script does not
have to pace itself down to the anonymous cap.

The key file sits beside ``usage-stats.json`` (itself beside ``catalog.db``
on the hosted ``/data`` volume). That is the same directory, and the same
atomic replace, the all-time counter already uses. The file stores a hash
of the secret, the contact email, the optional signup fields, ``created_at``,
whether the key is revoked, and the UTC day it was last used. It does not
store the secret or an address. Email verification is intentionally absent:
there is no mail-provider secret to send one with.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .exceptions import UmbraError

logger = logging.getLogger(__name__)

#: Sustained requests per minute for one valid key. Anonymous traffic stays
#: on :data:`umbra_py.serve.PUBLIC_RATE_LIMIT`.
API_KEY_RATE_LIMIT = 600
#: Tokens the keyed bucket holds. A script that fires a couple of dozen
#: ``/search`` calls back to back spends these, then continues at the
#: sustained rate. Anonymous burst stays :data:`umbra_py.serve.PUBLIC_RATE_BURST`.
API_KEY_BURST = 64
#: Catalog reads one key may hold at once. Higher than the per-IP cap; the
#: process-wide slot pool is unchanged.
API_KEY_CONCURRENCY = 32
API_KEY_WINDOW_S = 60.0

#: Sign-up attempts that create a key, per IP. The window is one hour.
#: Attempts that fail validation do not spend a token; successful creates do.
SIGNUP_RATE_LIMIT = 5
SIGNUP_BURST = 3
SIGNUP_WINDOW_S = 3600.0

API_KEY_RATE_LIMIT_ENV = "UMBRA_API_KEY_RATE_LIMIT"
API_KEY_BURST_ENV = "UMBRA_API_KEY_BURST"
API_KEY_CONCURRENCY_ENV = "UMBRA_API_KEY_CONCURRENCY"
API_KEY_PEPPER_ENV = "UMBRA_API_KEY_PEPPER"
SIGNUP_RATE_LIMIT_ENV = "UMBRA_SIGNUP_RATE_LIMIT"
SIGNUP_BURST_ENV = "UMBRA_SIGNUP_BURST"
SIGNUP_WINDOW_ENV = "UMBRA_SIGNUP_WINDOW_S"

#: Shown once, in the create response. Not recoverable later.
ISSUED_MESSAGE = "Copy this key now. It is shown only once and cannot be recovered."

_KEY_PREFIX = "umbra_"
_OPTIONAL_MAX = 200
_EMAIL_MAX = 254
_TOKEN_MAX = 200
_KEY_ID_HEX = 12
_FILE_VERSION = 1
_MAX_FILE_BYTES = 1_000_000
_KEYS_NAME = "api-keys.json"

_EMAIL = re.compile(
    r"[a-z0-9][a-z0-9.!#$%&'*+/=?^_`{|}~-]{0,63}"
    r"@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_ISO_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_DIGEST = re.compile(r"(?:sha256|hmac-sha256):[0-9a-f]{64}\Z")


class SignupError(ValueError):
    """The sign-up body is missing a field or a field is not acceptable."""


def keys_path_for(index_path: str | os.PathLike[str] | None) -> Path:
    """``api-keys.json`` beside the catalog index, next to ``usage-stats.json``.

    The hosted process keeps the index on the ``/data`` volume. A redeploy
    that keeps that volume keeps the keys. This is not a second store: it is
    the directory :func:`umbra_py.usage_stats.stats_path_for` already chose.
    """
    from .usage_stats import stats_path_for  # noqa: PLC0415

    return stats_path_for(index_path).with_name(_KEYS_NAME)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def api_key_rate_limit() -> int:
    """Sustained keyed requests per minute (``UMBRA_API_KEY_RATE_LIMIT``)."""
    return _env_int(API_KEY_RATE_LIMIT_ENV, API_KEY_RATE_LIMIT)


def api_key_burst() -> int:
    """Keyed burst size (``UMBRA_API_KEY_BURST``), never above the sustained limit."""
    limit = api_key_rate_limit()
    return min(_env_int(API_KEY_BURST_ENV, API_KEY_BURST), limit)


def api_key_concurrency() -> int:
    """Catalog reads one key may hold (``UMBRA_API_KEY_CONCURRENCY``)."""
    return _env_int(API_KEY_CONCURRENCY_ENV, API_KEY_CONCURRENCY)


def signup_rate_limit() -> int:
    """Successful sign-ups per window per IP (``UMBRA_SIGNUP_RATE_LIMIT``)."""
    return _env_int(SIGNUP_RATE_LIMIT_ENV, SIGNUP_RATE_LIMIT)


def signup_burst() -> int:
    """Sign-up burst (``UMBRA_SIGNUP_BURST``), never above the sustained limit."""
    limit = signup_rate_limit()
    return min(_env_int(SIGNUP_BURST_ENV, SIGNUP_BURST), limit)


def signup_window_s() -> float:
    """Sign-up window in seconds (``UMBRA_SIGNUP_WINDOW_S``). Default one hour."""
    return _env_float(SIGNUP_WINDOW_ENV, SIGNUP_WINDOW_S)


def configured_pepper() -> str | None:
    """``UMBRA_API_KEY_PEPPER`` when it is set to a non-empty string.

    Unset, the key is stored as SHA-256. Set, it is stored as HMAC-SHA256
    with this pepper. The pepper is not required. Changing it after keys
    have been issued makes those keys fail to authenticate.
    """
    raw = os.environ.get(API_KEY_PEPPER_ENV)
    if raw is None:
        return None
    text = raw.strip()
    return text or None


def digest_key(secret: str, pepper: str | None) -> str:
    """Hex digest prefixed with the algorithm, suitable as a lookup key."""
    raw = secret.encode("utf-8")
    if pepper:
        mac = hmac.new(pepper.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        return f"hmac-sha256:{mac}"
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def key_id_for(digest: str) -> str:
    """Short id logged instead of the secret: the first 12 hex digits of the hash."""
    hexpart = digest.split(":", 1)[1]
    return hexpart[:_KEY_ID_HEX]


def mint_secret() -> str:
    """A new key. The ``umbra_`` prefix marks it as one of ours; it is not a password."""
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def _stamp(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%d")


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SignupError(f"{field} must be a string.")
    text = value.strip()
    if not text:
        return None
    if len(text) > _OPTIONAL_MAX or any(ord(ch) < 32 for ch in text):
        raise SignupError(f"{field} must be a single line of at most {_OPTIONAL_MAX} characters.")
    return text


def normalize_email(value: str) -> str:
    """Strip and lowercase. Raises :class:`SignupError` when it is not an address."""
    text = value.strip().lower()
    if not text or len(text) > _EMAIL_MAX or not _EMAIL.fullmatch(text):
        raise SignupError("email is not a valid address.")
    return text


def signup_from_mapping(data: Mapping[str, Any]) -> Signup:
    """The four sign-up fields. Anything else in ``data`` is ignored, not stored."""
    if not isinstance(data, Mapping):
        raise SignupError("Sign-up body must be an object.")
    email = data.get("email")
    if email is None or email == "":
        raise SignupError("email is required.")
    if not isinstance(email, str):
        raise SignupError("email is not a valid address.")
    return Signup(
        email=normalize_email(email),
        name=_optional_text(data.get("name"), "name"),
        organization=_optional_text(data.get("organization"), "organization"),
        intended_use=_optional_text(data.get("intended_use"), "intended_use"),
    )


@dataclass(frozen=True)
class Signup:
    """What a sign-up is allowed to record. No address, no secret."""

    email: str
    name: str | None
    organization: str | None
    intended_use: str | None


@dataclass
class ApiKeyRecord:
    """One stored key. ``digest`` is the hash, never the secret."""

    digest: str
    email: str
    name: str | None
    organization: str | None
    intended_use: str | None
    created_at: str
    revoked: bool
    last_used: str | None

    @property
    def key_id(self) -> str:
        return key_id_for(self.digest)

    def public_row(self) -> dict[str, Any]:
        """Email, created date, and whether it is revoked. No hash."""
        return {
            "email": self.email,
            "created_at": self.created_at,
            "revoked": self.revoked,
        }


@dataclass(frozen=True)
class KeyAuth:
    """What a presented credential resolved to.

    ``presented`` is false when the request carried neither ``Authorization:
    Bearer`` nor ``X-API-Key``. ``status`` is ``anonymous``, ``ok``,
    ``invalid``, or ``revoked``. ``key_id`` is the log prefix when the
    token could be hashed. ``rate_identity`` is set only for a valid key;
    invalid and revoked credentials are not given that key's bucket.
    """

    presented: bool
    status: str
    key_id: str | None
    rate_identity: str | None
    record: ApiKeyRecord | None


def _split_bearer(value: str) -> tuple[bool, str]:
    """True when ``value`` is a Bearer credential, plus the token (may be empty)."""
    if len(value) < 6 or value[:6].lower() != "bearer":
        return False, ""
    if len(value) == 6:
        return True, ""
    if value[6] not in " \t":
        return False, ""
    return True, value[6:].strip()


def read_presented_token(headers: Mapping[str, Any]) -> tuple[str, str | None]:
    """``(absent|invalid|token, token)``.

    Absent means the client did not try to authenticate. Invalid means they
    did, but the headers disagree, the token is empty, or it is too long to
    be a key we issued. A token is returned only when there is one candidate.
    """
    has_api = "x-api-key" in headers
    raw_api = headers.get("x-api-key")
    api = raw_api.strip() if isinstance(raw_api, str) else ""
    raw_auth = headers.get("authorization")
    bearer_present = False
    bearer = ""
    if isinstance(raw_auth, str):
        bearer_present, bearer = _split_bearer(raw_auth)
    if not has_api and not bearer_present:
        return "absent", None
    if has_api and bearer_present and api != bearer:
        return "invalid", None
    token = api if has_api else bearer
    if not token or len(token) > _TOKEN_MAX or any(ch.isspace() for ch in token):
        return "invalid", None
    return "token", token


def unauthorized_body(status: str) -> dict[str, str]:
    """JSON body for a 401. ``status`` is ``invalid`` or ``revoked``."""
    if status == "revoked":
        message = "API key has been revoked."
    else:
        message = "API key is invalid."
    return {"error": "Unauthorized", "message": message}


def issued_document(secret: str, record: ApiKeyRecord) -> dict[str, str]:
    """The create response. ``secret`` is the only place the key appears."""
    return {
        "key": secret,
        "email": record.email,
        "created_at": record.created_at,
        "message": ISSUED_MESSAGE,
    }


def signup_form_html(*, error: str | None = None) -> str:
    """The sign-up form. No script, no third-party asset."""
    alert = ""
    if error:
        alert = f'<p role="alert">{html.escape(error)}</p>'
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        "<title>Umbra open-data API key</title>\n"
        "</head>\n"
        "<body>\n"
        "<main>\n"
        "<h1>Request an API key</h1>\n"
        "<p>A key raises the rate limit on the hosted API at "
        '<a href="https://api.umbra-py.space/">api.umbra-py.space</a>. '
        "The key is shown once. The server stores a hash of it, plus the "
        "email and any optional fields below. It does not store your address. "
        "No verification email is sent.</p>\n"
        f"{alert}"
        '<form method="post" action="/keys">\n'
        '<p><label>Email<br><input name="email" type="email" required '
        'autocomplete="email" maxlength="254"></label></p>\n'
        '<p><label>Name (optional)<br><input name="name" autocomplete="name" '
        'maxlength="200"></label></p>\n'
        '<p><label>Organization (optional)<br><input name="organization" '
        'maxlength="200"></label></p>\n'
        '<p><label>Intended use (optional)<br><input name="intended_use" '
        'maxlength="200"></label></p>\n'
        '<p><button type="submit">Create key</button></p>\n'
        "</form>\n"
        "<p>Limits and a Python example are on "
        '<a href="https://umbra-py.space/hosted-limits/">the hosted limits page</a>.</p>\n'
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )


def signup_result_html(*, secret: str, email: str) -> str:
    """The one page that contains the secret. A later GET of the form does not."""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        "<title>API key created</title>\n"
        "</head>\n"
        "<body>\n"
        "<main>\n"
        "<h1>Copy this key now</h1>\n"
        "<p>It is shown only once and cannot be recovered. "
        "Send it as <code>Authorization: Bearer</code> or <code>X-API-Key</code>.</p>\n"
        f"<p><code>{html.escape(secret)}</code></p>\n"
        f"<p>Registered to {html.escape(email)}.</p>\n"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )


def _record_from_json(raw: Any) -> ApiKeyRecord | None:
    """One file entry, or ``None`` when it is not a key we can trust."""
    if not isinstance(raw, dict):
        return None
    digest = raw.get("digest")
    email = raw.get("email")
    created = raw.get("created_at")
    revoked = raw.get("revoked")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        return None
    if not isinstance(email, str):
        return None
    try:
        email = normalize_email(email)
    except SignupError:
        return None
    if not isinstance(created, str) or not _ISO_STAMP.fullmatch(created):
        return None
    if not isinstance(revoked, bool):
        return None
    last_used = raw.get("last_used")
    if last_used is not None and (not isinstance(last_used, str) or not _DAY.fullmatch(last_used)):
        return None
    try:
        name = _optional_text(raw.get("name"), "name")
        organization = _optional_text(raw.get("organization"), "organization")
        intended = _optional_text(raw.get("intended_use"), "intended_use")
    except SignupError:
        return None
    return ApiKeyRecord(
        digest=digest,
        email=email,
        name=name,
        organization=organization,
        intended_use=intended,
        created_at=created,
        revoked=revoked,
        last_used=last_used,
    )


class ApiKeyStore:
    """Keys in memory, mirrored to an atomic JSON file.

    Create and revoke write before they return, so a key that was shown to
    a caller is on disk. ``last_used`` advances at most once per UTC day and
    writes then. A file that cannot be read is moved aside; it is not
    overwritten with an empty list.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        pepper: str | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.path = Path(path)
        self.pepper = configured_pepper() if pepper is None else (pepper.strip() or None)
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._by_digest: dict[str, ApiKeyRecord] = {}
        self._persist = True
        self._load()

    def authenticate(self, headers: Mapping[str, Any]) -> KeyAuth:
        """Resolve ``Authorization: Bearer`` or ``X-API-Key``. Does not write."""
        kind, token = read_presented_token(headers)
        if kind == "absent":
            return KeyAuth(False, "anonymous", None, None, None)
        if kind != "token" or token is None:
            return KeyAuth(True, "invalid", None, None, None)
        digest = digest_key(token, self.pepper)
        key_id = key_id_for(digest)
        with self._lock:
            record = self._by_digest.get(digest)
        if record is None:
            return KeyAuth(True, "invalid", key_id, None, None)
        if record.revoked:
            return KeyAuth(True, "revoked", record.key_id, None, record)
        hexpart = digest.split(":", 1)[1]
        return KeyAuth(True, "ok", record.key_id, f"key:{hexpart}", record)

    def touch(self, record: ApiKeyRecord) -> None:
        """Remember that ``record`` was used today. One write per key per UTC day."""
        today = _day(self._clock())
        with self._lock:
            current = self._by_digest.get(record.digest)
            if current is None or current.revoked or current.last_used == today:
                return
            previous = current.last_used
            current.last_used = today
            try:
                self._write_locked()
            except UmbraError:
                current.last_used = previous
                logger.warning("api key last_used could not be saved")

    def create(self, signup: Signup) -> tuple[str, ApiKeyRecord]:
        """Mint a key, store only its hash, and return the secret once.

        The write finishes before this returns. A disk error rolls the new
        row back and raises :class:`~umbra_py.exceptions.UmbraError`, and the
        secret is not returned.
        """
        secret = mint_secret()
        digest = digest_key(secret, self.pepper)
        record = ApiKeyRecord(
            digest=digest,
            email=signup.email,
            name=signup.name,
            organization=signup.organization,
            intended_use=signup.intended_use,
            created_at=_stamp(self._clock()),
            revoked=False,
            last_used=None,
        )
        with self._lock:
            self._by_digest[digest] = record
            try:
                self._write_locked()
            except UmbraError:
                self._by_digest.pop(digest, None)
                raise
        logger.info(json.dumps({"msg": "api_key_created", "key_id": record.key_id}))
        return secret, record

    def list_public(self) -> list[dict[str, Any]]:
        """Email, created date, and revoked. Sorted by created date. No hash."""
        with self._lock:
            rows = [record.public_row() for record in self._by_digest.values()]
        rows.sort(key=lambda row: (row["created_at"], row["email"]))
        return rows

    def revoke(self, email: str, *, created_at: str | None = None) -> list[ApiKeyRecord]:
        """Revoke one active key, or the only active key for ``email``.

        Several active keys and no ``created_at`` raises
        :class:`~umbra_py.exceptions.UmbraError` and changes nothing. The
        message names the email and the created dates, not the hashes.
        """
        try:
            normalized = normalize_email(email)
        except SignupError as exc:
            raise UmbraError(str(exc)) from exc
        with self._lock:
            active = [
                record
                for record in self._by_digest.values()
                if record.email == normalized and not record.revoked
            ]
            if created_at is not None:
                active = [record for record in active if record.created_at == created_at]
            if not active:
                raise UmbraError(f"No active key for {normalized}.")
            if created_at is None and len(active) > 1:
                dates = ", ".join(
                    record.created_at for record in sorted(active, key=lambda item: item.created_at)
                )
                raise UmbraError(
                    f"More than one active key for {normalized}.",
                    hint=f"Pass --created with one of: {dates}",
                )
            for record in active:
                record.revoked = True
            try:
                self._write_locked()
            except UmbraError:
                for record in active:
                    record.revoked = False
                raise
            revoked = list(active)
        for record in revoked:
            logger.info(json.dumps({"msg": "api_key_revoked", "key_id": record.key_id}))
        return revoked

    def _snapshot_locked(self) -> dict[str, Any]:
        records = sorted(self._by_digest.values(), key=lambda record: record.created_at)
        return {
            "version": _FILE_VERSION,
            "keys": [
                {
                    "digest": record.digest,
                    "email": record.email,
                    "name": record.name,
                    "organization": record.organization,
                    "intended_use": record.intended_use,
                    "created_at": record.created_at,
                    "revoked": record.revoked,
                    "last_used": record.last_used,
                }
                for record in records
            ],
        }

    def _load(self) -> None:
        path = self.path
        if not path.is_file():
            return
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                self._quarantine("too large")
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self._quarantine("unreadable")
            return
        if not isinstance(raw, dict) or raw.get("version") != _FILE_VERSION:
            self._quarantine("unreadable")
            return
        entries = raw.get("keys")
        if not isinstance(entries, list):
            self._quarantine("unreadable")
            return
        loaded: dict[str, ApiKeyRecord] = {}
        for entry in entries:
            record = _record_from_json(entry)
            if record is None or record.digest in loaded:
                self._quarantine("unreadable")
                self._by_digest = {}
                return
            loaded[record.digest] = record
        self._by_digest = loaded

    def _quarantine(self, reason: str) -> None:
        path = self.path
        if not path.is_file():
            return
        stamp = datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = path.with_name(f"{path.name}.bad-{stamp}")
        n = 0
        while dest.exists():
            n += 1
            dest = path.with_name(f"{path.name}.bad-{stamp}-{n}")
        try:
            os.replace(path, dest)
        except OSError:
            logger.warning("api key file is %s and could not be moved aside", reason)
            self._persist = False
            return
        logger.warning("api key file is %s; moved it aside and starting empty", reason)

    def _write_locked(self) -> None:
        if not self._persist:
            raise UmbraError(
                "API keys could not be saved.",
                hint="The existing key file could not be moved aside.",
            )
        path = self.path
        document = self._snapshot_locked()
        blob = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(blob) > _MAX_FILE_BYTES:
            raise UmbraError("API keys could not be saved.", hint="The key file would exceed 1 MB.")
        tmp: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
            tmp = Path(tmp_name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            tmp = None
        except OSError as exc:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            raise UmbraError(
                "API keys could not be saved.",
                hint="The key file sits beside the catalog index on the data volume.",
            ) from exc


def authenticate_headers(headers: Mapping[str, Any], store: ApiKeyStore) -> KeyAuth:
    """:meth:`ApiKeyStore.authenticate` for call sites that already hold the store."""
    return store.authenticate(headers)
