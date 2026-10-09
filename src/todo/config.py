"""Configuration management for Todo CLI."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import ast
import os
import tomllib


@dataclass
class Config:
    db_path: Path
    filemeta_command: str = "filemeta"
    telegram_api_id: Optional[int] = None
    telegram_api_hash: Optional[str] = None
    telegram_session_path: Optional[Path] = None

    @classmethod
    def load(cls) -> "Config":
        """Load configuration from config.toml, environment variables, or defaults."""
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        config_dir = (
            Path(xdg_config) / "todo"
            if xdg_config
            else Path.home() / ".config" / "todo"
        )
        config_file = config_dir / "config.toml"

        env_config = os.environ.get("TODO_CONFIG_PATH")
        if env_config:
            config_file = Path(env_config).expanduser().resolve()

        xdg_data = os.environ.get("XDG_DATA_HOME")
        default_data_dir = (
            Path(xdg_data) / "todo"
            if xdg_data
            else Path.home() / ".local" / "share" / "todo"
        )
        default_db_path = default_data_dir / "todo.db"

        # Check environment variable override for db_path
        env_db = os.environ.get("TODO_DB_PATH")
        if env_db:
            db_path = Path(env_db).expanduser().resolve()
        else:
            db_path = default_db_path

        filemeta_command = "filemeta"
        telegram_api_id: Optional[int] = None
        telegram_api_hash: Optional[str] = None
        telegram_session_path: Optional[Path] = None

        # Check environment variables for Telegram
        env_tg_api_id = os.environ.get("TELEGRAM_API_ID")
        if env_tg_api_id and env_tg_api_id.isdigit():
            telegram_api_id = int(env_tg_api_id)

        env_tg_api_hash = os.environ.get("TELEGRAM_API_HASH")
        if env_tg_api_hash:
            telegram_api_hash = env_tg_api_hash.strip()

        env_tg_session = os.environ.get("TELEGRAM_SESSION_PATH")
        if env_tg_session:
            telegram_session_path = Path(env_tg_session).expanduser().resolve()

        if config_file.exists():
            try:
                with open(config_file, "rb") as f:
                    data = tomllib.load(f)

                if "general" in data and "db_path" in data["general"]:
                    if not env_db:  # Environment variable takes precedence
                        db_path = Path(data["general"]["db_path"]).expanduser().resolve()

                if "filemeta" in data and "command" in data["filemeta"]:
                    filemeta_command = str(data["filemeta"]["command"])

                if "telegram" in data:
                    tg_sec = data["telegram"]
                    if not telegram_api_id and "api_id" in tg_sec:
                        telegram_api_id = int(tg_sec["api_id"])
                    if not telegram_api_hash and "api_hash" in tg_sec:
                        telegram_api_hash = str(tg_sec["api_hash"]).strip()
                    if not telegram_session_path and "session_path" in tg_sec:
                        telegram_session_path = Path(tg_sec["session_path"]).expanduser().resolve()
            except Exception:
                # If config parsing fails, fallback gracefully to defaults
                pass

        # Automatic fallback for Telegram session path if not explicitly configured
        if not telegram_session_path:
            candidate_home_session = Path.home() / "course_session.session"
            if candidate_home_session.exists():
                telegram_session_path = Path.home() / "course_session"
            else:
                candidate_config_session = config_dir / "course_session.session"
                if candidate_config_session.exists():
                    telegram_session_path = config_dir / "course_session"

        # Automatic fallback for API credentials from local duration.py if available
        if not telegram_api_id or not telegram_api_hash:
            leg_id, leg_hash = cls._extract_local_telegram_creds()
            if not telegram_api_id and leg_id:
                telegram_api_id = leg_id
            if not telegram_api_hash and leg_hash:
                telegram_api_hash = leg_hash

        return cls(
            db_path=db_path,
            filemeta_command=filemeta_command,
            telegram_api_id=telegram_api_id,
            telegram_api_hash=telegram_api_hash,
            telegram_session_path=telegram_session_path,
        )

    @classmethod
    def _extract_local_telegram_creds(cls) -> tuple[Optional[int], Optional[str]]:
        """Safely parse legacy telegram-tools/duration.py AST without executing it."""
        duration_file = Path.home() / "telegram-tools" / "duration.py"
        if not duration_file.exists():
            return None, None
        try:
            tree = ast.parse(duration_file.read_text())
            api_id = None
            api_hash = None
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            if target.id == "api_id" and isinstance(node.value, ast.Constant):
                                api_id = int(node.value.value)
                            elif target.id == "api_hash" and isinstance(node.value, ast.Constant):
                                api_hash = str(node.value.value)
            return api_id, api_hash
        except Exception:
            return None, None

    def ensure_directories(self) -> None:
        """Ensure database and configuration directories exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

