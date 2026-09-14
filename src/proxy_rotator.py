"""Estratégias de rotação de proxies."""

import random
import threading
from enum import Enum
from typing import Optional

from src.logger import logger
from src.proxy_pool import ProxyPool, ProxyEntry


def _log_activity(event_type: str, message: str, proxy_id: str = "") -> None:
    """Registra atividade no log do GUI (se disponível)."""
    try:
        from src.web_server import activity_log
        activity_log.add(event_type, message, proxy_id)
    except ImportError:
        pass


class RotationStrategy(Enum):
    """Estratégias disponíveis de rotação."""
    ROUND_ROBIN = "round_robin"
    RANDOM = "random"
    CYCLE = "cycle"
    LEAST_USED = "least_used"


class ProxyRotator:
    """Gerencia a rotação entre proxies rotativas com ciclo controlado."""

    def __init__(
        self,
        pool: ProxyPool,
        strategy: str = "cycle",
        max_rotations: int = 5,
        shuffle_on_restart: bool = True,
    ):
        self.pool = pool
        self.strategy = RotationStrategy(strategy)
        self.max_rotations = max_rotations
        self.shuffle_on_restart = shuffle_on_restart
        self._lock = threading.RLock()

        # Estado do ciclo
        self._current_rotations = 0
        self._round_robin_index = 0
        self._cycle_order: list[str] = []
        self._cycle_index = 0
        self._last_proxy_id: Optional[str] = None
        self._pending_proxy_id: Optional[str] = None

    def _build_cycle_order(self) -> list[str]:
        """Constrói a ordem do ciclo com proxies rotativas vivas."""
        rotating = self.pool.get_rotating_pool()
        if not rotating:
            return []
        ids = [p.id for p in rotating]
        if self.shuffle_on_restart:
            random.shuffle(ids)
        return ids

    def get_next_proxy(self, exclude: Optional[str] = None) -> Optional[ProxyEntry]:
        """Retorna a próxima proxy de acordo com a estratégia."""
        with self._lock:
            if self._pending_proxy_id:
                pending = self.pool.get_proxy(self._pending_proxy_id)
                self._pending_proxy_id = None
                if pending and pending.enabled and pending.is_alive and pending.id != exclude:
                    if self.strategy == RotationStrategy.CYCLE:
                        pending.increment_rotation()
                    self._last_proxy_id = pending.id
                    return pending
            if self.strategy == RotationStrategy.ROUND_ROBIN:
                return self._get_round_robin(exclude)
            elif self.strategy == RotationStrategy.RANDOM:
                return self._get_random(exclude)
            elif self.strategy == RotationStrategy.CYCLE:
                return self._get_cycle(exclude)
            elif self.strategy == RotationStrategy.LEAST_USED:
                return self._get_least_used(exclude)
            else:
                return self._get_cycle(exclude)

    def force_next(self, exclude=None):
        """Prepara a próxima requisição sem contabilizar uso fictício."""
        with self._lock:
            proxy = self.get_next_proxy(exclude=exclude or self._last_proxy_id)
            if proxy:
                if self.strategy == RotationStrategy.CYCLE:
                    proxy.current_rotations = max(0, proxy.current_rotations - 1)
                self._pending_proxy_id = proxy.id
            return proxy

    def _get_round_robin(self, exclude: Optional[str] = None) -> Optional[ProxyEntry]:
        """Estratégia round-robin: seleciona em ordem sequencial."""
        rotating = self.pool.get_rotating_pool()
        if not rotating:
            return None

        candidates = [p for p in rotating if p.id != exclude] if exclude else rotating
        if not candidates:
            return None

        self._round_robin_index = self._round_robin_index % len(candidates)
        proxy = candidates[self._round_robin_index]
        self._round_robin_index += 1
        self._last_proxy_id = proxy.id
        return proxy

    def _get_random(self, exclude: Optional[str] = None) -> Optional[ProxyEntry]:
        """Estratégia aleatória: seleciona qualquer proxy viva."""
        proxy = self.pool.get_random_alive_rotating(exclude=exclude)
        if proxy:
            self._last_proxy_id = proxy.id
        return proxy

    def _get_cycle(self, exclude: Optional[str] = None) -> Optional[ProxyEntry]:
        alive = {p.id: p for p in self.pool.get_rotating_pool()}
        if not any(pid != exclude for pid in alive):
            return None
        if set(self._cycle_order) != set(alive):
            self._cycle_order = self._build_cycle_order()
            self._cycle_index = 0
        for _ in range(2 * len(self._cycle_order) + 1):
            self._cycle_index %= len(self._cycle_order)
            proxy = alive[self._cycle_order[self._cycle_index]]
            if proxy.id == exclude:
                self._cycle_index += 1
                continue
            if proxy.current_rotations >= proxy.rotation_count:
                proxy.reset_rotations()
                self._cycle_index += 1
                self._current_rotations = (self._current_rotations + 1) % self.max_rotations
                if self._cycle_index >= len(self._cycle_order) and self._current_rotations == 0:
                    self._cycle_order = self._build_cycle_order()
                continue
            proxy.increment_rotation()
            self._last_proxy_id = proxy.id
            return proxy
        return None

    def _get_least_used(self, exclude: Optional[str] = None) -> Optional[ProxyEntry]:
        """Estratégia least-used: seleciona a proxy com menos requests."""
        rotating = self.pool.get_rotating_pool()
        if not rotating:
            return None

        candidates = [p for p in rotating if p.id != exclude] if exclude else rotating
        if not candidates:
            return None

        proxy = min(candidates, key=lambda p: p.total_requests)
        self._last_proxy_id = proxy.id
        return proxy

    def notify_rotation(self, proxy_id: str) -> Optional[ProxyEntry]:
        """Notifica que uma proxy completou sua rotação e precisa trocar.
        
        Retorna a próxima proxy a ser usada.
        """
        with self._lock:
            proxy = self.pool.get_proxy(proxy_id)
            if proxy:
                if proxy.increment_rotation():
                    logger.info(
                        f"Proxy {proxy_id} atingiu rotation_count. "
                        f"Trocando para próxima..."
                    )
                    proxy.reset_rotations()
                    self._current_rotations += 1
                    self._cycle_index += 1

            return self.get_next_proxy(exclude=proxy_id)

    def get_state(self) -> dict:
        """Retorna o estado atual da rotação."""
        with self._lock:
            return {
                "strategy": self.strategy.value,
                "shuffle_on_restart": self.shuffle_on_restart,
                "current_rotations": self._current_rotations,
                "max_rotations": self.max_rotations,
                "cycle_index": self._cycle_index,
                "cycle_order": self._cycle_order,
                "last_proxy_id": self._last_proxy_id,
            }

    def reset(self) -> None:
        """Reseta o estado da rotação."""
        with self._lock:
            self._current_rotations = 0
            self._round_robin_index = 0
            self._cycle_order = []
            self._cycle_index = 0
            self._last_proxy_id = None
            self._pending_proxy_id = None
            for proxy in self.pool.get_all():
                proxy.reset_rotations()
            logger.info("Estado da rotação resetado")
