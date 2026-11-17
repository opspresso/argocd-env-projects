"""Deploy, verify and remove one Project environment on an azure-vmss Target (agent-platform docs/APP_DELIVERY.md, target).

Usage (the steps of app-deploy-vmss and app-remove-vmss, after target.sh azure_login):
  vmss-deploy.py deploy <artifact file> <install script file>
  vmss-deploy.py verify
  vmss-deploy.py remove
  vmss-deploy.py removed
The environment holds PROJECT, ENVIRONMENT, DEPLOYMENT, TARGET and, for deploy and verify, APPLICATION, HOST and ARTIFACT.

Resources of the Project environment, all named runway-<project>-<environment>: in target.resourceGroup the app identity,
the public IP, the load balancer, the scale set and its autoscale setting (vmss-app.json); the bundle container in
target.artifactStorage; the Front Door origin group, custom domain and route on target.frontDoorProfileId; the records
<label> and _dnsauth.<label> in target.dnsZoneId. The app identity gets Key Vault Secrets User on each declared secret
and Storage Blob Data Reader on the container. No secret value passes through this script: it reads only secret names
and their metadata.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARM = "https://management.azure.com"
VAULT = "https://vault.azure.net"
API = {"identity": "2023-01-31", "network": "2024-05-01", "compute": "2024-07-01", "autoscale": "2022-10-01",
       "cdn": "2024-02-01", "dns": "2018-05-01", "roles": "2022-04-01", "graph": "2021-03-01"}
# The only roles the deploy identity may assign (terraform-env-azure dmz/8-runway-target 70-roles.tf).
SECRETS_USER = "4633458b-17de-408a-b874-0445c86b69e6"
BLOB_READER = "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1"
HEALTH_PORT = 61000  # The Application Health extension's local endpoint (vmss-boot.sh).
VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"


class Refused(Exception):
    """A request or answer that this Deployment cannot use; the message names it without any value."""


# ---------------------------------------------------------------------------------------------------------- plan


def plan_of(environ, deploy=True):
    """Checks the parameters and derives every name. deploy=False reads only what removal needs (the remove target)."""
    project, environment, deployment = environ["PROJECT"], environ["ENVIRONMENT"], environ["DEPLOYMENT"]
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}[a-z0-9]", project) or not re.fullmatch(r"[a-z][a-z0-9]{1,11}", environment):
        raise Refused("invalid project or environment")
    name = f"runway-{project}-{environment}"
    if "--" in name:
        raise Refused(f"{name} has consecutive hyphens, which a storage container name may not have")
    target = json.loads(environ["TARGET"])
    if target.get("kind") != "azure-vmss":
        raise Refused("Target is not of kind azure-vmss")

    def text(key, pattern, flags=0):
        value = target.get(key)
        if not isinstance(value, str) or not re.fullmatch(pattern, value, flags):
            raise Refused(f"Target field {key} is missing or malformed")
        return value

    subscription = text("subscriptionId", r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    sub = re.escape(subscription)
    plan = {
        "project": project, "environment": environment, "deployment": deployment, "name": name,
        "scope": text("name", r"[a-z0-9-]{1,63}"), "subscription": subscription,
        "location": text("location", r"[a-z][a-z0-9]{2,30}"),
        "vault": text("keyVault", r"[a-zA-Z][a-zA-Z0-9-]{1,22}[a-zA-Z0-9]"),
        "storage": text("artifactStorage", r"[a-z0-9]{3,24}"),
        "group": text("resourceGroup", r"[A-Za-z0-9._()-]{1,89}[A-Za-z0-9_()-]"),
        "zone": text("dnsZoneId", rf"/subscriptions/{sub}/resourceGroups/[^/]+/providers/Microsoft\.Network/dnszones/[^/]+", re.I),
        "profile": text("frontDoorProfileId", rf"/subscriptions/{sub}/resourceGroups/[^/]+/providers/Microsoft\.Cdn/profiles/[^/]+", re.I),
    }
    plan["container"] = name
    plan["groupId"] = f"/subscriptions/{subscription}/resourceGroups/{plan['group']}"
    plan["zoneName"] = plan["zone"].rsplit("/", 1)[1].lower()
    plan["labels"] = [project, f"{project}-{environment}"]
    if not deploy:
        return plan
    plan["subnet"] = text("subnetId", rf"/subscriptions/{sub}/resourceGroups/[^/]+/providers/Microsoft\.Network/virtualNetworks/[^/]+/subnets/[^/]+", re.I)

    host = environ["HOST"]
    label = host[: -len(plan["zoneName"]) - 1] if host.endswith("." + plan["zoneName"]) else None
    if label not in plan["labels"]:
        raise Refused(f"host {host} is not <project>.<domain> or <project>-<environment>.<domain> of {plan['zoneName']}")
    plan["host"], plan["label"] = host, label

    application = json.loads(environ["APPLICATION"])
    native, runtime, settings = application.get("native") or {}, application["runtime"], application["environment"]
    if native.get("format") != "jar":
        raise Refused("an azure-vmss Target runs JARs only")
    java = target.get("javaRuntime")
    if java != f"temurin-{native.get('javaVersion')}-jre" or not re.fullmatch(r"temurin-(17|21|25)-jre", java or ""):
        raise Refused("Target field javaRuntime does not match the Release's Java version")
    port = runtime.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535 or port == HEALTH_PORT:
        raise Refused(f"invalid application port (1024-65535, not {HEALTH_PORT})")
    health = runtime.get("health") or "/health"
    if not re.fullmatch(r"/[A-Za-z0-9._~/-]{0,200}", health):
        raise Refused("runtime.health must be a path of letters, digits and ._~/-")
    command = [*(runtime.get("command") or ["java", "-jar", "application.jar"]), *runtime.get("args", [])]
    if not all(isinstance(arg, str) and "\x00" not in arg for arg in command):
        raise Refused("invalid process command")
    plain = {**settings.get("env", {}), **application.get("config", {}), "PORT": str(port)}
    secrets = settings.get("secrets", [])
    if any(not isinstance(name_, str) or not VARIABLE.fullmatch(name_) for name_ in [*plain, *secrets]):
        raise Refused("invalid variable name")
    if not all(isinstance(value, str) for value in plain.values()):
        raise Refused("a variable value is not a string")
    if set(plain).intersection(secrets):
        raise Refused("a secret also has a plaintext value")

    size = settings.get("instanceType")
    family = re.fullmatch(r"Standard_[A-Z]+[0-9]+([a-z]*)_?[A-Za-z0-9_]*", size or "")
    if not isinstance(size, str) or not re.fullmatch(r"Standard_[A-Za-z0-9_]{2,40}", size) or not family:
        raise Refused("application field environment.instanceType is missing or is not an Azure VM size")
    architecture = settings.get("architecture") or "amd64"
    if architecture not in ("amd64", "arm64") or ("p" in family.group(1)) != (architecture == "arm64"):
        raise Refused(f"VM size {size} does not run {architecture}")
    replicas = settings.get("replicas", 1)
    most = settings.get("maxReplicas", replicas)
    if not all(isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 10 for n in (replicas, most)) or replicas > most:
        raise Refused("replicas and maxReplicas must be 1-10 with replicas <= maxReplicas")
    rollout = settings.get("rollout") or "immutable"
    if rollout not in ("immutable", "all-at-once"):
        raise Refused("application field environment.rollout must be immutable or all-at-once")
    artifact = json.loads(environ["ARTIFACT"])
    if not re.fullmatch(r"[a-f0-9]{64}", artifact.get("sha256", "")) or not isinstance(artifact.get("size"), int):
        raise Refused("invalid artifact identity")

    plan.update({
        "tenant": text("tenantId", UUID.pattern), "java": java, "port": port, "health": health, "size": size,
        "architecture": architecture, "replicas": replicas, "maxReplicas": most, "rollout": rollout,
        "artifact": {"sha256": artifact["sha256"], "size": artifact["size"]},
        "runway": {"command": command, "env": plain, "secrets": {name_: secret_name(plan["scope"], project, environment, name_) for name_ in secrets}},
    })
    return plan


def secret_name(scope, project, environment, variable):
    """The Key Vault secret of a Variable (Agent Runway's Azure secret store): ar-<sha256 of its path>."""
    return "ar-" + hashlib.sha256(f"/agent-runway/{scope}/{project}/{environment}/{variable}".encode()).hexdigest()


def ids(plan):
    group, name = plan["groupId"], plan["name"]
    endpoint = f"{plan['profile']}/afdEndpoints/runway"
    return {
        "identity": f"{group}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{name}",
        "vmss": f"{group}/providers/Microsoft.Compute/virtualMachineScaleSets/{name}",
        "autoscale": f"{group}/providers/Microsoft.Insights/autoscalesettings/{name}",
        "lb": f"{group}/providers/Microsoft.Network/loadBalancers/{name}",
        "ip": f"{group}/providers/Microsoft.Network/publicIPAddresses/{name}",
        "endpoint": endpoint,
        "route": f"{endpoint}/routes/{name}",
        "domain": f"{plan['profile']}/customDomains/{name}",
        "originGroup": f"{plan['profile']}/originGroups/{name}",
        "origin": f"{plan['profile']}/originGroups/{name}/origins/app",
    }


def api_of(resource_id):
    kind = resource_id.lower()
    for marker, api in (("/microsoft.cdn/", "cdn"), ("/dnszones/", "dns"), ("/microsoft.managedidentity/", "identity"),
                        ("/microsoft.compute/", "compute"), ("/microsoft.insights/", "autoscale"),
                        ("/microsoft.network/", "network"), ("/microsoft.authorization/", "roles")):
        if marker in kind:
            return API[api]
    raise ValueError(f"no API version for {resource_id}")


# ---------------------------------------------------------------------------------------------------------- Azure


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refused("Azure answered with a redirect")


class Azure:
    """ARM and Key Vault REST with the tokens of the signed-in deploy identity, and the az CLI for the rest."""

    def __init__(self):
        self.tokens = {}
        self.client = urllib.request.build_opener(NoRedirect)

    def token(self, resource):
        cached = self.tokens.get(resource)
        if not cached or cached[1] - time.time() < 300:
            answer = self.az("account", "get-access-token", "--resource", resource)
            self.tokens[resource] = cached = (answer["accessToken"], int(answer["expires_on"]))
        return cached[0]

    def request(self, method, url, body=None, resource=ARM):
        """(status, JSON body or None). Retries throttling and server errors; any other status goes to the caller."""
        data = None if body is None else json.dumps(body).encode()
        for attempt in range(6):
            headers = {"Authorization": "Bearer " + self.token(resource), "Content-Type": "application/json"}
            try:
                with self.client.open(urllib.request.Request(url, data=data, method=method, headers=headers), timeout=120) as response:
                    status, text = response.status, response.read()
            except urllib.error.HTTPError as error:
                status, text = error.code, error.read()
                error.close()
            if status == 429 or status >= 500:
                if attempt < 5:
                    time.sleep(min(60, 5 * 2 ** attempt))
                    continue
            return status, (json.loads(text) if text.strip() else None)

    def az(self, *args):
        result = subprocess.run(["az", *args, "--only-show-errors", "--output", "json"], text=True, capture_output=True)
        if result.returncode:
            raise Refused(f"az {' '.join(args[:3])} failed: {result.stderr.strip()}")
        return json.loads(result.stdout) if result.stdout.strip() else None


def error_text(body):
    error = (body or {}).get("error") or {}
    return f"{error.get('code', 'unknown')}: {error.get('message', '')}".strip()


def arm_get(azure, resource_id, api=None):
    """The resource, or None when ARM answers 404. Any other failure stops the step: it is not absence."""
    status, body = azure.request("GET", f"{ARM}{resource_id}?api-version={api or api_of(resource_id)}")
    if status == 404:
        return None
    if status != 200:
        raise Refused(f"GET {resource_id} answered {status} {error_text(body)}")
    return body


def matches(want, have):
    """Whether `have` already holds every value of `want` (strings compare case-insensitively, as ARM IDs do)."""
    if isinstance(want, dict):
        return isinstance(have, dict) and all(matches(value, have.get(key)) for key, value in want.items())
    if isinstance(want, list):
        return isinstance(have, list) and len(want) == len(have) and all(matches(a, b) for a, b in zip(want, have))
    if isinstance(want, str) and isinstance(have, str):
        return want.lower() == have.lower()
    return want == have


def ensure(azure, resource_id, body, clock, timeout=600):
    """PUTs body unless the resource already matches it, then waits for provisioningState Succeeded."""
    current = arm_get(azure, resource_id)
    if current and matches(body, current) and current.get("properties", {}).get("provisioningState", "Succeeded") == "Succeeded":
        return current
    status, answer = azure.request("PUT", f"{ARM}{resource_id}?api-version={api_of(resource_id)}", body)
    if status not in (200, 201, 202):
        raise Refused(f"PUT {resource_id} answered {status} {error_text(answer)}")
    deadline = clock.now() + timeout
    while True:
        current = arm_get(azure, resource_id)
        state = (current or {}).get("properties", {}).get("provisioningState", "Succeeded")
        if current and state == "Succeeded":
            return current
        if state in ("Failed", "Canceled"):
            raise Refused(f"{resource_id} is {state}")
        if clock.now() > deadline:
            raise Refused(f"{resource_id} did not provision in {timeout // 60} minutes ({state})")
        clock.sleep(10)


def delete(azure, resource_id, clock, timeout=1200):
    """Deletes the resource and waits until ARM answers 404. A missing resource is already deleted."""
    if arm_get(azure, resource_id) is None:
        return False
    status, answer = azure.request("DELETE", f"{ARM}{resource_id}?api-version={api_of(resource_id)}")
    if status not in (200, 202, 204, 404):
        raise Refused(f"DELETE {resource_id} answered {status} {error_text(answer)}")
    deadline = clock.now() + timeout
    while arm_get(azure, resource_id) is not None:
        if clock.now() > deadline:
            raise Refused(f"{resource_id} was not deleted in {timeout // 60} minutes")
        clock.sleep(10)
    print(f"Deleted {resource_id}", flush=True)
    return True


def find(azure, plan, kind, name):
    """The resource ID of the account's Key Vault or storage account. Resource Graph returns only what the deploy identity
    may read, so a list of the whole subscription (which it may not read) is not needed."""
    query = f"resources | where type =~ '{kind}' and name =~ '{name}' | project id"
    status, body = azure.request("POST", f"{ARM}/providers/Microsoft.ResourceGraph/resources?api-version={API['graph']}",
                                 {"subscriptions": [plan["subscription"]], "query": query})
    rows = (body or {}).get("data") or []
    if status != 200 or len(rows) != 1:
        raise Refused(f"{kind} {name} is not visible to the deploy identity ({status})")
    return rows[0]["id"]


class Clock:
    now = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)


# ---------------------------------------------------------------------------------------------------------- roles


def assignment_id(scope, role, principal):
    return f"{scope}/providers/Microsoft.Authorization/roleAssignments/{uuid.uuid5(uuid.NAMESPACE_URL, f'{scope}|{role}|{principal}')}"


def assign(azure, plan, scope, role, principal, clock):
    body = {"properties": {"roleDefinitionId": f"/subscriptions/{plan['subscription']}/providers/Microsoft.Authorization/roleDefinitions/{role}",
                           "principalId": principal, "principalType": "ServicePrincipal"}}
    deadline = clock.now() + 300
    while True:
        status, answer = azure.request("PUT", f"{ARM}{assignment_id(scope, role, principal)}?api-version={API['roles']}", body)
        code = ((answer or {}).get("error") or {}).get("code")
        if status in (200, 201) or (status == 409 and code == "RoleAssignmentExists"):
            return
        # A new identity's service principal takes a while to reach every Entra replica.
        if status == 400 and code == "PrincipalNotFound" and clock.now() < deadline:
            clock.sleep(10)
            continue
        raise Refused(f"assigning role {role} on {scope} answered {status} {error_text(answer)}")


def assignments(azure, scope, principal):
    """The role assignments of principal at and below scope."""
    status, body = azure.request("GET", f"{ARM}{scope}/providers/Microsoft.Authorization/roleAssignments?api-version={API['roles']}"
                                        f"&$filter=principalId%20eq%20'{principal}'")
    if status != 200:
        raise Refused(f"listing role assignments on {scope} answered {status} {error_text(body)}")
    return [item for item in body.get("value", []) if item["properties"]["scope"].lower().startswith(scope.lower())]


def unassign(azure, items, keep=()):
    keep = {scope.lower() for scope in keep}
    for item in items:
        if item["properties"]["scope"].lower() in keep:
            continue
        status, answer = azure.request("DELETE", f"{ARM}{item['id']}?api-version={API['roles']}")
        if status not in (200, 204, 404):
            raise Refused(f"removing role assignment {item['id']} answered {status} {error_text(answer)}")
        print(f"Removed role assignment on {item['properties']['scope']}", flush=True)


# ---------------------------------------------------------------------------------------------------------- deploy


def front_door(plan, ip):
    """The Front Door and DNS resources of the app: (resource ID, body) in creation order, without the DNS TXT record,
    whose value is the custom domain's validation token."""
    names = ids(plan)
    return [
        (names["domain"], {"properties": {"hostName": plan["host"],
                                          "tlsSettings": {"certificateType": "ManagedCertificate", "minimumTlsVersion": "TLS12"}}}),
        (names["originGroup"], {"properties": {
            "loadBalancingSettings": {"sampleSize": 4, "successfulSamplesRequired": 3, "additionalLatencyInMilliseconds": 50},
            "healthProbeSettings": {"probePath": plan["health"], "probeRequestType": "GET", "probeProtocol": "Http", "probeIntervalInSeconds": 30},
            "sessionAffinityState": "Disabled"}}),
        # The app sees its own host: Front Door sends it as Host to the load balancer's address over HTTP.
        (names["origin"], {"properties": {"hostName": ip, "httpPort": 80, "httpsPort": 443, "originHostHeader": plan["host"],
                                          "priority": 1, "weight": 1000, "enabledState": "Enabled", "enforceCertificateNameCheck": True}}),
        (names["route"], {"properties": {
            "customDomains": [{"id": names["domain"]}], "originGroup": {"id": names["originGroup"]},
            "supportedProtocols": ["Http", "Https"], "patternsToMatch": ["/*"], "forwardingProtocol": "HttpOnly",
            "linkToDefaultDomain": "Disabled", "httpsRedirect": "Enabled", "enabledState": "Enabled"}}),
    ]


def owner(plan):
    """The DNS metadata that marks a record set as this Project environment's (record sets have no tags)."""
    return {"deliveryproject": plan["project"], "deliveryenv": plan["environment"]}


def records(plan, endpoint_host, token):
    return [
        (f"{plan['zone']}/TXT/_dnsauth.{plan['label']}", {"properties": {"TTL": 3600, "TXTRecords": [{"value": [token]}], "metadata": owner(plan)}}),
        (f"{plan['zone']}/CNAME/{plan['label']}", {"properties": {"TTL": 60, "CNAMERecord": {"cname": endpoint_host}, "metadata": owner(plan)}}),
    ]


def boot_script(plan, identity_client, front_door_id, config_sha, install):
    boot = {
        "deployment": plan["deployment"], "storage": plan["storage"], "container": plan["container"],
        "jar": {"blob": f"{plan['deployment']}/application.jar", **plan["artifact"]},
        "config": {"blob": f"{plan['deployment']}/runway.json", "sha256": config_sha},
        "keyVault": plan["vault"], "clientId": identity_client, "frontDoorId": front_door_id, "javaRuntime": plan["java"],
        "port": plan["port"], "health": plan["health"], "installScript": base64.b64encode(install).decode(),
        "adoptiumKey": (HERE / "adoptium.asc").read_text(),
    }
    script = (HERE / "vmss-boot.sh").read_text().replace("__RUNWAY_BOOT__", base64.b64encode(json.dumps(boot).encode()).decode())
    data = base64.b64encode(script.encode()).decode()
    if len(data) > 65535:
        raise Refused("the instance custom data exceeds 64 KiB; shorten the Target's install script")
    return data


def template_parameters(plan, identity_id, custom_data):
    values = {"name": plan["name"], "location": plan["location"], "project": plan["project"], "environment": plan["environment"],
              "deployment": plan["deployment"], "identityId": identity_id, "subnetId": plan["subnet"], "vmSize": plan["size"],
              "architecture": plan["architecture"], "replicas": plan["replicas"], "maxReplicas": plan["maxReplicas"],
              "rollout": plan["rollout"], "healthPath": plan["health"], "customData": custom_data}
    return {key: {"value": value} for key, value in values.items()}


def instances(azure, plan):
    status, body = azure.request("GET", f"{ARM}{ids(plan)['vmss']}/virtualMachines?$expand=instanceView&api-version={API['compute']}")
    if status != 200:
        raise Refused(f"listing the instances of {plan['name']} answered {status} {error_text(body)}")
    result = []
    for vm in body.get("value", []):
        properties = vm.get("properties", {})
        health = (((properties.get("instanceView") or {}).get("vmHealth") or {}).get("status") or {}).get("code", "HealthState/none")
        result.append({"id": vm["instanceId"], "latest": properties.get("latestModelApplied") is True,
                       "state": properties.get("provisioningState"), "health": health.split("/")[-1].lower()})
    return result


def latest_upgrade(azure, plan):
    status, body = azure.request("GET", f"{ARM}{ids(plan)['vmss']}/rollingUpgrades/latest?api-version={API['compute']}")
    if status == 404:
        return None
    if status != 200:
        raise Refused(f"reading the rolling upgrade of {plan['name']} answered {status} {error_text(body)}")
    return body.get("properties", {})


def summary(vms):
    return ", ".join(f"{vm['id']}:{'new' if vm['latest'] else 'old'}/{vm['state']}/{vm['health']}" for vm in vms) or "no instance"


def ready(plan, vms):
    """Every instance runs the scale set's model, and at least replicas of them are healthy."""
    return all(vm["latest"] for vm in vms) and sum(vm["health"] == "healthy" and vm["state"] == "Succeeded" for vm in vms) >= plan["replicas"]


def wait_instances(azure, plan, clock, deadline, before=None):
    """Waits for ready(); with the immutable rollout, also fails when this Deployment's rolling upgrade fails."""
    started = clock.now()
    while True:
        vms = instances(azure, plan)
        if ready(plan, vms):
            return vms
        if plan["rollout"] == "immutable" and any(not vm["latest"] for vm in vms):
            upgrade = latest_upgrade(azure, plan)
            ours = upgrade is not None and upgrade.get("runningStatus", {}).get("startTime") != before
            code = (upgrade or {}).get("runningStatus", {}).get("code")
            if ours and code in ("Faulted", "Cancelled"):
                raise Refused(f"the rolling upgrade of {plan['name']} is {code}: {((upgrade.get('error') or {}).get('message') or '')[:500]}")
            if not ours and clock.now() - started > 300:
                raise Refused(f"no rolling upgrade of {plan['name']} started for the new model ({summary(vms)})")
        if clock.now() > deadline:
            raise Refused(f"the instances of {plan['name']} did not all run Deployment {plan['deployment']} healthy in time ({summary(vms)})")
        print(f"Waiting for the instances of {plan['name']}: {summary(vms)}", flush=True)
        clock.sleep(15)


def wait_domain(azure, plan, clock, deadline):
    domain = ids(plan)["domain"]
    refreshed = False
    while True:
        current = arm_get(azure, domain) or {}
        state = current.get("properties", {}).get("domainValidationState")
        if state == "Approved":
            return
        if state in ("TimedOut", "PendingRevalidation") and not refreshed:
            status, answer = azure.request("POST", f"{ARM}{domain}/refreshValidationToken?api-version={API['cdn']}")
            if status not in (200, 202):
                raise Refused(f"refreshing the validation token of {plan['host']} answered {status} {error_text(answer)}")
            refreshed = True
        if state in ("Rejected", "InternalError"):
            raise Refused(f"Front Door refused the domain {plan['host']} ({state})")
        if clock.now() > deadline:
            raise Refused(f"Front Door did not validate the domain {plan['host']} in time ({state})")
        print(f"Waiting for Front Door to validate {plan['host']} ({state})", flush=True)
        clock.sleep(20)


def deploy(plan, artifact_file, install, azure, clock=Clock, template=HERE / "vmss-app.json", workdir=Path("/tmp")):
    names = ids(plan)
    deadline = clock.now() + 3000  # app-deploy-vmss gives the deploy step 3300 seconds

    identity = ensure(azure, names["identity"], {"location": plan["location"], "tags": tags(plan)}, clock)
    principal, client = identity["properties"]["principalId"], identity["properties"]["clientId"]

    vault_id = find(azure, plan, "microsoft.keyvault/vaults", plan["vault"])
    for variable, secret in plan["runway"]["secrets"].items():
        status, body = azure.request("GET", f"https://{plan['vault']}.vault.azure.net/secrets/{secret}/versions?api-version=7.5&maxresults=1", resource=VAULT)
        if status == 404 or (status == 200 and not body.get("value")):
            raise Refused(f"Variable {variable} has no value in Key Vault {plan['vault']}")
        if status != 200:
            raise Refused(f"reading the metadata of Variable {variable} answered {status}")
    storage_id = find(azure, plan, "microsoft.storage/storageaccounts", plan["storage"])
    azure.az("storage", "container", "create", "--name", plan["container"], "--account-name", plan["storage"], "--auth-mode", "login")
    container_scope = f"{storage_id}/blobServices/default/containers/{plan['container']}"
    secret_scopes = [f"{vault_id}/secrets/{secret}" for secret in plan["runway"]["secrets"].values()]
    for scope in secret_scopes:
        assign(azure, plan, scope, SECRETS_USER, principal, clock)
    assign(azure, plan, container_scope, BLOB_READER, principal, clock)
    # A secret the app no longer declares stops being readable by it.
    unassign(azure, assignments(azure, vault_id, principal), keep=secret_scopes)
    unassign(azure, assignments(azure, storage_id, principal), keep=[container_scope])
    print(f"The app identity reads {len(secret_scopes)} secret(s) and its container", flush=True)

    config = json.dumps(plan["runway"], sort_keys=True).encode()
    config_file = workdir / "runway.json"
    config_file.write_bytes(config)
    for source, blob in ((artifact_file, "application.jar"), (config_file, "runway.json")):
        azure.az("storage", "blob", "upload", "--account-name", plan["storage"], "--container-name", plan["container"],
                 "--name", f"{plan['deployment']}/{blob}", "--file", str(source), "--overwrite", "--no-progress", "--auth-mode", "login")

    profile = arm_get(azure, plan["profile"])
    endpoint = arm_get(azure, names["endpoint"])
    if not profile or (profile.get("sku") or {}).get("name") not in ("Standard_AzureFrontDoor", "Premium_AzureFrontDoor") or not endpoint:
        raise Refused("the Target's Front Door profile or its endpoint runway is missing")
    front_door_id = profile["properties"].get("frontDoorId", "")
    if not UUID.fullmatch(front_door_id):
        raise Refused("the Front Door profile has no Front Door ID")

    custom_data = boot_script(plan, client, front_door_id, hashlib.sha256(config).hexdigest(), install)
    parameters = workdir / "parameters.json"
    parameters.write_text(json.dumps(template_parameters(plan, names["identity"], custom_data)))
    before = None
    if arm_get(azure, names["vmss"]) is not None:
        before = ((latest_upgrade(azure, plan) or {}).get("runningStatus") or {}).get("startTime")
    print(f"Deploying {plan['name']} ({plan['size']} x{plan['replicas']}-{plan['maxReplicas']}, {plan['rollout']})", flush=True)
    result = azure.az("deployment", "group", "create", "--resource-group", plan["group"], "--name", plan["name"],
                      "--template-file", str(template), "--parameters", f"@{parameters}")
    parameters.unlink()
    ip = result["properties"]["outputs"]["publicIp"]["value"]
    if plan["maxReplicas"] == plan["replicas"]:
        delete(azure, names["autoscale"], clock)

    if plan["rollout"] == "all-at-once":
        # Custom data reaches only new instances: replace every instance of an older model at once.
        old = [vm["id"] for vm in instances(azure, plan) if not vm["latest"]]
        if old:
            print(f"Replacing {len(old)} instance(s) at once", flush=True)
            azure.az("vmss", "delete-instances", "--resource-group", plan["group"], "--name", plan["name"], "--instance-ids", *old)
            azure.az("vmss", "scale", "--resource-group", plan["group"], "--name", plan["name"], "--new-capacity", str(plan["replicas"]))

    domain, *rest = front_door(plan, ip)
    current = ensure(azure, *domain, clock)
    while not (token := ((current.get("properties", {}).get("validationProperties") or {}).get("validationToken"))):
        if clock.now() > deadline:
            raise Refused(f"Front Door gave no validation token for {plan['host']}")
        clock.sleep(10)
        current = arm_get(azure, names["domain"]) or {}
    for resource_id, body in records(plan, endpoint["properties"]["hostName"], token) + rest:
        ensure(azure, resource_id, body, clock)

    wait_instances(azure, plan, clock, min(deadline, clock.now() + 1500), before)
    prune_bundles(azure, plan)
    wait_domain(azure, plan, clock, deadline)
    print(f"{plan['name']} runs Deployment {plan['deployment']} behind {plan['host']}", flush=True)


def prune_bundles(azure, plan):
    """Deletes the bundles of earlier Deployments once every instance runs this one. The container then holds only the
    bundle that new and repaired instances download, and nothing in it expires."""
    names = azure.az("storage", "blob", "list", "--container-name", plan["container"], "--account-name", plan["storage"],
                     "--auth-mode", "login", "--query", "[].name") or []
    for name in names:
        if not name.startswith(f"{plan['deployment']}/"):
            azure.az("storage", "blob", "delete", "--container-name", plan["container"], "--account-name", plan["storage"],
                     "--name", name, "--auth-mode", "login")
            print(f"Deleted bundle {name} of an earlier Deployment", flush=True)


def tags(plan):
    return {"opspresso.com/delivery-project": plan["project"], "opspresso.com/delivery-env": plan["environment"]}


def verify(plan, azure, clock=Clock, timeout=600):
    """Waits until the scale set carries this Deployment and every instance runs its model healthy."""
    deadline = clock.now() + timeout
    while True:
        vmss = arm_get(azure, ids(plan)["vmss"])
        if vmss is None:
            raise Refused(f"scale set {plan['name']} does not exist")
        deployed = (vmss.get("tags") or {}).get("opspresso.com/delivery-deployment")
        vms = instances(azure, plan)
        if deployed == plan["deployment"] and ready(plan, vms):
            return
        if clock.now() > deadline:
            raise Refused(f"{plan['name']} did not run Deployment {plan['deployment']} healthy in {timeout // 60} minutes "
                          f"(scale set Deployment {deployed}; {summary(vms)})")
        clock.sleep(10)


# ---------------------------------------------------------------------------------------------------------- remove


def owned_records(azure, plan):
    """The record sets of this Project environment. Removal does not know which of the two hosts the app ran at, and
    the bare host <project>.<domain> can belong to another environment of the Project on the same zone: only record
    sets with this environment's metadata count."""
    found = []
    for label in plan["labels"]:
        for record in (f"{plan['zone']}/CNAME/{label}", f"{plan['zone']}/TXT/_dnsauth.{label}"):
            current = arm_get(azure, record)
            metadata = {key.lower(): value for key, value in ((current or {}).get("properties", {}).get("metadata") or {}).items()}
            if current and metadata == owner(plan):
                found.append(record)
    return found


def remove(plan, azure, clock=Clock):
    names = ids(plan)
    for key in ("route", "domain", "originGroup"):
        delete(azure, names[key], clock)
    for record in owned_records(azure, plan):
        delete(azure, record, clock)
    for key in ("autoscale", "vmss", "lb", "ip"):
        delete(azure, names[key], clock)
    identity = arm_get(azure, names["identity"])
    if identity:
        principal = identity["properties"]["principalId"]
        for kind, name in (("microsoft.keyvault/vaults", plan["vault"]), ("microsoft.storage/storageaccounts", plan["storage"])):
            unassign(azure, assignments(azure, find(azure, plan, kind, name), principal))
        delete(azure, names["identity"], clock)
    answer = azure.az("storage", "container", "delete", "--name", plan["container"], "--account-name", plan["storage"], "--auth-mode", "login")
    if (answer or {}).get("deleted"):
        print(f"Deleted container {plan['container']}", flush=True)


def remaining(plan, azure):
    """The resources of the Project environment that still exist."""
    names = ids(plan)
    left = [names[key] for key in ("route", "domain", "originGroup", "autoscale", "vmss", "lb", "ip", "identity")
            if arm_get(azure, names[key]) is not None]
    left += owned_records(azure, plan)
    exists = azure.az("storage", "container", "exists", "--name", plan["container"], "--account-name", plan["storage"], "--auth-mode", "login")
    if exists.get("exists") is not False:
        left.append(f"container {plan['container']}")
    return left


if __name__ == "__main__":
    try:
        command = sys.argv[1:2]
        if command == ["deploy"] and len(sys.argv) == 4:
            deploy(plan_of(os.environ), sys.argv[2], Path(sys.argv[3]).read_bytes(), Azure())
        elif command == ["verify"]:
            verify(plan_of(os.environ), Azure())
        elif command == ["remove"]:
            remove(plan_of(os.environ, deploy=False), Azure())
        elif command == ["removed"]:
            left = remaining(plan_of(os.environ, deploy=False), Azure())
            if left:
                sys.exit("Still present: " + ", ".join(left))
        else:
            sys.exit(__doc__)
    except Refused as refused:
        sys.exit(str(refused))
