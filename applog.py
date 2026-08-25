"""
Central logging setup for Guardian's own operational log -- separate from
the MicroPython session logs in data/sessions/. This is where you look
when the *tool itself* misbehaves (silently stops logging, hangs, etc.)
rather than when the board does.

Writes to DATA_DIR/guardian.log (rotated so it can't grow unbounded) and
also to stdout, which `journalctl -u serial-guardian -f` picks up when
running under systemd.
"""
import logging
import logging.handlers
import os


def setup(cfg):
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    log_path = os.path.join(cfg.DATA_DIR, "guardian.log")

    logger = logging.getLogger("guardian")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger  # already configured -- avoid duplicate handlers on re-entry

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    return logger
