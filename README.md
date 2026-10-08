# Todo CLI

A local-first, terminal-based personal task-management system with **Filemeta** integration for Linux.

---

## Features

- **Today's Focus**: View tasks selected specifically for today, toggle their completion with simple numbers (`1`, `2`, ...), and see immediate status feedback.
- **Universal Task Master List**: Reusable tasks that exist independently of any given day. Selecting a universal task for today links it to the day without duplicating the underlying task entity.
- **Filemeta Integration**: Connect existing local course folders via the system `filemeta` command. Extract video and file metadata without typing lesson names or altering original file names.
- **Duplicate Prevention**: Re-importing a folder updates existing records and rescans new/modified items instead of creating duplicate sources or tasks.
- **Local-First SQLite Database**: Self-contained SQLite database stored at `~/.local/share/todo/todo.db` with WAL mode and foreign keys enabled.
- **Interactive UI & Scriptable CLI**: Polished Rich terminal interface when launched as `todo`, plus convenient scriptable commands like `todo today`, `todo add`, `todo list`, and `todo sources`.

---

## Architecture

```
todo/
├── pyproject.toml
├── README.md
├── src/
│   └── todo/
│       ├── __init__.py
│       ├── cli.py             # Click CLI entrypoint & commands
│       ├── database.py        # SQLite schema & connection management
│       ├── models.py          # Dataclass models (Source, FileItem, Task, DailyTask)
│       ├── config.py          # Config loader (TOML & XDG standards)
│       │
│       ├── services/
│       │   ├── task_service.py    # Core task lifecycle & universal tasks
│       │   ├── daily_service.py   # Daily scheduling & completion toggles
│       │   └── source_service.py  # Filemeta source & item persistence
│       │
│       ├── integrations/
│       │   └── filemeta.py        # Filemeta CLI adapter & tree parser
│       │
│       └── ui/
│           ├── console.py         # Shared console & rendering helpers
│           ├── today_view.py      # Main Today tasks dashboard
│           ├── add_task_view.py   # Add Task router menu
│           ├── filemeta_view.py   # Source listing, folder import, item selection
│           ├── universal_view.py  # Universal task list & selector
│           ├── manual_view.py     # Manual task input flow
│           └── app.py             # Interactive application controller
│
└── tests/
```

---

## Installation

Install in editable mode:

```bash
pip install -e /home/redsheronin/todo --break-system-packages
```

Or using pipx:

```bash
pipx install /home/redsheronin/todo
```

This installs the `todo` command into your PATH (`~/.local/bin/todo`).

---

## Usage

### Interactive Mode

Simply run:

```bash
todo
```

#### Controls:
- `1..N`: Toggle completion of task #1..N.
- `+`: Open the **Add Task** menu:
  - `1. Filemeta`: View imported course folders or add a new folder to scan.
  - `2. Universal task`: Select a task from your master list to schedule today.
  - `3. Manual task`: Enter a task and choose to add to Today or Universal list.
- `u`: View Universal task list directly.
- `q`: Quit.

### Scriptable Commands

```bash
# Print today's tasks
todo today

# Add a manual task to today
todo add "Complete DBMS assignment"

# Add a task to the universal list
todo add "Study DSA" --universal

# List all universal tasks
todo list

# List imported Filemeta sources
todo sources
```

---

## Configuration

Optionally configure `todo` at `~/.config/todo/config.toml`:

```toml
[general]
db_path = "~/.local/share/todo/todo.db"

[filemeta]
command = "filemeta"
```

Environment variables:
- `TODO_DB_PATH`: Override database path (useful for testing or custom environments).
- `TODO_CONFIG_PATH`: Override configuration file path.

---

## Running Tests

```bash
pytest /home/redsheronin/todo/tests
```
