"""Desktop audio playback service independent of desktop notifications."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple


def get_default_sound_path() -> Path:
    """
    Resolve the bundled project audio asset path.
    Supports running from repository root, installed package, or subdirectories.
    """
    # 1. Project root assets/sounds/nintendo-switch-snap.m4a
    repo_asset = Path(__file__).resolve().parents[3] / "assets" / "sounds" / "nintendo-switch-snap.m4a"
    if repo_asset.exists():
        return repo_asset

    # 2. Package-relative asset if packaged inside src/todo/assets
    pkg_asset = Path(__file__).resolve().parent.parent / "assets" / "sounds" / "nintendo-switch-snap.m4a"
    if pkg_asset.exists():
        return pkg_asset

    # 3. Current working directory fallback
    cwd_asset = Path.cwd() / "assets" / "sounds" / "nintendo-switch-snap.m4a"
    if cwd_asset.exists():
        return cwd_asset

    return repo_asset


class AudioService:
    """Non-blocking desktop audio playback using compatible system players (mpv, ffplay, cvlc, etc.)."""

    # Audio extensions requiring containers/codecs beyond standard libsndfile
    M4A_EXTENSIONS = {".m4a", ".aac", ".mp4", ".m4v"}

    # Players capable of decoding AAC / M4A containers
    M4A_COMPATIBLE_PLAYERS = ["mpv", "ffplay", "cvlc"]

    # General player candidates for basic formats (e.g. WAV, OGG)
    GENERAL_PLAYERS = ["mpv", "pw-play", "paplay", "aplay", "ffplay", "cvlc"]

    def __init__(
        self,
        player_cmd: Optional[str] = None,
        work_sound: Optional[str] = None,
        break_sound: Optional[str] = None,
        enabled: bool = True,
    ):
        self.enabled = enabled
        self._custom_player = player_cmd
        self._custom_work_sound = work_sound
        self._custom_break_sound = break_sound
        self._current_process: Optional[subprocess.Popen] = None

    def get_default_sound(self) -> str:
        """Return the default project sound asset path."""
        return str(get_default_sound_path().resolve())

    def get_player(self, sound_path: Optional[str] = None) -> Optional[str]:
        """
        Find the executable path for the audio player.
        Selects a player compatible with the target sound format.
        """
        if self._custom_player:
            which = shutil.which(self._custom_player)
            return which or self._custom_player

        is_m4a = False
        if sound_path:
            ext = Path(sound_path).suffix.lower()
            if ext in self.M4A_EXTENSIONS:
                is_m4a = True

        candidates = self.M4A_COMPATIBLE_PLAYERS if is_m4a else self.GENERAL_PLAYERS
        for candidate in candidates:
            which = shutil.which(candidate)
            if which:
                return which
        return None

    def get_work_sound(self) -> Optional[str]:
        """Resolve path to the work completion sound."""
        if self._custom_work_sound:
            p = Path(self._custom_work_sound)
            if p.exists():
                return str(p.resolve())
            return self._custom_work_sound

        return self.get_default_sound()

    def get_break_sound(self) -> Optional[str]:
        """Resolve path to the break completion sound."""
        if self._custom_break_sound:
            p = Path(self._custom_break_sound)
            if p.exists():
                return str(p.resolve())
            return self._custom_break_sound

        return self.get_default_sound()

    def is_available(self, sound_path: Optional[str] = None) -> bool:
        """Check whether a compatible audio player is available."""
        return self.get_player(sound_path) is not None

    def is_playing(self) -> bool:
        """Check whether an audio playback process is currently running."""
        if self._current_process is not None:
            poll_fn = getattr(self._current_process, "poll", None)
            if poll_fn is not None:
                return poll_fn() is None
        return False

    def stop_audio(self) -> None:
        """Terminate any currently running playback process."""
        if self._current_process is not None:
            poll_fn = getattr(self._current_process, "poll", None)
            term_fn = getattr(self._current_process, "terminate", None)
            if poll_fn is not None and poll_fn() is None and term_fn is not None:
                try:
                    term_fn()
                except OSError:
                    pass
            self._current_process = None

    def play_sound(self, sound_path: Optional[str]) -> Tuple[bool, str]:
        """
        Play sound asynchronously via subprocess.Popen.
        Completely non-blocking, detached from UI thread, zero desktop notifications.
        Guarantees no overlapping playback by rejecting concurrent triggers while playing.
        """
        if not self.enabled:
            return False, "Audio is disabled"

        if not sound_path or not Path(sound_path).exists():
            return False, f"Sound file not found: {sound_path}"

        # Prevent overlapping playback if a sound is already currently playing
        if self.is_playing():
            return False, "Audio playback already in progress"

        player = self.get_player(sound_path)
        if not player:
            ext = Path(sound_path).suffix.lower()
            if ext in self.M4A_EXTENSIONS:
                return False, "No M4A-compatible audio player found (mpv, ffplay, cvlc)"
            return False, "No compatible audio player found"

        try:
            player_name = Path(player).name.lower()
            # Build argument list depending on player
            if "mpv" in player_name:
                # --no-config ensures user configs like keep-open=yes or idle=yes do not hang mpv
                args = [player, "--no-config", "--no-video", "--really-quiet", sound_path]
            elif "ffplay" in player_name:
                args = [player, "-nodisp", "-autoexit", "-loglevel", "quiet", sound_path]
            elif "cvlc" in player_name:
                args = [player, "--play-and-exit", "--no-video", "--quiet", sound_path]
            else:
                args = [player, sound_path]

            proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._current_process = proc
            return True, f"Playing sound with {player}"
        except Exception as e:
            return False, f"Failed to play audio: {e}"

    def play_work_sound(self) -> Tuple[bool, str]:
        """Play sound for work session completion."""
        sound = self.get_work_sound()
        return self.play_sound(sound)

    def play_break_sound(self) -> Tuple[bool, str]:
        """Play sound for break completion."""
        sound = self.get_break_sound()
        return self.play_sound(sound)

    def test_sound(self, session_type: str = "work") -> Tuple[bool, str]:
        """Trigger sound test."""
        if session_type == "break":
            return self.play_break_sound()
        return self.play_work_sound()
