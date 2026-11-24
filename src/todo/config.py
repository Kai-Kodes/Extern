"""Configuration management for Todo CLI."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import tomllib


DEFAULT_DROPBOX_APP_KEY = "bcfyaam7zsvenih"
DEFAULT_DROPBOX_PATH = "/todo.db"


def _serialize_toml(data: Dict[str, Dict[str, Any]]) -> str:
    """Serialize dictionary of sections into standard TOML string."""
    lines = []
    for section, sec_data in data.items():
        if not sec_data:
            continue
        lines.append(f"[{section}]")
        for k, v in sec_data.items():
            if v is None:
                continue
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, (int, float)):
                lines.append(f"{k} = {v}")
            else:
                s_val = str(v).replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{k} = "{s_val}"')
        lines.append("")
    return "\n".join(lines).strip() + "\n"


@dataclass
class Config:
    db_path: Path
    filemeta_command: str = "filemeta"
    filemeta_timeout: int = 60
    filemeta_device_timeout: int = 300
    telegram_api_id: Optional[int] = None
    telegram_api_hash: Optional[str] = None
    telegram_session_path: Optional[Path] = None
    pomodoro_work_sound: Optional[str] = None
    pomodoro_break_sound: Optional[str] = None
    pomodoro_audio_player: Optional[str] = None
    dropbox_app_key: str = DEFAULT_DROPBOX_APP_KEY
    dropbox_refresh_token: Optional[str] = None
    dropbox_access_token: Optional[str] = None
    dropbox_path: str = DEFAULT_DROPBOX_PATH
    dropbox_sync_enabled: bool = True
    is_first_run: bool = False

    @classmethod
    def get_config_dir(cls) -> Path:
        """Resolve the active configuration directory (~/.config/todo)."""
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config:
            return Path(xdg_config) / "todo"
        try:
            import platformdirs

            return Path(platformdirs.user_config_dir("todo", appauthor=False))
        except Exception:
            return Path.home() / ".config" / "todo"

    @classmethod
    def get_config_path(cls) -> Path:
        """Resolve the active configuration file path (~/.config/todo/config.toml)."""
        env_config = os.environ.get("TODO_CONFIG_PATH")
        if env_config:
            return Path(env_config).expanduser().resolve()
        return cls.get_config_dir() / "config.toml"

    @classmethod
    def get_data_dir(cls) -> Path:
        """Resolve the active data directory (~/.local/share/todo)."""
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            return Path(xdg_data) / "todo"
        try:
            import platformdirs

            return Path(platformdirs.user_data_dir("todo", appauthor=False))
        except Exception:
            return Path.home() / ".local" / "share" / "todo"

    @classmethod
    def create_default_config(cls, path: Optional[Path] = None) -> Path:
        """Create default config.toml file."""
        cfg_path = path or cls.get_config_path()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        default_data = {
            "general": {},
            "dropbox": {
                "app_key": DEFAULT_DROPBOX_APP_KEY,
                "path": DEFAULT_DROPBOX_PATH,
                "enabled": True,
            },
            "filemeta": {
                "command": "filemeta",
                "timeout": 60,
                "device_timeout": 300,
            },
        }
        cls._write_raw_toml(default_data, path=cfg_path)
        return cfg_path

    @classmethod
    def load(cls, auto_create: bool = True) -> "Config":
        """Load configuration from config.toml, environment variables, or defaults."""
        config_file = cls.get_config_path()
        data_dir = cls.get_data_dir()
        default_db_path = data_dir / "todo.db"

        is_first_run = False
        if not config_file.exists() and auto_create:
            is_first_run = True
            try:
                cls.create_default_config(config_file)
            except Exception:
                pass
        default_db_path = data_dir / "todo.db"

        # Database path
        env_db = os.environ.get("TODO_DB_PATH")
        db_path = Path(env_db).expanduser().resolve() if env_db else default_db_path

        filemeta_command = "filemeta"
        filemeta_timeout = 60
        filemeta_device_timeout = 300
        telegram_api_id: Optional[int] = None
        telegram_api_hash: Optional[str] = None
        telegram_session_path: Optional[Path] = None

        env_fm_timeout = os.environ.get("FILEMETA_TIMEOUT") or os.environ.get("TODO_FILEMETA_TIMEOUT")
        if env_fm_timeout and env_fm_timeout.isdigit():
            filemeta_timeout = int(env_fm_timeout)

        env_fm_dev_timeout = os.environ.get("FILEMETA_DEVICE_TIMEOUT") or os.environ.get("TODO_FILEMETA_DEVICE_TIMEOUT")
        if env_fm_dev_timeout and env_fm_dev_timeout.isdigit():
            filemeta_device_timeout = int(env_fm_dev_timeout)

        env_tg_api_id = os.environ.get("TELEGRAM_API_ID")
        if env_tg_api_id and env_tg_api_id.isdigit():
            telegram_api_id = int(env_tg_api_id)

        env_tg_api_hash = os.environ.get("TELEGRAM_API_HASH")
        if env_tg_api_hash:
            telegram_api_hash = env_tg_api_hash.strip()

        env_tg_session = os.environ.get("TELEGRAM_SESSION_PATH")
        if env_tg_session:
            telegram_session_path = Path(env_tg_session).expanduser().resolve()

        pomodoro_work_sound = os.environ.get("TODO_POMODORO_WORK_SOUND")
        pomodoro_break_sound = os.environ.get("TODO_POMODORO_BREAK_SOUND")
        pomodoro_audio_player = os.environ.get("TODO_POMODORO_PLAYER")

        dropbox_app_key = os.environ.get("DROPBOX_APP_KEY") or DEFAULT_DROPBOX_APP_KEY
        dropbox_refresh_token = os.environ.get("DROPBOX_REFRESH_TOKEN")
        dropbox_access_token = os.environ.get("DROPBOX_ACCESS_TOKEN")
        dropbox_path = os.environ.get("DROPBOX_PATH") or DEFAULT_DROPBOX_PATH
        dropbox_sync_enabled = True
        if os.environ.get("DROPBOX_SYNC_ENABLED", "").lower() in ("0", "false", "no"):
            dropbox_sync_enabled = False

        if config_file.exists():
            try:
                with open(config_file, "rb") as f:
                    data = tomllib.load(f)

                if "general" in data and "db_path" in data["general"]:
                    if not env_db:
                        db_path = Path(data["general"]["db_path"]).expanduser().resolve()

                if "filemeta" in data:
                    fm_sec = data["filemeta"]
                    if "command" in fm_sec:
                        filemeta_command = str(fm_sec["command"])
                    if "timeout" in fm_sec and not env_fm_timeout:
                        try:
                            filemeta_timeout = int(fm_sec["timeout"])
                        except (ValueError, TypeError):
                            pass
                    if "device_timeout" in fm_sec and not env_fm_dev_timeout:
                        try:
                            filemeta_device_timeout = int(fm_sec["device_timeout"])
                        except (ValueError, TypeError):
                            pass

                if "telegram" in data:
                    tg_sec = data["telegram"]
                    if not telegram_api_id and "api_id" in tg_sec:
                        telegram_api_id = int(tg_sec["api_id"])
                    if not telegram_api_hash and "api_hash" in tg_sec:
                        telegram_api_hash = str(tg_sec["api_hash"]).strip()
                    if not telegram_session_path and "session_path" in tg_sec:
                        telegram_session_path = Path(tg_sec["session_path"]).expanduser().resolve()

                if "pomodoro" in data:
                    pomo_sec = data["pomodoro"]
                    if not pomodoro_work_sound and "work_sound" in pomo_sec:
                        pomodoro_work_sound = str(pomo_sec["work_sound"]).strip()
                    if not pomodoro_break_sound and "break_sound" in pomo_sec:
                        pomodoro_break_sound = str(pomo_sec["break_sound"]).strip()
                    if not pomodoro_audio_player and "player" in pomo_sec:
                        pomodoro_audio_player = str(pomo_sec["player"]).strip()

                if "dropbox" in data:
                    dbx_sec = data["dropbox"]
                    if "app_key" in dbx_sec and not os.environ.get("DROPBOX_APP_KEY"):
                        dropbox_app_key = str(dbx_sec["app_key"]).strip()
                    if "refresh_token" in dbx_sec and not os.environ.get("DROPBOX_REFRESH_TOKEN"):
                        dropbox_refresh_token = str(dbx_sec["refresh_token"]).strip()
                    if "access_token" in dbx_sec and not os.environ.get("DROPBOX_ACCESS_TOKEN"):
                        dropbox_access_token = str(dbx_sec["access_token"]).strip()
                    if "path" in dbx_sec and not os.environ.get("DROPBOX_PATH"):
                        dropbox_path = str(dbx_sec["path"]).strip()
                    if "enabled" in dbx_sec and not os.environ.get("DROPBOX_SYNC_ENABLED"):
                        dropbox_sync_enabled = bool(dbx_sec["enabled"])

            except Exception:
                pass

        # Standard default session path in data directory (~/.local/share/todo/telegram.session)
        if not telegram_session_path:
            default_session = data_dir / "telegram.session"
            telegram_session_path = default_session

        cfg = cls(
            db_path=db_path,
            filemeta_command=filemeta_command,
            filemeta_timeout=filemeta_timeout,
            filemeta_device_timeout=filemeta_device_timeout,
            telegram_api_id=telegram_api_id,
            telegram_api_hash=telegram_api_hash,
            telegram_session_path=telegram_session_path,
            pomodoro_work_sound=pomodoro_work_sound,
            pomodoro_break_sound=pomodoro_break_sound,
            pomodoro_audio_player=pomodoro_audio_player,
            dropbox_app_key=dropbox_app_key,
            dropbox_refresh_token=dropbox_refresh_token,
            dropbox_access_token=dropbox_access_token,
            dropbox_path=dropbox_path,
            dropbox_sync_enabled=dropbox_sync_enabled,
            is_first_run=is_first_run,
        )
        try:
            cfg.ensure_directories()
        except Exception:
            pass
        return cfg

    @classmethod
    def _read_raw_toml(cls, path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
        """Read existing config.toml into a nested dict, or return empty dict."""
        cfg_path = path or cls.get_config_path()
        if not cfg_path.exists():
            return {}
        try:
            with open(cfg_path, "rb") as f:
                return tomllib.load(f)
        except Exception:
            return {}

    @classmethod
    def _write_raw_toml(
        cls, data: Dict[str, Dict[str, Any]], path: Optional[Path] = None
    ) -> None:
        """Atomically write dictionary to config.toml."""
        cfg_path = path or cls.get_config_path()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        content = _serialize_toml(data)
        tmp_path = cfg_path.with_suffix(".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(cfg_path)

    @classmethod
    def save_dropbox_credentials(
        cls,
        refresh_token: str,
        app_key: Optional[str] = None,
        path: Optional[str] = None,
    ) -> None:
        """Save Dropbox credentials into config.toml."""
        data = cls._read_raw_toml()
        if "dropbox" not in data:
            data["dropbox"] = {}
        data["dropbox"]["refresh_token"] = refresh_token
        if app_key:
            data["dropbox"]["app_key"] = app_key
        if path:
            data["dropbox"]["path"] = path
        data["dropbox"]["enabled"] = True
        cls._write_raw_toml(data)

    @classmethod
    def clear_dropbox_credentials(cls) -> None:
        """Remove Dropbox refresh token from config.toml."""
        data = cls._read_raw_toml()
        if "dropbox" in data:
            data["dropbox"].pop("refresh_token", None)
            cls._write_raw_toml(data)

    @classmethod
    def save_telegram_credentials(
        cls,
        api_id: int,
        api_hash: str,
        session_path: Optional[Path] = None,
    ) -> None:
        """Save Telegram credentials into config.toml."""
        data = cls._read_raw_toml()
        if "telegram" not in data:
            data["telegram"] = {}
        data["telegram"]["api_id"] = int(api_id)
        data["telegram"]["api_hash"] = str(api_hash).strip()
        if session_path:
            data["telegram"]["session_path"] = str(session_path)
        cls._write_raw_toml(data)

    @classmethod
    def clear_telegram_credentials(cls) -> None:
        """Remove Telegram credentials from config.toml."""
        data = cls._read_raw_toml()
        if "telegram" in data:
            data["telegram"].pop("api_id", None)
            data["telegram"].pop("api_hash", None)
            cls._write_raw_toml(data)

    def ensure_directories(self) -> None:
        """Ensure database and configuration directories exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.get_config_dir().mkdir(parents=True, exist_ok=True)
