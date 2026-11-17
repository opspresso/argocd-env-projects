"""Runs the azure-vmss boot script (vmss-boot.sh) in an Ubuntu 24.04 container (manual check, not in test.sh).

Usage: python3 scripts/vmss_boot_check.py [amd64|arm64]   (needs Docker and internet access for apt; takes a few minutes)
The container runs the real apt steps (nginx and the Temurin JRE from the Adoptium repository with the shipped key) and
the install script. The Python block runs with IMDS, Blob Storage and Key Vault answered locally, then a stand-in app
takes the app port. The check fails unless nginx admits only this Front Door profile, the health endpoint follows the
app's health path, the app receives its secret only through env.json, and other users cannot read env.json.
"""
import base64
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_vmss as fixture  # noqa: E402

vmss = fixture.vmss

DRIVER = r'''
import io, json, re, sys, urllib.request
script = open("/t/boot.sh").read()
block = re.search(r"<<'PYTHON'\n(.*?)\nPYTHON\n", script, re.S).group(1)
files = {"application.jar": open("/t/jar", "rb").read(), "runway.json": open("/t/runway.json", "rb").read()}
class Answer(io.BytesIO):
    status = 200
    def __enter__(self): return self
    def __exit__(self, *a): self.close()
class Opener:
    def open(self, request, timeout=None):
        url = request.full_url
        if url.startswith("http://169.254.169.254/"):
            assert request.headers.get("Metadata") == "true" and "client_id=cccccccc" in url
            return Answer(json.dumps({"access_token": "token"}).encode())
        if ".blob.core.windows.net/runway-todo-dmz/" in url:
            return Answer(files[url.rsplit("/", 1)[1]])
        if ".vault.azure.net/secrets/ar-" in url:
            return Answer(json.dumps({"value": open("/t/secret").read()}).encode())
        raise AssertionError(url)
urllib.request.build_opener = lambda *a: Opener()
sys.argv = ["boot", "/opt/runway/boot.json"]
exec(compile(block, "boot", "exec"), {"__name__": "__main__"})
'''

APP = r'''
import http.server, os
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = "|".join([os.environ.get("DB_PASSWORD", ""), os.environ.get("PORT", ""), self.headers.get("Host", "")]).encode()
        self.send_response(302 if os.path.exists("/tmp/redirect") else 200 if self.path in ("/actuator/health", "/") else 404)
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args): pass
http.server.HTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
'''

RUN = r'''
set -eu
apt-get update -qq >/dev/null && apt-get install -y -qq curl ca-certificates python3 procps >/dev/null 2>&1
# Everything before the Python block, as cloud-init runs it. Azure's Ubuntu images already have python3 and curl.
sed -n '1,/^python3 -I - "\$boot" <<.PYTHON.$/p' /t/boot.sh | sed '$d' > /tmp/pre.sh
bash /tmp/pre.sh > /tmp/pre.log 2>&1 || { tail -30 /tmp/pre.log; exit 1; }
check() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1 ($3)"; failed=1; fi; }
failed=0
check "the install script ran" install-script-ran "$(cat /opt/runway/installed)"
check "the Temurin JRE is installed" 0 "$(java -version >/dev/null 2>&1; echo $?)"
python3 /t/driver.py >/dev/null
rm -f /etc/nginx/sites-enabled/default
nginx -t >/dev/null 2>&1 && nginx
check "env.json belongs to the service user only" "runway 400" "$(stat -c '%U %a' /etc/runway/env.json)"
cp /t/app.py /opt/runway/app/app.py
printf '["python3", "/opt/runway/app/app.py"]' > /opt/runway/command.json
su -s /bin/sh runway -c 'cd /opt/runway/app && exec python3 -I /opt/runway/launch.py' &
sleep 2
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
fdid="X-Azure-FDID: $(cat /t/fdid)"
check "port 80 refuses a request without X-Azure-FDID" 403 "$(code http://127.0.0.1/)"
check "port 80 refuses another Front Door profile" 403 "$(code -H 'X-Azure-FDID: 11111111-1111-1111-1111-111111111111' http://127.0.0.1/)"
check "port 80 passes this profile's request with its host" "$(cat /t/secret)|8080|todo-dmz.dmz.rovr.dev" \
  "$(curl -s -H "$fdid" -H 'Host: todo-dmz.dmz.rovr.dev' http://127.0.0.1/)"
check "the health endpoint reports Healthy" '{"ApplicationHealthState":"Healthy"} 200' "$(curl -s -w ' %{http_code}' http://127.0.0.1:61000/runway-health)"
touch /tmp/redirect
check "a redirecting health path is not healthy" 500 "$(code http://127.0.0.1:61000/runway-health)"
rm /tmp/redirect
pkill -f /opt/runway/app/app.py; sleep 1
check "a stopped app is not healthy" 500 "$(code http://127.0.0.1:61000/runway-health)"
check "other users cannot read env.json" 1 "$(su -s /bin/sh nobody -c 'cat /etc/runway/env.json' >/dev/null 2>&1; echo $?)"
exit $failed
'''


def main(architecture=None):
    plan = vmss.plan_of(fixture.environ())
    jar = b"stand-in jar"
    plan["artifact"] = {"sha256": hashlib.sha256(jar).hexdigest(), "size": len(jar)}
    config = json.dumps(plan["runway"], sort_keys=True).encode()
    install = b"#!/bin/bash\necho install-script-ran > /opt/runway/installed\n"
    data = vmss.boot_script(plan, "cccccccc-0000-4000-8000-000000000002", fixture.FDID, hashlib.sha256(config).hexdigest(), install)
    with tempfile.TemporaryDirectory() as work:
        root = Path(work)
        files = {"boot.sh": base64.b64decode(data), "runway.json": config, "jar": jar, "secret": fixture.SECRET_VALUE.encode(),
                 "fdid": fixture.FDID.encode(), "driver.py": DRIVER.encode(), "app.py": APP.encode(), "run.sh": RUN.encode()}
        for name, content in files.items():
            (root / name).write_bytes(content)
        sys.exit(subprocess.run(["docker", "run", "--rm", *(["--platform", f"linux/{architecture}"] if architecture else []), "-v", f"{root}:/t:ro", "ubuntu:24.04", "bash", "/t/run.sh"]).returncode)


if __name__ == "__main__":
    main(*sys.argv[1:2])
