"""Sistema de logging profissional para o ProxsR."""

import logging
import logging.handlers
import os
import sys
import json
from pathlib import Path


CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _load_logging_config() -> dict:
    """Carrega configuração de logging do config.json."""
    try:
        config_path = CONFIG_DIR / "config.json"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            return config.get("logging", {})
    except Exception:
        pass
    return {}


def setup_logger(
    name: str = "proxsr",
    level: str = "INFO",
    log_file: str = "logs/proxsr.log",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """Configura e retorna um logger profissional com rotação de arquivos."""
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    # Lê configuração do config.json e usa como overrides
    log_config = _load_logging_config()
    level = log_config.get("level", level)
    log_file = log_config.get("file", log_file)
    max_bytes = log_config.get("max_bytes", max_bytes)
    backup_count = log_config.get("backup_count", backup_count)

    log_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(log_level)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s.%(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        filename=str(log_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


logger = setup_logger()
