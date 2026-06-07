import time
import logging

logger = logging.getLogger(__name__)


def retry(fn, retries: int = 3, backoff: float = 2.0):
    """Call fn(), retry on exception with exponential backoff."""
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = backoff ** attempt
            logger.warning(f"Attempt {attempt + 1}/{retries} failed: {e}. Retry in {wait:.0f}s...")
            time.sleep(wait)
