"""Prints the Workflow contract of charts/delivery/files for scripts/test.sh and asserts the step isolation.

Lines: "<template> tasks|parameters|outputs <names>" and "all deploy-key <template>:<step>:<ServiceAccount> ...".
"""
import re

import yaml

ENTRYPOINTS = {"app-build-v2": "release", "app-deploy": "deployment", "app-remove": "removal",
               "app-deploy-eb": "deployment", "app-remove-eb": "removal", "app-deploy-vmss": "deployment",
               "app-remove-vmss": "removal", "resource-sync": "sync", "env-cleanup": "sweep"}
VMSS = ("app-deploy-vmss", "app-remove-vmss")
AZURE_TOKEN = {"audience": "api://AzureADTokenExchange", "expirationSeconds": 3600, "path": "azure-identity-token"}


def unassigned_variables(script):
    """Lower-case shell variables a step script reads without assigning them; `sh -u` stops the step on the first one."""
    script = re.sub(r"^\s*#.*$", "", script, flags=re.M)  # comments
    script = re.sub(r"<<'(\w+)'\n.*?\n\s*\1\n", "\n", script, flags=re.S)  # quoted heredocs (python, JSON)
    script = re.sub(r"'[^']*'", "''", script)  # single-quoted text (jq and awk programs)
    used = set(re.findall(r"\$\{?([a-z_][a-z0-9_]*)", script))
    assigned = set(re.findall(r"(?:^|[\s;(&|])(?:local\s+|export\s+)?([a-z_][a-z0-9_]*)=", script, re.M))
    assigned |= set(re.findall(r"\bfor\s+([a-z_][a-z0-9_]*)\s+in\b", script))
    assigned |= {name for names in re.findall(r"\bread\s+(?:-\w+\s+)*([a-z_ ][a-z0-9_ ]*)", script) for name in names.split()}
    return sorted(used - assigned)


def load(name):
    with open(f"charts/delivery/files/{name}.yaml") as file:
        return yaml.safe_load(file)


key_holders = []
target_steps = []
for name, entry in ENTRYPOINTS.items():
    template = load(name)
    spec = template["spec"]
    assert template["metadata"]["name"] == name
    assert spec["entrypoint"] == entry, name
    # Agent Runway reads results and logs: Workflows stay 1 day. The build and deploy steps keep every Pod as long,
    # so deployment_logs can return a successful step's output too; resource-sync output is not read.
    assert spec["ttlStrategy"] == {"secondsAfterSuccess": 86400, "secondsAfterFailure": 86400}, name
    if name in ("app-build-v2", "app-deploy", "app-remove", "app-deploy-eb", "app-remove-eb", *VMSS):
        # Explicit, because the controller's workflowDefaults (OnPodSuccess) apply when podGC is absent.
        assert spec["podGC"] == {"strategy": "OnWorkflowCompletion", "deleteDelayDuration": "24h"}, name
    else:
        assert spec["podGC"]["strategy"] == "OnWorkflowSuccess", name
    steps = {t["name"]: t for t in spec["templates"]}
    for step in spec["templates"]:
        if step.get("serviceAccountName") == "delivery-target":
            # Target steps run for minutes on nodes that can go away: Karpenter must not consolidate them, and they
            # retry a lost Pod.
            assert step.get("metadata", {}).get("annotations", {}).get("karpenter.sh/do-not-disrupt") == "true", f"{name}:{step['name']}"
            assert 'lastRetry.status == "Error"' in step.get("retryStrategy", {}).get("expression", ""), f"{name}:{step['name']}"
        if name in ("app-deploy", "app-remove") and "container" in step:
            # verify waits up to 10 minutes for a rollout: a Pod that Karpenter deletes to consolidate fails the Deployment.
            assert step.get("metadata", {}).get("annotations", {}).get("karpenter.sh/do-not-disrupt") == "true", f"{name}:{step['name']}"
        if name in VMSS and "container" in step:
            # Entra exchanges only a token of this audience (the federated credential of delivery-target), read from the
            # file that target.sh azure_login reads. The Workflow passes no Azure credential otherwise.
            tokens = [source["serviceAccountToken"] for volume in step.get("volumes", []) for source in volume.get("projected", {}).get("sources", [])
                      if "serviceAccountToken" in source]
            assert tokens == [AZURE_TOKEN], f"{name}:{step['name']} projected token {tokens}"
            env = {item["name"]: item for item in step["container"]["env"]}
            assert env["AZURE_FEDERATED_TOKEN_FILE"]["value"] == "/var/run/secrets/azure/tokens/azure-identity-token", f"{name}:{step['name']}"
            assert {"name": "azure-identity-token", "mountPath": "/var/run/secrets/azure/tokens", "readOnly": True} in step["container"]["volumeMounts"]
            assert spec["securityContext"]["fsGroup"] == step["container"]["securityContext"]["runAsGroup"], f"{name} cannot read its token"
            # No secret value reaches a vmss step: only deploy reads a Kubernetes Secret, the Nexus reader password.
            secrets = [item["valueFrom"]["secretKeyRef"]["name"] for item in env.values() if "secretKeyRef" in item.get("valueFrom", {})]
            assert secrets == (["delivery-nexus"] if (name, step["name"]) == ("app-deploy-vmss", "deploy") else []), f"{name}:{step['name']} {secrets}"
            assert "secretRef" not in yaml.safe_dump(step["container"].get("envFrom", [])), f"{name}:{step['name']}"
            assert not any("secret" in volume for volume in step.get("volumes", [])), f"{name}:{step['name']} mounts a Secret"
    for step in spec["templates"]:
        for container in [step.get("container")] + step.get("initContainers", []):
            if container and container.get("args"):
                missing = unassigned_variables(container["args"][0])
                assert not missing, f"{name}:{step['name']} reads unassigned variables {missing}"
    print(name, "tasks", " ".join(task["name"] for task in steps[entry]["dag"]["tasks"]))
    print(name, "parameters", " ".join(p["name"] for p in spec.get("arguments", {}).get("parameters", [])))
    outputs = sorted(
        o["globalName"] for t in spec["templates"] for o in t.get("outputs", {}).get("parameters", []) if "globalName" in o
    )
    # Argo keeps one value per global output and does not define which of several writers wins: one step owns each.
    assert len(outputs) == len(set(outputs)), f"{name}: more than one step writes a global output: {outputs}"
    print(name, "outputs", " ".join(outputs))
    mutexes = [m["name"] for m in spec.get("synchronization", {}).get("mutexes", [])]
    print(name, "mutexes", " ".join(mutexes))
    for step in spec["templates"]:
        if "dag" in step or "steps" in step:
            continue
        # Every step names its identity. delivery-runner steps run app code: the token is in the executor only and
        # the runner NetworkPolicy applies.
        account = step.get("serviceAccountName") or step["executor"]["serviceAccountName"]
        if account == "delivery-runner":
            assert step.get("serviceAccountName") is None, (name, step["name"])
            assert step.get("automountServiceAccountToken") is False, (name, step["name"])
            assert step["metadata"]["labels"]["opspresso.com/delivery-role"] == "runner", (name, step["name"])
        if account == "delivery-target":
            target_steps.append(f"{name}:{step['name']}")
        secrets = [v["secret"]["secretName"] for v in step.get("volumes", []) if "secret" in v]
        if "gitops-deploy-key" in secrets:
            key_holders.append(f"{name}:{step['name']}:{account}")
print("all deploy-key", " ".join(key_holders))
print("all delivery-target", " ".join(target_steps))

# No step of the build holds a Git credential or runs git: checkout unpacks the archive Agent Runway uploaded.
build = {t["name"]: t for t in load("app-build-v2")["spec"]["templates"]}
for task in ("test", "build", "build-artifact", "check-application"):
    assert build[task]["executor"]["serviceAccountName"] == "delivery-runner", task
    assert "secretKeyRef" not in yaml.safe_dump(build[task]) and "secretRef" not in yaml.safe_dump(build[task]), task
checkout = build["checkout"]
assert checkout["serviceAccountName"] == "delivery-source", "checkout reads the archive as delivery-source"
assert [n for n, t in build.items() if "secretKeyRef" in yaml.safe_dump(t.get("container", {}))] == ["publish-image", "publish-artifact"], "only trusted publishers receive Nexus credentials"
for task in ("publish-image", "publish-artifact"):
    mounts = build[task]["container"]["volumeMounts"]
    assert next(m for m in mounts if m["name"] == "work").get("readOnly") is True, "publisher credentials and outputs must not enter the source volume"
assert "--no-push" in build["build"]["container"]["args"][0], "Dockerfile code must not have push credentials"
assert "volumes" not in checkout, "checkout mounts no Secret or key"
script = checkout["container"]["args"][0]
assert "git " not in script and "ssh " not in script, "checkout does not reach a Git host"
assert 'aws s3 cp --only-show-errors "${SOURCE}"' in script and "s3://*/_sources/*.tar.gz" in script, "checkout reads only a Release source archive"

# A large build runs up to two hours on a build NodePool (terraform-env-opsp demo/7-eks-shared delivery-<arch>), whose
# taint only these steps tolerate. Karpenter must not consolidate a node under a step that runs app code.
assert build["build"]["nodeSelector"] == {"kubernetes.io/arch": "{{inputs.parameters.arch}}",
                                          "karpenter.sh/nodepool": "delivery-{{inputs.parameters.arch}}"}, "build runs on its build NodePool"
for task in ("test", "build", "build-artifact"):
    assert build[task]["metadata"]["annotations"]["karpenter.sh/do-not-disrupt"] == "true", task
workflow_deadline = load("app-build-v2")["spec"]["activeDeadlineSeconds"]
for task, step in build.items():
    assert step.get("activeDeadlineSeconds", 0) < workflow_deadline, f"{task} deadline exceeds the Workflow deadline"
