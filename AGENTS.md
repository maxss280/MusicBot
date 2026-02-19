# AGENTS.md - MusicBot Codebase Guidelines

## Quick Start
- **Python Version**: 3.11+ (minimum 3.10)
- **Main Branch**: `master` - active development, may break
- **Stable Branch**: `Stable-1.0.0` - first stable release, for production use
- **Git Remote**: `maxss280` (points to https://github.com/maxss280/MusicBot)
- **Project Root**: `/workspace`
- **Core Module**: `musicbot/` contains all bot logic
- **Test Framework**: No automated tests; manual testing via Discord commands
- **Branching**: All changes push directly to `review` branch, then to `master` when tested

## Changelog

### Review Branch (Testing)
- **Voice Connection Fix**: Added voice client connection check before playback in `player.py:579`
  - Prevents `ClientException('Not connected to voice.')` errors when voice disconnection occurs between songs
  - Checks `self.voice_client.is_connected()` before attempting `play()`
  - Gracefully handles disconnection by calling `_playback_finished()` and returning early
  - Allows auto-reconnection logic to take over without exceptions

### Stable-1.0.0
- **Audio Stuttering Fix**: Fixed audio stuttering by implementing in-memory EQ processing with `InMemoryAudioSource`
  - When `LoadAudioIntoMemory=True`, audio is now played using `InMemoryAudioSource` directly
  - The `InMemoryAudioSource` class now supports `loudnorm_gain` parameter
  - Loudnorm EQ offset (in dB) is extracted from FFmpeg and converted to linear gain
  - Gain is applied to each 16-bit PCM sample during playback
  - This bypasses FFmpeg's stdin buffering which caused stuttering
  - Loudnorm gain extraction and application to PCM16 audio samples

### Agent Development
- **agents_playground/** - Untracked folder for agent-related work, documentation, and experiments
  - Store agent documents, plans, notes here
  - Not committed to Git repository

### Cache Shortcut
When a YouTube video is already cached in the audio cache folder, MusicBot **short-circuits** the entire yt-dlp extraction process:
1. **Early cache check** - Before any network request, checks if file exists in cache
2. **Metadata sidecar** - Stores title/duration in a `.json` sidecar file alongside cached audio
3. **No authentication required** - Completely bypasses extraction for cached files

This provides:
- Faster playback for cached content
- No unnecessary network requests
- Duration extracted via ffprobe on first play if metadata sidecar is missing

## Directory Structure
```
/workspace/
├── musicbot/          # Core bot module (19 Python files)
├── musicbot/lib/      # Internal libraries (event_emitter.py)
├── config/            # Configuration files (INI, i18n, playlists)
├── data/              # Runtime data (guild-specific JSON, server info)
├── audio_cache/       # Downloaded media storage
├── logs/              # Log files
├── run.py             # Main entry point (1149 lines)
├── run.sh/run.bat     # Launch scripts
└── update.py          # Update wrapper
```

## Build & Development Commands

### Environment Setup
```bash
# Install dependencies
pip install -r requirements.txt

# Install pre-commit hooks
pip install pre-commit
pre-commit install
```

### Linting & Formatting
**Note**: Project uses flake8 and Black with specific rules. Type checking with mypy is optional but recommended.

```bash
# Run flake8 (primary linter)
flake8 musicbot/ --max-line-length=99 --max-complexity=18

# Run Black formatter (auto-fix to 88 chars for compatibility)
black musicbot/ --line-length=88 --target-version=py311

# Check Black formatting without changes (CI)
black musicbot/ --line-length=88 --target-version=py311 --check

# Run pre-commit on staged files
pre-commit run --all-files

# Run mypy type checking (optional)
mypy musicbot/ --ignore-missing-imports
```

### Testing
**Note**: This project has no automated test suite. Manual testing required:
- Test commands in Discord using `run.py`
- Verify config files in `config/` directory
- Check logs in `logs/musicbot.log`
- Run `flake8` and `black --check` before committing

## Code Style Guidelines

### Imports
```python
# Standard library first
import asyncio
import logging
import os

# Third-party
import discord
import aiohttp

# Local (musicbot) - relative imports only
from .constants import VERSION
from .exceptions import HelpfulError
from .utils import format_size_from_bytes
```

**Rules**:
- Always use relative imports within `musicbot/` module
- Group imports: stdlib → third-party → local
- Type imports in `TYPE_CHECKING` blocks
- Add `# type: ignore[...]` for known issues (e.g., `import-untyped`)

### Naming Conventions
| Type | Convention | Examples |
|------|------------|----------|
| Classes | PascalCase | `MusicBot`, `Playlist`, ` downloader.py` |
| Functions | snake_case | `play_command()`, `format_size_from_bytes()` |
| Variables | snake_case | `voice_client`, `current_song` |
| Constants | UPPER_CASE | `VERSION`, `DISCORD_MSG_CHAR_LIMIT` |
| Async Functions | snake_case + async | `async def add_entry(...)` |
| Protected | Single underscore | `_func_prototype()` |
| Private | Double underscore | `__private_attr` |

### Type Hints
```python
# Required for function signatures
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

# For TYPE_CHECKING blocks (imports not executed at runtime)
if TYPE_CHECKING:
    from .bot import MusicBot
    from .playlist import Playlist

# Type aliases for readability
EntryTypes = Union[URLPlaylistEntry, StreamPlaylistEntry]
AsyncTask = asyncio.Task[Any]
```

### Error Handling
```python
# Use custom exceptions hierarchy
from .exceptions import (
    MusicbotException,
    CommandError,
    ExtractionError,
    HelpfulError,
    PermissionsError,
)

# Example: User-friendly error messages
async def play_command(self, message):
    try:
        # ... command logic
    except ExtractionError as e:
        raise HelpfulError(
            issue=f"Could not extract audio: {str(e)}",
            solution="Check the URL or try a different source",
            expire_in=30
        ).raise_()
```

**Error Classes**:
- `MusicbotException`: Base exception with `expire_in` for auto-deletion
- `CommandError`: Command processing failures
- `ExtractionError`: YouTube/yt-dlp extraction failures
- `PermissionsError`: Insufficient permissions (auto-formats message)
- `HelpfulError`: User-facing with formatted problem/solution sections
- `FFmpegError/FFmpegWarning`: FFmpeg-specific errors

### Logging
```python
log = logging.getLogger(__name__)

# Use appropriate levels
log.info("General information")
log.warning("Warning: %s", variable)
log.error("Error: %s", variable)
log.critical("Critical failure")
log.debug("Debug info")
log.voice("Voice connection details")
log.ffmpeg("FFmpeg subprocess output")
log.noise("Extremely verbose debug")
```

**Rules**:
- Always use `log = logging.getLogger(__name__)`
- Use `%` formatting for variables, not f-strings in logging
- Log at appropriate levels (debug for internals, info for user actions)

### Formatting Standards
- **Line length**: 99 chars (flake8), 88 chars (Black)
- **Max complexity**: 18 (flake8 C901)
- **Strings**: Use double quotes except when avoiding escaping
- **Type hints**: Use annotations, not comments
- **Blank lines**: 1 between functions, 2 between top-level definitions

### Async/Await Patterns
```python
# Always use async/await (no callbacks)
async def fetch_data(url: str) -> Dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return await response.json()

# Cancel tasks safely
task = asyncio.create_task(some_coroutine())
try:
    await task
except asyncio.CancelledError:
    # Cleanup if needed
    pass
```

## Git Workflow

This repository uses a local fork of MusicBot with remote `maxss280`.

### Adding/Changing Remotes
If you need to update the remote URL:
```bash
git remote set-url maxss280 https://new-url-here.com/repo.git
```

### Common Git Operations
```bash
# View current remotes
git remote -v

# Add a new remote
git remote add <name> <url>

# Remove a remote
git remote remove <name>

# Fetch from all remotes
git fetch --all

# Push to maxss280 remote
git push maxss280 <branch>
```

### Git Branches
- **`master`**: Active development branch - may be unstable, breaking changes accepted
- **`Stable-1.0.0`**: First stable release branch - for production use, only critical patches
- **`review`**: Testing branch for all changes before merging to master

### Common Git Workflow
```bash
# 1. Configure commit author (run once per session)
git config user.name "Maxss280"
git config user.email "17280185+maxss280@users.noreply.github.com"

# 2. Update review branch and create working branch from it
git checkout review
git pull maxss280 review

# 3. Make changes and commit directly to review (no feature branches needed)
git add .
git commit -m "fix: Add voice connection check before playback"

# 4. Test locally
python run.py --no-checks  # Skip optional checks
# Test commands in Discord

# 5. Format and lint before pushing
black musicbot/
flake8 musicbot/

# 6. Push directly to review branch
git push maxss280 review
```

### Branch Operations

**Branching Strategy for Releases:**
- **Review**: Test all fixes and features here before merging to master
- **Master**: Merge stable changes from review
- **Stable-1.0.0**: Only critical patches - NEVER features or major changes

**Pushing workflow:**
1. Update review: `git checkout review && git pull maxss280 review`
2. Make changes and commit directly to review
3. Push to review: `git push maxss280 review`
4. Test on review branch
5. When stable-ish, merge review to master
6. Only critical patches go to Stable-1.0.0 directly from master

**Create and push stable branch:**
```bash
# From master or desired commit
git checkout -b Stable-1.0.0
git push maxss280 Stable-1.0.0

# Create and push tag
git tag v1.0.0
git push maxss280 v1.0.0
```

**Switch to stable branch:**
```bash
git fetch maxss280
git checkout Stable-1.0.0
git pull maxss280 Stable-1.0.0
```

**Commit Message Types**:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `refactor`: Code restructuring
- `chore`: Maintenance tasks

## Configuration Files
- **INI files**: Use `configupdater` library (preserves comments)
- **JSON files**: Use `musicbot.json.Json` class
- **i18n files**: UTF-8 JSON in `config/i18n/`

## Common Patterns

### Serializable Objects
```python
from .constructs import Serializable

class MyObject(Serializable):
    def serialize(self) -> Dict[str, Any]:
        return {"attr": self.attr}
    
    @classmethod
    def deserialize(cls, data: Dict[str, Any], bot: "MusicBot"):
        obj = cls(bot)
        obj.attr = data["attr"]
        return obj
```

### Command Decorators
```python
from .utils import owner_only, dev_only

@owner_only  # Only bot owner can invoke
async def admin_command(self, message):
    pass

@dev_only  # Only dev IDs in config can invoke
async def debug_command(self, message):
    pass
```

### Permissions System
```python
from .permissions import Permissions

# Check user permissions
if not self.permissions.has(message, Permissions.use_speak):
    raise PermissionsError("You don't have permission to speak")

# Check specific permission
if self.permissions.check(message, "skip", 3):
    # Skip requires 3 votes
    pass
```

## Important Constants
```python
from musicbot.constants import (
    VERSION,                    # Git-derived version string
    DISCORD_MSG_CHAR_LIMIT,    # 2000 (Discord message limit)
    DEFAULT_LOG_LEVEL,        # "INFO"
    DEFAULT_VOLUME,           # 0.25 (25%)
)
```

## Docker Development
```bash
# Build image
docker build -t musicbot .

# Run with config
docker run -v $(pwd)/config:/musicbot/config musicbot

# Debug mode
docker run -it musicbot sh -c "python run.py --log-level DEBUG"
```

## Testing Checklist & Common Pitfalls

**Testing**: Use `run.py` with Discord commands - no automated tests exist.
**Pre-commit**: Run `flake8` and `black --check` before committing.
**Common Issues**: Memory leaks (cache cleanup), voice disconnections, rate limits, missing `TYPE_CHECKING` imports, `print()` usage, error formatting.

### Voice Disconnection Errors

If you see `ClientException('Not connected to voice.')` errors followed by auto-reconnection:
- **Cause**: Voice connection drop between songs (Discord API disconnection or network heartbeat failure)
- **Fix**: Add `if not self.voice_client.is_connected():` check before `play()` call in `player.py`
- **Expected behavior**: Gracefully call `_playback_finished()` and let auto-reconnection handle it

## References

### Core Libraries
- [Discord.py Docs](https://discordpy.readthedocs.io/) - Main Discord API wrapper
- [yt-dlp Docs](https://github.com/yt-dlp/yt-dlp) - Media extraction/downloading
- [aiohttp Docs](https://docs.aiohttp.org/) - Async HTTP client/server

### Development Tools
- [Black Formatter Docs](https://black.readthedocs.io/) - Code formatter
- [Flake8 Docs](https://flake8.pycqa.org/) - Style linter
- [Bandit Docs](https://bandit.readthedocs.io/) - Security linter
- [Mypy Docs](https://mypy.readthedocs.io/) - Type checker

## Bug Fix Process

When working on bug fixes, follow this iterative debugging workflow:
Can use agent_playground folder for these test scripts.
1. **Identify the issue**: Analyze error messages, logs, and user reports to understand the problem
2. **Reproduce the issue**: Create a consistent scenario to reliably trigger the bug Doesn't have to be full rerun could be small script to verify the error you are experiencing is from the issue identified. 
3. **Validate the fix approach**: Confirm the proposed solution makes sense and addresses the root cause. Fixing the test script is possablity. 
4. **Implement the fix**: Apply the coding changes
5. **Verify the fix**: Test that the issue is resolved and no new issues are introduced
6. **Iterate if needed**: If the issue persists, evaluate whether to:
   - Refine the current approach
   - Revert and try a different solution
   - Loop back to step 1 with a new hypothesis

Continue iterating through this process until the bug is successfully resolved.

### Project Resources
- [Project Documentation](https://just-some-bots.github.io/MusicBot/)
- [Original Repo](https://github.com/Just-Some-Bots/MusicBot)
- [PyPI Package Index](https://pypi.org/project/discord.py/)
- [yt-dlp PyPI](https://pypi.org/project/yt-dlp/)
