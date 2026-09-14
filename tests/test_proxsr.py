import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from src.proxy_pool import ProxyPool
from src.proxy_rotator import ProxyRotator
from src.web_server import WebDashboard
from src.health_checker import HealthChecker
from src.proxy_validator import ProxyValidationResult
from src.server import ProxyServer, SOCKS5Server
from src.utils import build_proxy_url


def entry(pid='one', **values):
    return dict(id=pid, type='rotating', host='127.0.0.1', port=12345, protocol='http', rotation_count=2, **values)


class PoolTests(unittest.TestCase):
    def test_disabled_and_dead_are_retained(self):
        pool = ProxyPool()
        pool.add_proxy(entry(enabled=False))
        pool.add_proxy(entry('two')).is_alive = False
        self.assertEqual(len(pool.get_all()), 2)
        self.assertEqual(pool.get_all_alive(), [])
        self.assertEqual(pool.get_stats()['rotating_total'], 2)
        pool.enable_proxy('one')
        self.assertEqual(len(pool.get_rotating_pool()), 1)

    def test_upsert_moves_type_without_duplicate(self):
        pool = ProxyPool()
        pool.add_proxy(entry())
        data = entry()
        data['type'] = 'static'
        pool.add_proxy(data)
        self.assertEqual(len(pool.get_all()), 1)
        self.assertEqual(pool.get_rotating_pool(), [])
        self.assertEqual(len(pool.get_static_pool()), 1)

    def test_exact_rotation_count_and_single_request(self):
        pool = ProxyPool()
        pool.add_proxy(entry())
        pool.add_proxy(entry('two'))
        rotator = ProxyRotator(pool, shuffle_on_restart=False)
        self.assertEqual([rotator.get_next_proxy().id for _ in range(6)], ['one', 'one', 'two', 'two', 'one', 'one'])
        for p in pool.get_all():
            p.rotation_count = 1
        rotator.reset()
        self.assertEqual([rotator.get_next_proxy().id for _ in range(4)], ['one', 'two', 'one', 'two'])
        self.assertNotEqual(rotator.get_next_proxy(exclude='one').id, 'one')
        pool.disable_proxy('two')
        self.assertIsNone(rotator.get_next_proxy(exclude='one'))
        pool.disable_proxy('one')
        self.assertIsNone(rotator.get_next_proxy())

    def test_force_next_does_not_consume_request(self):
        for strategy in ('cycle', 'round_robin', 'random', 'least_used'):
            pool = ProxyPool()
            pool.add_proxy(entry())
            pool.add_proxy(entry('two'))
            rotator = ProxyRotator(pool, strategy=strategy, shuffle_on_restart=False)
            first = rotator.get_next_proxy()
            chosen = rotator.force_next(exclude=first.id)
            self.assertNotEqual(chosen.id, first.id)
            self.assertEqual(chosen.current_rotations, 0)
            self.assertEqual(rotator.get_next_proxy().id, chosen.id)
            if strategy == 'cycle':
                self.assertEqual(chosen.current_rotations, 1)

    def test_credentials_and_ipv6(self):
        self.assertEqual(build_proxy_url('http', '::1', 80, 'a@b', 'x:y'), 'http://a%40b:x%3Ay@[::1]:80')


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = ProxyPool()
        self.pool.add_proxy(entry(password='secret'))
        self.pool.add_proxy(entry('disabled', enabled=False))
        self.pool.add_proxy(entry('dead')).is_alive = False
        manager = SimpleNamespace(pool=self.pool, rotator=ProxyRotator(self.pool))
        manager.get_status = lambda: {'pool': self.pool.get_stats(), 'rotator': manager.rotator.get_state()}
        self.dashboard = WebDashboard(manager)
        self.dashboard._setup_routes()
        self.client = TestClient(TestServer(self.dashboard._app))
        await self.client.start_server()
        self.temp = tempfile.TemporaryDirectory()
        self.saved = Path(self.temp.name) / 'proxies.json'
        self.patches = [patch('src.web_server.get_proxies_config', return_value={}),
                        patch('src.web_server.save_json', side_effect=lambda path, data: self.saved.write_text(json.dumps(data)))]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        await self.client.close()
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    async def test_edit_preserves_password_disabled_dead_and_type(self):
        response = await self.client.post('/api/proxy/save', json={'edit_id': 'one', 'host': 'localhost', 'port': 8888, 'proxy_type': 'static', 'password': ''})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.pool.get_proxy('one').password, 'secret')
        self.assertEqual(self.pool.get_proxy('one').proxy_type, 'static')
        saved = json.loads(self.saved.read_text())
        self.assertEqual(len(saved['rotating_proxies']) + len(saved['static_proxies']), 3)
        response = await self.client.get('/api/proxies')
        self.assertEqual(len((await response.json())['proxies']), 3)
        self.assertNotIn('password', (await response.json())['proxies'][0])

    async def test_validation_and_missing_id(self):
        for values in [{'port': 0}, {'protocol': 'ftp'}, {'rotation_count': 0}, {'id': '<script>'}]:
            response = await self.client.post('/api/proxy/save', json={'host': 'localhost', **values})
            self.assertEqual(response.status, 400)
        response = await self.client.post('/api/proxy/disable', json={'proxy_id': 'missing'})
        self.assertEqual(response.status, 404)

    async def test_toggle_persists_and_foreign_origin_rejected(self):
        response = await self.client.post('/api/proxy/disable', json={'proxy_id': 'one'})
        self.assertEqual(response.status, 200)
        self.assertFalse(json.loads(self.saved.read_text())['rotating_proxies'][0]['enabled'])
        response = await self.client.post('/api/proxy/delete', json={'proxy_id': 'one'}, headers={'Origin': 'https://example.org'})
        self.assertEqual(response.status, 403)
        self.assertIsNotNone(self.pool.get_proxy('one'))

    async def test_manual_health_recovers_dead_without_nested_loop(self):
        async def validate(**kwargs):
            return ProxyValidationResult(kwargs['proxy_id'], True, ip_address='127.0.0.2')
        self.dashboard.health_checker = HealthChecker(self.pool, SimpleNamespace(validate=validate))
        response = await self.client.post('/api/health/run', json={})
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertTrue(result['results']['dead']['valid'])
        self.assertTrue(self.pool.get_proxy('dead').is_alive)
        self.assertNotIn('disabled', result['results'])

    async def test_save_failure_rolls_back_memory(self):
        with patch('src.web_server.save_json', side_effect=OSError('disk unavailable')):
            response = await self.client.post('/api/proxy/save', json={'edit_id': 'one', 'host': 'changed', 'port': 8888})
        self.assertEqual(response.status, 500)
        self.assertEqual(self.pool.get_proxy('one').host, '127.0.0.1')
        self.assertEqual(len(self.pool.get_all()), 3)

    async def test_proxy_name_survives_save_and_reload(self):
        response = await self.client.post('/api/proxy/save', json={'edit_id': 'one', 'name': 'Brasil · principal', 'host': 'localhost', 'port': 8888})
        self.assertEqual(response.status, 200)
        persisted = json.loads(self.saved.read_text())
        reloaded = ProxyPool()
        reloaded.load_from_config(persisted)
        self.assertEqual(reloaded.get_proxy('one').name, 'Brasil · principal')
        response = await self.client.get('/api/proxies')
        self.assertEqual((await response.json())['proxies'][0]['name'], 'Brasil · principal')
        response = await self.client.post('/api/proxy/save', json={'name': 'x' * 81, 'host': 'localhost', 'port': 80})
        self.assertEqual(response.status, 400)

    async def test_rotation_configuration_applies(self):
        response = await self.client.post('/api/config', json={'section': 'rotation', 'strategy': 'random', 'max_rotations': 4, 'interval_seconds': 20, 'shuffle_on_restart': False})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.dashboard.manager.rotator.strategy.value, 'random')
        self.assertEqual(json.loads(self.saved.read_text())['rotation']['max_rotations'], 4)


class TunnelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.headers = []
        async def upstream(reader, writer):
            try:
                header = await reader.readuntil(b'\r\n\r\n')
                self.headers.append(header)
                writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n' if header.startswith(b'CONNECT ') else header)
                await writer.drain()
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
        self.upstream = await asyncio.start_server(upstream, '127.0.0.1', 0)
        pool = ProxyPool()
        data = entry(username='user', password='password')
        data['port'] = self.upstream.sockets[0].getsockname()[1]
        proxy = pool.add_proxy(data)
        manager = SimpleNamespace(get_proxy_for_request=lambda: proxy, report_success=lambda pid: None, report_failure=lambda pid: None)
        self.http = ProxyServer(manager, port=0)
        self.socks = SOCKS5Server(manager, port=0)
        await self.http.start_async()
        await self.socks.start_async()

    async def asyncTearDown(self):
        await self.http.stop_async()
        await self.socks.stop_async()
        self.upstream.close()
        await self.upstream.wait_closed()

    async def test_http_connect_transfers_bytes(self):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.http._server.sockets[0].getsockname()[1])
        writer.write(b'CONNECT example.org:443 HTTP/1.1\r\nHost: example.org:443\r\n\r\n')
        await writer.drain()
        self.assertIn(b'200', await reader.readuntil(b'\r\n\r\n'))
        writer.write(b'tunnel payload')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(14), 2), b'tunnel payload')
        self.assertIn(b'Proxy-Authorization: Basic', self.headers[0])
        writer.close()
        await writer.wait_closed()

    async def test_http_forward_keeps_body_and_strips_proxy_auth(self):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.http._server.sockets[0].getsockname()[1])
        writer.write(b'POST http://example.org/path?q=1 HTTP/1.1\r\nHost: example.org\r\nProxy-Authorization: secret\r\nContent-Length: 4\r\n\r\ndata')
        await writer.drain()
        header = await reader.readuntil(b'\r\n\r\n')
        self.assertTrue(header.startswith(b'POST http://example.org/path?q=1 HTTP/1.1'))
        self.assertNotIn(b'Proxy-Authorization: secret', header)
        self.assertIn(b'Proxy-Authorization: Basic', header)
        self.assertEqual(await reader.readexactly(4), b'data')
        writer.close()
        await writer.wait_closed()

    async def test_socks_handshake_and_tunnel(self):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.socks._server.sockets[0].getsockname()[1])
        writer.write(b'\x05\x01\x00')
        await writer.drain()
        self.assertEqual(await reader.readexactly(2), b'\x05\x00')
        writer.write(b'\x05\x01\x00\x03\x0bexample.org\x01\xbb')
        await writer.drain()
        self.assertEqual((await reader.readexactly(10))[1], 0)
        writer.write(b'socks')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(5), 2), b'socks')
        writer.close()
        await writer.wait_closed()

    async def test_socks_rejects_unsupported_auth(self):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.socks._server.sockets[0].getsockname()[1])
        writer.write(b'\x05\x01\x02')
        await writer.drain()
        self.assertEqual(await reader.readexactly(2), b'\x05\xff')
        writer.close()
        await writer.wait_closed()


if __name__ == '__main__':
    unittest.main()
