import logging
import feedparser
from datetime import datetime
from utils import retry

logger = logging.getLogger(__name__)

RSS_FEEDS = [
    "https://weworkremotely.com/categories/remote-product-jobs.rss",
    "https://weworkremotely.com/categories/remote-management-and-finance-jobs.rss",
]


def fetch() -> list[dict]:
    jobs = []
    seen_urls: set[str] = set()

    for feed_url in RSS_FEEDS:
        try:
            feed = retry(lambda url=feed_url: feedparser.parse(url))
        except Exception as e:
            logger.error(f"WWR RSS parse failed after retries for {feed_url}: {e}")
            continue

        for entry in feed.entries:
            url = entry.get("link", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            title = entry.get("title", "")
            company = ""
            # WWR titles are often formatted as "Company: Job Title"
            if ": " in title:
                parts = title.split(": ", 1)
                company = parts[0].strip()
                title = parts[1].strip()

            published = ""
            pp = entry.get("published_parsed")
            if pp:
                published = datetime(*pp[:3]).date().isoformat()

            jobs.append({
                "title": title,
                "company": company,
                "url": url,
                "description": entry.get("summary", ""),
                "location": "Remote",
                "salary": "",
                "source": "WeWorkRemotely",
                "published": published,
            })

    logger.info(f"WeWorkRemotely: fetched {len(jobs)} jobs")
    return jobs
