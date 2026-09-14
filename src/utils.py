"""Utilitários gerais do ProxsR."""

import json
import tempfile
from urllib.parse import quote
import os
import time
import asyncio
from pathlib import Path
from typing import Any, Callable, Optional
from functools import wraps

from src.logger import logger


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"


def load_json(filepath: Path | str) -> dict:
    """Carrega um arquivo JSON e retorna o dicionário."""
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {filepath}")
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(filepath: Path | str, data: dict, indent: int = 2) -> None:
    """Salva dados em um arquivo JSON."""
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=filepath.parent, delete=False) as f:
            temporary = f.name
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, filepath)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def get_config() -> dict:
    """Carrega a configuração principal."""
    return load_json(CONFIG_DIR / "config.json")


def get_proxies_config() -> dict:
    """Carrega a configuração de proxies."""
    return load_json(CONFIG_DIR / "proxies.json")


def build_proxy_url(
    protocol: str,
    host: str,
    port: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> str:
    """Constrói a URL de um proxy."""
    host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if username:
        return f"{protocol}://{quote(username, safe='')}:{quote(password or '', safe='')}@{host}:{port}"
    return f"{protocol}://{host}:{port}"



