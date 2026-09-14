"""Configuração de proxy em nível de sistema (Linux)."""

import os
import subprocess
import configparser
from pathlib import Path
from typing import Optional, Tuple

from src.logger import logger
from src.utils import get_config


class SystemProxyManager:
    """Gerencia configuração de proxy do sistema Linux."""

    def __init__(self):
        self.config = get_config()
        self.server_config = self.config.get("server", {})
        self.http_host = self.server_config.get("http_host", "127.0.0.1")
        self.http_port = self.server_config.get("http_port", 8080)
        self.socks5_host = self.server_config.get("socks5_host", "127.0.0.1")
        self.socks5_port = self.server_config.get("socks5_port", 1080)

        self._pac_file_path = Path("/tmp/proxsr.pac")
        self._env_file_path = Path.home() / ".proxsr_env"

    def _get_proxy_url(self, protocol: str = "http") -> str:
        """Retorna URL do proxy local."""
        if protocol == "socks5":
            return f"socks5://{self.socks5_host}:{self.socks5_port}"
        return f"http://{self.http_host}:{self.http_port}"

    def _get_no_proxy(self) -> str:
        """Retorna lista de hosts que não devem usar proxy."""
        return "localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.local"

    # === GNOME (Ubuntu, Fedora, etc.) ===

    def _get_gnome_proxy_mode(self) -> str:
        """Obtém modo de proxy atual do GNOME."""
        try:
            result = subprocess.run(
                ["gsettings", "get", "org.gnome.system.proxy", "mode"],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout.strip().strip("'")
        except Exception:
            return "none"

    def _set_gnome_proxy(self, enabled: bool) -> bool:
        """Configura proxy no GNOME via gsettings (manual + PAC autoconfig-url)."""
        try:
            if enabled:
                # Gera PAC file e obtém URL
                pac_url = self.generate_pac_file()

                # Define modo manual (HTTP/HTTPS/SOCKS)
                subprocess.run(
                    ["gsettings", "set", "org.gnome.system.proxy", "mode", "'manual'"],
                    check=True, timeout=5
                )
                # HTTP proxy
                subprocess.run(
                    ["gsettings", "set", "org.gnome.system.proxy.http",
                     "host", f"'{self.http_host}'"],
                    check=True, timeout=5
                )
                subprocess.run(
                    ["gsettings", "set", "org.gnome.system.proxy.http",
                     "port", str(self.http_port)],
                    check=True, timeout=5
                )
                # HTTPS proxy
                subprocess.run(
                    ["gsettings", "set", "org.gnome.system.proxy.https",
                     "host", f"'{self.http_host}'"],
                    check=True, timeout=5
                )
                subprocess.run(
                    ["gsettings", "set", "org.gnome.system.proxy.https",
                     "port", str(self.http_port)],
                    check=True, timeout=5
                )
                # SOCKS proxy
                subprocess.run(
                    ["gsettings", "set", "org.gnome.system.proxy.socks",
                     "host", f"'{self.socks5_host}'"],
                    check=True, timeout=5
                )
                subprocess.run(
                    ["gsettings", "set", "org.gnome.system.proxy.socks",
                     "port", str(self.socks5_port)],
                    check=True, timeout=5
                )
                # Ignorar hosts locais
                no_proxy = self._get_no_proxy()
                hosts = [f"'{h.strip()}'" for h in no_proxy.split(',')]
                ignore_hosts = f"[{', '.join(hosts)}]"
                subprocess.run(
                    ["gsettings", "set", "org.gnome.system.proxy",
                     "ignore-hosts", ignore_hosts],
                    check=True, timeout=5
                )
                # Habilita PAC autoconfig-url (apps que suportam PAC usam ele)
                subprocess.run(
                    ["gsettings", "set", "org.gnome.system.proxy",
                     "autoconfig-url", f"'{pac_url}'"],
                    check=True, timeout=5
                )
                logger.info(f"Proxy do sistema GNOME ativado (PAC: {pac_url})")
            else:
                # Desativa proxy (modo none)
                subprocess.run(
                    ["gsettings", "set", "org.gnome.system.proxy", "mode", "'none'"],
                    check=True, timeout=5
                )
                subprocess.run(
                    ["gsettings", "set", "org.gnome.system.proxy",
                     "autoconfig-url", "''"],
                    check=True, timeout=5
                )
                logger.info("Proxy do sistema GNOME desativado")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Erro ao configurar proxy GNOME: {e}")
            return False
        except FileNotFoundError:
            logger.debug("gsettings não encontrado (não é GNOME)")
            return False

    # === KDE Plasma ===

    def _set_kde_proxy(self, enabled: bool) -> bool:
        """Configura proxy no KDE Plasma via kwriteconfig5."""
        try:
            config_file = Path.home() / ".config" / "kioslaverc"

            if enabled:
                if not config_file.exists():
                    config_file.parent.mkdir(parents=True, exist_ok=True)

                # Gera PAC file e obtém URL
                pac_url = self.generate_pac_file()

                # Define proxy manual + PAC
                commands = [
                    ["kwriteconfig5", "--file", "kioslaverc",
                     "--group", "Proxy Settings",
                     "--key", "ProxyType", "1"],  # Manual
                    ["kwriteconfig5", "--file", "kioslaverc",
                     "--group", "Proxy Settings",
                     "--key", "httpProxy", f"{self.http_host}:{self.http_port}"],
                    ["kwriteconfig5", "--file", "kioslaverc",
                     "--group", "Proxy Settings",
                     "--key", "httpsProxy", f"{self.http_host}:{self.http_port}"],
                    ["kwriteconfig5", "--file", "kioslaverc",
                     "--group", "Proxy Settings",
                     "--key", "socksProxy", f"{self.socks5_host}:{self.socks5_port}"],
                    ["kwriteconfig5", "--file", "kioslaverc",
                     "--group", "Proxy Settings",
                     "--key", "NoProxyFor", self._get_no_proxy()],
                    ["kwriteconfig5", "--file", "kioslaverc",
                     "--group", "Proxy Settings",
                     "--key", "UseReverseProxy", "false"],
                    # PAC autoconfig-url para apps KDE que suportam
                    ["kwriteconfig5", "--file", "kioslaverc",
                     "--group", "Proxy Settings",
                     "--key", "AutoConfigUrl", pac_url],
                ]
                for cmd in commands:
                    subprocess.run(cmd, check=True, timeout=5)

                subprocess.run(["kioslave5", "--quit"], capture_output=True, timeout=3)
                logger.info(f"Proxy do sistema KDE ativado (PAC: {pac_url})")
            else:
                subprocess.run([
                    "kwriteconfig5", "--file", "kioslaverc",
                    "--group", "Proxy Settings",
                    "--key", "ProxyType", "0"  # None
                ], check=True, timeout=5)
                subprocess.run([
                    "kwriteconfig5", "--file", "kioslaverc",
                    "--group", "Proxy Settings",
                    "--key", "AutoConfigUrl", ""
                ], check=True, timeout=5)
                subprocess.run(["kioslave5", "--quit"], capture_output=True, timeout=3)
                logger.info("Proxy do sistema KDE desativado")
            return True
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.debug(f"KDE proxy config não disponível: {e}")
            return False

    # === Variáveis de Ambiente (funciona em qualquer DE) ===

    def _set_env_proxy(self, enabled: bool) -> bool:
        """Configura proxy via variáveis de ambiente (persistente em ~/.proxsr_env).
        Também auto-adiciona 'source ~/.proxsr_env' ao .bashrc/.zshrc se necessário."""
        try:
            if enabled:
                content = f"""# ProxsR - Variáveis de proxy do sistema
# Gerenciado automaticamente pelo ProxsR

export http_proxy="{self._get_proxy_url('http')}"
export https_proxy="{self._get_proxy_url('http')}"
export socks_proxy="{self._get_proxy_url('socks5')}"
export all_proxy="{self._get_proxy_url('socks5')}"
export no_proxy="{self._get_no_proxy()}"
export HTTP_PROXY="{self._get_proxy_url('http')}"
export HTTPS_PROXY="{self._get_proxy_url('http')}"
export SOCKS_PROXY="{self._get_proxy_url('socks5')}"
export ALL_PROXY="{self._get_proxy_url('socks5')}"
export NO_PROXY="{self._get_no_proxy()}"
"""
                self._env_file_path.write_text(content)
                # Auto-adiciona source ao .bashrc/.zshrc
                self._ensure_shell_integration()
                logger.info(f"Variáveis de proxy salvas em {self._env_file_path}")
            else:
                if self._env_file_path.exists():
                    self._env_file_path.unlink()
                self._remove_shell_integration()
                logger.info("Variáveis de proxy removidas")
            return True
        except Exception as e:
            logger.error(f"Erro ao configurar variáveis de ambiente: {e}")
            return False

    def _ensure_shell_integration(self) -> None:
        """Adiciona 'source ~/.proxsr_env' ao .bashrc e .zshrc se não existir."""
        source_line = f"source {self._env_file_path}"
        marker = "# ProxsR proxy"
        block = f"{marker}\n{source_line}\n"

        for rc_file in [".bashrc", ".zshrc"]:
            rc_path = Path.home() / rc_file
            if not rc_path.exists():
                continue
            content = rc_path.read_text()
            if source_line in content:
                continue  # Já existe
            # Adiciona no final
            with open(rc_path, "a") as f:
                f.write(f"\n{block}")
            logger.info(f"Adicionado '{source_line}' ao {rc_file}")

    def _remove_shell_integration(self) -> None:
        """Remove bloco ProxsR do .bashrc e .zshrc."""
        source_line = f"source {self._env_file_path}"
        marker = "# ProxsR proxy"

        for rc_file in [".bashrc", ".zshrc"]:
            rc_path = Path.home() / rc_file
            if not rc_path.exists():
                continue
            content = rc_path.read_text()
            if source_line not in content:
                continue
            # Remove o bloco marker + source
            lines = content.splitlines(keepends=True)
            new_lines = []
            skip_next = False
            for line in lines:
                if marker in line:
                    skip_next = True
                    continue
                if skip_next and source_line in line:
                    skip_next = False
                    continue
                new_lines.append(line)
            rc_path.write_text("".join(new_lines))
            logger.info(f"Removido '{source_line}' do {rc_file}")

    # === PAC File ===

    def generate_pac_file(self) -> str:
        """Gera arquivo PAC (Proxy Auto-Config) para navegadores e apps."""
        pac_content = f"""function FindProxyForURL(url, host) {{
    // ProxsR - Proxy Auto-Config
    // HTTP: {self.http_host}:{self.http_port} | SOCKS5: {self.socks5_host}:{self.socks5_port}

    // Não usar proxy para hosts locais
    if (isPlainHostName(host) ||
        dnsDomainIs(host, "localhost") ||
        shExpMatch(host, "127.*.*.*") ||
        shExpMatch(host, "10.*.*.*") ||
        shExpMatch(host, "172.1[6-9].*.*") ||
        shExpMatch(host, "172.2[0-9].*.*") ||
        shExpMatch(host, "172.3[0-1].*.*") ||
        shExpMatch(host, "192.168.*.*") ||
        shExpMatch(host, "*.local")) {{
        return "DIRECT";
    }}

    // HTTP/HTTPS -> proxy HTTP com fallback SOCKS5
    if (url.substring(0, 5) == "http:" || url.substring(0, 6) == "https:") {{
        return "PROXY {self.http_host}:{self.http_port}; SOCKS5 {self.socks5_host}:{self.socks5_port}; DIRECT";
    }}

    // Outros protocolos -> SOCKS5 com fallback direto
    return "SOCKS5 {self.socks5_host}:{self.socks5_port}; DIRECT";
}}
"""
        self._pac_file_path.write_text(pac_content)
        logger.debug(f"Arquivo PAC gerado: {self._pac_file_path}")
        return f"file://{self._pac_file_path}"

    def get_pac_url(self) -> str:
        """Retorna URL do arquivo PAC."""
        if not self._pac_file_path.exists():
            self.generate_pac_file()
        return f"file://{self._pac_file_path}"

    # === Método Principal ===

    def set_system_proxy(self, enabled: bool) -> Tuple[bool, str]:
        """
        Configura proxy do sistema.
        Tenta GNOME -> KDE -> Variáveis de ambiente.
        Retorna (sucesso, método_usado).
        """
        # Tenta GNOME
        if self._is_gnome():
            if self._set_gnome_proxy(enabled):
                self._set_env_proxy(enabled)
                return True, "gnome"

        # Tenta KDE
        if self._is_kde():
            if self._set_kde_proxy(enabled):
                self._set_env_proxy(enabled)
                return True, "kde"

        # Fallback: apenas variáveis de ambiente
        if self._set_env_proxy(enabled):
            return True, "environment"

        return False, "none"

    def _is_gnome(self) -> bool:
        """Verifica se está rodando no GNOME."""
        return os.environ.get("XDG_CURRENT_DESKTOP", "").lower().find("gnome") >= 0 or \
               os.environ.get("DESKTOP_SESSION", "").lower().find("gnome") >= 0

    def _is_kde(self) -> bool:
        """Verifica se está rodando no KDE."""
        return os.environ.get("XDG_CURRENT_DESKTOP", "").lower().find("kde") >= 0 or \
               os.environ.get("DESKTOP_SESSION", "").lower().find("plasma") >= 0

    def get_status(self) -> dict:
        """Retorna status atual do proxy do sistema."""
        gnome_mode = self._get_gnome_proxy_mode() if self._is_gnome() else "unknown"
        env_exists = self._env_file_path.exists()
        pac_exists = self._pac_file_path.exists()
        shell_sourced = self._is_shell_sourced()

        kde_enabled = False
        if self._is_kde():
            kde = configparser.ConfigParser(interpolation=None)
            kde.read(Path.home() / ".config" / "kioslaverc")
            kde_enabled = kde.get("Proxy Settings", "ProxyType", fallback="0") == "1"
        return {
            "enabled": gnome_mode == "manual" or kde_enabled,
            "environment_prepared": env_exists,
            "shell_integration": shell_sourced,
            "method": "gnome" if gnome_mode == "manual" else ("kde" if kde_enabled else ("environment" if env_exists else "none")),
            "gnome_mode": gnome_mode,
            "env_file": str(self._env_file_path) if env_exists else None,
            "pac_file": str(self._pac_file_path) if pac_exists else None,
            "pac_url": self.get_pac_url() if pac_exists else None,
            "proxy_http": f"{self.http_host}:{self.http_port}",
            "proxy_socks5": f"{self.socks5_host}:{self.socks5_port}",
        }

    def _is_shell_sourced(self) -> bool:
        """Verifica se o .bashrc/.zshrc já sourceia ~/.proxsr_env."""
        source_line = f"source {self._env_file_path}"
        for rc_file in [".bashrc", ".zshrc"]:
            rc_path = Path.home() / rc_file
            if rc_path.exists() and source_line in rc_path.read_text():
                return True
        return False

    def reload_ports(self) -> None:
        """Recarrega portas do config.json (após alteração via painel web)."""
        try:
            config = get_config()
            server = config.get("server", {})
            self.http_host = server.get("http_host", self.http_host)
            self.http_port = server.get("http_port", self.http_port)
            self.socks5_host = server.get("socks5_host", self.socks5_host)
            self.socks5_port = server.get("socks5_port", self.socks5_port)
            logger.debug(f"Portas do system_proxy recarregadas: HTTP={self.http_port}, SOCKS5={self.socks5_port}")
        except Exception as e:
            logger.error(f"Erro ao recarregar portas do system_proxy: {e}")

    def restore(self) -> bool:
        """Restaura proxy do sistema para desativado (usado no shutdown)."""
        try:
            status = self.get_status()
            if status.get("enabled"):
                logger.info("Restaurando proxy do sistema (desativando)...")
                success, method = self.set_system_proxy(False)
                if success:
                    logger.info(f"Proxy do sistema restaurado via {method}")
                else:
                    logger.warning("Falha ao restaurar proxy do sistema")
                return success
            return True
        except Exception as e:
            logger.error(f"Erro ao restaurar proxy do sistema: {e}")
            return False


# Instância global
system_proxy_manager = SystemProxyManager()