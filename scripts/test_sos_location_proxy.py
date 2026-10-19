import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import sos_location_proxy


class FakeDjangoHandler(BaseHTTPRequestHandler):
    last_request = None

    def do_GET(self):
        self._record_and_reply()

    def do_POST(self):
        self._record_and_reply()

    def _record_and_reply(self):
        length = int(self.headers.get('Content-Length', '0'))
        body = self.rfile.read(length) if length else b''
        type(self).last_request = {
            'method': self.command,
            'path': self.path,
            'headers': dict(self.headers.items()),
            'body': body,
        }
        simulate_error = body == b'{"token":"debug-error"}'
        response_body = (
            b'Traceback with a simulated secret'
            if simulate_error
            else b'<html>safe page</html>' if self.command == 'GET' else b'{"status":"stale"}'
        )
        self.send_response(500 if simulate_error else 200)
        self.send_header('Content-Type', 'text/html' if self.command == 'GET' or simulate_error else 'application/json')
        self.send_header('Cache-Control', 'no-store, private')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Set-Cookie', 'sensitive=not-forwarded')
        self.send_header('Location', 'http://untrusted.example/')
        self.send_header('Content-Length', str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, format_string, *args):
        pass


class SOSLocationProxyTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream_server = ThreadingHTTPServer(('127.0.0.1', 0), FakeDjangoHandler)
        cls.upstream_thread = threading.Thread(target=cls.upstream_server.serve_forever, daemon=True)
        cls.upstream_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream_server.shutdown()
        cls.upstream_server.server_close()
        cls.upstream_thread.join(timeout=2)

    def setUp(self):
        FakeDjangoHandler.last_request = None
        self.proxy_server = sos_location_proxy.BoundedThreadingHTTPServer(
            ('127.0.0.1', 0),
            sos_location_proxy.SOSLocationProxyHandler,
        )
        self.proxy_thread = threading.Thread(target=self.proxy_server.serve_forever, daemon=True)
        self.proxy_thread.start()
        self.connection = http.client.HTTPConnection('127.0.0.1', self.proxy_server.server_port, timeout=3)
        self.upstream_base_url = 'http://127.0.0.1:{}'.format(self.upstream_server.server_port)
        self.upstream_patch = patch.object(sos_location_proxy, 'UPSTREAM_BASE_URL', self.upstream_base_url)
        self.upstream_patch.start()

    def tearDown(self):
        self.connection.close()
        self.upstream_patch.stop()
        self.proxy_server.shutdown()
        self.proxy_server.server_close()
        self.proxy_thread.join(timeout=2)

    def test_allowed_recipient_page_get_is_forwarded(self):
        self.connection.request('GET', '/SOS/LiveLocation')
        response = self.connection.getresponse()

        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b'<html>safe page</html>')
        self.assertEqual(FakeDjangoHandler.last_request['path'], '/SOS/LiveLocation')
        self.assertEqual(FakeDjangoHandler.last_request['method'], 'GET')

    def test_allowed_location_data_post_is_forwarded_as_json(self):
        payload = json.dumps({'token': 'test-token'})
        self.connection.request(
            'POST',
            '/SOS/LiveLocation/Data',
            body=payload,
            headers={'Content-Type': 'application/json'},
        )
        response = self.connection.getresponse()

        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b'{"status":"stale"}')
        forwarded = FakeDjangoHandler.last_request
        self.assertEqual(forwarded['path'], '/SOS/LiveLocation/Data')
        self.assertEqual(forwarded['body'], payload.encode('utf-8'))
        self.assertEqual(forwarded['headers'].get('Content-Type'), 'application/json')

    def test_unlisted_paths_queries_and_trailing_slashes_are_blocked(self):
        for path in ('/admin/', '/SOS/Activate', '/SOS/LocationUpdate', '/SOS/LiveLocation/', '/SOS/LiveLocation?x=1'):
            with self.subTest(path=path):
                self.connection.request('GET', path)
                response = self.connection.getresponse()
                response.read()
                self.assertEqual(response.status, 404)
                self.assertIsNone(FakeDjangoHandler.last_request)

    def test_methods_not_allowed_for_recipient_paths_are_blocked(self):
        for method, path in (
            ('POST', '/SOS/LiveLocation'),
            ('GET', '/SOS/LiveLocation/Data'),
            ('PUT', '/SOS/LiveLocation'),
            ('BREW', '/SOS/LiveLocation'),
        ):
            with self.subTest(method=method, path=path):
                self.connection.request(method, path)
                response = self.connection.getresponse()
                response.read()
                self.assertEqual(response.status, 405)
                self.assertIsNone(FakeDjangoHandler.last_request)

    def test_untrusted_request_headers_and_sensitive_response_headers_are_not_forwarded(self):
        self.connection.request(
            'POST',
            '/SOS/LiveLocation/Data',
            body='{"token":"test-token"}',
            headers={
                'Content-Type': 'application/json',
                'Cookie': 'sessionid=untrusted',
                'X-CSRFToken': 'untrusted-csrf',
                'X-Forwarded-For': '203.0.113.8',
                'X-Forwarded-Host': 'attacker.example',
                'X-Forwarded-Proto': 'https',
                'Host': 'attacker.example',
                'Authorization': 'Bearer untrusted',
            },
        )
        response = self.connection.getresponse()
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        response.read()
        forwarded_headers = {
            name.lower(): value
            for name, value in FakeDjangoHandler.last_request['headers'].items()
        }

        self.assertEqual(forwarded_headers.get('host'), '127.0.0.1:{}'.format(self.upstream_server.server_port))
        for name in ('cookie', 'x-csrftoken', 'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto', 'authorization'):
            with self.subTest(header=name):
                self.assertNotIn(name, forwarded_headers)
        self.assertEqual(response_headers.get('cache-control'), 'no-store, private')
        self.assertEqual(response_headers.get('referrer-policy'), 'no-referrer')
        self.assertNotIn('set-cookie', response_headers)
        self.assertNotIn('location', response_headers)
        self.assertEqual(response_headers.get('x-content-type-options'), 'nosniff')

    def test_post_requires_small_json_body(self):
        self.connection.request(
            'POST',
            '/SOS/LiveLocation/Data',
            body='token=test-token',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )
        response = self.connection.getresponse()
        response.read()

        self.assertEqual(response.status, 415)
        self.assertIsNone(FakeDjangoHandler.last_request)

    def test_post_without_content_length_is_rejected(self):
        self.connection.putrequest('POST', '/SOS/LiveLocation/Data')
        self.connection.putheader('Content-Type', 'application/json')
        self.connection.endheaders()
        response = self.connection.getresponse()
        response.read()

        self.assertEqual(response.status, 400)
        self.assertIsNone(FakeDjangoHandler.last_request)

    def test_oversized_post_is_rejected_before_forwarding(self):
        body = b' ' * (sos_location_proxy.MAX_REQUEST_BYTES + 1)
        self.connection.request(
            'POST',
            '/SOS/LiveLocation/Data',
            body=body,
            headers={'Content-Type': 'application/json'},
        )
        response = self.connection.getresponse()
        response.read()

        self.assertEqual(response.status, 413)
        self.assertIsNone(FakeDjangoHandler.last_request)

    def test_django_debug_error_response_is_never_exposed(self):
        self.connection.request(
            'POST',
            '/SOS/LiveLocation/Data',
            body='{"token":"debug-error"}',
            headers={'Content-Type': 'application/json'},
        )
        response = self.connection.getresponse()
        body = response.read()

        self.assertEqual(response.status, 502)
        self.assertNotIn(b'Traceback', body)
        self.assertNotIn(b'simulated secret', body)


if __name__ == '__main__':
    unittest.main()
