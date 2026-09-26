# main.py
# InstaReelBulkDownload - v4.0.0
# 2026-09-26
#
# Reads link lists from the "Links/" folder (one or more .txt files that may
# also contain notes/annotations), extracts the Instagram links, and downloads
# them in bulk - reels, photos and whole carousels. The text written above a
# link is kept as its note and shown next to the saved file at the end. A JSON
# history and the logs are kept in the "Core/" folder.
#
# Instagram no longer answers anonymous requests (it replies 403 Forbidden), so
# the script signs in on the first run and reuses the saved session afterwards.
#
# Links are only removed from a .txt file once they are actually downloaded;
# anything still pending stays in the file, so a failed run never loses links.

import instaloader
from instaloader import Post
from instaloader.exceptions import (
    BadCredentialsException,
    ConnectionException,
    LoginException,
    LoginRequiredException,
    QueryReturnedBadRequestException,
    QueryReturnedForbiddenException,
    TooManyRequestsException,
    TwoFactorAuthRequiredException,
)
import requests
import time
import random
import os
import re
import json
import glob
import getpass
import logging
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from datetime import datetime
from urllib.parse import urlparse

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# Paths / configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LINKS_DIR = os.path.join(BASE_DIR, "Links")         # user drops .txt files here
DOWNLOAD_DIR = os.path.join(BASE_DIR, "Downloads")  # videos are saved here
CORE_DIR = os.path.join(BASE_DIR, "Core")           # logs + history + session

LOG_FILE = os.path.join(CORE_DIR, "app.log")
QUEUE_FILE = os.path.join(CORE_DIR, "queue.log")
HISTORY_FILE = os.path.join(CORE_DIR, "history.json")
FAILED_FILE = os.path.join(CORE_DIR, "failed_downloads.txt")
SESSION_INFO_FILE = os.path.join(CORE_DIR, "session.json")  # remembers the username

# Deprecated files from the old version (root folder) - migrated then removed.
LEGACY_SOURCE_FILE = os.path.join(BASE_DIR, "links_to_download.txt")
LEGACY_QUEUE_FILE = os.path.join(BASE_DIR, "queue.log")
LEGACY_FAILED_FILE = os.path.join(BASE_DIR, "failed_downloads.txt")

# Give up after this many refusals in a row: once Instagram starts blocking,
# hammering it with the rest of the queue only makes things worse.
BLOCK_ABORT_THRESHOLD = 3

# Matches Instagram reel/post URLs anywhere inside a line of text, so notes
# and annotations around the link are ignored. The query string (?igsh=...)
# is intentionally not captured.
INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reels?|p|tv)/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
# The whole link, query string included, so it can be cut out of a line to
# leave only the note written around it.
INSTAGRAM_URL_FULL_RE = re.compile(INSTAGRAM_URL_RE.pattern + r"\S*", re.IGNORECASE)
# Bullets and separators trimmed off the edges of a note ("- ", "Note:", ...).
NOTE_TRIM = " \t-–—:•*>#|"

# Files that count as "already downloaded". A carousel is saved as
# <shortcode>_01.jpg, <shortcode>_02.mp4, ...
MEDIA_EXTENSIONS = (".mp4", ".jpg", ".jpeg", ".png", ".webp", ".heic")
CAROUSEL_SUFFIX_RE = re.compile(r"_\d{2,}$")

for _directory in (LINKS_DIR, DOWNLOAD_DIR, CORE_DIR):
    os.makedirs(_directory, exist_ok=True)

# ---------------------------------------------------------------------------
# Console + logging (Core/app.log)
# ---------------------------------------------------------------------------
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass

console = Console(highlight=False)
ACCENT = "#E1306C"   # Instagram pink
ACCENT_2 = "#F77737"  # Instagram orange

# Pass as ``extra=QUIET`` to write a record to the log file only.
QUIET = {"console": False}


class ConsoleHandler(logging.Handler):
    """Show log records on the rich console, coloured by level."""

    STYLES = {logging.WARNING: "yellow", logging.ERROR: "bold red"}

    def emit(self, record):
        if not getattr(record, "console", True):
            return
        try:
            console.print(self.format(record), style=self.STYLES.get(record.levelno), markup=False)
        except Exception:  # noqa: BLE001 - logging must never crash the run
            self.handleError(record)


logger = logging.getLogger("insta_bulk")
logger.setLevel(logging.INFO)

_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
)
logger.addHandler(_file_handler)

_console_handler = ConsoleHandler()
_console_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_console_handler)


class InstagramBlocked(Exception):
    """Instagram refused the request (403, rate limit, or login required)."""


class DownloadFailed(Exception):
    """The post was found, but its media could not be saved."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def shortcode_from_url(url):
    """Return the shortcode of an Instagram URL, ignoring any query string."""
    return url.split("?")[0].rstrip("/").split("/")[-1]


def migrate_legacy():
    """One-time migration from the old layout.

    Moves the old ``links_to_download.txt`` into ``Links/`` (so its links are
    not lost) and removes the deprecated files from the project root.
    """
    if os.path.exists(LEGACY_SOURCE_FILE):
        try:
            with open(LEGACY_SOURCE_FILE, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
            if content.strip():
                target = os.path.join(LINKS_DIR, "links_to_download.txt")
                if not os.path.exists(target):
                    with open(target, "w", encoding="utf-8") as out:
                        out.write(content)
                    logger.info("Imported old links_to_download.txt into the Links folder.")
            os.remove(LEGACY_SOURCE_FILE)
            logger.info("Removed deprecated links_to_download.txt from the project root.")
        except OSError as exc:
            logger.warning("Could not migrate links_to_download.txt: %s", exc)

    for legacy in (LEGACY_QUEUE_FILE, LEGACY_FAILED_FILE):
        if os.path.exists(legacy) and os.path.abspath(legacy) != os.path.abspath(QUEUE_FILE):
            try:
                os.remove(legacy)
                logger.info("Removed deprecated %s from the project root.", os.path.basename(legacy))
            except OSError:
                pass


def read_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.readlines()
    except OSError as exc:
        logger.warning("Could not read %s: %s", os.path.basename(path), exc)
        return None


def clean_note(text):
    return " ".join(text.split()).strip(NOTE_TRIM)


def link_blocks(lines):
    """Split a links file into blocks: the note lines written above a link, and
    the line holding it.

    Yields ``(note_indexes, link_index, urls, note)``. Text on the link line
    itself is part of the note too. Blank lines are ignored, so a note may sit
    a few lines above its link; lines after the last link belong to no link.
    """
    note_indexes = []
    for index, line in enumerate(lines):
        urls = INSTAGRAM_URL_RE.findall(line)
        if not urls:
            if line.strip():
                note_indexes.append(index)
            continue
        parts = [clean_note(lines[i]) for i in note_indexes]
        parts.append(clean_note(INSTAGRAM_URL_FULL_RE.sub(" ", line)))
        yield note_indexes, index, urls, "\n".join(p for p in parts if p)
        note_indexes = []


def extract_links(path):
    """Return an ordered ``{shortcode: {"url", "note"}}`` dict for one .txt
    file. The note is the text written above the link (may be empty)."""
    lines = read_lines(path)
    if lines is None:
        return {}

    links = {}
    for _, _, urls, note in link_blocks(lines):
        for url in urls:
            code = shortcode_from_url(url)
            if code not in links:
                links[code] = {"url": url.split("?")[0], "note": note}
            elif note and not links[code]["note"]:
                links[code]["note"] = note
    logger.info("Found %d link(s) in %s", len(links), os.path.basename(path), extra=QUIET)
    return links


def prune_file(path, resolved):
    """Drop the already-downloaded links - and the notes above them - from a
    .txt file.

    Links that are still pending keep their notes, so a failed run never loses
    anything. The file is only deleted once no link is left in it. Returns the
    number of links still pending in the file, or None if it could not be read.
    """
    name = os.path.basename(path)
    lines = read_lines(path)
    if lines is None:
        return None

    drop = set()
    for note_indexes, index, urls, _ in link_blocks(lines):
        if all(shortcode_from_url(u) in resolved for u in urls):
            drop.add(index)
            drop.update(note_indexes)
    kept = [line for index, line in enumerate(lines) if index not in drop]

    pending = sum(len(INSTAGRAM_URL_RE.findall(line)) for line in kept)
    if not pending:
        try:
            os.remove(path)
            logger.info("Finished and removed: %s", name, extra=QUIET)
        except OSError as exc:
            logger.warning("Could not remove %s: %s", name, exc)
        return 0

    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(kept)
        logger.info("Kept %s - %d link(s) still pending.", name, pending, extra=QUIET)
    except OSError as exc:
        logger.warning("Could not update %s: %s", name, exc)
    return pending


def media_on_disk(folder):
    """Return ``{shortcode: [file names]}`` for every video/photo already under
    ``folder``, including sub-folders the user may have organised manually."""
    found = {}
    for _root, _dirs, files in os.walk(folder):
        for name in files:
            stem, extension = os.path.splitext(name)
            if extension.lower() not in MEDIA_EXTENSIONS:
                continue
            found.setdefault(stem, []).append(name)
            base = CAROUSEL_SUFFIX_RE.sub("", stem)  # <shortcode>_01.jpg
            if base != stem:
                found.setdefault(base, []).append(name)
    return found


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read history (%s); starting a fresh history.", exc)
    return []


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as fh:
        json.dump(history, fh, indent=2, ensure_ascii=False)


def write_failed_report(history, resolved):
    """Rebuild failed_downloads.txt from the whole history.

    Every link that failed at least once and is still not on disk is listed,
    so failures from earlier runs are never overwritten and lost.
    """
    pending = {}
    for record in history:
        code = record.get("shortcode")
        if not code or code in resolved:
            continue
        pending[code] = record.get("link") or f"https://www.instagram.com/reel/{code}"

    if not pending:
        if os.path.exists(FAILED_FILE):
            os.remove(FAILED_FILE)
        return []

    with open(FAILED_FILE, "w", encoding="utf-8") as fh:
        fh.writelines(url + "\n" for url in pending.values())
    return list(pending.values())


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------
def session_file(username):
    return os.path.join(CORE_DIR, "session-" + username)


def remembered_username():
    try:
        with open(SESSION_INFO_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh).get("username")
    except (OSError, json.JSONDecodeError):
        return None


def remember_username(username):
    try:
        with open(SESSION_INFO_FILE, "w", encoding="utf-8") as fh:
            json.dump({"username": username}, fh)
    except OSError:
        pass


def use_saved_session(loader):
    """Reuse the session saved by a previous run. Returns the username or None."""
    username = remembered_username()
    if not username or not os.path.exists(session_file(username)):
        return None
    try:
        loader.load_session_from_file(username, session_file(username))
        who = loader.test_login()
    except Exception as exc:  # noqa: BLE001 - any failure just means "sign in again"
        logger.info("Saved session for %s could not be used (%s).", username, exc)
        return None
    if not who:
        logger.info("The saved session for %s has expired.", username)
    return who


# Chromium-based browsers, read through browser_cookie3. Firefox is handled
# separately (see firefox_profiles) so that every profile is covered.
BROWSERS = (
    ("chrome", "Chrome"),
    ("edge", "Edge"),
    ("brave", "Brave"),
    ("vivaldi", "Vivaldi"),
    ("opera", "Opera"),
    ("chromium", "Chromium"),
)


def cookie_value(cookiejar, name):
    for cookie in cookiejar:
        if cookie.name == name and cookie.value:
            return cookie.value
    return None


def firefox_profiles():
    """Every Firefox cookie database on this machine.

    browser_cookie3 only reads the profile flagged as default in profiles.ini,
    which misses Developer Edition, ESR and any extra profile - so they are
    collected here by hand.
    """
    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA", "")
    roots = [
        os.path.join(appdata, "Mozilla", "Firefox", "Profiles"),
        os.path.join(appdata, "librewolf", "Profiles"),
        os.path.join(home, ".mozilla", "firefox"),
        os.path.join(home, ".librewolf"),
        os.path.join(home, "snap", "firefox", "common", ".mozilla", "firefox"),
        os.path.join(home, "Library", "Application Support", "Firefox", "Profiles"),
    ]
    databases = []
    # A session left behind by the login window (option 1) is the most likely
    # one to be wanted, so it comes first.
    from_login_window = os.path.join(LOGIN_PROFILE_DIR, "cookies.sqlite")
    if os.path.exists(from_login_window):
        databases.append(from_login_window)
    for root in roots:
        if root and os.path.isdir(root):
            databases.extend(sorted(glob.glob(os.path.join(root, "*", "cookies.sqlite"))))
    return databases


def firefox_cookies(database):
    """Read the Instagram cookies out of one Firefox profile.

    Returns ``(cookiejar, error)``. The write-ahead log is copied and checked in
    as well, otherwise a session created moments ago - while Firefox is still
    running - would not be visible yet.
    """
    workspace = tempfile.mkdtemp(prefix="instareel-cookies-")
    snapshot = os.path.join(workspace, "cookies.sqlite")
    try:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(database + suffix):
                shutil.copy2(database + suffix, snapshot + suffix)
        connection = sqlite3.connect(snapshot)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            rows = connection.execute(
                "SELECT name, value, host, path, isSecure, expiry FROM moz_cookies "
                "WHERE host LIKE '%instagram.com'"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        return None, str(exc)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    jar = requests.cookies.RequestsCookieJar()
    for name, value, host, path, secure, expiry in rows:
        jar.set(name, value, domain=host, path=path or "/",
                secure=bool(secure), expires=expiry or None)
    return jar, None


def login_with_browser_cookies(loader):
    """Borrow the Instagram session from a browser you are already logged into."""
    unreadable = []
    signed_out = []

    def use(label, cookiejar):
        """Adopt a cookie jar if it carries a signed-in session."""
        # "sessionid" is the cookie Instagram sets once you are logged in. Checking
        # it directly is more reliable than test_login(), which also fails when
        # Instagram is merely rate limiting us.
        if not cookie_value(cookiejar, "sessionid"):
            signed_out.append(label)
            return None

        loader.context._session.cookies.update(cookiejar)
        try:
            who = loader.test_login()
        except Exception:  # noqa: BLE001 - treated the same as "could not confirm"
            who = None

        if who:
            loader.context.username = who
            print(f"  {label}: signed in as {who}.")
            return who

        # The browser really is logged in, Instagram just would not confirm it
        # (usually rate limiting). Use the session anyway.
        account = cookie_value(cookiejar, "ds_user_id") or "browser"
        loader.context.username = account
        print(f"  {label}: session found, but Instagram would not confirm it right now")
        print("            (it is rate limiting this connection). Using it anyway.")
        return account

    # Firefox first, read directly so that every profile is covered.
    for database in firefox_profiles():
        folder = os.path.dirname(database)
        if os.path.abspath(folder) == os.path.abspath(LOGIN_PROFILE_DIR):
            label = "Login window from an earlier run"
        else:
            label = "Firefox [%s]" % os.path.basename(folder)
        cookiejar, error = firefox_cookies(database)
        if error:
            unreadable.append((label, error))
            continue
        who = use(label, cookiejar)
        if who:
            return who

    # Then the Chromium-based browsers, which need browser_cookie3.
    try:
        import browser_cookie3
    except ImportError:
        browser_cookie3 = None
        unreadable.append(("Chrome/Edge/Brave", "browser_cookie3 is not installed"))

    if browser_cookie3 is not None:
        for attribute, label in BROWSERS:
            reader = getattr(browser_cookie3, attribute, None)
            if reader is None:
                continue
            try:
                cookiejar = reader(domain_name="instagram.com")
            except Exception as exc:  # noqa: BLE001 - browser missing or cookies locked
                unreadable.append((label, str(exc)))
                continue
            who = use(label, cookiejar)
            if who:
                return who

    for label, reason in unreadable:
        print(f"  {label}: cookies could not be read ({reason})")
    if signed_out:
        print(f"  No Instagram session found in: {', '.join(signed_out)}")
        print("  (those profiles only carry logged-out cookies)")
    print()
    print("  Log in to instagram.com in a normal Firefox window - not a private one,")
    print("  since private windows never write the session to disk - then try again.")
    print("  Or just use option 1, which opens a login window for you.")
    if any(label == "Chrome" for label, _ in unreadable):
        print("  (Chrome encrypts its cookies on Windows and usually cannot be read.)")
    return None


FIREFOX_PATHS = (
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    r"C:\Program Files\Firefox Developer Edition\firefox.exe",
    r"C:\Program Files\Firefox Nightly\firefox.exe",
    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    "/Applications/Firefox.app/Contents/MacOS/firefox",
    "/Applications/Firefox Developer Edition.app/Contents/MacOS/firefox",
)

LOGIN_PROFILE_DIR = os.path.join(CORE_DIR, "login-profile")
LOGIN_URL = "https://www.instagram.com/accounts/login/"
LOGIN_TIMEOUT = 600  # seconds to wait for the sign-in to happen


def firefox_executable():
    """Locate Firefox (any edition) on this machine."""
    for path in FIREFOX_PATHS:
        if os.path.exists(path):
            return path

    found = shutil.which("firefox")
    if found:
        return found

    if os.name != "nt":
        return None

    # Registry fallback, for installs in a custom folder.
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Clients\StartMenuInternet") as root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                if "firefox" not in name.lower():
                    continue
                try:
                    with winreg.OpenKey(root, name + r"\shell\open\command") as key:
                        command = winreg.QueryValueEx(key, "")[0]
                except OSError:
                    continue
                path = command.strip().strip('"')
                if os.path.exists(path):
                    return path
    except OSError:
        pass
    return None


def profile_in_use(directory):
    """True while Firefox still holds this profile open.

    More reliable than watching the process we launched: on Windows that one is
    only a launcher, which spawns the real browser and exits immediately.
    """
    if os.name == "nt":
        lock = os.path.join(directory, "parent.lock")
        if not os.path.exists(lock):
            return False
        try:
            with open(lock, "a"):
                return False  # we could lock it, so Firefox has let go
        except OSError:
            return True
    return os.path.lexists(os.path.join(directory, "lock"))


def wait_for_profile_release(directory, seconds):
    """Wait for Firefox to let go of a profile.

    The window usually outlives the launcher process we started, so the profile
    can stay locked for a while after we are done with it.
    """
    for _ in range(seconds):
        if not profile_in_use(directory):
            return True
        time.sleep(1)
    return False


def remove_profile(directory, attempts=8):
    """Delete the login profile, retrying while Firefox releases its files."""
    for _ in range(attempts):
        if not os.path.exists(directory):
            return True
        try:
            shutil.rmtree(directory)
            return True
        except OSError:
            time.sleep(1)  # Firefox is still shutting down
    shutil.rmtree(directory, ignore_errors=True)
    return not os.path.exists(directory)


def prepare_login_profile(directory):
    """Create the throwaway Firefox profile used by the login window."""
    os.makedirs(directory, exist_ok=True)
    preferences = (
        'user_pref("browser.aboutwelcome.enabled", false);',
        'user_pref("browser.shell.checkDefaultBrowser", false);',
        'user_pref("browser.startup.homepage_override.mstone", "ignore");',
        'user_pref("datareporting.policy.dataSubmissionEnabled", false);',
        'user_pref("trailhead.firstrun.didSeeAboutWelcome", true);',
        # On Windows the process we launch is only a launcher: it spawns the real
        # browser and exits at once, which would look like "window closed".
        'user_pref("browser.launcherProcess.enabled", false);',
    )
    with open(os.path.join(directory, "user.js"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(preferences) + "\n")


def login_with_window(loader):
    """Open a Firefox window on the Instagram login page and pick up the session.

    The window uses a profile of its own inside Core/, so signing in here does
    not touch (or depend on) your everyday browser, and any account can be used.
    """
    executable = firefox_executable()
    if not executable:
        print("  Firefox was not found, and the login window needs it.")
        print("  Install it from https://www.mozilla.org/firefox/ or use option 3.")
        return None

    remove_profile(LOGIN_PROFILE_DIR)  # always start clean
    prepare_login_profile(LOGIN_PROFILE_DIR)
    database = os.path.join(LOGIN_PROFILE_DIR, "cookies.sqlite")

    print()
    print("  Opening a Firefox window on the Instagram login page.")
    print("  Sign in with whichever account you want - that window is separate")
    print("  from your normal browser, so nothing there is used or changed.")
    print("  Waiting for the sign-in (up to 10 minutes)...")

    try:
        process = subprocess.Popen(
            [executable, "--no-remote", "--profile", LOGIN_PROFILE_DIR, LOGIN_URL]
        )
    except OSError as exc:
        print(f"  Could not open Firefox: {exc}")
        return None

    def captured_session():
        if not os.path.exists(database):
            return None
        cookiejar, _ = firefox_cookies(database)
        if cookiejar is not None and cookie_value(cookiejar, "sessionid"):
            return cookiejar
        return None

    session_cookies = None
    deadline = time.time() + LOGIN_TIMEOUT
    startup_deadline = time.time() + 30
    opened = False

    while time.time() < deadline:
        session_cookies = captured_session()
        if session_cookies:
            break

        running = profile_in_use(LOGIN_PROFILE_DIR)
        opened = opened or running
        if opened and not running:
            # Firefox really has closed - cookies are flushed on exit.
            session_cookies = captured_session()
            break
        if not opened and process.poll() is not None and time.time() > startup_deadline:
            print("  Firefox did not start.")
            break
        time.sleep(2)

    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()

    if not session_cookies:
        wait_for_profile_release(LOGIN_PROFILE_DIR, 15)
        remove_profile(LOGIN_PROFILE_DIR)
        print("  No sign-in was detected in that window.")
        return None

    print("  Signed in. You can close the login window now.")
    wait_for_profile_release(LOGIN_PROFILE_DIR, 60)

    loader.context._session.cookies.update(session_cookies)
    try:
        who = loader.test_login()
    except Exception:  # noqa: BLE001 - treated the same as "could not confirm"
        who = None

    if not who:
        who = cookie_value(session_cookies, "ds_user_id") or "browser"
        print("  Signed in, but Instagram would not confirm it right now")
        print("  (it is rate limiting this connection). Using the session anyway.")
    else:
        print(f"  Signed in as {who}.")

    loader.context.username = who
    # The session now lives in Core/session-<user>; the profile would only be a
    # second copy of the same credentials sitting on disk.
    if not remove_profile(LOGIN_PROFILE_DIR):
        print(f"  Note: {LOGIN_PROFILE_DIR} could not be deleted; it will be")
        print("  cleared on the next sign-in.")
    return who


def login_with_password(loader):
    """Classic username + password login (with two-factor support)."""
    try:
        username = input("  Instagram username: ").strip()
        if not username:
            return None
        password = getpass.getpass("  Password (hidden while you type): ")
    except EOFError:
        return None

    try:
        loader.login(username, password)
    except TwoFactorAuthRequiredException:
        try:
            code = input("  Two-factor code from your app/SMS: ").strip()
            loader.two_factor_login(code)
        except (EOFError, LoginException, ConnectionException) as exc:
            print(f"  Two-factor sign-in failed: {exc}")
            return None
    except BadCredentialsException:
        print("  Wrong username or password.")
        return None
    except (LoginException, ConnectionException) as exc:
        print(f"  Sign-in failed: {exc}")
        return None

    return loader.test_login()


def sign_in(loader):
    """Return the signed-in username, or None if running anonymously."""
    who = use_saved_session(loader)
    if who:
        logger.info("Signed in as %s (saved session).", who)
        if os.path.exists(LOGIN_PROFILE_DIR) and not profile_in_use(LOGIN_PROFILE_DIR):
            remove_profile(LOGIN_PROFILE_DIR)  # leftover from an earlier login window
        return who

    options = Table.grid(padding=(0, 2))
    options.add_column(style=f"bold {ACCENT}")
    options.add_column()
    options.add_row("1", "Open a login window and sign in there (any account)  [dim](recommended)[/]")
    options.add_row("2", "Use the session from a browser where you are already logged in")
    options.add_row("3", "Type an Instagram username and password")
    options.add_row("4", "Continue without signing in [dim](downloads will almost certainly fail)[/]")
    console.print()
    console.print(Panel(
        Group(
            Text("Instagram no longer allows anonymous downloads (it answers 403)."),
            Text("You only need to do this once - the session is then saved in the Core "
                 "folder and reused automatically on the next runs.", style="dim"),
            Text(),
            options,
        ),
        title="[bold]Sign in[/]",
        title_align="left",
        border_style=ACCENT_2,
        box=box.ROUNDED,
        padding=(1, 2),
    ))

    while True:
        try:
            choice = console.input(f" [bold {ACCENT}]Option[/] [dim][1/2/3/4][/]: ").strip()
        except EOFError:
            return None

        if choice == "1":
            who = login_with_window(loader)
        elif choice == "2":
            who = login_with_browser_cookies(loader)
        elif choice == "3":
            who = login_with_password(loader)
        elif choice == "4":
            logger.info("Continuing without signing in.")
            return None
        else:
            print("  Please type 1, 2, 3 or 4.")
            continue

        if who:
            try:
                loader.save_session_to_file(session_file(who))
                remember_username(who)
                logger.info("Signed in as %s. Session saved for the next runs.", who)
            except OSError as exc:
                logger.warning("Signed in as %s, but the session could not be saved: %s", who, exc)
            if os.path.exists(LOGIN_PROFILE_DIR) and not profile_in_use(LOGIN_PROFILE_DIR):
                remove_profile(LOGIN_PROFILE_DIR)
            return who

        print("  Could not sign in. Pick another option (or 4 to carry on anyway).")


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------
def fetch_post(loader, shortcode):
    """Fetch the post metadata, turning Instagram refusals into InstagramBlocked."""
    try:
        return Post.from_shortcode(loader.context, shortcode)
    except TypeError as exc:
        # instaloader hands back None when Instagram refuses the GraphQL query,
        # which then surfaces as "'NoneType' object is not subscriptable".
        raise InstagramBlocked("Instagram returned no data for this post") from exc
    except (
        QueryReturnedForbiddenException,
        QueryReturnedBadRequestException,
        TooManyRequestsException,
        LoginRequiredException,
    ) as exc:
        raise InstagramBlocked(str(exc) or type(exc).__name__) from exc
    except ConnectionException as exc:
        text = str(exc)
        if any(marker in text for marker in ("401", "403", "429", "Please wait", "login_required")):
            raise InstagramBlocked(text) from exc
        raise


def media_of(post):
    """Every piece of media in a post as ``(url, is_video)``: one for a reel or
    a photo, one per slide for a carousel."""
    if post.typename == "GraphSidecar":
        nodes = [
            (node.video_url if node.is_video else node.display_url, node.is_video)
            for node in post.get_sidecar_nodes()
        ]
        if nodes:
            return nodes
    if post.is_video:
        return [(post.video_url, True)]
    return [(post.url, False)]


def media_extension(url, is_video):
    if is_video:
        return ".mp4"
    extension = os.path.splitext(urlparse(url).path)[1].lower()
    return extension if extension in MEDIA_EXTENSIONS and extension != ".mp4" else ".jpg"


def fetch_file(url, target, progress, task):
    """Stream one file to ``target``, advancing the byte progress bar."""
    response = requests.get(url, stream=True, timeout=60)
    if response.status_code != 200:
        raise DownloadFailed(f"HTTP {response.status_code} from the media server")
    size = int(response.headers.get("Content-Length") or 0)
    progress.reset(task, total=size or None)
    with open(target, "wb") as out_file:
        for chunk in response.iter_content(chunk_size=65536):
            if chunk:
                out_file.write(chunk)
                progress.advance(task, len(chunk))


def download_post(loader, shortcode, progress, task):
    """Download a reel, photo or carousel into Downloads/.

    Returns ``(kind, file_names)``. Every file is written as ``.part`` first and
    only renamed once the whole post is in, so a half-finished carousel never
    looks downloaded on the next run.
    """
    post = fetch_post(loader, shortcode)
    media = media_of(post)
    if not media or not all(url for url, _ in media):
        raise DownloadFailed("no downloadable media in this post")

    if len(media) > 1:
        kind = "carousel"
    else:
        kind = "video" if media[0][1] else "photo"

    names = []
    for number, (url, is_video) in enumerate(media, 1):
        suffix = f"_{number:02d}" if len(media) > 1 else ""
        names.append(shortcode + suffix + media_extension(url, is_video))

    parts = []
    try:
        for number, ((url, _), name) in enumerate(zip(media, names), 1):
            label = shortcode if len(media) == 1 else f"{shortcode}  {number}/{len(media)}"
            progress.update(task, description=label, visible=True)
            part = os.path.join(DOWNLOAD_DIR, name + ".part")
            parts.append(part)
            fetch_file(url, part, progress, task)
        for part, name in zip(parts, names):
            os.replace(part, os.path.join(DOWNLOAD_DIR, name))
    finally:
        for part in parts:
            try:
                if os.path.exists(part):
                    os.remove(part)
            except OSError:
                pass
        progress.update(task, visible=False)

    logger.info("Downloaded %s (%s): %s", shortcode, kind, ", ".join(names), extra=QUIET)
    return kind, names


# ---------------------------------------------------------------------------
# Console layout
# ---------------------------------------------------------------------------
KIND_STYLES = {"video": "cyan", "photo": "green", "carousel": "magenta"}


def show_banner():
    console.print()
    console.print(Panel(
        Align.center(Group(
            Text("InstaReel Bulk Downloader", style=f"bold {ACCENT}", justify="center"),
            Text("reels · photos · carousels", style=ACCENT_2, justify="center"),
        )),
        box=box.HEAVY,
        border_style=ACCENT,
        padding=(1, 4),
    ))


def show_scan(file_rows, total_links, total_new):
    table = Table(box=box.SIMPLE_HEAVY, header_style=f"bold {ACCENT_2}", expand=False,
                  title="[bold]Links folder[/]", title_justify="left")
    table.add_column("File", style="bold")
    table.add_column("Links", justify="right")
    table.add_column("With note", justify="right", style="dim")
    table.add_column("New", justify="right", style="bold green")
    for name, found, with_note, new in file_rows:
        table.add_row(name, str(found), str(with_note), str(new) if new else "[dim]0[/]")
    if len(file_rows) > 1:
        table.add_section()
        table.add_row("Total", str(total_links), "", str(total_new))
    console.print(table)


def pause(progress, task, seconds, reason):
    """Sleep with a countdown in the overall progress bar."""
    end = time.time() + seconds
    while True:
        left = end - time.time()
        if left <= 0:
            break
        progress.update(task, description=f"{reason} [dim]{left:3.0f}s[/]")
        time.sleep(min(1.0, left))
    progress.update(task, description="Downloading")


def result_line(result):
    line = Text("  ")
    if result["success"]:
        line.append("✔ ", style="bold green")
        line.append(result["code"], style="bold")
        line.append(f"  {result['kind']}", style=KIND_STYLES.get(result["kind"], ""))
        if len(result["files"]) > 1:
            line.append(f" · {len(result['files'])} files", style="dim")
    else:
        line.append("✘ ", style="bold red")
        line.append(result["code"], style="bold")
        line.append(f"  {short(result['error'], 70)}", style="red")
    if result["note"]:
        line.append("  — " + short(result["note"].replace("\n", " / "), 50), style=f"italic {ACCENT_2}")
    return line


def short(text, width):
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def files_cell(names):
    if len(names) <= 3:
        return "\n".join(names)
    return "\n".join(names[:2] + [f"… +{len(names) - 2} more"])


def show_summary(results, skipped, outstanding, file_status):
    ok = [r for r in results if r["success"]]
    bad = [r for r in results if not r["success"] and not r.get("skipped")]
    by_kind = {kind: sum(1 for r in ok if r["kind"] == kind) for kind in KIND_STYLES}

    stats = Table.grid(padding=(0, 3))
    for _ in range(4):
        stats.add_column(justify="center")
    stats.add_row(
        Text(str(len(ok)), style="bold green"),
        Text(str(len(bad)), style="bold red" if bad else "dim"),
        Text(str(skipped), style="bold"),
        Text(str(len(outstanding)), style="bold yellow" if outstanding else "dim"),
    )
    stats.add_row(
        Text("downloaded", style="green"),
        Text("failed", style="red" if bad else "dim"),
        Text("already had", style="dim"),
        Text("still pending", style="yellow" if outstanding else "dim"),
    )
    breakdown = Text(justify="center")
    for kind, count in by_kind.items():
        if not count:
            continue
        if breakdown:
            breakdown.append("  ·  ", style="dim")
        breakdown.append(f"{count} {kind}{'s' if count != 1 else ''}", style=KIND_STYLES[kind])

    border = "green" if not bad else ("red" if not ok else "yellow")
    console.print()
    console.print(Panel(
        Group(Align.center(stats), Text(), breakdown),
        title="[bold]Summary[/]",
        border_style=border,
        box=box.ROUNDED,
        padding=(1, 2),
    ))

    noted = [r for r in results if r["note"]]
    if noted:
        table = Table(box=box.ROUNDED, header_style=f"bold {ACCENT_2}", border_style="dim",
                      title="[bold]Notes[/]", title_justify="left", show_lines=True, expand=True)
        table.add_column("#", justify="right", style="dim", width=3)
        table.add_column("Note", ratio=3)
        table.add_column("Saved as", ratio=2, style="bold")
        table.add_column("", justify="center", width=12)
        for number, result in enumerate(noted, 1):
            if result["success"]:
                status = Text(result["kind"], style=KIND_STYLES.get(result["kind"], "green"))
                saved = files_cell(result["files"])
            elif result.get("skipped"):
                status = Text("already had", style="dim")
                saved = files_cell(result["files"]) or "[dim]on disk[/]"
            else:
                status = Text("failed", style="bold red")
                saved = Text(f"{result['code']} (not downloaded)", style="dim")
            table.add_row(str(number), Text(result["note"], style="italic"), saved, status)
        console.print(table)

    if bad:
        table = Table(box=box.SIMPLE, header_style="bold red", title="[bold red]Failed[/]",
                      title_justify="left", expand=True)
        table.add_column("Link", style="bold", no_wrap=True)
        table.add_column("Reason", style="red", ratio=1)
        for result in bad:
            table.add_row(result["url"], short(result["error"], 120))
        console.print(table)

    if file_status:
        line = Text("  ")
        for name, pending in file_status:
            if len(line) > 2:
                line.append("   ")
            if pending:
                line.append(f"{name}", style="bold")
                line.append(f" kept, {pending} pending", style="yellow")
            else:
                line.append(f"{name}", style="bold")
                line.append(" done, removed", style="green")
        console.print(line)

    if outstanding:
        console.print(f"  [dim]Pending links stay in the Links folder and are listed in[/] {FAILED_FILE}",
                      markup=True)


def show_block_help(signed_in, aborted, streak, blocked_total):
    if aborted:
        headline = f"Run stopped early: Instagram refused {streak} requests in a row."
    else:
        headline = f"Instagram refused every request in this run ({blocked_total})."
    if signed_in:
        body = (
            f"You are signed in as [bold]{signed_in}[/], so it is one of these two:\n"
            "  • Instagram is rate limiting you. Wait a few hours and run it again;\n"
            "    your links were kept.\n"
            "  • instaloader is out of date. Instagram rotates the query ids it relies\n"
            "    on, and an old version cannot fetch any post. Fix with:\n"
            "      [bold]python -m pip install --upgrade instaloader[/]"
        )
    else:
        body = (
            "You are [bold]NOT[/] signed in, and Instagram no longer serves anonymous\n"
            "requests. Run the script again and pick option [bold]1[/] to sign in."
        )
    console.print(Panel(body, title=f"[bold red]{headline}[/]", title_align="left",
                        border_style="red", box=box.ROUNDED, padding=(1, 2)))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    logger.info("=" * 60, extra=QUIET)
    logger.info("InstaReelBulkDownload started.", extra=QUIET)
    show_banner()

    migrate_legacy()

    txt_files = sorted(glob.glob(os.path.join(LINKS_DIR, "*.txt")))
    if not txt_files:
        console.print(Panel(
            f"No .txt files found in the Links folder.\n"
            f"Add one or more .txt files with your Instagram links into:\n[bold]{LINKS_DIR}[/]",
            border_style="yellow", box=box.ROUNDED, padding=(1, 2),
        ))
        return

    history = load_history()
    on_disk = media_on_disk(DOWNLOAD_DIR)
    # Everything that no longer needs downloading: already on disk, or recorded
    # as a successful download in the history.
    resolved = set(on_disk)
    resolved.update(rec.get("shortcode") for rec in history if rec.get("success"))

    # Read every file first (so we can report totals and build queue.log).
    files_links = [(path, extract_links(path)) for path in txt_files]

    # De-duplicated list of links that still need downloading.
    pending = []
    queued = set()
    file_rows = []
    for path, links in files_links:
        new = 0
        for code, item in links.items():
            if code in resolved or code in queued:
                continue
            queued.add(code)
            pending.append(item["url"])
            new += 1
        with_note = sum(1 for item in links.values() if item["note"])
        file_rows.append((os.path.basename(path), len(links), with_note, new))
    with open(QUEUE_FILE, "w", encoding="utf-8") as fh:
        fh.writelines(url + "\n" for url in pending)

    total = len(pending)
    total_links = sum(len(links) for _, links in files_links)
    logger.info(
        "Collected %d link(s) across %d file(s); %d new to download.",
        total_links, len(txt_files), total, extra=QUIET,
    )
    show_scan(file_rows, total_links, total)
    if not total:
        console.print("  [green]Nothing new to download.[/]")

    loader = instaloader.Instaloader()
    signed_in = sign_in(loader) if total else None
    if total:
        console.print()

    results = []
    skipped = 0
    seen = set()
    file_status = []
    blocked_streak = 0
    blocked_total = 0
    aborted = False

    overall = Progress(
        SpinnerColumn(style=ACCENT),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=None, style="grey23", complete_style=ACCENT, finished_style="green"),
        MofNCompleteColumn(),
        TextColumn("[dim]•[/]"),
        TimeElapsedColumn(),
        console=console,
    )
    current = Progress(
        TextColumn("   [dim]↳[/] {task.description}"),
        BarColumn(bar_width=None, style="grey23", complete_style=ACCENT_2),
        DownloadColumn(),
        TransferSpeedColumn(),
        console=console,
    )
    overall_task = overall.add_task("Downloading", total=total)
    file_task = current.add_task("", total=None, visible=False)

    live = Live(Group(overall, current), console=console, refresh_per_second=10) if total else nullcontext()
    with live:
        for path, links in files_links:
            if aborted:
                break

            for code, item in links.items():
                url, note = item["url"], item["note"]
                if code in resolved:
                    skipped += 1
                    # Show the note of an already-downloaded link too: it is
                    # about to be removed from the .txt file along with the link.
                    if note and code not in seen:
                        seen.add(code)
                        results.append({
                            "code": code, "url": url, "note": note, "success": False,
                            "skipped": True, "kind": None, "files": on_disk.get(code, []),
                        })
                    continue

                logger.info("Processing %s", url, extra=QUIET)
                result = {"code": code, "url": url, "note": note, "success": False,
                          "kind": None, "files": [], "error": None}
                try:
                    result["kind"], result["files"] = download_post(loader, code, current, file_task)
                    result["success"] = True
                    blocked_streak = 0
                except InstagramBlocked as exc:
                    result["error"] = str(exc)
                    blocked_streak += 1
                    blocked_total += 1
                    logger.warning("Instagram refused the request for %s (%s).", url, exc, extra=QUIET)
                    if blocked_streak >= BLOCK_ABORT_THRESHOLD:
                        aborted = True
                except Exception as exc:  # noqa: BLE001 - one failure must not stop the run
                    result["error"] = str(exc) or type(exc).__name__
                    blocked_streak = 0
                    logger.warning("Failed to download %s: %s", url, result["error"], extra=QUIET)

                results.append(result)
                console.print(result_line(result))
                overall.advance(overall_task)

                record = {
                    "link": url,
                    "shortcode": code,
                    "datetime": datetime.now().isoformat(timespec="seconds"),
                    "success": result["success"],
                }
                if result["success"]:
                    record["kind"] = result["kind"]
                    record["files"] = result["files"]
                if note:
                    record["note"] = note
                if result["error"]:
                    record["error"] = result["error"]
                history.append(record)
                save_history(history)

                if result["success"]:
                    resolved.add(code)
                    downloaded = sum(1 for r in results if r["success"])
                    # Extended break every 20 successful downloads (1-2 minutes).
                    if downloaded % 20 == 0:
                        pause(overall, overall_task, random.uniform(60, 120), "Taking a longer break")
                    else:
                        pause(overall, overall_task, random.uniform(3, 8), "Waiting")
                else:
                    if aborted:
                        break
                    pause(overall, overall_task, random.uniform(2, 5), "Waiting")

            # Remove only the links that are actually downloaded; whatever is
            # still pending stays in the file so it can be retried next run.
            left = prune_file(path, resolved)
            if left is not None:
                file_status.append((os.path.basename(path), left))

        if total:
            overall.update(overall_task, description="Done" if not aborted else "Stopped")

    # Refresh the queue file and the failed-downloads record.
    still_queued = [url for url in pending if shortcode_from_url(url) not in resolved]
    with open(QUEUE_FILE, "w", encoding="utf-8") as fh:
        fh.writelines(url + "\n" for url in still_queued)
    outstanding = write_failed_report(history, resolved)

    downloaded = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"] and not r.get("skipped")]
    logger.info(
        "Run finished: %d downloaded, %d failed, %d skipped, %d still pending.",
        len(downloaded), len(failed), skipped, len(outstanding), extra=QUIET,
    )

    show_summary(results, skipped, outstanding, file_status)
    if aborted or (blocked_total and not downloaded):
        show_block_help(signed_in, aborted, blocked_streak, blocked_total)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print()
        console.print("[yellow]Interrupted. Nothing was lost - your pending links are still in the Links folder.[/]")
        logger.info("Interrupted by the user.", extra=QUIET)
