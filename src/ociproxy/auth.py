"""Accounts, credentials and repo visibility.

Who can do what:

  anonymous  pull public repos
  reader     + pull private repos
  pusher     + push
  admin      + manage users (web UI / admin API)

Credentials are HTTP Basic on every registry/Helm request, which is what
``docker login`` and ``helm registry login`` / ``helm repo add --username``
send. The password part is either a local user's password or a personal
access token (``cpx_<id>_<secret>``). OIDC users have no password: they sign
in to the web UI and create tokens there.

The bootstrap account from ``push_username`` / ``push_password`` (env or
file) is an admin that is never stored: it works on every node from
configuration alone, so a fresh cluster can be administered.

Passwords are scrypt-hashed; tokens are 256-bit random secrets, so a plain
SHA-256 of the secret is stored. Docker sends credentials on every request,
so successful checks are cached briefly (the cache is dropped on any account
change on this node), and scrypt runs in worker threads, a few at a time.

Visibility of a client-visible repo path ("ghcr/org/app", "myteam/app",
"helm/stable") comes from, in order: the first matching ``[[access.rule]]``,
the upstream's / Helm repo's ``visibility``, ``access.default_visibility``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase

from starlette.requests import Request

from .config import Config
from .store import HELM_NS, Store

ROLES = ("reader", "pusher", "admin")
_RANK = {r: i for i, r in enumerate(ROLES)}

USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")
MIN_PASSWORD_LENGTH = 10
TOKEN_PREFIX = "cpx_"
TOKEN_NAME_MAX = 64

# scrypt cost: ~16 MiB and a few tens of ms per hash.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 1 << 14, 8, 1
_SCRYPT_MAXMEM = 64 << 20
# Verified for an unknown username, so a miss costs the same as a wrong
# password (no username enumeration by timing).
_DUMMY_HASH: str | None = None

CACHE_TTL = 60.0
CACHE_MAX = 4096
MAX_CONCURRENT_HASHES = 4
TOKEN_TOUCH_RESOLUTION = 3600.0


def role_at_least(role: str, minimum: str) -> bool:
    return _RANK.get(role, -1) >= _RANK[minimum]


# -- passwords ------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                        p=_SCRYPT_P, maxmem=_SCRYPT_MAXMEM, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt, expected = encoded.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n),
                            r=int(r), p=int(p), maxmem=_SCRYPT_MAXMEM, dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, base64.b64decode(expected))


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(16))
    return _DUMMY_HASH


# -- tokens ---------------------------------------------------------------------

def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_token() -> tuple[str, str, str]:
    """(token id, full token to hand out once, hash to store)."""
    token_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    return token_id, f"{TOKEN_PREFIX}{token_id}_{secret}", _hash_secret(secret)


def parse_token(value: str) -> tuple[str, str] | None:
    if not value.startswith(TOKEN_PREFIX):
        return None
    token_id, sep, secret = value[len(TOKEN_PREFIX):].partition("_")
    if not sep or len(token_id) != 12 or not secret:
        return None
    return token_id, secret


# -- account management (shared by the admin API and the CLI) -------------------

class AccountError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def validate_username(username) -> str:
    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        raise AccountError("username must be 1-64 characters: letters, digits, . _ @ - "
                           "(starting with a letter or digit)")
    return username


def validate_role(role) -> str:
    if role not in ROLES:
        raise AccountError(f"role must be one of {', '.join(ROLES)}")
    return role


def validate_password(password) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


def _not_reserved(username: str, reserved: str | None) -> None:
    if reserved and username == reserved:
        raise AccountError(f"{username!r} is the bootstrap admin from the configuration", 409)


def create_user(store: Store, username: str, role: str, password: str,
                *, reserved: str | None = None) -> dict:
    """A new local user. ``reserved`` is the bootstrap admin's name."""
    validate_username(username)
    _not_reserved(username, reserved)
    validate_role(role)
    validate_password(password)
    if store.get_user(username) is not None:
        raise AccountError(f"user {username!r} already exists", 409)
    now = time.time()
    user = {"username": username, "role": role, "source": "local",
            "password_hash": hash_password(password), "disabled": False,
            "created_at": now, "updated_at": now}
    store.put_user(user)
    return user


def update_user(store: Store, username: str, *, role: str | None = None,
                disabled: bool | None = None, password: str | None = None) -> dict:
    user = store.get_user(username)
    if user is None:
        raise AccountError(f"no user {username!r}", 404)
    if role is not None:
        user["role"] = validate_role(role)
    if disabled is not None:
        user["disabled"] = bool(disabled)
    if password is not None:
        if user["source"] != "local":
            raise AccountError("OIDC users have no password; they sign in through the provider")
        user["password_hash"] = hash_password(validate_password(password))
    user["updated_at"] = time.time()
    store.put_user(user)
    return user


def upsert_oidc_user(store: Store, username: str, role: str, *, reserved: str | None = None) -> dict:
    """Provision / re-sync a user signing in through OIDC.

    The role follows the provider's groups on every sign-in. A local
    account with the same name is never taken over.
    """
    validate_username(username)
    _not_reserved(username, reserved)
    validate_role(role)
    user = store.get_user(username)
    now = time.time()
    if user is not None and user["source"] != "oidc":
        raise AccountError(f"a local user named {username!r} already exists", 409)
    if user is not None and user["disabled"]:
        raise AccountError(f"user {username!r} is disabled", 403)
    if user is None:
        user = {"username": username, "source": "oidc", "password_hash": None,
                "disabled": False, "created_at": now}
    elif user["role"] == role:
        return user
    user.update(role=role, updated_at=now)
    store.put_user(user)
    return user


def create_token(store: Store, username: str, name: str,
                 expires_days: float | None = None) -> tuple[str, dict]:
    """(full token, stored row). The full token is shown exactly once."""
    if store.get_user(username) is None:
        raise AccountError(f"no user {username!r}", 404)
    if not isinstance(name, str) or not name.strip() or len(name) > TOKEN_NAME_MAX:
        raise AccountError(f"token name must be 1-{TOKEN_NAME_MAX} characters")
    expires_at = None
    if expires_days is not None:
        try:
            days = float(expires_days)
        except (TypeError, ValueError):
            raise AccountError("expires_days must be a number") from None
        if not 0 < days <= 3650:
            raise AccountError("expires_days must be between 0 and 3650")
        expires_at = time.time() + days * 86400
    token_id, full, secret_hash = new_token()
    row = {"id": token_id, "username": username, "name": name.strip(),
           "secret_hash": secret_hash, "created_at": time.time(), "expires_at": expires_at}
    store.put_token(row)
    return full, row


def public_user(user: dict) -> dict:
    return {k: user[k] for k in ("username", "role", "source", "disabled",
                                 "created_at", "updated_at")}


def public_token(token: dict) -> dict:
    return {k: token.get(k) for k in ("id", "username", "name", "created_at",
                                      "expires_at", "last_used_at")}


# -- visibility -------------------------------------------------------------------

class AccessPolicy:
    def __init__(self, config: Config):
        self.config = config

    def visibility(self, path: str) -> str:
        """Visibility of a client-visible repo path (no leading slash)."""
        path = path.strip("/")
        for rule in self.config.access_rules:
            if fnmatchcase(path, rule.path):
                return rule.visibility
        first, _, rest = path.partition("/")
        if first == HELM_NS.rstrip("/") and rest:
            repo = self.config.helm_repo(rest.partition("/")[0])
            if repo is not None and repo.visibility:
                return repo.visibility
        else:
            u = self.config.upstream_by_prefix(first)
            if u is not None and rest:
                return u.visibility or self.config.default_visibility
            # Unprefixed: a local push, or the default upstream on a local
            # miss. Take the stricter of the two, so a private default
            # upstream can't be read through its unprefixed form.
            dflt = self.config.default_upstream()
            if dflt is not None and dflt.visibility == "private":
                return "private"
        return self.config.default_visibility

    def is_public(self, path: str) -> bool:
        return self.visibility(path) == "public"

    @property
    def anything_private(self) -> bool:
        c = self.config
        return (c.default_visibility == "private"
                or any(r.visibility == "private" for r in c.access_rules)
                or any(u.visibility == "private" for u in c.upstreams)
                or any(r.visibility == "private" for r in c.helm_repos))


# -- authentication ---------------------------------------------------------------

@dataclass(frozen=True)
class Principal:
    username: str
    role: str
    source: str  # 'local' | 'oidc' | 'config'


def basic_credentials(request: Request) -> tuple[str, str] | None:
    header = request.headers.get("authorization", "")
    if not header[:6].lower() == "basic ":
        return None
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    user, sep, secret = decoded.partition(":")
    if not sep:
        return None
    return user, secret


class Authenticator:
    def __init__(self, store: Store, config: Config):
        self.store = store
        self.config = config
        # sha256(user \0 secret) -> (valid until, principal)
        self._cache: dict[bytes, tuple[float, Principal]] = {}
        self._hashing = asyncio.Semaphore(MAX_CONCURRENT_HASHES)
        store.add_account_listener(self.invalidate)

    def invalidate(self) -> None:
        self._cache = {}  # swap, not clear: safe from any thread

    @property
    def has_bootstrap(self) -> bool:
        return bool(self.config.push_username and self.config.push_password)

    async def authenticate(self, request: Request) -> Principal | None:
        creds = basic_credentials(request)
        if creds is None:
            return None
        return await self.check(*creds)

    async def check(self, username: str, secret: str, *, allow_tokens: bool = True) -> Principal | None:
        key = hashlib.sha256(f"{username}\0{secret}\0{allow_tokens}".encode()).digest()
        hit = self._cache.get(key)
        now = time.time()
        if hit is not None and hit[0] > now:
            return hit[1]
        principal, valid_until = await self._check(username, secret, allow_tokens)
        if principal is not None:
            cache = self._cache
            if len(cache) >= CACHE_MAX:
                cache.pop(next(iter(cache)), None)
            cache[key] = (min(now + CACHE_TTL, valid_until), principal)
        return principal

    async def _check(self, username: str, secret: str,
                     allow_tokens: bool) -> tuple[Principal | None, float]:
        forever = float("inf")
        if self.has_bootstrap:
            # Evaluate both so a wrong username takes as long as a wrong password.
            user_ok = hmac.compare_digest(username.encode(), self.config.push_username.encode())
            pwd_ok = hmac.compare_digest(secret.encode(), self.config.push_password.encode())
            if user_ok and pwd_ok:
                return Principal(username, "admin", "config"), forever
        parsed = parse_token(secret)
        if parsed is not None:
            if not allow_tokens:
                return None, 0
            return self._check_token(username, *parsed)
        user = self.store.get_user(username)
        usable = user is not None and not user["disabled"] and user["password_hash"]
        encoded = user["password_hash"] if usable else None
        async with self._hashing:
            ok = await asyncio.to_thread(lambda: verify_password(secret, encoded or _dummy_hash()))
        if ok and usable:
            return Principal(username, user["role"], user["source"]), forever
        return None, 0

    def _check_token(self, username: str, token_id: str, secret: str) -> tuple[Principal | None, float]:
        token = self.store.get_token(token_id)
        if token is None or not hmac.compare_digest(_hash_secret(secret), token["secret_hash"]):
            return None, 0
        now = time.time()
        expires_at = token["expires_at"] or float("inf")
        if token["username"] != username or expires_at <= now:
            return None, 0
        user = self.store.get_user(username)
        if user is None or user["disabled"]:
            return None, 0
        if now - (token["last_used_at"] or 0) >= TOKEN_TOUCH_RESOLUTION:
            self.store.touch_token(token_id, now)
        return Principal(username, user["role"], user["source"]), expires_at
