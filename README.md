# Todo CLI

A fast, local-first, terminal-based personal task-management and course tracking system for Linux.

Built with Python, Click, Rich, SQLite, and Telethon. Designed to be visually quiet, responsive, keyboard-driven, and distraction-free.

---

## Highlights

- **Today's Dashboard**: Full-screen alternate-screen terminal interface showing tasks scheduled specifically for today, responsive progress tracking (`42% · 5/12`), and instant arrow/numeric navigation.
- **Strict Global Silence**: Zero transient status banners or toast chatter on routine actions. Toggle, delete, schedule, and navigate cleanly; only genuine errors are highlighted in red.
- **Chunky Tasks & Presets**: Consume large videos and books in manageable contiguous chunks with interval tracking (`00:20:00–00:40:00`), customizable presets, and "Entire remaining resource" options.
- **Pomodoro Timer**: Integrated interval focus timer with configurable work/break cycles, audio cues, and background timer ticking.
- **Timeline View**: Historical completion calendar and future day scheduling with day-by-day task inspection.
- **Universal Task Master List**: Maintain a reusable backlog of tasks independent of any date. Schedule universal tasks to Today without duplicating underlying records.
- **Filemeta Course Integration**: Import local course directories via `filemeta`. Extracts titles, durations, and page counts directly from media metadata without manual typing.
- **Native Telegram & YouTube Integration**: Browse course channels and YouTube playlists directly inside the TUI. Import video lessons into your local library, track completion, and schedule daily study sessions.
- **Cross-Device Client Mode (Termux / Android / Linux)**: Fully functional without mandatory scrapers or external binaries. Browse, toggle, and schedule cached items on mobile; optional tools (`telethon`, `yt-dlp`, `filemeta`) are only required for active rescans or network imports.
- **Safe SQLite Sync via rclone**: WAL checkpointing (`PRAGMA wal_checkpoint(TRUNCATE)`) on exit and dedicated `todo db-checkpoint` CLI command for clean, corruption-free syncing with Dropbox via `rclone`.

---

## Architecture Overview

```
todo/
├── pyproject.toml             # Package metadata and entry points
├── README.md                  # Project documentation
├── src/
│   └── todo/
│       ├── cli.py             # Click CLI commands & entrypoint
│       ├── config.py          # Configuration loader (TOML & XDG standards)
│       ├── database.py        # SQLite schema, migrations & connection pooling
│       ├── models.py          # Data models (Source, FileItem, Task, DailyTask)
│       ├── search.py          # Fuzzy search ranking and token matching
│       │
│       ├── integrations/
│       │   ├── filemeta.py    # Filemeta CLI adapter & file tree parser
│       │   └── telegram.py    # Telethon adapter with persistent asyncio event loop thread
│       │
│       ├── services/
│       │   ├── daily_service.py   # Daily scheduling, ordering & completion toggles
│       │   ├── source_service.py  # Source persistence, channel caching & two-way sync
│       │   └── task_service.py    # Core task lifecycle & universal tasks
│       │
│       └── ui/
│           ├── add_task_view.py   # Add task workflow router
│           ├── app.py             # Main application loop and screen manager
│           ├── console.py         # Rich console configuration & header rendering
│           ├── filemeta_view.py   # Filemeta source & course lesson browser
│           ├── key_reader.py      # Raw cbreak terminal reader & ANSI escape parser
│           ├── manual_view.py     # Manual task input view
│           ├── telegram_view.py   # Telegram imported library & channel picker
│           ├── today_view.py      # Main Today tasks dashboard
│           └── universal_view.py  # Universal task master list view
│
└── tests/                     # Comprehensive unit, integration, and E2E test suite
```

---

## Installation

### Requirements

- Python >= 3.11
- Linux (x86_64, aarch64) or Android (Termux)
- Optional extras: `telethon` (for Telegram), `yt-dlp` (for YouTube), `filemeta` CLI (for local directories)

### Quick Install (Linux Desktop / Server)

Clone the repository and install:

```bash
git clone https://github.com/Kai-Kodes/Extern.git
cd Extern
pip install -e .
```

To install optional scraper tools (`telethon` and `yt-dlp`):

```bash
pip install -e ".[full]"
```

Or using [pipx](https://pypa.github.io/pipx/):

```bash
pipx install .
```

### Termux on Android Installation

Extern runs in client mode on Termux without requiring external tools:

```bash
# 1. Update packages and install Python & Git
pkg update && pkg install -y python git rclone

# 2. Clone repository
git clone https://github.com/Kai-Kodes/Extern.git ~/Extern
cd ~/Extern

# 3. Install in editable mode
pip install -e .
```

All existing task libraries, cached video metadata, and progress can be scheduled and toggled on Android without heavy scraping dependencies.

---

## Usage

### Interactive TUI Dashboard

Launch the full-screen terminal interface:

```bash
todo
```

#### Today Dashboard Keybindings

| Key | Action |
| :--- | :--- |
| `↑` / `↓` | Navigate task list |
| `Enter` | Toggle task completion |
| `1` .. `9` | Directly toggle task #1..9 |
| `+` | Open Add Task menu (1. Filemeta, 2. Telegram, 3. YouTube, 4. Universal, 5. Manual) |
| `c` | Open Chunky Tasks view (chunk creation, presets, and gap progress) |
| `p` | Open Pomodoro Focus Timer |
| `l` | Open Timeline view (calendar completion history & future planning) |
| `e` | Edit selected task title, notes, or schedule |
| `d` | Remove selected task from Today's schedule |
| `m` | Move selected task to Universal backlog |
| `u` | Open Universal task master list |
| `f` | Open Filemeta course library |
| `t` | Open Telegram course library |
| `y` | Open YouTube playlist library |
| `q` / `Esc` | Quit cleanly (automatically truncates WAL) |

---

## Safe Cross-Device Sync (Dropbox via rclone)

To keep `todo.db` safely synced across your desktop machine and mobile Termux without SQLite WAL corruption:

### 1. Checkpoint WAL State

Extern automatically flushes and truncates SQLite WAL (`PRAGMA wal_checkpoint(TRUNCATE)`) on clean exit. You can also run it explicitly from the command line:

```bash
todo db-checkpoint
```

### 2. Synchronization Wrapper Script (`todo-sync.sh`)

Save this wrapper script on both your desktop and Termux to sync with Dropbox using `rclone`:

```bash
#!/usr/bin/env bash
set -euo pipefail

DB_LOCAL="${XDG_DATA_HOME:-$HOME/.local/share}/todo/todo.db"
DB_REMOTE="dropbox:Extern/todo.db"

# 1. Pull latest database from Dropbox
echo "Pulling latest todo.db from remote..."
rclone copyto "$DB_REMOTE" "$DB_LOCAL"

# 2. Checkpoint and run Todo
todo db-checkpoint
todo

# 3. Checkpoint WAL into single database file before upload
todo db-checkpoint

# 4. Push updated database back to Dropbox
echo "Pushing updated todo.db to remote..."
rclone copyto "$DB_LOCAL" "$DB_REMOTE"
echo "Sync complete."
```

Make it executable: `chmod +x ~/bin/todo-sync.sh`. Run `todo-sync.sh` whenever you want to start a session with automatic sync.

---

### Course Lesson Browsing (Filemeta & Telegram)

Both Filemeta and Telegram course views share a unified interaction model:

| Key | Action |
| :--- | :--- |
| `↑` / `↓` | Navigate lesson list |
| `Space` | Toggle multi-select checkbox (`[x]`) |
| `Enter` | Schedule selected lessons (or highlighted lesson) to Today |
| `t` | Toggle completion status (`✓` / `○`) in database |
| `n` | Jump to next unfinished lesson (with wrap-around) |
| `/` | Live fuzzy search filtering |
| `r` | Rescan / refresh lessons from source |
| `d` | Remove imported source from Todo (files & Telegram untouched) |
| `b` / `Esc` | Return to previous menu |

---

### Telegram Course Library Workflow

1. Press `t` from Today to open **Telegram Channels**.
2. See only channels already imported into your local library.
3. Select an imported channel with `Enter` to browse its cached lessons instantly (zero network latency).
4. Press `+` to open the **Channel Picker**:
   - Searches across all accessible dialogs in your Telegram session.
   - Filter channels with `/`.
   - Press `Enter` to import video lessons into your local SQLite library.
   - Press `r` to refresh the discovered channel list from Telegram.
5. Scheduled lessons display a `[Today]` badge and sync two-way with your daily tasks.

---

### Scriptable CLI Commands

Use `todo` directly in terminal scripts or shell aliases:

```bash
# Display today's tasks in text format
todo today

# Add a task to today's schedule
todo add "Review operating systems assignment"

# Add a task to the universal master list
todo add "Read Chapter 4 of Networking Book" --universal

# List all universal tasks
todo list

# List imported Filemeta course sources
todo sources

# Truncate and flush SQLite WAL for clean backup/sync
todo db-checkpoint
```

---

## Configuration

Configuration is loaded from `~/.config/todo/config.toml` (or `$XDG_CONFIG_HOME/todo/config.toml`):

```toml
[general]
db_path = "~/.local/share/todo/todo.db"

[filemeta]
command = "filemeta"

[telegram]
api_id = 12345678
api_hash = "your_telegram_api_hash_here"
session_path = "~/.config/todo/course_session"
```

### Environment Variables

Configuration values can be overridden via environment variables:

| Variable | Description |
| :--- | :--- |
| `TODO_DB_PATH` | Path to SQLite database file |
| `TODO_CONFIG_PATH` | Path to custom `config.toml` |
| `TELEGRAM_API_ID` | Telegram API ID (integer) |
| `TELEGRAM_API_HASH` | Telegram API Hash (string) |
| `TELEGRAM_SESSION_PATH` | Path to Telethon `.session` file |

---

## Testing

The project includes an extensive test suite covering models, database migrations, TUI key handling, ANSI parsing, Filemeta scanning, and Telegram integration (with strictly mocked network calls):

```bash
pytest
```

Run with verbose output:

```bash
pytest -v
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
