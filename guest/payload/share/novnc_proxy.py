#!/usr/bin/env python3
"""SSH-only passwordless viewer; block cross-site and framed access."""
from websockify.auth_plugins import ExpectOrigin
from websockify.websocketproxy import ProxyRequestHandler, WebSocketProxy


class LocalViewerHandler(ProxyRequestHandler):
    def parse_request(self):
        if not super().parse_request():
            return False
        values = self.headers.get_all('Host', [])
        if len(values) != 1 or values[0].lower() not in self.server.viewer_hosts:
            self.send_error(403, 'Invalid viewer Host')
            self.close_connection = True
            return False
        return True

    def handle_upgrade(self):
        if len(self.headers.get_all('Origin', [])) != 1:
            self.send_error(403, 'Exactly one viewer Origin is required')
            self.close_connection = True
            return
        return super().handle_upgrade()

    def end_headers(self):
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
        self.send_header('X-Content-Type-Options', 'nosniff')
        return super().end_headers()


def make_proxy(port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Invalid client viewer port')
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    proxy = WebSocketProxy(RequestHandlerClass=LocalViewerHandler,
                           listen_host='127.0.0.1', listen_port=6080,
                           target_host='127.0.0.1', target_port=5900,
                           web='/usr/share/novnc', file_only=True, web_auth=False,
                           auth_plugin=ExpectOrigin(' '.join('http://' + x for x in sorted(hosts))))
    proxy.viewer_hosts = hosts
    return proxy


if __name__ == '__main__':
    from runtime_config import config
    make_proxy(config()['viewer_port']).start_server()
