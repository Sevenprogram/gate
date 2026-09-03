"""Runtime configuration, read once from the environment.

A *profile* is one set of API credentials — one Gate account or sub-account.
Each profile enables its own set of account types (spot, futures, unified), so
the dashboard's identity for a slice of data is the pair (profile, account).

Credentials for several profiles live in a TOML file rather than the environment,
because numbered env vars stop being readable at about two entries. A single key
in .env still works and is loaded as a profile called "default", so an existing
setup keeps running unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")

# Account view identifiers understood by the routers and the frontend.
SPOT = "spot"
FUTURES_USDT = "futures_usdt"
FUTURES_BTC = "futures_btc"
UNIFIED = "unified"

ALL_ACCOUNTS = (SPOT, FUTURES_USDT, FUTURES_BTC, UNIFIED)

# settle currency in the /futures/{settle}/... path
SETTLE_BY_ACCOUNT = {FUTURES_USDT: "usdt", FUTURES_BTC: "btc"}

# Account types whose balances are denominated in something other than the
# dashboard quote currency. They are excluded from portfolio totals rather than
# converted, because converting needs a historical rate per day that this app
# does not store — and a wrong rate is worse than an acknowledged gap.
NON_QUOTE_ACCOUNTS = frozenset({FUTURES_BTC})

DEFAULT_PROFILE_ID = "default"


@dataclass(frozen=True)
class Profile:
    """One set of credentials, and the account types it exposes."""

    id: str
    label: str
    api_key: str
    api_secret: str
    accounts: tuple[str, ...]

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)


def _toml_string(value: str) -> str:
    """JSON string escaping is valid TOML basic-string escaping, and the values
    here (hex keys, labels) need nothing more. ensure_ascii=False keeps labels
    readable in UTF-8 — the ASCII-escaped form parses back identically but is
    opaque to a human checking the file. A proper TOML writer is overkill for
    a flat list-of-tables schema we fully control."""
    return json.dumps(value, ensure_ascii=False)


def _serialize_profiles(profiles: list[Profile]) -> str:
    blocks = []
    for profile in profiles:
        lines = [
            "[[profile]]",
            f"id = {_toml_string(profile.id)}",
            f"label = {_toml_string(profile.label)}",
            f"key = {_toml_string(profile.api_key)}",
            f"secret = {_toml_string(profile.api_secret)}",
            "accounts = ["
            + ", ".join(_toml_string(a) for a in profile.accounts)
            + "]",
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


class ProfileStore:
    """The mutable set of profiles, persisted to the TOML file.

    Accounts can be added and removed from the UI at runtime, so the list has to
    outlive the startup read. The TOML file stays the single source of truth:
    every mutation is written back immediately, so a crash mid-session loses
    nothing and a hand edit before startup is still honoured (until the next UI
    mutation rewrites the file — comments included).
    """

    def __init__(
        self,
        path: Path,
        profiles: list[Profile],
        materialize_env_default: bool = False,
    ) -> None:
        self._path = path
        self._profiles = profiles
        # Set when the profiles came from .env rather than the file: the first
        # save has to write them out, which moves the secret from .env into
        # profiles.toml. Both are local and gitignored, but it deserves a log.
        self._materialize = materialize_env_default

    @property
    def profiles(self) -> list[Profile]:
        return self._profiles

    def add(self, profile: Profile) -> None:
        if any(p.id == profile.id for p in self._profiles):
            raise ValueError(f"profile id '{profile.id}' already exists")
        self._profiles.append(profile)
        self.save()

    def update(self, profile: Profile) -> None:
        """Replace a profile in place, keeping its position in the list."""
        for index, existing in enumerate(self._profiles):
            if existing.id == profile.id:
                self._profiles[index] = profile
                self.save()
                return
        raise KeyError(profile.id)

    def remove(self, profile_id: str) -> Profile:
        for index, profile in enumerate(self._profiles):
            if profile.id == profile_id:
                del self._profiles[index]
                self.save()
                return profile
        raise KeyError(profile_id)

    def save(self) -> None:
        if self._materialize:
            log.warning(
                "writing the .env-derived profile into %s — the key now lives "
                "there instead of the environment",
                self._path,
            )
            self._materialize = False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            _serialize_profiles(self._profiles), encoding="utf-8"
        )


@dataclass(frozen=True)
class Settings:
    profile_store: ProfileStore
    api_host: str
    tz: ZoneInfo
    tz_name: str
    quote: str
    db_path: Path
    spot_pairs: tuple[str, ...] = ()
    # Serve fabricated account snapshots so the page can be previewed without
    # credentials. Off unless explicitly asked for, and every response it
    # produces is flagged so no number can pass for real.
    demo: bool = False
    enabled_features: dict[str, bool] = field(default_factory=dict)

    @property
    def profiles(self) -> tuple[Profile, ...]:
        """Current profiles. Read through the store so UI edits are visible
        without a restart; returns a tuple per call so callers can't mutate."""
        return tuple(self.profile_store.profiles)

    def profile(self, profile_id: str) -> Profile | None:
        return next((p for p in self.profiles if p.id == profile_id), None)

    @property
    def scopes(self) -> tuple[tuple[str, str], ...]:
        """Every (profile, account) pair the dashboard can show."""
        return tuple(
            (profile.id, account)
            for profile in self.profiles
            for account in profile.accounts
        )

    @property
    def aggregatable_scopes(self) -> tuple[tuple[str, str], ...]:
        """Scopes that can go into a portfolio total, i.e. share the quote."""
        return tuple(
            (profile_id, account)
            for profile_id, account in self.scopes
            if account not in NON_QUOTE_ACCOUNTS
        )

    @property
    def has_credentials(self) -> bool:
        return any(p.has_credentials for p in self.profiles)


def _parse_accounts(raw: str, source: str) -> tuple[str, ...]:
    wanted = [item.strip() for item in raw.split(",") if item.strip()]
    unknown = [item for item in wanted if item not in ALL_ACCOUNTS]
    if unknown:
        raise ValueError(
            f"unknown account types {unknown} in {source}; "
            f"valid values are {list(ALL_ACCOUNTS)}"
        )
    # Preserve the written order — that is the tab order in the UI.
    return tuple(dict.fromkeys(wanted))


def _load_profiles_file(path: Path, default_accounts: tuple[str, ...]) -> list[Profile]:
    with path.open("rb") as handle:
        document = tomllib.load(handle)

    entries = document.get("profile")
    if not isinstance(entries, list) or not entries:
        raise ValueError(
            f"{path} has no [[profile]] entries; see profiles.example.toml"
        )

    profiles: list[Profile] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        profile_id = str(entry.get("id") or "").strip()
        if not profile_id:
            raise ValueError(f"{path}: profile #{index + 1} is missing an id")
        if not profile_id.replace("_", "").replace("-", "").isalnum():
            raise ValueError(
                f"{path}: profile id '{profile_id}' must be alphanumeric with "
                "dashes or underscores — it appears in URLs and in the database"
            )
        if profile_id in seen:
            raise ValueError(f"{path}: duplicate profile id '{profile_id}'")
        seen.add(profile_id)

        raw_accounts = entry.get("accounts")
        if isinstance(raw_accounts, list):
            accounts = _parse_accounts(",".join(str(a) for a in raw_accounts), str(path))
        else:
            accounts = default_accounts

        profiles.append(
            Profile(
                id=profile_id,
                label=str(entry.get("label") or profile_id),
                api_key=str(entry.get("key") or "").strip(),
                api_secret=str(entry.get("secret") or "").strip(),
                accounts=accounts,
            )
        )
    return profiles


def load_settings() -> Settings:
    tz_name = os.getenv("DASHBOARD_TZ", "Asia/Shanghai")
    db_path = Path(os.getenv("DB_PATH", "./gate_dashboard.sqlite3"))
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    default_accounts = _parse_accounts(
        os.getenv("GATE_ACCOUNTS", "spot,futures_usdt"), "GATE_ACCOUNTS"
    )

    profiles_path = Path(os.getenv("GATE_PROFILES_FILE", "./profiles.toml"))
    if not profiles_path.is_absolute():
        profiles_path = PROJECT_ROOT / profiles_path

    if profiles_path.is_file():
        profiles = _load_profiles_file(profiles_path, default_accounts)
        log.info("loaded %d profile(s) from %s", len(profiles), profiles_path)
        store = ProfileStore(profiles_path, profiles)
    else:
        # Single-key setup: the .env credentials become one profile so the rest
        # of the app only ever deals with the multi-profile shape.
        profiles = [
            Profile(
                id=DEFAULT_PROFILE_ID,
                label=os.getenv("GATE_PROFILE_LABEL", "主账户"),
                api_key=os.getenv("GATE_API_KEY", "").strip(),
                api_secret=os.getenv("GATE_API_SECRET", "").strip(),
                accounts=default_accounts,
            )
        ]
        # If an account is added from the UI, everything — including this
        # .env-derived profile — gets written to the file.
        store = ProfileStore(
            profiles_path, profiles, materialize_env_default=True
        )

    raw_pairs = os.getenv("GATE_SPOT_PAIRS", "")
    spot_pairs = tuple(p.strip().upper() for p in raw_pairs.split(",") if p.strip())

    return Settings(
        profile_store=store,
        api_host=os.getenv("GATE_API_HOST", "https://api.gateio.ws").rstrip("/"),
        tz=ZoneInfo(tz_name),
        tz_name=tz_name,
        quote=os.getenv("DASHBOARD_QUOTE", "USDT").upper(),
        db_path=db_path,
        spot_pairs=spot_pairs,
        demo=os.getenv("DASHBOARD_DEMO", "").strip() in {"1", "true", "yes"},
    )


settings = load_settings()
