# InstaReelBulkDownload

![logo_inta_resized](https://github.com/user-attachments/assets/fb17df3b-1d98-44a2-8248-39ae4c789452)


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

Create a file named `links_to_download.txt` and list each Instagram Reel link on a separate line.

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

- Checks for the existence of `queue.log`. If it doesn't exist or is empty, the script copies links from `links_to_download.txt` to `queue.log`.
- Downloads each video listed in `queue.log` into a folder called `Downloads/`.
- Removes successfully downloaded links from the queue to avoid duplicate processing.
- Clearly reports invalid or skipped links in the console.
- Introduces random pauses between downloads to mitigate IP blocking.

## Project Structure

```
project-folder/
├── Downloads/                 # Folder where videos will be saved
├── links_to_download.txt      # Source file containing Instagram Reel URLs
├── queue.log                  # Temporary queue file managed automatically
├── insta_reel_bulk_download.py # Main Python script
├── README.md                  # Project documentation
```

## Notes

- Ensure the Instagram Reel links are **public**.
- Be responsible: excessive or aggressive scraping may result in IP blocking.
- **Use at your own risk.** Always respect Instagram’s terms of service.
