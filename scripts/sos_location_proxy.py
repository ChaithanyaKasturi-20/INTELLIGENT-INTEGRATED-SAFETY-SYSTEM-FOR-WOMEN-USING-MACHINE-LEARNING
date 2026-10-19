"""Path-restricted local reverse proxy for the public SOS recipient pages."""

import http.client
import logging
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LISTEN_HOST = '127.0.0.1'
LISTEN_PORT = 8765
UPSTREAM_BASE_URL = 'http://127.0.0.1:8000'
UPSTREAM_TIMEOUT_SECONDS = 10
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ALLOWED_PATHS = {
    '/SOS/LiveLocation': frozenset(('GET',)),
    '/SOS/LiveLocation/Data': frozenset(('POST',)),
}
FORWARDED_RESPONSE_HEADERS = frozenset((
    'cache-control',
    'content-type',
    'referrer-policy',
))
ALLOWED_UPSTREAM_STATUSES = {
    '/SOS/LiveLocation': frozenset((200,)),
    '/SOS/LiveLocation/Data': frozenset((200, 400, 404, 405, 410, 429)),
}

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger('sos_location_proxy')


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class SOSLocationProxyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    server_version = 'SOSLocationProxy'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(UPSTREAM_TIMEOUT_SECONDS + 5)

    def do_GET(self):
        self._handle_request()

    def do_POST(self):
        self._handle_request()

    def do_HEAD(self):
        self._handle_request()

    def do_PUT(self):
        self._handle_request()

    def do_PATCH(self):
        self._handle_request()

    def do_DELETE(self):
        self._handle_request()

    def do_OPTIONS(self):
        self._handle_request()

    def do_TRACE(self):
        self._handle_request()

    def send_error(self, code, message=None, explain=None):
        if code == 501 and hasattr(self, 'command'):
            self._handle_request()
            return
        super().send_error(code, message, explain)

    def _handle_request(self):
        allowed_methods = ALLOWED_PATHS.get(self.path)
        if allowed_methods is None:
            self._send_error_response(404, 'Not found.')
            return
        if self.command not in allowed_methods:
            self._send_error_response(405, 'Method not allowed.')
            return

        body = b''
        request_headers = {}
        if self.command == 'POST':
            content_lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get_all('Transfer-Encoding') or len(content_lengths) != 1:
                self._send_error_response(400, 'Invalid request.')
                return
            content_types = self.headers.get_all('Content-Type', [])
            if len(content_types) != 1 or content_types[0].lower() != 'application/json':
                self._send_error_response(415, 'JSON is required.')
                return
            try:
                content_length = int(content_lengths[0])
            except (TypeError, ValueError):
                self._send_error_response(400, 'Invalid request.')
                return
            if content_length < 1:
                self._send_error_response(400, 'Invalid request.')
                return
            if content_length > MAX_REQUEST_BYTES:
                self._send_error_response(413, 'Request too large.')
                return
            body = self.rfile.read(content_length)
            if len(body) != content_length:
                self._send_error_response(400, 'Invalid request.')
                return
            request_headers['Content-Type'] = 'application/json'
        else:
            content_lengths = self.headers.get_all('Content-Length', [])
            if (
                self.headers.get_all('Transfer-Encoding')
                or len(content_lengths) > 1
                or (content_lengths and content_lengths[0] != '0')
            ):
                self._send_error_response(400, 'Invalid request.')
                return

        upstream_url = UPSTREAM_BASE_URL + self.path
        upstream_request = urllib.request.Request(
            upstream_url,
            data=body if self.command == 'POST' else None,
            headers=request_headers,
            method=self.command,
        )
        try:
            opener = urllib.request.build_opener(NoRedirectHandler())
            try:
                upstream = opener.open(upstream_request, timeout=UPSTREAM_TIMEOUT_SECONDS)
            except urllib.error.HTTPError as error_response:
                upstream = error_response
            with upstream:
                self._relay_response(upstream)
        except (OSError, urllib.error.URLError, http.client.HTTPException, socket.timeout):
            logger.warning('Django SOS location upstream is unavailable.')
            self._send_error_response(502, 'The SOS location service is temporarily unavailable.')

    def _relay_response(self, upstream):
        if upstream.status not in ALLOWED_UPSTREAM_STATUSES[self.path]:
            self._send_error_response(502, 'The SOS location service is temporarily unavailable.')
            return
        body = upstream.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            self._send_error_response(502, 'The SOS location service returned an invalid response.')
            return

        self.send_response(upstream.status)
        for name, value in upstream.headers.items():
            if name.lower() in FORWARDED_RESPONSE_HEADERS:
                self.send_header(name, value)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def _send_error_response(self, status, message):
        body = (message + '\n').encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Cache-Control', 'no-store, private')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        # Request targets can contain sensitive values; never log them.
        logger.info('SOS proxy request from %s', self.client_address[0])


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    max_concurrent_requests = 32

    def __init__(self, server_address, request_handler_class):
        self.request_slots = threading.BoundedSemaphore(self.max_concurrent_requests)
        super().__init__(server_address, request_handler_class)

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


def main():
    server = BoundedThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), SOSLocationProxyHandler)
    server.daemon_threads = True
    logger.info('Listening on http://%s:%s', LISTEN_HOST, LISTEN_PORT)
    logger.info('Forwarding only the read-only SOS recipient page and data endpoint.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info('Stopping SOS location proxy.')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
