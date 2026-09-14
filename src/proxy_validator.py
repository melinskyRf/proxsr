"""Validador e testador de proxies HTTP/SOCKS5."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from aiohttp_socks import ProxyConnector

from src.logger import logger
from src.utils import build_proxy_url


@dataclass
class ProxyValidationResult:
    """Resultado da validação de um proxy."""
    proxy_id: str
    is_valid: bool
    response_time: float = 0.0
    ip_address: Optional[str] = None
    error: Optional[str] = None
    validated_at: float = field(default_factory=time.time)

    @property
    def proxy_url_masked(self) -> str:
        return f"[{self.proxy_id}]"


class ProxyValidator:
    """Valida se proxies estão funcionais."""

    def __init__(
        self,
        test_url: str = "http://httpbin.org/ip",
        timeout: int = 10,
    ):
        self.test_url = test_url
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def validate(
        self,
        proxy_id: str,
        protocol: str,
        host: str,
        port: int,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> ProxyValidationResult:
        """Valida um proxy fazendo uma request de teste."""
        proxy_url = build_proxy_url(protocol, host, port, username, password)
        start_time = time.time()

        try:
            if protocol == "socks5":
                connector = ProxyConnector.from_url(proxy_url)
            else:
                connector = None

            async with aiohttp.ClientSession(
                timeout=self.timeout,
                connector=connector,
            ) as session:
                proxy_kwargs = {}
                if protocol in ("http", "https"):
                    proxy_kwargs["proxy"] = proxy_url

                async with session.get(
                    self.test_url,
                    **proxy_kwargs,
                ) as response:
                    elapsed = time.time() - start_time
                    if response.status == 200:
                        data = await response.json()
                        ip = data.get("origin", "unknown")
                        logger.debug(
                            f"Proxy {proxy_id} válida em {elapsed:.2f}s - IP: {ip}"
                        )
                        return ProxyValidationResult(
                            proxy_id=proxy_id,
                            is_valid=True,
                            response_time=elapsed,
                            ip_address=ip,
                        )
                    else:
                        return ProxyValidationResult(
                            proxy_id=proxy_id,
                            is_valid=False,
                            response_time=elapsed,
                            error=f"HTTP {response.status}",
                        )

        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            logger.warning(f"Proxy {proxy_id} timeout após {elapsed:.2f}s")
            return ProxyValidationResult(
                proxy_id=proxy_id,
                is_valid=False,
                response_time=elapsed,
                error="Timeout",
            )
        except Exception as e:
            elapsed = time.time() - start_time
            logger.warning(f"Proxy {proxy_id} falhou na validação: {e}")
            return ProxyValidationResult(
                proxy_id=proxy_id,
                is_valid=False,
                response_time=elapsed,
                error=str(e),
            )

    async def validate_all(
        self,
        proxies: list[dict],
    ) -> list[ProxyValidationResult]:
        """Valida múltiplos proxies em paralelo."""
        tasks = [
            self.validate(
                proxy_id=p["id"],
                protocol=p["protocol"],
                host=p["host"],
                port=p["port"],
                username=p.get("username"),
                password=p.get("password"),
            )
            for p in proxies
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        validated = []
        for r in results:
            if isinstance(r, Exception):
                logger.error(f"Exceção na validação em lote: {r}")
            else:
                validated.append(r)
        return validated
