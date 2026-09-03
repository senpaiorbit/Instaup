import json
import logging
import os
import random
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


def load_config(path="config.json"):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def setup_logger(log_file, level="INFO"):
    p = Path(log_file)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    p.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("instabot")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = RotatingFileHandler(str(p), maxBytes=1 << 20, backupCount=1)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    if os.environ.get("RENDER"):
        sh.setLevel(logging.WARNING)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


def random_delay(min_s, max_s, logger=None):
    delay = random.uniform(min_s, max_s)
    if logger:
        logger.info(f"Waiting {delay:.2f}s")
    time.sleep(delay)


def human_delay(config, logger=None):
    min_s = config.get("delay_min", 1.0)
    max_s = config.get("delay_max", 3.0)
    random_delay(min_s, max_s, logger)
