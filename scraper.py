import asyncio
import aiohttp
import feedparser
import json
import re
import os
from datetime import datetime, timedelta
from parser import scrape_custom, SCRAPE_CONFIGS
from bs4 import BeautifulSoup
import random

# Configuration
DATA_DIR = "feeds"
TEMP_DIR = "temp_feeds"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

DATE_FORMAT = "%d-%b-%Y" # e.g. 02-May-2026
INDEX_FILE = os.path.join(DATA_DIR, "index.json")

# Limit concurrency
MAX_CONCURRENT_REQUESTS = 25
semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

# List of all feeds
try:
    with open('feeds.json', 'r', encoding='utf-8') as f:
        FEEDS = json.load(f)
except Exception as e:
    print(f"Could not load feeds.json: {e}")
    FEEDS = []

async def fetch_url(session, url):
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.159 Safari/537.36"
    }
    async with semaphore:
        try:
            async with session.get(url, headers=headers, timeout=30) as response:
                if response.status == 200:
                    return await response.read()
                return None
        except Exception as e:
            print(f"Error fetching {url}: {e}")
            return None

async def get_link_preview(session, url):
    content = await fetch_url(session, url)
    if not content:
        return {"title": None, "description": None, "image": None, "url": url, "textContent": ""}

    try:
        soup = BeautifulSoup(content, 'lxml')

        def get_meta(name):
            tag = soup.find("meta", property=name) or soup.find("meta", attrs={"name": name})
            return tag['content'] if tag and 'content' in tag.attrs else None

        preview = {
            "title": (soup.title.string if soup.title else get_meta("og:title")) or "",
            "description": (get_meta("og:description") or get_meta("description")) or "",
            "image": get_meta("og:image"),
            "url": get_meta("og:url") or url,
            "textContent": soup.get_text()[:1000]
        }
        return preview
    except Exception as e:
        print(f"Error parsing preview for {url}: {e}")
        return {"title": None, "description": None, "image": None, "url": url, "textContent": ""}

def slugify(text):
    return re.sub(r'[-\s]+', '-', re.sub(r'[^\w\s-]', '', text.lower())).strip('-')

def get_existing_links_and_latest_timestamp():
    """Look through the last few days of daily feeds to get existing links and the latest timestamp."""
    links = set()
    latest_ts = (datetime.now() - timedelta(days=2)).isoformat()
    
    # Check last 3 days of files
    for i in range(3):
        date_str = (datetime.now() - timedelta(days=i)).strftime(DATE_FORMAT)
        path = os.path.join(DATA_DIR, f"feed-{date_str}.json")
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for item in data:
                        if 'link' in item: links.add(item['link'])
                        if 'datetimestamp' in item:
                            if item['datetimestamp'] > latest_ts:
                                latest_ts = item['datetimestamp']
            except:
                pass
    return links, latest_ts

async def fetch_feed(session, feed, existing_links, latest_timestamp):
    feed_url = feed.get('feedurl')
    feed_name = feed.get('feedname', 'Unknown Source')
    if not feed_url: return

    print(f"Phase 1 - Fetching : {feed_url}")
    
    content = await fetch_url(session, feed_url)
    if not content: return

    try:
        parsed = feedparser.parse(content)
        new_items = []
        for entry in parsed.entries:
            dt = entry.get('published_parsed')
            timestamp = datetime(*dt[:6]).isoformat() if dt else datetime.now().isoformat()

            if timestamp <= latest_timestamp or entry.get('link') in existing_links:
                continue
            
            item = {
                "title": entry.get('title', ''),
                "link": entry.get('link', ''),
                "description": entry.get('summary', ''),
                "image": "",
                "datetimestamp": timestamp,
                "scraped_at": datetime.now().isoformat(),
                "source": feed_name,
                "category": feed.get('category', 'General')
            }
            new_items.append(item)
        
        if new_items:
            temp_path = os.path.join(TEMP_DIR, f"{slugify(feed_name)}.json")
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(new_items[:50], f, indent=2)
            print(f"Phase 1 - Found {len(new_items)} new items for {feed_name}")
    except Exception as e:
        print(f"Error parsing feed {feed_name}: {e}")

async def process_temp_file(session, filename):
    temp_path = os.path.join(TEMP_DIR, filename)
    try:
        with open(temp_path, 'r', encoding='utf-8') as f:
            new_items = json.load(f)
        
        print(f"Phase 2 - Processing previews for: {filename}")
        preview_tasks = [get_link_preview(session, item['link']) for item in new_items if item.get('link')]
        previews = await asyncio.gather(*preview_tasks)
        preview_map = {p['url']: p for p in previews}
        
        for item in new_items:
            preview = preview_map.get(item['link'])
            if preview:
                if preview.get('title') and not item.get('title'):
                    item['title'] = preview['title']
                if preview.get('description') and (not item.get('description') or len(item['description']) < 50):
                    item['description'] = preview['description']
                if preview.get('image'):
                    item['image'] = preview['image']

        return new_items
    except Exception as e:
        print(f"Error Phase 2 processing {filename}: {e}")
        return []
    finally:
        if os.path.exists(temp_path): os.remove(temp_path)

async def main_async():
    # 0. Cleanup and Prep
    for filename in os.listdir(TEMP_DIR):
        try: os.remove(os.path.join(TEMP_DIR, filename))
        except: pass

    existing_links, latest_timestamp = get_existing_links_and_latest_timestamp()
    today_str = datetime.now().strftime(DATE_FORMAT)
    today_file = os.path.join(DATA_DIR, f"feed-{today_str}.json")

    async with aiohttp.ClientSession() as session:
        # Phase 1: RSS Feeds
        print("--- Phase 1: RSS Feeds ---")
        await asyncio.gather(*(fetch_feed(session, feed, existing_links, latest_timestamp) for feed in FEEDS))
        
        # Phase 1.5: Custom Scrapers
        print("--- Phase 1.5: Custom Scrapers ---")
        for site in SCRAPE_CONFIGS:
            try:
                # Note: scrape_custom is synchronous in parser.py, but we can wrap it or just run it
                print(f"Scraping custom site: {site['source_name']}")
                items = scrape_custom(site['url'], site['config'])
                new_items = []
                for item in items:
                    if item['link'] not in existing_links and item['datetimestamp'] > latest_timestamp:
                        new_items.append(item)
                
                if new_items:
                    temp_path = os.path.join(TEMP_DIR, f"custom-{slugify(site['source_name'])}.json")
                    with open(temp_path, 'w', encoding='utf-8') as f:
                        json.dump(new_items[:50], f, indent=2)
            except Exception as e:
                print(f"Error scraping {site['source_name']}: {e}")

        # Phase 2: Link Previews
        print("--- Phase 2: Link Previews ---")
        temp_files = [f for f in os.listdir(TEMP_DIR) if f.endswith('.json')]
        results = await asyncio.gather(*(process_temp_file(session, f) for f in temp_files))
        
        all_new_items = []
        for batch in results:
            all_new_items.extend(batch)

    # Phase 3: Update Daily Feed
    print("--- Phase 3: Updating Daily Feed ---")
    today_data = []
    if os.path.exists(today_file):
        try:
            with open(today_file, 'r', encoding='utf-8') as f:
                today_data = json.load(f)
        except: pass

    # Filter out duplicates that might have slipped through
    today_links = {item['link'] for item in today_data}
    added_count = 0
    for item in all_new_items:
        if item['link'] not in today_links:
            today_data.append(item)
            today_links.add(item['link'])
            added_count += 1
    
    today_data.sort(key=lambda x: x['datetimestamp'], reverse=True)
    
    with open(today_file, 'w', encoding='utf-8') as f:
        json.dump(today_data, f, indent=2)
    print(f"Updated {today_file} with {added_count} new items.")

    # Phase 4: Update Index and news.json
    print("--- Phase 4: Updating Index and news.json ---")
    all_files = [f for f in os.listdir(DATA_DIR) if f.startswith('feed-') and f.endswith('.json')]
    # Sort files by date (extracting date from filename)
    def extract_date(filename):
        try:
            d_str = filename.replace('feed-', '').replace('.json', '')
            return datetime.strptime(d_str, DATE_FORMAT)
        except:
            return datetime(2000, 1, 1)
    
    all_files.sort(key=extract_date, reverse=True)
    index_data = []
    for f in all_files:
        d_str = f.replace('feed-', '').replace('.json', '')
        index_data.append({"date": d_str, "file": f})
    
    with open(INDEX_FILE, 'w', encoding='utf-8') as f:
        json.dump(index_data, f, indent=2)

    # Update news.json (shuffled version of today's feed for the main page)
    # If today is empty, use the most recent available file
    latest_feed_data = today_data
    if not latest_feed_data and index_data:
        try:
            with open(os.path.join(DATA_DIR, index_data[0]['file']), 'r', encoding='utf-8') as f:
                latest_feed_data = json.load(f)
        except: pass

    # For news.json, we still shuffle or just keep it sorted? 
    # Let's keep it sorted for reliability but shuffle as requested in previous logic
    random.shuffle(latest_feed_data)
    with open('news.json', 'w', encoding='utf-8') as f:
        json.dump(latest_feed_data, f, indent=2)

    # Phase 5: SEO Updates
    print("--- Phase 5: SEO Updates ---")
    current_date_str = datetime.now().strftime("%A, %B %d, %Y")
    iso_date = datetime.now().strftime("%Y-%m-%d")
    index_files = ['index.html', 'v1/index.html']
    
    top_10 = [item for item in today_data if item.get('title')][:10]
    schema_items = []
    for i, item in enumerate(top_10):
        img = item['image'] if item.get('image') and item['image'].startswith('http') else "https://sivasubramoniam-js.github.io/the-dev-summary/logo.png"
        schema_items.append({
            "@type": "ListItem",
            "position": i + 1,
            "item": {
                "@type": "NewsArticle",
                "headline": item['title'],
                "url": item['link'],
                "datePublished": item['datetimestamp'],
                "image": img,
                "author": {"@type": "Organization", "name": item['source']},
                "publisher": {"@id": "https://sivasubramoniam-js.github.io/the-dev-summary/#organization"}
            }
        })
    
    items_json = json.dumps(schema_items, indent=12)

    for index_path in index_files:
        if os.path.exists(index_path):
            with open(index_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            content = re.sub(r'<title>.*?</title>', lambda m: f'<title>The Dev Summary | Tech News - {current_date_str}</title>', content)
            content = re.sub(r'<meta property="og:title" content=".*?">', lambda m: f'<meta property="og:title" content="The Dev Summary | News for {current_date_str}">', content)
            content = re.sub(r'<meta property="twitter:title" content=".*?">', lambda m: f'<meta property="twitter:title" content="The Dev Summary | News for {current_date_str}">', content)
            content = re.sub(
                r'("name":\s*"Top Tech Stories Today",\s*"itemListElement":\s*\[).*?(\])',
                lambda m: f'{m.group(1)}\n{items_json}\n{m.group(2)}',
                content,
                flags=re.DOTALL
            )
            with open(index_path, 'w', encoding='utf-8') as f:
                f.write(content)

    sitemap_path = 'sitemap.xml'
    if os.path.exists(sitemap_path):
        with open(sitemap_path, 'r', encoding='utf-8') as f:
            sitemap_content = f.read()
        sitemap_content = re.sub(r'<lastmod>.*?</lastmod>', lambda m: f'<lastmod>{iso_date}</lastmod>', sitemap_content)
        with open(sitemap_path, 'w', encoding='utf-8') as f:
            f.write(sitemap_content)

    print("Completed successfully!")

if __name__ == "__main__":
    asyncio.run(main_async())