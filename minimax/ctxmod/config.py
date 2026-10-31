"""Runtime configuration for the SDD context module.

Every path and connection setting can be overridden through environment
variables so the module can be replayed from a clean checkout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_PREFIX = "CTXMOD_"

#: Database name used inside the seekdb instance.
DATABASE = "ctxmod"


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(ENV_PREFIX + name, default)


def _env_path(name: str, default: Path) -> Path:
    raw = _env(name)
    return Path(raw).expanduser().resolve() if raw else default


@dataclass(frozen=True)
class Settings:
    """Resolved runtime settings."""

    root: Path
    seekdb_dir: Path
    state_db: Path
    log_dir: Path
    database: str = DATABASE
    minimax_model: str = "MiniMax-M3"
    minimax_base_url: str = "https://api.minimax.cn/v1"

    @classmethod
    def load(cls, root: Path | None = None) -> Settings:
        """Build settings from ``root`` (the ``minimax/`` folder) and the environment."""

        base = Path(root).resolve() if root else Path(__file__).resolve().parent.parent
        return cls(
            root=base,
            seekdb_dir=_env_path("SEEKDB_DIR", base / ".runtime" / "seekdb"),
            state_db=_env_path("STATE_DB", base / ".runtime" / "state" / "powercontext.sqlite3"),
            log_dir=_env_path("LOG_DIR", base / "logs"),
            database=_env("DATABASE", DATABASE) or DATABASE,
            minimax_model=_env("MINIMAX_MODEL", "MiniMax-M3") or "MiniMax-M3",
            minimax_base_url=_env("MINIMAX_BASE_URL", "https://api.minimax.cn/v1")
            or "https://api.minimax.cn/v1",
        )

    def ensure_dirs(self) -> None:
        """Create the writable runtime directories."""

        self.seekdb_dir.parent.mkdir(parents=True, exist_ok=True)
        self.state_db.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def relative(self, path: Path) -> str:
        """Return ``path`` relative to the module root when possible."""

        try:
            return str(Path(path).resolve().relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(Path(path).resolve()).replace("\\", "/")
