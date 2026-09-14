"""Servidor Web GUI para o ProxsR - Dashboard em tempo real."""

import asyncio
import json
import time
import threading
import re
import uuid
import copy
from urllib.parse import urlsplit
from pathlib import Path
from typing import Optional
from collections import deque

from aiohttp import web

from src.logger import logger
from src.proxy_manager import ProxyManager
from src.health_checker import HealthChecker
from src.system_proxy import system_proxy_manager
from src.utils import get_config, get_proxies_config, save_json, CONFIG_DIR
from src.proxy_rotator import RotationStrategy


WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class ActivityLog:
    """Armazena log de atividades em memória para exibição no GUI."""

    def __init__(self, max_size: int = 200):
        self._entries: deque = deque(maxlen=max_size)
        self._lock = threading.Lock()

    def add(self, event_type: str, message: str, proxy_id: str = "", details: Optional[dict] = None) -> None:
        entry = {
            "timestamp": time.time(),
            "time_str": time.strftime("%H:%M:%S"),
            "type": event_type,
            "message": message,
            "proxy_id": proxy_id,
            "details": details or {},
        }
        with self._lock:
            self._entries.appendleft(entry)

    def get_recent(self, count: int = 50) -> list[dict]:
        with self._lock:
            return list(self._entries)[:count]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


activity_log = ActivityLog()


class WebDashboard:
    """Servidor web que serve o dashboard GUI."""

    def __init__(
        self,
        manager: ProxyManager,
        health_checker: Optional[HealthChecker] = None,
        host: str = "127.0.0.1",
        port: int = 5000,
    ):
        self._started_at = time.monotonic()
        self.manager = manager
        self.health_checker = health_checker
        self.host = host
        self.port = port
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _setup_routes(self) -> None:
        """Configura as rotas do servidor web."""
        @web.middleware
        async def guard(request, handler):
            if request.method == "POST":
                origin = request.headers.get("Origin")
                if origin and urlsplit(origin).netloc != request.host:
                    return web.json_response({"ok": False, "error": "Origem não permitida"}, status=403)
                if request.content_type != "application/json":
                    return web.json_response({"ok": False, "error": "Envie JSON"}, status=415)
            try:
                return await handler(request)
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=400)
        self._app = web.Application(middlewares=[guard])

        # Rotas de páginas
        self._app.router.add_get("/", self._handle_index)
        self._app.router.add_get("/dashboard", self._handle_index)

        # Rotas de API
        self._app.router.add_get("/api/status", self._api_status)
        self._app.router.add_get("/api/proxies", self._api_proxies)
        self._app.router.add_get("/api/rotator", self._api_rotator)
        self._app.router.add_get("/api/activity", self._api_activity)
        self._app.router.add_get("/api/health", self._api_health)
        self._app.router.add_get("/api/stats", self._api_stats)

        self._app.router.add_get("/api/config", self._api_config)
        self._app.router.add_post("/api/config", self._api_save_config)
        self._app.router.add_post("/api/activity/clear", self._api_clear_activity)

        # API de ações
        self._app.router.add_post("/api/proxy/enable", self._api_enable_proxy)
        self._app.router.add_post("/api/proxy/disable", self._api_disable_proxy)
        self._app.router.add_post("/api/proxy/save", self._api_save_proxy)
        self._app.router.add_post("/api/proxy/delete", self._api_delete_proxy)
        self._app.router.add_post("/api/rotator/reset", self._api_reset_rotator)
        self._app.router.add_post("/api/rotator/force-next", self._api_force_next_proxy)
        self._app.router.add_post("/api/health/run", self._api_run_health_check)
        
        # System Proxy API
        self._app.router.add_get("/api/system-proxy/status", self._api_system_proxy_status)
        self._app.router.add_post("/api/system-proxy/toggle", self._api_system_proxy_toggle)
        self._app.router.add_get("/api/system-proxy/pac", self._api_system_proxy_pac)

        # Proxy Test API
        self._app.router.add_post("/api/proxy/test", self._api_test_proxy)
        self._app.router.add_post("/api/proxy/test-all", self._api_test_all_proxies)
        self._app.router.add_get("/api/proxy/test-results", self._api_test_results)

        # Arquivos estáticos
        static_path = WEB_DIR / "static"
        self._app.router.add_static("/static", static_path)

    async def _handle_index(self, request: web.Request) -> web.Response:
        """Serve a página principal do dashboard."""
        html_path = WEB_DIR / "templates" / "index.html"
        html_content = html_path.read_text(encoding="utf-8")
        return web.Response(text=html_content, content_type="text/html")

    async def _api_status(self, request: web.Request) -> web.Response:
        """API: retorna status geral do sistema."""
        status = self.manager.get_status()
        status["health_checker"] = self.health_checker.get_status() if self.health_checker else {}
        status["uptime"] = time.monotonic() - self._started_at
        return web.json_response(status)

    async def _api_proxies(self, request: web.Request) -> web.Response:
        """API: retorna lista detalhada de todas as proxies."""
        proxies = []
        for p in self.manager.pool.get_all():
            proxies.append({
                "id": p.id,
                "name": p.name,
                "type": p.proxy_type,
                "protocol": p.protocol,
                "host": p.host,
                "port": p.port,
                "username": p.username,
                "url_masked": p.url_masked,
                "enabled": p.enabled,
                "is_alive": p.is_alive,
                "current_rotations": p.current_rotations,
                "rotation_count": p.rotation_count,
                "total_requests": p.total_requests,
                "failures": p.failures,
                "current_ip": p.current_ip,
                "last_used": p.last_used,
                "last_checked": p.last_checked,
            })
        return web.json_response({"proxies": proxies})

    async def _api_rotator(self, request: web.Request) -> web.Response:
        """API: retorna estado do rotator."""
        state = self.manager.rotator.get_state() if self.manager.rotator else {}
        return web.json_response(state)

    async def _api_activity(self, request: web.Request) -> web.Response:
        """API: retorna log de atividades recentes."""
        count = max(0, min(200, int(request.query.get("count", "50"))))
        entries = activity_log.get_recent(count)
        return web.json_response({"entries": entries})

    async def _api_health(self, request: web.Request) -> web.Response:
        """API: retorna status do health checker."""
        if self.health_checker:
            return web.json_response(self.health_checker.get_status())
        return web.json_response({"enabled": False, "running": False})

    async def _api_stats(self, request: web.Request) -> web.Response:
        """API: retorna estatísticas gerais."""
        status = self.manager.get_status()
        pool = status["pool"]
        rotator = status.get("rotator", {})

        rotating = self.manager.pool.get_rotating_pool()
        rotating_details = []
        for p in rotating:
            rotating_details.append({
                "id": p.id,
                "name": p.name,
                "protocol": p.protocol,
                "host": p.host,
                "port": p.port,
                "current_rotations": p.current_rotations,
                "rotation_count": p.rotation_count,
                "total_requests": p.total_requests,
                "failures": p.failures,
                "is_alive": p.is_alive,
                "current_ip": p.current_ip,
                "progress": (p.current_rotations / p.rotation_count * 100) if p.rotation_count > 0 else 0,
            })

        return web.json_response({
            "pool": pool,
            "rotator": rotator,
            "rotating_details": rotating_details,
            "activity_count": len(activity_log.get_recent(1000)),
        })

    async def _api_enable_proxy(self, request: web.Request) -> web.Response:
        """API: ativa uma proxy."""
        data = await request.json()
        proxy_id = data.get("proxy_id")
        if proxy_id and not self.manager.pool.get_proxy(proxy_id):
            return web.json_response({"ok": False, "error": "Proxy não encontrada"}, status=404)
        if proxy_id:
            self._persist_change(lambda: self.manager.pool.enable_proxy(proxy_id))
            activity_log.add("action", f"Proxy {proxy_id} ativada", proxy_id)
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "proxy_id obrigatório"}, status=400)

    async def _api_disable_proxy(self, request: web.Request) -> web.Response:
        """API: desativa uma proxy."""
        data = await request.json()
        proxy_id = data.get("proxy_id")
        if proxy_id and not self.manager.pool.get_proxy(proxy_id):
            return web.json_response({"ok": False, "error": "Proxy não encontrada"}, status=404)
        if proxy_id:
            self._persist_change(lambda: self.manager.pool.disable_proxy(proxy_id))
            activity_log.add("action", f"Proxy {proxy_id} desativada", proxy_id)
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "proxy_id obrigatório"}, status=400)

    async def _api_reset_rotator(self, request: web.Request) -> web.Response:
        """API: reseta o estado do rotator."""
        if self.manager.rotator:
            self.manager.rotator.reset()
            activity_log.add("action", "Rotator resetado")
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "Rotator não disponível"}, status=500)

    async def _api_run_health_check(self, request: web.Request) -> web.Response:
        """API: executa health check manual."""
        if self.health_checker:
            activity_log.add("action", "Health check manual iniciado")
            results = await asyncio.to_thread(self.health_checker.run_once)
            activity_log.add("health", f"Health check concluído: {len(results)} proxies testadas")
            return web.json_response({"ok": True, "results": results})
        return web.json_response({"ok": False, "error": "Health checker não disponível"}, status=500)

    async def _api_save_proxy(self, request: web.Request) -> web.Response:
        """API: salva/atualiza uma proxy."""
        try:
            data = await request.json()
            edit_id = data.pop("edit_id", None)
            
            proxy_data = {
                "id": edit_id or data.get("id") or f"proxy_{uuid.uuid4().hex[:12]}",
                "name": data.get("name", "").strip(),
                "type": data.get("proxy_type", "rotating"),
                "protocol": data.get("protocol", "http"),
                "host": data.get("host"),
                "port": int(data.get("port", 8080)),
                "username": data.get("username"),
                "password": data.get("password"),
                "rotation_count": int(data.get("rotation_count", 10)),
                "enabled": data.get("enabled", True),
            }
            
            if len(proxy_data["name"]) > 80:
                raise ValueError("O nome pode ter até 80 caracteres")
            if not isinstance(proxy_data["id"], str) or not re.fullmatch(r"[\w.-]{1,100}", proxy_data["id"]):
                raise ValueError("ID inválido: use letras, números, ponto, hífen ou sublinhado")
            if proxy_data["protocol"] not in ("http", "https", "socks5") or proxy_data["type"] not in ("rotating", "static"):
                raise ValueError("Protocolo ou tipo inválido")
            if not 1 <= proxy_data["port"] <= 65535 or not 1 <= proxy_data["rotation_count"] <= 1000:
                raise ValueError("Porta ou número de requisições fora do intervalo")
            if not isinstance(proxy_data["enabled"], bool):
                raise ValueError("enabled deve ser booleano")
            if not isinstance(proxy_data["host"], str) or not re.fullmatch(r"[\w.:-]+", proxy_data["host"]):
                raise ValueError("Informe apenas o host, sem protocolo ou caminho")
            if not edit_id and self.manager.pool.get_proxy(proxy_data["id"]):
                raise ValueError("ID já cadastrado")
            if not proxy_data["host"]:
                return web.json_response({"ok": False, "error": "host obrigatório"}, status=400)
            
            if edit_id:
                # Atualiza proxy existente
                existing = self.manager.pool.get_proxy(edit_id)
                if existing:
                    if not proxy_data["password"]:
                        proxy_data["password"] = existing.password
                    self._persist_change(lambda: self.manager.pool.add_proxy(proxy_data))
                    activity_log.add("config", f"Proxy {edit_id} atualizada", edit_id)
                else:
                    return web.json_response({"ok": False, "error": "Proxy não encontrada"}, status=404)
            else:
                # Adiciona nova proxy
                self._persist_change(lambda: self.manager.pool.add_proxy(proxy_data))
                activity_log.add("config", f"Nova proxy adicionada: {proxy_data['id']}", proxy_data["id"])
            
            
            return web.json_response({"ok": True, "id": proxy_data["id"]})
        except (ValueError, TypeError) as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except Exception as e:
            logger.error(f"Erro ao salvar proxy: {e}")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _api_delete_proxy(self, request: web.Request) -> web.Response:
        """API: exclui uma proxy."""
        try:
            data = await request.json()
            proxy_id = data.get("proxy_id")
            
            if not proxy_id:
                return web.json_response({"ok": False, "error": "proxy_id obrigatório"}, status=400)
            
            proxy = self.manager.pool.get_proxy(proxy_id)
            if not proxy:
                return web.json_response({"ok": False, "error": "Proxy não encontrada"}, status=404)
            
            self._persist_change(lambda: self.manager.pool.remove_proxy(proxy_id))

            activity_log.add("config", f"Proxy {proxy_id} excluída", proxy_id)
            
            
            return web.json_response({"ok": True})
        except Exception as e:
            logger.error(f"Erro ao excluir proxy: {e}")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _api_force_next_proxy(self, request: web.Request) -> web.Response:
        """API: força rotação para próxima proxy."""
        try:
            if self.manager.rotator:
                current_id = (await request.json()).get("current")
                new_proxy = self.manager.rotator.force_next(exclude=current_id)
                if new_proxy:
                    activity_log.add("rotation", f"Rotação forçada para {new_proxy.id}", new_proxy.id)
                    return web.json_response({"ok": True, "new_proxy": new_proxy.id})
            return web.json_response({"ok": False, "error": "Não foi possível rotacionar"}, status=500)
        except Exception as e:
            logger.error(f"Erro ao forçar rotação: {e}")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    def _persist_change(self, change):
        pool = self.manager.pool
        with pool._lock:
            snapshot = (copy.deepcopy(pool._proxies), pool._rotating_pool[:], pool._static_pool[:])
            try:
                change()
                self._save_proxies_to_file()
            except Exception:
                pool._proxies, pool._rotating_pool, pool._static_pool = snapshot
                raise

    def _save_proxies_to_file(self) -> None:
        """Salva as proxies atuais no arquivo proxies.json."""
        try:
            proxies_config = get_proxies_config()
            
            # Atualiza com proxies atuais do pool
            rotating_proxies = []
            static_proxies = []
            
            for p in self.manager.pool.get_all():
                proxy_data = {
                    "id": p.id,
                "name": p.name,
                    "type": p.proxy_type,
                    "protocol": p.protocol,
                    "host": p.host,
                    "port": p.port,
                    "username": p.username,
                    "password": p.password,
                    "rotation_count": p.rotation_count,
                    "enabled": p.enabled,
                }
                if p.proxy_type == "rotating":
                    rotating_proxies.append(proxy_data)
                else:
                    static_proxies.append(proxy_data)
            
            proxies_config["rotating_proxies"] = rotating_proxies
            proxies_config["static_proxies"] = static_proxies
            
            save_json(WEB_DIR.parent / "config" / "proxies.json", proxies_config)
            logger.info("Proxies salvas no arquivo proxies.json")
        except Exception as e:
            logger.error(f"Erro ao salvar proxies no arquivo: {e}")
            raise

    async def _api_clear_activity(self, request):
        activity_log.clear()
        return web.json_response({"ok": True})

    async def _api_config(self, request):
        return web.json_response(get_config())

    async def _api_save_config(self, request):
        data = await request.json()
        config = get_config()
        section = data.get("section")
        if section == "rotation":
            strategy = RotationStrategy(data["strategy"])
            maximum = int(data["max_rotations"])
            interval = int(data["interval_seconds"])
            shuffle = data.get("shuffle_on_restart", True)
            if not 1 <= maximum <= 50 or not 10 <= interval <= 3600 or not isinstance(shuffle, bool):
                raise ValueError("Valores de rotação inválidos")
            config["rotation"].update(strategy=strategy.value, max_rotations=maximum, shuffle_on_restart=shuffle)
            config["health_check"]["interval_seconds"] = interval
            save_json(CONFIG_DIR / "config.json", config)
            with self.manager.rotator._lock:
                self.manager.rotator.strategy = strategy
                self.manager.rotator.max_rotations = maximum
                self.manager.rotator.shuffle_on_restart = shuffle
                self.manager.rotator.reset()
            if self.health_checker:
                self.health_checker.interval = interval
        elif section == "server":
            server = dict(config["server"])
            for key in ("http_port", "socks5_port", "web_port"):
                server[key] = int(data[key])
                if not 1 <= server[key] <= 65535:
                    raise ValueError("Porta inválida")
            if len({server[k] for k in ("http_port", "socks5_port", "web_port")}) != 3:
                raise ValueError("Use portas diferentes para cada serviço")
            for key in ("http_host", "socks5_host"):
                if not re.fullmatch(r"[\w.:-]+", data[key]):
                    raise ValueError("Host inválido")
                server[key] = data[key]
            config["server"] = server
            save_json(CONFIG_DIR / "config.json", config)
            # Recarrega portas no system_proxy_manager
            system_proxy_manager.reload_ports()
        else:
            raise ValueError("Seção inválida")
        activity_log.add("config", "Configuração salva: " + section)
        return web.json_response({"ok": True, "restart_required": section == "server"})

    # === System Proxy APIs ===

    async def _api_system_proxy_status(self, request: web.Request) -> web.Response:
        """API: retorna status do proxy do sistema."""
        try:
            status = await asyncio.to_thread(system_proxy_manager.get_status)
            return web.json_response({"ok": True, **status})
        except Exception as e:
            logger.error(f"Erro ao obter status do system proxy: {e}")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _api_system_proxy_toggle(self, request: web.Request) -> web.Response:
        """API: ativa/desativa proxy do sistema."""
        try:
            data = await request.json()
            enabled = data.get("enabled", True)
            
            if not isinstance(enabled, bool):
                raise ValueError("enabled deve ser booleano")
            success, method = await asyncio.to_thread(system_proxy_manager.set_system_proxy, enabled)
            
            if success:
                activity_log.add("config", f"Proxy do sistema {'ativado' if enabled else 'desativado'} via {method}")
                return web.json_response({"ok": True, "method": method})
            else:
                return web.json_response({"ok": False, "error": "Não foi possível configurar proxy do sistema"}, status=500)
        except Exception as e:
            logger.error(f"Erro ao toggle system proxy: {e}")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _api_system_proxy_pac(self, request: web.Request) -> web.Response:
        """API: retorna URL do arquivo PAC."""
        try:
            system_proxy_manager.generate_pac_file()
            return web.Response(text=system_proxy_manager._pac_file_path.read_text(),
                                content_type="application/x-ns-proxy-autoconfig",
                                headers={"Content-Disposition": 'attachment; filename="proxsr.pac"'})
        except Exception as e:
            logger.error(f"Erro ao gerar PAC: {e}")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    # === Proxy Test API ===

    _test_results: dict = {}
    _test_lock = threading.Lock()

    async def _api_test_proxy(self, request: web.Request) -> web.Response:
        """API: testa uma proxy específica (por ID ou dados customizados)."""
        try:
            data = await request.json()
            proxy_id = data.get("proxy_id")

            if proxy_id:
                proxy = self.manager.pool.get_proxy(proxy_id)
                if not proxy:
                    return web.json_response({"ok": False, "error": "Proxy não encontrada"}, status=404)
                proxy_data = {
                    "id": proxy.id,
                    "protocol": proxy.protocol,
                    "host": proxy.host,
                    "port": proxy.port,
                    "username": proxy.username,
                    "password": proxy.password,
                }
            else:
                proxy_data = {
                    "id": data.get("id", "custom_test"),
                    "protocol": data.get("protocol", "http"),
                    "host": data.get("host", ""),
                    "port": int(data.get("port", 80)),
                    "username": data.get("username"),
                    "password": data.get("password"),
                }
                if not proxy_data["host"]:
                    return web.json_response({"ok": False, "error": "host obrigatório"}, status=400)

            activity_log.add("test", f"Iniciando teste: {proxy_data['id']}", proxy_data["id"])
            result = await self._run_proxy_test(proxy_data)

            with self._test_lock:
                self._test_results[proxy_data["id"]] = result

            activity_log.add("test", f"Teste concluído: {proxy_data['id']} - {'OK' if result['ok'] else 'FALHA'}",
                             proxy_data["id"], details=result)
            return web.json_response({"ok": True, "result": result})
        except Exception as e:
            logger.error(f"Erro ao testar proxy: {e}")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _api_test_all_proxies(self, request: web.Request) -> web.Response:
        """API: testa todas as proxies em paralelo."""
        try:
            all_proxies = self.manager.pool.get_all()
            if not all_proxies:
                return web.json_response({"ok": True, "results": []})

            activity_log.add("test", f"Iniciando teste de {len(all_proxies)} proxies...")

            async def _test_one(proxy):
                proxy_data = {
                    "id": proxy.id,
                    "protocol": proxy.protocol,
                    "host": proxy.host,
                    "port": proxy.port,
                    "username": proxy.username,
                    "password": proxy.password,
                }
                return await self._run_proxy_test(proxy_data)

            tasks = [_test_one(p) for p in all_proxies]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            final_results = []
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    final_results.append({
                        "id": all_proxies[i].id,
                        "ok": False,
                        "error": str(r),
                        "response_time": 0,
                    })
                else:
                    final_results.append(r)
                    # Atualiza IP no pool se sucesso
                    if r.get("ok") and r.get("ip"):
                        proxy = self.manager.pool.get_proxy(r["id"])
                        if proxy:
                            proxy.mark_checked(True, r["ip"])

            with self._test_lock:
                for r in final_results:
                    self._test_results[r["id"]] = r

            ok_count = sum(1 for r in final_results if r.get("ok"))
            activity_log.add("test", f"Teste concluído: {ok_count}/{len(final_results)} proxies funcionando")
            return web.json_response({"ok": True, "results": final_results})
        except Exception as e:
            logger.error(f"Erro ao testar todas as proxies: {e}")
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def _api_test_results(self, request: web.Request) -> web.Response:
        """API: retorna resultados de testes anteriores."""
        with self._test_lock:
            return web.json_response({"ok": True, "results": dict(self._test_results)})

    async def _run_proxy_test(self, proxy_data: dict) -> dict:
        """Executa teste completo de um proxy: conexão + IP + velocidade."""
        import aiohttp
        from aiohttp_socks import ProxyConnector
        from src.utils import build_proxy_url

        proxy_id = proxy_data["id"]
        protocol = proxy_data["protocol"]
        host = proxy_data["host"]
        port = proxy_data["port"]
        username = proxy_data.get("username")
        password = proxy_data.get("password")

        result = {
            "id": proxy_id,
            "protocol": protocol,
            "host": host,
            "port": port,
            "ok": False,
            "ip": None,
            "response_time": 0,
            "error": None,
            "tests": {},
            "timestamp": time.time(),
        }

        test_urls = [
            ("connect", "http://httpbin.org/ip"),
            ("ip_check", "https://api.ipify.org?format=json"),
            ("https", "https://httpbin.org/get"),
        ]

        proxy_url = build_proxy_url(protocol, host, port, username, password)

        # Teste 1: Conexão básica + IP check
        start = time.time()
        try:
            if protocol == "socks5":
                connector = ProxyConnector.from_url(proxy_url)
            else:
                connector = None

            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                proxy_kwargs = {}
                if protocol in ("http", "https"):
                    proxy_kwargs["proxy"] = proxy_url

                async with session.get("http://httpbin.org/ip", **proxy_kwargs) as resp:
                    elapsed = time.time() - start
                    if resp.status == 200:
                        body = await resp.json()
                        ip = body.get("origin", "unknown")
                        result["tests"]["connect"] = {"ok": True, "time": round(elapsed, 3), "ip": ip}
                        result["ip"] = ip
                        result["response_time"] = round(elapsed, 3)
                    else:
                        result["tests"]["connect"] = {"ok": False, "time": round(elapsed, 3), "error": f"HTTP {resp.status}"}
        except asyncio.TimeoutError:
            result["tests"]["connect"] = {"ok": False, "time": round(time.time() - start, 3), "error": "Timeout"}
        except Exception as e:
            result["tests"]["connect"] = {"ok": False, "time": round(time.time() - start, 3), "error": str(e)[:200]}

        # Teste 2: IP check via HTTPS (ipify)
        start2 = time.time()
        try:
            if protocol == "socks5":
                connector2 = ProxyConnector.from_url(proxy_url)
            else:
                connector2 = None

            timeout2 = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout2, connector=connector2) as session:
                proxy_kwargs2 = {}
                if protocol in ("http", "https"):
                    proxy_kwargs2["proxy"] = proxy_url

                async with session.get("https://api.ipify.org?format=json", **proxy_kwargs2) as resp:
                    elapsed2 = time.time() - start2
                    if resp.status == 200:
                        body2 = await resp.json()
                        ip2 = body2.get("ip", "unknown")
                        result["tests"]["ip_check"] = {"ok": True, "time": round(elapsed2, 3), "ip": ip2}
                        if not result["ip"]:
                            result["ip"] = ip2
                    else:
                        result["tests"]["ip_check"] = {"ok": False, "time": round(elapsed2, 3), "error": f"HTTP {resp.status}"}
        except asyncio.TimeoutError:
            result["tests"]["ip_check"] = {"ok": False, "time": round(time.time() - start2, 3), "error": "Timeout"}
        except Exception as e:
            result["tests"]["ip_check"] = {"ok": False, "time": round(time.time() - start2, 3), "error": str(e)[:200]}

        # Teste 3: HTTPS GET completo
        start3 = time.time()
        try:
            if protocol == "socks5":
                connector3 = ProxyConnector.from_url(proxy_url)
            else:
                connector3 = None

            timeout3 = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout3, connector=connector3) as session:
                proxy_kwargs3 = {}
                if protocol in ("http", "https"):
                    proxy_kwargs3["proxy"] = proxy_url

                async with session.get("https://httpbin.org/get", **proxy_kwargs3) as resp:
                    elapsed3 = time.time() - start3
                    if resp.status == 200:
                        result["tests"]["https"] = {"ok": True, "time": round(elapsed3, 3)}
                    else:
                        result["tests"]["https"] = {"ok": False, "time": round(elapsed3, 3), "error": f"HTTP {resp.status}"}
        except asyncio.TimeoutError:
            result["tests"]["https"] = {"ok": False, "time": round(time.time() - start3, 3), "error": "Timeout"}
        except Exception as e:
            result["tests"]["https"] = {"ok": False, "time": round(time.time() - start3, 3), "error": str(e)[:200]}

        # Resultado final
        any_ok = any(t.get("ok") for t in result["tests"].values())
        result["ok"] = any_ok
        if not any_ok and not result["error"]:
            errors = [t.get("error", "") for t in result["tests"].values() if not t.get("ok")]
            result["error"] = errors[0] if errors else "Todos os testes falharam"

        return result

    async def start_async(self) -> None:
        """Inicia o servidor web async."""
        self._setup_routes()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(f"Dashboard web rodando em http://{self.host}:{self.port}")

    async def stop_async(self) -> None:
        """Para o servidor web."""
        if self._runner:
            await self._runner.cleanup()
            logger.info("Dashboard web parado")

    def start(self) -> None:
        ready = threading.Event()
        errors = []
        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self.start_async())
            except Exception as exc:
                errors.append(exc)
                self._loop.close()
                ready.set()
                return
            ready.set()
            try:
                self._loop.run_forever()
            finally:
                self._loop.close()
        self._thread = threading.Thread(target=run, daemon=True, name="WebDashboard")
        self._thread.start()
        if not ready.wait(10):
            raise TimeoutError("Tempo esgotado ao iniciar dashboard")
        if errors:
            raise errors[0]

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self.stop_async(), self._loop).result(timeout=5)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
