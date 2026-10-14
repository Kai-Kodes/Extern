"""Native YouTube integration using yt-dlp for Todo CLI.

Provides URL parsing, validation, video/playlist metadata extraction,
and translation to Todo Source and FileItem data models.
Does NOT download video or audio files.
"""

from dataclasses import dataclass, field
import json
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import parse_qs, urlparse


class YouTubeError(Exception):
    """Base exception for YouTube integration."""
    pass


class YouTubeInvalidURLError(YouTubeError):
    """Raised when an invalid or unsupported YouTube URL is provided."""
    pass


class YouTubeNotFoundError(YouTubeError):
    """Raised when yt-dlp executable cannot be found."""
    pass


class YouTubeFetchError(YouTubeError):
    """Raised when metadata extraction fails."""
    pass


def format_duration(seconds: Optional[Union[int, float]]) -> str:
    """Format duration in seconds to a human-friendly string like '05:30' or '1h 32m 54s'."""
    if seconds is None:
        return ""
    try:
        total = int(seconds)
        if total < 0:
            return ""
        if total < 3600:
            return f"{total // 60:02d}:{total % 60:02d}"
        else:
            return f"{total // 3600:d}h {(total % 3600) // 60:02d}m {total % 60:02d}s"
    except (ValueError, TypeError):
        return ""


def parse_youtube_url(url: str) -> Dict[str, Any]:
    """
    Parse and validate a YouTube URL.

    Supports:
    - https://www.youtube.com/watch?v=VIDEO_ID
    - https://youtu.be/VIDEO_ID
    - https://www.youtube.com/shorts/VIDEO_ID
    - https://www.youtube.com/embed/VIDEO_ID
    - https://www.youtube.com/playlist?list=PLAYLIST_ID
    - https://www.youtube.com/watch?v=VIDEO_ID&list=PLAYLIST_ID
    - https://youtu.be/VIDEO_ID?list=PLAYLIST_ID

    Returns a dict with:
        kind: 'video' | 'playlist'
        video_id: Optional[str]
        playlist_id: Optional[str]
        canonical_url: str
        source_path: Optional[str]  # e.g. youtube://playlist/{playlist_id}
        item_path: Optional[str]    # e.g. youtube://video/{video_id}

    Raises:
        YouTubeInvalidURLError: If the URL is not a valid or supported YouTube link.
    """
    if not url or not isinstance(url, str):
        raise YouTubeInvalidURLError("Invalid URL: URL must be a non-empty string.")

    cleaned = url.strip()
    if not cleaned:
        raise YouTubeInvalidURLError("Invalid URL: URL cannot be blank.")

    # Add scheme if missing
    if not cleaned.startswith(("http://", "https://")):
        cleaned = "https://" + cleaned

    try:
        parsed = urlparse(cleaned)
    except Exception as e:
        raise YouTubeInvalidURLError(f"Invalid URL structure: {e}")

    hostname = (parsed.hostname or "").lower()
    valid_hosts = ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be")
    if not (hostname in valid_hosts or hostname.endswith(".youtube.com")):
        raise YouTubeInvalidURLError(
            f"Unsupported domain '{hostname}'. Please provide a valid YouTube link."
        )

    qs = parse_qs(parsed.query)
    playlist_id_list = qs.get("list", [])
    playlist_id = playlist_id_list[0].strip() if playlist_id_list else None

    video_id_list = qs.get("v", [])
    video_id = video_id_list[0].strip() if video_id_list else None

    path = parsed.path
    if hostname == "youtu.be":
        parts = [p for p in path.split("/") if p]
        if parts:
            video_id = parts[0].strip()
    elif "/shorts/" in path:
        parts = [p for p in path.split("/shorts/")[1].split("/") if p]
        if parts:
            video_id = parts[0].strip()
    elif "/embed/" in path:
        parts = [p for p in path.split("/embed/")[1].split("/") if p]
        if parts:
            video_id = parts[0].strip()

    # Determine kind
    # If list= is provided, treat as playlist (even if v= is also present)
    if playlist_id:
        return {
            "kind": "playlist",
            "playlist_id": playlist_id,
            "video_id": video_id,
            "canonical_url": f"https://www.youtube.com/playlist?list={playlist_id}",
            "source_path": f"youtube://playlist/{playlist_id}",
            "item_path": f"youtube://video/{video_id}" if video_id else None,
        }
    elif video_id:
        return {
            "kind": "video",
            "playlist_id": None,
            "video_id": video_id,
            "canonical_url": f"https://www.youtube.com/watch?v={video_id}",
            "source_path": None,
            "item_path": f"youtube://video/{video_id}",
        }

    raise YouTubeInvalidURLError(
        "Could not detect a valid YouTube video ID or playlist ID in the URL."
    )


@dataclass
class YouTubeVideo:
    video_id: str
    title: str
    canonical_url: str
    path: str  # e.g. youtube://video/{video_id}
    duration: Optional[float] = None
    duration_formatted: str = ""
    channel: Optional[str] = None
    uploader: Optional[str] = None
    position: int = 1


@dataclass
class YouTubePlaylist:
    playlist_id: str
    title: str
    canonical_url: str
    source_path: str  # e.g. youtube://playlist/{playlist_id}
    items: List[YouTubeVideo] = field(default_factory=list)
    video_count: int = 0
    skipped_count: int = 0


class YouTubeAdapter:
    """Adapter for extracting YouTube video and playlist metadata using yt-dlp."""

    def __init__(self, yt_dlp_command: str = "yt-dlp"):
        self.yt_dlp_command = yt_dlp_command

    def _check_binary(self) -> None:
        if not shutil.which(self.yt_dlp_command):
            raise YouTubeNotFoundError(
                f"Executable '{self.yt_dlp_command}' not found on PATH. Please install yt-dlp."
            )

    def fetch_single_video(self, url: str) -> YouTubeVideo:
        """
        Fetch metadata for a single video.
        Ensures `--no-playlist` is passed so that playlists are never silently imported.
        """
        self._check_binary()
        parsed_info = parse_youtube_url(url)
        video_id = parsed_info.get("video_id")
        canonical_url = parsed_info["canonical_url"]

        cmd = [
            self.yt_dlp_command,
            "--dump-json",
            "--skip-download",
            "--no-warnings",
            "--ignore-errors",
            "--no-playlist",
            canonical_url,
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=45,
            )
        except subprocess.TimeoutExpired:
            raise YouTubeFetchError("Fetching YouTube video timed out.")
        except Exception as e:
            raise YouTubeFetchError(f"Error executing yt-dlp: {e}")

        if proc.returncode != 0 and not proc.stdout.strip():
            err_msg = proc.stderr.strip() or "Failed to retrieve video metadata."
            raise YouTubeFetchError(err_msg)

        video_data: Optional[Dict[str, Any]] = None
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("id"):
                    video_data = data
                    break
            except json.JSONDecodeError:
                continue

        if not video_data:
            raise YouTubeFetchError("No valid video metadata returned by YouTube.")

        vid_id = video_data.get("id") or video_id or "unknown"
        title = video_data.get("title") or "Unknown Video"
        duration = video_data.get("duration")
        duration_fmt = format_duration(duration)
        channel = video_data.get("channel") or video_data.get("uploader")
        uploader = video_data.get("uploader")
        actual_canonical = video_data.get("webpage_url") or f"https://www.youtube.com/watch?v={vid_id}"

        return YouTubeVideo(
            video_id=vid_id,
            title=title,
            canonical_url=actual_canonical,
            path=f"youtube://video/{vid_id}",
            duration=duration,
            duration_formatted=duration_fmt,
            channel=channel,
            uploader=uploader,
            position=1,
        )

    def fetch_playlist(self, url: str) -> YouTubePlaylist:
        """
        Fetch metadata for a YouTube playlist and enumerate its videos.
        Deduplicates items by video ID.
        Handles unavailable, private, or deleted videos gracefully.
        """
        self._check_binary()
        parsed_info = parse_youtube_url(url)
        playlist_id = parsed_info.get("playlist_id")
        canonical_url = parsed_info["canonical_url"]

        cmd = [
            self.yt_dlp_command,
            "--dump-json",
            "--skip-download",
            "--no-warnings",
            "--ignore-errors",
            canonical_url,
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise YouTubeFetchError("Fetching YouTube playlist timed out.")
        except Exception as e:
            raise YouTubeFetchError(f"Error executing yt-dlp: {e}")

        if proc.returncode != 0 and not proc.stdout.strip():
            err_msg = proc.stderr.strip() or "Failed to retrieve playlist metadata."
            raise YouTubeFetchError(err_msg)

        playlist_title: Optional[str] = None
        items: List[YouTubeVideo] = []
        seen_ids = set()
        skipped_count = 0
        position = 1

        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Capture playlist title from first entry
            if not playlist_title:
                playlist_title = data.get("playlist_title") or data.get("playlist")

            vid_id = data.get("id")
            title = data.get("title")

            # Check for deleted/private/inaccessible
            if not vid_id or not title or title.strip() in ("[Deleted video]", "[Private video]"):
                skipped_count += 1
                continue

            # Deduplicate by stable ID within playlist
            if vid_id in seen_ids:
                continue
            seen_ids.add(vid_id)

            duration = data.get("duration")
            duration_fmt = format_duration(duration)
            channel = data.get("channel") or data.get("uploader")
            uploader = data.get("uploader")
            item_url = data.get("webpage_url") or f"https://www.youtube.com/watch?v={vid_id}"

            video_item = YouTubeVideo(
                video_id=vid_id,
                title=title,
                canonical_url=item_url,
                path=f"youtube://video/{vid_id}",
                duration=duration,
                duration_formatted=duration_fmt,
                channel=channel,
                uploader=uploader,
                position=position,
            )
            items.append(video_item)
            position += 1

        if not items and skipped_count == 0:
            raise YouTubeFetchError("No videos found in the specified playlist.")

        final_title = playlist_title or f"Playlist {playlist_id}"

        return YouTubePlaylist(
            playlist_id=playlist_id or "unknown",
            title=final_title,
            canonical_url=canonical_url,
            source_path=f"youtube://playlist/{playlist_id}",
            items=items,
            video_count=len(items),
            skipped_count=skipped_count,
        )
