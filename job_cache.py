import json
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).parent / "processed.json"


def load() -> dict:
    """Load processed jobs cache. Returns {} if missing or corrupt."""
    if not CACHE_PATH.exists():
        logger.info("Cache: no processed.json found, starting fresh")
        return {}
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        logger.info(f"Cache: loaded {len(data)} previously processed URLs")
        return data
    except Exception as e:
        logger.warning(f"Cache: read failed ({e}), starting fresh")
        return {}


def save(cache: dict) -> None:
    try:
        CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"Cache: saved {len(cache)} entries to processed.json")
    except Exception as e:
        logger.error(f"Cache: save failed: {e}")


def record(cache: dict, job: dict, reason: str, score: int = 0) -> None:
    """Add a processed job to the cache dict (in-memory, call save() at end)."""
    cache[job["url"]] = {
        "title": job["title"],
        "company": job["company"],
        "source": job["source"],
        "reason": reason,   # "qualified" | "low_score" | "role" | "location"
        "score": score,
        "date": date.today().isoformat(),
    }
