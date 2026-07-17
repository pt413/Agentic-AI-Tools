"""Logging setup for deduplication"""
import sys
import logging
import logging.handlers
from app.dedupe_config import CONFIG

def setup_dedupe_logger(name: str, log_file: str = None):
    log_file = log_file or CONFIG.LOG_FILE
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    
    if logger.handlers:
        return logger

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10_000_000, backupCount=10
    )
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(funcName)s: %(message)s"
    )
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    return logger

log = setup_dedupe_logger("dedupe_v7", CONFIG.LOG_FILE)