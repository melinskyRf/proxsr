"""Pool thread-safe de proxies rotativas e normais."""

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from src.logger import logger


@dataclass
class ProxyEntry:
    """Representa uma proxy no pool."""
    id: str
    proxy_type: str  # 'rotating' ou 'static'
    protocol: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    rotation_count: int = 10
    enabled: bool = True

    name: str = ""

    # Estado em tempo de execução
    current_rotations: int = 0
    total_requests: int = 0
    failures: int = 0
    is_alive: bool = True
    last_used: float = field(default_factory=time.time)
    last_checked: float = 0.0
    current_ip: Optional[str] = None

    @property
    def url(self) -> str:
        from src.utils import build_proxy_url
        return build_proxy_url(self.protocol, self.host, self.port, self.username, self.password)

    @property
    def url_masked(self) -> str:
        if self.username:
            return f"{self.protocol}://{self.username}:***@{self.host}:{self.port}"
        return f"{self.protocol}://{self.host}:{self.port}"

    def increment_rotation(self) -> bool:
        """Incrementa o contador de rotação. Retorna True se atingiu o limite."""
        self.current_rotations += 1
        self.last_used = time.time()
        return self.current_rotations >= self.rotation_count

    def reset_rotations(self) -> None:
        """Reseta o contador de rotações para 0."""
        self.current_rotations = 0

    def record_success(self) -> None:
        """Registra uma request bem-sucedida."""
        self.total_requests += 1
        self.failures = 0
        self.is_alive = True

    def record_failure(self, max_failures: int = 3) -> None:
        """Registra uma falha. Usa max_failures configurável."""
        self.failures += 1
        self.total_requests += 1
        if self.failures >= max_failures:
            self.is_alive = False
            logger.warning(f"Proxy {self.id} marcada como morta após {self.failures} falhas")

    def mark_checked(self, alive: bool, ip: Optional[str] = None) -> None:
        """Marca o resultado do health check."""
        self.last_checked = time.time()
        self.is_alive = alive
        if ip:
            self.current_ip = ip


class ProxyPool:
    """Pool thread-safe de proxies com gerenciamento de estado."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proxies: dict[str, ProxyEntry] = {}
        self._rotating_pool: list[str] = []
        self._static_pool: list[str] = []

    def add_proxy(self, proxy_data: dict) -> ProxyEntry:
        """Adiciona uma proxy ao pool."""
        with self._lock:
            self._rotating_pool[:] = [pid for pid in self._rotating_pool if pid != proxy_data["id"]]
            self._static_pool[:] = [pid for pid in self._static_pool if pid != proxy_data["id"]]
            entry = ProxyEntry(
                id=proxy_data["id"],
                name=proxy_data.get("name", ""),
                proxy_type=proxy_data.get("type", "rotating"),
                protocol=proxy_data["protocol"],
                host=proxy_data["host"],
                port=proxy_data["port"],
                username=proxy_data.get("username"),
                password=proxy_data.get("password"),
                rotation_count=proxy_data.get("rotation_count", 10),
                enabled=proxy_data.get("enabled", True),
            )
            self._proxies[entry.id] = entry

            if entry.proxy_type == "rotating":
                self._rotating_pool.append(entry.id)
            elif entry.proxy_type == "static":
                self._static_pool.append(entry.id)

            logger.debug(f"Proxy adicionada ao pool: {entry.id} ({entry.proxy_type})")
            return entry

    def get_all(self) -> list[ProxyEntry]:
        with self._lock:
            return list(self._proxies.values())

    def remove_proxy(self, proxy_id: str) -> None:
        with self._lock:
            self._proxies.pop(proxy_id, None)
            self._rotating_pool[:] = [pid for pid in self._rotating_pool if pid != proxy_id]
            self._static_pool[:] = [pid for pid in self._static_pool if pid != proxy_id]

    def load_from_config(self, proxies_config: dict) -> int:
        """Carrega proxies a partir do arquivo de configuração."""
        count = 0
        for proxy_data in proxies_config.get("rotating_proxies", []):
            self.add_proxy({**proxy_data, "type": "rotating"})
            count += 1
        for proxy_data in proxies_config.get("static_proxies", []):
            self.add_proxy({**proxy_data, "type": "static"})
            count += 1
        logger.info(f"Carregadas {count} proxies no pool")
        return count

    def get_proxy(self, proxy_id: str) -> Optional[ProxyEntry]:
        """Retorna uma proxy específica por ID."""
        with self._lock:
            return self._proxies.get(proxy_id)

    def get_rotating_pool(self) -> list[ProxyEntry]:
        """Retorna cópia da lista de proxies rotativas ativas."""
        with self._lock:
            return [
                self._proxies[pid]
                for pid in self._rotating_pool
                if pid in self._proxies and self._proxies[pid].enabled and self._proxies[pid].is_alive
            ]

    def get_static_pool(self) -> list[ProxyEntry]:
        """Retorna cópia da lista de proxies normais ativas."""
        with self._lock:
            return [
                self._proxies[pid]
                for pid in self._static_pool
                if pid in self._proxies and self._proxies[pid].enabled and self._proxies[pid].is_alive
            ]

    def get_all_alive(self) -> list[ProxyEntry]:
        """Retorna todas as proxies ativas (rotativas + normais)."""
        with self._lock:
            return [p for p in self._proxies.values() if p.enabled and p.is_alive]

    def get_random_alive_rotating(self, exclude: Optional[str] = None) -> Optional[ProxyEntry]:
        """Retorna uma proxy rotativa aleatória, excluindo uma específica."""
        import random
        with self._lock:
            candidates = [
                self._proxies[pid]
                for pid in self._rotating_pool
                if (
                    pid in self._proxies
                    and self._proxies[pid].enabled
                    and self._proxies[pid].is_alive
                    and pid != exclude
                )
            ]
            if candidates:
                return random.choice(candidates)
        return None

    def disable_proxy(self, proxy_id: str) -> None:
        """Desativa uma proxy."""
        with self._lock:
            if proxy_id in self._proxies:
                self._proxies[proxy_id].enabled = False
                logger.info(f"Proxy {proxy_id} desativada")

    def enable_proxy(self, proxy_id: str) -> None:
        """Ativa uma proxy."""
        with self._lock:
            if proxy_id in self._proxies:
                self._proxies[proxy_id].enabled = True
                if self._proxies[proxy_id].proxy_type == "rotating":
                    if proxy_id not in self._rotating_pool:
                        self._rotating_pool.append(proxy_id)
                elif self._proxies[proxy_id].proxy_type == "static":
                    if proxy_id not in self._static_pool:
                        self._static_pool.append(proxy_id)
                logger.info(f"Proxy {proxy_id} ativada")

    def get_stats(self) -> dict:
        """Retorna estatísticas do pool."""
        with self._lock:
            rotating_alive = sum(
                1 for pid in self._rotating_pool
                if pid in self._proxies and self._proxies[pid].enabled and self._proxies[pid].is_alive
            )
            static_alive = sum(
                1 for pid in self._static_pool
                if pid in self._proxies and self._proxies[pid].enabled and self._proxies[pid].is_alive
            )
            total_requests = sum(p.total_requests for p in self._proxies.values())
            return {
                "total_proxies": len(self._proxies),
                "rotating_total": len(self._rotating_pool),
                "rotating_alive": rotating_alive,
                "static_total": len(self._static_pool),
                "static_alive": static_alive,
                "total_requests": total_requests,
            }
