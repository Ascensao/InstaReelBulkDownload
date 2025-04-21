# InstaReelBulkDownload
![logo_InstaReelBulkDownload](https://github.com/user-attachments/assets/cff71e04-7be6-4e8b-bbfa-5d08d525b715)

A Python script designed to easily download multiple Instagram Reels videos in bulk without requiring Instagram login credentials.

## Features

- Download videos from a provided list of Instagram Reel links.
- Automatically manage the download queue.
- Skip invalid or unrelated links.
- Implement random pauses to prevent IP blocking.
- Automatically take extended breaks after every 20 videos downloaded.

## Requirements

- Python 3.x

Install necessary Python libraries using pip:

```bash
pip install instaloader requests
```

## How to Use

### Step 1: Prepare Your Links

Create a file named `reels.txt` and list each Instagram Reel link on a separate line.

Example:

```
https://www.instagram.com/reel/ABC123/
https://www.instagram.com/reel/XYZ789/
```

### Step 2: Run the Script

Execute the Python script:

```bash
python insta_reel_bulk_download.py
```

### What the script does:

- Checks for the existence of `queue.txt`. If it doesn't exist or is empty, the script copies links from `reels.txt` to `queue.txt`.
- Downloads each video listed in `queue.txt` into a folder called `Downloads/`.
- Removes successfully downloaded links from the queue to avoid duplicate processing.
- Clearly reports invalid or skipped links in the console.
- Introduces random pauses between downloads to mitigate IP blocking.

## Project Structure

```
project-folder/
├── Downloads/                 # Folder where videos will be saved
├── reels.txt                   # Source file containing Instagram Reel URLs
├── queue.txt                   # Temporary queue file managed automatically
├── insta_reel_bulk_download.py # Main Python script
├── README.md                   # Project documentation
```

## Notes

- Ensure the Instagram Reel links are **public**.
- Be responsible: excessive or aggressive scraping may result in IP blocking.
- **Use at your own risk.** Always respect Instagram’s terms of service.
