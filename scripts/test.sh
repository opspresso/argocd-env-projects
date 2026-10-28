#!/usr/bin/env bash
# Renders both charts the way Argo CD does and checks the contract of agent-platform docs/APP_DELIVERY.md.
set -euo pipefail
cd "$(dirname "$0")/.."

failures=0
pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; failures=$((failures + 1)); }

helm dependency build charts/app >/dev/null

# The parameters the projects ApplicationSet sets from the file path and the cluster.
app() {
  local project=$1 environment=$2 values=$3; shift 3
  local host="${project}-${environment}.apps.rovr.dev"
  [ "$environment" = prod ] && host="${project}.apps.rovr.dev"
  helm template app charts/app --namespace "app-${project}-${environment}" -f "$values" \
    --set "app.tenant.project=${project}" --set "app.tenant.environment=${environment}" --set app.tenant.domain=apps.rovr.dev \
    --set app.tenant.cluster=eks-demo --set app.tenant.tenantSecretsRoleArn=arn:aws:iam::123456789012:role/agent-delivery-tenant-secrets \
    --set "app.routing.hosts[0]=${host}" "$@"
}
expect() { if grep -qE -- "$2" <<<"$3"; then pass "$1"; else fail "$1"; fi; }
refuse() { if grep -qE -- "$2" <<<"$3"; then fail "$1"; else pass "$1"; fi; }
fails() { local name=$1 pattern=$2; shift 2; local out; if out=$("$@" 2>&1); then fail "$name (rendered)"; elif grep -q -- "$pattern" <<<"$out"; then pass "$name"; else fail "$name ($out)"; fi; }

# The workflow renders the values with charts/delivery/files/render.jq; the fixtures must be its output.
render() {
  jq -n --argjson application "$1" --arg delivery 11111111-1111-4111-8111-111111111111 --arg revision 0123456789abcdef0123456789abcdef01234567 \
    --arg repository 123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/projects/todo --arg digest "sha256:$2" -f charts/delivery/files/render.jq | yq -p json -o yaml
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
expect "the Delivery annotation lets verify find this rollout" 'opspresso.com/delivery: "?11111111' "$dev"
expect "SecretStore tags the Project" 'key: delivery-project' "$dev"
expect "ExternalSecret reads this environment" "path: '?/agent-delivery/eks-demo/todo/dev'?$" "$dev"
expect "apps reach the cluster DNS Service IP" 'cidr: 172.20.0.10/32' "$dev"
expect "egress excludes instance metadata" '- 169\.254\.0\.0/16' "$dev"
expect "the environment has a ResourceQuota" 'kind: ResourceQuota' "$dev"
refuse "one replica has no HPA" 'kind: HorizontalPodAutoscaler' "$dev"
refuse "one replica has no PDB" 'kind: PodDisruptionBudget' "$dev"

prod=$(app todo prod tests/app/prod.yaml -f tests/app/override.yaml)
expect "prod host is <slug>.<domain>" '"todo.apps.rovr.dev"' "$prod"
refuse "a values file cannot claim another domain" 'agrt.dev' "$prod"
refuse "a values file cannot move the namespace" 'namespace: app-other' "$prod"
expect "maxReplicas above replicas adds an HPA" 'kind: HorizontalPodAutoscaler' "$prod"
expect "HPA keeps the declared minimum" 'minReplicas: 2' "$prod"
expect "two replicas add a PDB" 'kind: PodDisruptionBudget' "$prod"
expect "command is set" 'command:' "$prod"
expect "health path is probed" 'path: /healthz' "$prod"

fails "a release outside app-<slug>-<env> is refused" "release namespace" \
  helm template app charts/app --namespace app-todo-prod -f tests/app/dev.yaml --set app.tenant.project=todo --set app.tenant.environment=dev \
  --set app.tenant.domain=apps.rovr.dev --set app.tenant.cluster=eks-demo --set app.tenant.tenantSecretsRoleArn=arn:aws:iam::1:role/r --set 'app.routing.hosts[0]=todo-dev.apps.rovr.dev'
fails "another host is refused" "routing.hosts must be exactly" app todo dev tests/app/dev.yaml --set 'app.routing.hosts[0]=studio.agrt.dev'
fails "an unknown environment is refused" "must be dev or prod" app todo stage tests/app/dev.yaml
fails "a tag instead of a digest is refused" "pin the image by digest" app todo dev tests/app/dev.yaml --set app.image.reference=nginx:latest
fails "an invalid slug is refused" "not an Agent Delivery Project slug" app Todo dev tests/app/dev.yaml

delivery=$(helm template delivery charts/delivery --namespace app-delivery \
  --set registry=123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/projects --set pushRoleArn=arn:aws:iam::123456789012:role/app-delivery-push \
  --set 'apiServer.cidrs={172.20.0.1/32,10.10.114.40/32}')
expect "delivery renders the WorkflowTemplate" 'kind: WorkflowTemplate' "$delivery"
expect "Agent Delivery may submit Workflows" 'name: agent-delivery' "$delivery"
refuse "Helm leaves no template of its own" '\{\{ *\.' "$delivery"
expect "the runner executor has its token Secret" 'name: delivery-runner.service-account-token' "$delivery"
expect "runner Pods reach the cluster DNS Service IP" 'cidr: 172.20.0.10/32' "$delivery"
expect "runner Pods reach the API server on 443" 'cidr: 172.20.0.1/32' "$delivery"
fails "the API server address is required" "apiServer.cidrs is required" \
  helm template delivery charts/delivery --namespace app-delivery --set registry=x/projects --set pushRoleArn=arn:aws:iam::1:role/p
expect "Argo parameters reach the WorkflowTemplate" '\{\{workflow\.parameters\.project\}\}' "$delivery"
tasks=$(python3 - <<'PY'
import yaml
spec = yaml.safe_load(open("charts/delivery/files/workflow-template.yaml"))["spec"]
dag = next(t for t in spec["templates"] if t["name"] == "delivery")["dag"]["tasks"]
print(" ".join(task["name"] for task in dag))
names = {t["name"]: t for t in spec["templates"]}
for task in ("test", "build", "check-application"):
    assert names[task].get("serviceAccountName") is None, task  # runs as delivery-runner without credentials
    # Only the executor holds the token; the step's own code cannot read it.
    assert names[task].get("automountServiceAccountToken") is False, task
    assert names[task]["executor"]["serviceAccountName"] == "delivery-runner", task
print(sorted(p["name"] for p in spec["arguments"]["parameters"]))
PY
)
expect "the DAG keeps the step names Agent Delivery maps" '^checkout check-application test registry build deploy verify$' "$(head -1 <<<"$tasks")"
expect "a changed declaration exits 3 for Agent Delivery" 'exit 3' "$(cat charts/delivery/files/workflow-template.yaml)"
expect "the template takes Agent Delivery's parameters" "\['application', 'applicationSha256', 'delivery', 'environment', 'host', 'project', 'ref', 'repository'\]" "$tasks"

if command -v argo >/dev/null; then
  argo lint --offline charts/delivery/files/workflow-template.yaml >/dev/null && pass "argo lint accepts the WorkflowTemplate" || fail "argo lint accepts the WorkflowTemplate"
else
  echo "skip argo lint (argo CLI not installed)"
fi

if [ "$failures" -gt 0 ]; then echo "$failures check(s) failed"; exit 1; fi
echo "all checks passed"
