import json
import os
import sys
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Dict, Any

from auth import TokenManager
from models import get_models, get_default_model
from translator import anthropic_to_antigravity, process_antigravity_stream

__version__ = '1.0.1'

ANTIGRAVITY_ENDPOINT = os.environ.get(
    'ANTIGRAVITY_ENDPOINT',
    'https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse'
)
DEFAULT_PORT = int(os.environ.get('PORT', 8317))
token_manager = TokenManager()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # ponytail: simple log without dumping secret tokens
        sys.stderr.write(f"[{self.log_date_time_string()}] {self.command} {self.path} - {format % args}\n")

    def _send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, anthropic-version, anthropic-beta, x-api-key')

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path in ('/health', '/'):
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok', 'proxy': 'aside-antigravity-proxy', 'version': __version__}).encode('utf-8'))
        elif self.path in ('/v1/models', '/models'):
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            active_models = get_models()
            models_res = {
                "data": [
                    {
                        "type": "model",
                        "id": m["id"],
                        "display_name": m["name"],
                        "created_at": "2025-01-01T00:00:00Z"
                    }
                    for m in active_models
                ],
                "has_more": False,
                "first_id": active_models[0]["id"] if active_models else "",
                "last_id": active_models[-1]["id"] if active_models else "",
            }
            self.wfile.write(json.dumps(models_res).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.rstrip('/') in ('/v1/messages', '/messages'):
            self.handle_messages()
        else:
            self.send_response(404)
            self.end_headers()

    def handle_messages(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body_bytes = self.rfile.read(content_length)

        try:
            anthropic_req = json.loads(body_bytes.decode('utf-8'))
        except Exception as e:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(json.dumps({'error': {'type': 'invalid_request_error', 'message': str(e)}}).encode('utf-8'))
            return

        is_stream = anthropic_req.get('stream', False)
        ag_payload = anthropic_to_antigravity(anthropic_req)
        requested_model = ag_payload.get('model', get_default_model())

        # Obtain OAuth token and make request to Antigravity
        resp_stream = self._call_antigravity(ag_payload)
        if resp_stream is None:
            self.send_response(502)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': {'type': 'api_error', 'message': 'Failed to reach Antigravity gateway'}}).encode('utf-8'))
            return

        if is_stream:
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            # An SSE body has no Content-Length, so closing the socket is the only
            # end-of-response signal. Advertising keep-alive left clients waiting
            # after message_stop — the caller saw the text but the request never
            # finished.
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True

            generator = process_antigravity_stream(resp_stream, model=requested_model, stream=True)
            for event_str in generator:
                try:
                    self.wfile.write(event_str.encode('utf-8'))
                    self.wfile.flush()
                except Exception:
                    break
        else:
            res_dict = process_antigravity_stream(resp_stream, model=requested_model, stream=False)
            res_bytes = json.dumps(res_dict).encode('utf-8')
            self.send_response(200)
            self._send_cors_headers()
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(res_bytes)))
            self.end_headers()
            self.wfile.write(res_bytes)

    def _call_antigravity(self, ag_payload: Dict[str, Any], retry_auth: bool = True):
        token = token_manager.get_access_token()
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'User-Agent': 'antigravity-cli'
        }
        data = json.dumps(ag_payload).encode('utf-8')
        req = urllib.request.Request(ANTIGRAVITY_ENDPOINT, data=data, headers=headers, method='POST')

        try:
            return urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry_auth:
                sys.stderr.write("[Auth] Received 401 from Antigravity, refreshing token...\n")
                token_manager.get_access_token(force_refresh=True)
                return self._call_antigravity(ag_payload, retry_auth=False)
            else:
                sys.stderr.write(f"[Error] Antigravity gateway error {e.code}: {e.read().decode('utf-8', errors='ignore')}\n")
                return None
        except Exception as ex:
            sys.stderr.write(f"[Error] Network error talking to Antigravity: {ex}\n")
            return None


def run_server(port: int = DEFAULT_PORT):
    server_address = ('127.0.0.1', port)
    try:
        httpd = ThreadedHTTPServer(server_address, ProxyHandler)
    except OSError as e:
        # ponytail: exit gracefully with code 0 so launchd (SuccessfulExit=false) stops looping
        sys.stderr.write(f"[Fatal] Failed to bind to 127.0.0.1:{port}: {e}\nExiting gracefully to prevent restart loop.\n")
        sys.exit(0)

    print(f"[*] Aside Antigravity Proxy Server running on http://127.0.0.1:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Server stopping...")
        httpd.server_close()


if __name__ == '__main__':
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    run_server(port)
