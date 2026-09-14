#!/usr/bin/env python3
"""ProxsR - Sistema Profissional de Proxies Rotativas.

Entry point principal do sistema. Fornece:
- Servidor proxy local HTTP (localhost:8080)
- Servidor proxy local SOCKS5 (localhost:1080)
- Dashboard web GUI (localhost:5000)
- Health check automático em background
- Rotação inteligente de proxies
"""

import sys
import signal
import argparse
import time
import asyncio
import webbrowser
import threading
from pathlib import Path

# Adiciona o diretório raiz ao path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.logger import logger
from src.proxy_manager import ProxyManager
from src.health_checker import HealthChecker
from src.server import ProxyServer, SOCKS5Server
from src.web_server import WebDashboard, activity_log
from src.utils import get_config, get_proxies_config


def print_banner(with_gui: bool = False) -> None:
    """Exibe o banner do ProxsR."""
    gui_line = "║  Dashboard   : http://127.0.0.1:5000       ║" if with_gui else ""
    banner = f"""
╔══════════════════════════════════════════════════╗
║             ProxsR - Proxy Rotation System       ║
║          Sistema Profissional de Proxies         ║
╠══════════════════════════════════════════════════╣
║  HTTP Proxy  : 127.0.0.1:8080                   ║
║  SOCKS5 Proxy: 127.0.0.1:1080                   ║
{gui_line}
║  Health Check: Ativo                             ║
╚══════════════════════════════════════════════════╝
"""
    print(banner)


def _open_browser_delayed(url: str, delay: float = 1.5) -> None:
    """Abre o navegador após um delay para o servidor iniciar."""
    def _open():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    t = threading.Thread(target=_open, daemon=True)
    t.start()


def cmd_start(args) -> None:
    """Comando: inicia o servidor de proxies + GUI."""
    with_gui = not args.no_gui
    print_banner(with_gui=with_gui)

    config = get_config()
    server_config = config.get("server", {})
    health_config = config.get("health_check", {})

    http_host = args.host or server_config.get("http_host", "127.0.0.1")
    http_port = args.port or server_config.get("http_port", 8080)
    socks5_host = args.socks5_host or server_config.get("socks5_host", "127.0.0.1")
    socks5_port = args.socks5_port or server_config.get("socks5_port", 1080)
    web_port = args.web_port or server_config.get("web_port", 5000)

    from src.system_proxy import system_proxy_manager
    system_proxy_manager.http_host = http_host
    system_proxy_manager.http_port = http_port
    system_proxy_manager.socks5_host = socks5_host
    system_proxy_manager.socks5_port = socks5_port

    # Inicializa o gerenciador de proxies
    manager = ProxyManager()
    manager.initialize()

    # Health checker
    health_checker = HealthChecker(
        pool=manager.pool,
        validator=manager.validator,
        interval=health_config.get("interval_seconds", 60),
        max_failures=health_config.get("max_failures", 3),
        enabled=health_config.get("enabled", True),
    )

    # Servidores de proxy
    http_server = ProxyServer(manager, http_host, http_port)
    socks5_server = SOCKS5Server(manager, socks5_host, socks5_port)

    # Dashboard web
    web_dashboard = None
    if with_gui:
        web_dashboard = WebDashboard(
            manager=manager,
            health_checker=health_checker,
            host="127.0.0.1",
            port=web_port,
        )

    # Graceful shutdown
    def shutdown_handler(signum, frame):
        logger.info("Sinal de shutdown recebido...")
        print("\n[*] Encerrando servidores...")
        health_checker.stop()
        http_server.stop()
        socks5_server.stop()
        if web_dashboard:
            web_dashboard.stop()
        manager.shutdown()
        # Restaura proxy do sistema ao encerrar
        from src.system_proxy import system_proxy_manager
        system_proxy_manager.restore()
        print("[*] ProxsR encerrado.")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Valida proxies
    if args.validate:
        print("[*] Validando proxies...")
        results = asyncio.run(manager.validate_all_proxies())
        for result in results:
            print(f"  [{'OK' if result.is_valid else 'FALHOU'}] {result.proxy_id}")

    # Inicia health checker
    health_checker.start()

    # Inicia servidores
    http_server.start()
    socks5_server.start()

    print(f"\n[+] Servidor HTTP proxy:   {http_host}:{http_port}")
    print(f"[+] Servidor SOCKS5 proxy: {socks5_host}:{socks5_port}")
    print(f"[+] Health check: {'ativo' if health_config.get('enabled', True) else 'inativo'}")

    # Inicia dashboard web
    if web_dashboard:
        web_dashboard.start()
        web_url = f"http://127.0.0.1:{web_port}"
        print(f"[+] Dashboard GUI:         {web_url}")
        if not args.no_browser:
            _open_browser_delayed(web_url)

    print(f"[+] Pressione Ctrl+C para encerrar\n")

    # Mantém o programa rodando
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown_handler(None, None)


def cmd_status(args) -> None:
    """Comando: exibe status do sistema."""
    manager = ProxyManager()
    manager.initialize()

    status = manager.get_status()

    print("\n╔══════════════════════════════════════════╗")
    print("║        Status do ProxsR                  ║")
    print("╚══════════════════════════════════════════╝\n")

    pool = status["pool"]
    print(f"  Total de proxies:     {pool['total_proxies']}")
    print(f"  Rotativas (ativas):   {pool['rotating_alive']}/{pool['rotating_total']}")
    print(f"  Normais (ativas):     {pool['static_alive']}/{pool['static_total']}")
    print(f"  Total de requests:    {pool['total_requests']}")
    print()

    rotator = status.get("rotator", {})
    if rotator:
        print(f"  Estratégia:           {rotator.get('strategy', 'N/A')}")
        print(f"  Rotações:             {rotator.get('current_rotations', 0)}/{rotator.get('max_rotations', 5)}")
        print(f"  Índice do ciclo:      {rotator.get('cycle_index', 0)}")
        print()

    proxies = status.get("alive_proxies", [])
    if proxies:
        print("  Proxies ativas:")
        for p in proxies:
            print(f"    [{p['id']}] {p['type']} {p['protocol']}://{p['host']}:{p['port']}")
            print(f"      IP: {p.get('ip', 'N/A')} | Requests: {p['requests']} | Falhas: {p['failures']} | Rotações: {p['rotations']}")
    else:
        print("  Nenhuma proxy ativa")
    print()


def cmd_validate(args) -> None:
    """Comando: valida todas as proxies."""
    manager = ProxyManager()
    manager.initialize()

    print("\n[*] Validando todas as proxies...\n")

    import asyncio
    results = asyncio.run(manager.validate_all_proxies())

    for result in results:
        status = "✓ OK" if result.is_valid else "✗ FALHOU"
        ip = result.ip_address or "N/A"
        print(f"  {status} | {result.proxy_id} | IP: {ip} | Tempo: {result.response_time:.2f}s")
        if result.error:
            print(f"    Erro: {result.error}")

    valid = sum(1 for r in results if r.is_valid)
    print(f"\n  Resultado: {valid}/{len(results)} proxies válidas\n")


def cmd_config(args) -> None:
    """Comando: exibe configuração atual."""
    config = get_config()
    proxies = get_proxies_config()

    print("\n╔══════════════════════════════════════════╗")
    print("║        Configuração ProxsR               ║")
    print("╚══════════════════════════════════════════╝\n")

    print("  Servidor:")
    server = config.get("server", {})
    print(f"    HTTP:  {server.get('http_host', '127.0.0.1')}:{server.get('http_port', 8080)}")
    print(f"    SOCKS5: {server.get('socks5_host', '127.0.0.1')}:{server.get('socks5_port', 1080)}")
    print()

    print("  Rotação:")
    rotation = config.get("rotation", {})
    print(f"    Estratégia:           {rotation.get('strategy', 'cycle')}")
    print(f"    Máx. rotações:        {rotation.get('max_rotations', 5)}")
    print(f"    Shuffle no restart:   {rotation.get('shuffle_on_restart', True)}")
    print()

    rotating = proxies.get("rotating_proxies", [])
    static = proxies.get("static_proxies", [])

    print(f"  Proxies rotativas: {len(rotating)}")
    for p in rotating:
        print(f"    [{p['id']}] {p['protocol']}://{p['host']}:{p['port']} (count: {p.get('rotation_count', 10)})")

    print(f"\n  Proxies normais: {len(static)}")
    for p in static:
        print(f"    [{p['id']}] {p['protocol']}://{p['host']}:{p['port']}")
    print()


def main() -> None:
    """Função principal - parse de argumentos CLI."""
    parser = argparse.ArgumentParser(
        description="ProxsR - Sistema Profissional de Proxies Rotativas",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  python main.py start                    # Inicia servidor + dashboard
  python main.py start --no-gui           # Inicia sem dashboard
  python main.py start --web-port 8000    # Dashboard na porta 8000
  python main.py start --port 9090        # Inicia na porta 9090
  python main.py status                   # Exibe status
  python main.py validate                 # Valida proxies
  python main.py config                   # Exibe configuração
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Comandos disponíveis")

    # Comando: start
    start_parser = subparsers.add_parser("start", help="Inicia o servidor de proxies + dashboard")
    start_parser.add_argument("--host", type=str, help="Host do proxy HTTP")
    start_parser.add_argument("--port", type=int, help="Porta do proxy HTTP")
    start_parser.add_argument("--socks5-host", type=str, help="Host do proxy SOCKS5")
    start_parser.add_argument("--socks5-port", type=int, help="Porta do proxy SOCKS5")
    start_parser.add_argument("--web-port", type=int, help="Porta do dashboard web (default: 5000)")
    start_parser.add_argument("--no-gui", action="store_true", help="Inicia sem dashboard web")
    start_parser.add_argument("--no-browser", action="store_true", help="Não abre o navegador automaticamente")
    start_parser.add_argument("--validate", action="store_true", help="Valida proxies antes de iniciar")

    # Comando: status
    subparsers.add_parser("status", help="Exibe status do sistema")

    # Comando: validate
    subparsers.add_parser("validate", help="Valida todas as proxies")

    # Comando: config
    subparsers.add_parser("config", help="Exibe configuração atual")

    args = parser.parse_args()

    if args.command == "start":
        cmd_start(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "validate":
        cmd_validate(args)
    elif args.command == "config":
        cmd_config(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
