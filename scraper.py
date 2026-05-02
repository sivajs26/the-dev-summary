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
os.makedirs("images", exist_ok=True)

# Limit concurrency to avoid overloading the network/OS
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
        # BS4 parsing is CPU bound, but we can't easily avoid it here without complex process pools
        # For this scale, it's usually fine to run in the main thread
        soup = BeautifulSoup(content, 'lxml')

        def get_meta(name):
            tag = soup.find("meta", property=name) or soup.find("meta", attrs={"name": name})
            return tag['content'] if tag and 'content' in tag.attrs else None

        preview = {
            "title": soup.title.string if soup.title else get_meta("og:title"),
            "description": get_meta("og:description") or get_meta("description"),
            "image": get_meta("og:image"),
            "url": get_meta("og:url") or url,
            "textContent": soup.get_text()[:1000] # Truncate to save memory/space
        }
        return preview
    except Exception as e:
        print(f"Error parsing preview for {url}: {e}")
        return {"title": None, "description": None, "image": None, "url": url, "textContent": ""}

def slugify(text):
    return re.sub(r'[-\s]+', '-', re.sub(r'[^\w\s-]', '', text.lower())).strip('-')

async def fetch_feed(session, feed):
    feed_url = feed.get('feedurl')
    feed_name = feed.get('feedname', 'Unknown Source')
    if not feed_url:
        return

    print(f"Phase 1 - Fetching : {feed_url}")
    
    # Check existing data to only fetch new items
    json_path = os.path.join(DATA_DIR, f"{slugify(feed_name)}.json")
    latest_timestamp = (datetime.now() - timedelta(days=5)).isoformat()
    
    if os.path.exists(json_path):
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                existing = json.load(f)
                if existing:
                    latest_timestamp = max(i.get('datetimestamp', "0000-01-01T00:00:00") for i in existing)
        except:
            pass

    content = await fetch_url(session, feed_url)
    if not content:
        return

    try:
        parsed = feedparser.parse(content)
        new_items = []
        for entry in parsed.entries:
            dt = entry.get('published_parsed')
            timestamp = datetime(*dt[:6]).isoformat() if dt else datetime.now().isoformat()

            if timestamp <= latest_timestamp:
                continue
            
            item = {
                "title": entry.get('title', ''),
                "link": entry.get('link', ''),
                "description": entry.get('summary', ''),
                "image": "",
                "datetimestamp": timestamp,
                "source": feed_name,
                "category": feed.get('category', 'General')
            }
            new_items.append(item)
        
        if new_items:
            temp_path = os.path.join(TEMP_DIR, f"{slugify(feed_name)}.json")
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(new_items[:100], f, indent=2)
            print(f"Phase 1 - Saved {len(new_items)} new items for {feed_name}")
    except Exception as e:
        print(f"Error parsing feed {feed_name}: {e}")

async def process_temp_file(session, filename):
    temp_path = os.path.join(TEMP_DIR, filename)
    final_path = os.path.join(DATA_DIR, filename)
    
    try:
        with open(temp_path, 'r', encoding='utf-8') as f:
            new_items = json.load(f)
        
        print(f"Phase 2 - Processing previews for: {filename}")
        
        # Parallel preview fetching for items in this feed
        preview_tasks = [get_link_preview(session, item['link']) for item in new_items if item.get('link')]
        previews = await asyncio.gather(*preview_tasks)
        
        # Map previews back to items
        preview_map = {p['url']: p for p in previews}
        
        for item in new_items:
            preview = preview_map.get(item['link'])
            if preview:
                if preview.get('title') and not item.get('title'):
                    item['title'] = preview['title']
                if preview.get('description') and (not item.get('description') or len(item['description']) < len(preview['description'])):
                    item['description'] = preview['description']
                if preview.get('image'):
                    item['image'] = preview['image']

        # Load existing and merge
        existing_items = []
        if os.path.exists(final_path):
            try:
                with open(final_path, 'r', encoding='utf-8') as f:
                    existing_items = json.load(f)
            except:
                pass
        
        all_items = new_items + existing_items
        all_items.sort(key=lambda x: x['datetimestamp'], reverse=True)
        
        with open(final_path, 'w', encoding='utf-8') as f:
            json.dump(all_items[:200], f, indent=2) # Keep last 200 items per source
            
        os.remove(temp_path)
    except Exception as e:
        print(f"Error Phase 2 processing {filename}: {e}")

async def main_async():
    # 0. Cleanup
    for filename in os.listdir(TEMP_DIR):
        try:
            os.remove(os.path.join(TEMP_DIR, filename))
        except:
            pass

    async with aiohttp.ClientSession() as session:
        # Phase 1: Fetch all feeds in parallel
        print("--- Starting Phase 1: Fetching new feed items ---")
        await asyncio.gather(*(fetch_feed(session, feed) for feed in FEEDS))
        
        # Phase 2: Process temp files (previews) in parallel
        print("--- Starting Phase 2: Processing link previews ---")
        temp_files = [f for f in os.listdir(TEMP_DIR) if f.endswith('.json')]
        await asyncio.gather(*(process_temp_file(session, f) for f in temp_files))

    # Phase 3: Aggregation (Synchronous as it's local I/O)
    print("--- Starting Phase 3: Aggregating flat news feed ---")
    flat = []
    for filename in os.listdir(DATA_DIR):
        if filename.endswith('.json'):
            try:
                with open(os.path.join(DATA_DIR, filename), 'r', encoding='utf-8') as f:
                    flat.extend(json.load(f))
            except:
                pass

    flat.sort(key=lambda x: x['datetimestamp'], reverse=True)
    
    # Filter for last 24h and shuffle for a fresh feel
    cutoff = datetime.now() - timedelta(hours=24)
    fresh_news = [item for item in flat if datetime.fromisoformat(item['datetimestamp']) > cutoff]
    random.shuffle(fresh_news)
    
    with open('news.json', 'w', encoding='utf-8') as f:
        json.dump(fresh_news, f, indent=2)

    # SEO Updates
    print("--- Starting SEO Update Phase ---")
    current_date = datetime.now().strftime("%A, %B %d, %Y")
    iso_date = datetime.now().strftime("%Y-%m-%d")
    
    # Update index.html files
    index_files = ['index.html', 'v1/index.html']
    
    # Prepare schema items once
    top_10 = fresh_news[:10]
    schema_items = []
    for i, item in enumerate(top_10):
        # Fallback for image
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
                "author": {
                    "@type": "Organization",
                    "name": item['source']
                },
                "publisher": {
                    "@id": "https://sivasubramoniam-js.github.io/the-dev-summary/#organization"
                }
            }
        })
    
    items_json = json.dumps(schema_items, indent=12)

    for index_path in index_files:
        if os.path.exists(index_path):
            with open(index_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            content = re.sub(r'<title>.*?</title>', lambda m: f'<title>The Dev Summary | Tech News - {current_date}</title>', content)
            content = re.sub(r'<meta property="og:title" content=".*?">', lambda m: f'<meta property="og:title" content="The Dev Summary | News for {current_date}">', content)
            content = re.sub(r'<meta property="twitter:title" content=".*?">', lambda m: f'<meta property="twitter:title" content="The Dev Summary | News for {current_date}">', content)
            
            # Replace the empty or existing itemListElement array
            # Inject into Top Tech Stories schema (avoiding BreadcrumbList)
            content = re.sub(
                r'("name":\s*"Top Tech Stories Today",\s*"itemListElement":\s*\[).*?(\])',
                lambda m: f'{m.group(1)}\n{items_json}\n{m.group(2)}',
                content,
                flags=re.DOTALL
            )

            with open(index_path, 'w', encoding='utf-8') as f:
                f.write(content)

    # Update sitemap.xml
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