import instaloader
from instaloader import Post
import requests
import time
import random
import os
import shutil

# Ensure Downloads directory exists
download_dir = "Downloads"
os.makedirs(download_dir, exist_ok=True)

queue_file = "queue.log"
source_file = "links_to_download.txt"

# Copy links_to_download.txt to queue.log if queue.log doesn't exist or is empty
if not os.path.exists(queue_file) or os.path.getsize(queue_file) == 0:
    shutil.copyfile(source_file, queue_file)
    print("Copied links_to_download.txt to queue.log for processing.")

# Function to validate Instagram reel links
def is_valid_instagram_reel(link):
    return link.startswith("https://www.instagram.com/reel/")

# Load and filter URLs from queue.log
def load_urls_from_file(file_path):
    valid_urls = []
    invalid_urls = []

    with open(file_path, "r") as file:
        lines = file.readlines()

    for line in lines:
        line = line.strip()
        if not line:
            print("Skipped blank line.")
            continue
        elif not is_valid_instagram_reel(line):
            print(f"Invalid or non-Instagram link skipped: {line}")
            invalid_urls.append(line)
        else:
            valid_urls.append(line)

    return valid_urls

urls_to_process = load_urls_from_file(queue_file)

# Initialize Instaloader without authentication
loader = instaloader.Instaloader()

# Main download loop
videos_downloaded = 0
for idx, link in enumerate(urls_to_process.copy(), start=1):
    shortcode = link.split('?')[0].rstrip('/').split('/')[-1]

    try:
        post = Post.from_shortcode(loader.context, shortcode)
    except Exception as e:
        print(f"Failed to fetch data for {link}: {e}")
        continue

    video_url = post.video_url
    if not video_url:
        print(f"No video found at {link}. Skipping.")
        continue

    filename = os.path.join(download_dir, f"{shortcode}.mp4")

    print(f"Downloading video {idx}: {link}")
    response = requests.get(video_url, stream=True)

    if response.status_code == 200:
        with open(filename, "wb") as out_file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    out_file.write(chunk)
        print(f"Downloaded successfully: {filename}")

        # Remove successfully downloaded URL from queue
        urls_to_process.remove(link)
        with open(queue_file, "w") as file:
            file.writelines(url + "\n" for url in urls_to_process)

        videos_downloaded += 1

        # Regular random sleep to avoid Instagram blocking (3-8 sec)
        sleep_duration = random.uniform(3, 8)
        print(f"Sleeping for {sleep_duration:.2f} seconds.")
        time.sleep(sleep_duration)

        # Extended break every 20 videos (1-2 minutes)
        if videos_downloaded % 20 == 0:
            extended_sleep = random.uniform(60, 120)
            print(f"Taking extended break for {extended_sleep/60:.2f} minutes.")
            time.sleep(extended_sleep)
    else:
        print(f"Failed to download video from {link} (HTTP Status: {response.status_code})")

print("Download process completed.")
