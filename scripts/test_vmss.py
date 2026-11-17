"""Checks charts/delivery/files/vmss-deploy.py, vmss-app.json and vmss-boot.sh against a fake Azure (no network).

The azure-vmss contract (agent-platform docs/APP_DELIVERY.md, target): every name derives from the Project environment,
the instances read secrets by name with their own identity, and no secret value reaches a request, a template
parameter, the custom data or the bundle.
"""
import base64
import copy
import hashlib
import importlib.util
import json
import re
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "charts/delivery/files"
spec = importlib.util.spec_from_file_location("vmss_deploy", FILES / "vmss-deploy.py")
vmss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vmss)

SUB = "ce097188-9b76-44f9-8bbd-366ae73ade04"
APPS = f"/subscriptions/{SUB}/resourceGroups/rg-agent-runway-dmz-apps"
VAULT_ID = f"/subscriptions/{SUB}/resourceGroups/rg-agent-runway-dmz/providers/Microsoft.KeyVault/vaults/kv-runway-dmz-ce097188"
STORAGE_ID = f"/subscriptions/{SUB}/resourceGroups/rg-agent-runway-dmz/providers/Microsoft.Storage/storageAccounts/strunwaydmzce097188"
PROFILE = f"/subscriptions/{SUB}/resourceGroups/rg-agent-runway-dmz/providers/Microsoft.Cdn/profiles/afd-agent-runway-dmz"
ZONE = f"/subscriptions/{SUB}/resourceGroups/rg-dns-dmz/providers/Microsoft.Network/dnsZones/dmz.rovr.dev"
FDID = "0f1e2d3c-4b5a-4968-8776-655443322110"
SECRET_VALUE = "s3cr3t-value-never-leaves-key-vault"
TARGET = {
    "kind": "azure-vmss", "name": "dmz", "tenantId": "b84c843a-4ece-4ef7-8788-372e1f232c0a", "subscriptionId": SUB,
    "location": "koreacentral", "deployClientId": "9d46ecbc-80b8-467f-8e8c-85bd727d274f", "keyVault": "kv-runway-dmz-ce097188",
    "artifactStorage": "strunwaydmzce097188", "resourceGroup": "rg-agent-runway-dmz-apps",
    "subnetId": f"/subscriptions/{SUB}/resourceGroups/rg-agent-runway-dmz/providers/Microsoft.Network/virtualNetworks/vnet-agent-runway-dmz/subnets/snet-apps",
    "dnsZoneId": ZONE, "frontDoorProfileId": PROFILE, "javaRuntime": "temurin-21-jre",
    "installScript": "#!/bin/bash\napt-get install -y fontconfig\n",
}
APPLICATION = {
    "runtime": {"port": 8080, "health": "/actuator/health"},
    "native": {"format": "jar", "javaVersion": 21},
    "environment": {"secrets": ["DB_PASSWORD"], "env": {"MODE": "prod"}, "instanceType": "Standard_B2s_v2",
                    "replicas": 1, "maxReplicas": 1, "rollout": "immutable", "architecture": "amd64"},
    "config": {"GREETING": "hello"},
}
ARTIFACT = {"format": "jar", "url": "https://nexus.example/repository/runway-artifacts/todo/main/r/app.jar", "sha256": "a" * 64, "size": 3}


def environ(target=None, application=None, host="todo-dmz.dmz.rovr.dev"):
    return {"PROJECT": "todo", "ENVIRONMENT": "dmz", "DEPLOYMENT": "11111111-1111-4111-8111-111111111111", "HOST": host,
            "TARGET": json.dumps(target or TARGET), "APPLICATION": json.dumps(application or APPLICATION), "ARTIFACT": json.dumps(ARTIFACT)}


def contract_secret(variable):
    """Agent Runway's Key Vault name of a Variable (the contract), computed independently of vmss-deploy.py."""
    return "ar-" + hashlib.sha256(f"/agent-runway/dmz/todo/dmz/{variable}".encode()).hexdigest()


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeAzure:
    """ARM, Resource Graph, Key Vault metadata and the az commands vmss-deploy.py uses, kept in memory."""

    def __init__(self, existing=False):
        self.resources = {
            PROFILE.lower(): {"sku": {"name": "Standard_AzureFrontDoor"}, "properties": {"frontDoorId": FDID}},
            (PROFILE + "/afdEndpoints/runway").lower(): {"properties": {"hostName": "runway-g0c4gmctcvfuapbc.z01.azurefd.net"}},
        }
        self.assignments = {}
        self.secrets = {contract_secret("DB_PASSWORD"): SECRET_VALUE}
        self.instances = [{"id": "0", "latest": True, "health": "healthy"}]
        self.upgrade = None
        self.upgrade_after_deploy = {"runningStatus": {"code": "Completed", "startTime": "2026-11-17T01:00:00Z"}}
        self.log = []  # (method, path, body) of every request and ("az", args)
        self.uploads = {}
        self.deployed = None
        self.container = existing
        if existing:
            self.resources[self.vmss_id()] = {"tags": {}, "properties": {}}
            self.instances = [{"id": "0", "latest": True, "health": "healthy"}]

    def vmss_id(self):
        return f"{APPS}/providers/Microsoft.Compute/virtualMachineScaleSets/runway-todo-dmz".lower()

    def request(self, method, url, body=None, resource=vmss.ARM):
        parts = urlsplit(url)
        path = unquote(parts.path)
        self.log.append((method, path, copy.deepcopy(body)))
        key = path.lower()
        if parts.netloc.endswith(".vault.azure.net"):
            name = path.split("/")[2]
            assert path.endswith("/versions"), "the workflow reads only secret metadata"
            return (200, {"value": [{"id": name, "attributes": {"enabled": True}}]}) if name in self.secrets else (404, {"error": {"code": "SecretNotFound"}})
        if key == "/providers/microsoft.resourcegraph/resources":
            match = {"microsoft.keyvault/vaults": VAULT_ID, "microsoft.storage/storageaccounts": STORAGE_ID}
            kind = re.search(r"type =~ '([^']+)'", body["query"]).group(1)
            return 200, {"data": [{"id": match[kind]}]}
        if "/providers/microsoft.authorization/roleassignments" in key:
            if method == "PUT":
                self.assignments[key] = {"id": path, "properties": {**body["properties"], "scope": path.split("/providers/Microsoft.Authorization/")[0]}}
                return 201, self.assignments[key]
            if method == "GET":
                scope = key.split("/providers/microsoft.authorization/")[0]
                principal = re.search(r"principalId eq '([^']+)'", unquote(parts.query)).group(1)
                return 200, {"value": [a for a in self.assignments.values() if a["properties"]["principalId"] == principal
                                       and a["properties"]["scope"].lower().startswith(scope)]}
            self.assignments.pop(key, None)
            return 200, None
        if key.endswith("/virtualmachines"):
            return 200, {"value": [{"instanceId": vm["id"], "properties": {"latestModelApplied": vm["latest"], "provisioningState": "Succeeded",
                                                                            "instanceView": {"vmHealth": {"status": {"code": f"HealthState/{vm['health']}"}}}}}
                                   for vm in self.instances]}
        if key.endswith("/rollingupgrades/latest"):
            return (404, {"error": {"code": "NotFound"}}) if self.upgrade is None else (200, {"properties": self.upgrade})
        if method == "GET":
            return (200, copy.deepcopy(self.resources[key])) if key in self.resources else (404, {"error": {"code": "ResourceNotFound"}})
        if method == "PUT":
            stored = copy.deepcopy(body)
            properties = stored.setdefault("properties", {})
            properties["provisioningState"] = "Succeeded"
            if "/userassignedidentities/" in key:
                properties.update({"principalId": "pppppppp-0000-4000-8000-000000000001", "clientId": "cccccccc-0000-4000-8000-000000000002"})
            if "/customdomains/" in key:
                properties.update({"validationProperties": {"validationToken": "token-123"}, "domainValidationState": "Approved"})
            self.resources[key] = stored
            return 201, stored
        if method == "DELETE":
            self.resources.pop(key, None)
            return 202, None
        if method == "POST":
            return 200, None
        raise AssertionError(f"unexpected {method} {path}")

    def az(self, *args):
        self.log.append(("az", args))
        if args[:3] == ("storage", "container", "create"):
            self.container = True
            return {"created": True}
        if args[:3] == ("storage", "container", "delete"):
            deleted, self.container = self.container, False
            return {"deleted": deleted}
        if args[:3] == ("storage", "container", "exists"):
            return {"exists": self.container}
        if args[:3] == ("storage", "blob", "upload"):
            assert self.container
            self.uploads[args[args.index("--name") + 1]] = Path(args[args.index("--file") + 1]).read_bytes()
            return {}
        if args[:3] == ("deployment", "group", "create"):
            parameters = json.loads(Path(args[args.index("--parameters") + 1][1:]).read_text())
            self.deployed = parameters
            self.resources[self.vmss_id()] = {"tags": {"opspresso.com/delivery-deployment": parameters["deployment"]["value"]}, "properties": {}}
            for kind in ("loadBalancers", "publicIPAddresses"):
                self.resources[f"{APPS}/providers/Microsoft.Network/{kind}/runway-todo-dmz".lower()] = {"properties": {}}
            if parameters["maxReplicas"]["value"] > parameters["replicas"]["value"]:
                self.resources[f"{APPS}/providers/Microsoft.Insights/autoscalesettings/runway-todo-dmz".lower()] = {"properties": {}}
            if self.upgrade_on_model is not None:
                self.instances = [dict(vm, latest=False) for vm in self.instances]
                self.upgrade = self.upgrade_on_model
            return {"properties": {"outputs": {"publicIp": {"value": "20.0.0.10"}}}}
        if args[:2] == ("vmss", "delete-instances"):
            gone = set(args[args.index("--instance-ids") + 1:])
            self.instances = [vm for vm in self.instances if vm["id"] not in gone]
            return None
        if args[:2] == ("vmss", "scale"):
            self.instances += [{"id": "9", "latest": True, "health": "healthy"}]
            return None
        raise AssertionError(f"unexpected az {args}")

    upgrade_on_model = None


class Fixture(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.dir = Path(self.work.name)
        self.jar = self.dir / "application-artifact"
        self.jar.write_bytes(b"jar")

    def tearDown(self):
        self.work.cleanup()

    def deploy(self, azure, env=None, install=b"#!/bin/bash\ntrue\n"):
        vmss.deploy(vmss.plan_of(env or environ()), self.jar, install, azure, Clock(), workdir=self.dir)
        return azure

    def puts(self, azure, part):
        return [(path, body) for method, path, *rest in azure.log if method == "PUT" for body in rest if part in path.lower()]


class Plan(Fixture):
    def test_names_and_secret_names_follow_the_contract(self):
        plan = vmss.plan_of(environ())
        self.assertEqual(plan["name"], "runway-todo-dmz")
        self.assertEqual(plan["container"], "runway-todo-dmz")
        self.assertEqual(plan["label"], "todo-dmz")
        self.assertEqual(plan["runway"]["secrets"], {"DB_PASSWORD": contract_secret("DB_PASSWORD")})
        self.assertEqual(len(plan["runway"]["secrets"]["DB_PASSWORD"]), 67)
        self.assertEqual(plan["runway"]["env"], {"MODE": "prod", "GREETING": "hello", "PORT": "8080"})
        self.assertEqual(plan["runway"]["command"], ["java", "-jar", "application.jar"])
        self.assertEqual(vmss.plan_of(environ(host="todo.dmz.rovr.dev"))["label"], "todo")

    def refused(self, message, **changes):
        target = {**TARGET, **changes.pop("target", {})}
        application = copy.deepcopy(APPLICATION)
        for key, value in changes.pop("environment", {}).items():
            application["environment"][key] = value
        for key, value in changes.pop("runtime", {}).items():
            application["runtime"][key] = value
        if "native" in changes:
            application["native"] = changes.pop("native")
        with self.assertRaisesRegex(vmss.Refused, message):
            vmss.plan_of(environ(target, application, **changes))

    def test_refusals(self):
        self.refused("runs JARs only", native={"format": "war", "javaVersion": 21})
        self.refused("javaRuntime does not match", target={"javaRuntime": "temurin-25-jre"})
        self.refused("does not run arm64", environment={"architecture": "arm64"})
        self.refused("does not run amd64", environment={"instanceType": "Standard_D2ps_v5"})
        self.refused("not an Azure VM size", environment={"instanceType": "t4g.small"})
        self.refused("is not <project>", host="other.dmz.rovr.dev")
        self.refused("is not <project>", host="todo-dmz.apps.rovr.dev")
        self.refused("invalid application port", runtime={"port": 61000})
        self.refused("runtime.health must be a path", runtime={"health": "/h;return 200"})
        self.refused("frontDoorProfileId is missing or malformed", target={"frontDoorProfileId": PROFILE.replace(SUB, "00000000-0000-4000-8000-000000000000")})
        self.refused("subnetId is missing or malformed", target={"subnetId": "subnet-1"})
        self.refused("Target is not of kind azure-vmss", target={"kind": "aws-eb"})
        self.refused("replicas and maxReplicas", environment={"replicas": 3, "maxReplicas": 2})
        self.refused("rollout must be", environment={"rollout": "AllAtOnce"})
        with self.assertRaisesRegex(vmss.Refused, "consecutive hyphens"):
            vmss.plan_of({**environ(), "PROJECT": "to--do", "HOST": "to--do-dmz.dmz.rovr.dev"})

    def test_arm64_sizes_and_resource_id_case(self):
        application = copy.deepcopy(APPLICATION)
        application["environment"].update({"architecture": "arm64", "instanceType": "Standard_B2pts_v2"})
        plan = vmss.plan_of(environ({**TARGET, "dnsZoneId": ZONE.replace("dnsZones", "dnszones")}, application))
        self.assertEqual((plan["architecture"], plan["zoneName"]), ("arm64", "dmz.rovr.dev"))

    def test_removal_needs_no_application(self):
        target = {key: value for key, value in TARGET.items() if key not in ("javaRuntime", "installScript", "subnetId")}
        plan = vmss.plan_of({"PROJECT": "todo", "ENVIRONMENT": "dmz", "DEPLOYMENT": "x", "TARGET": json.dumps(target)}, deploy=False)
        self.assertEqual(plan["labels"], ["todo", "todo-dmz"])


class Deploy(Fixture):
    def test_no_secret_value_leaves_key_vault(self):
        azure = self.deploy(FakeAzure())
        seen = json.dumps([entry for entry in azure.log if entry[0] != "az"]) + json.dumps(azure.deployed)
        seen += "".join(data.decode("latin-1") for data in azure.uploads.values())
        custom = base64.b64decode(azure.deployed["customData"]["value"]).decode()
        seen += custom + base64.b64decode(re.search(r"printf '%s' '([A-Za-z0-9+/=]+)'", custom).group(1)).decode()
        self.assertNotIn(SECRET_VALUE, seen)
        # The instance learns the secret's name only from the bundle, whose SHA-256 the custom data pins.
        config = json.loads(azure.uploads["11111111-1111-4111-8111-111111111111/runway.json"])
        self.assertEqual(config["secrets"], {"DB_PASSWORD": contract_secret("DB_PASSWORD")})
        boot = json.loads(base64.b64decode(re.search(r"printf '%s' '([A-Za-z0-9+/=]+)'", custom).group(1)))
        self.assertEqual(boot["config"]["sha256"], hashlib.sha256(azure.uploads["11111111-1111-4111-8111-111111111111/runway.json"]).hexdigest())
        self.assertEqual(boot["jar"], {"blob": "11111111-1111-4111-8111-111111111111/application.jar", "sha256": "a" * 64, "size": 3})
        self.assertEqual((boot["frontDoorId"], boot["clientId"], boot["javaRuntime"]), (FDID, "cccccccc-0000-4000-8000-000000000002", "temurin-21-jre"))
        self.assertEqual(base64.b64decode(boot["installScript"]), b"#!/bin/bash\ntrue\n")

    def test_identity_reads_only_its_secrets_and_container(self):
        azure = FakeAzure()
        stale = f"{VAULT_ID}/secrets/{contract_secret('OLD')}"
        principal = "pppppppp-0000-4000-8000-000000000001"
        azure.assignments[(stale + "/providers/Microsoft.Authorization/roleAssignments/x").lower()] = {"id": stale + "/providers/Microsoft.Authorization/roleAssignments/x",
                                      "properties": {"principalId": principal, "scope": stale, "roleDefinitionId": vmss.SECRETS_USER}}
        self.deploy(azure)
        granted = sorted((a["properties"]["scope"], a["properties"]["roleDefinitionId"].rsplit("/", 1)[1], a["properties"]["principalType"])
                         for a in azure.assignments.values())
        self.assertEqual(granted, sorted([
            (f"{VAULT_ID}/secrets/{contract_secret('DB_PASSWORD')}", vmss.SECRETS_USER, "ServicePrincipal"),
            (f"{STORAGE_ID}/blobServices/default/containers/runway-todo-dmz", vmss.BLOB_READER, "ServicePrincipal"),
        ]))
        identity = self.puts(azure, "/userassignedidentities/")[0][1]
        self.assertEqual(identity["tags"], {"opspresso.com/delivery-project": "todo", "opspresso.com/delivery-env": "dmz"})

    def test_missing_variable_stops_before_any_instance_changes(self):
        azure = FakeAzure()
        azure.secrets = {}
        with self.assertRaisesRegex(vmss.Refused, "Variable DB_PASSWORD has no value"):
            self.deploy(azure)
        self.assertIsNone(azure.deployed)

    def test_template_parameters_and_front_door(self):
        azure = self.deploy(FakeAzure())
        parameters = {key: value["value"] for key, value in azure.deployed.items()}
        self.assertEqual({key: parameters[key] for key in ("name", "vmSize", "architecture", "replicas", "maxReplicas", "rollout", "healthPath")},
                         {"name": "runway-todo-dmz", "vmSize": "Standard_B2s_v2", "architecture": "amd64", "replicas": 1,
                          "maxReplicas": 1, "rollout": "immutable", "healthPath": "/actuator/health"})
        route = self.puts(azure, "/routes/")[0][1]["properties"]
        self.assertEqual((route["forwardingProtocol"], route["httpsRedirect"], route["linkToDefaultDomain"], route["supportedProtocols"]),
                         ("HttpOnly", "Enabled", "Disabled", ["Http", "Https"]))
        origin = self.puts(azure, "/origins/")[0][1]["properties"]
        self.assertEqual((origin["hostName"], origin["httpPort"], origin["originHostHeader"]), ("20.0.0.10", 80, "todo-dmz.dmz.rovr.dev"))
        probe = self.puts(azure, "/origingroups/")[0][1]["properties"]["healthProbeSettings"]
        self.assertEqual((probe["probePath"], probe["probeProtocol"]), ("/actuator/health", "Http"))
        domain = self.puts(azure, "/customdomains/")[0][1]["properties"]
        self.assertEqual((domain["hostName"], domain["tlsSettings"]["certificateType"]), ("todo-dmz.dmz.rovr.dev", "ManagedCertificate"))
        self.assertEqual(self.puts(azure, "/txt/_dnsauth.todo-dmz")[0][1]["properties"]["TXTRecords"], [{"value": ["token-123"]}])
        self.assertEqual(self.puts(azure, "/cname/todo-dmz")[0][1]["properties"]["CNAMERecord"]["cname"], "runway-g0c4gmctcvfuapbc.z01.azurefd.net")
        self.assertFalse([path for path, _ in self.puts(azure, "/afdendpoints/") if path.lower().endswith("/afdendpoints/runway")],
                         "the workflow never changes the endpoint")

    def test_repeat_changes_nothing_that_already_matches(self):
        azure = self.deploy(FakeAzure())
        before = len(self.puts(azure, "/microsoft.cdn/"))
        self.deploy(azure)
        self.assertEqual(len(self.puts(azure, "/microsoft.cdn/")), before)

    def test_failed_rolling_upgrade_fails_the_deployment(self):
        azure = FakeAzure(existing=True)
        azure.upgrade_on_model = {"runningStatus": {"code": "Faulted", "startTime": "2026-11-17T02:00:00Z"}, "error": {"message": "instance 1 unhealthy"}}
        with self.assertRaisesRegex(vmss.Refused, "rolling upgrade of runway-todo-dmz is Faulted: instance 1 unhealthy"):
            self.deploy(azure)

    def test_an_upgrade_that_never_starts_fails(self):
        azure = FakeAzure(existing=True)
        azure.upgrade = {"runningStatus": {"code": "Completed", "startTime": "earlier"}}
        azure.upgrade_on_model = {"runningStatus": {"code": "Completed", "startTime": "earlier"}}
        with self.assertRaisesRegex(vmss.Refused, "no rolling upgrade"):
            self.deploy(azure)

    def test_all_at_once_replaces_old_instances(self):
        application = copy.deepcopy(APPLICATION)
        application["environment"]["rollout"] = "all-at-once"
        azure = FakeAzure(existing=True)
        azure.upgrade_on_model = {"runningStatus": {"code": "Completed", "startTime": "x"}}
        self.deploy(azure, environ(application=application))
        commands = [entry[1][:2] for entry in azure.log if entry[0] == "az" and entry[1][0] == "vmss"]
        self.assertEqual(commands, [("vmss", "delete-instances"), ("vmss", "scale")])
        self.assertEqual(azure.deployed["rollout"]["value"], "all-at-once")

    def test_autoscale_only_with_room_to_scale(self):
        azure = FakeAzure()
        application = copy.deepcopy(APPLICATION)
        application["environment"]["maxReplicas"] = 3
        self.deploy(azure, environ(application=application))
        autoscale = f"{APPS}/providers/Microsoft.Insights/autoscalesettings/runway-todo-dmz".lower()
        self.assertIn(autoscale, azure.resources)
        self.deploy(azure)
        self.assertNotIn(autoscale, azure.resources)

    def test_verify_needs_this_deployment(self):
        azure = self.deploy(FakeAzure())
        vmss.verify(vmss.plan_of(environ()), azure, Clock())
        other = {**environ(), "DEPLOYMENT": "22222222-2222-4222-8222-222222222222"}
        with self.assertRaisesRegex(vmss.Refused, "did not run Deployment 22222222"):
            vmss.verify(vmss.plan_of(other), azure, Clock(), timeout=30)


class Remove(Fixture):
    def test_removes_everything_and_tolerates_missing(self):
        azure = self.deploy(FakeAzure())
        plan = vmss.plan_of(environ(), deploy=False)
        vmss.remove(plan, azure, Clock())
        self.assertEqual(vmss.remaining(plan, azure), [])
        self.assertEqual(azure.assignments, {})
        deleted = [path.lower() for method, path, *_ in azure.log if method == "DELETE" and "roleassignments" not in path.lower()]
        order = ["/routes/", "/customdomains/", "/origingroups/", "/cname/todo-dmz", "/txt/_dnsauth.todo-dmz",
                 "/virtualmachinescalesets/", "/loadbalancers/", "/publicipaddresses/", "/userassignedidentities/"]
        positions = [next(i for i, path in enumerate(deleted) if marker in path) for marker in order]
        self.assertEqual(positions, sorted(positions))
        role_deletes = [i for i, entry in enumerate(azure.log) if entry[0] == "DELETE" and "roleassignments" in entry[1].lower()]
        identity_delete = next(i for i, entry in enumerate(azure.log) if entry[0] == "DELETE" and "/userassignedidentities/" in entry[1].lower())
        self.assertTrue(role_deletes and max(role_deletes) < identity_delete, "role assignments go before their identity")
        count = len(azure.log)
        vmss.remove(plan, azure, Clock())  # again: everything is missing, nothing fails and nothing is deleted
        self.assertFalse([entry for entry in azure.log[count:] if entry[0] == "DELETE"])

    def test_records_of_another_environment_stay(self):
        azure = self.deploy(FakeAzure())
        bare = f"{ZONE}/CNAME/todo".lower()
        azure.resources[bare] = {"properties": {"CNAMERecord": {"cname": "x"}, "metadata": {"deliveryproject": "todo", "deliveryenv": "prod"}}}
        vmss.remove(vmss.plan_of(environ(), deploy=False), azure, Clock())
        self.assertIn(bare, azure.resources)
        self.assertNotIn(f"{ZONE}/CNAME/todo-dmz".lower(), azure.resources)

    def test_a_failed_lookup_is_not_absence(self):
        azure = FakeAzure()
        original = azure.request
        azure.request = lambda method, url, *rest, **kw: (403, {"error": {"code": "AuthorizationFailed"}}) if method == "GET" else original(method, url, *rest, **kw)
        with self.assertRaisesRegex(vmss.Refused, "answered 403"):
            vmss.remove(vmss.plan_of(environ(), deploy=False), azure, Clock())


class Templates(unittest.TestCase):
    def test_template_matches_the_boot_script(self):
        template = json.loads((FILES / "vmss-app.json").read_text())
        vmss_resource = next(r for r in template["resources"] if r["type"] == "Microsoft.Compute/virtualMachineScaleSets")
        health = vmss_resource["properties"]["virtualMachineProfile"]["extensionProfile"]["extensions"][0]["properties"]
        self.assertEqual((health["type"], health["typeHandlerVersion"], health["settings"]["port"]), ("ApplicationHealthLinux", "2.0", vmss.HEALTH_PORT))
        self.assertIn(f"listen 127.0.0.1:{vmss.HEALTH_PORT};", (FILES / "vmss-boot.sh").read_text())
        self.assertFalse(vmss_resource["properties"]["overprovision"], "overprovisioning would need more vCPU quota")
        self.assertEqual(template["variables"]["rolling"]["maxSurge"], True)
        self.assertEqual(template["variables"]["rolling"]["maxBatchInstancePercent"], 20, "one extra instance for up to 5 replicas")

    def test_ssh_key_has_no_private_key(self):
        """The key only satisfies Azure's Linux requirement: its modulus is SHA-256 output, so no one holds its private key."""
        key = json.loads((FILES / "vmss-app.json").read_text())["variables"]["sshKey"]
        raw = bytearray(b"".join(hashlib.sha256(b"agent-runway vmss: no SSH login" + struct.pack(">I", i)).digest() for i in range(12)))
        raw[0] |= 0x80
        raw[-1] |= 1
        blob = base64.b64decode(key.split()[1])
        self.assertTrue(blob.endswith(struct.pack(">I", 385) + b"\x00" + bytes(raw)))

    def test_boot_script_is_valid_shell(self):
        plan = vmss.plan_of(environ())
        data = vmss.boot_script(plan, "cccccccc-0000-4000-8000-000000000002", FDID, "b" * 64, b"")
        script = base64.b64decode(data).decode()
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        python = re.search(r"<<'PYTHON'\n(.*?)\nPYTHON\n", script, re.S).group(1)
        compile(python, "vmss-boot.sh", "exec")
        self.assertNotIn("__RUNWAY_BOOT__", script)

    def test_adoptium_key_is_the_published_signing_key(self):
        try:
            listing = subprocess.run(["gpg", "--show-keys", "--with-colons", str(FILES / "adoptium.asc")], text=True, capture_output=True, check=True).stdout
        except FileNotFoundError:
            self.skipTest("gpg is not installed")
        self.assertIn("fpr:::::::::3B04D753C9050D9A5D343F39843C48A565F8F04B:", listing)


if __name__ == "__main__":
    unittest.main(buffer=True)
