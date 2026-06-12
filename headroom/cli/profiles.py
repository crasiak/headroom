"""Profile resolution for `headroom wrap --profile <name>`.

A profile is a named backend defined in ``~/.headroom/profiles.toml`` and
seeded from the user's existing agent config files. The resolver reads the
profile + its seed files and returns a ``ResolvedProfile`` the wrap commands
use to start a profile-complete proxy and configure the child agent. Pure
except for reading those files; never writes anything.
"""

from __future__ import annotations

import json
import tomllib
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from headroom.paths import workspace_dir

DEFAULT_PROFILE = "personal"


def default_profiles_path() -> Path:
    """Location of the user's profiles file (``~/.headroom/profiles.toml``)."""
    return workspace_dir() / "profiles.toml"


@dataclass(frozen=True)
class ResolvedProfile:
    name: str
    port: int
    bedrock_base_url: str | None = None
    claude_env: dict[str, str] = field(default_factory=dict)
    openai_upstream: str | None = None
    codex_seed_dir: str | None = None


def _strip_v1(url: str | None) -> str | None:
    if not isinstance(url, str):
        return None
    normalized = url.strip().rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[:-3]
    return normalized or None


def _profile_port(name: str, *, default_profile: str) -> int:
    """Deterministic per-profile proxy port.

    The configured default profile pins to the canonical 8787; every other
    profile gets a stable crc32-derived port (8788–9787). NOTE: changing
    `[profiles] default` reassigns 8787 to the new default — a profile's port
    is therefore relative to which profile is default, by design.
    """
    if name == default_profile:
        return 8787
    return 8788 + (zlib.crc32(name.encode()) % 1000)


def _expand(p: str) -> Path:
    return Path(p).expanduser()


def load_profiles(profiles_path_: Path) -> dict:
    if not profiles_path_.exists():
        raise FileNotFoundError(f"No headroom profiles file at {profiles_path_}")
    try:
        with open(profiles_path_, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{profiles_path_} is malformed TOML: {e}") from e


def select_profile_name(*, flag: str | None, env: dict[str, str], config_default: str | None) -> str:
    return flag or env.get("HEADROOM_PROFILE") or config_default or DEFAULT_PROFILE


def _read_claude_seed(path: Path) -> tuple[str | None, dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"claude_seed not found: {path}")
    data = json.loads(path.read_text())
    env = {str(k): str(v) for k, v in (data.get("env") or {}).items()}
    return env.get("ANTHROPIC_BEDROCK_BASE_URL"), env


def _read_codex_seed(dir_path: Path) -> str | None:
    """Return the active provider's upstream (``base_url`` minus /v1), or None
    when the seed uses the default OpenAI provider or already points at a local
    proxy (a prior in-place wrap)."""
    cfg = dir_path / "config.toml"
    if not dir_path.exists() or not cfg.exists():
        raise FileNotFoundError(f"codex_seed config not found: {cfg}")
    with open(cfg, "rb") as f:
        data = tomllib.load(f)
    provider = data.get("model_provider")
    providers = data.get("model_providers") or {}
    base_url = None
    if isinstance(provider, str) and provider in providers:
        base_url = providers[provider].get("base_url")
    if base_url is None:
        base_url = data.get("openai_base_url")
    upstream = _strip_v1(base_url)
    if upstream and any(h in upstream for h in ("127.0.0.1", "localhost", "::1")):
        return None  # seed already wrapped to a local proxy -> treat as default
    return upstream


_STARTER_PROFILES = '''\
# Headroom wrap profiles. Select with: headroom wrap <agent> --profile <name>
# (or set HEADROOM_PROFILE, or change `default` below).
[profiles]
default = "personal"

[profiles.personal]
codex_seed = "~/.codex"                              # ChatGPT / OpenAI Pro
# no claude_seed -> Claude Max (default Anthropic, no Bedrock)

[profiles.company]
claude_seed = "~/.claude/settings.corelight.json"    # Bedrock aperture
codex_seed  = "~/.codex-corelight"                    # aperture /v1 Responses
'''


def ensure_starter_profiles() -> Path:
    """Create a starter profiles.toml if none exists. Idempotent."""
    path = default_profiles_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_STARTER_PROFILES)
    return path


def resolve_profile(name: str, *, profiles_path: Path | None = None) -> ResolvedProfile:
    path = profiles_path if profiles_path is not None else default_profiles_path()
    doc = load_profiles(path)
    table = doc.get("profiles") or {}
    default_profile = table.get("default") or DEFAULT_PROFILE
    profile = table.get(name)
    if not isinstance(profile, dict):
        available = sorted(k for k in table if k != "default")
        raise KeyError(f"Unknown profile '{name}'. Available: {available}")

    if "port" in profile:
        try:
            port = int(profile["port"])
        except (TypeError, ValueError) as e:
            raise ValueError(f"profile '{name}' has a non-integer port: {profile['port']!r}") from e
    else:
        port = _profile_port(name, default_profile=default_profile)

    bedrock_base_url: str | None = None
    claude_env: dict[str, str] = {}
    if profile.get("claude_seed"):
        bedrock_base_url, claude_env = _read_claude_seed(_expand(profile["claude_seed"]))

    openai_upstream: str | None = None
    codex_seed_dir: str | None = None
    if profile.get("codex_seed"):
        seed_dir = _expand(profile["codex_seed"])
        openai_upstream = _read_codex_seed(seed_dir)
        codex_seed_dir = str(seed_dir)

    return ResolvedProfile(
        name=name,
        port=port,
        bedrock_base_url=bedrock_base_url,
        claude_env=claude_env,
        openai_upstream=openai_upstream,
        codex_seed_dir=codex_seed_dir,
    )
