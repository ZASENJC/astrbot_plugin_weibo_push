# AGENTS.md

Guide for agentic coding agents working in this repository.

## Project Overview

AstrBot plugin for monitoring Weibo (微博) accounts and pushing new posts to chat sessions.
Single-file architecture: all logic resides in `main.py` (~2266 lines).
Config schema in `_conf_schema.json`, metadata in `metadata.yaml`.

## Build / Lint / Test

### Dependencies

```bash
pip install -r requirements.txt
playwright install chromium   # optional, for screenshot feature
```

Dependencies: `httpx`, `beautifulsoup4`, `playwright` (optional).

### Lint / Format

No project-level linting or formatting configuration exists (no pyproject.toml, setup.cfg, tox.ini, or Makefile).
Use standard Python tooling when modifying code:

```bash
ruff check main.py
ruff format main.py
mypy main.py --ignore-missing-imports
```

### Tests

No test suite exists in this repository. There are no test directories or test files.
When adding tests, place them in a `tests/` directory at the project root and use `pytest`:

```bash
pytest                    # run all tests
pytest tests/test_foo.py  # run a single test file
pytest tests/test_foo.py::test_name  # run a single test
```

## Repository Structure

```
main.py               # All plugin code (classes, data models, entry point)
_conf_schema.json     # Plugin configuration schema (AstrBot config panel)
metadata.yaml         # Plugin metadata (name, version, author, repo)
requirements.txt      # Python dependencies
README.md             # User-facing documentation
CHANGELOG.md          # Version history
.gitignore
```

## Code Style Guidelines

### Architecture

- Single-file structure per AstrBot plugin convention. All code goes in `main.py`.
- Plugin entry point is class `Main(Star)` at module level, exported via `__all__ = ["Main"]`.
- Delegated components are classes defined in the same file:
  - `WeiboHttpClient` — HTTP requests and response validation
  - `WeiboPostParser` — Weibo post text/media/topic extraction and HTML cleaning
  - `MediaCacheManager` — cached file lifecycle (create, mark active/inactive, cleanup)
  - `RetryManager` — retry queue with exponential backoff + jitter
  - `MonitorRuleResolver` — rule resolution (manual subscriptions + auto-following)
  - `WeiboDeliveryService` — message rendering, media download, screenshot, delivery
  - `Main` — lifecycle, orchestration, command handlers

### Imports

- Standard library imports first (alphabetical), then third-party, then AstrBot SDK.
- Use explicit imports; avoid wildcard imports.
- Import from AstrBot SDK:
  ```python
  from astrbot.api import logger
  from astrbot.api.event import AstrMessageEvent, MessageChain, filter
  from astrbot.api.message_components import Image, Node, Nodes, Plain, Video
  from astrbot.api.star import Context, Star, StarTools
  ```

### Type Annotations

- Use `typing` module types: `Optional`, `List`, `Dict`, `Set`, `Tuple`, `Any`, `Callable`, `Awaitable`.
- All function signatures have full type annotations (parameters and return types).
- Use `dataclasses` for data containers (`@dataclass`, `@dataclass(frozen=True)`).

### Naming Conventions

- **Classes**: PascalCase (`WeiboHttpClient`, `MonitorRule`, `WeiboPost`).
- **Functions/methods**: snake_case (`extract_topics`, `send_chain_to_targets`).
- **Private methods**: single underscore prefix (`_safe_int`, `_load_state`).
- **Constants**: UPPER_SNAKE_CASE at module level (`DEFAULT_CHECK_INTERVAL_MINUTES`, `WEIBO_API_BASE`).
- **Compiled regex patterns**: UPPER_SNAKE_CASE (`UID_IN_URL_PATTERN`, `TOPIC_PATTERN`).
- **Config properties on Main**: snake_case properties matching config section names (`auth_config`, `monitor_config`).

### Formatting

- Indentation: 4 spaces.
- Line length: generally kept under ~120 characters.
- Blank lines: 2 blank lines between top-level classes/functions, 1 between methods.
- Trailing commas in multi-line collections.

### Error Handling

- Always re-raise `asyncio.CancelledError` — never swallow it:
  ```python
  except asyncio.CancelledError:
      raise
  except Exception as err:
      logger.error(f"WeiboMonitor: ...")
      return None
  ```
- Use specific exception types when possible (e.g., `binascii.Error`, `ValueError`, `json.JSONDecodeError`).
- Log all errors with `logger.error()` or `logger.warning()` prefixed with `WeiboMonitor:`.
- Return safe defaults on failure (`None`, `[]`, `""`, `0`) rather than raising.
- Use `logger.debug()` for verbose/routine messages, `logger.info()` for state changes, `logger.warning()` for degraded operation.

### Async Patterns

- All I/O-bound operations are `async`.
- Use `asyncio.gather()` for concurrent operations (e.g., sending to multiple targets).
- Use `asyncio.Queue` for the retry worker.
- Use `asyncio.Lock` for shared mutable state (e.g., browser lifecycle).
- Run CPU-bound work via `asyncio.to_thread()` (used for BeautifulSoup parsing).
- Background tasks are `asyncio.Task` instances created with `asyncio.create_task()`.

### Configuration Access

- Access config via `@property` methods on `Main` that return `Dict[str, Any]`:
  - `auth_config` → `config["auth_settings"]`
  - `monitor_config` → `config["monitoring_settings"]`
  - `content_config` → `config["content_settings"]`
  - `screenshot_config` → `config["screenshot_settings"]`
  - `runtime_config` → `config["runtime_settings"]`
- Always use `self._safe_int()` for integer config values with default, min, max bounds.
- Config schema is defined in `_conf_schema.json`.

### State Persistence

- State stored in `monitor_data.json` via `_state_get`, `_state_set`, `_state_update`.
- State file writes use atomic rename (write to `.tmp` then `replace()`).
- Corrupted state files are backed up with timestamp suffix.

### Command Handlers

- Use AstrBot decorators: `@filter.command("name")` and optionally `@filter.permission_type(filter.PermissionType.ADMIN)`.
- Command handlers are async generators (`yield event.plain_result(...)`).
- Validate inputs early and return error messages.

### Media / File Handling

- Cached files live in `{data_dir}/media_cache/`.
- Always mark files as active before sending, release after sending.
- Expire cached files older than 6 hours (`CACHE_RETENTION_SECONDS`).
- Use `uuid.uuid4().hex` for unique filenames.

### Comments

- Comments are written in Chinese (matching the project locale).
- Log messages use the prefix `WeiboMonitor:`.
- User-facing strings use Chinese text.
