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
    filemeta_timeout: int = 60
    filemeta_device_timeout: int = 300
    telegram_api_id: Optional[int] = None
    telegram_api_hash: Optional[str] = None
    telegram_session_path: Optional[Path] = None
    pomodoro_work_sound: Optional[str] = None
    pomodoro_break_sound: Optional[str] = None
    pomodoro_audio_player: Optional[str] = None
    google_client_id: Optional[str] = None
    google_client_secret: Optional[str] = None
    google_credentials_file: Optional[Path] = None
    google_token_path: Optional[Path] = None

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
        filemeta_timeout = 60
        filemeta_device_timeout = 300
        telegram_api_id: Optional[int] = None
        telegram_api_hash: Optional[str] = None
        telegram_session_path: Optional[Path] = None

        # Check environment variables for Filemeta timeouts
        env_fm_timeout = os.environ.get("FILEMETA_TIMEOUT") or os.environ.get("TODO_FILEMETA_TIMEOUT")
        if env_fm_timeout and env_fm_timeout.isdigit():
            filemeta_timeout = int(env_fm_timeout)

        env_fm_dev_timeout = os.environ.get("FILEMETA_DEVICE_TIMEOUT") or os.environ.get("TODO_FILEMETA_DEVICE_TIMEOUT")
        if env_fm_dev_timeout and env_fm_dev_timeout.isdigit():
            filemeta_device_timeout = int(env_fm_dev_timeout)

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

        pomodoro_work_sound = os.environ.get("TODO_POMODORO_WORK_SOUND")
        pomodoro_break_sound = os.environ.get("TODO_POMODORO_BREAK_SOUND")
        pomodoro_audio_player = os.environ.get("TODO_POMODORO_PLAYER")

        google_client_id = (
            os.environ.get("GOOGLE_TASKS_CLIENT_ID")
            or os.environ.get("TODO_GOOGLE_CLIENT_ID")
        )
        google_client_secret = (
            os.environ.get("GOOGLE_TASKS_CLIENT_SECRET")
            or os.environ.get("TODO_GOOGLE_CLIENT_SECRET")
        )
        env_google_creds = (
            os.environ.get("GOOGLE_TASKS_CREDENTIALS_FILE")
            or os.environ.get("TODO_GOOGLE_CREDENTIALS_FILE")
        )
        google_credentials_file = (
            Path(env_google_creds).expanduser().resolve()
            if env_google_creds
            else None
        )
        env_google_token = (
            os.environ.get("GOOGLE_TASKS_TOKEN_PATH")
            or os.environ.get("TODO_GOOGLE_TOKEN_PATH")
        )
        google_token_path = (
            Path(env_google_token).expanduser().resolve()
            if env_google_token
            else None
        )

        if config_file.exists():
            try:
                with open(config_file, "rb") as f:
                    data = tomllib.load(f)

                if "general" in data and "db_path" in data["general"]:
                    if not env_db:  # Environment variable takes precedence
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

                gt_sec = data.get("google_tasks") or data.get("google")
                if gt_sec and isinstance(gt_sec, dict):
                    if not google_client_id and "client_id" in gt_sec:
                        google_client_id = str(gt_sec["client_id"]).strip()
                    if not google_client_secret and "client_secret" in gt_sec:
                        google_client_secret = str(gt_sec["client_secret"]).strip()
                    if not google_credentials_file and "credentials_file" in gt_sec:
                        google_credentials_file = Path(gt_sec["credentials_file"]).expanduser().resolve()
                    if not google_token_path and "token_path" in gt_sec:
                        google_token_path = Path(gt_sec["token_path"]).expanduser().resolve()
            except Exception:
                # If config parsing fails, fallback gracefully to defaults
                pass

        # Defaults for Google Tasks credentials and token path
        if not google_token_path:
            google_token_path = config_dir / "google_token.json"

        if not google_credentials_file:
            # Check default candidate paths in config_dir
            for candidate in [config_dir / "client_secret.json", config_dir / "credentials.json"]:
                if candidate.exists():
                    google_credentials_file = candidate
                    break
            if not google_credentials_file:
                google_credentials_file = config_dir / "credentials.json"

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
            filemeta_timeout=filemeta_timeout,
            filemeta_device_timeout=filemeta_device_timeout,
            telegram_api_id=telegram_api_id,
            telegram_api_hash=telegram_api_hash,
            telegram_session_path=telegram_session_path,
            pomodoro_work_sound=pomodoro_work_sound,
            pomodoro_break_sound=pomodoro_break_sound,
            pomodoro_audio_player=pomodoro_audio_player,
            google_client_id=google_client_id,
            google_client_secret=google_client_secret,
            google_credentials_file=google_credentials_file,
            google_token_path=google_token_path,
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
        if self.google_token_path:
            self.google_token_path.parent.mkdir(parents=True, exist_ok=True)

