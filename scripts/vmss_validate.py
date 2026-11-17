"""Validates the azure-vmss resources against a real subscription without changing it (manual check, not in test.sh).

Usage: python3 scripts/vmss_validate.py <target JSON file> [<VM size> <architecture>]
Needs `az login` as a user who may read the Target's resource groups. The script
  1. reads the Front Door profile, its endpoint `runway`, and the Key Vault and storage account through Resource Graph,
     the way vmss-deploy.py does;
  2. runs `az deployment group validate` for vmss-app.json in target.resourceGroup;
  3. wraps the Front Door and DNS request bodies of vmss-deploy.py (origin group, origin, custom domain, route, TXT and
     CNAME records) in ARM templates and validates them in the profile's and the zone's resource groups.
Validation runs the resource providers' preflight checks only; nothing is created, changed or deleted.
"""
import base64
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("vmss_deploy", ROOT / "charts/delivery/files/vmss-deploy.py")
vmss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vmss)


def az(*args):
    result = subprocess.run(["az", *args, "--only-show-errors", "--output", "json"], text=True, capture_output=True)
    return result.returncode, (result.stdout if result.returncode == 0 else result.stderr).strip()


def validate(group, name, template, parameters=None):
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / "template.json"
        path.write_text(json.dumps(template))
        args = ["deployment", "group", "validate", "--resource-group", group, "--name", name, "--template-file", str(path)]
        if parameters:
            (Path(work) / "parameters.json").write_text(json.dumps(parameters))
            args += ["--parameters", f"@{Path(work) / 'parameters.json'}"]
        code, out = az(*args)
    if code:
        print(f"FAIL {name}: {out[:1500]}")
        return False
    resources = [item["id"].split("/providers/", 1)[1] for item in json.loads(out)["properties"].get("validatedResources", [])]
    print(f"ok   {name}: {json.loads(out)['properties']['provisioningState']} {resources}")
    return True


def nested(resource_id, body):
    """An ARM resource for a child resource ID of the template's resource group."""
    provider = resource_id.split("/providers/", 1)[1].split("/")
    namespace, rest = provider[0], provider[1:]
    types, names = rest[0::2], rest[1::2]
    if namespace.lower() == "microsoft.network" and types[0].lower() == "dnszones":
        types = ["dnszones", types[1]]
    return {"type": f"{namespace}/{'/'.join(types)}", "apiVersion": vmss.api_of(resource_id), "name": "/".join(names), **body}


def main(target_file, size="Standard_D2s_v6", architecture="amd64"):
    target = json.loads(Path(target_file).read_text())
    application = {"runtime": {"port": 8080, "health": "/health"}, "native": {"format": "jar", "javaVersion": int(target["javaRuntime"].split("-")[1])},
                   "environment": {"secrets": [], "env": {}, "instanceType": size, "replicas": 1, "maxReplicas": 2,
                                   "rollout": "immutable", "architecture": architecture}, "config": {}}
    zone = target["dnsZoneId"].rsplit("/", 1)[1]
    environ = {"PROJECT": "validate", "ENVIRONMENT": target["name"].replace("-", "")[:12] or "dev", "DEPLOYMENT": "00000000-0000-4000-8000-000000000000",
               "TARGET": json.dumps(target), "APPLICATION": json.dumps(application),
               "ARTIFACT": json.dumps({"sha256": "0" * 64, "size": 1})}
    environ["HOST"] = f"validate-{environ['ENVIRONMENT']}.{zone}"
    plan = vmss.plan_of(environ)
    names = vmss.ids(plan)
    azure = vmss.Azure()
    ok = True

    profile = vmss.arm_get(azure, plan["profile"])
    endpoint = vmss.arm_get(azure, names["endpoint"])
    print(f"ok   profile sku {profile['sku']['name']}, Front Door ID present: {vmss.UUID.fullmatch(profile['properties'].get('frontDoorId', '')) is not None}")
    print(f"ok   endpoint runway host {endpoint['properties']['hostName']}")
    for kind, name in (("microsoft.keyvault/vaults", plan["vault"]), ("microsoft.storage/storageaccounts", plan["storage"])):
        print(f"ok   Resource Graph finds {vmss.find(azure, plan, kind, name)}")

    template = json.loads((ROOT / "charts/delivery/files/vmss-app.json").read_text())
    custom = base64.b64encode(b"#!/bin/bash\ntrue\n").decode()
    parameters = vmss.template_parameters(plan, names["identity"], custom)
    ok &= validate(plan["group"], plan["name"], template, parameters)

    schema = "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#"
    domain, group, origin, route = vmss.front_door(plan, "20.0.0.10")
    cdn = [nested(*domain), nested(*group), {**nested(*origin), "dependsOn": [group[0]]},
           {**nested(*route), "dependsOn": [domain[0], origin[0]]}]
    profile_group = plan["profile"].split("/")[4]
    ok &= validate(profile_group, plan["name"] + "-frontdoor", {"$schema": schema, "contentVersion": "1.0.0.0", "resources": cdn})
    dns = [nested(resource_id, body) for resource_id, body in vmss.records(plan, endpoint["properties"]["hostName"], "validation-token")]
    ok &= validate(plan["zone"].split("/")[4], plan["name"] + "-dns", {"$schema": schema, "contentVersion": "1.0.0.0", "resources": dns})
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) not in (2, 4):
        sys.exit(__doc__)
    main(*sys.argv[1:])
