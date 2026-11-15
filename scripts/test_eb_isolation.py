import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import xml.etree.ElementTree as ET
import yaml

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
BUCKET = "arn:aws:s3:::elasticbeanstalk-ap-northeast-2-123456789012"
PREFIXES = [f"resources/environments/{ENV_ID}/*", *[f"resources/environments/logs/{kind}/{ENV_ID}/*" for kind in ("tail", "bundle", "publish", "deployments")]]
OWN = [BUCKET + "/" + prefix for prefix in PREFIXES]


class AWS:
    def __init__(self, existing=False):
        self.calls = []
        self.current = {"StackStatus": "CREATE_COMPLETE", "Outputs": [{"OutputKey": "EnvironmentName", "OutputValue": ENV_NAME}], "Parameters": []} if existing else None
        self.fail_scope = False
        self.broad = False
        self.wrong_owner = False
        self.missing_deny = False
        self.missing_managed_list = False

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
            if self.missing_managed_list:
                return {}
            return {"AttachedPolicies": [{"PolicyName": "AWSElasticBeanstalkWebTier"}] if self.broad else []}
        if args[:2] == ("iam", "get-role-policy"):
            if args[-1] == "runway-app":
                return {"PolicyDocument": {"Statement": [{"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": OWN[0]}]}}
            statements = []
            if not self.missing_deny:
                condition = {"StringEquals": {"s3:ResourceAccount": "123456789012"}}
                statements += [
                    {"Sid": "DenyOtherEnvironmentObjects", "Effect": "Deny", "Action": ["s3:GetObject*"], "NotResource": OWN, "Condition": condition},
                    {"Sid": "DenyArtifactWrites", "Effect": "Deny", "Action": ["s3:PutObject*", "s3:DeleteObject*"], "NotResource": OWN[1:], "Condition": condition},
                    {"Sid": "DenyOtherEnvironmentListing", "Effect": "Deny", "Action": ["s3:ListBucket", "s3:ListBucketVersions"], "Resource": BUCKET,
                     "Condition": {"StringNotLike": {"s3:prefix": PREFIXES}}},
                ]
            return {"PolicyDocument": {"Statement": statements}}
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
        for setting in ("fail_scope", "broad", "wrong_owner", "missing_deny", "missing_managed_list"):
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
        for invalid in ({}, {"Stacks": []}):
            with self.assertRaises((KeyError, IndexError)):
                deploy.describe_stack(STACK, lambda *args: invalid)

    def test_remove_and_verify_never_turn_lookup_errors_into_success(self):
        workflow = yaml.safe_load((ROOT / "charts/delivery/files/app-remove-eb.yaml").read_text())
        scripts = {t["name"]: t["container"]["args"][0] for t in workflow["spec"]["templates"] if "container" in t}
        fake = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] == ['sts', 'assume-role']:
    print(json.dumps({'Credentials': {'AccessKeyId':'fixture', 'SecretAccessKey':'fixture', 'SessionToken':'fixture'}}))
elif args[:2] == ['cloudformation', 'describe-stacks']:
    mode = os.environ['FAKE_STACK']
    if mode in ('denied', 'network', 'missing'):
        print({'denied':'(AccessDenied) Not authorized', 'network':'Could not connect to endpoint',
               'missing':'(ValidationError) Stack with id fixture does not exist'}[mode], file=sys.stderr)
        sys.exit(1)
    status = {'present':'CREATE_COMPLETE', 'deleting':'DELETE_IN_PROGRESS', 'deleted':'DELETE_COMPLETE'}.get(mode)
    print(json.dumps({} if mode == 'empty' else {'Stacks':[{'StackStatus':status}]}))
elif args[:2] in (['cloudformation', 'delete-stack'], ['cloudformation', 'wait']):
    with open(os.environ['FAKE_ACTIONS'], 'a') as f: f.write(args[1]+'\\n')
else:
    sys.exit(99)
'''
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "aws"
            executable.write_text(fake)
            executable.chmod(0o755)
            actions = Path(directory) / "actions"
            environment = {**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"], "PROJECT": "fixture", "ENVIRONMENT": "dev",
                           "DEPLOYMENT": "fixture", "TARGET_KIND": "aws-eb", "FAKE_ACTIONS": str(actions),
                           "TARGET": json.dumps({"kind": "aws-eb", "region": "ap-northeast-2", "deployRole": "arn:aws:iam::123456789012:role/agent-runway-target-deploy"})}
            for step in ("remove", "verify"):
                script = scripts[step].replace("/etc/delivery-scripts/", shlex.quote(str(ROOT / "charts/delivery/files")) + "/")
                for mode in ("denied", "network", "empty", "missing", "deleted", "present", "deleting"):
                    actions.write_text("")
                    result = subprocess.run(["sh", "-euc", script], env={**environment, "FAKE_STACK": mode}, capture_output=True, text=True, timeout=10)
                    succeeds = mode in ("missing", "deleted") or (step == "remove" and mode in ("present", "deleting"))
                    self.assertEqual(result.returncode == 0, succeeds, (step, mode, result.stderr))
                    expected = "delete-stack\nwait\n" if step == "remove" and mode == "present" else "wait\n" if step == "remove" and mode == "deleting" else ""
                    self.assertEqual(actions.read_text(), expected, (step, mode))

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
