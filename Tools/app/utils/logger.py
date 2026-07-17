# app/utils/logger.py
import logging
import sys
from typing import Optional

def get_logger(name: str, level: Optional[int] = None) -> logging.Logger:
    logger = logging.getLogger(name)

    root_logger = logging.getLogger()
    if root_logger.handlers:
        logger.propagate = True
    elif not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        # simple structured-ish formatter; you can plug JSON formatter later
        fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        handler.setFormatter(logging.Formatter(fmt))
        logger.addHandler(handler)
        logger.propagate = False
    if level is not None:
        logger.setLevel(level)
    elif not logger.level:
        logger.setLevel(logging.INFO)
    return logger
