# Extern

A fast, local-first, terminal-based personal task-management and course tracking system for Linux, macOS, and Android (Termux).

Built with Python, Click, Rich, SQLite, Telethon, and the Dropbox SDK. Designed to be standalone, visually quiet, responsive, keyboard-driven, and distraction-free.

---

## Highlights

- **Today's Dashboard**: Full-screen alternate-screen terminal interface showing tasks scheduled specifically for today, responsive progress tracking (`42% · 5/12`), and instant arrow/numeric navigation.
- **Strict Global Silence**: Zero transient status banners or toast chatter on routine actions. Toggle, delete, schedule, and navigate cleanly; only genuine errors are highlighted.
- **Native Dropbox Synchronization**: Built-in Python sync engine using the official Dropbox SDK. Automatically pulls before startup and checkpoints SQLite WAL + pushes on exit. No `rclone`, cron jobs, or bash wrappers required.
- **Native Built-In Course Scanner**: Pure-Python recursive directory scanner with `tinytag` audio/video duration extraction and natural alphanumeric sorting. External `filemeta` CLI is completely optional.
- **Native Telegram & YouTube Integration**: Browse course channels and YouTube playlists directly inside the TUI. Interactive terminal login (`extern auth telegram`) saves sessions automatically to `~/.local/share/todo/telegram.session`.
- **Chunky Tasks & Presets**: Break large media lessons and books into manageable contiguous chunks (`00:20:00–00:40:00`) with gap coverage calculation, custom duration presets, and "Entire remaining resource" support.
- **Pomodoro Timer**: Integrated interval focus timer with configurable work/break cycles, audio cues, and background timer ticking.
- **Timeline & Universal Backlog**: Historical calendar view and reusable universal backlog independent of any date.
- **System Diagnostics**: Built-in `extern doctor` checks configuration, database, scanner, and integration health in seconds.
- **Dual CLI Aliases**: Invoke as `extern` or short alias `todo`.

---

## Quick Installation

Install directly with `pipx` (recommended) or `pip`:

```bash
# Recommended: Isolated global CLI installation
pipx install git+https://github.com/Kai-Kodes/Extern.git

# Or with pip
pip install git+https://github.com/Kai-Kodes/Extern.git
```

To install from a local clone:

```bash
git clone https://github.com/Kai-Kodes/Extern.git
cd Extern
pip install .
```

Both `extern` and `todo` commands will be available in your `PATH`.

### Termux on Android

Extern is fully self-contained and runs on Android via Termux:

```bash
# 1. Update Termux and install Python + Git
pkg update && pkg install -y python git

# 2. Install Extern directly
pip install git+https://github.com/Kai-Kodes/Extern.git
```

---

## Initial Setup & Authentication

### 1. Link Dropbox for Automatic Cross-Device Sync

Connect your Dropbox account using secure OAuth2 PKCE without copying config files:

```bash
extern auth dropbox
```

1. Open the generated link in your browser and click **Allow**.
2. Paste the authorization code back into the terminal.
3. Extern securely stores your offline refresh token in `~/.config/todo/config.toml`.

From then on, running `extern` or `todo` automatically:
- Pulls `/todo.db` from Dropbox before launching if a newer remote revision exists.
- Flushes the SQLite WAL (`PRAGMA wal_checkpoint(TRUNCATE)`) and uploads back to Dropbox upon exit.
- Operates offline gracefully without crashing if no internet connection is present.

To unlink Dropbox at any time:
```bash
extern auth dropbox --logout
```

### 2. Link Telegram for Video Lesson Courses

Connect your Telegram account interactively:

```bash
extern auth telegram
```

1. Enter your `api_id` and `api_hash` (obtain from [my.telegram.org](https://my.telegram.org) under *API development tools*).
2. Enter your phone number, the verification code sent to your Telegram app, and your 2FA password (if enabled).
3. The session is saved to `~/.local/share/todo/telegram.session`.

To log out:
```bash
extern auth telegram --logout
```

---

## Usage

### Interactive TUI Dashboard

Launch the full-screen terminal interface:

```bash
extern
# or
todo
```

To launch without remote syncing (offline mode):
```bash
extern --no-sync
```

#### Today Dashboard Keybindings

| Key | Action |
| :--- | :--- |
| `↑` / `↓` | Navigate task list |
| `Enter` | Toggle task completion |
| `1` .. `9` | Directly toggle task #1..9 |
| `+` | Open Add Task menu (1. Local Folder, 2. Telegram, 3. YouTube, 4. Universal, 5. Manual) |
| `c` | Open Chunky Tasks view (chunk creation, presets, and gap progress) |
| `p` | Open Pomodoro Focus Timer |
| `l` | Open Timeline view (calendar completion history & future planning) |
| `e` | Edit selected task title, notes, or schedule |
| `d` | Remove selected task from Today's schedule |
| `m` | Move selected task to Universal backlog |
| `u` | Open Universal task master list |
| `f` | Open Local Course folder library |
| `t` | Open Telegram course library |
| `y` | Open YouTube playlist library |
| `q` / `Esc` | Quit cleanly (checkpoints WAL & pushes to Dropbox) |

---

## CLI Commands

### Tasks & Daily Scheduling

```bash
# Print tasks scheduled for today (non-interactive)
extern today

# Add task to today
extern add "Complete Module 3 Video Lesson"

# Add task to universal backlog
extern add "Read Design Patterns Book" --universal

# List all universal backlog tasks
extern list

# List imported course sources
extern sources

# Explicitly checkpoint SQLite WAL
extern db-checkpoint
```

### Manual Sync Control

```bash
# View sync status and cached revisions
extern sync status

# Manually pull latest database from Dropbox
extern sync pull

# Manually push local database to Dropbox
extern sync push
```

### System Diagnostics

Inspect your local configuration, database status, scanner availability, and integrations:

```bash
extern doctor
# or
extern status
```

Example diagnostic report:

```
EXTERN SYSTEM DIAGNOSTICS

  Component           Status         Details
  Configuration       OK             ~/.config/todo/config.toml
  SQLite Database     OK             ~/.local/share/todo/todo.db (864.0 KiB, clean)
  Course Scanner      OK             Built-in Pure-Python scanner with TinyTag
  YouTube (yt-dlp)    OK             yt-dlp version 2025.04.30
  Telegram            Authenticated  Session: ~/.local/share/todo/telegram.session
  Dropbox Sync        Linked         Online, rev 0123456789abcdef -> /todo.db
```

---

## Configuration (`config.toml`)

Extern follows the XDG Base Directory specification. Configuration is loaded from `~/.config/todo/config.toml` (or `$XDG_CONFIG_HOME/todo/config.toml`):

```toml
[general]
db_path = "~/.local/share/todo/todo.db"

[dropbox]
app_key = "bcfyaam7zsvenih"
refresh_token = "your_offline_refresh_token"
path = "/todo.db"
enabled = true

[telegram]
api_id = 12345678
api_hash = "your_telegram_api_hash"
session_path = "~/.local/share/todo/telegram.session"

[filemeta]
command = "filemeta"
timeout = 60
device_timeout = 300

[pomodoro]
work_sound = "/usr/share/sounds/freedesktop/stereo/complete.oga"
break_sound = "/usr/share/sounds/freedesktop/stereo/bell.oga"
player = "pw-play"
```

Environment variables take precedence over `config.toml`:
- `TODO_DB_PATH`: Custom path to SQLite database.
- `TODO_CONFIG_PATH`: Custom path to `config.toml`.
- `DROPBOX_REFRESH_TOKEN`: Override Dropbox refresh token.
- `DROPBOX_APP_KEY`: Override Dropbox app key.
- `DROPBOX_PATH`: Override Dropbox remote path (defaults to `/todo.db`).
- `TELEGRAM_API_ID` & `TELEGRAM_API_HASH`: Telegram credentials.
- `TELEGRAM_SESSION_PATH`: Telegram session file location.

---

## Development & Testing

Run the test suite with pytest:

```bash
pytest
```
