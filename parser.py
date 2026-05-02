import requests
from bs4 import BeautifulSoup
import json
from datetime import datetime
import re

def scrape_custom(url, config):
    """
    Generic scraper function using CSS selectors.
    config = {
        'container': '.post-card',
        'title': 'h2',
        'link': 'a',
        'date': '.date',
        'base_url': 'https://example.com' # for relative links
    }
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        items = []
        containers = soup.select(config['container'])
        
        for card in containers:
            try:
                title_el = card.select_one(config['title'])
                
                # If card itself is an 'a' tag, use it as the link
                if config['link'] == 'a' and card.name == 'a':
                    link_el = card
                else:
                    link_el = card.select_one(config['link'])
                    
                date_el = card.select_one(config['date'])
                
                if not title_el or not link_el:
                    continue
                    
                title = title_el.get_text(strip=True)
                description = card.select_one(config['description']).get_text(strip=True)
                link = link_el.get('href', '#')
                if not link.startswith('http'):
                    link = config.get('base_url', '') + link
                
                # Robust date parsing
                date_str = ""
                if date_el:
                    date_str = date_el.get('datetime', date_el.get_text(strip=True))
                
                timestamp = datetime.now().isoformat()
                if date_str:
                    try:
                        # Try ISO format (YYYY-MM-DD)
                        if re.match(r'\d{4}-\d{2}-\d{2}', date_str):
                            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
                        else:
                            # Try textual (April 27, 2026)
                            date_clean = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', date_str)
                            dt = datetime.strptime(date_clean, "%B %d, %Y")
                        timestamp = dt.isoformat()
                    except:
                        pass

                items.append({
                    "title": title,
                    "link": link,
                    "description": description,
                    "image": "https://via.placeholder.com/400x250?text=Custom+Scrape",
                    "datetimestamp": timestamp,
                    "scraped_at": datetime.now().isoformat(),
                    "source": config.get('source_name', 'Custom Source'),
                    "category": config.get('category', 'General')
                })
            except Exception as e:
                print(f"Error parsing card: {e}")
                continue
                
        return items
    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return []

# Configuration for sites without RSS
SCRAPE_CONFIGS = [
    {
        'source_name': 'AssemblyAI',
        'url': 'https://www.assemblyai.com/blog',
        'category': 'AI',
        'config': {
            'container': '.blog-card_component',
            'title': '.blog-card_heading',
            'link': 'a.absolute-link_component',
            'date': '.blog-card_date',
            'base_url': 'https://www.assemblyai.com'
        }
    },
    {
        'source_name': 'Anthropic News',
        'url': 'https://www.anthropic.com/news',
        'category': 'AI',
        'config': {
            'container': 'li:has(> a[href*="/news/"])', 
            'title': 'h4, span:last-child', # h4 for featured, span for list
            'description': 'p',
            'link': 'a',
            'date': 'time',
            'base_url': 'https://www.anthropic.com'
        }
    }
    # Add more here
]

def main():
    all_news = []
    for site in SCRAPE_CONFIGS:
        print(f"Scraping {site['source_name']}...")
        news = scrape_custom(site['url'], site['config'])
        news = [ {**item, 'source': site['source_name'], 'category': site['category']} for item in news ]
        all_news.extend(news)
        print(f"Found {len(news)} items.")

    if all_news:
        with open('custom_scraped_news.json', 'w') as f:
            json.dump(all_news, f, indent=2)
        print(f"Successfully saved {len(all_news)} items to custom_scraped_news.json")

if __name__ == "__main__":
    main()
