from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import threading
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "charts/delivery/files/http-health.sh"


class HealthStatus(unittest.TestCase):
    def test_only_2xx_is_healthy(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                status = int(self.path.removeprefix("/"))
                self.send_response(status)
                if status == 301:
                    self.send_header("Location", "/200")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            # The old curl -f condition accepts this redirect; a deployment must not call it 2xx.
            baseline = subprocess.run(["curl", "-fsS", "-o", "/dev/null", base + "/301"], capture_output=True)
            self.assertEqual(baseline.returncode, 0)
            for status in (200, 204, 299, 301, 304, 401, 500):
                result = subprocess.run(["sh", "-c", '. "$1"; http_healthy "$2"', "sh", str(SCRIPT), base + f"/{status}"], capture_output=True)
                self.assertEqual(result.returncode == 0, 200 <= status < 300, status)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
