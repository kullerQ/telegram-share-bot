"""Parsing and validation for individual configuration values."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import TypeVar

from telegram_share_bot.config.models import (
    _ENV_PATH,
    DEFAULT_ALLOWED_MEDIA_HOSTS,
    DEFAULT_CAPTION_MODE,
    DEFAULT_DELETE_STORAGE_MESSAGES,
    CaptionMode,
)

logger = logging.getLogger(__name__)

_TRUE_VALUES = frozenset({"true", "1", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "n", "off"})

T = TypeVar("T")

def parse_caption_mode(
    param_name: str,
    raw_value: str | None,
    default: CaptionMode = DEFAULT_CAPTION_MODE,
    env_file: Path = _ENV_PATH,
) -> CaptionMode:
    """Parse CAPTION_MODE: media | custom | off."""
    if raw_value is None or not raw_value.strip():
        return default
    stripped = raw_value.strip().lower()
    try:
        return CaptionMode(stripped)
    except ValueError:
        allowed = ", ".join(mode.value for mode in CaptionMode)
        reset = _handle_invalid_param(
            param_name=param_name,
            raw_value=raw_value,
            default_value=default.value,
            reason=f"Expected one of: {allowed}",
            env_file=env_file,
        )
        return CaptionMode(reset)


def parse_media_hosts(
    param_name: str,
    raw_value: str | None,
) -> frozenset[str] | None:
    """Parse ALLOWED_MEDIA_HOSTS.

    - unset/empty → default platform allowlist
    - ``*`` → allow any host (None)
    - comma-separated host suffixes otherwise
    """
    _ = param_name
    if raw_value is None or not raw_value.strip():
        return DEFAULT_ALLOWED_MEDIA_HOSTS
    stripped = raw_value.strip()
    if stripped == "*":
        return None
    hosts: set[str] = set()
    for part in stripped.split(","):
        host = part.strip().lower().removeprefix("www.").rstrip(".")
        if not host or "/" in host or "://" in host:
            raise RuntimeError(
                f"Invalid configuration for {param_name}='{stripped}': "
                "Expected comma-separated hostnames or '*'."
            )
        hosts.add(host)
    if not hosts:
        return DEFAULT_ALLOWED_MEDIA_HOSTS
    return frozenset(hosts)


def parse_user_ids(
    param_name: str,
    raw_value: str | None,
    env_file: Path = _ENV_PATH,
) -> frozenset[int]:
    """Parse a comma-separated list of Telegram user ids.

    Empty / unset returns an empty set. Access still requires ALLOW_PUBLIC=true
    or a non-empty allowlist (see load_settings).
    """
    _ = env_file
    if raw_value is None or not raw_value.strip():
        return frozenset()

    stripped = raw_value.strip()
    ids: set[int] = set()
    for part in stripped.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            ids.add(int(token))
        except ValueError as exc:
            # Never reset to empty (that would open the bot). Fail closed.
            raise RuntimeError(
                f"Invalid configuration for {param_name}='{stripped}': "
                "Expected a comma-separated list of integers. "
                "Please fix it in your .env file."
            ) from exc
    return frozenset(ids)


def _update_env_file(env_file: Path, key: str, new_value: str) -> None:
    try:
        if not env_file.exists():
            return
        lines = env_file.read_text(encoding="utf-8").splitlines()
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        updated = False
        new_lines: list[str] = []
        for line in lines:
            if pattern.match(line):
                new_lines.append(f"{key}={new_value}")
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append(f"{key}={new_value}")
        env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        logger.info("Updated %s=%s in %s", key, new_value, env_file.name)
    except Exception as exc:
        logger.warning("Could not update %s in %s: %s", key, env_file, exc)


def _handle_invalid_param(
    param_name: str,
    raw_value: str,
    default_value: T,
    reason: str,
    env_file: Path = _ENV_PATH,
) -> T:
    msg = f"Invalid configuration for {param_name}='{raw_value}': {reason}"
    logger.warning(msg)

    # In a non-interactive environment (CI, Docker, background service), fail fast.
    if not (sys.stdin and sys.stdin.isatty()):
        raise RuntimeError(
            f"{msg}. Non-interactive environment: please fix {param_name} in your .env file."
        )

    print(f"\n[WARNING] {msg}", file=sys.stderr)
    try:
        prompt = (
            f"Would you like to reset {param_name} to its default value '{default_value}'? [y/N]: "
        )
        choice = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt) as exc:
        raise RuntimeError(f"Configuration setup aborted for {param_name}.") from exc

    if choice in ("y", "yes"):
        logger.info("Resetting %s to default value: %s", param_name, default_value)
        _update_env_file(env_file, param_name, str(default_value).lower())
        return default_value

    raise RuntimeError(
        f"Invalid configuration for {param_name}='{raw_value}'. Please fix it in your .env file."
    )


def parse_bool(
    param_name: str,
    raw_value: str | None,
    default: bool = DEFAULT_DELETE_STORAGE_MESSAGES,
    env_file: Path = _ENV_PATH,
) -> bool:
    if raw_value is None or not raw_value.strip():
        return default
    val = raw_value.strip().lower()
    if val in _TRUE_VALUES:
        return True
    if val in _FALSE_VALUES:
        return False
    return _handle_invalid_param(
        param_name=param_name,
        raw_value=raw_value,
        default_value=default,
        reason="Expected a boolean (true, false, 1, 0, yes, no)",
        env_file=env_file,
    )


def parse_int(
    param_name: str,
    raw_value: str | None,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
    env_file: Path = _ENV_PATH,
) -> int:
    if raw_value is None or not raw_value.strip():
        return default
    stripped = raw_value.strip()
    try:
        val = int(stripped)
        if min_value is not None and val < min_value:
            raise ValueError(f"must be >= {min_value}")
        if max_value is not None and val > max_value:
            raise ValueError(f"must be <= {max_value}")
        return val
    except ValueError as exc:
        bounds = []
        if min_value is not None:
            bounds.append(f">= {min_value}")
        if max_value is not None:
            bounds.append(f"<= {max_value}")
        bounds_info = f" ({', '.join(bounds)})" if bounds else ""
        return _handle_invalid_param(
            param_name=param_name,
            raw_value=stripped,
            default_value=default,
            reason=f"Expected an integer{bounds_info}: {exc}",
            env_file=env_file,
        )


def parse_path(
    param_name: str,
    raw_value: str | None,
    default: Path,
    env_file: Path = _ENV_PATH,
    *,
    must_be_under: Path | None = None,
) -> Path:
    if raw_value is None or not raw_value.strip():
        path = default
    else:
        stripped = raw_value.strip()
        try:
            path = Path(stripped)
            if path.exists() and path.is_dir():
                raise ValueError(f"Path '{path}' is a directory, expected a file path")
        except Exception as exc:
            return _handle_invalid_param(
                param_name=param_name,
                raw_value=stripped,
                default_value=default,
                reason=str(exc),
                env_file=env_file,
            )

    if must_be_under is not None:
        try:
            path.resolve().relative_to(must_be_under.resolve())
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid configuration for {param_name}='{path}': "
                f"path must be under {must_be_under}"
            ) from exc
    return path


