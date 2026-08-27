# Plex Library Tool

A single Python script that scans your Movies / TV Shows folders, looks each one up on [TMDb](https://www.themoviedb.org/), and renames everything into clean, Plex-friendly names: folders, video files, season folders, and subtitles.

No installation, no dependencies, nothing to compile. It's one `.py` file that runs anywhere Python runs: Mac, Windows, or Linux.

This guide assumes you've never used Python or GitHub before. If you already know your way around both, skip to [Quick Start](#quick-start).

---

## What it does

- Matches your existing folder names against TMDb and renames them to `Movie Name (Year)` / `Show Name (Year)` format
- Renames video files to match, and organizes TV episodes into `S01`, `S02`, etc. season folders
- Detects loose movie or TV episode files sitting directly in the share root (no folder of their own) and organizes them into a proper movie or show folder
- Detects a duplicate show folder for a show you already have (e.g. a separately-downloaded "Show S02" folder) and merges its episodes into the existing show folder instead of creating a second one
- Checks your organized TV shows against TMDb's episode list and reports any already-aired episodes you're missing (`-e`), without changing anything
- Understands anime-style absolute episode numbering (episodes numbered 1, 2, 3... straight through instead of per-season) when TMDb has that show's episode order data, converting them to the correct season/episode automatically
- Finds subtitle files, figures out which one matches your primary language (by filename, and by reading the file's content/metadata if the filename doesn't say), and renames it to match the video. Defaults to English, but follows whatever language you've set for TMDb results (see [Non-English users](#non-english-users)).
- Optionally cleans up junk files/folders (samples, `.nfo`, `.txt`, screenshots, unwanted-language subtitles) into a local trash folder. Nothing is deleted permanently, and every cleanup can be reversed.
- Fully customizable naming convention (`names.yaml`): change `S01` to `Season 01`, use dots instead of spaces, uppercase everything, rename the "Subs" folder to something else, etc.
- Every rename is logged, and can be undone with one command
- Works on Mac, Linux, and Windows

Nothing this script does is destructive by default. It asks for confirmation before renaming anything (unless you pass `-y`), and everything it changes is logged so it can be undone.

---

## Requirements

- **Python 3.8 or later.** That's it. No other software or packages are required.
- **A free TMDb account**, to get an API key. Takes about 2 minutes, and is covered below.

---

## Quick Start

### 1. Install Python

**Windows 11:**

Open PowerShell (search "PowerShell" in the Start menu) and run:

```
winget install Python.Python.3.14
```

Close and reopen PowerShell afterward, then check it worked:

```
python --version
```

If `winget` doesn't work on your machine, download the installer from [python.org/downloads](https://www.python.org/downloads/) instead. **Important:** on the first screen of the installer, check the box that says **"Add python.exe to PATH"** before clicking Install. This is the single most common thing people miss, and without it Windows won't recognize the `python` command.

**Mac:**

Macs usually already have Python 3 installed. Open Terminal (search "Terminal" in Spotlight) and run:

```
python3 --version
```

If that fails, download the installer from [python.org/downloads](https://www.python.org/downloads/) and run it.

**Linux:**

Almost every Linux distribution comes with Python 3 preinstalled. Confirm with:

```
python3 --version
```

If it's missing, install it with your distro's package manager, e.g. `sudo apt install python3` on Ubuntu/Debian.

### 2. Download this repository

If you've never used GitHub before: this page is a "repository" (a folder of files), and you don't need a GitHub account or `git` installed just to download it.

1. Click the green **"Code"** button near the top of this page
2. Click **"Download ZIP"**
3. Once it's downloaded, extract/unzip it somewhere you'll remember (e.g. your Desktop or Downloads folder)

(If you do have `git` installed and prefer it, `git clone` this repository's URL instead.)

### 3. Get a free TMDb API key

The script uses [TMDb](https://www.themoviedb.org/) (The Movie Database) to look up correct titles and years. This is free.

1. Log in or create a free account: <https://www.themoviedb.org/login>
2. Request an API key: <https://www.themoviedb.org/settings/api/request>
3. Choose **"Developer"** when asked what type of key
4. Fill in the short form (you can use placeholder info for personal/non-commercial use)
5. On the resulting settings page, copy the value labeled **"API Key"** (the short one, **not** the longer "API Read Access Token")

You don't need to do anything with this key yet. The script will ask for it the first time you run it.

### 4. Run it

Open a terminal in the folder you extracted:

- **Windows:** open the extracted folder in File Explorer, click the address bar, type `powershell`, and press Enter
- **Mac/Linux:** open Terminal, then `cd` into the folder, e.g. `cd ~/Downloads/plex-library-tool`

Then run:

```
python plex-library-tool.py -r
```

(On Mac/Linux, use `python3` instead of `python` if `python` isn't recognized.)

The first time you run it, it'll ask you to paste in the TMDb API key from step 3, and offer to save it to a local `.env` file so you're never asked again. That file stays on your computer and is never uploaded anywhere.

### 5. Point it at your media

After the API key step, the script will either:
- automatically find SMB/network shares mounted on your computer and let you pick one, or
- you can skip that entirely by giving it a path directly:

```
python plex-library-tool.py -r "/path/to/your/Movies"
```

```
python plex-library-tool.py -r "D:\Movies"
```

It'll then walk through each folder, look it up, show you the proposed rename, and ask for confirmation before doing anything.

**Tip:** before renaming anything for real, run it with `-t` (test mode) first to preview what it *would* do without changing anything:

```
python plex-library-tool.py -r "/path/to/your/Movies" -t
```

---

## Command reference

| Flag | What it does |
|---|---|
| `-r`, `--rename [PATH]` | Scan and rename a share. Pass a path to skip the share-selection prompt. |
| `-c`, `--cleanup [PATH]` | Move junk files/folders (per `delete.yaml`) to a local trash folder. |
| `-e`, `--episodes [PATH]` | Check TV shows against TMDb's episode list and report any missing (already-aired) episodes. Read-only, makes no changes. |
| `-t`, `--test [N]` | Preview only. No changes are made. Optionally limit how many folders are shown. |
| `-y`, `--yes` | Don't ask for confirmation before each rename. |
| `-f`, `--force` | Force a full scan even if nothing looks like it changed since the last run. |
| `-v`, `--verbose` | Print detailed diagnostic output about what the script is doing and why. |
| `-m`, `--manual-rename CURRENT NEW` | Manually rename one specific folder/file, bypassing TMDb entirely. |
| `--backup` | Snapshot current names to a log file without changing anything. |
| `--restore [LOGFILE]` | Undo a previous run using its log file. Pick from a list if no file is given. |
| `-u`, `--undo` | Instantly undo the most recent run, no need to look up a log filename. Can't be combined with any other flag. |
| `-T`, `--type movies\|tv` | Manually set the library type instead of auto-detecting or prompting. |
| `--service [N\|PATH]` | Run rename + cleanup automatically in the background. See [Running as a service](#running-as-a-service) below. |

Flags can be combined, e.g. `-rc` runs rename and cleanup back to back on the same share, `-rf` forces a full rename scan, `-ty` previews everything without prompting.

---

## Running as a service

Instead of running the script by hand every time you add something, `--service` runs it automatically in the background, on a timer, so newly added movies/shows get renamed within a minute or two of showing up.

```
python plex-library-tool.py --service                    # pick share(s) interactively, then start
python plex-library-tool.py --service "/Volumes/Movies"  # add a share and start (default: every 60s)
python plex-library-tool.py --service 120                # set the interval to 120 seconds and start
python plex-library-tool.py --service start               # start using the current configuration
python plex-library-tool.py --service stop                # stop the service
```

Each cycle runs the equivalent of `-rcy` (rename + cleanup, no prompts) on every configured share. It runs as a detached background process, so it keeps running after you close the terminal, at a lowered OS priority so it yields to things like Plex during playback. Progress is written to `logs/service.log` instead of the screen. Settings (shares + interval) are stored in `service.json` next to the script; you can add more shares later by running `--service <path>` again while it's running, no need to stop it first.

Both rename and cleanup skip folders that haven't changed since the last pass, so most cycles do little more than a quick unchanged-folder check rather than a full rescan.

The service reuses your saved TMDb API key and language from `.env`, so run the script normally at least once first if you haven't already.

---

## Configuration files

These live alongside the script and are all optional. The script works out of the box with sensible defaults. Every file is fully documented with comments and examples inside it.

- **`names.yaml`**: customize the naming convention. Folder/file name format, season folder naming (`S01` vs `Season 01`), separators (spaces vs dots vs underscores), uppercase/lowercase, whether to include resolution tags like `1080p`, and the subtitle folder name (e.g. rename "Subs" to "Subtitles" or any word in your own language).
- **`delete.yaml`**: what cleanup moves to trash. Folder name patterns (e.g. `sample`, `extras`), file name/extension patterns, and subtitle language rules (e.g. "only keep Spanish subtitles").

---

## Non-English users

TMDb can return movie/show titles in your own language instead of English (e.g. Spanish, French, German titles). The first time you run the script, it detects your system's language and asks you to confirm before saving it to `.env`. You can also set it yourself at any time by adding a line to `.env`:

```
TMDB_LANGUAGE=es-MX
```

Use any TMDb-supported language code (ISO 639-1, optionally with a region, e.g. `en-US`, `fr-FR`, `de-DE`, `ja-JP`). This also decides which subtitle language is treated as primary: whichever subtitle file matches `TMDB_LANGUAGE` gets renamed to match the video (e.g. `Movie.es.srt` for Spanish), and everything else keeps its original filename. The script's own prompts and status messages (like "Renamed folder:") stay in English regardless of this setting.

---

## Safety

- **Nothing is renamed without asking first**, unless you pass `-y`.
- **Every rename is logged** to a `logs/` folder created next to the script.
- **Cleanup never permanently deletes anything.** Matched files/folders are moved to a local `.trash/` folder, not deleted, and you're prompted per item unless `-y` is used.
- **Undo anytime** with `-u` (most recent run) or `--restore <logfile>` (any past run).
- **Your TMDb API key is never uploaded anywhere.** It's stored locally in a `.env` file, which is excluded from Git via `.gitignore`.

---

## Disclaimer

This script is provided as-is, with no warranty. Always test with `-t` before renaming for real, and keep backups of anything irreplaceable. The author is not responsible for any lost, corrupted, or misplaced files resulting from the use of this script.

Renaming a file changes its modification time, which Plex uses to decide when to re-analyze a video for things like intro/credit markers. After renaming a large batch of files, Plex may re-scan and re-analyze them, which can spike your Plex server's CPU usage for a while. This is normal and temporary; performance should return to normal once Plex finishes catching up, and is usually tolerable even for large libraries.

---

## Troubleshooting

**`'python' is not recognized as an internal or external command`** (Windows)
Python isn't on your PATH. Reinstall using the winget command above, or rerun the python.org installer and make sure "Add python.exe to PATH" is checked.

**`No SMB shares found.`**
The script couldn't auto-detect a network share. Pass the path directly instead: `python plex-library-tool.py -r "Z:\Movies"` (Windows) or `python plex-library-tool.py -r "/Volumes/Movies"` (Mac).

**`No match found (Ignoring): <name>`**
TMDb couldn't confidently match that folder to a title. This is intentionally conservative: the script would rather skip a folder than rename it wrong. You can rename it manually with `-m "current name" "new name"`, or clean up the raw folder name a bit and try again.

**I want to undo something**
Run `python plex-library-tool.py -u` to instantly undo the most recent run, or `python plex-library-tool.py --restore` to pick from any past run.
