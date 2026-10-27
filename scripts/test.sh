#!/usr/bin/env bash
# Renders both charts the way Argo CD does and checks the contract of agent-platform docs/APP_DELIVERY.md.
set -euo pipefail
cd "$(dirname "$0")/.."

failures=0
pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; failures=$((failures + 1)); }

# The parameters the projects ApplicationSet sets from the file path and the cluster.
app() {
  local project=$1 environment=$2 values=$3; shift 3
  helm template app charts/app --namespace "app-${project}-${environment}" -f "$values" \
    --set "project=${project}" --set "environment=${environment}" --set domain=apps.rovr.dev \
    --set cluster=eks-demo --set tenantSecretsRoleArn=arn:aws:iam::123456789012:role/agent-broker-tenant-secrets "$@"
}
expect() { if grep -qE "$2" <<<"$3"; then pass "$1"; else fail "$1"; fi; }
refuse() { if grep -qE "$2" <<<"$3"; then fail "$1"; else pass "$1"; fi; }
fails() { local name=$1 pattern=$2; shift 2; local out; if out=$("$@" 2>&1); then fail "$name (rendered)"; elif grep -q "$pattern" <<<"$out"; then pass "$name"; else fail "$name ($out)"; fi; }

helm lint charts/app -f tests/app/dev.yaml --namespace app-todo-dev --set project=todo --set environment=dev \
  --set domain=apps.rovr.dev --set cluster=eks-demo --set tenantSecretsRoleArn=arn:aws:iam::1:role/r >/dev/null && pass "app chart lints" || fail "app chart lints"

dev=$(app todo dev tests/app/dev.yaml)
expect "dev host is <slug>-dev.<domain>" '"todo-dev.apps.rovr.dev"' "$dev"
expect "image is pinned by digest" 'projects/todo@sha256:a{64}' "$dev"
expect "PORT follows runtime.port" 'value: "8080"' "$dev"
expect "plain env is set" 'name: LOG_LEVEL' "$dev"
expect "Variables come from app-env" 'name: app-env' "$dev"
expect "SecretStore tags the Project" 'value: "todo"' "$dev"
expect "ExternalSecret reads this environment" 'path: /agent-broker/eks-demo/todo/dev$' "$dev"
refuse "one replica has no HPA" 'kind: HorizontalPodAutoscaler' "$dev"
refuse "one replica has no PDB" 'kind: PodDisruptionBudget' "$dev"
expect "one replica sets replicas" 'replicas: 1' "$dev"

prod=$(app todo prod tests/app/prod.yaml)
expect "prod host is <slug>.<domain>" '"todo.apps.rovr.dev"' "$prod"
refuse "a values file cannot claim another domain" 'agrt.dev' "$prod"
refuse "a values file cannot move the namespace" 'namespace: app-other' "$prod"
expect "maxReplicas above replicas adds an HPA" 'kind: HorizontalPodAutoscaler' "$prod"
expect "HPA keeps the declared minimum" 'minReplicas: 2' "$prod"
expect "two replicas add a PDB" 'kind: PodDisruptionBudget' "$prod"
refuse "HPA owns replicas" '^  replicas:' "$prod"
expect "command is set" 'command:' "$prod"
expect "health path is probed" 'path: /healthz' "$prod"

fails "a release outside app-<slug>-<env> is refused" "release namespace" \
  helm template app charts/app --namespace app-todo-prod -f tests/app/dev.yaml --set project=todo --set environment=dev \
  --set domain=apps.rovr.dev --set cluster=eks-demo --set tenantSecretsRoleArn=arn:aws:iam::1:role/r
fails "an unknown environment is refused" "must be dev or prod" app todo stage tests/app/dev.yaml
fails "a tag instead of a digest is refused" "not a sha256 digest" app todo dev tests/app/dev.yaml --set image.digest=latest
fails "an invalid slug is refused" "not a Broker Project slug" app Todo dev tests/app/dev.yaml

delivery=$(helm template delivery charts/delivery --namespace app-delivery \
  --set registry=123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/projects --set pushRoleArn=arn:aws:iam::123456789012:role/app-delivery-push)
expect "delivery renders the WorkflowTemplate" 'kind: WorkflowTemplate' "$delivery"
expect "Broker may submit Workflows" 'name: agent-broker' "$delivery"
refuse "Helm leaves no template of its own" '\{\{ *\.' "$delivery"
expect "Argo parameters reach the WorkflowTemplate" '\{\{workflow\.parameters\.project\}\}' "$delivery"
tasks=$(python3 - <<'PY'
import yaml
spec = yaml.safe_load(open("charts/delivery/files/workflow-template.yaml"))["spec"]
dag = next(t for t in spec["templates"] if t["name"] == "delivery")["dag"]["tasks"]
print(" ".join(task["name"] for task in dag))
names = {t["name"]: t for t in spec["templates"]}
for task in ("test", "build", "check-application"):
    assert names[task].get("serviceAccountName") is None, task  # runs as delivery-runner without credentials
print(sorted(p["name"] for p in spec["arguments"]["parameters"]))
PY
)
expect "the DAG keeps the step names Broker maps" '^checkout check-application test registry build deploy verify$' "$(head -1 <<<"$tasks")"
expect "the template takes Broker's parameters" "\['application', 'applicationSha256', 'delivery', 'environment', 'host', 'project', 'ref', 'repository'\]" "$tasks"

if command -v argo >/dev/null; then
  argo lint --offline charts/delivery/files/workflow-template.yaml >/dev/null && pass "argo lint accepts the WorkflowTemplate" || fail "argo lint accepts the WorkflowTemplate"
else
  echo "skip argo lint (argo CLI not installed)"
fi

if [ "$failures" -gt 0 ]; then echo "$failures check(s) failed"; exit 1; fi
echo "all checks passed"
