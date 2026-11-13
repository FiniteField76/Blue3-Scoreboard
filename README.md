## Update on Nov 14, 2026

Shared score entry is now available through a Windows client and a server running on the host computer. The latest Windows client is **v1.3.0**.

### What's New

- **Shared database:** remote submissions are saved to the host's existing SQLite database and appear in the original match records, rankings, and statistics.
- **Windows score-entry client:** run `Blue3-ScoreEntry.exe` to open the score-entry page in your default browser. Participants do not need to install Python, Go, or Node.js.
- **Multiplayer entry:** select an existing season and players, then enter each player's score and number of half-games. Scores must add up to zero. Half-game counts are recorded separately and do not multiply the score.
- **Fixed rate in the remote client:** new submissions use `rate = 1`. The rate input and the controls for creating seasons or players are not available in client v1.3.0. The host's local management and scoring features remain available.
- **Submission recovery:** if a score submission is interrupted, retry the original submission to confirm it without creating a duplicate. Unconfirmed submissions can also be recovered after restarting the client.

### Using the Shared Server

1. On the host computer, open `vibe-scoreboard` and run `start-server.bat`, or run `python server.py` with Python 3.10 or later.
2. Use the host's management page at `http://127.0.0.1:8765` to manage seasons, players, and records. The shared score-entry service uses port `8766`. Do not run `start.bat` and `start-server.bat` at the same time with the default ports.
3. On a participant's Windows 10/11 64-bit computer, extract `Blue3-ScoreEntry-Windows.zip`, run the EXE, and enter the server address and access code provided by the host.

For a local network, use the host's LAN address, such as `http://192.168.1.100:8766`. Access from another network requires a VPN or an HTTPS tunnel to the score-entry service. Keep the host computer and any required tunnel running while others are recording scores.

When updating the client, finish or confirm any pending submission, click **退出客户端** (Exit Client) in the old page, and then open the new EXE. The page displays **客户端 1.3.0** (Client 1.3.0) so you can confirm which version is running.



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

For detailed instructions and scoring rules, see the [User guide](vibe-scoreboard/README.md).

Bug reports and feature suggestions are welcome through GitHub Issues. Please include steps to reproduce the problem, your operating system, and relevant screenshots.

## AI-Assisted Development

Most of the code and Chinese documentation was generated with GPT-6.1 Sol and GPT 6 Astra, guided by prompts and iterative feedback. Further features and improvements are planned.
