#!/usr/bin/env python3
"""Starts the Agent Runway app of an aws-asg instance (agent-platform docs/DELIVERY_V3.md D4).

The golden image installs this file as /usr/local/bin/runway-app. The launch template's user data writes
/etc/runway/app.json (every value from Agent Runway: image digest, port, health path, the non-secret Variables, command,
args, the SSM path of the environment's Variables) and runs `runway-app start`. The command logs in to the image's ECR
registry, reads the secret Variables from SSM, runs the container and waits until the health path answers with 2xx.
It exits 0 only then, so the user data signals CloudFormation success only for a healthy instance.

Variable values reach Docker through this process's environment (`docker run -e NAME`), never through a command line
or a file, so any value (newlines, quotes, `$`) arrives unchanged and stays out of the process list.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

CONFIG = os.environ.get("RUNWAY_APP_CONFIG", "/etc/runway/app.json")
CONTAINER = "runway-app"
IMAGE = re.compile(r"^(\d{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def run(*args, env=None, stdin=None):
    subprocess.run(args, check=True, env=env, input=stdin, text=True)


def secrets(path, region):
    """Every Variable under the environment's SSM path, decrypted, by name."""
    out = subprocess.run(
        ["aws", "ssm", "get-parameters-by-path", "--region", region, "--path", path, "--with-decryption",
         "--output", "json", "--query", "Parameters[].[Name,Value]"],
        check=True, capture_output=True, text=True).stdout
    return {name.rsplit("/", 1)[1]: value for name, value in json.loads(out)}


def start():
    with open(CONFIG) as file:
        config = json.load(file)
    image = config["image"]
    match = IMAGE.match(image)
    if not match:
        sys.exit(f"image must be an ECR image pinned by digest: {image}")
    port = int(config["port"])
    values = {"PORT": str(port), **config.get("env", {}), **secrets(config["secretsPath"], config["region"])}
    for name in values:
        if not VARIABLE.match(name):
            sys.exit(f"invalid variable name: {name}")

    registry = image.split("/", 1)[0]
    password = subprocess.run(["aws", "ecr", "get-login-password", "--region", match.group(2)],
                              check=True, capture_output=True, text=True).stdout
    run("docker", "login", "--username", "AWS", "--password-stdin", registry, stdin=password)
    run("docker", "pull", image)
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    args = ["docker", "run", "-d", "--name", CONTAINER, "--restart", "always", "-p", f"80:{port}"]
    for name in sorted(values):
        args += ["-e", name]
    if config.get("command"):
        args += ["--entrypoint", config["command"][0]]
    args.append(image)
    args += config.get("command", [])[1:] + config.get("args", [])
    run(*args, env={**os.environ, **values})

    url = f"http://127.0.0.1{config.get('health', '/health')}"
    deadline = time.time() + int(config.get("healthTimeoutSeconds", 600))
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if 200 <= response.status < 300:
                    print(f"{url} answered with {response.status}")
                    return
        except OSError:
            pass
        time.sleep(5)
    subprocess.run(["docker", "logs", "--tail", "50", CONTAINER])
    sys.exit(f"{url} did not answer with 2xx")


if __name__ == "__main__":
    if sys.argv[1:] != ["start"]:
        sys.exit("usage: runway-app start")
    start()
