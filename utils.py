import re
import time
import logging
from html.parser import HTMLParser

logger = logging.getLogger(__name__)


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts)


def strip_html(text: str) -> str:
    """Remove HTML tags and decode entities from job board descriptions."""
    if not text:
        return text
    stripper = _HTMLStripper()
    try:
        stripper.feed(text)
        result = stripper.get_text()
    except Exception:
        result = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", result).strip()


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
