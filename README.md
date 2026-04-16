# Faceit Scout — Dota 2 Draft Analyzer

A lightweight desktop tool and web interface for scouting Faceit Dota 2 match rooms.  
Displays each player's Dota 2 rank, primary position, Faceit ELO/level, and links their OpenDota profile — all fetched in parallel.

---

## Features

- **Match room lookup** — paste a Faceit room ID or full room URL to load both teams instantly
- **Dota 2 rank** — fetched from OpenDota (Herald → Immortal with leaderboard position)
- **Primary & secondary position** — calculated from the last 20 matches using lane role + GPM heuristics
- **Faceit ELO & level** — fetched from the Faceit Data API
- **Rank icons** — Dota 2 seasonal rank medal images rendered inline
- **Captain badge** — highlights the team captain in each lineup
- **Draft mode** — when a match is in `CAPTAIN_PICK` status, the view switches to a three-column layout: captain left, captain right, and a shared player pool sorted by rank in the center
- **Live polling** — during an active draft, the app polls every 15 seconds and auto-fetches OpenDota data for newly added players (using a local cache to avoid re-fetching)
- **Rank sorting** — players are sorted highest rank first within each team
- **System tray** — minimize the launcher to the Windows system tray; double-click or use the tray menu to restore
- **Single instance** — a Windows Mutex prevents multiple copies from running simultaneously
- **Dark themed UI** — both the launcher (Tkinter) and the web page use a matching dark palette

---

## Requirements

- **Python 3.10+**
- Dependencies listed in `requirements.txt`:

```
aiohttp>=3.9
rich>=13.0
fastapi>=0.110
uvicorn>=0.29
pystray>=0.19
pillow>=10.0
```

---

## Running Without Building (Python)

1. **Clone or download** the repository.

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Launch the GUI:**
   ```bash
   python launcher.py
   ```

4. The launcher window will appear. Click **ЗАПУСТИТИ** (or press Enter after pasting a room ID/URL).  
   The web interface will open automatically in your default browser at `http://127.0.0.1:8000`.

> You can also run the server directly without the GUI:
> ```bash
> python server.py
> ```
> Then open `http://127.0.0.1:8000` manually.

---

## Building the .exe

The project uses [PyInstaller](https://pyinstaller.org/) with a pre-configured spec file.

### Quick build (Windows)

Double-click `build.bat` or run it from a terminal:

```bat
build.bat
```

This will:
1. Install PyInstaller via `pip install pyinstaller`
2. Run `pyinstaller faceit_scout.spec --clean`
3. Output the executable to `dist\FaceitScout.exe`

### Manual build

```bash
pip install pyinstaller
pyinstaller faceit_scout.spec --clean
```

The spec file bundles:
- `launcher.py` as the entry point
- `index.html` and the `photo/` folder (rank medal images) as data files
- All necessary hidden imports for uvicorn, anyio, pystray, and Pillow

The result is a **single-file `.exe`** (`console=False`, no terminal window) that runs completely standalone — no Python installation required on the target machine.

---

## Project Structure

```
faceit-checker/
├── launcher.py          # Tkinter GUI launcher + uvicorn server management
├── server.py            # FastAPI backend (match/player API endpoints)
├── faceit_checker.py    # CLI tool (terminal table output via Rich)
├── index.html           # Web UI (single-page, vanilla JS)
├── photo/               # Dota 2 rank medal images (.webp)
├── requirements.txt     # Python dependencies
├── faceit_scout.spec    # PyInstaller build spec
└── build.bat            # One-click build script (Windows)
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serves `index.html` |
| `GET` | `/api/match/{room_id}` | Full match data — both teams with ranks and positions |
| `GET` | `/api/match/{room_id}/poll` | Lightweight poll — Faceit data only, no OpenDota |
| `GET` | `/api/player/{player_id}` | Full data for a single player by Faceit player ID |

---

## CLI Usage

For terminal-only use without the GUI or web server:

```bash
# By match room ID
python faceit_checker.py --match 1-20ca85e4-1574-4705-b8b4-d1dce9938484

# By player nicknames or profile URLs
python faceit_checker.py --players Player1 Player2 Player3
```

Output is a Rich table with Faceit name, Dota rank, primary position, and OpenDota link.

---

## Credits

Created by **[Chasil](https://steamcommunity.com/id/Chasil/)**
