#!/usr/bin/env bash
# Renders both charts the way Argo CD does and checks the contract of agent-platform docs/APP_DELIVERY.md and
# docs/DELIVERY_V2.md.
set -euo pipefail
cd "$(dirname "$0")/.."

failures=0
pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; failures=$((failures + 1)); }

helm dependency build charts/app >/dev/null

# The parameters the projects ApplicationSet sets from the directory path, the environment profile and the cluster.
# ENVIRONMENTS and PRIMARY are the profile (argocd-env-addons app_delivery); the default is [dev, prod] with prod primary.
ENVIRONMENTS="dev prod"
PRIMARY=prod
app() {
  local project=$1 environment=$2 values=$3; shift 3
  local host="${project}-${environment}.apps.rovr.dev"
  [ "$environment" = "$PRIMARY" ] && host="${project}.apps.rovr.dev"
  helm template app charts/app --namespace "app-${project}-${environment}" -f "$values" \
    --set "app.tenant.project=${project}" --set "app.tenant.environment=${environment}" \
    --set "app.tenant.environments=${ENVIRONMENTS}" --set "app.tenant.primaryEnvironment=${PRIMARY}" \
    --set app.tenant.domain=apps.rovr.dev --set app.tenant.cluster=eks-demo \
    --set app.tenant.tenantSecretsRoleArn=arn:aws:iam::123456789012:role/agent-delivery-tenant-secrets \
    --set "app.routing.hosts[0]=${host}" "$@"
}
# Runs a command with another environment profile: profile <environments> <primary> <command...>.
profile() { local ENVIRONMENTS=$1 PRIMARY=$2; shift 2; "$@"; }
expect() { if grep -qE -- "$2" <<<"$3"; then pass "$1"; else fail "$1"; fi; }
refuse() { if grep -qE -- "$2" <<<"$3"; then fail "$1"; else pass "$1"; fi; }
fails() { local name=$1 pattern=$2; shift 2; local out; if out=$("$@" 2>&1); then fail "$name (rendered)"; elif grep -q -- "$pattern" <<<"$out"; then pass "$name"; else fail "$name ($out)"; fi; }

# The deploy step renders the values with charts/delivery/files/render.jq; the fixtures must be its output.
render() {
  jq -n --argjson application "$1" --arg deployment 11111111-1111-4111-8111-111111111111 \
    --arg image "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/projects/todo@sha256:$2" -f charts/delivery/files/render.jq | yq -p json -o yaml
}
DEV_APP='{"runtime":{"port":8080,"health":"/health","build":{"context":".","dockerfile":"Dockerfile"}},"environment":{"replicas":1,"maxReplicas":1,"cpu":"100m","memory":"256Mi","resources":[],"secrets":[],"env":{"LOG_LEVEL":"debug"}}}'
PROD_APP='{"runtime":{"port":3000,"health":"/healthz","build":{"context":".","dockerfile":"Dockerfile"},"command":["node"],"args":["server.js"]},"environment":{"replicas":2,"maxReplicas":4,"cpu":"250m","memory":"512Mi","resources":[],"secrets":[],"env":{}}}'
if diff -q <(render "$DEV_APP" "$(printf 'a%.0s' $(seq 64))") tests/app/dev.yaml >/dev/null \
  && diff -q <(render "$PROD_APP" "$(printf 'b%.0s' $(seq 64))") tests/app/prod.yaml >/dev/null; then
  pass "fixtures are render.jq output"; else fail "fixtures are render.jq output"; fi

dev=$(app todo dev tests/app/dev.yaml)
expect "dev host is <slug>-dev.<domain>" '"todo-dev.apps.rovr.dev"' "$dev"
expect "image is pinned by digest" 'projects/todo@sha256:a{64}' "$dev"
expect "PORT follows runtime.port" 'value: "8080"' "$dev"
expect "plain env is set" 'name: LOG_LEVEL' "$dev"
expect "Variables come from app-env" 'name: "?app-env"?$' "$dev"
expect "an app without Variables still starts" 'optional: true' "$dev"
expect "the app runs as ServiceAccount app" 'serviceAccountName: "?app"?' "$dev"
expect "the Pod gets no API token" 'automountServiceAccountToken: false' "$dev"
expect "privilege escalation is off" 'allowPrivilegeEscalation: false' "$dev"
expect "the Deployment annotation lets verify find this rollout" 'opspresso.com/deployment: "?11111111' "$dev"
expect "each Deployment refreshes app-env before the rollout" 'force-sync: "?11111111' "$dev"
expect "the workload has a Service" 'kind: Service$' "$dev"
expect "the workload has an HTTPRoute" 'kind: HTTPRoute$' "$dev"
expect "SecretStore tags the Project" 'key: delivery-project' "$dev"
expect "ExternalSecret reads this environment" "path: '?/agent-delivery/eks-demo/todo/dev'?$" "$dev"
expect "apps reach the cluster DNS Service IP" 'cidr: 172.20.0.10/32' "$dev"
expect "egress excludes instance metadata" '- 169\.254\.0\.0/16' "$dev"
expect "the environment has a ResourceQuota" 'kind: ResourceQuota' "$dev"
expect "the ResourceQuota allows two RdsPostgres instances" 'count/rdspostgres.kro.run: "2"' "$dev"
refuse "one replica has no HPA" 'kind: HorizontalPodAutoscaler' "$dev"
refuse "one replica has no PDB" 'kind: PodDisruptionBudget' "$dev"

prod=$(app todo prod tests/app/prod.yaml -f tests/app/override.yaml)
expect "prod host is <slug>.<domain>" '"todo.apps.rovr.dev"' "$prod"
refuse "a values file cannot claim another domain" 'agrt.dev' "$prod"
refuse "a values file cannot move the namespace" 'namespace: app-other' "$prod"
refuse "a values file cannot rename the environment" '/agent-delivery/eks-demo/[a-z-]+/dev' "$prod"
expect "maxReplicas above replicas adds an HPA" 'kind: HorizontalPodAutoscaler' "$prod"
expect "HPA keeps the declared minimum" 'minReplicas: 2' "$prod"
expect "two replicas add a PDB" 'kind: PodDisruptionBudget' "$prod"
expect "command is set" 'command:' "$prod"
expect "health path is probed" 'path: /healthz' "$prod"

fails "a release outside app-<slug>-<env> is refused" "release namespace" \
  helm template app charts/app --namespace app-todo-prod -f tests/app/dev.yaml --set app.tenant.project=todo --set app.tenant.environment=dev \
  --set 'app.tenant.environments=dev prod' --set app.tenant.primaryEnvironment=prod \
  --set app.tenant.domain=apps.rovr.dev --set app.tenant.cluster=eks-demo --set app.tenant.tenantSecretsRoleArn=arn:aws:iam::1:role/r --set 'app.routing.hosts[0]=todo-dev.apps.rovr.dev'
fails "another host is refused" "routing.hosts must be exactly" app todo dev tests/app/dev.yaml --set 'app.routing.hosts[0]=studio.agrt.dev'
fails "a non-primary environment cannot claim <slug>.<domain>" "routing.hosts must be exactly" app todo dev tests/app/dev.yaml --set 'app.routing.hosts[0]=todo.apps.rovr.dev'
fails "an unknown environment is refused" "is not one of tenant.environments" app todo staging tests/app/dev.yaml
# tests/app/override.yaml lists staging and makes it primary; the ApplicationSet parameters replace both.
fails "a values file cannot add an environment" "is not one of tenant.environments" app todo staging tests/app/override.yaml
fails "an invalid environment name is refused" "invalid name" profile "dev Prod" prod app todo dev tests/app/dev.yaml
fails "the primary environment must be in the profile" "primaryEnvironment" profile "dev prod" live app todo dev tests/app/dev.yaml
fails "a slug that ends with an environment is refused" "must not end with -dev" app todo-dev prod tests/app/prod.yaml
fails "a tag instead of a digest is refused" "pin the image by digest" app todo dev tests/app/dev.yaml --set app.image.reference=nginx:latest
fails "an invalid slug is refused" "not an Agent Delivery Project slug" app Todo dev tests/app/dev.yaml

# A profile with more environments: each has its own namespace and host; the primary one is <slug>.<domain>.
staging=$(profile "dev staging prod" prod app todo staging tests/app/dev.yaml)
expect "an added environment is served on <slug>-<env>.<domain>" '"todo-staging.apps.rovr.dev"' "$staging"
expect "an added environment reads its own Variables" "path: '?/agent-delivery/eks-demo/todo/staging'?$" "$staging"
live=$(profile "dev live" live app todo live tests/app/prod.yaml)
expect "the primary environment is served on <slug>.<domain>" '"todo.apps.rovr.dev"' "$live"

# Before the first deployment (no app.yaml) the environment has its tenant objects only, so resources.yaml can use app-env.
base=$(app todo dev /dev/null)
for kind in ServiceAccount SecretStore ExternalSecret NetworkPolicy ResourceQuota; do
  expect "an environment without app.yaml has its $kind" "kind: ${kind}$" "$base"
done
for kind in Deployment Service HTTPRoute; do
  refuse "an environment without app.yaml has no $kind" "kind: ${kind}$" "$base"
done

# Every deployed environment renders: its directory is an allowed environment and its app.yaml a valid workload.
for values in projects/*/*/app.yaml; do
  environment=$(basename "$(dirname "$values")"); project=$(basename "$(dirname "$(dirname "$values")")")
  expect "$values deploys its workload" 'kind: Deployment' "$(app "$project" "$environment" "$values" 2>&1)"
done

delivery=$(helm template delivery charts/delivery --namespace app-delivery \
  --set registry=123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/projects --set pushRoleArn=arn:aws:iam::123456789012:role/app-delivery-push \
  --set 'apiServer.cidrs={172.20.0.1/32,10.10.114.40/32}')
for name in app-build app-deploy resource-sync; do
  expect "delivery renders WorkflowTemplate $name" "^  name: ${name}$" "$delivery"
done
secret_verbs=$(yq 'select(.kind == "Role" and .metadata.name == "agent-delivery") | .rules[] | select(.resources[] == "secrets") | .verbs | join(",")' <<<"$delivery")
if [ "$secret_verbs" = "create" ]; then pass "Agent Delivery may only create Secrets"; else fail "Agent Delivery may only create Secrets ($secret_verbs)"; fi
expect "Agent Delivery may submit Workflows" 'name: agent-delivery' "$delivery"
resource_verbs=$(yq 'select(.kind == "ClusterRole" and .metadata.name == "agent-delivery-resources") | .rules[] | (.apiGroups | join(",")) + " " + (.verbs | join(","))' <<<"$delivery")
if [ "$resource_verbs" = "kro.run get,list,watch" ]; then pass "Agent Delivery may only read kro instances"; else fail "Agent Delivery may only read kro instances ($resource_verbs)"; fi
refuse "Helm leaves no template of its own" '\{\{ *\.' "$delivery"
expect "the runner executor has its token Secret" 'name: delivery-runner.service-account-token' "$delivery"
expect "runner Pods reach the cluster DNS Service IP" 'cidr: 172.20.0.10/32' "$delivery"
expect "runner Pods reach the API server on 443" 'cidr: 172.20.0.1/32' "$delivery"
fails "the API server address is required" "apiServer.cidrs is required" \
  helm template delivery charts/delivery --namespace app-delivery --set registry=x/projects --set pushRoleArn=arn:aws:iam::1:role/p
expect "Argo parameters reach the WorkflowTemplates" '\{\{workflow\.parameters\.project\}\}' "$delivery"
expect "resource-sync accepts RdsPostgres instances" 'RESOURCE_KINDS: "RdsPostgres"' "$delivery"

contract=$(python3 scripts/workflow_contract.py)
line() { grep -E "^$1 $2( |$)" <<<"$contract" | cut -d' ' -f3-; }
expect "app-build keeps the step names Agent Delivery maps" '^checkout check-application test registry build$' "$(line app-build tasks)"
expect "app-build takes Agent Delivery's parameters" '^release project repository ref applicationSha256 runtime$' "$(line app-build parameters)"
expect "app-build returns revision and image" '^image revision$' "$(line app-build outputs)"
expect "app-deploy keeps the step names Agent Delivery maps" '^deploy verify$' "$(line app-deploy tasks)"
expect "app-deploy takes Agent Delivery's parameters" '^deployment project environment image application host$' "$(line app-deploy parameters)"
expect "app-deploy returns url" '^url$' "$(line app-deploy outputs)"
expect "resource-sync has one write step" '^write$' "$(line resource-sync tasks)"
expect "resource-sync takes Agent Delivery's parameters" '^sync project environment manifest$' "$(line resource-sync parameters)"
expect "resource-sync returns nothing" '^$' "$(line resource-sync outputs)"
expect "only deploy and write hold the deploy key, as delivery-git" '^app-deploy:deploy:delivery-git resource-sync:write:delivery-git$' "$(line all deploy-key)"
expect "a changed declaration exits 3 for Agent Delivery" 'exit 3' "$(cat charts/delivery/files/app-build.yaml)"
expect "deploy refuses an image of another Project" 'is not an image of \$\{REGISTRY\}/\$\{PROJECT\} pinned by digest' "$(cat charts/delivery/files/app-deploy.yaml)"

# resource-sync writes only allowed kro instances of the environment's namespace.
check() { sh charts/delivery/files/check-resources.sh app-todo-dev RdsPostgres; }
rds='apiVersion: kro.run/v1alpha1
kind: RdsPostgres
metadata:
  name: main
spec:
  passwordSecret: {name: app-env, key: MAIN_PGPASSWORD}'
expect "an RdsPostgres instance is written" '^kind: RdsPostgres$' "$(check <<<"$rds" 2>&1)"
fails "an empty manifest is refused" "manifest is empty" check </dev/null
fails "a manifest of empty documents is refused" "no document" check <<<"$(printf -- '---\n# none\n')"
if out=$(check <<<'{"apiVersion": "v1", "kind": "List", "items": []}' 2>&1) && [ -z "$out" ]; then
  pass "the explicit empty List removes every resource"; else fail "the explicit empty List removes every resource ($out)"; fi
fails "a List with items is refused" "only be the empty list" check <<<"$(printf 'apiVersion: v1\nkind: List\nitems:\n  - %s\n' '{"kind": "Secret"}')"
long=$(printf 'a%.0s' $(seq 50))
expect "a 50-character name is written" "name: ${long}$" "$(check <<<"${rds/name: main/name: ${long}}" 2>&1)"
fails "a 51-character name is refused" "uniquely named" check <<<"${rds/name: main/name: ${long}b}"
fails "a name that ends with a hyphen is refused" "uniquely named" check <<<"${rds/name: main/name: main-}"
fails "another kind is refused" "kinds \[RdsPostgres\]" check <<<"$(printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: x\n')"
fails "another API group is refused" "kinds \[RdsPostgres\]" check <<<"${rds/kro.run/rds.services.k8s.aws}"
fails "another namespace is refused" "namespace app-todo-dev" check <<<"${rds/name: main/name: main
  namespace: app-other-dev}"
fails "a duplicate name is refused" "uniquely named" check <<<"$(printf '%s\n---\n%s' "$rds" "$rds")"
fails "a manifest that is not YAML is refused" "" check <<<"kind: [unclosed"

if command -v argo >/dev/null; then
  for name in app-build app-deploy resource-sync; do
    if argo lint --offline "charts/delivery/files/$name.yaml" >/dev/null; then pass "argo lint accepts $name"; else fail "argo lint accepts $name"; fi
  done
else
  echo "skip argo lint (argo CLI not installed)"
fi

if [ "$failures" -gt 0 ]; then echo "$failures check(s) failed"; exit 1; fi
echo "all checks passed"
