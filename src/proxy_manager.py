"""Gerenciador central do sistema de proxies rotativas."""

import asyncio
import time
import threading
from typing import Optional

from src.logger import logger
from src.utils import get_config, get_proxies_config
from src.proxy_pool import ProxyPool, ProxyEntry
from src.proxy_rotator import ProxyRotator, RotationStrategy
from src.proxy_validator import ProxyValidator


def _log_activity(event_type: str, message: str, proxy_id: str = "") -> None:
    """Registra atividade no log do GUI (se disponível)."""
    try:
        from src.web_server import activity_log
        activity_log.add(event_type, message, proxy_id)
    except ImportError:
        pass


class ProxyManager:
    """Classe central que coordena todo o sistema de proxies.
    
    Responsabilidades:
    - Carregar configurações e proxies
    - Gerenciar o pool de proxies
    - Coordenar a rotação entre proxies
    - Fornecer proxy válida para uso
    - Integrar com health checker
    """

    _instance: Optional["ProxyManager"] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs) -> "ProxyManager":
        """Singleton pattern - garante apenas uma instância."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if hasattr(self, "_initialized") and self._initialized:
            return
        self._initialized = True

        self._config: dict = {}
        self._proxies_config: dict = {}
        self._pool = ProxyPool()
        self._rotator: Optional[ProxyRotator] = None
        self._validator: Optional[ProxyValidator] = None
        self._request_lock = threading.RLock()
        self._total_requests = 0
        self._max_failures: int = 3

        logger.info("ProxyManager inicializado")

    def initialize(self) -> None:
        """Inicializa o sistema completo."""
        self._config = get_config()
        self._proxies_config = get_proxies_config()

        rotation_config = self._config.get("rotation", {})
        health_config = self._config.get("health_check", {})
        proxy_defaults = self._config.get("proxy_defaults", {})

        # Lê max_failures configurável
        self._max_failures = proxy_defaults.get("max_failures_before_dead", 3)

        # Inicializa pool de proxies
        self._pool = ProxyPool()
        self._pool.load_from_config(self._proxies_config)

        # Inicializa rotator
        self._rotator = ProxyRotator(
            pool=self._pool,
            strategy=rotation_config.get("strategy", "cycle"),
            max_rotations=rotation_config.get("max_rotations", 5),
            shuffle_on_restart=rotation_config.get("shuffle_on_restart", True),
        )

        # Inicializa validador
        self._validator = ProxyValidator(
            test_url=health_config.get("test_url", "http://httpbin.org/ip"),
            timeout=health_config.get("timeout_seconds", 10),
        )

        stats = self._pool.get_stats()
        logger.info(
            f"Sistema inicializado: {stats['rotating_alive']} proxies rotativas, "
            f"{stats['static_alive']} proxies normais"
        )

    @property
    def pool(self) -> ProxyPool:
        return self._pool

    @property
    def rotator(self) -> ProxyRotator:
        return self._rotator

    @property
    def validator(self) -> ProxyValidator:
        return self._validator

    def get_next_proxy(self) -> Optional[ProxyEntry]:
        """Retorna a próxima proxy válida para uso.
        
        Tenta pegar uma proxy rotativa. Se não houver disponíveis,
        fallback para proxy estática.
        """
        with self._request_lock:
            # Tenta proxy rotativa primeiro
            proxy = self._rotator.get_next_proxy()
            if proxy:
                logger.debug(
                    f"Proxy selecionada (rotativa): {proxy.id} "
                    f"[{proxy.current_rotations}/{proxy.rotation_count}]"
                )
                return proxy

            # Fallback para proxy estática
            static_proxies = self._pool.get_static_pool()
            if static_proxies:
                proxy = static_proxies[0]
                logger.debug(f"Proxy selecionada (estática): {proxy.id}")
                return proxy

            logger.error("Nenhuma proxy disponível!")
            return None

    def get_proxy_for_request(self) -> Optional[ProxyEntry]:
        """Retorna uma proxy para uma request. Registra uso."""
        proxy = self.get_next_proxy()
        if proxy:
            self._total_requests += 1
            proxy.last_used = time.time()
        return proxy

    def report_success(self, proxy_id: str) -> None:
        """Reporta sucesso de uma request."""
        proxy = self._pool.get_proxy(proxy_id)
        if proxy:
            proxy.record_success()
            _log_activity("request", f"Request OK via {proxy.url_masked}", proxy_id)
            logger.debug(f"Proxy {proxy_id} reportou sucesso")

    def report_failure(self, proxy_id: str) -> Optional[ProxyEntry]:
        """Reporta falha de uma request e retorna nova proxy.
        
        Se a proxy falhou, tenta trocar para outra disponível.
        """
        proxy = self._pool.get_proxy(proxy_id)
        if proxy:
            proxy.record_failure(max_failures=self._max_failures)
            _log_activity("error", f"Falha na request via {proxy.url_masked} (erros: {proxy.failures})", proxy_id)
            logger.warning(f"Proxy {proxy_id} reportou falha")

        # Tenta pegar outra proxy
        new_proxy = self._rotator.get_next_proxy(exclude=proxy_id)
        if new_proxy:
            return new_proxy

        # Fallback para estática
        static_proxies = self._pool.get_static_pool()
        if static_proxies:
            for sp in static_proxies:
                if sp.id != proxy_id:
                    return sp

        return None

    async def validate_all_proxies(self) -> list:
        """Valida todas as proxies do pool."""
        all_proxies = [
            {
                "id": p.id,
                "protocol": p.protocol,
                "host": p.host,
                "port": p.port,
                "username": p.username,
                "password": p.password,
            }
            for p in self._pool.get_all() if p.enabled
        ]
        results = await self._validator.validate_all(all_proxies)

        # Atualiza estado das proxies baseado na validação
        for result in results:
            proxy = self._pool.get_proxy(result.proxy_id)
            if proxy:
                proxy.mark_checked(result.is_valid, result.ip_address)
                if not result.is_valid:
                    proxy.failures += 1

        return results

    def get_status(self) -> dict:
        """Retorna status completo do sistema."""
        pool_stats = self._pool.get_stats()
        rotator_state = self._rotator.get_state() if self._rotator else {}
        return {
            "pool": pool_stats,
            "rotator": rotator_state,
            "total_requests_served": self._total_requests,
            "alive_proxies": [
                {
                    "id": p.id,
                    "type": p.proxy_type,
                    "protocol": p.protocol,
                    "host": p.host,
                    "port": p.port,
                    "ip": p.current_ip,
                    "requests": p.total_requests,
                    "failures": p.failures,
                    "rotations": f"{p.current_rotations}/{p.rotation_count}",
                }
                for p in self._pool.get_all_alive()
            ],
        }

    def shutdown(self) -> None:
        """Encerra o sistema graciosamente."""
        logger.info("Encerrando ProxyManager...")
        self._rotator.reset()
        logger.info("ProxyManager encerrado")
