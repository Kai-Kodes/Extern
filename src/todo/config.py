"""Configuration management for Todo CLI."""

import os
from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass
class Config:
    db_path: Path
    filemeta_command: str = "filemeta"

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

        if config_file.exists():
            try:
                with open(config_file, "rb") as f:
                    data = tomllib.load(f)

                if "general" in data and "db_path" in data["general"]:
                    if not env_db:  # Environment variable takes precedence
                        db_path = Path(data["general"]["db_path"]).expanduser().resolve()

                if "filemeta" in data and "command" in data["filemeta"]:
                    filemeta_command = str(data["filemeta"]["command"])
            except Exception:
                # If config parsing fails, fallback gracefully to defaults
                pass

        return cls(db_path=db_path, filemeta_command=filemeta_command)

    def ensure_directories(self) -> None:
        """Ensure database and configuration directories exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
