#!/usr/bin/env python3

import argparse
import concurrent.futures
import ctypes
import datetime
import difflib
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
API_KEY_FILE = SCRIPT_DIR / ".env"
LOG_DIR = SCRIPT_DIR / "logs"
BACKUP_DIR = SCRIPT_DIR / "backups"
CACHE_FILE = SCRIPT_DIR / "cache.json"
TMDB_BASE = "https://api.themoviedb.org/3"
DELETE_FILE = SCRIPT_DIR / "delete.yaml"
NAMES_FILE = SCRIPT_DIR / "names.yaml"
CLEANUP_TRASH_DIR = SCRIPT_DIR / ".trash"

VERBOSE = False


def vprint(*args, **kwargs):
    if VERBOSE:
        print(*args, **kwargs)


VIDEO_EXTENSIONS = {
    "mp4", "mkv", "avi", "mov", "wmv", "m4v", "mpg", "mpeg", "flv", "ts", "m2ts", "webm",
}

SUBTITLE_EXTENSIONS = {"srt", "sub"}
SUBTITLE_FOLDER_NAMES = {"subs", "subtitles"}

SEASON_EP_PATTERNS = [
    (re.compile(r'[Ss](\d{1,2})[Ee](\d{1,2})'), 'E'),
    (re.compile(r'[Ss](\d{1,2})[Xx](\d{1,2})'), 'X'),
    (re.compile(r'[Ss](\d{1,2})[Mm](\d{1,2})'), 'M'),
    (re.compile(r'(?<![A-Za-z0-9])(\d{1,2})[xX](\d{1,2})(?![A-Za-z0-9])'), 'E'),
]

SEASON_ONLY_PATTERN = re.compile(r'(?<![A-Za-z0-9])[Ss](\d{1,2})')

SEASON_FOLDER_PATTERN = re.compile(r'season\s*0*(\d{1,3})\b', re.IGNORECASE)
SEASON_FOLDER_CANONICAL_PATTERN = re.compile(r'^[Ss](\d{1,3})$')

EPISODE_ONLY_PATTERN = re.compile(r'\b[Ee](?:p(?:isode)?)?\.?\s*0*(\d{1,3})\b')
LEADING_NUMBER_PATTERN = re.compile(r'^0*(\d{1,3})[\s._-]')

LEGACY_SEE_PATTERN = re.compile(r'(?<![A-Za-z0-9])([1-9])(\d{2})(?![A-Za-z0-9])')


class RenameLog:
    def __init__(self):
        self.entries = []
        self.label = None
        self.log_path = None

    def set_label(self, label):
        self.label = label

    def _ensure_path(self):
        if self.log_path is not None:
            return
        LOG_DIR.mkdir(exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = f"-{re.sub(r'[^A-Za-z0-9]+', '_', self.label).strip('_')}" if self.label else ""
        log_path = LOG_DIR / f"{timestamp}{suffix}.json"
        counter = 1
        while log_path.exists():
            log_path = LOG_DIR / f"{timestamp}{suffix}-{counter}.json"
            counter += 1
        self.log_path = log_path

    def record(self, from_path, to_path):
        self.entries.append({"from": str(from_path), "to": str(to_path)})
        self._ensure_path()
        self.log_path.write_text(json.dumps(self.entries, indent=2), encoding="utf-8")

    def save(self, label=None):
        if not self.entries:
            return None
        if label and self.label is None:
            self.set_label(label)
        self._ensure_path()
        self.log_path.write_text(json.dumps(self.entries, indent=2), encoding="utf-8")
        return self.log_path


SUPERSCRIPT_DIGITS = {
    '⁰': ' 0', '¹': ' 1', '²': ' 2', '³': ' 3',
    '⁴': ' 4', '⁵': ' 5', '⁶': ' 6', '⁷': ' 7',
    '⁸': ' 8', '⁹': ' 9',
}


def normalize_special_chars(name):
    for special, normal in SUPERSCRIPT_DIGITS.items():
        name = name.replace(special, normal)
    decomposed = unicodedata.normalize('NFKD', name)
    return ''.join(c for c in decomposed if not unicodedata.combining(c))


def clean_name(name):
    name = normalize_special_chars(name)
    name = re.sub(r'[._-]+', ' ', name)
    return re.sub(r'[^A-Za-z0-9 ]', '', name)


def squeeze_spaces(name):
    return re.sub(r'\s+', ' ', name).strip()


def clean_and_squeeze(name):
    return squeeze_spaces(clean_name(name))


def clean_name_keep_hyphens(name):
    name = normalize_special_chars(name)
    name = re.sub(r'[._]+', ' ', name)
    return re.sub(r'[^A-Za-z0-9 \-]', '', name)


def clean_and_squeeze_keep_hyphens(name):
    return squeeze_spaces(clean_name_keep_hyphens(name))


def clean_display_title(name):
    return squeeze_spaces(normalize_special_chars(name))


def extract_year(name):
    paren_years = re.findall(r'\((19\d{2}|20\d{2})\)', name)
    if paren_years:
        return paren_years[-1]
    bare_years = re.findall(r'(?<![A-Za-z0-9])(19\d{2}|20\d{2})(?![A-Za-z0-9])', name)
    if bare_years:
        return bare_years[-1]
    return None


def parse_season_episode(filename):
    for pat, marker in SEASON_EP_PATTERNS:
        m = pat.search(filename)
        if m:
            season = int(m.group(1))
            episode = int(m.group(2))
            return season, episode, marker

    if parse_season_only(filename) is None and parse_episode_only(filename) is None:
        m = LEGACY_SEE_PATTERN.search(filename)
        if m:
            season = int(m.group(1))
            episode = int(m.group(2))
            return season, episode, 'E'

    return None


def parse_season_only(filename):
    m = SEASON_ONLY_PATTERN.search(filename)
    if m:
        return int(m.group(1))
    return None


def parse_episode_only(filename):
    m = EPISODE_ONLY_PATTERN.search(filename)
    if m:
        return int(m.group(1))
    m = LEADING_NUMBER_PATTERN.search(filename)
    if m:
        return int(m.group(1))
    return None


TRAILING_TAG_PATTERN = re.compile(r'[\[\(][^\[\]\(\)]*[\]\)]\s*$')
TRAILING_NUMBER_PATTERN = re.compile(r'(?<![A-Za-z0-9])0*(\d{1,3})\s*$')


def parse_trailing_episode_number(filename):
    stem = Path(filename).stem
    stripped = stem
    for _ in range(3):
        new_stripped = TRAILING_TAG_PATTERN.sub('', stripped).rstrip()
        if new_stripped == stripped:
            break
        stripped = new_stripped
    m = TRAILING_NUMBER_PATTERN.search(stripped)
    if m:
        return int(m.group(1))
    return None


def parse_season_folder_name(name):
    stripped = name.strip()
    m = SEASON_FOLDER_CANONICAL_PATTERN.match(stripped)
    if m:
        return int(m.group(1))
    m = SEASON_FOLDER_PATTERN.search(stripped)
    if m:
        return int(m.group(1))
    m = SEASON_ONLY_PATTERN.search(stripped)
    if m:
        return int(m.group(1))
    return None


def parse_env_file(path):
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def save_env_value(key, value):
    values = parse_env_file(API_KEY_FILE)
    values[key] = value
    lines = [f"{k}={v}" for k, v in values.items()]
    API_KEY_FILE.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(API_KEY_FILE, 0o600)
    except OSError:
        pass


def get_api_key():
    env_key = os.environ.get("TMDB_API_KEY")
    if env_key:
        return env_key.strip()

    key = parse_env_file(API_KEY_FILE).get("TMDB_API_KEY", "").strip()
    if key:
        return key

    print("TMDb API key not found.")
    print("Get a free key:")
    print("  1. Log in (or create a free account): https://www.themoviedb.org/login")
    print("  2. Request an API key: https://www.themoviedb.org/settings/api/request")
    print("  3. Choose 'Developer'")
    print("  4. On the settings page, copy the 'API Key' value")
    print("     (NOT the longer 'API Read Access Token')")
    print()
    key = input("Enter your TMDb API key: ").strip()
    if not key:
        print("No API key provided. Exiting.")
        sys.exit(1)

    save = input("Save this key to .env for future runs? [Y/n] ").strip().lower()
    if save in ("", "y", "yes"):
        save_env_value("TMDB_API_KEY", key)

    return key


def detect_os_language():
    system = platform.system()

    if system == "Windows":
        try:
            buf = ctypes.create_unicode_buffer(85)
            ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85)
            if buf.value:
                return buf.value
        except (AttributeError, OSError) as e:
            vprint(f"  Could not detect Windows locale: {e}")
        return None

    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            lang = value.split(".")[0].replace("_", "-")
            if lang and lang.lower() not in ("c", "posix"):
                return lang
    return None


_TMDB_LANGUAGE = None


def get_tmdb_language():
    global _TMDB_LANGUAGE
    if _TMDB_LANGUAGE is not None:
        return _TMDB_LANGUAGE

    env_lang = os.environ.get("TMDB_LANGUAGE")
    if env_lang:
        _TMDB_LANGUAGE = env_lang.strip()
        return _TMDB_LANGUAGE

    saved_lang = parse_env_file(API_KEY_FILE).get("TMDB_LANGUAGE", "").strip()
    if saved_lang:
        _TMDB_LANGUAGE = saved_lang
        return _TMDB_LANGUAGE

    detected = detect_os_language()
    if detected:
        answer = input(f"Detected system language: {detected}. Use this for TMDb results (movie/show titles)? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            chosen = detected
        else:
            custom = input("Enter a TMDb language code instead (e.g. en-US, es-MX, fr-FR), or press Enter for English: ").strip()
            chosen = custom or "en-US"
    else:
        custom = input("Could not detect your system language. Enter a TMDb language code (e.g. en-US, es-MX, fr-FR), or press Enter for English: ").strip()
        chosen = custom or "en-US"

    save = input("Save this to .env for future runs? [Y/n] ").strip().lower()
    if save in ("", "y", "yes"):
        save_env_value("TMDB_LANGUAGE", chosen)

    _TMDB_LANGUAGE = chosen
    return chosen


def find_smb_mounts():
    system = platform.system()
    mounts = []
    vprint(f"Detecting SMB mounts (platform: {system})")

    if system == "Darwin":
        try:
            out = subprocess.run(
                ["mount"], capture_output=True, text=True, check=True
            ).stdout
            for line in out.splitlines():
                if "smbfs" in line:
                    m = re.search(r' on (.+?) \(', line)
                    if m:
                        mounts.append(m.group(1))
                        vprint(f"  Found smbfs mount: {m.group(1)}")
        except (subprocess.SubprocessError, OSError) as e:
            vprint(f"  'mount' command failed: {e}")

        if not mounts and os.path.isdir("/Volumes"):
            vprint("  No smbfs mounts via 'mount', falling back to /Volumes listing")
            mounts = [str(p) for p in Path("/Volumes").iterdir() if p.is_dir()]
            for m in mounts:
                vprint(f"  Found /Volumes entry: {m}")

    elif system == "Linux":
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3 and parts[2] == "cifs":
                        mounts.append(parts[1])
                        vprint(f"  Found cifs mount: {parts[1]}")
        except OSError as e:
            vprint(f"  Could not read /proc/mounts: {e}")

        try:
            gvfs_base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
            gvfs_dir = Path(gvfs_base) / "gvfs"
            vprint(f"  Checking GVFS directory: {gvfs_dir}")
            if gvfs_dir.is_dir():
                for entry in gvfs_dir.iterdir():
                    if entry.is_dir() and entry.name.startswith("smb-share:"):
                        mounts.append(str(entry))
                        vprint(f"  Found GVFS smb-share mount: {entry}")
        except OSError as e:
            vprint(f"  Could not read GVFS directory: {e}")

    elif system == "Windows":
        DRIVE_REMOTE = 4
        try:
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                if not (bitmask & (1 << i)):
                    continue
                drive = f"{chr(ord('A') + i)}:\\"
                drive_type = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive))
                if drive_type == DRIVE_REMOTE:
                    mounts.append(drive)
                    vprint(f"  Found mapped network drive: {drive}")
        except (AttributeError, OSError) as e:
            vprint(f"  Could not enumerate drives via GetLogicalDrives: {e}")

        try:
            out = subprocess.run(
                ["net", "use"], capture_output=True, text=True, check=True
            ).stdout
            for line in out.splitlines():
                m = re.search(r'([A-Za-z]:)\s+(\\\\[^\s]+)', line)
                if m:
                    unc = m.group(2)
                    if unc not in mounts:
                        mounts.append(unc)
                        vprint(f"  Found 'net use' mapping: {unc}")
        except (subprocess.SubprocessError, OSError) as e:
            vprint(f"  'net use' command failed: {e}")

    result = sorted(set(mounts))
    vprint(f"Total SMB mounts found: {len(result)}")
    return result


def select_mount(mounts):
    print("Available SMB shares:")
    for i, m in enumerate(mounts, 1):
        print(f"  {i}) {m}")
    while True:
        sel = input(f"Select a share to scan [1-{len(mounts)}]: ").strip()
        if sel.isdigit() and 1 <= int(sel) <= len(mounts):
            return mounts[int(sel) - 1]
        print("Invalid selection.")


def resolve_share(path_arg=None):
    if isinstance(path_arg, str):
        vprint(f"Path given on the command line: {path_arg}")
        if not Path(path_arg).is_dir():
            print(f"Path not found: {path_arg}")
            sys.exit(1)
        return path_arg

    mounts = find_smb_mounts()
    if not mounts:
        print("No SMB shares found.")
        sys.exit(1)
    return select_mount(mounts)


def select_media_type():
    while True:
        sel = input("Is this a Movies or TV Shows library? [M/T]: ").strip().lower()
        if sel in ("m", "movie", "movies"):
            return "movie"
        if sel in ("t", "tv", "show", "shows", "tvshows"):
            return "tv"
        print("Please enter M or T.")


MOVIE_SHARE_KEYWORDS = ("movie", "film")
TV_SHARE_KEYWORDS = ("tv", "show", "series")


def infer_media_type(share):
    path = Path(share)
    candidates = [path.name]
    if path.parent != path:
        candidates.append(path.parent.name)
    candidates = [c.lower() for c in candidates if c]

    is_movie = any(any(k in c for k in MOVIE_SHARE_KEYWORDS) for c in candidates)
    is_tv = any(any(k in c for k in TV_SHARE_KEYWORDS) for c in candidates)
    if is_movie and not is_tv:
        return "movie"
    if is_tv and not is_movie:
        return "tv"
    return None


def normalize_media_type_arg(value):
    v = value.strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    if v in ("m", "movie", "movies", "film", "films"):
        return "movie"
    if v in ("t", "tv", "tvshow", "tvshows", "show", "shows", "series"):
        return "tv"
    raise argparse.ArgumentTypeError(f"Invalid type: {value!r}. Use 'movies' or 'tv'.")


def determine_media_type(share, override=None):
    if override:
        label = "Movies" if override == "movie" else "TV Shows"
        print(f"Using library type from --type: {label}")
        return override

    inferred = infer_media_type(share)
    if inferred:
        label = "Movies" if inferred == "movie" else "TV Shows"
        print(f"Detected library type from share name: {label}")
        return inferred
    return select_media_type()


def is_v4_token(api_key):
    return api_key.startswith("eyJ") or len(api_key) > 60


def tmdb_search(api_key, media_type, query):
    v4 = is_v4_token(api_key)

    params = {"query": query, "language": get_tmdb_language()}
    if not v4:
        params["api_key"] = api_key

    endpoint = "search/movie" if media_type == "movie" else "search/tv"
    url = f"{TMDB_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"

    headers = {"Authorization": f"Bearer {api_key}"} if v4 else {}
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return None, str(e)

    return data.get("results", []), None


def tmdb_alternative_titles(api_key, media_type, tmdb_id):
    v4 = is_v4_token(api_key)

    params = {}
    if not v4:
        params["api_key"] = api_key

    endpoint = f"movie/{tmdb_id}/alternative_titles" if media_type == "movie" else f"tv/{tmdb_id}/alternative_titles"
    url = f"{TMDB_BASE}/{endpoint}"
    if params:
        url += f"?{urllib.parse.urlencode(params)}"

    headers = {"Authorization": f"Bearer {api_key}"} if v4 else {}
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return []

    key = "titles" if media_type == "movie" else "results"
    entries = data.get(key, [])
    return [e.get("title") or e.get("name") for e in entries if e.get("title") or e.get("name")]


def tmdb_tv_details(api_key, tv_id):
    v4 = is_v4_token(api_key)

    params = {"language": get_tmdb_language()}
    if not v4:
        params["api_key"] = api_key

    url = f"{TMDB_BASE}/tv/{tv_id}?{urllib.parse.urlencode(params)}"
    headers = {"Authorization": f"Bearer {api_key}"} if v4 else {}
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return None, str(e)

    return data, None


def tmdb_tv_season(api_key, tv_id, season_number):
    v4 = is_v4_token(api_key)

    params = {"language": get_tmdb_language()}
    if not v4:
        params["api_key"] = api_key

    url = f"{TMDB_BASE}/tv/{tv_id}/season/{season_number}?{urllib.parse.urlencode(params)}"
    headers = {"Authorization": f"Bearer {api_key}"} if v4 else {}
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return None, str(e)

    return data, None


def tmdb_tv_episode_groups(api_key, tv_id):
    v4 = is_v4_token(api_key)

    params = {}
    if not v4:
        params["api_key"] = api_key

    url = f"{TMDB_BASE}/tv/{tv_id}/episode_groups"
    if params:
        url += f"?{urllib.parse.urlencode(params)}"
    headers = {"Authorization": f"Bearer {api_key}"} if v4 else {}
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return None, str(e)

    return data, None


def tmdb_episode_group_details(api_key, group_id):
    v4 = is_v4_token(api_key)

    params = {}
    if not v4:
        params["api_key"] = api_key

    url = f"{TMDB_BASE}/tv/episode_group/{group_id}"
    if params:
        url += f"?{urllib.parse.urlencode(params)}"
    headers = {"Authorization": f"Bearer {api_key}"} if v4 else {}
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return None, str(e)

    return data, None


ABSOLUTE_EPISODE_GROUP_TYPE = 2
_ABSOLUTE_EPISODE_CACHE = {}


def get_absolute_episode_map(api_key, tv_id):
    if tv_id in _ABSOLUTE_EPISODE_CACHE:
        return _ABSOLUTE_EPISODE_CACHE[tv_id]

    mapping = None
    groups, err = tmdb_tv_episode_groups(api_key, tv_id)
    if err:
        vprint(f"  Could not fetch episode groups for tv_id={tv_id}: {err}")
    else:
        candidates = [
            g for g in groups.get("results", [])
            if g.get("type") == ABSOLUTE_EPISODE_GROUP_TYPE
        ]
        candidates.sort(key=lambda g: g.get("episode_count", 0), reverse=True)

        if candidates:
            group_id = candidates[0].get("id")
            details, err = tmdb_episode_group_details(api_key, group_id)
            if err:
                vprint(f"  Could not fetch absolute episode group details (id={group_id}): {err}")
            else:
                built = {}
                absolute_number = 0
                for group in details.get("groups", []):
                    for ep in group.get("episodes", []):
                        absolute_number += 1
                        season_number = ep.get("season_number")
                        episode_number = ep.get("episode_number")
                        if season_number is not None and episode_number is not None:
                            built[absolute_number] = (season_number, episode_number)
                if built:
                    mapping = built
                    vprint(f"  Loaded absolute episode order for tv_id={tv_id}: {len(built)} episode(s)")

    if not mapping:
        mapping = build_absolute_map_from_season_counts(api_key, tv_id)

    _ABSOLUTE_EPISODE_CACHE[tv_id] = mapping
    return mapping


def build_absolute_map_from_season_counts(api_key, tv_id):
    details, err = tmdb_tv_details(api_key, tv_id)
    if err:
        vprint(f"  Could not fetch tv details for tv_id={tv_id}: {err}")
        return None

    seasons = [
        s for s in details.get("seasons", [])
        if s.get("season_number", 0) > 0 and s.get("episode_count")
    ]
    seasons.sort(key=lambda s: s.get("season_number"))

    if not seasons:
        return None

    built = {}
    absolute_number = 0
    for s in seasons:
        season_number = s.get("season_number")
        for episode_number in range(1, s.get("episode_count") + 1):
            absolute_number += 1
            built[absolute_number] = (season_number, episode_number)

    if built:
        vprint(f"  Built absolute episode order for tv_id={tv_id} from season episode counts: {len(built)} episode(s)")
        return built
    return None


def resolve_absolute_episode(api_key, tmdb_id, name):
    if tmdb_id is None:
        return None
    absolute_number = parse_episode_only(name)
    if absolute_number is None:
        absolute_number = parse_trailing_episode_number(name)
    if absolute_number is None:
        return None
    mapping = get_absolute_episode_map(api_key, tmdb_id)
    if not mapping:
        return None
    return mapping.get(absolute_number)


def result_title(media_type, result):
    return result.get("title") if media_type == "movie" else result.get("name")


def result_year(media_type, result):
    date_field = result.get("release_date") if media_type == "movie" else result.get("first_air_date")
    if date_field and len(date_field) >= 4:
        return date_field[:4]
    return None


def best_match(media_type, results, year, query=None):
    if not results:
        return None

    query_norm = clean_and_squeeze(query).lower() if query else None

    def is_exact_title(r):
        return query_norm is not None and clean_and_squeeze(result_title(media_type, r) or "").lower() == query_norm

    if query_norm and year:
        for r in results:
            if is_exact_title(r) and result_year(media_type, r) == year:
                return r

    if year:
        for r in results:
            if result_year(media_type, r) == year:
                return r
        return None

    if query_norm:
        for r in results:
            if is_exact_title(r):
                return r

    return results[0]


FUZZY_TITLE_MATCH_THRESHOLD = 0.85


def fuzzy_title_match(media_type, results, query, year):
    if not query or not results or not year:
        return None

    query_norm = clean_and_squeeze(query).lower()
    best = None
    best_ratio = 0.0

    for r in results:
        if result_year(media_type, r) != year:
            continue
        title_norm = clean_and_squeeze(result_title(media_type, r) or "").lower()
        if not title_norm:
            continue
        ratio = difflib.SequenceMatcher(None, query_norm, title_norm).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = r

    if best is not None and best_ratio >= FUZZY_TITLE_MATCH_THRESHOLD:
        return best, best_ratio
    return None


def same_existing_path(dest, src):
    try:
        return dest.exists() and src.exists() and dest.samefile(src)
    except OSError:
        return False


def safe_rename(src, dest):
    try:
        src.rename(dest)
        return True, None
    except FileExistsError:
        return False, "target already exists"
    except OSError as e:
        return False, str(e)


def confirm(prompt):
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in ("y", "yes")


def confirm_delete_choice(prompt):
    while True:
        answer = input(f"{prompt} [y/N/a] ").strip().lower()
        if answer in ("y", "yes"):
            return "y"
        if answer in ("a", "all"):
            return "a"
        if answer in ("n", "no", ""):
            return "n"
        print("Please enter y (yes), n (no), or a (all).")


JUNK_TOKENS = {
    "bluray", "blueray", "bdrip", "brrip", "bdremux", "remux",
    "webrip", "webdl", "web", "dl", "hdtv", "hdrip", "dvdrip", "dvd",
    "hevc", "x264", "x265", "h264", "h265", "avc", "xvid", "divx",
    "aac", "ac3", "eac3", "dts", "atmos", "ddp", "dd",
    "proper", "repack", "extended", "unrated", "uncut", "director", "directors", "cut",
    "internal", "limited", "theatrical", "multi", "dual", "audio",
    "hdr", "sdr", "4k", "uhd", "10bit", "8bit", "hi10p", "hi444pp",
    "deluxe", "boxset", "box", "set", "extras", "hd",
    "complete", "collection", "series",
    "se", "ce", "dc", "remastered", "anniversary", "edition",
    "amzn", "nf", "dsnp", "hmax", "atvp", "pcok", "hulu",
    "mb", "gb",
    "subbed", "dubbed", "subs", "multisub",
    "telesync", "telecine", "camrip", "hdcam", "hdts", "hdtc",
    "r5", "screener", "dvdscr", "workprint",
    "retail",
}

SEASON_RANGE_PATTERN = re.compile(r'\bSeasons?\b(?:\s+\d{1,2})+', re.IGNORECASE)
IN_FORMAT_PATTERN = re.compile(r'\bin\s+(?:full\s+)?(?:hd|4k|uhd|sd)\b', re.IGNORECASE)


FILE_SIZE_PATTERN = re.compile(r'\b\d+(?:\.\d+)?\s?(?:MB|GB)\b', re.IGNORECASE)
BIT_DEPTH_PATTERN = re.compile(r'\b\d{1,2}bits?\b', re.IGNORECASE)


def strip_junk_tokens(text):
    text = re.sub(r'\b\d{3,4}p\b', ' ', text, flags=re.IGNORECASE)
    text = FILE_SIZE_PATTERN.sub(' ', text)
    text = BIT_DEPTH_PATTERN.sub(' ', text)
    text = SEASON_RANGE_PATTERN.sub(' ', text)
    text = IN_FORMAT_PATTERN.sub(' ', text)
    words = text.split()
    kept = [w for w in words if w.lower() not in JUNK_TOKENS]
    return squeeze_spaces(' '.join(kept))


SEASON_TRUNCATE_PATTERN = re.compile(r'\bSeasons?\b\s+\d', re.IGNORECASE)
BARE_SEASON_TRUNCATE_PATTERN = re.compile(r'(?<![A-Za-z0-9])[Ss]\d{1,2}(?:[Ee]\d{1,3})?(?![A-Za-z0-9])')

RELEASE_GROUP_SUFFIX_PATTERN = re.compile(
    r'\b(x264|x265|h264|h265|hevc|avc|aac|ac3|eac3|dts|atmos|bluray|blueray|'
    r'webrip|webdl|dvdrip|dvd|hdtv|hdrip|bdrip|brrip|remux)'
    r'(?:[.\-_ ]+\w+)?[.\-_ ]+[A-Za-z0-9]+(?:\[[A-Za-z0-9]+\])?$',
    re.IGNORECASE,
)

AUDIO_CHANNELS_PATTERN = re.compile(r'(?<![0-9])\d\.\d(?![0-9])')
AUDIO_CODEC_CHANNELS_PATTERN = re.compile(
    r'\b(DDP?|EAC3|AC3|TrueHD|Atmos|DTS(?:-HD)?|FLAC|AAC)[.\s]?\d(?:[.\s]\d)?\b',
    re.IGNORECASE,
)
VIDEO_CODEC_SPACED_PATTERN = re.compile(r'\b[Hh][.\s]?(26[45])\b')
LEADING_TAG_DASH_CHARS = '-' + chr(8211) + chr(8212)
LEADING_TAG_PATTERN = re.compile(r'^\s*\[[^\[\]]*\](?:\([^()]*\))?\s*[' + re.escape(LEADING_TAG_DASH_CHARS) + r']+\s*')
URL_PATTERN = re.compile(r'https?://\S+', re.IGNORECASE)
WWW_DOMAIN_PATTERN = re.compile(r'\bwww\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b', re.IGNORECASE)
TRAILING_PAREN_BLOCK_PATTERN = re.compile(r'\([^()]*\)\s*$')
TRAILING_BRACKET_TAG_PATTERN = re.compile(r'\[[^\[\]]*\]\s*$')
BARE_YEAR_PATTERN = re.compile(r'(19|20)\d{2}$')


def strip_leading_watermark(raw_name):
    return LEADING_TAG_PATTERN.sub('', raw_name, count=1)


def strip_trailing_bracket_tags(name):
    stripped = name
    while True:
        match = TRAILING_BRACKET_TAG_PATTERN.search(stripped)
        if not match:
            break
        candidate = stripped[:match.start()].rstrip()
        if not candidate:
            break
        stripped = candidate
    return stripped


def strip_junk_trailing_paren(raw_name):
    working = raw_name
    bracket_match = TRAILING_BRACKET_TAG_PATTERN.search(working)
    if bracket_match:
        working = working[:bracket_match.start()]

    match = TRAILING_PAREN_BLOCK_PATTERN.search(working)
    if not match:
        return raw_name

    words = re.findall(r'[A-Za-z0-9]+', match.group(0))
    if not words:
        return raw_name

    significant = [
        w for w in words
        if not re.fullmatch(r'\d{3,4}p', w, re.IGNORECASE)
        and not BARE_YEAR_PATTERN.fullmatch(w)
        and w.lower() not in JUNK_TOKENS
    ]

    if 0 < len(significant) <= 1:
        return working[:match.start()]
    return raw_name


def build_query(raw_name, year, keep_hyphens=False):
    raw_name = strip_leading_watermark(raw_name)
    raw_name = URL_PATTERN.sub(' ', raw_name)
    raw_name = WWW_DOMAIN_PATTERN.sub(' ', raw_name)

    cut_points = []
    match = SEASON_TRUNCATE_PATTERN.search(raw_name)
    if match:
        cut_points.append(match.start())
    match = BARE_SEASON_TRUNCATE_PATTERN.search(raw_name)
    if match:
        cut_points.append(match.start())
    if cut_points:
        cut = min(cut_points)
        if cut > 0:
            raw_name = raw_name[:cut]

    raw_name = VIDEO_CODEC_SPACED_PATTERN.sub(r'H\1', raw_name)
    raw_name = AUDIO_CODEC_CHANNELS_PATTERN.sub(' ', raw_name)
    raw_name = AUDIO_CHANNELS_PATTERN.sub(' ', raw_name)
    raw_name = strip_junk_trailing_paren(raw_name)

    match = RELEASE_GROUP_SUFFIX_PATTERN.search(raw_name)
    if match:
        raw_name = raw_name[:match.start()] + match.group(1)

    raw_name = strip_trailing_bracket_tags(raw_name)

    cleaner = clean_and_squeeze_keep_hyphens if keep_hyphens else clean_and_squeeze
    cleaned = cleaner(raw_name)
    if year:
        without_year = squeeze_spaces(re.sub(rf'(?<!\d){year}(?!\d)', '', cleaned))
        if without_year:
            cleaned = without_year
    cleaned = strip_junk_tokens(cleaned)
    return cleaned


ALT_TITLE_CHECK_LIMIT = 20

DEFAULT_NAME_TEMPLATES = {
    "movie_folder": "{title} ({year})",
    "movie_folder_no_year": "{title}",
    "show_folder": "{title} ({year})",
    "show_folder_no_year": "{title}",
    "movie_file": "{title}.{ext}",
    "episode_file": "{title} S{season:02d}E{episode:02d}.{ext}",
    "season_folder": "S{season:02d}",
    "subtitle_file": "{stem}.{lang}.{ext}",
    "subtitle_folder": "Subs",
    "word_separator": " ",
    "name_case": "",
}

FILESYSTEM_ILLEGAL_CHARS_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
EMPTY_GROUP_PATTERN = re.compile(r'[\(\[\{]\s*[\)\]\}]')

RESOLUTION_PATTERN = re.compile(r'\b(4320p|2160p|1080p|720p|480p|360p|4K|8K|UHD)\b', re.IGNORECASE)
RESOLUTION_CANONICAL = {
    "4320p": "4320p", "2160p": "2160p", "1080p": "1080p", "720p": "720p",
    "480p": "480p", "360p": "360p", "4k": "4K", "8k": "8K", "uhd": "UHD",
}

_NAME_TEMPLATES = None


def detect_resolution(name):
    match = RESOLUTION_PATTERN.search(name)
    if not match:
        return None
    return RESOLUTION_CANONICAL.get(match.group(1).lower())


def sanitize_rendered_name(name):
    name = FILESYSTEM_ILLEGAL_CHARS_PATTERN.sub('', name)
    name = EMPTY_GROUP_PATTERN.sub('', name)
    name = squeeze_spaces(name)
    name = name.strip().rstrip('. ')
    case = get_name_templates().get("name_case", "")
    if case == "lower":
        name = name.lower()
    elif case == "upper":
        name = name.upper()
    return name


def load_name_templates():
    templates = dict(DEFAULT_NAME_TEMPLATES)
    if not NAMES_FILE.exists():
        return templates
    try:
        lines = NAMES_FILE.read_text().splitlines()
    except OSError as e:
        vprint(f"Could not read {NAMES_FILE.name}: {e}")
        return templates

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if key not in DEFAULT_NAME_TEMPLATES:
            continue
        quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'")
        if quoted:
            value = value[1:-1]
        elif "#" in value:
            value = value.split("#", 1)[0].strip()
        if value or quoted:
            templates[key] = value
            vprint(f"  Loaded naming template {key!r}: {value!r}")

    return templates


def get_name_templates():
    global _NAME_TEMPLATES
    if _NAME_TEMPLATES is None:
        _NAME_TEMPLATES = load_name_templates()
    return _NAME_TEMPLATES


def render_name(template, fallback, **context):
    try:
        rendered = template.format(**context)
    except (KeyError, ValueError, IndexError) as e:
        vprint(f"  Bad naming template {template!r}: {e}, falling back to default")
        rendered = fallback.format(**context)
    return sanitize_rendered_name(rendered)


def apply_word_separator(title):
    templates = get_name_templates()
    separator = templates.get("word_separator", " ")
    if separator == " ":
        return title
    return title.replace(" ", separator)


def movie_folder_name(title, year, resolution=None):
    templates = get_name_templates()
    title = apply_word_separator(title)
    resolution = resolution or ""
    if year:
        return render_name(templates["movie_folder"], DEFAULT_NAME_TEMPLATES["movie_folder"], title=title, year=int(year), resolution=resolution)
    return render_name(templates["movie_folder_no_year"], DEFAULT_NAME_TEMPLATES["movie_folder_no_year"], title=title, resolution=resolution)


def show_folder_name(title, year, resolution=None):
    templates = get_name_templates()
    title = apply_word_separator(title)
    resolution = resolution or ""
    if year:
        return render_name(templates["show_folder"], DEFAULT_NAME_TEMPLATES["show_folder"], title=title, year=int(year), resolution=resolution)
    return render_name(templates["show_folder_no_year"], DEFAULT_NAME_TEMPLATES["show_folder_no_year"], title=title, resolution=resolution)


def movie_file_name(title, ext, resolution=None):
    templates = get_name_templates()
    title = apply_word_separator(title)
    resolution = resolution or ""
    return render_name(templates["movie_file"], DEFAULT_NAME_TEMPLATES["movie_file"], title=title, ext=ext, resolution=resolution)


def episode_file_name(title, season, episode, ext, resolution=None):
    templates = get_name_templates()
    title = apply_word_separator(title)
    resolution = resolution or ""
    return render_name(
        templates["episode_file"], DEFAULT_NAME_TEMPLATES["episode_file"],
        title=title, season=season, episode=episode, ext=ext, resolution=resolution,
    )


def season_folder_name(season):
    templates = get_name_templates()
    return render_name(templates["season_folder"], DEFAULT_NAME_TEMPLATES["season_folder"], season=season)


def subtitle_file_name(stem, ext, lang):
    templates = get_name_templates()
    return render_name(templates["subtitle_file"], DEFAULT_NAME_TEMPLATES["subtitle_file"], stem=stem, ext=ext, lang=lang)


def subtitle_folder_name():
    templates = get_name_templates()
    return render_name(templates["subtitle_folder"], DEFAULT_NAME_TEMPLATES["subtitle_folder"])


def folder_target_name(media_type, final_name, match_year, raw_name=None):
    resolution = detect_resolution(raw_name) if raw_name else None
    if media_type == "movie":
        return movie_folder_name(final_name, match_year, resolution)
    return show_folder_name(final_name, match_year, resolution)


def infer_year_from_files(folder):
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.suffix.lower().lstrip(".") in VIDEO_EXTENSIONS:
            year = extract_year(path.stem)
            if year:
                return year
    return None


def lookup_folder(api_key, media_type, raw_name, hint_year=None):
    year = extract_year(raw_name) or hint_year
    query = build_query(raw_name, year)
    vprint(f"Looking up: {raw_name!r} -> query={query!r}, year={year!r}, media_type={media_type!r}")

    results, err = tmdb_search(api_key, media_type, query)
    if err:
        return None, None, None, f"Lookup failed: {raw_name} ({err})"
    vprint(f"  TMDb returned {len(results)} result(s)")

    match = best_match(media_type, results, year, query)

    if not match and "-" in raw_name:
        hyphen_query = build_query(raw_name, year, keep_hyphens=True)
        if hyphen_query != query:
            quoted_query = f'"{hyphen_query}"'
            vprint(f"  No match, retrying as quoted phrase (TMDb treats bare hyphens as query operators): query={quoted_query!r}")
            hyphen_results, hyphen_err = tmdb_search(api_key, media_type, quoted_query)
            if not hyphen_err:
                vprint(f"  TMDb returned {len(hyphen_results)} result(s)")
                hyphen_match = best_match(media_type, hyphen_results, year, hyphen_query)
                if hyphen_match:
                    match = hyphen_match
                    results = hyphen_results
                    query = hyphen_query

    if not match and year:
        words = query.split()
        if len(words) > 1:
            broadened_query = " ".join(words[:-1])
            vprint(f"  No match, retrying with broadened query (possible title typo): query={broadened_query!r}")
            broad_results, broad_err = tmdb_search(api_key, media_type, broadened_query)
            if not broad_err:
                vprint(f"  TMDb returned {len(broad_results)} result(s)")
                fuzzy = fuzzy_title_match(media_type, broad_results, query, year)
                if fuzzy:
                    fuzzy_match, ratio = fuzzy
                    vprint(f"  Fuzzy title match: {result_title(media_type, fuzzy_match)!r} (ratio={ratio:.2f}, id={fuzzy_match.get('id')})")
                    match = fuzzy_match
                    results = broad_results

    if not match:
        return None, None, None, f"No match found (Ignoring): {raw_name}"

    query_norm = clean_and_squeeze(query).lower()
    title = result_title(media_type, match)
    final_name = clean_display_title(title)
    title_norm = clean_and_squeeze(title).lower()
    vprint(f"  Best match: {title!r} (id={match.get('id')})")

    if title_norm != query_norm:
        vprint("  Title differs from query, checking alternative titles...")
        for candidate in results[:ALT_TITLE_CHECK_LIMIT]:
            alt_titles = tmdb_alternative_titles(api_key, media_type, candidate.get("id"))
            hit = next((a for a in alt_titles if clean_and_squeeze(a).lower() == query_norm), None)
            if hit:
                match = candidate
                final_name = clean_display_title(hit)
                vprint(f"  Alternative title match: {hit!r} (id={candidate.get('id')})")
                break

    match_year = result_year(media_type, match)
    match_id = match.get("id")
    vprint(f"  Resolved: {final_name!r} ({match_year})")

    return final_name, match_year, match_id, None


def list_video_files(folder):
    return [
        item for item in sorted(folder.iterdir())
        if item.is_file() and item.suffix.lower().lstrip(".") in VIDEO_EXTENSIONS
    ]


def stem_match_pattern(stem):
    words = [w for w in re.split(r'[^A-Za-z0-9]+', stem) if w]
    if not words:
        return None
    body = r'[\W_]*'.join(re.escape(w) for w in words)
    return re.compile(r'^' + body + r'(?=[\W_])', re.IGNORECASE)


ENGLISH_LANGUAGE_TOKENS = {"en", "eng", "english"}

OTHER_LANGUAGE_TOKENS_RELIABLE = {
    "spanish", "fre", "fra", "french",
    "ger", "deu", "german", "ita", "italian",
    "portuguese", "dut", "nld", "dutch",
    "jpn", "japanese", "zho", "chinese", "cmn", "mandarin",
    "kor", "korean", "rus", "russian", "ara", "arabic",
    "swe", "swedish", "norwegian", "danish",
    "finnish", "polish", "tur", "turkish",
    "heb", "hebrew", "hin", "hindi", "tha", "thai",
    "cze", "ces", "czech", "ell", "greek",
    "hun", "hungarian", "romanian",
    "vie", "vietnamese", "ind", "indonesian", "ukr", "ukrainian",
    "malayalam", "tam", "tamil", "tel", "telugu", "kan", "kannada",
    "bengali", "panjabi", "punjabi", "marathi",
    "guj", "gujarati", "urd", "urdu", "odia", "oriya",
    "asm", "assamese", "sanskrit", "nep", "nepali", "sinhala",
    "fas", "persian", "farsi", "pashto",
    "kur", "kurdish", "aze", "azerbaijani", "kaz", "kazakh",
    "uzb", "uzbek", "tgk", "tajik", "tuk", "turkmen",
    "tgl", "fil", "filipino", "tagalog", "msa", "malay",
    "khm", "khmer", "lao", "laotian", "mya", "bur", "burmese", "myanmar",
    "mongolian", "swa", "swahili", "amh", "amharic",
    "hau", "hausa", "yor", "yoruba", "ibo", "igbo", "zul", "zulu",
    "xho", "xhosa", "sna", "shona", "somali",
    "sqi", "alb", "albanian", "hye", "armenian",
    "eus", "baq", "basque", "belarusian", "bul", "bulgarian",
    "catalan", "hrv", "croatian", "estonian",
    "glg", "galician", "kat", "georgian", "isl", "icelandic",
    "gle", "irish", "lav", "latvian", "lithuanian",
    "ltz", "luxembourgish", "mkd", "macedonian", "mlt", "maltese",
    "slk", "slo", "slovak", "slv", "slovenian",
    "cym", "wel", "welsh", "yid", "yiddish", "haitian",
    "srp", "serbian", "bos", "bosnian",
}

OTHER_LANGUAGE_TOKENS_RESTRICTED = {
    "es", "fr", "de", "it", "pt", "nl", "ja", "zh", "ko", "ru", "ar",
    "sv", "no", "da", "fi", "pl", "tr", "he", "hi", "th", "cs", "el",
    "hu", "ro", "vi", "id", "uk",
    "spa", "por", "chi", "dan", "pol", "fin", "gre",
    "mal", "san", "sin", "per", "mon", "pan", "mar", "pus", "ron",
    "may", "mac", "arm", "geo", "bel", "ice", "lit", "rum",
}

OTHER_LANGUAGE_TOKENS = OTHER_LANGUAGE_TOKENS_RELIABLE | OTHER_LANGUAGE_TOKENS_RESTRICTED


LANGUAGE_CANONICAL = {
    "en": {"en", "eng", "english"},
    "es": {"es", "spa", "spanish"},
    "fr": {"fr", "fre", "fra", "french"},
    "de": {"de", "ger", "deu", "german"},
    "it": {"it", "ita", "italian"},
    "pt": {"pt", "por", "portuguese"},
    "nl": {"nl", "dut", "nld", "dutch"},
    "ja": {"ja", "jpn", "japanese"},
    "zh": {"zh", "chi", "zho", "chinese", "cmn", "mandarin"},
    "ko": {"ko", "kor", "korean"},
    "ru": {"ru", "rus", "russian"},
    "ar": {"ar", "ara", "arabic"},
    "sv": {"sv", "swe", "swedish"},
    "no": {"no", "nor", "norwegian"},
    "da": {"da", "dan", "danish"},
    "fi": {"fi", "fin", "finnish"},
    "pl": {"pl", "pol", "polish"},
    "tr": {"tr", "tur", "turkish"},
    "he": {"he", "heb", "hebrew"},
    "hi": {"hi", "hin", "hindi"},
    "th": {"th", "tha", "thai"},
    "cs": {"cs", "cze", "ces", "czech"},
    "el": {"el", "gre", "ell", "greek"},
    "hu": {"hu", "hun", "hungarian"},
    "ro": {"ro", "ron", "rum", "romanian"},
    "vi": {"vi", "vie", "vietnamese"},
    "id": {"id", "ind", "indonesian"},
    "uk": {"uk", "ukr", "ukrainian"},
    "ml": {"mal", "malayalam"},
    "ta": {"tam", "tamil"},
    "te": {"tel", "telugu"},
    "kn": {"kan", "kannada"},
    "bn": {"ben", "bengali"},
    "pa": {"pan", "panjabi", "punjabi"},
    "mr": {"mar", "marathi"},
    "gu": {"guj", "gujarati"},
    "ur": {"urd", "urdu"},
    "or": {"ori", "odia", "oriya"},
    "as": {"asm", "assamese"},
    "sa": {"san", "sanskrit"},
    "ne": {"nep", "nepali"},
    "si": {"sin", "sinhala"},
    "fa": {"per", "fas", "persian", "farsi"},
    "ps": {"pus", "pashto"},
    "ku": {"kur", "kurdish"},
    "az": {"aze", "azerbaijani"},
    "kk": {"kaz", "kazakh"},
    "uz": {"uzb", "uzbek"},
    "tg": {"tgk", "tajik"},
    "tk": {"tuk", "turkmen"},
    "tl": {"tgl", "fil", "filipino", "tagalog"},
    "ms": {"may", "msa", "malay"},
    "km": {"khm", "khmer"},
    "lo": {"lao", "laotian"},
    "my": {"mya", "bur", "burmese", "myanmar"},
    "mn": {"mon", "mongolian"},
    "sw": {"swa", "swahili"},
    "am": {"amh", "amharic"},
    "ha": {"hau", "hausa"},
    "yo": {"yor", "yoruba"},
    "ig": {"ibo", "igbo"},
    "zu": {"zul", "zulu"},
    "xh": {"xho", "xhosa"},
    "sn": {"sna", "shona"},
    "so": {"som", "somali"},
    "sq": {"sqi", "alb", "albanian"},
    "hy": {"hye", "arm", "armenian"},
    "eu": {"eus", "baq", "basque"},
    "be": {"bel", "belarusian"},
    "bg": {"bul", "bulgarian"},
    "ca": {"cat", "catalan"},
    "hr": {"hrv", "croatian"},
    "et": {"est", "estonian"},
    "gl": {"glg", "galician"},
    "ka": {"kat", "geo", "georgian"},
    "is": {"isl", "ice", "icelandic"},
    "ga": {"gle", "irish"},
    "lv": {"lav", "latvian"},
    "lt": {"lit", "lithuanian"},
    "lb": {"ltz", "luxembourgish"},
    "mk": {"mkd", "mac", "macedonian"},
    "mt": {"mlt", "maltese"},
    "sk": {"slk", "slo", "slovak"},
    "sl": {"slv", "slovenian"},
    "cy": {"cym", "wel", "welsh"},
    "yi": {"yid", "yiddish"},
    "ht": {"hat", "haitian"},
    "sr": {"srp", "serbian"},
    "bs": {"bos", "bosnian"},
}

LANGUAGE_ALIAS_TO_CODE = {
    alias: code for code, aliases in LANGUAGE_CANONICAL.items() for alias in aliases
}


SUBTITLE_DESCRIPTOR_WORDS = {"forced", "sdh", "cc", "full", "commentary"}


def subtitle_descriptor_from_name(name):
    stem = name.rsplit(".", 1)[0] if "." in name else name
    segments = [s.lower() for s in re.split(r'[._\-\s]+', stem) if s]
    if segments and segments[-1] in SUBTITLE_DESCRIPTOR_WORDS:
        return segments[-1]
    return None


def language_tag_from_name(name):
    stem = name.rsplit(".", 1)[0] if "." in name else name
    segments = [s.lower() for s in re.split(r'[._\-\s]+', stem) if s]
    if len(segments) < 2:
        return None

    trimmed = list(segments)
    while len(trimmed) > 1 and trimmed[-1] in SUBTITLE_DESCRIPTOR_WORDS:
        trimmed.pop()

    tag_window = set(segments[-2:])
    last_only = {trimmed[-1]} if trimmed else set()

    has_en = bool(tag_window & ENGLISH_LANGUAGE_TOKENS)
    has_foreign = bool(tag_window & OTHER_LANGUAGE_TOKENS_RELIABLE) or bool(last_only & OTHER_LANGUAGE_TOKENS_RESTRICTED)

    if has_en and not has_foreign:
        return "en"
    if has_foreign and not has_en:
        return "foreign"
    return None


def specific_language_from_name(name):
    stem = name.rsplit(".", 1)[0] if "." in name else name
    segments = [s.lower() for s in re.split(r'[._\-\s]+', stem) if s]
    if len(segments) < 2:
        return None

    trimmed = list(segments)
    while len(trimmed) > 1 and trimmed[-1] in SUBTITLE_DESCRIPTOR_WORDS:
        trimmed.pop()

    tag_window = segments[-2:]
    last_only = trimmed[-1] if trimmed else None

    for seg in tag_window:
        if seg in ENGLISH_LANGUAGE_TOKENS:
            return "en"
        if seg in OTHER_LANGUAGE_TOKENS_RELIABLE:
            return LANGUAGE_ALIAS_TO_CODE.get(seg)

    if last_only and last_only in OTHER_LANGUAGE_TOKENS_RESTRICTED:
        return LANGUAGE_ALIAS_TO_CODE.get(last_only)

    return None


def resolve_subtitle_language(path):
    hint = specific_language_from_name(path.name)
    if hint:
        return hint
    detected = detect_subtitle_language(path)
    if detected and detected != "unknown":
        return LANGUAGE_ALIAS_TO_CODE.get(detected, detected)
    return None


def gather_video_subtitles(video_path, multi):
    folder = video_path.parent
    pattern = stem_match_pattern(video_path.stem) if multi else None
    matches = []

    search_dirs = [folder]
    try:
        for entry in sorted(folder.iterdir()):
            if entry.is_dir() and entry.name.lower() in SUBTITLE_FOLDER_NAMES:
                search_dirs.append(entry)
    except OSError:
        pass

    for search_dir in search_dirs:
        try:
            entries = sorted(search_dir.iterdir())
        except OSError:
            continue
        for item in entries:
            if not item.is_file() or item == video_path:
                continue
            ext = item.suffix.lower().lstrip(".")
            if ext not in SUBTITLE_EXTENSIONS:
                continue
            if multi and (pattern is None or not pattern.match(item.name)):
                continue
            matches.append(item)

    return matches


def compute_consolidated_subtitle_pairs(subtitles, new_video_path):
    new_stem = new_video_path.stem
    target_dir = new_video_path.parent / subtitle_folder_name()
    primary_lang = get_tmdb_language().split("-")[0].lower()

    by_ext = {}
    for item in subtitles:
        by_ext.setdefault(item.suffix.lower(), []).append(item)

    canonical = {}
    for ext, items in by_ext.items():
        detected = {i: resolve_subtitle_language(i) for i in items}
        group = [i for i in items if detected[i] == primary_lang]
        if not group:
            fallback = next((i for i in items if detected[i] is None), None)
            if fallback:
                group = [fallback]

        used_names = set()
        for item in group:
            descriptor = subtitle_descriptor_from_name(item.name) if len(group) > 1 else None
            lang_tag = f"{primary_lang}.{descriptor}" if descriptor else primary_lang
            name = subtitle_file_name(new_stem, ext.lstrip("."), lang_tag)
            if name in used_names:
                continue
            used_names.add(name)
            canonical[item] = name

    pairs = []
    for item in subtitles:
        dest_name = canonical.get(item, item.name)
        dest = target_dir / dest_name
        pairs.append((item, dest))

        if item.suffix.lower() == ".sub":
            idx_item = item.with_suffix(".idx")
            if idx_item.exists():
                if item in canonical:
                    idx_dest = target_dir / (canonical[item].rsplit(".", 1)[0] + ".idx")
                else:
                    idx_dest = target_dir / idx_item.name
                pairs.append((idx_item, idx_dest))

    return pairs


def unique_destination(dest):
    if not dest.exists():
        return dest
    stem = dest.stem
    ext = dest.suffix
    counter = 2
    while True:
        candidate = dest.parent / f"{stem} ({counter}){ext}"
        if not candidate.exists():
            return candidate
        counter += 1


def is_already_disambiguated(item, dest):
    if item.parent != dest.parent or item.suffix.lower() != dest.suffix.lower():
        return False
    pattern = re.compile(r'^' + re.escape(dest.stem) + r' \(\d+\)$', re.IGNORECASE)
    return bool(pattern.match(item.stem))


def rename_consolidated_subtitles(subtitles, new_video_path, log):
    renamed = 0
    source_dirs = set()
    for item, dest in compute_consolidated_subtitle_pairs(subtitles, new_video_path):
        if dest == item:
            continue
        if dest.exists() and not same_existing_path(dest, item):
            if is_already_disambiguated(item, dest):
                continue
            dest = unique_destination(dest)
        dest.parent.mkdir(exist_ok=True)
        source_dir = item.parent
        ok, err = safe_rename(item, dest)
        if not ok:
            print(f"Skipping subtitle ({err}): {item.name}")
            continue
        log.record(item, dest)
        label = "subtitle index" if dest.suffix.lower() == ".idx" else "subtitle"
        if source_dir == dest.parent:
            print(f"Renamed {label}: {item.name} -> {dest.name}")
        else:
            print(f"Moved {label}: {item.name} -> {dest.parent.name}/{dest.name}")
        renamed += 1
        if source_dir.name.lower() in SUBTITLE_FOLDER_NAMES:
            source_dirs.add(source_dir)

    for source_dir in source_dirs:
        try:
            if source_dir.exists() and not any(source_dir.iterdir()):
                source_dir.rmdir()
                print(f"Removed empty folder: {source_dir}")
        except OSError:
            pass

    return renamed


def preview_consolidated_subtitles(subtitles, new_video_path):
    for item, dest in compute_consolidated_subtitle_pairs(subtitles, new_video_path):
        if dest == item:
            continue
        label = "subtitle index" if dest.suffix.lower() == ".idx" else "subtitle"
        if item.parent == dest.parent:
            print(f"Renamed {label}: {item.name} -> {dest.name}")
        else:
            print(f"Moved {label}: {item.name} -> {dest.parent.name}/{dest.name}")


def organize_subtitle_folder(folder, log):
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return

    canonical_name = subtitle_folder_name()
    for entry in entries:
        if not entry.is_dir() or entry.name == canonical_name:
            continue
        if entry.name.lower() not in SUBTITLE_FOLDER_NAMES:
            continue

        dest = folder / canonical_name
        if dest.exists() and not same_existing_path(dest, entry):
            print(f"Skipping (target already exists): {entry.name} -> {canonical_name}")
            continue

        ok, err = safe_rename(entry, dest)
        if not ok:
            print(f"Skipping ({err}): {entry.name} -> {canonical_name}")
            continue
        log.record(entry, dest)
        print(f"Renamed folder: {entry.name} -> {canonical_name}")


def preview_subtitle_folder(folder):
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return

    canonical_name = subtitle_folder_name()
    for entry in entries:
        if not entry.is_dir() or entry.name == canonical_name:
            continue
        if entry.name.lower() not in SUBTITLE_FOLDER_NAMES:
            continue
        print(f"Renamed folder: {entry.name} -> {canonical_name}")


def list_subfolders(folder):
    return sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))


def preview_season_folders(folder):
    for sub in list_subfolders(folder):
        season = parse_season_folder_name(sub.name)
        if season is None:
            continue
        new_name = season_folder_name(season)
        if new_name != sub.name:
            print(f"Renamed folder: {sub.name} -> {new_name}")


def organize_season_folders(folder, log):
    renamed = 0
    for sub in list_subfolders(folder):
        season = parse_season_folder_name(sub.name)
        if season is None:
            continue
        new_name = season_folder_name(season)
        if new_name == sub.name:
            continue
        dest = folder / new_name
        if dest.exists() and not same_existing_path(dest, sub):
            print(f"Skipping (target already exists): {sub.name} -> {new_name}")
            continue
        ok, err = safe_rename(sub, dest)
        if not ok:
            print(f"Skipping ({err}): {sub.name} -> {new_name}")
            continue
        log.record(sub, dest)
        print(f"Renamed folder: {sub.name} -> {new_name}")
        renamed += 1
    return renamed


def resolve_season_folder_file(item, season, api_key=None, tmdb_id=None):
    se = parse_season_episode(item.name)
    if se:
        return se[0], se[1], se[2]

    file_season = parse_season_only(item.name)
    episode = parse_episode_only(item.name)
    if episode is None:
        resolved = resolve_absolute_episode(api_key, tmdb_id, item.name)
        if resolved is not None:
            return resolved[0], resolved[1], 'E'

    return file_season, episode, 'E'


def preview_season_folder_files(folder, final_name, api_key=None, tmdb_id=None):
    for sub in list_subfolders(folder):
        season = parse_season_folder_name(sub.name)
        if season is None:
            continue

        preview_subtitle_folder(sub)

        for item in list_video_files(sub):
            ext = item.suffix.lower().lstrip(".")
            file_season, episode, marker = resolve_season_folder_file(item, season, api_key, tmdb_id)
            target_season = file_season if file_season is not None else season
            subtitles = gather_video_subtitles(item, True)

            if episode is None:
                if target_season != season:
                    target_season_folder = season_folder_name(target_season)
                    dest = folder / target_season_folder / item.name
                    print(f"Move: {item.name} -> {target_season_folder}/{item.name}")
                    preview_consolidated_subtitles(subtitles, dest)
                else:
                    print(f"No episode found, already in {season_folder_name(season)}: {item.name}")
                continue

            new_name = episode_file_name(final_name, target_season, episode, ext, detect_resolution(item.name))
            if target_season != season:
                target_season_folder = season_folder_name(target_season)
                dest = folder / target_season_folder / new_name
                print(f"Move: {item.name} -> {target_season_folder}/{new_name}")
                preview_consolidated_subtitles(subtitles, dest)
            elif new_name != item.name:
                dest = sub / new_name
                print(f"Renamed file: {item.name} -> {new_name}")
                preview_consolidated_subtitles(subtitles, dest)
            else:
                preview_consolidated_subtitles(subtitles, item)

    preview_orphaned_subtitle_packs(folder, folder, None)


def rename_season_folder_files(folder, final_name, log, api_key=None, tmdb_id=None):
    renamed = 0
    skipped = 0

    for sub in list_subfolders(folder):
        season = parse_season_folder_name(sub.name)
        if season is None:
            continue

        organize_subtitle_folder(sub, log)

        for item in list_video_files(sub):
            ext = item.suffix.lower().lstrip(".")
            file_season, episode, marker = resolve_season_folder_file(item, season, api_key, tmdb_id)
            target_season = file_season if file_season is not None else season
            target_season_folder = season_folder_name(target_season)
            target_dir = folder / target_season_folder if target_season != season else sub
            subtitles = gather_video_subtitles(item, True)

            if episode is None:
                if target_season == season:
                    print(f"No episode found, already in {season_folder_name(season)}: {item.name}")
                    skipped += 1
                    continue
                new_name = item.name
            else:
                new_name = episode_file_name(final_name, target_season, episode, ext, detect_resolution(item.name))

            dest = target_dir / new_name
            if dest == item:
                renamed += rename_consolidated_subtitles(subtitles, item, log)
                continue
            if dest.exists() and not same_existing_path(dest, item):
                print(f"Skipping (target already exists): {item.name}")
                skipped += 1
                continue

            if target_dir != sub:
                target_dir.mkdir(exist_ok=True)
            ok, err = safe_rename(item, dest)
            if not ok:
                print(f"Skipping ({err}): {item.name}")
                skipped += 1
                continue
            log.record(item, dest)
            if target_dir != sub:
                print(f"Moved: {item.name} -> {target_season_folder}/{new_name}")
            else:
                print(f"Renamed file: {item.name} -> {new_name}")
            renamed += 1
            renamed += rename_consolidated_subtitles(subtitles, dest, log)

    renamed += merge_orphaned_subtitle_packs(folder, folder, None, log)

    return renamed, skipped


def infer_season_from_files(folder):
    for item in sorted(folder.rglob("*")):
        if not item.is_file() or item.suffix.lower().lstrip(".") not in VIDEO_EXTENSIONS:
            continue
        se = parse_season_episode(item.name)
        if se:
            return se[0]
        season = parse_season_only(item.name)
        if season is not None:
            return season
    return None


def find_matching_episode_video(target_season_dir, episode):
    for vid in list_video_files(target_season_dir):
        v_se = parse_season_episode(vid.name)
        v_episode = v_se[1] if v_se else parse_episode_only(vid.name)
        if v_episode == episode:
            return vid
    return None


def find_subtitle_pack_folder(container_folder):
    canonical_name = subtitle_folder_name()
    try:
        for entry in sorted(container_folder.iterdir()):
            if entry.is_dir() and (entry.name == canonical_name or entry.name.lower() in SUBTITLE_FOLDER_NAMES):
                return entry
    except OSError:
        pass
    return None


def resolve_subtitle_pack_episode(pack_name, fallback_season):
    se = parse_season_episode(pack_name)
    if se:
        return se[0], se[1]
    return parse_season_only(pack_name) or fallback_season, parse_episode_only(pack_name)


def merge_orphaned_subtitle_packs(container_folder, target_folder, season, log):
    moved = 0
    subs_folder = find_subtitle_pack_folder(container_folder)
    if subs_folder is None:
        return moved

    for pack in sorted(p for p in subs_folder.iterdir() if p.is_dir()):
        target_season, episode = resolve_subtitle_pack_episode(pack.name, season)
        if episode is None:
            print(f"Skipping subtitle pack (no episode number found): {pack.name}")
            continue
        if target_season is None:
            print(f"Skipping subtitle pack (unknown season): {pack.name}")
            continue

        target_season_dir = target_folder / season_folder_name(target_season)
        if not target_season_dir.is_dir():
            print(f"Skipping subtitle pack (no matching season in library): {pack.name}")
            continue

        video_match = find_matching_episode_video(target_season_dir, episode)
        if video_match is None:
            print(f"Skipping subtitle pack (no matching episode in library): {pack.name}")
            continue

        subtitle_files = [
            f for f in sorted(pack.rglob("*"))
            if f.is_file() and f.suffix.lower().lstrip(".") in SUBTITLE_EXTENSIONS
        ]
        if not subtitle_files:
            continue

        moved += rename_consolidated_subtitles(subtitle_files, video_match, log)

        try:
            for leftover in sorted(pack.rglob("*"), reverse=True):
                if leftover.is_dir() and not any(leftover.iterdir()):
                    leftover.rmdir()
            if pack.exists() and not any(pack.iterdir()):
                pack.rmdir()
        except OSError:
            pass

    try:
        if subs_folder.exists() and not any(subs_folder.iterdir()):
            subs_folder.rmdir()
            print(f"Removed empty folder: {subs_folder}")
    except OSError:
        pass

    return moved


def preview_orphaned_subtitle_packs(container_folder, target_folder, season):
    subs_folder = find_subtitle_pack_folder(container_folder)
    if subs_folder is None:
        return

    for pack in sorted(p for p in subs_folder.iterdir() if p.is_dir()):
        target_season, episode = resolve_subtitle_pack_episode(pack.name, season)
        if episode is None or target_season is None:
            continue

        target_season_dir = target_folder / season_folder_name(target_season)
        if not target_season_dir.is_dir():
            continue

        video_match = find_matching_episode_video(target_season_dir, episode)
        if video_match is None:
            continue

        subtitle_files = [
            f for f in sorted(pack.rglob("*"))
            if f.is_file() and f.suffix.lower().lstrip(".") in SUBTITLE_EXTENSIONS
        ]
        if not subtitle_files:
            continue

        preview_consolidated_subtitles(subtitle_files, video_match)


def merge_duplicate_show_folder(source_folder, target_folder, raw_name, final_name, log):
    moved = 0
    skipped = 0

    for sub in list_subfolders(source_folder):
        season = parse_season_folder_name(sub.name)
        if season is None:
            continue

        organize_subtitle_folder(sub, log)

        for item in list_video_files(sub):
            ext = item.suffix.lower().lstrip(".")
            file_season, episode, marker = resolve_season_folder_file(item, season)
            item_season = file_season if file_season is not None else season
            item_season_folder = season_folder_name(item_season)
            item_target_dir = target_folder / item_season_folder
            subtitles = gather_video_subtitles(item, True)

            new_name = item.name if episode is None else episode_file_name(
                final_name, item_season, episode, ext, detect_resolution(item.name)
            )
            dest = item_target_dir / new_name
            if dest.exists() and not same_existing_path(dest, item):
                print(f"Skipping (target already exists): {item.name}")
                skipped += 1
                continue

            item_target_dir.mkdir(exist_ok=True)
            ok, err = safe_rename(item, dest)
            if not ok:
                print(f"Skipping ({err}): {item.name}")
                skipped += 1
                continue
            log.record(item, dest)
            print(f"Moved: {item.name} -> {target_folder.name}/{item_season_folder}/{new_name}")
            moved += 1
            moved += rename_consolidated_subtitles(subtitles, dest, log)

        moved += merge_orphaned_subtitle_packs(sub, target_folder, season, log)

        try:
            if sub.exists() and not any(sub.iterdir()):
                sub.rmdir()
        except OSError:
            pass

    folder_season = parse_season_folder_name(raw_name)
    if folder_season is None:
        folder_season = infer_season_from_files(source_folder)

    organize_subtitle_folder(source_folder, log)

    for item in list_video_files(source_folder):
        ext = item.suffix.lower().lstrip(".")
        file_season, episode, marker = resolve_season_folder_file(item, folder_season)
        item_season = file_season if file_season is not None else folder_season
        subtitles = gather_video_subtitles(item, True)

        if item_season is None or episode is None:
            print(f"No season/episode found, skipping: {item.name}")
            skipped += 1
            continue

        item_season_folder = season_folder_name(item_season)
        item_target_dir = target_folder / item_season_folder
        new_name = episode_file_name(final_name, item_season, episode, ext, detect_resolution(item.name))
        dest = item_target_dir / new_name

        if dest.exists() and not same_existing_path(dest, item):
            print(f"Skipping (target already exists): {item.name}")
            skipped += 1
            continue

        item_target_dir.mkdir(exist_ok=True)
        ok, err = safe_rename(item, dest)
        if not ok:
            print(f"Skipping ({err}): {item.name}")
            skipped += 1
            continue
        log.record(item, dest)
        print(f"Moved: {item.name} -> {target_folder.name}/{item_season_folder}/{new_name}")
        moved += 1
        moved += rename_consolidated_subtitles(subtitles, dest, log)

    moved += merge_orphaned_subtitle_packs(source_folder, target_folder, folder_season, log)

    try:
        if source_folder.exists() and not any(source_folder.iterdir()):
            source_folder.rmdir()
            print(f"Removed empty folder: {source_folder}")
    except OSError:
        pass

    return moved, skipped


def preview_merge_duplicate_show_folder(source_folder, target_folder, raw_name, final_name):
    for sub in list_subfolders(source_folder):
        season = parse_season_folder_name(sub.name)
        if season is None:
            continue

        preview_subtitle_folder(sub)

        for item in list_video_files(sub):
            ext = item.suffix.lower().lstrip(".")
            file_season, episode, marker = resolve_season_folder_file(item, season)
            item_season = file_season if file_season is not None else season
            item_season_folder = season_folder_name(item_season)
            subtitles = gather_video_subtitles(item, True)

            new_name = item.name if episode is None else episode_file_name(
                final_name, item_season, episode, ext, detect_resolution(item.name)
            )
            dest = target_folder / item_season_folder / new_name
            print(f"Move: {item.name} -> {target_folder.name}/{item_season_folder}/{new_name}")
            preview_consolidated_subtitles(subtitles, dest)

        preview_orphaned_subtitle_packs(sub, target_folder, season)

    folder_season = parse_season_folder_name(raw_name)
    if folder_season is None:
        folder_season = infer_season_from_files(source_folder)

    preview_subtitle_folder(source_folder)

    for item in list_video_files(source_folder):
        ext = item.suffix.lower().lstrip(".")
        file_season, episode, marker = resolve_season_folder_file(item, folder_season)
        item_season = file_season if file_season is not None else folder_season
        subtitles = gather_video_subtitles(item, True)

        if item_season is None or episode is None:
            print(f"No season/episode found, skipping: {item.name}")
            continue

        item_season_folder = season_folder_name(item_season)
        new_name = episode_file_name(final_name, item_season, episode, ext, detect_resolution(item.name))
        dest = target_folder / item_season_folder / new_name
        print(f"Move: {item.name} -> {target_folder.name}/{item_season_folder}/{new_name}")
        preview_consolidated_subtitles(subtitles, dest)

    preview_orphaned_subtitle_packs(source_folder, target_folder, folder_season)


def preview_video_files(folder, media_type, final_name, api_key, tmdb_id=None):
    files = list_video_files(folder)

    if media_type == "movie":
        preview_subtitle_folder(folder)
        multi = len(files) > 1
        for item in files:
            ext = item.suffix.lower().lstrip(".")
            name = final_name
            if multi:
                per_name, _, _, error = lookup_folder(api_key, media_type, item.stem)
                if error:
                    print(error)
                    continue
                name = per_name
            new_name = movie_file_name(name, ext, detect_resolution(item.name))
            dest = folder / new_name
            subtitles = gather_video_subtitles(item, multi)
            if new_name != item.name:
                print(f"Renamed file: {item.name} -> {new_name}")
                preview_consolidated_subtitles(subtitles, dest)
            else:
                preview_consolidated_subtitles(subtitles, item)
        return

    for item in files:
        ext = item.suffix.lower().lstrip(".")
        se = parse_season_episode(item.name)
        subtitles = gather_video_subtitles(item, True)
        if se:
            season, episode, marker = se
            new_name = episode_file_name(final_name, season, episode, ext, detect_resolution(item.name))
            target_season_folder = season_folder_name(season)
            print(f"Move: {item.name} -> {target_season_folder}/{new_name}")
            preview_consolidated_subtitles(subtitles, folder / target_season_folder / new_name)
            continue

        season = parse_season_only(item.name)
        if season is None:
            resolved = resolve_absolute_episode(api_key, tmdb_id, item.name)
            if resolved is None:
                print(f"No season/episode found, skipping: {item.name}")
                continue
            season, episode = resolved
            new_name = episode_file_name(final_name, season, episode, ext, detect_resolution(item.name))
            target_season_folder = season_folder_name(season)
            print(f"Move: {item.name} -> {target_season_folder}/{new_name}")
            preview_consolidated_subtitles(subtitles, folder / target_season_folder / new_name)
            continue

        target_season_folder = season_folder_name(season)
        print(f"Move: {item.name} -> {target_season_folder}/{item.name}")
        preview_consolidated_subtitles(subtitles, folder / target_season_folder / item.name)


def rename_video_files(folder, media_type, final_name, log, api_key, tmdb_id=None):
    renamed = 0
    skipped = 0

    files = list_video_files(folder)

    if media_type == "movie":
        organize_subtitle_folder(folder, log)
        multi = len(files) > 1
        for item in files:
            ext = item.suffix.lower().lstrip(".")
            name = final_name
            if multi:
                per_name, _, _, error = lookup_folder(api_key, media_type, item.stem)
                if error:
                    print(error)
                    skipped += 1
                    continue
                name = per_name
            new_name = movie_file_name(name, ext, detect_resolution(item.name))
            dest = folder / new_name
            subtitles = gather_video_subtitles(item, multi)
            if dest == item:
                renamed += rename_consolidated_subtitles(subtitles, item, log)
                continue
            if dest.exists() and not same_existing_path(dest, item):
                print(f"Skipping (target already exists): {item.name}")
                skipped += 1
                continue
            ok, err = safe_rename(item, dest)
            if not ok:
                print(f"Skipping ({err}): {item.name}")
                skipped += 1
                continue
            log.record(item, dest)
            print(f"Renamed file: {item.name} -> {new_name}")
            renamed += 1
            renamed += rename_consolidated_subtitles(subtitles, dest, log)
        return renamed, skipped

    for item in files:
        ext = item.suffix.lower().lstrip(".")
        se = parse_season_episode(item.name)
        subtitles = gather_video_subtitles(item, True)

        if se:
            season, episode, marker = se
            new_name = episode_file_name(final_name, season, episode, ext, detect_resolution(item.name))
        else:
            season = parse_season_only(item.name)
            if season is None:
                resolved = resolve_absolute_episode(api_key, tmdb_id, item.name)
                if resolved is None:
                    print(f"No season/episode found, skipping: {item.name}")
                    skipped += 1
                    continue
                season, episode = resolved
                new_name = episode_file_name(final_name, season, episode, ext, detect_resolution(item.name))
            else:
                new_name = item.name

        target_season_folder = season_folder_name(season)
        season_dir = folder / target_season_folder
        dest = season_dir / new_name

        if dest == item:
            renamed += rename_consolidated_subtitles(subtitles, item, log)
            continue
        if dest.exists() and not same_existing_path(dest, item):
            print(f"Skipping (target already exists): {item.name}")
            skipped += 1
            continue

        season_dir.mkdir(exist_ok=True)
        ok, err = safe_rename(item, dest)
        if not ok:
            print(f"Skipping ({err}): {item.name}")
            skipped += 1
            continue
        log.record(item, dest)
        print(f"Moved: {item.name} -> {target_season_folder}/{new_name}")
        renamed += 1
        renamed += rename_consolidated_subtitles(subtitles, dest, log)

    return renamed, skipped


def compute_folder_signature(folder):
    entries = []
    for root, dirs, files in os.walk(folder):
        rel_root = os.path.relpath(root, folder)
        for name in dirs + files:
            entries.append(os.path.normpath(os.path.join(rel_root, name)))

    hasher = hashlib.sha256()
    for entry in sorted(entries):
        hasher.update(entry.encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def load_scan_cache():
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_scan_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def parse_simple_yaml_list(path, key):
    if not path.exists():
        return []

    items = []
    in_list = False
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(f"{key}:"):
            in_list = True
            continue
        if in_list and line.startswith("-"):
            value = line[1:].strip().strip('"').strip("'")
            if value:
                items.append(value)
        else:
            in_list = False

    return items


def load_delete_folder_names():
    return [name.lower() for name in parse_simple_yaml_list(DELETE_FILE, "folders")]


TRAILING_WRAPPER_CHARS = ")]}'\""


def split_patterns(patterns):
    include = []
    exclude = []
    for pattern in patterns:
        if pattern.startswith("!"):
            exclude.append(pattern[1:])
        else:
            include.append(pattern)
    return include, exclude


def match_with_wrapper_stripping(name_lower, patterns):
    if any(fnmatch.fnmatch(name_lower, pattern) for pattern in patterns):
        return True
    stripped = name_lower.rstrip(TRAILING_WRAPPER_CHARS)
    if stripped and stripped != name_lower:
        return any(fnmatch.fnmatch(stripped, pattern) for pattern in patterns)
    return False


def matches_folder_name(name, patterns):
    include, exclude = split_patterns(patterns)
    if not match_with_wrapper_stripping(name.lower(), include):
        return False
    if exclude and match_with_wrapper_stripping(name.lower(), exclude):
        return False
    return True


def load_delete_file_patterns():
    return [p.lower() for p in parse_simple_yaml_list(DELETE_FILE, "files")]


def load_delete_subtitle_rules():
    keep_languages = set()
    delete_languages = set()
    for entry in parse_simple_yaml_list(DELETE_FILE, "subtitles"):
        is_keep = entry.startswith("!")
        name = entry[1:] if is_keep else entry
        code = LANGUAGE_ALIAS_TO_CODE.get(name.strip().lower())
        if not code:
            continue
        if is_keep:
            keep_languages.add(code)
        else:
            delete_languages.add(code)
    return keep_languages, delete_languages


def subtitle_language_cleanup_target(item, keep_languages, delete_languages):
    if not keep_languages and not delete_languages:
        return False
    ext = item.suffix.lower().lstrip(".")
    if ext not in SUBTITLE_EXTENSIONS and ext != "idx":
        return False
    lang = resolve_subtitle_language(item)
    if keep_languages:
        if lang is None:
            return False
        return lang not in keep_languages
    if lang is None:
        return False
    return lang in delete_languages


def parse_idx_language(path):
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None
    match = re.search(r'^\s*id:\s*([a-zA-Z]{2,3})', text, re.MULTILINE)
    if match:
        return match.group(1).lower()
    return None


def count_unicode_range(text, lo, hi):
    return sum(1 for ch in text if lo <= ord(ch) <= hi)


SUBTITLE_SCRIPT_RANGES = {
    "ru": (0x0400, 0x04FF),
    "el": (0x0370, 0x03FF),
    "he": (0x0590, 0x05FF),
    "ar": (0x0600, 0x06FF),
    "th": (0x0E00, 0x0E7F),
    "hi": (0x0900, 0x097F),
    "ko": (0xAC00, 0xD7A3),
    "ja": (0x3040, 0x30FF),
    "zh": (0x4E00, 0x9FFF),
}

SUBTITLE_STOPWORDS = {
    "en": {"the", "and", "is", "of", "in", "to", "a", "that", "it", "you", "was",
           "for", "on", "are", "with", "as", "this", "have", "be", "not", "but",
           "he", "she", "they", "we", "his", "her", "at", "from", "by", "or",
           "an", "if", "what", "so", "all", "can", "just", "one", "like", "get",
           "know", "will", "would", "there", "when", "who", "how", "out", "up",
           "about", "then", "them", "were", "been", "had", "do", "did", "yes", "no"},
    "fr": {"le", "la", "les", "de", "et", "un", "une", "des", "est", "que", "qui",
           "pas", "pour", "dans", "ce", "il", "elle", "vous", "je", "nous", "au",
           "du", "se", "ne", "tu", "on", "avec", "sur", "son", "sa", "ses",
           "mais", "comme", "tout", "ça", "oui", "non"},
    "es": {"el", "la", "los", "las", "de", "y", "un", "una", "es", "que", "no",
           "por", "con", "para", "en", "se", "su", "lo", "como", "más", "pero",
           "le", "les", "yo", "tu", "este", "esta", "ese", "esa", "sí"},
    "de": {"der", "die", "das", "und", "ist", "nicht", "ein", "eine", "zu", "den",
           "mit", "auf", "für", "sich", "du", "ich", "er", "sie", "wir", "es",
           "war", "sind", "aber", "was", "wie", "wenn", "ja", "nein"},
    "it": {"il", "la", "di", "e", "un", "una", "che", "non", "per", "con", "del",
           "della", "sono", "questo", "questa", "ma", "come", "io", "tu", "lui",
           "lei", "noi", "sì", "no"},
    "pt": {"o", "a", "os", "as", "de", "e", "um", "uma", "que", "não", "por",
           "com", "para", "em", "se", "seu", "sua", "como", "mas", "eu", "tu",
           "ele", "ela", "nós", "sim"},
    "nl": {"de", "het", "een", "en", "is", "niet", "van", "dat", "je", "ik",
           "hij", "zij", "wij", "met", "voor", "op", "aan", "maar", "zoals",
           "ja", "nee"},
}


def detect_srt_language(path):
    try:
        raw = path.read_bytes()
    except OSError:
        return "unknown"

    text = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        return "unknown"

    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        lines.append(line)
    sample = " ".join(lines)[:20000]
    if not sample.strip():
        return "unknown"

    for lang, (lo, hi) in SUBTITLE_SCRIPT_RANGES.items():
        if count_unicode_range(sample, lo, hi) >= 5:
            vprint(f"    {path.name}: detected non-Latin script -> {lang}")
            return lang

    words = re.findall(r"[a-zà-öø-ÿ]+", sample.lower())
    total = len(words)
    if total < 20:
        vprint(f"    {path.name}: only {total} word(s) sampled, too little to classify -> unknown")
        return "unknown"

    scores = {lang: sum(1 for w in words if w in stopwords) for lang, stopwords in SUBTITLE_STOPWORDS.items()}
    best_lang = max(scores, key=scores.get)
    best_score = scores[best_lang]
    en_score = scores.get("en", 0)
    vprint(f"    {path.name}: word count={total}, scores={scores}")

    if best_lang == "en":
        vprint(f"    {path.name}: classified as en")
        return "en"
    if best_score >= 5 and best_score >= en_score * 1.5 and best_score >= en_score + 3:
        vprint(f"    {path.name}: classified as {best_lang} (en_score={en_score}, {best_lang}_score={best_score})")
        return best_lang
    vprint(f"    {path.name}: inconclusive (en_score={en_score}, best={best_lang}:{best_score}) -> unknown")
    return "unknown"


def detect_subtitle_language(path):
    ext = path.suffix.lower()
    if ext == ".idx":
        return parse_idx_language(path) or "unknown"
    if ext == ".sub":
        idx_path = path.with_suffix(".idx")
        if idx_path.exists():
            lang = parse_idx_language(idx_path)
            if lang:
                return lang
        return "unknown"
    if ext == ".srt":
        return detect_srt_language(path)
    return "unknown"


LANGUAGE_EXCLUDE_TOKEN = "language:en"


def subtitle_language_hint_from_name(name):
    return language_tag_from_name(name)


def matches_file_name(name, patterns, path=None):
    include, exclude = split_patterns(patterns)
    if not match_with_wrapper_stripping(name.lower(), include):
        return False

    literal_exclude = [p for p in exclude if p != LANGUAGE_EXCLUDE_TOKEN]
    if literal_exclude and match_with_wrapper_stripping(name.lower(), literal_exclude):
        return False

    if LANGUAGE_EXCLUDE_TOKEN in exclude and path is not None:
        hint = subtitle_language_hint_from_name(name)
        if hint == "en":
            return False
        if hint is None:
            detected = detect_subtitle_language(path)
            if detected in ("en", "unknown"):
                return False

    return True


def find_cleanup_targets(share, delete_folder_names, delete_file_patterns, keep_languages=None, delete_languages=None):
    targets = []
    keep_languages = keep_languages or set()
    delete_languages = delete_languages or set()

    def walk(folder):
        vprint(f"Scanning folder: {folder}")
        try:
            entries = sorted(folder.iterdir())
        except OSError as e:
            vprint(f"  Could not read folder: {e}")
            return
        for entry in entries:
            if entry.is_dir():
                if matches_folder_name(entry.name, delete_folder_names):
                    vprint(f"  Match (folder name): {entry}")
                    targets.append(("folder", entry))
                    continue
                vprint(f"  No match, descending into: {entry}")
                walk(entry)
            elif entry.is_file():
                ext = entry.suffix.lower().lstrip(".")
                if ext in VIDEO_EXTENSIONS:
                    vprint(f"  Skipping (protected video file): {entry}")
                    continue
                if matches_file_name(entry.name, delete_file_patterns, entry):
                    vprint(f"  Match (file pattern): {entry}")
                    targets.append(("file", entry))
                elif subtitle_language_cleanup_target(entry, keep_languages, delete_languages):
                    vprint(f"  Match (subtitle language): {entry}")
                    targets.append(("file", entry))
                else:
                    vprint(f"  No match: {entry}")

    walk(Path(share))
    return targets


def trash_path_for(share, item, timestamp):
    rel = item.relative_to(share)
    share_label = Path(share).name
    return CLEANUP_TRASH_DIR / timestamp / share_label / rel


def remove_empty_folders(share, confirm_all, dry_run=False):
    removed = 0
    skipped = 0
    removed_paths = set()
    share_path = Path(share).resolve()
    for root, dirs, files in os.walk(share, topdown=False):
        root_path = Path(root)
        if root_path.resolve() == share_path:
            continue
        try:
            remaining = [c for c in root_path.iterdir() if c not in removed_paths]
        except OSError as e:
            vprint(f"  Could not read {root_path}: {e}")
            continue
        if remaining:
            continue

        if dry_run:
            print(f"Would remove empty folder: {root_path}")
            removed_paths.add(root_path)
            removed += 1
            continue

        if not confirm_all:
            choice = confirm_delete_choice(f"Remove empty folder: {root_path}?")
            if choice == "a":
                confirm_all = True
            elif choice == "n":
                print(f"Skipped: {root_path}")
                skipped += 1
                continue

        try:
            root_path.rmdir()
            print(f"Removed empty folder: {root_path}")
            removed_paths.add(root_path)
            removed += 1
        except OSError as e:
            vprint(f"  Could not remove {root_path}: {e}")
    return removed, skipped


def run_cleanup(args, log):
    path_arg = args.cleanup if isinstance(args.cleanup, str) else None
    share = resolve_share(path_arg)
    log.set_label(f"{Path(share).name}-cleanup")

    print()
    print("Performing action: Cleanup")

    delete_folder_names = load_delete_folder_names()
    delete_file_patterns = load_delete_file_patterns()
    keep_languages, delete_languages = load_delete_subtitle_rules()
    vprint(f"Loaded {len(delete_folder_names)} folder pattern(s) from {DELETE_FILE.name}: {delete_folder_names}")
    vprint(f"Loaded {len(delete_file_patterns)} file pattern(s) from {DELETE_FILE.name}: {delete_file_patterns}")
    vprint(f"Loaded subtitle rules: keep={keep_languages or 'none'}, delete={delete_languages or 'none'}")

    if not delete_folder_names and not delete_file_patterns and not keep_languages and not delete_languages:
        print("No cleanup rules configured.")
        print(f"Edit {DELETE_FILE.name} to enable cleanup.")
        return

    print()
    print(f"Scanning: {share}")
    print()

    targets = find_cleanup_targets(share, delete_folder_names, delete_file_patterns, keep_languages, delete_languages)
    vprint(f"Scan complete. {len(targets)} item(s) matched.")

    test_mode = args.test is not None
    test_limit = args.test or 0

    if test_mode:
        shown = 0
        for kind, item in targets:
            if test_limit > 0 and shown >= test_limit:
                break
            print(f"Would move to trash ({kind}): {item}")
            shown += 1
        empty_preview, _ = remove_empty_folders(share, True, dry_run=True)
        print()
        if not targets and not empty_preview:
            print("Nothing to clean up.")
        else:
            print(
                f"Test mode: {len(targets)} item(s) would be moved to trash, "
                f"{empty_preview} empty folder(s) would be removed. No changes were made."
            )
        return

    if targets:
        print(f"Found {len(targets)} item(s) to move to trash:")
        for kind, item in targets:
            print(f"  [{kind}] {item}")
        print()

    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    moved_folders = 0
    moved_files = 0
    skipped_folders = 0
    skipped_files = 0
    confirm_all = args.yes

    for kind, item in targets:
        if not item.exists():
            if kind == "folder":
                skipped_folders += 1
            else:
                skipped_files += 1
            continue

        if not confirm_all:
            choice = confirm_delete_choice(f"Move to trash ({kind}): {item}?")
            if choice == "a":
                confirm_all = True
            elif choice == "n":
                print(f"Skipped: {item}")
                if kind == "folder":
                    skipped_folders += 1
                else:
                    skipped_files += 1
                continue

        dest = trash_path_for(share, item, timestamp)
        if dest.exists():
            print(f"Skipping (trash target already exists): {item}")
            if kind == "folder":
                skipped_folders += 1
            else:
                skipped_files += 1
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        vprint(f"Moving {item} -> {dest}")
        shutil.move(str(item), str(dest))
        log.record(item, dest)
        print(f"Moved to trash: {item}")
        if kind == "folder":
            moved_folders += 1
        else:
            moved_files += 1

    empty_removed, empty_skipped = remove_empty_folders(share, confirm_all)

    folders_deleted = moved_folders + empty_removed
    folders_deleted_skipped = skipped_folders + empty_skipped

    if not targets and not folders_deleted and not moved_files and not folders_deleted_skipped and not skipped_files:
        print("Nothing to clean up.")
        return

    print()
    print("=== Summary ===")
    print(f"Folders deleted: {folders_deleted}, skipped: {folders_deleted_skipped}")
    print(f"Files deleted: {moved_files}, skipped: {skipped_files}")

    log_path = log.save(label=f"{Path(share).name}-cleanup")
    if log_path:
        print(f"Log saved: {log_path}")
        print(f"Trash location: {CLEANUP_TRASH_DIR / timestamp}")
        print(f"Run --restore {log_path.name} to undo, or delete the trash folder once you're confident.")


def list_loose_video_files(share):
    return [
        item for item in sorted(Path(share).iterdir())
        if item.is_file() and item.suffix.lower().lstrip(".") in VIDEO_EXTENSIONS
    ]


def process_loose_movie_files(share, api_key, log, test_mode, test_limit, args):
    files = list_loose_video_files(share)
    if not files:
        return 0, 0, 0, 0

    print(f"Found {len(files)} loose movie file(s) directly in {Path(share).name} with no folder of their own:")
    print()

    folders_renamed = 0
    folders_skipped = 0
    files_renamed = 0
    files_skipped = 0
    shown = 0
    total = len(files)

    for index, item in enumerate(files, start=1):
        if test_mode and test_limit > 0 and shown >= test_limit:
            break

        print(f"[{index}/{total}] {item.name}")
        final_name, match_year, _, error = lookup_folder(api_key, "movie", item.stem)
        if error:
            print(error)
            shown += 1
            folders_skipped += 1
            continue

        folder_name = folder_target_name("movie", final_name, match_year, item.stem)
        target_folder = Path(share) / folder_name
        ext = item.suffix.lower().lstrip(".")
        new_name = movie_file_name(final_name, ext, detect_resolution(item.name))
        dest = target_folder / new_name
        subtitles = gather_video_subtitles(item, True)

        if test_mode:
            print(f"Move: {item.name} -> {folder_name}/{new_name}")
            preview_consolidated_subtitles(subtitles, dest)
            shown += 1
            folders_renamed += 1
            files_renamed += 1
            continue

        if not args.yes and not confirm(f"Create '{folder_name}' and move '{item.name}' into it?"):
            print(f"Skipped: {item.name}")
            folders_skipped += 1
            continue

        if dest.exists() and not same_existing_path(dest, item):
            print(f"Skipping (target already exists): {item.name}")
            folders_skipped += 1
            continue

        target_folder.mkdir(exist_ok=True)
        ok, err = safe_rename(item, dest)
        if not ok:
            print(f"Skipping ({err}): {item.name}")
            folders_skipped += 1
            continue
        log.record(item, dest)
        print(f"Moved: {item.name} -> {folder_name}/{new_name}")
        folders_renamed += 1
        files_renamed += 1
        files_renamed += rename_consolidated_subtitles(subtitles, dest, log)

    print()
    return folders_renamed, folders_skipped, files_renamed, files_skipped


def process_loose_tv_files(share, api_key, log, test_mode, test_limit, args):
    files = list_loose_video_files(share)
    if not files:
        return 0, 0, 0, 0

    print(f"Found {len(files)} loose TV episode file(s) directly in {Path(share).name} with no show folder of their own:")
    print()

    folders_renamed = 0
    folders_skipped = 0
    files_renamed = 0
    files_skipped = 0
    shown = 0
    total = len(files)

    for index, item in enumerate(files, start=1):
        if test_mode and test_limit > 0 and shown >= test_limit:
            break

        print(f"[{index}/{total}] {item.name}")
        final_name, match_year, match_id, error = lookup_folder(api_key, "tv", item.stem)
        if error:
            print(error)
            shown += 1
            files_skipped += 1
            continue

        se = parse_season_episode(item.name)
        if se:
            season, episode, marker = se
        else:
            resolved = resolve_absolute_episode(api_key, match_id, item.name)
            if resolved is None:
                print(f"No season/episode found, skipping: {item.name}")
                shown += 1
                files_skipped += 1
                continue
            season, episode = resolved

        folder_name = folder_target_name("tv", final_name, match_year, item.stem)
        target_folder = Path(share) / folder_name
        target_season_folder_name = season_folder_name(season)
        ext = item.suffix.lower().lstrip(".")
        new_name = episode_file_name(final_name, season, episode, ext, detect_resolution(item.name))
        dest = target_folder / target_season_folder_name / new_name
        subtitles = gather_video_subtitles(item, True)

        if test_mode:
            print(f"Move: {item.name} -> {folder_name}/{target_season_folder_name}/{new_name}")
            preview_consolidated_subtitles(subtitles, dest)
            shown += 1
            folders_renamed += 1
            files_renamed += 1
            continue

        if not args.yes and not confirm(f"Move '{item.name}' into show folder '{folder_name}'?"):
            print(f"Skipped: {item.name}")
            files_skipped += 1
            continue

        if dest.exists() and not same_existing_path(dest, item):
            print(f"Skipping (target already exists): {item.name}")
            files_skipped += 1
            continue

        (target_folder / target_season_folder_name).mkdir(parents=True, exist_ok=True)
        ok, err = safe_rename(item, dest)
        if not ok:
            print(f"Skipping ({err}): {item.name}")
            files_skipped += 1
            continue
        log.record(item, dest)
        print(f"Moved: {item.name} -> {folder_name}/{target_season_folder_name}/{new_name}")
        folders_renamed += 1
        files_renamed += 1
        files_renamed += rename_consolidated_subtitles(subtitles, dest, log)

    print()
    return folders_renamed, folders_skipped, files_renamed, files_skipped


LOOKUP_WORKERS = 8


def prefetch_lookups(api_key, media_type, folders):
    lookup_cache = {}
    if not folders:
        return lookup_cache

    print(f"Looking up {len(folders)} folder(s) on TMDb...")

    global vprint
    real_vprint = vprint
    if not VERBOSE:
        vprint = lambda *a, **k: None

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as executor:
            futures = {}
            for folder in folders:
                raw_name = folder.name
                hint_year = None
                if extract_year(raw_name) is None:
                    hint_year = infer_year_from_files(folder)
                futures[executor.submit(lookup_folder, api_key, media_type, raw_name, hint_year)] = (folder, hint_year)

            for future in concurrent.futures.as_completed(futures):
                folder, hint_year = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = (None, None, None, f"Lookup failed: {folder.name} ({e})")
                lookup_cache[folder] = (hint_year, result)
    finally:
        vprint = real_vprint

    print()
    return lookup_cache


def run_scan(args, log):
    path_arg = args.rename if isinstance(args.rename, str) else None
    share = resolve_share(path_arg)
    log.set_label(Path(share).name)

    cache = load_scan_cache()
    share_key = str(Path(share).resolve())
    baseline = cache.get(share_key)
    if not isinstance(baseline, dict):
        baseline = {}
    vprint(f"Baseline has {len(baseline)} folder(s) recorded")

    media_type = determine_media_type(share, getattr(args, "type", None))

    print()
    print("Performing action: Rename")
    print()
    print(f"Scanning: {share}")
    print()

    api_key = get_api_key()
    get_tmdb_language()

    test_mode = args.test is not None
    test_limit = args.test or 0

    share_path = Path(share)
    is_single_show = media_type == "tv" and looks_like_single_show_folder(share_path)
    if is_single_show:
        vprint(f"  {share_path.name} looks like a single show's folder, processing it directly")
        subfolders = [share_path]
    else:
        subfolders = sorted(
            p for p in share_path.iterdir() if p.is_dir() and not p.name.startswith(".")
        )
    loose_files = [] if is_single_show else list_loose_video_files(share)

    unchanged_count = 0
    if not args.force:
        to_process = []
        for folder in subfolders:
            signature = compute_folder_signature(folder)
            if baseline.get(folder.name) == signature:
                vprint(f"Unchanged, skipping: {folder.name}")
                unchanged_count += 1
                continue
            to_process.append(folder)

        if not to_process and not loose_files:
            print(f"No changes detected since last scan ({unchanged_count} folder(s) unchanged). Use --force to force a full scan.")
            return

        subfolders = to_process
        if unchanged_count:
            print(f"Skipping {unchanged_count} unchanged folder(s) ({len(subfolders)} to process). Use --force to force a full scan.")
            print()

    examples_shown = 0
    folders_renamed = 0
    folders_skipped = 0
    files_renamed = 0
    files_skipped = 0
    simulated_tv_folder_names = set()

    total_folders = len(subfolders)

    prefetch_targets = subfolders[:test_limit] if (test_mode and test_limit > 0) else subfolders
    lookup_cache = prefetch_lookups(api_key, media_type, prefetch_targets)

    for index, folder in enumerate(subfolders, start=1):
        if test_mode and test_limit > 0 and examples_shown >= test_limit:
            break

        raw_name = folder.name
        print(f"[{index}/{total_folders}] {raw_name}")
        vprint(f"Processing folder: {folder}")

        if folder in lookup_cache:
            hint_year, (final_name, match_year, match_id, error) = lookup_cache[folder]
            if hint_year is not None:
                vprint(f"  No year in folder name, inferred from files: {hint_year}")
        else:
            hint_year = None
            if extract_year(raw_name) is None:
                hint_year = infer_year_from_files(folder)
                vprint(f"  No year in folder name, inferred from files: {hint_year}")
            final_name, match_year, match_id, error = lookup_folder(api_key, media_type, raw_name, hint_year)

        if error:
            print(error)
            examples_shown += 1
            folders_skipped += 1
            continue

        folder_name = folder_target_name(media_type, final_name, match_year, raw_name)

        if test_mode:
            new_folder = folder.parent / folder_name
            needs_rename = new_folder != folder
            target_already_taken = (
                new_folder.is_dir() or folder_name in simulated_tv_folder_names
            ) and not same_existing_path(new_folder, folder)

            if media_type == "tv" and needs_rename and target_already_taken:
                print(f"Would merge into existing show folder: {raw_name} -> {folder_name}")
                preview_merge_duplicate_show_folder(folder, new_folder, raw_name, final_name)
                examples_shown += 1
                continue

            if needs_rename:
                print(f"Renamed folder: {raw_name} -> {folder_name}")
            else:
                print(f"Skipping renaming (already correctly named): {raw_name}")
            if media_type == "tv":
                simulated_tv_folder_names.add(folder_name)
                preview_season_folders(folder)
                preview_season_folder_files(folder, final_name, api_key, match_id)
            preview_video_files(folder, media_type, final_name, api_key, match_id)
            examples_shown += 1
            continue

        new_folder = folder.parent / folder_name
        needs_rename = new_folder != folder

        if needs_rename and media_type == "tv" and new_folder.is_dir() and not same_existing_path(new_folder, folder):
            if not args.yes and not confirm(f"Merge '{raw_name}' into existing show folder '{folder_name}'?"):
                print(f"Skipped: {raw_name}")
                folders_skipped += 1
                continue

            merged, merge_skipped = merge_duplicate_show_folder(folder, new_folder, raw_name, final_name, log)
            files_renamed += merged
            files_skipped += merge_skipped
            folders_renamed += 1
            continue

        if needs_rename:
            if not args.yes and not confirm(f"Rename '{raw_name}' -> '{folder_name}'?"):
                print(f"Skipped: {raw_name}")
                folders_skipped += 1
                continue

            if new_folder.exists() and not same_existing_path(new_folder, folder):
                print(f"Skipping (target already exists): {raw_name} -> {folder_name}")
                folders_skipped += 1
                continue

            ok, err = safe_rename(folder, new_folder)
            if not ok:
                print(f"Skipping ({err}): {raw_name} -> {folder_name}")
                folders_skipped += 1
                continue
            log.record(folder, new_folder)
            print(f"Renamed folder: {raw_name} -> {folder_name}")
            folders_renamed += 1
        else:
            print(f"Skipping renaming (already correctly named): {raw_name}")
        folder = new_folder

        if media_type == "tv":
            organize_season_folders(folder, log)
            sub_renamed, sub_skipped = rename_season_folder_files(folder, final_name, log, api_key, match_id)
            files_renamed += sub_renamed
            files_skipped += sub_skipped

        renamed, skipped = rename_video_files(folder, media_type, final_name, log, api_key, match_id)
        files_renamed += renamed
        files_skipped += skipped

        baseline[folder.name] = compute_folder_signature(folder)

    if loose_files:
        loose_processor = process_loose_movie_files if media_type == "movie" else process_loose_tv_files
        loose_folders_renamed, loose_folders_skipped, loose_files_renamed, loose_files_skipped = loose_processor(
            share, api_key, log, test_mode, test_limit, args
        )
        folders_renamed += loose_folders_renamed
        folders_skipped += loose_folders_skipped
        files_renamed += loose_files_renamed
        files_skipped += loose_files_skipped

    print()
    if test_mode:
        print("Test mode: no changes were made.")
        return

    print("=== Summary ===")
    print(f"Folders renamed: {folders_renamed}, skipped: {folders_skipped}")
    print(f"Files renamed: {files_renamed}, skipped: {files_skipped}")

    log_path = log.save(label=Path(share).name)
    if log_path:
        print(f"Log saved: {log_path}")

    current_names = {
        p.name for p in Path(share).iterdir() if p.is_dir() and not p.name.startswith(".")
    }
    baseline = {name: sig for name, sig in baseline.items() if name in current_names}
    cache[share_key] = baseline
    save_scan_cache(cache)


def collect_local_episodes(show_folder):
    local = {}

    for sub in list_subfolders(show_folder):
        season = parse_season_folder_name(sub.name)
        if season is None:
            continue
        for item in list_video_files(sub):
            file_season, episode, marker = resolve_season_folder_file(item, season)
            target_season = file_season if file_season is not None else season
            if episode is not None:
                local.setdefault(target_season, set()).add(episode)

    for item in list_video_files(show_folder):
        se = parse_season_episode(item.name)
        if se:
            local.setdefault(se[0], set()).add(se[1])

    return local


def check_show_episodes(api_key, show_folder):
    raw_name = show_folder.name
    final_name, match_year, tmdb_id, error = lookup_folder(api_key, "tv", raw_name)
    if error or not tmdb_id:
        return None, error or f"Could not resolve on TMDb: {raw_name}"

    details, err = tmdb_tv_details(api_key, tmdb_id)
    if err:
        return None, f"Could not fetch show details: {raw_name} ({err})"

    local = collect_local_episodes(show_folder)
    today = datetime.date.today().isoformat()
    missing = {}

    for season_info in details.get("seasons", []):
        season_number = season_info.get("season_number")
        if season_number is None or season_number == 0:
            continue

        season_data, err = tmdb_tv_season(api_key, tmdb_id, season_number)
        if err:
            vprint(f"  Could not fetch season {season_number} for {raw_name}: {err}")
            continue

        local_episodes = local.get(season_number, set())
        season_missing = []
        for ep in season_data.get("episodes", []):
            ep_number = ep.get("episode_number")
            if ep_number is None or ep_number in local_episodes:
                continue
            air_date = ep.get("air_date")
            if not air_date or air_date > today:
                continue
            season_missing.append(ep_number)

        if season_missing:
            missing[season_number] = sorted(season_missing)

    return missing, None


SPECIALS_FOLDER_PATTERN = re.compile(r'^specials?$', re.IGNORECASE)


def looks_like_single_show_folder(folder):
    subfolders = list_subfolders(folder)
    if subfolders:
        season_like = sum(
            1 for s in subfolders
            if parse_season_folder_name(s.name) is not None or SPECIALS_FOLDER_PATTERN.match(s.name)
        )
        return season_like > len(subfolders) / 2
    return bool(list_video_files(folder))


def run_episode_check(args):
    path_arg = args.episodes if isinstance(args.episodes, str) else None
    share = resolve_share(path_arg)

    media_type = getattr(args, "type", None) or infer_media_type(share)
    if media_type == "movie":
        print("This looks like a Movies share. --episodes only checks TV show libraries.")
        return

    print()
    print("Performing action: Episode Check")
    print()
    print(f"Scanning: {share}")
    print()

    api_key = get_api_key()
    get_tmdb_language()

    share_path = Path(share)

    if looks_like_single_show_folder(share_path):
        vprint(f"  {share_path.name} looks like a single show's folder (season subfolders found), checking it directly")
        shows = [share_path]
    else:
        shows = sorted(
            p for p in share_path.iterdir() if p.is_dir() and not p.name.startswith(".")
        )

    checked = 0
    incomplete = 0
    missing_episode_count = 0
    lookup_errors = 0

    for index, show_folder in enumerate(shows, start=1):
        vprint(f"[{index}/{len(shows)}] Checking: {show_folder.name}")
        missing, error = check_show_episodes(api_key, show_folder)

        if error:
            print(f"{show_folder.name}: {error}")
            lookup_errors += 1
            continue

        checked += 1
        if not missing:
            continue

        incomplete += 1
        print(f"{show_folder.name}:")
        for season_number in sorted(missing):
            episodes = missing[season_number]
            missing_episode_count += len(episodes)
            ep_list = ", ".join(f"E{e:02d}" for e in episodes)
            print(f"  S{season_number:02d}: missing {ep_list}")

    print()
    print("=== Summary ===")
    print(f"Shows checked: {checked}, incomplete: {incomplete}, missing episodes: {missing_episode_count}")
    if lookup_errors:
        print(f"Shows skipped due to lookup errors: {lookup_errors}")


def run_backup(args):
    mounts = find_smb_mounts()
    if not mounts:
        print("No SMB shares found.")
        sys.exit(1)

    share = select_mount(mounts)

    print()
    print(f"Scanning: {share}")
    print()

    paths = []
    for root, dirs, files in os.walk(share):
        rel_root = os.path.relpath(root, share)
        for name in sorted(dirs):
            rel = name if rel_root == "." else os.path.join(rel_root, name)
            rel = os.path.normpath(rel) + "/"
            paths.append(rel)
            vprint(f"  Captured folder: {rel}")
        for name in sorted(files):
            rel = name if rel_root == "." else os.path.join(rel_root, name)
            rel = os.path.normpath(rel)
            paths.append(rel)
            vprint(f"  Captured file: {rel}")

    paths.sort()

    BACKUP_DIR.mkdir(exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    label = re.sub(r'[^A-Za-z0-9]+', '_', Path(share).name).strip('_')
    backup_path = BACKUP_DIR / f"{timestamp}-{label}-backup.json"
    counter = 1
    while backup_path.exists():
        vprint(f"  Backup filename already exists, trying next: {backup_path}")
        backup_path = BACKUP_DIR / f"{timestamp}-{label}-backup-{counter}.json"
        counter += 1

    backup_path.write_text(json.dumps({"share": share, "timestamp": timestamp, "paths": paths}, indent=2))

    print(f"Captured {len(paths)} folder(s)/file(s).")
    print(f"Backup saved: {backup_path}")


def run_manual_rename(current_name, new_name, log):
    log.set_label("manual")
    src = Path(current_name)
    dst = Path(new_name)

    if not src.exists():
        print(f"Path not found: {current_name}")
        sys.exit(1)
    if dst.exists() and not same_existing_path(dst, src):
        print(f"Target already exists: {new_name}")
        sys.exit(1)

    src.rename(dst)
    log.record(src, dst)
    print(f"Renamed: {current_name} -> {new_name}")

    log_path = log.save(label="manual")
    if log_path:
        print(f"Log saved: {log_path}")


def format_log_timestamp(name):
    m = re.match(r'^(\d{8}-\d{6})', name)
    if not m:
        return None
    try:
        dt = datetime.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
    except ValueError:
        return None
    hour = dt.strftime("%I").lstrip("0") or "12"
    return dt.strftime(f"%Y-%m-%d {hour}:%M %p")


def select_log_file():
    logs = sorted(LOG_DIR.glob("*.json"), key=lambda p: p.name, reverse=True) if LOG_DIR.exists() else []
    if not logs:
        print("No logs found.")
        sys.exit(1)

    print("Restore from log:")
    for i, log_path in enumerate(logs, 1):
        friendly = format_log_timestamp(log_path.name)
        suffix = f" ({friendly})" if friendly else ""
        print(f"  {i}) {log_path.name}{suffix}")
    while True:
        sel = input(f"Select a log to restore [1-{len(logs)}]: ").strip()
        if sel.isdigit() and 1 <= int(sel) <= len(logs):
            return logs[int(sel) - 1]
        print("Invalid selection.")


def find_latest_log():
    logs = sorted(LOG_DIR.glob("*.json"), key=lambda p: p.name, reverse=True) if LOG_DIR.exists() else []
    return logs[0] if logs else None


def run_undo():
    latest = find_latest_log()
    if not latest:
        print("No logs found.")
        sys.exit(1)
    friendly = format_log_timestamp(latest.name)
    suffix = f" ({friendly})" if friendly else ""
    print(f"Undoing last action: {latest.name}{suffix}")
    print()
    run_restore(str(latest))


def run_restore(log_arg):
    if isinstance(log_arg, str):
        path = Path(log_arg)
        if not path.is_absolute() and not path.exists():
            candidate = LOG_DIR / log_arg
            if candidate.exists():
                path = candidate

        if not path.exists():
            print(f"Log file not found: {log_arg}")
            sys.exit(1)
    else:
        path = select_log_file()

    vprint(f"Restoring from log: {path}")
    entries = json.loads(path.read_text())
    vprint(f"Log contains {len(entries)} entry(ies), processing in reverse order")

    restored = 0
    skipped = 0

    for entry in reversed(entries):
        src = Path(entry["to"])
        dst = Path(entry["from"])

        if not src.exists():
            print(f"Skipping (no longer exists): {src}")
            skipped += 1
            continue
        if dst.exists():
            print(f"Skipping (restore target already exists): {dst}")
            skipped += 1
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        print(f"Restored: {src} -> {dst}")
        restored += 1

        parent = src.parent
        try:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
                print(f"Removed empty folder: {parent}")
        except OSError:
            pass

    print()
    print(f"Restored: {restored}, skipped: {skipped}")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Scan SMB media shares, match against TMDb, and rename folders/files into Plex-friendly structure."
    )
    parser.add_argument("-y", "--yes", action="store_true", help="Rename everything without prompting")
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Scan even if no changes were detected since the last run."
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print detailed information about everything the script is doing."
    )
    parser.add_argument(
        "-t", "--test",
        nargs="?", const=0, type=int, default=None, metavar="N",
        help="Preview matches and renames without making changes. Optional N limits how many folders are previewed."
    )
    parser.add_argument(
        "-r", "--rename",
        nargs="?", const=True, default=False, metavar="PATH",
        help="Scan a share, match against TMDb, and rename folders/files into Plex-friendly structure. "
             "Optionally pass a path to skip the share-selection prompt."
    )
    parser.add_argument(
        "-m", "--manual-rename",
        nargs=2, metavar=("CURRENT_NAME", "NEW_NAME"),
        help="Manually rename a specific folder or file, bypassing TMDb lookup."
    )
    parser.add_argument(
        "--restore",
        nargs="?", const=True, default=False, metavar="LOGFILE",
        help="Restore original names from a saved rename log (filename or path). "
             "If no logfile is given, choose from a list of available logs (most recent first)."
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Snapshot the current folder/file names on a share to a log file, without making any changes."
    )
    parser.add_argument(
        "-c", "--cleanup",
        nargs="?", const=True, default=False, metavar="PATH",
        help="Move junk folders/files (per delete.yaml) to a trash folder. "
             "Optionally pass a path to skip the share-selection prompt."
    )
    parser.add_argument(
        "-u", "--undo",
        action="store_true",
        help="Restore from the most recent log file, without prompting. Cannot be combined with any other argument."
    )
    parser.add_argument(
        "-e", "--episodes",
        nargs="?", const=True, default=False, metavar="PATH",
        help="Check TV show folders against TMDb's episode list and report any missing (already-aired) episodes. "
             "Read-only, makes no changes. Optionally pass a path to skip the share-selection prompt."
    )
    parser.add_argument(
        "-T", "--type",
        type=normalize_media_type_arg, default=None, metavar="movies|tv",
        help="Manually specify the library type (movies or tv) instead of auto-detecting from the share name "
             "or prompting."
    )
    return parser


BUNDLABLE_FLAGS = {"y", "f", "t", "r", "v", "c", "e"}
PATH_TAKING_FLAGS = {"r", "c", "e"}


def expand_bundled_flags(argv):
    expanded = []
    for token in argv:
        if (
            len(token) > 2
            and token[0] == "-"
            and token[1] != "-"
            and all(c in BUNDLABLE_FLAGS for c in token[1:])
        ):
            letters = token[1:]
            path_flag = next((c for c in letters if c in PATH_TAKING_FLAGS), None)
            if path_flag:
                letters = letters.replace(path_flag, "") + path_flag
            expanded.extend(f"-{c}" for c in letters)
        else:
            expanded.append(token)
    return expanded


def parse_args():
    return build_parser().parse_args(expand_bundled_flags(sys.argv[1:]))


def main():
    if len(sys.argv) == 1:
        build_parser().print_help()
        return

    args = parse_args()

    if args.undo:
        other_args_used = (
            args.yes or args.force or args.verbose or args.test is not None
            or args.rename or args.manual_rename or args.restore
            or args.backup or args.cleanup or args.episodes or args.type
        )
        if other_args_used:
            print("--undo cannot be combined with any other argument.")
            sys.exit(1)
        run_undo()
        return

    global VERBOSE
    VERBOSE = args.verbose

    log = RenameLog()

    if args.restore:
        run_restore(args.restore)
        return

    if args.backup:
        run_backup(args)
        return

    if args.manual_rename:
        run_manual_rename(args.manual_rename[0], args.manual_rename[1], log)
        return

    if args.episodes:
        run_episode_check(args)
        return

    if args.cleanup and args.rename:
        path_arg = None
        if isinstance(args.rename, str):
            path_arg = args.rename
        elif isinstance(args.cleanup, str):
            path_arg = args.cleanup
        share = resolve_share(path_arg)
        args.rename = share
        args.cleanup = share
        run_scan(args, log)
        print()
        run_cleanup(args, log)
        return

    if args.cleanup:
        run_cleanup(args, log)
        return

    if args.rename:
        run_scan(args, log)
        return

    build_parser().print_help()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print("Interrupted. Exiting.")
        sys.exit(130)
