"""Logging shared by the command line tools."""
import logging
import time
from pathlib import Path


class SteadyTimeFilter(logging.Filter):
    """Log timestamps from start offset + monotonic clock: the WSL2 wall clock is stepped back and forth
    by time synchronisation, which would make the log go backwards in time."""

    offset = time.time() - time.monotonic()

    def filter(self, record):
        record.created = self.offset + time.monotonic()
        record.msecs = (record.created % 1) * 1000
        return True


def setup_logging(log_file: str | None = None, level=logging.INFO):
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for h in handlers:
        h.addFilter(SteadyTimeFilter())
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s", handlers=handlers)
    for noisy in ("urllib3", "websocket", "influxdb_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
