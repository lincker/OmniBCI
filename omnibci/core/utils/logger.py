#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-28 09:49
import logging
import logging.config
from pathlib import Path


class ColoredFormatter(logging.Formatter):
    grey = "\x1b[38;20m"
    green = "\x1b[32;20m"
    yellow = "\x1b[33;20m"
    red = "\x1b[31;20m"
    bold_red = "\x1b[31;1m"
    reset = "\x1b[0m"
    format_str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    FORMATS = {
        logging.DEBUG: grey + format_str + reset,
        logging.INFO: green + format_str + reset,
        logging.WARNING: yellow + format_str + reset,
        logging.ERROR: red + format_str + reset,
        logging.CRITICAL: bold_red + format_str + reset
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt, datefmt="%H:%M:%S")
        return formatter.format(record)

def setup_logging(
    default_level=logging.INFO,
    log_dir="logs",
    config_name="omnibci"
):
    """
    init log system。
    setup at the system beginning.
    """
    root_dir = Path.cwd()
    log_path = root_dir / log_dir
    log_path.mkdir(exist_ok=True)

    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S"
            },
            "colored": {
                "()": ColoredFormatter
            }
        },
        "handlers": {
            # console for develop
            "console": {
                "level": "DEBUG",
                "class": "logging.StreamHandler",
                "formatter": "colored"
            },
            # normal logfile.
            "file_info": {
                "level": "INFO",
                "class": "logging.handlers.RotatingFileHandler",
                "filename": log_path / f"{config_name}.log",
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 5,
                "formatter": "standard",
                "encoding": "utf8"
            },
            # error file for debug
            "file_error": {
                "level": "ERROR",
                "class": "logging.handlers.RotatingFileHandler",
                "filename": log_path / "error.log",
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 5,
                "formatter": "standard",
                "encoding": "utf8"
            },
        },
        "loggers": {
            # root config
            "": {
                "handlers": ["console", "file_info", "file_error"],
                "level": default_level,
                "propagate": True
            },
            "PyQt6": {"level": "WARNING"},
            "matplotlib": {"level": "WARNING"},
            "urllib3": {"level": "WARNING"},
        }
    }

    logging.config.dictConfig(logging_config)
    logging.info(f"Logging initialized. Saving to: {log_path}")