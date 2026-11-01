#!/usr/bin/env python3
"""
Lightweight mock backend server for the clean-room Onlook remake.
Provides endpoints captured in the network evidence:
- /api/auth/get-session
- /repos/onlook-dev/onlook
- /repos/onlook-dev/onlook/contributors
- /monitoring
"""

from http.server import HTTPServer, SimpleHTTPRequestHandler
import json
import os

PORT = 8080

class OnlookMockHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # Enable CORS for local testing
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        # Auth session mock endpoint
        if self.path.startswith('/api/auth/get-session'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            payload = {
                "session": {
                    "user": {
                        "name": "Design Engineer",
                        "email": "engineer@workspace.local"
                    },
                    "role": "designer",
                    "authenticated": True
                }
            }
            self.wfile.write(json.dumps(payload).encode('utf-8'))
            return

        # GitHub repository information endpoint
        if self.path == '/repos/onlook-dev/onlook':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            payload = {
                "name": "onlook",
                "full_name": "onlook-dev/onlook",
                "stargazers_count": 8240,
                "forks_count": 624,
                "open_issues_count": 32,
                "description": "The Developer Tool for Visual Engineers. Local-first React visual workspace."
            }
            self.wfile.write(json.dumps(payload).encode('utf-8'))
            return

        # GitHub contributors list
        if self.path.startswith('/repos/onlook-dev/onlook/contributors'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            payload = [
                {"login": "kiwi-dev", "contributions": 128},
                {"login": "alex-ui", "contributions": 94},
                {"login": "react-architect", "contributions": 65}
            ]
            self.wfile.write(json.dumps(payload).encode('utf-8'))
            return

        # Default static file serving
        return super().do_GET()

    def do_POST(self):
        # Monitoring telemetry endpoint observed in traffic
        if self.path.startswith('/monitoring') or self.path.startswith('/ingest/flags/'):
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                _ = self.rfile.read(content_length)

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
            return

        self.send_response(200)
        self.end_headers()

def run():
    server_address = ('', PORT)
    httpd = HTTPServer(server_address, OnlookMockHandler)
    print(f"[*] Onlook clean-room server running on http://localhost:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Server stopped.")

if __name__ == '__main__':
    run()