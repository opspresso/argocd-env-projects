import importlib.util
import io
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
import urllib.error
import zipfile
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("publisher", ROOT / "charts/delivery/files/artifacts.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
bundle_spec = importlib.util.spec_from_file_location("bundle", ROOT / "charts/delivery/files/eb-bundle.py")
bundle = importlib.util.module_from_spec(bundle_spec)
bundle_spec.loader.exec_module(bundle)


class Response(io.BytesIO):
    status = 200


class Registry:
    def __init__(self):
        self.data = None
        self.puts = 0
        self.failure = None

    def open(self, request, timeout):
        if self.failure:
            raise urllib.error.HTTPError(request.full_url, self.failure, "refused", {}, None)
        if request.get_method() == "PUT":
            self.data = request.data.read()
            self.puts += 1
            return Response()
        if self.data is None:
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
        return Response(self.data)


class NativeArtifacts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.build = {"context": ".", "output": "app.jar", "format": "jar"}
        with zipfile.ZipFile(self.root / "app.jar", "w") as jar:
            jar.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nMain-Class: example.App\n")
        self.registry = Registry()

    def publish(self):
        with patch.object(publisher.urllib.request, "build_opener", return_value=self.registry):
            return publisher.publish(self.root, self.build, "https://nexus.example/repository/runway-artifacts", "app-one", "main",
                                     "12345678-1234-1234-1234-123456789012", "publisher", "test-password")

    def test_upload_readback_and_idempotence(self):
        first = self.publish()
        self.assertEqual(first, self.publish())
        self.assertEqual(self.registry.puts, 1)
        self.assertEqual(first["size"], len(self.registry.data))
        self.assertEqual(first["sha256"], publisher.hashlib.sha256(self.registry.data).hexdigest())
        self.registry.data = b"other content"
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.publish()
        self.assertEqual(self.registry.puts, 1)

    def test_auth_and_server_failures_never_trigger_upload(self):
        for status in (401, 403, 429, 503):
            self.registry.failure = status
            with self.assertRaisesRegex(RuntimeError, str(status)):
                self.publish()
        self.assertEqual(self.registry.puts, 0)

    def test_output_cannot_follow_links_or_escape(self):
        (self.root / "link.jar").symlink_to(self.root / "app.jar")
        for output in ("link.jar", "../outside.jar", "/etc/passwd"):
            with self.subTest(output=output):
                self.build["output"] = output
                with self.assertRaises(ValueError):
                    self.publish()
        self.assertEqual(self.registry.puts, 0)

    def test_jar_requires_entrypoint_and_war_requires_web_application(self):
        with zipfile.ZipFile(self.root / "app.jar", "w") as jar:
            jar.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        with self.assertRaisesRegex(ValueError, "not executable"):
            self.publish()
        self.build = {"context": ".", "output": "app.war", "format": "war"}
        with zipfile.ZipFile(self.root / "app.war", "w") as war:
            war.writestr("WEB-INF/web.xml", "<web-app/>")
        self.assertEqual(self.publish()["format"], "war")

    def test_redirects_are_refused(self):
        with self.assertRaisesRegex(ValueError, "redirects"):
            publisher.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example/")

    def test_download_pins_project_hash_and_size(self):
        artifact = self.publish()
        with patch.object(publisher.urllib.request, "build_opener", return_value=self.registry):
            publisher.download(artifact, "https://nexus.example/repository/runway-artifacts", "app-one", "reader", "password", self.root / "download.jar")
            self.assertEqual((self.root / "download.jar").read_bytes(), self.registry.data)
            with self.assertRaisesRegex(ValueError, "outside"):
                publisher.download(artifact, "https://nexus.example/repository/runway-artifacts", "other-project", "reader", "password", self.root / "download.jar")
            self.registry.data += b"changed"
            with self.assertRaisesRegex(ValueError, "size"):
                publisher.download(artifact, "https://nexus.example/repository/runway-artifacts", "app-one", "reader", "password", self.root / "download.jar")


class NativeBundle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifact = self.root / "input.jar"
        self.artifact.write_bytes(b"verified-artifact-bytes")
        self.application = {"runtime": {"port": 8080}, "native": {"format": "jar", "javaVersion": 21},
                            "environment": {"env": {"GREETING": "a $HOME 'quoted'"}, "secrets": ["API_KEY"]}, "config": {}}

    def make(self, install=""):
        path = self.root / "app.json"
        path.write_text(json.dumps(self.application))
        bundle.main(path, self.artifact, "/agent-runway/eb-demo/app/dev", "ap-northeast-2", "123456789012", self.root / "bundle.zip", install)
        return zipfile.ZipFile(self.root / "bundle.zip")

    def test_jar_bytes_native_command_and_secret_references(self):
        self.application["runtime"]["command"] = [sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))"]
        self.application["runtime"]["args"] = ["$HOME", "hello ' world", "$(exit 1)"]
        with self.make() as archive:
            self.assertEqual(archive.read("application.jar"), self.artifact.read_bytes())
            self.assertNotIn("docker-compose.yml", archive.namelist())
            options = json.loads(archive.read(".ebextensions/01-environment.config"))["option_settings"]
            secret = next(x for x in options if x["option_name"] == "API_KEY")
            self.assertEqual(secret["namespace"], "aws:elasticbeanstalk:application:environmentsecrets")
            self.assertEqual(secret["value"], "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:/agent-runway/eb-demo/app/dev/API_KEY")
            archive.extractall(self.root / "expanded")
        process = subprocess.run([sys.executable, "runway-launch.py"], cwd=self.root / "expanded", capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(process.stdout), self.application["runtime"]["args"])

    def test_war_uses_tomcat_root_and_preserves_install_hooks(self):
        self.application["native"]["format"] = "war"
        install = self.root / "install.sh"
        install.write_text("#!/bin/sh\nexit 0\n")
        with self.make(str(install)) as archive:
            self.assertEqual(archive.read("ROOT.war"), self.artifact.read_bytes())
            self.assertNotIn("Procfile", archive.namelist())
            for kind in ("hooks", "confighooks"):
                name = f".platform/{kind}/prebuild/00-install.sh"
                self.assertEqual(archive.read(name), install.read_bytes())
                self.assertEqual(archive.getinfo(name).external_attr >> 16, 0o100755)
        self.application["runtime"]["port"] = 9090
        with self.assertRaisesRegex(ValueError, "8080"):
            self.make()


    def test_plain_secret_collision_and_property_limit_fail(self):
        self.application["config"]["API_KEY"] = "should-not-enter-bundle"
        with self.assertRaisesRegex(ValueError, "plaintext"):
            self.make()
        self.application["config"] = {"LARGE": "x" * 4096}
        with self.assertRaisesRegex(ValueError, "4096"):
            self.make()


class RegistrySecret(unittest.TestCase):
    def test_helm_and_external_secret_template_preserve_registry_credentials(self):
        values = {"app": {"controller": {"enabled": True}, "image": {"reference": "nexus.example/runway-images/app@sha256:" + "a" * 64},
                          "routing": {"hosts": ["test-dev.apps.example"]},
                          "namespace": {"labels": {"agentops.clush.net/delivery-project": "test", "agentops.clush.net/delivery-env": "dev"}},
                          "tenant": {"project": "test", "environment": "dev", "domain": "apps.example", "cluster": "eks-demo",
                                     "tenantSecretsRoleArn": "arn:aws:iam::123456789012:role/agent-runway-tenant-secrets"}}}
        generated = subprocess.check_output(["jq", "-n", "--argjson", "application", json.dumps({"runtime": {"port": 8080, "health": "/health"},
            "environment": {"replicas": 1, "cpu": "100m", "memory": "128Mi"}}), "--arg", "deployment", "test", "--arg", "host", "test-dev.apps.example",
            "--arg", "image", values["app"]["image"]["reference"], "-f", str(ROOT / "charts/delivery/files/render.jq")], text=True)
        values["app"].update(json.loads(generated)["app"])
        rendered = subprocess.run(["helm", "template", "app", str(ROOT / "charts/app"), "--namespace", "app-test-dev", "-f", "-"],
                                  input=json.dumps(values), capture_output=True, text=True, check=True).stdout
        secret = next(x for x in yaml.safe_load_all(rendered) if x and x.get("kind") == "ExternalSecret" and x["metadata"]["name"] == "nexus-reader")
        self.assertEqual(secret["spec"]["secretStoreRef"], {"kind": "SecretStore", "name": "agent-runway"})
        self.assertEqual(secret["spec"]["data"][0]["remoteRef"]["key"], "/k8s/common/nexus/runway-reader-password")
        pattern = secret["spec"]["target"]["template"]["data"][".dockerconfigjson"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "templates").mkdir()
            (root / "Chart.yaml").write_text("apiVersion: v2\nname: credential-template\nversion: 0.1.0\n")
            (root / "templates/result.yaml").write_text('apiVersion: v1\nkind: ConfigMap\nmetadata: {name: result}\ndata:\n  result: {{ tpl .Values.pattern (dict "password" .Values.password) | quote }}\n')
            password = 'synthetic-"quote"-$value\\slash'
            output = subprocess.run(["helm", "template", "check", directory, "-f", "-"], input=json.dumps({"pattern": pattern, "password": password}),
                                    capture_output=True, text=True, check=True).stdout
        result = json.loads(yaml.safe_load(output)["data"]["result"])
        auth = result["auths"]["nexus.example"]["auth"]
        self.assertEqual(publisher.base64.b64decode(auth).decode(), "runway-reader:" + password)



if __name__ == "__main__":
    unittest.main()
