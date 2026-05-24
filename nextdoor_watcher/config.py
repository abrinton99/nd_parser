"""Configuration loading (spec §16) and CLI-override merge.

Precedence: CLI flags > config file > built-in defaults.
The SMTP password is NEVER read from the config file — only the name of the
environment variable that holds it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10
    import tomli as _toml  # type: ignore


DEFAULT_OUTPUT_DIR = "./nextdoor-watcher-data"
DEFAULT_FIRST_RUN_MODE = "seed"
VALID_FIRST_RUN_MODES = ("seed", "capture-all")


@dataclass
class SmtpConfig:
    host: str = "localhost"
    port: int = 587
    username: str | None = None
    password_env: str | None = None
    use_starttls: bool = True
    use_ssl: bool = False
    timeout_seconds: int = 15


@dataclass
class NotifyConfig:
    enabled: bool = False
    to: list[str] = field(default_factory=list)
    sender: str = "nextdoor-watcher@localhost"
    subject_prefix: str = "[nextdoor-watcher]"
    send_recovery: bool = False
    consecutive_failure_threshold: int = 3
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    cooldown_hours: dict[str, float] = field(
        default_factory=lambda: {
            "auth": 6.0,
            "bad_input": 6.0,
            "partial": 1.0,
            "runtime": 6.0,
        }
    )


@dataclass
class Config:
    input_file: str | None = None
    output_dir: str = DEFAULT_OUTPUT_DIR
    first_run_mode: str = DEFAULT_FIRST_RUN_MODE
    quiet: bool = False
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    @property
    def output_path(self) -> Path:
        return Path(self.output_dir)

    @property
    def session_dir(self) -> Path:
        # Session is stored alongside the project, not in the output dir (spec §10).
        return Path("./session")

    @property
    def storage_state_path(self) -> Path:
        return self.session_dir / "storage_state.json"


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return _toml.load(fh)


def _parse_notify(raw: dict[str, Any]) -> NotifyConfig:
    nc = NotifyConfig()
    nc.enabled = bool(raw.get("enabled", nc.enabled))
    nc.to = list(raw.get("to", nc.to))
    nc.sender = raw.get("from", nc.sender)
    nc.subject_prefix = raw.get("subject_prefix", nc.subject_prefix)
    nc.send_recovery = bool(raw.get("send_recovery", nc.send_recovery))
    nc.consecutive_failure_threshold = int(
        raw.get("consecutive_failure_threshold", nc.consecutive_failure_threshold)
    )

    smtp_raw = raw.get("smtp", {})
    nc.smtp = SmtpConfig(
        host=smtp_raw.get("host", SmtpConfig.host),
        port=int(smtp_raw.get("port", SmtpConfig.port)),
        username=smtp_raw.get("username"),
        password_env=smtp_raw.get("password_env"),
        use_starttls=bool(smtp_raw.get("use_starttls", True)),
        use_ssl=bool(smtp_raw.get("use_ssl", False)),
        timeout_seconds=int(smtp_raw.get("timeout_seconds", 15)),
    )

    cooldown_raw = raw.get("cooldown_hours", {})
    for cls in ("auth", "bad_input", "partial", "runtime"):
        if cls in cooldown_raw:
            nc.cooldown_hours[cls] = float(cooldown_raw[cls])
    return nc


def load_config(
    config_path: str | None = None,
    *,
    input_file: str | None = None,
    output_dir: str | None = None,
    first_run_mode: str | None = None,
    quiet: bool | None = None,
) -> Config:
    """Load config from TOML (if present) and apply CLI overrides.

    `config_path` defaults to ./nextdoor-watcher.toml when it exists.
    """
    cfg = Config()

    path = Path(config_path) if config_path else Path("./nextdoor-watcher.toml")
    if path.exists():
        raw = _load_toml(path)
        cfg.input_file = raw.get("input_file", cfg.input_file)
        cfg.output_dir = raw.get("output_dir", cfg.output_dir)
        cfg.first_run_mode = raw.get("first_run_mode", cfg.first_run_mode)
        cfg.quiet = bool(raw.get("quiet", cfg.quiet))
        if "notify" in raw:
            cfg.notify = _parse_notify(raw["notify"])
    elif config_path:
        raise FileNotFoundError(f"config file not found: {config_path}")

    # CLI overrides win.
    if input_file is not None:
        cfg.input_file = input_file
    if output_dir is not None:
        cfg.output_dir = output_dir
    if first_run_mode is not None:
        cfg.first_run_mode = first_run_mode
    if quiet is not None:
        cfg.quiet = quiet

    if cfg.first_run_mode not in VALID_FIRST_RUN_MODES:
        raise ValueError(
            f"invalid first_run_mode {cfg.first_run_mode!r}; "
            f"expected one of {VALID_FIRST_RUN_MODES}"
        )
    return cfg
