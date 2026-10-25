#!/usr/bin/env python3
"""
The backend's port is checked before it starts (deploy/airlock-env.sh,
airlock_backend_port_status).

    python -m pytest systems/test_backend_port.py

On a new Mac, macOS's AirPlay Receiver listens on 5000: the backend container
cannot bind it and never starts, while :5000 still answers - with a 403 from
AirPlay - so nothing said "the port is taken". AirPlay is recognised by its
`Server: AirTunes/...` header. Tested on spare ports, so the real :5000 and a
running backend are never involved.
"""

import http.server
import os
import socket
import subprocess
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def serve():
    servers = []

    def start(server_header):
        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = server_header
            sys_version = ''

            def do_GET(self):
                self.send_response(403)
                self.end_headers()

            def log_message(self, *args):
                pass

        httpd = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return httpd.server_address[1]

    yield start
    for httpd in servers:
        httpd.shutdown()


def _status(port):
    out = subprocess.run(
        ['bash', '-c', f'source "{REPO}/deploy/airlock-env.sh" >/dev/null 2>&1; '
                       f'airlock_backend_port_status {port}; airlock_backend_port_problem {port}'],
        capture_output=True, text=True, timeout=60).stdout.strip().splitlines()
    return out[0], out[1:]


def test_airplay_is_named_with_its_fix(serve):
    status, problem = _status(serve('AirTunes/870.14.1'))
    assert status == 'airplay'
    assert 'AirPlay Receiver' in problem[0]
    assert 'AirDrop & Handoff > AirPlay Receiver: off' in problem[1]


def test_another_program_is_named(serve):
    status, problem = _status(serve('nginx'))
    assert status.startswith('other ')
    assert 'already in use' in problem[0]


def test_a_free_port_passes():
    status, problem = _status(_free_port())
    assert status == 'free' and problem == []
