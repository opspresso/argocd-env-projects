"""Writes the Elastic Beanstalk source bundle of one Deployment (app-deploy-eb, agent-platform docs/DELIVERY_V3.md D2).

Usage: eb-bundle.py <application JSON file> <image> <secrets path> <region> <out.zip>

The bundle holds docker-compose.yml (the Release image pinned by digest, PORT, the non-secret Variables, command and
args) and a prebuild hook that runs on every instance before the app starts: it logs in to the image's ECR registry and
writes the environment's secret Variables from SSM into docker-compose.override.yml, so secret values never enter the
bundle, the stack or the Workflow. Every input comes from Agent Runway; nothing comes from the app repository.
"""
import json
import re
import sys
import zipfile

IMAGE = re.compile(r"^(\d{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRETS_PATH = re.compile(r"^/agent-runway/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9]+$")


def compose_value(value: str) -> str:
    """A YAML double-quoted scalar (JSON is valid YAML) with `$` doubled, because Compose interpolates `$` in values."""
    return json.dumps(value.replace("$", "$$"))


def main(application_file: str, image: str, secrets_path: str, region: str, out: str) -> None:
    with open(application_file) as file:
        application = json.load(file)
    match = IMAGE.match(image)
    if not match:
        sys.exit(f"image must be an ECR image pinned by digest: {image}")
    if not SECRETS_PATH.match(secrets_path):
        sys.exit(f"secrets path is not an Agent Runway environment path: {secrets_path}")
    runtime = application["runtime"]
    port = int(runtime["port"])
    if not 1024 <= port <= 65535:
        sys.exit(f"runtime.port is out of range: {port}")
    environment = {"PORT": str(port), **application["environment"].get("env", {}), **application.get("config", {})}
    for name in environment:
        if not VARIABLE.match(name):
            sys.exit(f"invalid variable name: {name}")

    lines = ["services:", "  app:", f"    image: {json.dumps(image)}", "    restart: always",
             f"    ports: [{json.dumps(f'80:{port}')}]", "    environment:"]
    lines += [f"      {name}: {compose_value(value)}" for name, value in sorted(environment.items())]
    if runtime.get("command"):
        lines.append(f"    entrypoint: {json.dumps([value.replace('$', '$$') for value in runtime['command']])}")
    if runtime.get("args"):
        lines.append(f"    command: {json.dumps([value.replace('$', '$$') for value in runtime['args']])}")
    compose = "\n".join(lines) + "\n"

    registry = image.split("/", 1)[0]
    # Runs as root on the instance with its instance profile, which reads only this environment's SSM path.
    hook = f"""#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/../../.."
aws ecr get-login-password --region {match.group(2)} | docker login --username AWS --password-stdin {registry}
umask 077
aws ssm get-parameters-by-path --region {region} --path {secrets_path} --with-decryption --output json \\
  --query 'Parameters[].[Name,Value]' | python3 -c '
import json, sys
pairs = json.load(sys.stdin)
print("services:")
print("  app:")
print("    environment:" + ("" if pairs else " {{}}"))
for name, value in pairs:
    print("      " + json.dumps(name.rsplit("/", 1)[1]) + ": " + json.dumps(value.replace("$", "$$")))
' > docker-compose.override.yml
"""
    files = [("docker-compose.yml", compose, 0o100644)]
    # Elastic Beanstalk runs the hooks of application deployments and of configuration deployments.
    files += [(f".platform/{kind}/prebuild/01-runway.sh", hook, 0o100755) for kind in ("hooks", "confighooks")]
    with zipfile.ZipFile(out, "w") as bundle:
        for name, text, mode in files:
            info = zipfile.ZipInfo(name)
            info.create_system = 3  # Unix, so the mode bits apply
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, text)


if __name__ == "__main__":
    if len(sys.argv) != 6:
        sys.exit(__doc__)
    main(*sys.argv[1:])
