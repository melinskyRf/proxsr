"""Proxies HTTP persistente e SOCKS5, com limites e autenticação opcionais."""

import asyncio
import base64
import hmac
import ipaddress
import socket
import struct
import threading
from urllib.parse import urlsplit

import h11

from src.logger import logger


async def close_writer(writer):
    """O proprietário encerra cada transporte uma única vez, inclusive em cancelamentos."""
    if writer is None:
        return
    if not writer.is_closing():
        writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 2)
    except (OSError, asyncio.TimeoutError):
        pass


def address_bytes(host, port):
    try:
        address = ipaddress.ip_address(host)
        encoded = bytes([1 if address.version == 4 else 4]) + address.packed
    except ValueError:
        encoded_host = host.encode('idna')
        if not 1 <= len(encoded_host) <= 255:
            raise ValueError('Nome de destino inválido')
        encoded = b'\x03' + bytes([len(encoded_host)]) + encoded_host
    return encoded + struct.pack('!H', port)


async def read_address(reader, atyp):
    if atyp == 1:
        data = await reader.readexactly(4)
        host = socket.inet_ntop(socket.AF_INET, data)
    elif atyp == 4:
        data = await reader.readexactly(16)
        host = socket.inet_ntop(socket.AF_INET6, data)
    elif atyp == 3:
        size = await reader.readexactly(1)
        raw = await reader.readexactly(size[0])
        host, data = raw.decode('idna'), size + raw
    else:
        raise ValueError('Tipo de endereço inválido')
    port_data = await reader.readexactly(2)
    return host, struct.unpack('!H', port_data)[0], bytes([atyp]) + data + port_data


async def open_upstream(proxy, host, port, timeout=10):
    """Retorna streams e endereço de bind informado pelo SOCKS upstream."""
    writer = None
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(
                proxy.host, proxy.port, ssl=True if proxy.protocol == 'https' else None)
            if proxy.protocol == 'socks5':
                method = 2 if proxy.username else 0
                writer.write(bytes([5, 1, method]))
                await writer.drain()
                if await reader.readexactly(2) != bytes([5, method]):
                    raise ConnectionError('Método SOCKS5 recusado pelo upstream')
                if method == 2:
                    user, password = proxy.username.encode(), (proxy.password or '').encode()
                    if not 1 <= len(user) <= 255 or not 1 <= len(password) <= 255:
                        raise ValueError('Credenciais SOCKS5 devem ter entre 1 e 255 bytes')
                    writer.write(b'\x01' + bytes([len(user)]) + user + bytes([len(password)]) + password)
                    await writer.drain()
                    if await reader.readexactly(2) != b'\x01\x00':
                        raise ConnectionError('Autenticação SOCKS5 recusada')
                writer.write(b'\x05\x01\x00' + address_bytes(host, port))
                await writer.drain()
                version, reply, reserved, atyp = await reader.readexactly(4)
                if version != 5 or reply != 0 or reserved != 0:
                    raise ConnectionError('O upstream recusou o túnel SOCKS5')
                _, _, bound = await read_address(reader, atyp)
            else:
                authority = f'[{host}]:{port}' if ':' in host else f'{host}:{port}'
                headers = f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n'
                if proxy.username:
                    auth = base64.b64encode(f'{proxy.username}:{proxy.password or ""}'.encode()).decode()
                    headers += f'Proxy-Authorization: Basic {auth}\r\n'
                writer.write((headers + '\r\n').encode())
                await writer.drain()
                response = await reader.readuntil(b'\r\n\r\n')
                status = response.split(b'\r\n', 1)[0].split()
                if len(status) < 2 or status[1] != b'200':
                    raise ConnectionError('O upstream recusou o túnel HTTP')
                # HTTP CONNECT não revela o bind remoto. Informamos o socket local real.
                local = writer.get_extra_info('sockname')
                bound = address_bytes(local[0], local[1])
            return reader, writer, bound
    except BaseException:
        await close_writer(writer)
        raise


async def forward(reader1, writer1, reader2, writer2, idle_timeout=60):
    """Fluxo bidirecional; o timeout reinicia com atividade em qualquer direção."""
    loop = asyncio.get_running_loop()
    last_activity = loop.time()

    async def copy(source, target):
        nonlocal last_activity
        while data := await source.read(65536):
            last_activity = loop.time()
            target.write(data)
            await asyncio.wait_for(target.drain(), idle_timeout)
        if target.can_write_eof() and not target.is_closing():
            target.write_eof()

    async def watch_idle():
        while True:
            remaining = idle_timeout - (loop.time() - last_activity)
            if remaining <= 0:
                raise TimeoutError('Túnel sem atividade')
            await asyncio.sleep(remaining)

    copies = [asyncio.create_task(copy(reader1, writer2)), asyncio.create_task(copy(reader2, writer1))]
    transfer = asyncio.gather(*copies)
    watch = asyncio.create_task(watch_idle())
    try:
        done, _ = await asyncio.wait([transfer, watch], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        watch.cancel()
        for task in copies:
            task.cancel()
        await asyncio.gather(transfer, watch, return_exceptions=True)


async def next_event(connection, reader, timeout):
    while True:
        event = connection.next_event()
        if event is not h11.NEED_DATA:
            return event
        connection.receive_data(await asyncio.wait_for(reader.read(65536), timeout))


async def send_event(connection, writer, event, timeout):
    data = connection.send(event)
    if data:
        writer.write(data)
        await asyncio.wait_for(writer.drain(), timeout)


def clean_headers(headers):
    connection_tokens = {token.strip().lower() for k, value in headers if k.lower() == b'connection'
                         for token in value.split(b',')}
    omit = connection_tokens | {b'connection', b'proxy-connection', b'proxy-authorization',
                                b'proxy-authenticate', b'keep-alive', b'transfer-encoding', b'trailer'}
    return [(key, value) for key, value in headers if key.lower() not in omit]


class ProxyServer:
    def __init__(self, manager, host='127.0.0.1', port=8080, config=None):
        self.manager, self.host, self.port = manager, host, port
        self.config = config if config is not None else getattr(manager, '_config', {}).get('server', {})
        self.connect_timeout = float(self.config.get('connect_timeout_seconds', 10))
        self.request_timeout = float(self.config.get('request_timeout_seconds', 30))
        self.idle_timeout = float(self.config.get('idle_timeout_seconds', 60))
        if min(self.connect_timeout, self.request_timeout, self.idle_timeout) <= 0:
            raise ValueError('Timeouts devem ser positivos')
        self.auth = self.config.get('client_auth', {})
        if self.auth.get('enabled') and not all(self.auth.get(key) for key in ('username', 'password')):
            raise ValueError('Configure usuário e senha para autenticação de clientes')
        self._server = self._loop = self._thread = None
        self._clients = set()

    def authorized(self, headers):
        if not self.auth.get('enabled', False):
            return True
        supplied = next((value for key, value in headers if key.lower() == b'proxy-authorization'), b'')
        try:
            scheme, token = supplied.split(None, 1)
            credentials = base64.b64decode(token, validate=True)
            expected = f'{self.auth["username"]}:{self.auth["password"]}'.encode()
            return scheme.lower() == b'basic' and hmac.compare_digest(credentials, expected)
        except (ValueError, TypeError):
            return False

    async def handle_client(self, reader, writer):
        client = h11.Connection(h11.SERVER)
        upstream_writer = upstream_reader = upstream = upstream_key = None
        proxy = None
        response_started = False
        try:
            while True:
                proxy = None
                response_started = False
                request = await next_event(client, reader, self.idle_timeout)
                if isinstance(request, h11.ConnectionClosed):
                    return
                if not isinstance(request, h11.Request):
                    raise ValueError('Requisição HTTP inválida')
                if not self.authorized(request.headers):
                    await send_event(client, writer, h11.Response(status_code=407, headers=[
                        (b'Proxy-Authenticate', b'Basic realm="ProxsR"'), (b'Content-Length', b'0'),
                        (b'Connection', b'close')]), self.request_timeout)
                    await send_event(client, writer, h11.EndOfMessage(), self.request_timeout)
                    return
                method, target = request.method, request.target.decode('ascii')
                host_header = next((v.decode('ascii') for k, v in request.headers if k == b'host'), '')
                url = urlsplit('//' + target if method == b'CONNECT' else target)
                if method != b'CONNECT' and not url.hostname:
                    url = urlsplit('http://' + host_header + target)
                if not url.hostname or url.username or url.password or (method != b'CONNECT' and url.scheme != 'http'):
                    raise ValueError('Destino inválido; use CONNECT para HTTPS')
                if any(k == b'upgrade' for k, _ in request.headers):
                    raise ValueError('Use CONNECT para protocolos com upgrade')
                port = url.port or (443 if method == b'CONNECT' else 80)
                proxy = self.manager.get_proxy_for_request()
                if not proxy:
                    raise ConnectionError('Nenhuma proxy disponível')
                if method == b'CONNECT':
                    await close_writer(upstream_writer)
                    upstream_writer = None
                    upstream_reader, upstream_writer, _ = await open_upstream(proxy, url.hostname, port, self.connect_timeout)
                    await send_event(client, writer, h11.Response(status_code=200, headers=[]), self.request_timeout)
                    response_started = True
                    buffered, _ = client.trailing_data
                    if buffered:
                        upstream_writer.write(buffered)
                        await asyncio.wait_for(upstream_writer.drain(), self.request_timeout)
                    self.manager.report_success(proxy.id)
                    await forward(reader, writer, upstream_reader, upstream_writer, self.idle_timeout)
                    return

                key = (proxy.url, url.hostname if proxy.protocol == 'socks5' else None,
                       port if proxy.protocol == 'socks5' else None)
                if key != upstream_key or upstream_reader is None or upstream_reader.at_eof():
                    await close_writer(upstream_writer)
                    upstream_writer = None
                    if proxy.protocol == 'socks5':
                        upstream_reader, upstream_writer, _ = await open_upstream(proxy, url.hostname, port, self.connect_timeout)
                    else:
                        upstream_reader, upstream_writer = await asyncio.wait_for(asyncio.open_connection(
                            proxy.host, proxy.port, ssl=True if proxy.protocol == 'https' else None), self.connect_timeout)
                    upstream, upstream_key = h11.Connection(h11.CLIENT), key
                path = url.path or '/'
                if url.query:
                    path += '?' + url.query
                outgoing_target = path if proxy.protocol == 'socks5' else f'http://{url.netloc}{path}'
                headers = [(k, v) for k, v in clean_headers(request.headers) if k not in (b'host', b'expect')]
                headers.append((b'Host', url.netloc.encode('ascii')))
                if any(k == b'transfer-encoding' for k, _ in request.headers):
                    headers.append((b'Transfer-Encoding', b'chunked'))
                if proxy.protocol != 'socks5' and proxy.username:
                    headers.append((b'Proxy-Authorization', b'Basic ' + base64.b64encode(f'{proxy.username}:{proxy.password or ""}'.encode())))
                await send_event(upstream, upstream_writer, h11.Request(method=method, target=outgoing_target.encode('ascii'), headers=headers), self.request_timeout)
                if client.they_are_waiting_for_100_continue:
                    await send_event(client, writer, h11.InformationalResponse(status_code=100, headers=[]), self.request_timeout)
                while True:
                    event = await next_event(client, reader, self.request_timeout)
                    if isinstance(event, h11.Data):
                        await send_event(upstream, upstream_writer, event, self.request_timeout)
                    elif isinstance(event, h11.EndOfMessage):
                        await send_event(upstream, upstream_writer, h11.EndOfMessage(), self.request_timeout)
                        break
                    else:
                        raise ValueError('Corpo HTTP incompleto')
                while True:
                    event = await next_event(upstream, upstream_reader, self.request_timeout)
                    if isinstance(event, h11.InformationalResponse):
                        if event.status_code != 100:
                            await send_event(client, writer, h11.InformationalResponse(status_code=event.status_code, headers=clean_headers(event.headers)), self.request_timeout)
                    elif isinstance(event, h11.Response):
                        await send_event(client, writer, h11.Response(status_code=event.status_code, headers=clean_headers(event.headers), reason=event.reason), self.request_timeout)
                        response_started = True
                    elif isinstance(event, h11.Data):
                        await send_event(client, writer, event, self.request_timeout)
                    elif isinstance(event, h11.EndOfMessage):
                        await send_event(client, writer, h11.EndOfMessage(), self.request_timeout)
                        break
                    else:
                        raise ConnectionError('Resposta HTTP incompleta')
                self.manager.report_success(proxy.id)
                if upstream.our_state is h11.DONE and upstream.their_state is h11.DONE:
                    upstream.start_next_cycle()
                else:
                    await close_writer(upstream_writer)
                    upstream_writer = upstream_reader = upstream_key = None
                if client.our_state is h11.DONE and client.their_state is h11.DONE:
                    client.start_next_cycle()
                else:
                    return
        except (Exception,) as exc:
            logger.debug('Falha HTTP: %s', type(exc).__name__)
            if proxy:
                self.manager.report_failure(proxy.id)
            if not response_started and client.our_state in (h11.IDLE, h11.SEND_RESPONSE):
                status = 400 if isinstance(exc, (ValueError, h11.RemoteProtocolError)) else 504 if isinstance(exc, TimeoutError) else 502
                try:
                    await send_event(client, writer, h11.Response(status_code=status, headers=[(b'Content-Length', b'0'), (b'Connection', b'close')]), self.request_timeout)
                    await send_event(client, writer, h11.EndOfMessage(), self.request_timeout)
                except (OSError, h11.LocalProtocolError):
                    pass
        finally:
            await close_writer(upstream_writer)
            await close_writer(writer)

    async def _accept_client(self, reader, writer):
        task = asyncio.current_task()
        self._clients.add(task)
        try:
            await self.handle_client(reader, writer)
        finally:
            self._clients.discard(task)

    async def start_async(self):
        self._server = await asyncio.start_server(self._accept_client, self.host, self.port)
        logger.info('%s em %s:%s', type(self).__name__, self.host, self.port)

    async def stop_async(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        clients = list(self._clients)
        for task in clients:
            task.cancel()
        await asyncio.gather(*clients, return_exceptions=True)

    def start(self):
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
        self._thread = threading.Thread(target=run, daemon=True, name=type(self).__name__)
        self._thread.start()
        if not ready.wait(10):
            raise TimeoutError('Tempo esgotado ao iniciar servidor')
        if errors:
            raise errors[0]

    def stop(self):
        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self.stop_async(), self._loop).result(timeout=10)
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=5)


class SOCKS5Server(ProxyServer):
    def __init__(self, manager, host='127.0.0.1', port=1080, config=None):
        super().__init__(manager, host, port, config)

    async def handle_client(self, reader, writer):
        proxy = None
        upstream_writer = None
        established = False
        can_reply = False
        try:
            async with asyncio.timeout(self.connect_timeout):
                version, count = await reader.readexactly(2)
                methods = await reader.readexactly(count)
                if version != 5:
                    return
                method = 2 if self.auth.get('enabled', False) else 0
                if method not in methods:
                    writer.write(b'\x05\xff')
                    await writer.drain()
                    return
                writer.write(bytes([5, method]))
                await writer.drain()
                if method == 2:
                    auth_version, size = await reader.readexactly(2)
                    user = await reader.readexactly(size)
                    size = (await reader.readexactly(1))[0]
                    password = await reader.readexactly(size)
                    valid = auth_version == 1 and hmac.compare_digest(user, self.auth['username'].encode()) and hmac.compare_digest(password, self.auth['password'].encode())
                    writer.write(b'\x01' + bytes([0 if valid else 1]))
                    await writer.drain()
                    if not valid:
                        return
                version, command, reserved, atyp = await reader.readexactly(4)
                can_reply = True
                if version != 5 or reserved != 0 or command != 1:
                    writer.write(b'\x05\x07\x00\x01' + bytes(6))
                    await writer.drain()
                    return
                if atyp not in (1, 3, 4):
                    writer.write(b'\x05\x08\x00\x01' + bytes(6))
                    await writer.drain()
                    return
                host, port, _ = await read_address(reader, atyp)
                proxy = self.manager.get_proxy_for_request()
                if not proxy:
                    raise ConnectionError('Nenhuma proxy disponível')
                upstream_reader, upstream_writer, bound = await open_upstream(proxy, host, port, self.connect_timeout)
                writer.write(b'\x05\x00\x00' + bound)
                await writer.drain()
            established = True
            self.manager.report_success(proxy.id)
            await forward(reader, writer, upstream_reader, upstream_writer, self.idle_timeout)
        except Exception as exc:
            logger.debug('Falha SOCKS5: %s', type(exc).__name__)
            if proxy:
                self.manager.report_failure(proxy.id)
            if can_reply and not established and not writer.is_closing():
                writer.write(b'\x05\x05\x00\x01' + bytes(6))
                await writer.drain()
        finally:
            await close_writer(upstream_writer)
            await close_writer(writer)
