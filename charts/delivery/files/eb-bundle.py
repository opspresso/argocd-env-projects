"""Bundle a verified JAR or WAR for native Elastic Beanstalk execution.

Usage: eb-bundle.py <application.json> <artifact file> <secrets path> <region> <account> <out.zip> [<install script>]
Secret values remain in Secrets Manager. EB resolves their ARNs using the instance's scoped IAM role.
"""
import json
import re
import sys
import zipfile
import io
from pathlib import Path

VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRETS_PATH = re.compile(r"^/agent-runway/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9]+$")


def bootstrap(format_, out):
    """Trusted preparation app: no user artifact, command, hook, config or secret reference."""
    if format_ not in ("jar", "war"):
        raise ValueError("native format must be jar or war")
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        if format_ == "jar":
            bundle.writestr("Procfile", "web: python3 runway-bootstrap.py\n")
            bundle.writestr("runway-bootstrap.py", """from http.server import BaseHTTPRequestHandler, HTTPServer
class Ready(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'Preparing deployment'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
HTTPServer(('0.0.0.0', 5000), Ready).serve_forever()
""")
        else:
            war = io.BytesIO()
            with zipfile.ZipFile(war, "w", compression=zipfile.ZIP_DEFLATED) as app:
                app.writestr("WEB-INF/web.xml", """<?xml version="1.0" encoding="UTF-8"?>
<web-app xmlns="http://xmlns.jcp.org/xml/ns/javaee" version="3.1">
  <servlet><servlet-name>bootstrap</servlet-name><jsp-file>/index.jsp</jsp-file></servlet>
  <servlet-mapping><servlet-name>bootstrap</servlet-name><url-pattern>/*</url-pattern></servlet-mapping>
</web-app>
""")
                app.writestr("index.jsp", '<%@ page contentType="text/plain" %>Preparing deployment')
            bundle.writestr("ROOT.war", war.getvalue())


def main(application_file, artifact_file, secrets_path, region, account, out, install_file=""):
    application = json.loads(Path(application_file).read_text())
    if not SECRETS_PATH.fullmatch(secrets_path) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]", region) or not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("invalid environment secret scope")
    runtime = application["runtime"]
    native = application["native"]
    format_ = native["format"]
    if format_ not in ("jar", "war"):
        raise ValueError("native format must be jar or war")
    port = runtime["port"]
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("invalid application port")
    if format_ == "war" and (port != 8080 or runtime.get("command") or runtime.get("args")):
        raise ValueError("Tomcat owns the WAR entrypoint and listens on port 8080")
    environment = {**application["environment"].get("env", {}), **application.get("config", {}), "PORT": str(port)}
    secrets = application["environment"].get("secrets", [])
    if any(not VARIABLE.fullmatch(name) for name in [*environment, *secrets]):
        raise ValueError("invalid variable name")
    if set(environment).intersection(secrets):
        raise ValueError("a secret also has a plaintext value")
    options = [{"namespace": "aws:elasticbeanstalk:application:environment", "option_name": name, "value": str(value)}
               for name, value in sorted(environment.items())]
    options += [{"namespace": "aws:elasticbeanstalk:application:environmentsecrets", "option_name": name,
                 "value": f"arn:aws:secretsmanager:{region}:{account}:secret:{secrets_path}/{name}"} for name in sorted(secrets)]
    if sum(len((item["option_name"] + item["value"]).encode()) for item in options) > 4096:
        raise ValueError("Elastic Beanstalk environment properties exceed 4096 bytes")
    files = [(".ebextensions/01-environment.config", json.dumps({"option_settings": options}), 0o100644)]
    if format_ == "jar":
        command = runtime.get("command") or ["java", "-jar", "application.jar"]
        command = [*command, *runtime.get("args", [])]
        if not command or not all(isinstance(arg, str) and "\x00" not in arg for arg in command):
            raise ValueError("invalid process command")
        files += [("Procfile", "web: python3 runway-launch.py\n", 0o100644),
                  ("runway-command.json", json.dumps(command), 0o100644),
                  ("runway-launch.py", "import json, os\nwith open('runway-command.json') as f: command = json.load(f)\nos.execvpe(command[0], command, os.environ)\n", 0o100644)]
    install = Path(install_file).read_bytes() if install_file else b""
    if install:
        for kind in ("hooks", "confighooks"):
            files.append((f".platform/{kind}/prebuild/00-install.sh", install, 0o100755))
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(artifact_file, "application.jar" if format_ == "jar" else "ROOT.war")
        for name, data, mode in files:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, data)

    if Path(out).stat().st_size > 500_000_000:
        raise ValueError("Elastic Beanstalk source bundle exceeds 500 MB")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "bootstrap":
        bootstrap(sys.argv[2], sys.argv[3])
        sys.exit(0)
    if len(sys.argv) not in (7, 8):
        sys.exit(__doc__)
    main(*sys.argv[1:])
