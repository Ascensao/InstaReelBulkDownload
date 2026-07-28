# main.py
# InstaReelBulkDownload - v3.0.0
# 2026-07-28
#
# Reads link lists from the "Links/" folder (one or more .txt files that may
# also contain notes/annotations), extracts the Instagram Reel links, and
# downloads them in bulk. A JSON history and the logs are kept in the "Core/"
# folder.
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
import tempfile
from datetime import datetime

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
    r"https?://(?:www\.)?instagram\.com/(?:reels?|p)/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)

for _directory in (LINKS_DIR, DOWNLOAD_DIR, CORE_DIR):
    os.makedirs(_directory, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging (console + Core/app.log)
# ---------------------------------------------------------------------------
logger = logging.getLogger("insta_bulk")
logger.setLevel(logging.INFO)

_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
)
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_console_handler)


class InstagramBlocked(Exception):
    """Instagram refused the request (403, rate limit, or login required)."""


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


def extract_links(path):
    """Return an ordered ``{shortcode: clean_url}`` dict for one .txt file,
    ignoring any surrounding notes/annotations."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return {}

    links = {}
    for url in INSTAGRAM_URL_RE.findall(content):
        code = shortcode_from_url(url)
        links.setdefault(code, url.split("?")[0])  # keep first clean URL per code
    logger.info("Found %d link(s) in %s", len(links), os.path.basename(path))
    return links


def prune_file(path, resolved):
    """Drop the already-downloaded links from a .txt file.

    Lines whose links are all downloaded are removed; notes and links that are
    still pending are kept, so a failed run never loses anything. The file is
    only deleted once no link is left in it.
    """
    name = os.path.basename(path)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
    except OSError as exc:
        logger.warning("Could not update %s: %s", name, exc)
        return

    kept = []
    for line in lines:
        urls = INSTAGRAM_URL_RE.findall(line)
        if urls and all(shortcode_from_url(u) in resolved for u in urls):
            continue  # every link on this line is downloaded
        kept.append(line)

    pending = sum(len(INSTAGRAM_URL_RE.findall(line)) for line in kept)
    if not pending:
        try:
            os.remove(path)
            logger.info("Finished and removed: %s", name)
        except OSError as exc:
            logger.warning("Could not remove %s: %s", name, exc)
        return

    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(kept)
        logger.info("Kept %s - %d link(s) still pending.", name, pending)
    except OSError as exc:
        logger.warning("Could not update %s: %s", name, exc)


def existing_shortcodes(folder):
    """Return the set of shortcodes already present (any .mp4 under ``folder``,
    including sub-folders the user may have organised manually)."""
    codes = set()
    for path in glob.glob(os.path.join(folder, "**", "*.mp4"), recursive=True):
        codes.add(os.path.splitext(os.path.basename(path))[0])
    return codes


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

    print()
    print("-" * 62)
    print(" Instagram no longer allows anonymous downloads (it answers 403).")
    print(" You only need to do this once - the session is then saved in the")
    print(" Core folder and reused automatically on the next runs.")
    print()
    print("   1) Open a login window and sign in there (any account)")
    print("   2) Use the session from a browser where you are already logged in")
    print("   3) Type an Instagram username and password")
    print("   4) Continue without signing in (downloads will almost certainly fail)")
    print("-" * 62)

    while True:
        try:
            choice = input(" Option [1/2/3/4]: ").strip()
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


def download_reel(loader, shortcode, url):
    """Download a single reel. Returns ``True`` on success, ``False`` otherwise."""
    post = fetch_post(loader, shortcode)
    video_url = post.video_url
    if not video_url:
        logger.warning("No video found at %s. Skipping.", url)
        return False

    filename = os.path.join(DOWNLOAD_DIR, f"{shortcode}.mp4")
    response = requests.get(video_url, stream=True, timeout=60)
    if response.status_code != 200:
        logger.warning("Failed to download %s (HTTP %s).", url, response.status_code)
        return False

    with open(filename, "wb") as out_file:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                out_file.write(chunk)
    logger.info("Downloaded successfully: %s", filename)
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    logger.info("=" * 60)
    logger.info("InstaReelBulkDownload started.")

    migrate_legacy()

    txt_files = sorted(glob.glob(os.path.join(LINKS_DIR, "*.txt")))
    if not txt_files:
        logger.info("No .txt files found in the 'Links' folder.")
        logger.info("Add one or more .txt files with your reel links into:")
        logger.info("  %s", LINKS_DIR)
        return

    history = load_history()
    on_disk = existing_shortcodes(DOWNLOAD_DIR)
    # Everything that no longer needs downloading: already on disk, or recorded
    # as a successful download in the history.
    resolved = set(on_disk)
    resolved.update(rec.get("shortcode") for rec in history if rec.get("success"))

    # Read every file first (so we can report totals and build queue.log).
    files_links = [(path, extract_links(path)) for path in txt_files]

    # De-duplicated list of links that still need downloading.
    pending = []
    queued = set()
    for _, links in files_links:
        for code, url in links.items():
            if code in resolved or code in queued:
                continue
            queued.add(code)
            pending.append(url)
    with open(QUEUE_FILE, "w", encoding="utf-8") as fh:
        fh.writelines(url + "\n" for url in pending)

    total = len(pending)
    total_links = sum(len(links) for _, links in files_links)
    logger.info(
        "Collected %d link(s) across %d file(s); %d new to download.",
        total_links, len(txt_files), total,
    )
    if not total:
        logger.info("Nothing new to download.")

    loader = instaloader.Instaloader()
    signed_in = sign_in(loader) if total else None

    downloaded = 0
    skipped = 0
    failed = []
    blocked_streak = 0
    blocked_total = 0
    aborted = False
    idx = 0

    for path, links in files_links:
        if aborted:
            break

        for code, url in links.items():
            if code in resolved:
                skipped += 1
                continue

            idx += 1
            logger.info("")
            logger.info("Processing %d/%d: %s", idx, total, url)

            error = None
            try:
                success = download_reel(loader, code, url)
                blocked_streak = 0
            except InstagramBlocked as exc:
                error = str(exc)
                success = False
                blocked_streak += 1
                blocked_total += 1
                logger.warning("Instagram refused the request for %s (%s).", url, error)
                if blocked_streak >= BLOCK_ABORT_THRESHOLD:
                    aborted = True
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the run
                error = str(exc)
                success = False
                blocked_streak = 0
                logger.warning("Failed to fetch data for %s: %s", url, error)

            record = {
                "link": url,
                "shortcode": code,
                "datetime": datetime.now().isoformat(timespec="seconds"),
                "success": success,
            }
            if error:
                record["error"] = error
            history.append(record)
            save_history(history)

            if success:
                downloaded += 1
                resolved.add(code)
                sleep_duration = random.uniform(3, 8)
                logger.info("Sleeping for %.2f seconds.", sleep_duration)
                time.sleep(sleep_duration)

                # Extended break every 20 successful downloads (1-2 minutes).
                if downloaded % 20 == 0:
                    extended_sleep = random.uniform(60, 120)
                    logger.info("Taking an extended break for %.2f minutes.", extended_sleep / 60)
                    time.sleep(extended_sleep)
            else:
                failed.append(url)
                if aborted:
                    break
                time.sleep(random.uniform(2, 5))  # small delay even on failure

        # Remove only the links that are actually downloaded; whatever is still
        # pending stays in the file so it can be retried on the next run.
        prune_file(path, resolved)

    # Refresh the queue file and the failed-downloads record.
    still_queued = [url for url in pending if shortcode_from_url(url) not in resolved]
    with open(QUEUE_FILE, "w", encoding="utf-8") as fh:
        fh.writelines(url + "\n" for url in still_queued)
    outstanding = write_failed_report(history, resolved)

    logger.info("")
    logger.info("=" * 60)
    if aborted or (blocked_total and not downloaded):
        if aborted:
            logger.info("Run stopped early: Instagram refused %d requests in a row.", blocked_streak)
        else:
            logger.info("Instagram refused every request in this run (%d).", blocked_total)
        logger.info("")
        if signed_in:
            logger.info("You are signed in as %s, so it is one of these two:", signed_in)
            logger.info("  - Instagram is rate limiting you. Wait a few hours and run it")
            logger.info("    again; your links were kept.")
            logger.info("  - instaloader is out of date. Instagram rotates the query ids it")
            logger.info("    relies on, and an old version cannot fetch any post. Fix with:")
            logger.info("      python -m pip install --upgrade instaloader")
        else:
            logger.info("You are NOT signed in, and Instagram no longer serves anonymous")
            logger.info("requests. Run the script again and pick option 1 to sign in.")
    else:
        logger.info("Download process completed.")
    logger.info("Videos successfully downloaded: %d", downloaded)
    logger.info("Skipped (already downloaded): %d", skipped)
    logger.info("Failed this run: %d", len(failed))
    logger.info("Still waiting to be downloaded: %d", len(outstanding))
    if outstanding:
        logger.info("Your pending links are kept in the Links folder and listed in:")
        logger.info("  %s", FAILED_FILE)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("")
        logger.info("Interrupted. Nothing was lost - your pending links are still in the Links folder.")
