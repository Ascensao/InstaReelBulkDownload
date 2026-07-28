# InstaReelBulkDownload

![logo_inta_resized](https://github.com/user-attachments/assets/fb17df3b-1d98-44a2-8248-39ae4c789452)


A Python script designed to easily download multiple Instagram Reels videos in
bulk.

> **Instagram now requires a signed-in session.** Anonymous requests are
> answered with `403 Forbidden`, so the script asks you to sign in on the first
> run and reuses the saved session afterwards. See [Signing in](#signing-in).

## Features

- Drop **several `.txt` files** with links into the `Links/` folder — the script
  automatically extracts the Instagram Reel links and **ignores any notes or
  annotations** mixed in with them.
- **No duplicate downloads:** anything already in `Downloads/` (including
  sub-folders you organised yourself) or already recorded as successful in the
  history is skipped.
- **Links are never lost.** Only the links that were actually downloaded are
  removed from a `.txt` file; whatever is still pending stays there (notes
  included) for the next run. A file is only deleted once it is empty.
- **Signs in once**, through a login window it opens for you, by borrowing the
  session from your browser, or with a username and password. The session is
  stored in `Core/` and reused automatically from then on.
- **Stops early when Instagram blocks you** (after 3 refusals in a row) instead
  of burning through the whole queue, and says clearly what happened.
- Keeps a **JSON history** (`Core/history.json`) with the link, date/time and
  whether each download succeeded.
- Writes a full run **log** to `Core/app.log`.
- Random pauses and an extended break every 20 videos to reduce the chance of
  being rate-limited by Instagram.
- **`run.bat`** launcher — just double-click, no need to open a console.

## Requirements

- Python 3.x (during installation, tick **"Add Python to PATH"**)

The launcher installs the needed libraries automatically the first time. To do
it manually:

```bash
pip install -r requirements.txt
```

## How to Use

### Step 1: Add your links

Put one or more `.txt` files inside the **`Links/`** folder. Each file can
contain plain links, one per line, and you may freely add notes around them —
only the Instagram Reel links are picked up. Example file:

```
Workout ideas:
https://www.instagram.com/reel/ABC123/

Recipe to try later
https://www.instagram.com/reel/XYZ789/?igsh=abcdef
```

### Step 2: Run it

**Double-click `run.bat`** (recommended), or from a console:

```bash
python main.py
```

### Signing in

The first time there is something to download, the script offers four options:

1. **Open a login window** (recommended). A Firefox window opens on the
   Instagram login page, using a profile of its own inside `Core/`. Sign in with
   **any account** — your everyday browser session is neither used nor touched —
   then close the window. The script picks up the session and deletes that
   throwaway profile straight away. Requires Firefox (any edition) to be
   installed.
2. **Use the session from a browser** where you are already logged in. Nothing
   is typed and no password is stored. Every Firefox profile is checked
   (including Developer Edition and ESR); on Windows, Chrome encrypts its
   cookies and usually cannot be read. Private windows never write the session
   to disk, so a session opened in one cannot be borrowed.
3. **Type a username and password.** Two-factor authentication is supported.
4. **Continue without signing in** — downloads will almost certainly fail with
   `403`.

The session is saved to `Core/session-<username>` and reused automatically, so
you are only asked once. `Core/` is git-ignored, so the session never leaves
your machine. To sign in with a different account, delete `Core/session.json`
and the `Core/session-*` file.

### What the script does

1. Reads every `.txt` file in `Links/` and extracts the reel links.
2. Skips anything already downloaded (checked against `Downloads/` and the
   history).
3. Signs in (saved session, browser cookies, or username/password).
4. Downloads the rest into `Downloads/` as `<shortcode>.mp4`.
5. Records every attempt in `Core/history.json`.
6. **Removes the downloaded links from each `.txt` file**, keeping the ones that
   are still pending. Files are deleted only once no link is left in them.
7. Lists everything still waiting in `Core/failed_downloads.txt` (rebuilt from
   the full history, so earlier failures are never overwritten).

### If Instagram blocks you

After 3 refusals in a row the run stops and tells you why:

- **Not signed in** → run it again and pick option 1.
- **Signed in** → it is rate limiting. Wait a few hours and run it again; your
  links are still in `Links/`.

## Project Structure

```
project-folder/
├── Links/                  # Drop your .txt link lists here (consumed on run)
├── Downloads/              # Folder where videos are saved
├── Core/                   # Generated at runtime (git-ignored):
│   ├── app.log             #   run log
│   ├── history.json        #   link + datetime + success for every attempt
│   ├── queue.log           #   links still pending
│   ├── failed_downloads.txt#   links that failed and are not downloaded yet
│   ├── session.json        #   which account the saved session belongs to
│   └── session-<user>      #   the saved Instagram session
├── main.py                 # Main Python script
├── run.bat                 # Double-click launcher (Windows)
├── requirements.txt        # Python dependencies
└── README.md               # This file
```

## Notes

- Ensure the Instagram Reel links are **public**.
- Be responsible: excessive or aggressive scraping may result in IP blocking —
  or in the signed-in account being flagged. Prefer the browser-session option,
  and keep the batches small.
- The session lives in `Core/`, which is git-ignored. Do not share that folder.
- **Use at your own risk.** Always respect Instagram's terms of service.
