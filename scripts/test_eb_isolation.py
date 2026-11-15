import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"charts/delivery/files/{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


deploy = module("eb-deploy")
bundle = module("eb-bundle")
PARAMS = {"Project": "fixture", "Environment": "dev", "DeploymentId": "release",
          "VersionBucket": "private-source", "VersionKey": "fixture/dev/release.zip"}
ENV_ID = "e-0123456789"
ENV_NAME = "runway-fixture-dev"
STACK = "runway-fixture-dev"
ARN = "arn:aws:elasticbeanstalk:ap-northeast-2:123456789012:environment/runway-fixture-dev/" + ENV_NAME
OWN = "arn:aws:s3:::elasticbeanstalk-ap-northeast-2-123456789012/resources/environments/" + ENV_ID + "/*"


class AWS:
    def __init__(self, existing=False):
        self.calls = []
        self.current = {"StackStatus": "CREATE_COMPLETE", "Outputs": [{"OutputKey": "EnvironmentName", "OutputValue": ENV_NAME}], "Parameters": []} if existing else None
        self.fail_scope = False
        self.broad = False
        self.wrong_owner = False

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("cloudformation", "describe-stacks"):
            return {"Stacks": [self.current]} if self.current else None
        if args[:2] == ("cloudformation", "deploy"):
            values = dict(value.split("=", 1) for value in args[args.index("--parameter-overrides") + 1:])
            if self.fail_scope and set(values) == {"EbEnvironmentId", "EbEnvironmentName"}:
                raise RuntimeError("scope update failed")
            previous = {item["ParameterKey"]: item["ParameterValue"] for item in (self.current or {}).get("Parameters", [])}
            self.current = {"StackStatus": "UPDATE_COMPLETE", "Outputs": [{"OutputKey": "EnvironmentName", "OutputValue": ENV_NAME}],
                            "Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in {**previous, **values}.items()]}
            return {}
        if args[:2] == ("elasticbeanstalk", "describe-environments"):
            return {"Environments": [{"ApplicationName": "other" if self.wrong_owner else STACK, "EnvironmentName": ENV_NAME,
                                      "EnvironmentId": ENV_ID, "EnvironmentArn": ARN}]}
        if args[:2] == ("iam", "list-attached-role-policies"):
            return {"AttachedPolicies": [{"PolicyName": "AWSElasticBeanstalkWebTier"}] if self.broad else []}
        if args[:2] == ("iam", "get-role-policy"):
            return {"PolicyDocument": {"Statement": [{"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": OWN}]}}
        if args[:2] == ("s3", "cp"):
            return {}
        raise AssertionError(args)


class Isolation(unittest.TestCase):
    def run_deploy(self, fake):
        deploy.deploy(PARAMS, "ap-northeast-2", "/user.zip", "/bootstrap.zip", fake, lambda _: None)

    def test_new_environment_scopes_before_user_upload_and_execution(self):
        fake = AWS()
        self.run_deploy(fake)
        updates = [call for call in fake.calls if call[:2] == ("cloudformation", "deploy")]
        self.assertEqual(len(updates), 3)
        self.assertIn("Bootstrap=true", updates[0])
        self.assertIn("VersionKey=fixture/dev/_bootstrap/release.zip", updates[0])
        scoped = updates[1][updates[1].index("--parameter-overrides") + 1:]
        self.assertEqual(set(scoped), {f"EbEnvironmentId={ENV_ID}", f"EbEnvironmentName={ENV_NAME}"})
        self.assertIn("Bootstrap=false", updates[2])
        # The target role grants S3 writes only below its Project/environment session-tag prefix.
        for call in fake.calls:
            if call[:2] == ("s3", "cp"):
                self.assertTrue(call[-1].startswith("s3://private-source/fixture/dev/"))
        user_upload = next(c for c in fake.calls if "/user.zip" in c)
        policy_read = next(c for c in fake.calls if c[:2] == ("iam", "get-role-policy"))
        self.assertLess(fake.calls.index(policy_read), fake.calls.index(user_upload))
        self.assertLess(fake.calls.index(user_upload), fake.calls.index(updates[2]))

    def test_existing_environment_preserves_current_code_during_scoping(self):
        fake = AWS(existing=True)
        self.run_deploy(fake)
        self.assertFalse(any("/bootstrap.zip" in call for call in fake.calls))
        updates = [c for c in fake.calls if c[:2] == ("cloudformation", "deploy")]
        self.assertEqual(len(updates), 2)
        self.assertFalse(any(value.startswith("VersionKey=") for value in updates[0]))

    def test_scope_failure_or_broad_policy_never_publishes_user_bundle(self):
        for setting in ("fail_scope", "broad", "wrong_owner"):
            fake = AWS(existing=True)
            setattr(fake, setting, True)
            with self.assertRaises((RuntimeError, ValueError)):
                self.run_deploy(fake)
            self.assertFalse(any("/user.zip" in call for call in fake.calls), setting)

    def test_only_missing_stack_is_absence(self):
        for error in ("AccessDenied: not authorized", "Could not connect", "(ValidationError) invalid parameter"):
            with patch.object(deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", error)):
                with self.assertRaises(RuntimeError):
                    deploy.aws("cloudformation", "describe-stacks", "--stack-name", STACK)
        with patch.object(deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "(ValidationError) Stack with id runway-fixture-dev does not exist")):
            self.assertIsNone(deploy.aws("cloudformation", "describe-stacks", "--stack-name", STACK))

    def test_bootstrap_bundles_have_only_fixed_platform_code(self):
        with tempfile.TemporaryDirectory() as directory:
            for format_ in ("jar", "war"):
                file = Path(directory) / f"{format_}.zip"
                bundle.bootstrap(format_, file)
                with zipfile.ZipFile(file) as z:
                    self.assertNotIn(".ebextensions/01-environment.config", z.namelist())
                    self.assertFalse(any("install" in name for name in z.namelist()))
                    if format_ == "jar":
                        compile(z.read("runway-bootstrap.py"), "bootstrap", "exec")
                        self.assertEqual(z.read("Procfile"), b"web: python3 runway-bootstrap.py\n")
                    else:
                        with zipfile.ZipFile(io.BytesIO(z.read("ROOT.war"))) as war:
                            root = ET.fromstring(war.read("WEB-INF/web.xml"))
                            self.assertEqual(root.find(".//{*}url-pattern").text, "/*")
                            self.assertEqual(root.find(".//{*}jsp-file").text, "/index.jsp")


if __name__ == "__main__":
    unittest.main()
