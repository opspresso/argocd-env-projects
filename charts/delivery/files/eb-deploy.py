"""Create with trusted bootstrap code, scope the instance role, then deploy user code."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def aws(*args):
    plain = args[0:2] in (("cloudformation", "deploy"), ("s3", "cp"))
    result = subprocess.run(["aws", *args, *([] if plain else ["--output", "json"])], text=True, capture_output=True)
    if result.returncode:
        # Only the documented missing-stack response means absence. Authentication and transport errors stop work.
        if args[0:2] == ("cloudformation", "describe-stacks") and "(ValidationError)" in result.stderr and "does not exist" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip() or "AWS command failed")
    if args[0:2] == ("cloudformation", "deploy"):
        print(result.stdout, end="", flush=True)
        return {}
    return json.loads(result.stdout) if result.stdout.strip() else {}


def deploy(parameters, region, bundle, bootstrap_bundle, call=aws, pause=time.sleep):
    stack = f"runway-{parameters['Project']}-{parameters['Environment']}"
    template = str(Path(__file__).with_name("eb-stack.yaml"))

    def describe():
        response = call("cloudformation", "describe-stacks", "--stack-name", stack)
        return response["Stacks"][0] if response else None

    def update(values):
        # Unspecified existing parameters retain their previous values. The scope-only update never changes code.
        return call("cloudformation", "deploy", "--stack-name", stack, "--template-file", template,
                    "--capabilities", "CAPABILITY_NAMED_IAM", "--no-fail-on-empty-changeset",
                    "--tags", f"opspresso.com/delivery-project={parameters['Project']}",
                    f"opspresso.com/delivery-env={parameters['Environment']}",
                    "--parameter-overrides", *(f"{key}={value}" for key, value in values.items()))

    current = describe()
    while current and current["StackStatus"].endswith("_IN_PROGRESS"):
        print(f"Waiting for stack {stack} ({current['StackStatus']})", flush=True)
        pause(15)
        current = describe()
    if current and current["StackStatus"] == "ROLLBACK_COMPLETE":
        call("cloudformation", "delete-stack", "--stack-name", stack)
        call("cloudformation", "wait", "stack-delete-complete", "--stack-name", stack)
        current = None
    bucket = parameters["VersionBucket"]
    if current is None:
        key = f"{parameters['Project']}/{parameters['Environment']}/_bootstrap/{parameters['DeploymentId']}.zip"
        call("s3", "cp", "--only-show-errors", bootstrap_bundle, f"s3://{bucket}/{key}")
        print(f"Creating {stack} with platform bootstrap code", flush=True)
        update({**parameters, "VersionKey": key, "Bootstrap": "true", "EbEnvironmentId": "", "EbEnvironmentName": ""})
        current = describe()
    if not current:
        raise RuntimeError("Created stack could not be read")
    name = next((item["OutputValue"] for item in current.get("Outputs", []) if item["OutputKey"] == "EnvironmentName"), None)
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9-]{4,40}", name):
        raise ValueError("Stack has no valid EB environment name")
    response = call("elasticbeanstalk", "describe-environments", "--environment-names", name)
    environments = response.get("Environments", [])
    if len(environments) != 1 or environments[0].get("ApplicationName") != stack or environments[0].get("EnvironmentName") != name:
        raise ValueError("EB environment does not belong to this stack")
    environment_id = environments[0].get("EnvironmentId", "")
    if not re.fullmatch(r"e-[a-z0-9]{10}", environment_id):
        raise ValueError("Invalid EB environment ID")
    scope = {"EbEnvironmentId": environment_id, "EbEnvironmentName": name}
    print(f"Restricting {stack} to its own environment resources", flush=True)
    update(scope)
    # This separate successful stack update precedes uploading and running any user bundle.
    current = describe()
    if not current:
        raise RuntimeError("Scoped stack could not be read; user code was not deployed")
    applied = {item["ParameterKey"]: item["ParameterValue"] for item in current.get("Parameters", [])}
    if any(applied.get(key) != value for key, value in scope.items()):
        raise RuntimeError("Environment permission scope was not applied")
    role = f"{stack}-app"
    attached = call("iam", "list-attached-role-policies", "--role-name", role)
    if attached.get("AttachedPolicies"):
        raise RuntimeError("The instance role still has managed policies; user code was not deployed")
    policy = call("iam", "get-role-policy", "--role-name", role, "--policy-name", "runway-app")["PolicyDocument"]
    arn = environments[0]["EnvironmentArn"].split(":")
    bucket_arn = f"arn:{arn[1]}:s3:::elasticbeanstalk-{region}-{arn[4]}"
    prefixes = [f"resources/environments/{environment_id}/*", *[
        f"resources/environments/logs/{kind}/{environment_id}/*" for kind in ("tail", "bundle", "publish", "deployments")]]
    expected = {f"{bucket_arn}/{prefix}" for prefix in prefixes}
    log_resources = {f"{bucket_arn}/{prefix}" for prefix in prefixes[1:]}
    statements = policy["Statement"]
    reads = [statement for statement in statements if statement.get("Effect") == "Allow" and "s3:GetObject" in statement.get("Action", [])]
    if len(reads) != 1 or reads[0].get("Resource") != f"{bucket_arn}/{prefixes[0]}":
        raise RuntimeError("Instance artifact permission is not scoped to this environment")
    boundary = call("iam", "get-role-policy", "--role-name", role, "--policy-name", "runway-environment-boundary")["PolicyDocument"]
    denies = {statement.get("Sid"): statement for statement in boundary["Statement"] if statement.get("Effect") == "Deny"}
    objects = denies.get("DenyOtherEnvironmentObjects", {})
    listing = denies.get("DenyOtherEnvironmentListing", {})
    writes = denies.get("DenyArtifactWrites", {})
    if (set(objects.get("NotResource", [])) != expected
            or set(objects.get("Action", [])) != {"s3:GetObject*"}
            or set(writes.get("Action", [])) != {"s3:PutObject*", "s3:DeleteObject*"}
            or set(writes.get("NotResource", [])) != log_resources
            or writes.get("Condition") != {"StringEquals": {"s3:ResourceAccount": arn[4]}}
            or objects.get("Condition") != {"StringEquals": {"s3:ResourceAccount": arn[4]}}
            or listing.get("Resource") != bucket_arn
            or set(listing.get("Action", [])) != {"s3:ListBucket", "s3:ListBucketVersions"}
            or set(listing.get("Condition", {}).get("StringNotLike", {}).get("s3:prefix", [])) != set(prefixes)):
        raise RuntimeError("Instance role does not override cross-environment bucket-policy grants")
    print(f"Deploying user code to {stack} after scoped-role verification", flush=True)
    call("s3", "cp", "--only-show-errors", bundle, f"s3://{bucket}/{parameters['VersionKey']}")
    update({**parameters, **scope, "Bootstrap": "false"})


if __name__ == "__main__":
    deploy(json.loads(Path(sys.argv[1]).read_text()), os.environ["AWS_DEFAULT_REGION"], sys.argv[2], sys.argv[3])
