# Blue3-Scoreboard

An easy-to-use local scoreboard for recording and analyzing three-player mahjong matches. It also supports multiplayer score entry, season management, and historical data imports.

Current version: v1.0.0.

## Features

- Automatic score calculation from points, chips, additional scores, and a configurable rate.
- Competition and season management, with player rankings and cumulative scores.
- Player statistics and head-to-head records, including wins, draws, losses, and score transfers.
- Cumulative score charts with personal and global match progression, plus 1920 x 1080 PNG export.
- Excel and CSV imports and exports, with SQLite database backups.

## Getting Started

Python 3.10 or later is required. No third-party Python packages are needed.

1. Download and extract the repository.
2. Open the `vibe-scoreboard` folder.
3. On Windows, double-click `start.bat`. On macOS or Linux, run `bash start.command`.

The application will open in your browser at `http://127.0.0.1:8765`. Keep the terminal window open while using it. If it fails, try other ports like `http://127.0.0.1:8000`.

Data is stored locally. Use the built-in backup feature regularly, and preserve the `data` folder when updating the application. The online version is currently in development and will launch soon.

## Documentation and Feedback

For detailed instructions and scoring rules, see the [Chinese user guide](vibe-scoreboard/README.md).

Bug reports and feature suggestions are welcome through GitHub Issues. Please include steps to reproduce the problem, your operating system, and relevant screenshots.

## AI-Assisted Development

Most of the code and Chinese documentation was generated with GPT-6.1 Sol, guided by prompts and iterative feedback. Further features and improvements are planned.
