"""Prints the Workflow contract of charts/delivery/files for scripts/test.sh and asserts the step isolation.

Lines: "<template> tasks|parameters|outputs <names>" and "all deploy-key <template>:<step>:<ServiceAccount> ...".
"""
import yaml

ENTRYPOINTS = {"app-build": "release", "app-deploy": "deployment", "resource-sync": "sync"}


def load(name):
    with open(f"charts/delivery/files/{name}.yaml") as file:
        return yaml.safe_load(file)


key_holders = []
for name, entry in ENTRYPOINTS.items():
    template = load(name)
    spec = template["spec"]
    assert template["metadata"]["name"] == name
    assert spec["entrypoint"] == entry, name
    # Agent Delivery reads results and logs: Workflows stay 1 day. The build and deploy steps keep every Pod as long,
    # so deployment_logs can return a successful step's output too; resource-sync output is not read.
    assert spec["ttlStrategy"] == {"secondsAfterSuccess": 86400, "secondsAfterFailure": 86400}, name
    if name in ("app-build", "app-deploy"):
        # Explicit, because the controller's workflowDefaults (OnPodSuccess) apply when podGC is absent.
        assert spec["podGC"] == {"strategy": "OnWorkflowCompletion", "deleteDelayDuration": "24h"}, name
    else:
        assert spec["podGC"]["strategy"] == "OnWorkflowSuccess", name
    steps = {t["name"]: t for t in spec["templates"]}
    print(name, "tasks", " ".join(task["name"] for task in steps[entry]["dag"]["tasks"]))
    print(name, "parameters", " ".join(p["name"] for p in spec["arguments"]["parameters"]))
    outputs = sorted(
        o["globalName"] for t in spec["templates"] for o in t.get("outputs", {}).get("parameters", []) if "globalName" in o
    )
    print(name, "outputs", " ".join(sorted(set(outputs))))
    mutexes = [m["name"] for m in spec.get("synchronization", {}).get("mutexes", [])]
    print(name, "mutexes", " ".join(mutexes))
    for step in spec["templates"]:
        if "dag" in step:
            continue
        # Every step names its identity. delivery-runner steps run app code: the token is in the executor only and
        # the runner NetworkPolicy applies.
        account = step.get("serviceAccountName") or step["executor"]["serviceAccountName"]
        if account == "delivery-runner":
            assert step.get("serviceAccountName") is None, (name, step["name"])
            assert step.get("automountServiceAccountToken") is False, (name, step["name"])
            assert step["metadata"]["labels"]["opspresso.com/delivery-role"] == "runner", (name, step["name"])
        secrets = [v["secret"]["secretName"] for v in step.get("volumes", []) if "secret" in v]
        if "gitops-deploy-key" in secrets:
            key_holders.append(f"{name}:{step['name']}:{account}")
print("all deploy-key", " ".join(key_holders))

build = {t["name"]: t for t in load("app-build")["spec"]["templates"]}
for task in ("test", "build", "check-application"):
    assert build[task]["executor"]["serviceAccountName"] == "delivery-runner", task
# Only checkout reads the checkout token Secret, and never through the Git config or the remote URL.
readers = sorted(n for n, t in build.items() if "-checkout" in yaml.safe_dump(t.get("container", {}).get("env", [])))
assert readers == ["checkout"], readers
script = build["checkout"]["container"]["args"][0]
assert "@github.com" not in script and "x-access-token:%s@" not in script, "token stays out of the URL"
assert "git config" not in script and "--global" not in script, "token is not stored"
