"""Health checker em background para monitorar proxies."""

import asyncio
import threading
import time
from typing import Optional

from src.logger import logger
from src.proxy_validator import ProxyValidator
from src.proxy_pool import ProxyPool


def _log_activity(event_type: str, message: str, proxy_id: str = "") -> None:
    """Registra atividade no log do GUI (se disponível)."""
    try:
        from src.web_server import activity_log
        activity_log.add(event_type, message, proxy_id)
    except ImportError:
        pass


class HealthChecker:
    """Monitora a saúde das proxies periodicamente em background."""

    def __init__(
        self,
        pool: ProxyPool,
        validator: ProxyValidator,
        interval: int = 60,
        max_failures: int = 3,
        enabled: bool = True,
    ):
        self.pool = pool
        self.validator = validator
        self.interval = interval
        self.max_failures = max_failures
        self.enabled = enabled
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_check = 0.0
        self._check_count = 0

    def start(self) -> None:
        """Inicia o health check em background."""
        if not self.enabled:
            logger.info("Health checker desativado")
            return

        if self._running:
            logger.warning("Health checker já está rodando")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="HealthChecker",
        )
        self._thread.start()
        logger.info(
            f"Health checker iniciado (intervalo: {self.interval}s)"
        )

    def stop(self) -> None:
        """Para o health check."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("Health checker parado")

    def _run_loop(self) -> None:
        """Loop principal do health check."""
        while self._running:
            try:
                asyncio.run(self._run_checks())
                self._last_check = time.time()
                self._check_count += 1
            except Exception as e:
                logger.error(f"Erro no health check loop: {e}")

            # Sleep em intervalos curtos para poder parar rapidamente
            for _ in range(self.interval):
                if not self._running:
                    break
                time.sleep(1)

    async def _run_checks(self) -> None:
        """Executa checks em todas as proxies (paralelo)."""
        all_proxies = [p for p in self.pool.get_all() if p.enabled]
        if not all_proxies:
            return

        logger.debug(f"Health check: testando {len(all_proxies)} proxies...")

        async def _check_one(proxy):
            try:
                result = await self.validator.validate(
                    proxy_id=proxy.id,
                    protocol=proxy.protocol,
                    host=proxy.host,
                    port=proxy.port,
                    username=proxy.username,
                    password=proxy.password,
                )

                if result.is_valid:
                    proxy.mark_checked(True, result.ip_address)
                    proxy.failures = 0
                    _log_activity("health", f"Health check OK ({result.response_time:.2f}s) - IP: {result.ip_address}", proxy.id)
                    logger.debug(
                        f"Health check OK: {proxy.id} ({result.response_time:.2f}s)"
                    )
                else:
                    proxy.last_checked = time.time()
                    proxy.failures += 1
                    if proxy.failures >= self.max_failures:
                        proxy.mark_checked(False)
                        _log_activity("health", f"Proxy morta apos {proxy.failures} falhas", proxy.id)
                        logger.warning(
                            f"Proxy {proxy.id} marcada como morta "
                            f"({proxy.failures} falhas)"
                        )
                    else:
                        logger.debug(
                            f"Health check WARN: {proxy.id} "
                            f"({proxy.failures}/{self.max_failures} falhas)"
                        )
            except Exception as e:
                logger.error(f"Erro no health check de {proxy.id}: {e}")

        # Executa todas as verificações em paralelo
        tasks = [_check_one(p) for p in all_proxies]
        await asyncio.gather(*tasks, return_exceptions=True)

    def run_once(self) -> dict:
        """Executa um único ciclo de health check (síncrono, paralelo)."""
        all_proxies = [p for p in self.pool.get_all() if p.enabled]
        results = {}

        async def _check_one(proxy):
            try:
                loop = asyncio.get_event_loop()
                result = await self.validator.validate(
                    proxy_id=proxy.id,
                    protocol=proxy.protocol,
                    host=proxy.host,
                    port=proxy.port,
                    username=proxy.username,
                    password=proxy.password,
                )
                results[proxy.id] = {
                    "valid": result.is_valid,
                    "response_time": result.response_time,
                    "ip": result.ip_address,
                    "error": result.error,
                }
                proxy.mark_checked(result.is_valid, result.ip_address)
                if result.is_valid:
                    proxy.failures = 0
                else:
                    proxy.failures += 1
                    if proxy.failures >= self.max_failures:
                        proxy.mark_checked(False)
            except Exception as e:
                results[proxy.id] = {"valid": False, "error": str(e)}

        # Executa todas as verificações em paralelo
        loop = asyncio.new_event_loop()
        try:
            tasks = [_check_one(p) for p in all_proxies]
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        finally:
            loop.close()

        return results

    def get_status(self) -> dict:
        """Retorna status do health checker."""
        return {
            "enabled": self.enabled,
            "running": self._running,
            "interval": self.interval,
            "last_check": self._last_check,
            "check_count": self._check_count,
            "max_failures": self.max_failures,
        }
