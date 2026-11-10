"""Prints the Workflow contract of charts/delivery/files for scripts/test.sh and asserts the step isolation.

Lines: "<template> tasks|parameters|outputs <names>" and "all deploy-key <template>:<step>:<ServiceAccount> ...".
"""
import re

import yaml

ENTRYPOINTS = {"app-build": "release", "app-deploy": "deployment", "app-remove": "removal",
               "app-deploy-eb": "deployment", "app-remove-eb": "removal", "app-deploy-asg": "deployment",
               "app-remove-asg": "removal", "image-bake": "bake", "resource-sync": "sync", "env-cleanup": "sweep"}


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
    if name in ("app-build", "app-deploy", "app-remove", "app-deploy-eb", "app-remove-eb", "app-deploy-asg",
                "app-remove-asg", "image-bake"):
        # Explicit, because the controller's workflowDefaults (OnPodSuccess) apply when podGC is absent.
        assert spec["podGC"] == {"strategy": "OnWorkflowCompletion", "deleteDelayDuration": "24h"}, name
    else:
        assert spec["podGC"]["strategy"] == "OnWorkflowSuccess", name
    steps = {t["name"]: t for t in spec["templates"]}
    for step in spec["templates"]:
        if step.get("serviceAccountName") == "delivery-target":
            # Target steps run for minutes on nodes that can go away: Karpenter must not consolidate them, and the
            # repeatable ones (all but the packer build, which would leave its instance behind) retry a lost Pod.
            assert step.get("metadata", {}).get("annotations", {}).get("karpenter.sh/do-not-disrupt") == "true", f"{name}:{step['name']}"
            if name != "image-bake":
                assert 'lastRetry.status == "Error"' in step.get("retryStrategy", {}).get("expression", ""), f"{name}:{step['name']}"
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
    print(name, "outputs", " ".join(sorted(set(outputs))))
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

build = {t["name"]: t for t in load("app-build")["spec"]["templates"]}
for task in ("test", "build", "check-application"):
    assert build[task]["executor"]["serviceAccountName"] == "delivery-runner", task
# Only checkout reads the checkout token Secret, and never through the Git config or the remote URL.
readers = sorted(n for n, t in build.items() if "-checkout" in yaml.safe_dump(t.get("container", {}).get("env", [])))
assert readers == ["checkout"], readers
script = build["checkout"]["container"]["args"][0]
assert "@github.com" not in script and "x-access-token:%s@" not in script, "token stays out of the URL"
assert "git config" not in script and "--global" not in script, "token is not stored"
assert '"http.https://${host}/.extraheader=${header}"' in script and '"https://${host}/${path}.git"' in script, "the header is scoped to the repository's host"

