#!/bin/bash
# The custom data of an azure-vmss instance (vmss-app.json). cloud-init runs it once, as root, on each new instance.
# vmss-deploy.py replaces __RUNWAY_BOOT__ with the base64 JSON of this Deployment: the bundle blobs and their SHA-256,
# the app identity's client ID, the Key Vault, the Front Door ID, the Java runtime, the port, the health path and the
# Target's install script. It holds no secret value; the instance reads the secrets with its own identity.
# Every failure stops the script: the app never starts, the instance stays unhealthy and the rolling upgrade keeps the
# previous Release. The log is /var/log/cloud-init-output.log; it never shows a value.
set -euo pipefail
umask 022

boot=/opt/runway/boot.json
install -d -m 0755 /opt/runway /etc/runway /etc/apt/keyrings
printf '%s' '__RUNWAY_BOOT__' | base64 -d > "$boot"
chmod 0600 "$boot"
read_boot() { python3 -I -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$boot" "$1"; }

# The Java runtime from the Adoptium repository, signed by the key that the chart ships (adoptium.asc).
read_boot adoptiumKey > /etc/apt/keyrings/adoptium.asc
codename=$(. /etc/os-release && printf '%s' "$VERSION_CODENAME")
printf 'deb [signed-by=/etc/apt/keyrings/adoptium.asc] https://packages.adoptium.net/artifactory/deb %s main\n' "$codename" \
  > /etc/apt/sources.list.d/adoptium.list
export DEBIAN_FRONTEND=noninteractive
apt_get() { apt-get -o DPkg::Lock::Timeout=600 -o Acquire::Retries=5 "$@"; }
apt_get update
apt_get install -y --no-install-recommends nginx "$(read_boot javaRuntime)"

# The Target's install script runs before the app, like the prebuild hook of an aws-eb Target. A non-zero exit stops here.
read_boot installScript | base64 -d > /opt/runway/install.sh
if [ -s /opt/runway/install.sh ]; then
  chmod 0700 /opt/runway/install.sh
  echo "Running the Target install script"
  /opt/runway/install.sh
fi

id runway >/dev/null 2>&1 || useradd --system --home-dir /opt/runway/app --shell /usr/sbin/nologin runway
install -d -o runway -g runway -m 0750 /opt/runway/app

# Downloads the bundle, reads the declared secrets and writes the app, the launcher, the unit and the nginx site.
python3 -I - "$boot" <<'PYTHON'
import hashlib
import json
import os
import pwd
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

boot = json.load(open(sys.argv[1]))
# The identity's role assignments can take minutes to apply after the workflow created them.
deadline = time.monotonic() + 900
tokens = {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("redirects are refused")


client = urllib.request.build_opener(NoRedirect)


def call(what, url, headers):
    """Opens url until it answers 2xx. Only the status reaches the log, never a body."""
    while True:
        try:
            return client.open(urllib.request.Request(url, headers=headers), timeout=60)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
        except (urllib.error.URLError, TimeoutError) as error:
            status = type(error).__name__
        if time.monotonic() > deadline:
            raise SystemExit(f"{what} failed: {status}")
        print(f"Waiting for {what}: {status}", flush=True)
        time.sleep(10)


def token(resource):
    if resource not in tokens:
        query = urllib.parse.urlencode({"api-version": "2018-02-01", "resource": resource, "client_id": boot["clientId"]})
        with call(f"identity token for {resource}", f"http://169.254.169.254/metadata/identity/oauth2/token?{query}", {"Metadata": "true"}) as response:
            tokens[resource] = json.load(response)["access_token"]
    return tokens[resource]


def blob(name, destination, sha256, size=None):
    url = f"https://{boot['storage']}.blob.core.windows.net/{boot['container']}/{name}"
    headers = {"Authorization": "Bearer " + token("https://storage.azure.com/"), "x-ms-version": "2023-11-03"}
    digest, total = hashlib.sha256(), 0
    with call(f"blob {name}", url, headers) as response, open(destination + ".part", "wb") as output:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != sha256 or (size is not None and total != size):
        raise SystemExit(f"blob {name} does not match its recorded SHA-256 and size")
    os.replace(destination + ".part", destination)


def secret(name):
    url = f"https://{boot['keyVault']}.vault.azure.net/secrets/{name}?api-version=7.5"
    with call(f"secret {name}", url, {"Authorization": "Bearer " + token("https://vault.azure.net")}) as response:
        value = json.load(response).get("value")
    if not isinstance(value, str):
        raise SystemExit(f"secret {name} has no value")
    return value


def write(path, text, mode, owner="root"):
    user = pwd.getpwnam(owner)
    descriptor = os.open(path + ".part", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w") as output:
        output.write(text)
    os.chown(path + ".part", user.pw_uid, user.pw_gid)
    os.chmod(path + ".part", mode)
    os.replace(path + ".part", path)


runway = pwd.getpwnam("runway")
jar = "/opt/runway/app/application.jar"
blob(boot["jar"]["blob"], jar, boot["jar"]["sha256"], boot["jar"]["size"])
os.chown(jar, 0, runway.pw_gid)
os.chmod(jar, 0o640)
blob(boot["config"]["blob"], "/opt/runway/runway.json", boot["config"]["sha256"])
config = json.load(open("/opt/runway/runway.json"))
environment = dict(config["env"])
for name, secret_name in config["secrets"].items():
    environment[name] = secret(secret_name)
print(f"Read {len(config['secrets'])} secret(s) of the app", flush=True)
# Readable only by the service user; the launcher passes it to the app as its environment.
write("/etc/runway/env.json", json.dumps(environment), 0o400, "runway")
write("/opt/runway/command.json", json.dumps(config["command"]), 0o644)
write("/opt/runway/launch.py", """import json, os
environment = dict(os.environ)
with open('/etc/runway/env.json') as file:
    environment.update(json.load(file))
with open('/opt/runway/command.json') as file:
    command = json.load(file)
os.execvpe(command[0], command, environment)
""", 0o644)
write("/etc/systemd/system/runway-app.service", """[Unit]
Description=Agent Runway app
After=network-online.target
Wants=network-online.target

[Service]
User=runway
Group=runway
WorkingDirectory=/opt/runway/app
ExecStart=/usr/bin/python3 -I /opt/runway/launch.py
Restart=always
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=yes
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
""", 0o644)

port, health, front_door = boot["port"], boot["health"], boot["frontDoorId"]
# Port 80 takes only requests of this Front Door profile (X-Azure-FDID) and the load balancer probe (168.63.129.16, the
# Azure platform address). The subnet's network security group admits only AzureFrontDoor.Backend on port 80, which
# other Front Door profiles share. 127.0.0.1:61000 answers the Application Health extension in its rich-state form
# from the app's health path: any 2xx is Healthy, anything else (also a redirect) is no answer.
write("/etc/nginx/runway-health.json", '{"ApplicationHealthState":"Healthy"}', 0o644)
write("/etc/nginx/conf.d/runway.conf", f"""map $http_x_azure_fdid $runway_front_door {{
    default 0;
    "{front_door}" 1;
}}
geo $runway_probe {{
    default 0;
    168.63.129.16/32 1;
}}
map "$runway_front_door$runway_probe" $runway_denied {{
    "00" 1;
    default 0;
}}
map $http_x_forwarded_proto $runway_proto {{
    "" http;
    default $http_x_forwarded_proto;
}}
map $http_upgrade $runway_connection {{
    "" "";
    default upgrade;
}}
server {{
    listen 80 default_server;
    server_name _;
    location / {{
        if ($runway_denied) {{
            return 403;
        }}
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $runway_proto;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $runway_connection;
    }}
}}
server {{
    listen 127.0.0.1:61000;
    # A file, not `return`: return runs in the rewrite phase, before auth_request could refuse the request.
    location = /runway-health {{
        auth_request /runway-app-health;
        default_type application/json;
        alias /etc/nginx/runway-health.json;
    }}
    location = /runway-app-health {{
        internal;
        proxy_pass http://127.0.0.1:{port}{health};
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_connect_timeout 2s;
        proxy_read_timeout 5s;
    }}
}}
""", 0o644)
PYTHON

rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl daemon-reload
systemctl enable --now runway-app.service
systemctl reload nginx
echo "Started Deployment $(read_boot deployment)"
