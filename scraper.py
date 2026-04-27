import feedparser
import json
import re
import os
import time
import sqlite3
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from parser import scrape_custom, SCRAPE_CONFIGS
import requests
from bs4 import BeautifulSoup
import hashlib
from io import BytesIO
from PIL import Image

# Configuration
DATA_DIR = "feeds"
DB_NAME = "tech_news.db"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs("images", exist_ok=True)

# List of all 120+ feeds (Categories: AI, Frontend, System Design, etc.)
with open('feeds.json', 'r', encoding='utf-8') as f:
    FEEDS = json.load(f)

def get_link_preview(url):
    response = requests.get(url, headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.159 Safari/537.36"}, timeout=30)
    soup = BeautifulSoup(response.content, 'lxml')

    def get_meta(name):
        tag = soup.find("meta", property=name) or soup.find("meta", attrs={"name": name})
        return tag['content'] if tag and 'content' in tag.attrs else None

    preview = {
        "title": soup.title.string if soup.title else get_meta("og:title"),
        "description": get_meta("og:description") or get_meta("description"),
        "image": get_meta("og:image"),
        "url": get_meta("og:url") or url,
        "textContent": soup.get_text()
    }
    # save the compressed image in a folder in .webp format
    if preview.get("image") and preview["image"].startswith("http"):
        try:
            # Add user agent to image fetch as well to prevent 403s
            img_resp = requests.get(preview["image"], stream=True, timeout=10, headers={"user-agent": "Mozilla/5.0"})
            if img_resp.status_code == 200:
                img_data = img_resp.content
                img_hash = hashlib.md5(img_data).hexdigest()
                webp_filename = f"{img_hash}.webp"
                webp_path = os.path.join("images", webp_filename)
                
                with Image.open(BytesIO(img_data)) as img:
                    if img.mode in ("RGBA", "P"):
                        img = img.convert("RGB")
                    # Keep aspect ratio, max 800px on longest side
                    img.thumbnail((800, 800))
                    img.save(webp_path, "WEBP", quality=80)
                
                preview["image"] = f"images/{webp_filename}"
        except Exception as e:
            print(f"Error compressing image for {url}: {e}")

    return preview

def slugify(text):
    return re.sub(r'[-\s]+', '-', re.sub(r'[^\w\s-]', '', text.lower())).strip('-')

def init_db():
    # 30s timeout and WAL mode strictly prevent multithread "database locked" errors
    conn = sqlite3.connect(DB_NAME, timeout=30.0)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('''CREATE TABLE IF NOT EXISTS news (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    link TEXT UNIQUE, title TEXT, description TEXT, 
                    image TEXT, date TEXT, source TEXT, category TEXT)''')
    conn.commit()
    return conn

def extract_image(entry):
    # Try multiple common RSS image locations
    if 'media_content' in entry and len(entry.media_content) > 0:
        return entry.media_content[0].get('url', 'https://via.placeholder.com/400x250?text=Tech+News')
    if 'media_thumbnail' in entry and len(entry.media_thumbnail) > 0:
        return entry.media_thumbnail[0].get('url', 'https://via.placeholder.com/400x250?text=Tech+News')
    
    html = entry.get('summary', '') + entry.get('content', [{}])[0].get('value', '')
    match = re.search(r'<img [^>]*src="([^"]+)"', html)
    return match.group(1) if match else "https://via.placeholder.com/400x250?text=Tech+News"

def process_feed(feed):
    conn = init_db()
    feed_url = feed.get('feedurl')
    feed_name = feed.get('feedname', 'Unknown Source')
    
    if not feed_url:
        print(f"Skipping feed with no URL: {feed_name}")
        return []

    json_path = f"{DATA_DIR}/{slugify(feed_name)}.json"
    existing_items = []
    if os.path.exists(json_path):
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                existing_items = json.load(f)
        except: existing_items = []

    latest_timestamp = "0000-01-01T00:00:00"
    if existing_items:
        latest_timestamp = max(i.get('datetimestamp', "0000-01-01T00:00:00") for i in existing_items)

    new_fetched_items = []
    try:
        parsed = feedparser.parse(feed_url)
        for entry in parsed.entries:
            dt = entry.get('published_parsed')
            timestamp = datetime(*dt[:6]).isoformat() if dt else datetime.now().isoformat()

            if timestamp <= latest_timestamp:
                continue
            res = get_link_preview(entry.get('link'))
            item = {
                "title": res['title'],
                "link": res['url'],
                "description": res['description'],
                "image": res['image'],
                "datetimestamp": timestamp,
                "source": feed_name,
                "category": feed.get('category', 'General')
            }
            new_fetched_items.append(item)
            conn.execute("INSERT OR IGNORE INTO news (link, title, description, image, date, source, category) VALUES (?,?,?,?,?,?,?)",
                         (item['link'], item['title'], item['description'], item['image'], item['datetimestamp'], item['source'], item['category']))
        
        if new_fetched_items:
            all_items = new_fetched_items + existing_items
            all_items.sort(key=lambda x: x['datetimestamp'], reverse=True)
            with open(json_path, 'a', encoding='utf-8') as f:
                json.dump(all_items, f, indent=2)
            conn.commit()
            print(f"Added {len(new_fetched_items)} new items to {feed_name}")
    except Exception as e: 
        print(f"Error processing {feed_name}: {e}")
    finally: 
        conn.close()
    
    return new_fetched_items + existing_items

def process_custom_site(site):
    """Bridge function for custom scrapers to use the same persistence logic."""
    conn = init_db()
    source_name = site['source_name']
    json_path = f"{DATA_DIR}/{slugify(source_name)}.json"
    
    existing_items = []
    if os.path.exists(json_path):
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                existing_items = json.load(f)
        except: existing_items = []

    latest_timestamp = "0000-01-01T00:00:00"
    if existing_items:
        latest_timestamp = max(i.get('datetimestamp', "0000-01-01T00:00:00") for i in existing_items)

    print(f"Scraping custom source: {source_name}...")
    scraped_items = scrape_custom(site['url'], site['config'])
    
    new_items = []
    for item in scraped_items:
        item['source'] = source_name
        item['category'] = site['category']
        
        if item['datetimestamp'] <= latest_timestamp:
            continue
            
        new_items.append(item)
        conn.execute("INSERT OR IGNORE INTO news (link, title, description, image, date, source, category) VALUES (?,?,?,?,?,?,?)",
                     (item['link'], item['title'], item['description'], item['image'], item['datetimestamp'], item['source'], item['category']))

    if new_items:
        all_items = new_items + existing_items
        all_items.sort(key=lambda x: x['datetimestamp'], reverse=True)
        with open(json_path, 'a', encoding='utf-8') as f:
            json.dump(all_items, f, indent=2)
        conn.commit()
        print(f"Added {len(new_items)} new items to {source_name}")
    
    conn.close()
    return new_items + existing_items

def main():
    # 1. Process standard RSS feeds in parallel
    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(process_feed, FEEDS))
    
    # 2. Process custom-scraped sites
    # for site in SCRAPE_CONFIGS:
    #     try:
    #         custom_items = process_custom_site(site)
    #         results.append(custom_items)
    #     except Exception as e:
    #         print(f"Error processing custom site {site['source_name']}: {e}")
    
    # 3. Aggregate all Global Headlines
    flat = [i for sub in results for i in sub]
    flat.sort(key=lambda x: x['datetimestamp'], reverse=True)
    with open('news.json', 'w', encoding='utf-8') as f: 
        json.dump(flat[:20], f, indent=2) # Increased limit slightly

    # SQL Dump for Git-friendly backup
    os.system(f"sqlite3 {DB_NAME} .dump > backup.sql")

if __name__ == "__main__":
    main()