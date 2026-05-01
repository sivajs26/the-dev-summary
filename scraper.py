import feedparser
import json
import re
import os
# import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
# from email.utils import parsedate_to_datetime
from parser import scrape_custom, SCRAPE_CONFIGS
import requests
from bs4 import BeautifulSoup
import random
# import hashlib
# from io import BytesIO

# Configuration
DATA_DIR = "feeds"
TEMP_DIR = "temp_feeds"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs("images", exist_ok=True)

# List of all 120+ feeds (Categories: AI, Frontend, System Design, etc.)
try:
    with open('feeds.json', 'r', encoding='utf-8') as f:
        FEEDS = json.load(f)
except Exception as e:
    print(f"Could not load feeds.json: {e}")
    FEEDS = []

def get_link_preview(url):
    try:
        response = requests.get(
            url, 
            headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.159 Safari/537.36"}, 
            timeout=30
        )
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
        # if preview.get("image") and preview["image"].startswith("http"):
        #     try:
        #         # Add user agent to image fetch as well to prevent 403s
        #         img_resp = requests.get(preview["image"], stream=True, timeout=10, headers={"user-agent": "Mozilla/5.0"})
        #         if img_resp.status_code == 200:
        #             img_data = img_resp.content
        #             img_hash = hashlib.md5(img_data).hexdigest()
        #             webp_filename = f"{img_hash}.webp"
        #             webp_path = os.path.join("images", webp_filename)
                    
        #             if not os.path.exists(webp_path):
        #                 with Image.open(BytesIO(img_data)) as img:
        #                     if img.mode in ("RGBA", "P"):
        #                         img = img.convert("RGB")
        #                     # Keep aspect ratio, max 800px on longest side
        #                     img.thumbnail((800, 800))
        #                     img.save(webp_path, "WEBP", quality=80)
                    
        #             preview["image"] = f"images/{webp_filename}"
        #     except Exception as e:
        #         print(f"Error compressing image for {url}: {e}")

        return preview
    except Exception as e:
        print(f"Error fetching preview for {url}: {e}")
        return {
            "title": None,
            "description": None,
            "image": None,
            "url": url,
            "textContent": ""
        }

def load_concatenated_json(filepath):
    items = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        if not content:
            return []
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(content):
            while idx < len(content) and content[idx].isspace():
                idx += 1
            if idx >= len(content):
                break
            obj, end_idx = decoder.raw_decode(content[idx:])
            if isinstance(obj, list):
                items.extend(obj)
            elif isinstance(obj, dict):
                items.append(obj)
            idx += end_idx
    except Exception as e:
        print(f"Failed to recover {filepath}: {e}")
    return items

def slugify(text):
    return re.sub(r'[-\s]+', '-', re.sub(r'[^\w\s-]', '', text.lower())).strip('-')

def extract_image(entry):
    # Try multiple common RSS image locations
    if 'media_content' in entry and len(entry.media_content) > 0:
        return entry.media_content[0].get('url', 'https://via.placeholder.com/400x250?text=Tech+News')
    if 'media_thumbnail' in entry and len(entry.media_thumbnail) > 0:
        return entry.media_thumbnail[0].get('url', 'https://via.placeholder.com/400x250?text=Tech+News')
    
    html = entry.get('summary', '') + entry.get('content', [{}])[0].get('value', '')
    match = re.search(r'<img [^>]*src="([^"]+)"', html)
    return match.group(1) if match else "https://via.placeholder.com/400x250?text=Tech+News"

def phase1_fetch_feed(feed):
    feed_url = feed.get('feedurl')
    feed_name = feed.get('feedname', 'Unknown Source')
    print(f"Phase 1 - Fetching : {feed_url}")
    if not feed_url:
        print(f"Skipping feed with no URL: {feed_name}")
        return []

    # Get latest timestamp from the FINAL json path
    json_path = f"{DATA_DIR}/{slugify(feed_name)}.json"
    existing_items = []
    if os.path.exists(json_path):
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                existing_items = json.load(f)
        except: 
            existing_items = []

    if existing_items:
        latest_timestamp = max(i.get('datetimestamp', "0000-01-01T00:00:00") for i in existing_items)
    else:
        latest_timestamp = (datetime.now() - timedelta(days=5)).isoformat()

    new_fetched_items = []
    try:
        parsed = feedparser.parse(feed_url)
        for entry in parsed.entries:
            dt = entry.get('published_parsed')
            timestamp = datetime(*dt[:6]).isoformat() if dt else datetime.now().isoformat()

            if timestamp <= latest_timestamp:
                continue
            
            # Append the minimal data with empty image key
            item = {
                "title": entry.get('title', ''),
                "link": entry.get('link', ''),
                "description": entry.get('summary', ''),
                "image": "",
                "datetimestamp": timestamp,
                "source": feed_name,
                "category": feed.get('category', 'General')
            }
            new_fetched_items.append(item)
        
        # Save temp file with newly fetched items ONLY
        if new_fetched_items:
            new_fetched_items = new_fetched_items[:100]
            temp_path = f"{TEMP_DIR}/{slugify(feed_name)}.json"
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(new_fetched_items, f, indent=2)
            print(f"Phase 1 - Saved {len(new_fetched_items)} new temp items to {feed_name}")
            
    except Exception as e: 
        print(f"Error processing {feed_name}: {e}")
    
    return new_fetched_items

def phase1_process_custom_site(site):
    """Bridge function for custom scrapers to use the same persistence logic."""
    source_name = site['source_name']
    json_path = f"{DATA_DIR}/{slugify(source_name)}.json"
    
    existing_items = []
    if os.path.exists(json_path):
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                existing_items = json.load(f)
        except: 
            existing_items = []

    if existing_items:
        latest_timestamp = max(i.get('datetimestamp', "0000-01-01T00:00:00") for i in existing_items)
    else:
        latest_timestamp = (datetime.now() - timedelta(days=5)).isoformat()

    print(f"Phase 1 - Scraping custom source: {source_name}...")
    try:
        scraped_items = scrape_custom(site['url'], site['config'])
        # get the first 50 items from scraped_items
        scraped_items = scraped_items[:100]
        new_items = []
        for item in scraped_items:
            item['source'] = source_name
            item['category'] = site['category']
            
            if item['datetimestamp'] <= latest_timestamp:
                continue
                
            # Assume custom scraped items already might have title/link/description.
            # Give it empty image for now
            item['image'] = item.get('image', "")
            new_items.append(item)

        if new_items:
            temp_path = f"{TEMP_DIR}/{slugify(source_name)}.json"
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(new_items, f, indent=2)
            print(f"Phase 1 - Saved {len(new_items)} new temp items to {source_name}")
            
    except Exception as e:
        print(f"Error scraping custom source {source_name}: {e}")
        
    return []

def phase2_process_temp_file(filename):
    if not filename.endswith('.json'):
        return
        
    temp_path = os.path.join(TEMP_DIR, filename)
    final_path = os.path.join(DATA_DIR, filename)
    print(f"Phase 2 - Processing previews for: {filename}")
    
    try:
        with open(temp_path, 'r', encoding='utf-8') as f:
            new_items = json.load(f)
            
        processed_items = []
        for item in new_items:
            if item.get('link'):
                preview = get_link_preview(item['link'])
                # Use preview title ONLY if the original title from the feed is missing
                if preview.get('title') and not item.get('title'):
                    item['title'] = preview['title']
                
                # Update description and image if they are better than what we have
                if preview.get('description') and not preview.get('description').endswith('..'):
                    item['description'] = preview['description']
                if preview.get('image'):
                    item['image'] = preview['image']
            processed_items.append(item)
                
        existing_items = load_concatenated_json(final_path) if os.path.exists(final_path) else []
        all_items = processed_items + existing_items
        all_items.sort(key=lambda x: x['datetimestamp'], reverse=True)
        # We must overwrite (w) to wrap items in a single valid JSON array
        with open(final_path, 'w', encoding='utf-8') as f:
            json.dump(all_items, f, indent=2)
            
        # Done processing, remove the temp file
        os.remove(temp_path)
            
    except Exception as e:
        print(f"Error Phase 2 processing {filename}: {e}")

def main():
    # 0. Cleanup temp_feeds directory at the start to prevent old data processing
    if os.path.exists(TEMP_DIR):
        for filename in os.listdir(TEMP_DIR):
            file_path = os.path.join(TEMP_DIR, filename)
            try:
                if os.path.isfile(file_path):
                    os.unlink(file_path)
            except Exception as e:
                pass

    # 1. Process standard RSS feeds in parallel (Phase 1)
    print("--- Starting Phase 1: Fetching new feed items ---")
    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(phase1_fetch_feed, FEEDS))
    
    # 2. Process custom-scraped sites (Phase 1)
    # for site in SCRAPE_CONFIGS:
    #     try:
    #         phase1_process_custom_site(site)
    #     except Exception as e:
    #         print(f"Error processing custom site {site['source_name']}: {e}")
    
    # 3. Process temporary files to fetch previews and update main files (Phase 2)
    print("--- Starting Phase 2: Processing link previews ---")
    if os.path.exists(TEMP_DIR):
        for filename in os.listdir(TEMP_DIR):
            phase2_process_temp_file(filename)
            
    # 4. Aggregate all Global Headlines
    print("--- Starting Phase 3: Aggregating flat news feed ---")
    flat = []
    if os.path.exists(DATA_DIR):
        for filename in os.listdir(DATA_DIR):
            if filename.endswith('.json'):
                try:
                    filepath = os.path.join(DATA_DIR, filename)
                    items = load_concatenated_json(filepath)
                    if items:
                        # Re-save the file properly formatted to "heal" any corrupted arrays
                        with open(filepath, 'w', encoding='utf-8') as f:
                            json.dump(items, f, indent=2)
                    flat.extend(items)
                except Exception as e:
                    print(f"Error reading {filename} for aggregation: {e}")

    flat.sort(key=lambda x: x['datetimestamp'], reverse=True)
    # merge all the jsons in 'feeds' directory into a single file 'news.json'
    with open('news.json', 'w', encoding='utf-8') as f:
        flat = [x for x in flat if datetime.fromisoformat(x['datetimestamp']) > datetime.now() - timedelta(hours=24)]
        random.shuffle(flat)
        json.dump(flat, f, indent=2)

    # --- SEO Update Phase: Hardcode current date into HTML and Sitemap ---
    print("--- Starting SEO Update Phase: Syncing dates ---")
    current_date = datetime.now().strftime("%A, %B %d, %Y")
    iso_date = datetime.now().strftime("%Y-%m-%d")
    
    # 1. Update v1/index.html
    index_path = 'v1/index.html'
    if os.path.exists(index_path):
        with open(index_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Update <title>
        content = re.sub(r'<title>.*?</title>', f'<title>The Dev Summary | Tech News - {current_date}</title>', content)
        # Update Open Graph Title
        content = re.sub(r'<meta property="og:title" content=".*?">', f'<meta property="og:title" content="The Dev Summary | News for {current_date}">', content)
        # Update Twitter Title
        content = re.sub(r'<meta property="twitter:title" content=".*?">', f'<meta property="twitter:title" content="The Dev Summary | News for {current_date}">', content)
        
        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Updated {index_path} with date: {current_date}")

    # 2. Update sitemap.xml
    sitemap_path = 'sitemap.xml'
    if os.path.exists(sitemap_path):
        with open(sitemap_path, 'r', encoding='utf-8') as f:
            sitemap_content = f.read()
        
        sitemap_content = re.sub(r'<lastmod>.*?</lastmod>', f'<lastmod>{iso_date}</lastmod>', sitemap_content)
        
        with open(sitemap_path, 'w', encoding='utf-8') as f:
            f.write(sitemap_content)
        print(f"Updated {sitemap_path} with date: {iso_date}")

    print("Completed successfully!")

if __name__ == "__main__":
    main()