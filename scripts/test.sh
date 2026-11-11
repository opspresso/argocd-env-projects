#!/usr/bin/env bash
# Renders both charts the way Argo CD does and checks the contract of agent-platform docs/APP_DELIVERY.md and
# docs/DELIVERY_V2.md.
set -euo pipefail
cd "$(dirname "$0")/.."

failures=0
pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; failures=$((failures + 1)); }

helm dependency build charts/app >/dev/null
# A workload values file without a host.
hostless=$(mktemp); trap 'rm -f "$hostless"' EXIT

# The parameters the projects ApplicationSet sets from the directory path and the cluster. The host comes from the
# values file (app.yaml), where the deploy step wrote the one Agent Runway chose.
app() {
  local project=$1 environment=$2 values=$3; shift 3
  helm template app charts/app --namespace "app-${project}-${environment}" -f "$values" \
    --set "app.tenant.project=${project}" --set "app.tenant.environment=${environment}" \
    --set app.tenant.domain=apps.rovr.dev --set app.tenant.cluster=eks-demo \
    --set app.tenant.tenantSecretsRoleArn=arn:aws:iam::123456789012:role/agent-runway-tenant-secrets \
    --set "app.namespace.labels.opspresso\.com/delivery-project=${project}" \
    --set "app.namespace.labels.opspresso\.com/delivery-env=${environment}" "$@"
}
expect() { if grep -qE -- "$2" <<<"$3"; then pass "$1"; else fail "$1"; fi; }
refuse() { if grep -qE -- "$2" <<<"$3"; then fail "$1"; else pass "$1"; fi; }
fails() { local name=$1 pattern=$2; shift 2; local out; if out=$("$@" 2>&1); then fail "$name (rendered)"; elif grep -q -- "$pattern" <<<"$out"; then pass "$name"; else fail "$name ($out)"; fi; }

# The deploy step renders the values with charts/delivery/files/render.jq; the fixtures must be its output.
render() {
  jq -n --argjson application "$1" --arg deployment 11111111-1111-4111-8111-111111111111 --arg host "$3" \
    --arg image "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/projects/todo@sha256:$2" -f charts/delivery/files/render.jq | yq -p json -o yaml
}
DEV_APP='{"runtime":{"port":8080,"health":"/health","build":{"context":".","dockerfile":"Dockerfile"}},"environment":{"replicas":1,"maxReplicas":1,"cpu":"100m","memory":"256Mi","architecture":"arm64","resources":[],"secrets":[],"env":{"LOG_LEVEL":"debug"}},"config":{"PGHOST":"db.app-todo-dev.svc.cluster.local","PGPORT":"5432"}}'
PROD_APP='{"runtime":{"port":3000,"health":"/healthz","build":{"context":".","dockerfile":"Dockerfile"},"command":["node"],"args":["server.js"]},"environment":{"replicas":2,"maxReplicas":4,"cpu":"250m","memory":"512Mi","architecture":"amd64","resources":[],"secrets":[],"env":{}}}'
if diff -q <(render "$DEV_APP" "$(printf 'a%.0s' $(seq 64))" todo-dev.apps.rovr.dev) tests/app/dev.yaml >/dev/null \
  && diff -q <(render "$PROD_APP" "$(printf 'b%.0s' $(seq 64))" todo.apps.rovr.dev) tests/app/prod.yaml >/dev/null; then
  pass "fixtures are render.jq output"; else fail "fixtures are render.jq output"; fi
# environment.architecture picks the nodes: a Deployment without it (before Agent Runway V5) has no constraint.
no_arch=$(render "$(jq -c 'del(.environment.architecture)' <<<"$DEV_APP")" "$(printf 'a%.0s' $(seq 64))" todo-dev.apps.rovr.dev)
refuse "a declaration without architecture has no nodeSelector" 'nodeSelector' "$no_arch"
refuse "a declaration without architecture has no tolerations" 'tolerations' "$no_arch"
fails "an unknown architecture is refused" "architecture must be amd64 or arm64" \
  render "$(jq -c '.environment.architecture = "s390x"' <<<"$DEV_APP")" "$(printf 'a%.0s' $(seq 64))" todo-dev.apps.rovr.dev

dev=$(app todo dev tests/app/dev.yaml)
expect "the host is the one app.yaml carries" '"todo-dev.apps.rovr.dev"' "$dev"
expect "image is pinned by digest" 'projects/todo@sha256:a{64}' "$dev"
expect "PORT follows runtime.port" 'value: "8080"' "$dev"
expect "plain env is set" 'name: LOG_LEVEL' "$dev"
expect "secret Variables come from app-env" 'name: "?app-env"?$' "$dev"
expect "Variables that are not secret are a ConfigMap" 'kind: ConfigMap$' "$dev"
expect "the ConfigMap holds the plain Variables" 'PGHOST: "?db.app-todo-dev.svc.cluster.local"?' "$dev"
expect "the Pods read the ConfigMap" 'configMapRef:' "$dev"
expect "a changed plain Variable restarts the Pods" 'checksum/config:' "$dev"
expect "an app without Variables still starts" 'optional: true' "$dev"
expect "the app runs as ServiceAccount app" 'serviceAccountName: "?app"?' "$dev"
expect "the Pod gets no API token" 'automountServiceAccountToken: false' "$dev"
expect "privilege escalation is off" 'allowPrivilegeEscalation: false' "$dev"
expect "the Deployment annotation lets verify find this rollout" 'opspresso.com/deployment: "?11111111' "$dev"
expect "each Deployment refreshes app-env before the rollout" 'force-sync: "?11111111' "$dev"
expect "the workload has a Service" 'kind: Service$' "$dev"
expect "the workload has an HTTPRoute" 'kind: HTTPRoute$' "$dev"
expect "SecretStore tags the Project" 'key: delivery-project' "$dev"
expect "ExternalSecret reads this environment" "path: '?/agent-runway/eks-demo/todo/dev'?$" "$dev"
expect "app-env is deleted when the environment has no Variables" "deletionPolicy: Delete$" "$dev"
expect "apps reach the cluster DNS Service IP" 'cidr: 172.20.0.10/32' "$dev"
expect "egress excludes instance metadata" '- 169\.254\.0\.0/16' "$dev"
expect "the environment owns its namespace, so Argo CD deletes it with the Application" 'kind: Namespace$' "$dev"
expect "the namespace carries the tenant labels the secret store policy checks" 'opspresso.com/delivery-project: "?todo"?$' "$dev"
expect "the namespace is restricted to the baseline Pod security level" 'pod-security.kubernetes.io/enforce: "?baseline"?$' "$dev"
expect "the environment has a ResourceQuota" 'kind: ResourceQuota' "$dev"
expect "the ResourceQuota allows two RdsPostgres instances" 'count/rdspostgres.kro.run: "2"' "$dev"
expect "the ResourceQuota allows two S3Bucket instances" 'count/s3buckets.kro.run: "2"' "$dev"
expect "the ResourceQuota allows two DynamoTable instances" 'count/dynamotables.kro.run: "2"' "$dev"
expect "the ResourceQuota bounds volumes" 'persistentvolumeclaims: "4"' "$dev"
expect "arm64 Pods run on arm64 nodes" 'kubernetes.io/arch: "?arm64"?$' "$dev"
refuse "arm64 Pods do not tolerate the amd64 NodePool" 'opspresso.com/arch' "$dev"
refuse "one replica has no HPA" 'kind: HorizontalPodAutoscaler' "$dev"
refuse "one replica has no PDB" 'kind: PodDisruptionBudget' "$dev"

prod=$(app todo prod tests/app/prod.yaml -f tests/app/override.yaml)
expect "an environment may be served on <slug>.<domain>" '"todo.apps.rovr.dev"' "$prod"
refuse "an environment without plain Variables has no ConfigMap" 'kind: ConfigMap$' "$prod"
refuse "a values file cannot move the tenant to another domain" 'agrt.dev' "$prod"
refuse "a values file cannot move the namespace" 'namespace: app-other' "$prod"
refuse "a values file cannot rename the environment" '/agent-runway/eks-demo/[a-z-]+/dev' "$prod"
expect "maxReplicas above replicas adds an HPA" 'kind: HorizontalPodAutoscaler' "$prod"
expect "HPA keeps the declared minimum" 'minReplicas: 2' "$prod"
expect "two replicas add a PDB" 'kind: PodDisruptionBudget' "$prod"
expect "command is set" 'command:' "$prod"
expect "health path is probed" 'path: /healthz' "$prod"
expect "amd64 Pods run on amd64 nodes" 'kubernetes.io/arch: "?amd64"?$' "$prod"
expect "amd64 Pods tolerate the amd64 NodePool taint" 'key: "?opspresso.com/arch"?$' "$prod"
expect "apps reach the Pod Identity agent" 'cidr: 169.254.170.23/32' "$prod"

fails "a release outside app-<slug>-<env> is refused" "release namespace" \
  helm template app charts/app --namespace app-todo-prod -f tests/app/dev.yaml --set app.tenant.project=todo --set app.tenant.environment=dev \
  --set app.tenant.domain=apps.rovr.dev --set app.tenant.cluster=eks-demo --set app.tenant.tenantSecretsRoleArn=arn:aws:iam::1:role/r
fails "a host on another domain is refused" "routing.hosts must be exactly" app todo dev tests/app/dev.yaml --set 'app.routing.hosts[0]=studio.agrt.dev'
fails "a values file cannot claim another domain" "routing.hosts must be exactly" app todo prod tests/app/prod.yaml -f tests/app/override-host.yaml
fails "another Project's host is refused" "routing.hosts must be exactly" app todo dev tests/app/dev.yaml --set 'app.routing.hosts[0]=shop.apps.rovr.dev'
fails "another environment's host is refused" "routing.hosts must be exactly" app todo dev tests/app/dev.yaml --set 'app.routing.hosts[0]=todo-prod.apps.rovr.dev'
fails "a second host is refused" "routing.hosts must be exactly" app todo dev tests/app/dev.yaml --set 'app.routing.hosts[1]=todo.apps.rovr.dev'
yq 'del(.app.routing.hosts)' tests/app/dev.yaml > "$hostless"
fails "a workload without a host is refused" "routing.hosts must be exactly" app todo dev "$hostless"
expect "every environment may be served on <slug>.<domain>" '"todo.apps.rovr.dev"' "$(app todo dev tests/app/dev.yaml --set 'app.routing.hosts[0]=todo.apps.rovr.dev')"
expect "the last stage may carry its environment name" '"todo-prod.apps.rovr.dev"' "$(app todo prod tests/app/prod.yaml --set 'app.routing.hosts[0]=todo-prod.apps.rovr.dev')"
fails "a namespace labelled for another Project is refused" "namespace must be created with the tenant labels" app todo dev tests/app/dev.yaml --set 'app.namespace.labels.opspresso\.com/delivery-project=other'
fails "an environment that does not own its namespace is refused" "namespace must be created with the tenant labels" app todo dev tests/app/dev.yaml --set app.namespace.create=false
# tests/app/override.yaml names another Project and environment; the ApplicationSet parameters replace both.
expect "a values file cannot move the app to another environment" "path: '?/agent-runway/eks-demo/todo/dev'?$" "$(app todo dev tests/app/dev.yaml -f tests/app/override.yaml)"
fails "an environment name with a hyphen is refused" "not an Agent Runway environment name" app todo dev-2 tests/app/dev.yaml
fails "an environment name in another form is refused" "not an Agent Runway environment name" app todo Prod tests/app/prod.yaml
fails "a tag instead of a digest is refused" "pin the image by digest" app todo dev tests/app/dev.yaml --set app.image.reference=nginx:latest
fails "an invalid slug is refused" "not an Agent Runway Project slug" app Todo dev tests/app/dev.yaml

# Every environment Agent Runway has: each has its own namespace, Variables and hosts.
staging=$(app todo staging tests/app/dev.yaml --set 'app.routing.hosts[0]=todo-staging.apps.rovr.dev')
expect "another environment is served on <slug>-<env>.<domain>" '"todo-staging.apps.rovr.dev"' "$staging"
expect "another environment reads its own Variables" "path: '?/agent-runway/eks-demo/todo/staging'?$" "$staging"

# Before the first deployment (no app.yaml) the environment has its tenant objects only, so resources.yaml can use app-env.
base=$(app todo dev /dev/null)
for kind in ServiceAccount SecretStore ExternalSecret NetworkPolicy ResourceQuota; do
  expect "an environment without app.yaml has its $kind" "kind: ${kind}$" "$base"
done
for kind in Deployment Service HTTPRoute; do
  refuse "an environment without app.yaml has no $kind" "kind: ${kind}$" "$base"
done

# Every deployed environment renders: its directory is an allowed environment and its app.yaml a valid workload with
# the host Agent Runway chose.
for values in projects/*/*/app.yaml; do
  environment=$(basename "$(dirname "$values")"); project=$(basename "$(dirname "$(dirname "$values")")")
  expect "$values deploys its workload" 'kind: Deployment' "$(app "$project" "$environment" "$values" 2>&1)"
done

delivery=$(helm template delivery charts/delivery --namespace app-delivery \
  --set registry=123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/projects --set pushRoleArn=arn:aws:iam::123456789012:role/app-delivery-push \
  --set 'apiServer.cidrs={172.20.0.1/32,10.10.114.40/32}')
for name in app-build app-deploy app-remove app-deploy-eb app-remove-eb resource-sync env-cleanup; do
  expect "delivery renders WorkflowTemplate $name" "^  name: ${name}$" "$delivery"
done
secret_verbs=$(yq 'select(.kind == "Role" and .metadata.name == "agent-runway") | .rules[] | select(.resources[] == "secrets") | .verbs | join(",")' <<<"$delivery")
if [ "$secret_verbs" = "create" ]; then pass "Agent Runway may only create Secrets"; else fail "Agent Runway may only create Secrets ($secret_verbs)"; fi
expect "Agent Runway may submit Workflows" 'name: agent-runway' "$delivery"
for kind in RoleBinding ClusterRoleBinding; do
  name=agent-runway
  [ "$kind" = ClusterRoleBinding ] && name=agent-runway-resources
  subjects=$(yq "select(.kind == \"$kind\" and .metadata.name == \"$name\") | .subjects | map(.namespace + \"/\" + .name) | join(\",\")" <<<"$delivery")
  if [ "$subjects" = "agent-runway/agent-runway" ]; then
    pass "$kind permits the Runway identity"
  else
    fail "$kind has unexpected submitter identities ($subjects)"
  fi
done
resource_verbs=$(yq 'select(.kind == "ClusterRole" and .metadata.name == "agent-runway-resources") | .rules[] | (.apiGroups | join(",")) + " " + (.verbs | join(","))' <<<"$delivery" | paste -sd '|' -)
if [ "$resource_verbs" = "kro.run get,list,watch|apps get" ]; then pass "Agent Runway may only read kro instances and StatefulSets"; else fail "Agent Runway may only read kro instances and StatefulSets ($resource_verbs)"; fi
cron=$(yq 'select(.kind == "CronWorkflow")' <<<"$delivery")
expect "the cleanup sweep runs every 15 minutes" '^ *- "?\*/15 \* \* \* \*"?$' "$cron"
expect "the cleanup sweep runs from its WorkflowTemplate (reference mode)" 'workflowTemplateRef:' "$cron"
expect "the cleanup sweep never overlaps itself" 'concurrencyPolicy: Forbid' "$cron"
expect "the cleanup sweep is applied after its WorkflowTemplate" 'argocd.argoproj.io/sync-wave: "1"' "$cron"
refuse "Helm leaves no template of its own" '\{\{ *\.' "$delivery"
expect "the runner executor has its token Secret" 'name: delivery-runner.service-account-token' "$delivery"
expect "runner Pods reach the cluster DNS Service IP" 'cidr: 172.20.0.10/32' "$delivery"
expect "runner Pods reach the API server on 443" 'cidr: 172.20.0.1/32' "$delivery"
fails "the API server address is required" "apiServer.cidrs is required" \
  helm template delivery charts/delivery --namespace app-delivery --set registry=x/projects --set pushRoleArn=arn:aws:iam::1:role/p
expect "Argo parameters reach the WorkflowTemplates" '\{\{workflow\.parameters\.project\}\}' "$delivery"
expect "resource-sync accepts the platform kro kinds" 'RESOURCE_KINDS: "RdsPostgres S3Bucket DynamoTable"' "$delivery"
expect "resource-sync accepts the resource charts" 'RESOURCE_CHARTS: "postgres valkey"' "$delivery"
expect "Agent Runway reads every platform kro kind" 'resources: \[rdspostgres, s3buckets, dynamotables\]' "$delivery"

contract=$(python3 scripts/workflow_contract.py)
line() { grep -E "^$1 $2( |$)" <<<"$contract" | cut -d' ' -f3-; }
expect "app-build keeps the step names Agent Runway maps" '^checkout check-application test registry build manifest result$' "$(line app-build tasks)"
expect "app-build takes Agent Runway's parameters" '^release project source revision applicationSha256 runtime platforms$' "$(line app-build parameters)"
build_yaml=$(cat charts/delivery/files/app-build.yaml)
expect "app-build builds arm64 alone by default" "value: '\\[\"linux/arm64\"\\]'" "$build_yaml"
expect "each platform builds on a node of its architecture" 'kubernetes.io/arch: "\{\{inputs.parameters.arch\}\}"' "$build_yaml"
expect "steps run one at a time on the ReadWriteOnce work volume" '^      parallelism: 1$' "$build_yaml"
refuse "no platform is emulated" 'qemu|binfmt' "$build_yaml"
expect "app-build returns revision and image" '^image revision$' "$(line app-build outputs)"
expect "app-deploy keeps the step names Agent Runway maps" '^deploy verify$' "$(line app-deploy tasks)"
expect "app-deploy takes Agent Runway's parameters" '^deployment project environment image application host$' "$(line app-deploy parameters)"
expect "app-deploy returns url" '^url$' "$(line app-deploy outputs)"
expect "app-remove keeps the step names Agent Runway maps, then cleans up" '^remove verify cleanup$' "$(line app-remove tasks)"
expect "app-remove takes Agent Runway's parameters" '^deployment project environment$' "$(line app-remove parameters)"
expect "app-remove returns nothing" '^$' "$(line app-remove outputs)"
expect "a removal waits for a deployment of the same environment" '^deploy-\{\{workflow\.parameters\.project\}\}-\{\{workflow\.parameters\.environment\}\}$' "$(line app-remove mutexes)"
expect "resource-sync writes, then cleans up an emptied environment" '^write cleanup$' "$(line resource-sync tasks)"
expect "env-cleanup scans, then checks each environment" '^scan environment$' "$(line env-cleanup tasks)"
expect "resource-sync takes Agent Runway's parameters" '^sync project environment manifest$' "$(line resource-sync parameters)"
expect "resource-sync returns nothing" '^$' "$(line resource-sync outputs)"
expect "one resource sync runs per environment" '^resources-\{\{workflow\.parameters\.project\}\}-\{\{workflow\.parameters\.environment\}\}$' "$(line resource-sync mutexes)"
expect "one deployment runs per environment" '^deploy-\{\{workflow\.parameters\.project\}\}-\{\{workflow\.parameters\.environment\}\}$' "$(line app-deploy mutexes)"
expect "builds are not serialized" '^$' "$(line app-build mutexes)"
expect "only deploy, remove, write and prune hold the deploy key, as delivery-git" '^app-deploy:deploy:delivery-git app-remove:remove:delivery-git resource-sync:write:delivery-git env-cleanup:prune:delivery-git$' "$(line all deploy-key)"
expect "a changed declaration exits 3 for Agent Runway" 'exit 3' "$(cat charts/delivery/files/app-build.yaml)"
expect "app-deploy-eb keeps app-deploy's step names" '^deploy verify$' "$(line app-deploy-eb tasks)"
expect "app-deploy-eb takes app-deploy's parameters and the Target" '^deployment project environment image application host target$' "$(line app-deploy-eb parameters)"
expect "app-deploy-eb returns url" '^url$' "$(line app-deploy-eb outputs)"
expect "app-remove-eb keeps app-remove's step names" '^remove verify$' "$(line app-remove-eb tasks)"
expect "app-remove-eb takes app-remove's parameters and the Target" '^deployment project environment target$' "$(line app-remove-eb parameters)"
for name in app-deploy-eb app-remove-eb; do
  expect "$name shares the environment mutex" '^deploy-\{\{workflow\.parameters\.project\}\}-\{\{workflow\.parameters\.environment\}\}$' "$(line $name mutexes)"
done
expect "VM Target steps act as delivery-target" '^app-deploy-eb:deploy app-deploy-eb:verify app-remove-eb:remove app-remove-eb:verify$' "$(line all delivery-target)"
expect "app-deploy-eb refuses an image of another Project" 'is not an image of \$\{REGISTRY\}/\$\{PROJECT\} pinned by digest' "$(cat charts/delivery/files/app-deploy-eb.yaml)"
if command -v cfn-lint >/dev/null; then
  if cfn-lint charts/delivery/files/eb-stack.yaml; then pass "cfn-lint accepts the aws-eb stack"; else fail "cfn-lint accepts the aws-eb stack"; fi
else
  echo "skip cfn-lint (not installed)"
fi

# target.sh: the Target must be of the step's kind, name its fields and use an Agent Runway deploy role.
target_json='{"kind":"aws-eb","name":"prod-eb","region":"ap-northeast-2","instanceSubnets":["subnet-a","subnet-b"],"deployRole":"arn:aws:iam::123456789012:role/agent-runway-target-deploy"}'
target() { (PROJECT=todo ENVIRONMENT=prod TARGET_KIND=aws-eb TARGET=$1; set -eu; . charts/delivery/files/target.sh; shift; "$@"); }
expect "target.sh joins a list field" '^subnet-a,subnet-b$' "$(target "$target_json" list instanceSubnets)"
fails "target.sh refuses another kind" "is not of kind aws-eb" target '{"kind":"aws-eks"}' true
fails "target.sh refuses a missing field" "Target field vpcId is missing" target "$target_json" require name vpcId
fails "target.sh refuses another deploy role" "is not an Agent Runway Target deploy role" \
  target "${target_json/agent-runway-target-deploy/admin}" assume_target test
# assume_target sends the Target's externalId (a Target in another AWS account) only when the Target has one.
sts_dir=$(mktemp -d)
mkdir "$sts_dir/bin"
cat > "$sts_dir/bin/aws" <<'STUB'
#!/bin/sh
printf '%s\n' "$@" > "$STS_DIR/args"
printf '%s' '{"Credentials":{"AccessKeyId":"a","SecretAccessKey":"s","SessionToken":"t"}}'
STUB
chmod +x "$sts_dir/bin/aws"
sts() { rm -f "$sts_dir/args"; PATH="$sts_dir/bin:$PATH" STS_DIR=$sts_dir "$@" >/dev/null 2>&1 && cat "$sts_dir/args"; }
external_json=${target_json/\"region\"/\"externalId\":\"9d1c-ab=+,.@:/x\",\"region\"}
expect "assume_target sends the Target's externalId" ' --external-id 9d1c-ab=\+,\.@:/x ' "$(sts target "$external_json" assume_target test | paste -sd ' ' -)"
expect "assume_target keeps the session tags with an externalId" '^Key=delivery-project,Value=todo$' "$(sts target "$external_json" assume_target test)"
refuse "assume_target sends no externalId when the Target has none" '^--external-id$' "$(sts target "$target_json" assume_target test)"
expect "assume_target assumes the deploy role without an externalId" '^arn:aws:iam::123456789012:role/agent-runway-target-deploy$' "$(sts target "$target_json" assume_target test)"
fails "assume_target refuses an externalId with other characters" "is not an external ID" \
  target "${target_json/\"region\"/\"externalId\":\"a b\",\"region\"}" assume_target test
fails "assume_target refuses a one-character externalId" "is not an external ID" \
  target "${target_json/\"region\"/\"externalId\":\"a\",\"region\"}" assume_target test
fails "assume_target refuses an externalId that is not a string" "is not an external ID" \
  target "${target_json/\"region\"/\"externalId\":12345,\"region\"}" assume_target test
rm -rf "$sts_dir"

# The instance type comes only from application.environment.instanceType (Agent Runway resolves the Project's size).
sized() { (APPLICATION=$1; target "$target_json" instance_type); }
expect "instance_type reads application.environment.instanceType" '^t4g\.small$' "$(sized '{"environment":{"instanceType":"t4g.small"}}')"
fails "instance_type refuses an application without an instance type" "environment.instanceType is missing" sized '{"environment":{}}'
fails "instance_type refuses a value that is not an instance type" "is not an instance type" sized '{"environment":{"instanceType":"t4g.small;id"}}'
rolled() { (APPLICATION=$1; target "$target_json" rollout); }
expect "rollout replaces instances only when the new ones are healthy by default" '^Immutable$' "$(rolled '{"environment":{}}')"
expect "rollout reads application.environment.rollout" '^AllAtOnce$' "$(rolled '{"environment":{"rollout":"all-at-once"}}')"
expect "rollout takes the explicit default" '^Immutable$' "$(rolled '{"environment":{"rollout":"immutable"}}')"
fails "rollout refuses another value" "must be immutable or all-at-once" rolled '{"environment":{"rollout":"AllAtOnce; id"}}'

# install_script writes the Target's installScript; no field gives an empty file.
script_dir=$(mktemp -d)
installed() { target "$1" install_script "$script_dir/install.sh" && cat "$script_dir/install.sh"; }
with_script() { jq -c --arg script "$1" '. + {installScript: $script}' <<<"$target_json"; }
if installed "$target_json" >/dev/null && [ ! -s "$script_dir/install.sh" ]; then
  pass "install_script writes nothing without installScript"; else fail "install_script writes nothing without installScript"; fi
expect "install_script writes the script unchanged" '^touch /tmp/x$' "$(installed "$(with_script "$(printf '#!/bin/bash\ntouch /tmp/x\n')")")"
expect "install_script accepts #!/bin/sh" '^#!/bin/sh$' "$(installed "$(with_script "$(printf '#!/bin/sh\ntrue\n')")")"
fails "install_script refuses another interpreter" "must start with #!/bin/bash or #!/bin/sh" installed "$(with_script "$(printf '#!/usr/bin/python3\npass\n')")"
fails "install_script refuses a script over 16 KiB" "exceeds 16 KiB" installed "$(with_script "#!/bin/bash
$(printf '#%.0s' $(seq 16374))")"
fails "install_script refuses a value that is not a string" "must be a string" installed "${target_json/\"region\"/\"installScript\":[1],\"region\"}"
rm -rf "$script_dir"

# eb-bundle.py: the bundle pins the image, keeps `$` literal and holds no secret value.
bundle_dir=$(mktemp -d)
printf '%s' '{"runtime":{"port":8080,"args":["--x=$HOME"]},"environment":{"env":{"LOG_LEVEL":"debug"}},"config":{"GREETING":"a $b"}}' > "$bundle_dir/app.json"
eb_image="123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/projects/todo@sha256:$(printf 'a%.0s' $(seq 64))"
python3 -I charts/delivery/files/eb-bundle.py "$bundle_dir/app.json" "$eb_image" /agent-runway/prod-eb/todo/prod ap-northeast-2 "$bundle_dir/b.zip"
compose=$(unzip -p "$bundle_dir/b.zip" docker-compose.yml)
expect "the bundle runs the Release image by digest" "image: \"${eb_image}\"" "$compose"
expect "the bundle maps port 80 to runtime.port" 'ports: \["80:8080"\]' "$compose"
expect "the bundle keeps \$ literal for Compose" 'GREETING: "a \$\$b"' "$compose"
expect "the hook reads only the environment's SSM path" '--path /agent-runway/prod-eb/todo/prod ' "$(unzip -p "$bundle_dir/b.zip" .platform/hooks/prebuild/01-runway.sh)"
expect "the hook is executable" '-rwx' "$(unzip -Z "$bundle_dir/b.zip" .platform/hooks/prebuild/01-runway.sh)"
fails "the bundle refuses an image outside ECR" "pinned by digest" \
  python3 -I charts/delivery/files/eb-bundle.py "$bundle_dir/app.json" docker.io/library/nginx:latest /agent-runway/prod-eb/todo/prod ap-northeast-2 "$bundle_dir/c.zip"
refuse "a bundle without an install script has no install hook" '00-install\.sh' "$(unzip -Z1 "$bundle_dir/b.zip")"
: > "$bundle_dir/empty.sh"
python3 -I charts/delivery/files/eb-bundle.py "$bundle_dir/app.json" "$eb_image" /agent-runway/prod-eb/todo/prod ap-northeast-2 "$bundle_dir/e.zip" "$bundle_dir/empty.sh"
refuse "an empty install script adds no install hook" '00-install\.sh' "$(unzip -Z1 "$bundle_dir/e.zip")"
printf '#!/bin/bash\ndnf -y install htop\n' > "$bundle_dir/install.sh"
python3 -I charts/delivery/files/eb-bundle.py "$bundle_dir/app.json" "$eb_image" /agent-runway/prod-eb/todo/prod ap-northeast-2 "$bundle_dir/i.zip" "$bundle_dir/install.sh"
for kind in hooks confighooks; do
  if unzip -p "$bundle_dir/i.zip" ".platform/${kind}/prebuild/00-install.sh" | cmp -s - "$bundle_dir/install.sh"; then
    pass "the ${kind} install hook is the Target's script unchanged"; else fail "the ${kind} install hook is the Target's script unchanged"; fi
  expect "the ${kind} install hook is executable" '^-rwxr-xr-x' "$(unzip -Z "$bundle_dir/i.zip" ".platform/${kind}/prebuild/00-install.sh")"
done
expect "the install hook runs before the Variable hook" '^\.platform/hooks/prebuild/00-install\.sh \.platform/hooks/prebuild/01-runway\.sh$' \
  "$(unzip -Z1 "$bundle_dir/i.zip" | grep '^\.platform/hooks/' | LC_ALL=C sort | paste -sd ' ' -)"
rm -rf "$bundle_dir"

expect "deploy refuses an image of another Project" 'is not an image of \$\{REGISTRY\}/\$\{PROJECT\} pinned by digest' "$(cat charts/delivery/files/app-deploy.yaml)"

# resource-sync writes only allowed kro instances of the environment's namespace.
kinds="RdsPostgres S3Bucket DynamoTable"
declared=$(mktemp -d)
check() {
  rm -rf "$declared"
  sh charts/delivery/files/check-resources.sh app-todo-dev "$kinds" "postgres valkey" "$declared" "# header" || return
  cat "$declared/resources.yaml"
}
rds='apiVersion: kro.run/v1alpha1
kind: RdsPostgres
metadata:
  name: main
spec:
  passwordSecret: {name: app-env, key: MAIN_PGPASSWORD}'
expect "an RdsPostgres instance is written" '^kind: RdsPostgres$' "$(check <<<"$rds" 2>&1)"
table='apiVersion: kro.run/v1alpha1
kind: DynamoTable
metadata:
  name: events
spec:
  partitionKey: pk'
bucket='apiVersion: kro.run/v1alpha1
kind: S3Bucket
metadata:
  name: files
spec:
  versioning: false'
all=$(check <<<"$(printf '%s\n---\n%s\n---\n%s' "$rds" "$table" "$bucket")" 2>&1)
expect "a DynamoTable instance is written" '^kind: DynamoTable$' "$all"
expect "an S3Bucket instance is written" '^kind: S3Bucket$' "$all"
fails "two kinds with one name are refused" "uniquely named" check <<<"$(printf '%s\n---\n%s' "$rds" "${table/name: events/name: main}")"
release='apiVersion: runway.opspresso.com/v1alpha1
kind: ChartRelease
metadata:
  name: db
  labels: {opspresso.com/resource: 0123abcd-0123-4123-8123-0123456789ab}
spec:
  chart: postgres
  version: 1.0.0
  values:
    passwordSecret: {key: DB_PGPASSWORD}
    persistence: {storageGiB: 5}'
check <<<"$(printf '%s\n---\n%s' "$rds" "$release")" >/dev/null
file=$(cat "$declared/resources/db.yaml")
expect "a ChartRelease is written as its values file" '^chart: postgres$' "$file"
expect "the values file pins the chart version" '^chartVersion: 1.0.0$' "$file"
expect "the values file names the Application" '^resourceId: 0123abcd0123412381230123456789ab$' "$file"
expect "the values file keeps the chart values" 'storageGiB: 5' "$file"
if grep -q ChartRelease "$declared/resources.yaml"; then fail "resources.yaml holds no ChartRelease"; else pass "resources.yaml holds no ChartRelease"; fi
fails "a repeated resource ID is refused" "ChartReleases of charts" check <<<"$(printf '%s\n---\n%s' "$release" "${release/name: db/name: db2}")"
fails "a chart outside the list is refused" "ChartReleases of charts" check <<<"${release/chart: postgres/chart: app}"
fails "the name app is refused" "ChartReleases of charts" check <<<"${release/name: db/name: app}"
fails "an image override is refused" "ChartReleases of charts" check <<<"${release/storageGiB: 5\}/storageGiB: 5\}
    image: \{repository: evil\}}"
fails "a chart version without a fixed number is refused" "ChartReleases of charts" check <<<"${release/version: 1.0.0/version: latest}"
expect "a push that loses the GitHub ref lock is retried" 'cannot lock ref' "$(grep -E 'grep -qE' charts/delivery/files/commit.sh)"
# commit.sh's commit(): an empty SOURCE removes TARGET. An emptied environment directory stays with an empty
# resources.yaml (commit 1); PRUNE_EMPTY removes it only while it is still empty (commit 2).
gitops=$(mktemp -d)
commit_fn=$(sed -n '/^empty_environment() {/,/^}/p;/^commit() {/,/^}/p' charts/delivery/files/commit.sh)
gitops_commit() { (cd "$gitops" && SOURCE=$1 TARGET=$2 PRUNE_EMPTY=${3:-} SUBJECT=test BODY=test bash -c "$commit_fn"$'\n'"commit" >/dev/null 2>&1); }
gitops_prune() { gitops_commit "" "$1" 1; }
tracked() { git -C "$gitops" ls-files "$1"; }
clean() { [ -z "$(git -C "$gitops" status --porcelain)" ]; }
git -C "$gitops" init -q && git -C "$gitops" config user.email t@t && git -C "$gitops" config user.name t
mkdir -p "$gitops/projects/todo/dev/resources" && echo app > "$gitops/projects/todo/dev/app.yaml"
echo r > "$gitops/projects/todo/dev/resources/main.yaml" && echo h > "$gitops/projects/todo/dev/resources.yaml"
mkdir -p "$gitops/projects/solo/dev" && echo app > "$gitops/projects/solo/dev/app.yaml"
git -C "$gitops" add -A && git -C "$gitops" commit -qm init
gitops_commit "" projects/todo/dev/app.yaml
if [ ! -e "$gitops/projects/todo/dev/app.yaml" ] && [ -f "$gitops/projects/todo/dev/resources/main.yaml" ]; then pass "a removal keeps the resources"; else fail "a removal keeps the resources"; fi
if ! gitops_prune projects/todo/dev && [ -n "$(tracked projects/todo/dev/resources/main.yaml)" ]; then pass "cleanup keeps a directory that still has a resource"; else fail "cleanup keeps a directory that still has a resource"; fi
empty=$(mktemp -d) && mkdir "$empty/resources" && echo '# h' > "$empty/resources.yaml"
gitops_commit "$empty" projects/todo/dev
if [ -n "$(tracked projects/todo/dev/resources.yaml)" ] && [ -z "$(tracked projects/todo/dev/resources)" ] && clean; then
  pass "removing the last resource keeps the directory with an empty declaration"; else fail "removing the last resource keeps the directory with an empty declaration"; fi
gitops_commit "" projects/solo/dev/app.yaml
if [ -n "$(tracked projects/solo/dev/resources.yaml)" ] && [ -z "$(tracked projects/solo/dev/app.yaml)" ] && clean; then
  pass "removing the only app.yaml keeps the directory with an empty declaration"; else fail "removing the only app.yaml keeps the directory with an empty declaration"; fi
if gitops_commit "" projects/todo/dev/app.yaml; then fail "a repeated removal reports no change"; else pass "a repeated removal reports no change"; fi
# A deployment that raced in before cleanup: the directory stays.
echo app > "$gitops/projects/solo/dev/app.yaml" && git -C "$gitops" add -A && git -C "$gitops" commit -qm redeploy
if ! gitops_prune projects/solo/dev && [ -n "$(tracked projects/solo/dev/app.yaml)" ]; then pass "cleanup keeps a directory with app.yaml"; else fail "cleanup keeps a directory with app.yaml"; fi
printf '# h\nkind: RdsPostgres\n' > "$gitops/projects/solo/dev/resources.yaml" && rm "$gitops/projects/solo/dev/app.yaml"
git -C "$gitops" add -A && git -C "$gitops" commit -qm instance
if ! gitops_prune projects/solo/dev && [ -n "$(tracked projects/solo/dev/resources.yaml)" ]; then pass "cleanup keeps a directory whose resources.yaml declares an instance"; else fail "cleanup keeps a directory whose resources.yaml declares an instance"; fi
if gitops_prune projects/todo/dev && [ ! -e "$gitops/projects/todo/dev" ] && clean; then pass "cleanup removes an empty environment directory and commits"; else fail "cleanup removes an empty environment directory and commits"; fi
if gitops_prune projects/todo/dev; then fail "a repeated cleanup reports no change"; else pass "a repeated cleanup reports no change"; fi
rm -rf "$gitops" "$empty"
# live: lingering objects (also while deleting) keep the directory without failing; an API error fails the step.
template_script() { python3 -c 'import sys, yaml; t = {x["name"]: x for x in yaml.safe_load(open("charts/delivery/files/env-cleanup.yaml"))["spec"]["templates"]}; print(t[sys.argv[1]]["container"]["args"][0])' "$1"; }
live_script=$(template_script live)
stub=$(mktemp -d)
live() { # live <empty> <kubectl output> <kubectl exit>: prints the live result, or "error"
  printf '#!/bin/sh\nprintf "%%s" "%s"\nexit %s\n' "$2" "$3" > "$stub/kubectl" && chmod +x "$stub/kubectl"
  rm -f "$stub/live"
  if PATH="$stub:$PATH" PROJECT=todo ENVIRONMENT=dev EMPTY=$1 RESOURCE_KINDS="RdsPostgres S3Bucket DynamoTable" \
    sh -euc "${live_script//\/tmp\/live/$stub/live}" >/dev/null 2>&1; then cat "$stub/live"; else echo error; fi
}
expect "live: a deleting S3Bucket keeps the directory and succeeds" '^true$' "$(live true 's3bucket.kro.run/assets' 0)"
expect "live: a chart PVC keeps the directory" '^true$' "$(live true 'persistentvolumeclaim/data-cache-0' 0)"
expect "live: an empty namespace lets prune run" '^false$' "$(live true '' 0)"
expect "live: an API error fails the step" '^error$' "$(live true '' 1)"
expect "live: a sync that still declares resources skips prune" '^true$' "$(live false '' 0)"
rm -rf "$stub"
# scan: only directories that declare nothing are candidates.
scan_repo=$(mktemp -d)
mkdir -p "$scan_repo/projects/live/dev/resources" "$scan_repo/projects/gone/prod" "$scan_repo/projects/app/dev" "$scan_repo/projects/rds/dev"
echo r > "$scan_repo/projects/live/dev/resources/cache.yaml" && echo '# h' > "$scan_repo/projects/live/dev/resources.yaml"
printf '# h\n---\n' > "$scan_repo/projects/gone/prod/resources.yaml"
echo app > "$scan_repo/projects/app/dev/app.yaml"
printf '# h\nkind: RdsPostgres\n' > "$scan_repo/projects/rds/dev/resources.yaml"
scan_script=$(template_script scan)
scan_script=$(sed "1s|^git clone .*|cp -R '$scan_repo' '$scan_repo.work'|" <<<"$scan_script")
scan_script=${scan_script//\/tmp\/gitops/$scan_repo.work}
scan_script=${scan_script//\/tmp\/environments.json/$scan_repo.json}
GITOPS_BRANCH=main GITOPS_REPOSITORY=x sh -euc "$scan_script" >/dev/null
expect "scan lists only an environment that declares nothing" '^\[\{"project":"gone","environment":"prod"\}\]$' "$(cat "$scan_repo.json")"
rm -rf "$scan_repo" "$scan_repo.work" "$scan_repo.json"
fails "an empty manifest is refused" "manifest is empty" check </dev/null
fails "a manifest of empty documents is refused" "no document" check <<<"$(printf -- '---\n# none\n')"
# Agent Runway's zero-resource manifest, decoded the way the write step decodes the parameter.
EMPTY_LIST='{"apiVersion":"v1","kind":"List","items":[]}'
if out=$(printf '%s' "$EMPTY_LIST" | base64 | tr -d '\n' | base64 -d | check 2>&1) && [ "$out" = "# header" ] && [ -z "$(ls "$declared/resources")" ]; then
  pass "Agent Runway's empty List removes every resource"; else fail "Agent Runway's empty List removes every resource ($out)"; fi
fails "a List with items is refused" "only be the single document" check <<<'{"apiVersion":"v1","kind":"List","items":[{"kind":"Secret"}]}'
fails "a List with other fields is refused" "only be the single document" check <<<'{"apiVersion":"v1","kind":"List","items":[],"metadata":{}}'
fails "a List of another apiVersion is refused" "only be the single document" check <<<'{"apiVersion":"v2","kind":"List","items":[]}'
fails "an empty List next to a resource is refused" "only be the single document" check <<<"$(printf '%s\n---\n%s' "$EMPTY_LIST" "$rds")"
fails "an empty JSON array is refused" "uniquely named" check <<<'[]'
long=$(printf 'a%.0s' $(seq 50))
expect "a 50-character name is written" "name: ${long}$" "$(check <<<"${rds/name: main/name: ${long}}" 2>&1)"
fails "a 51-character name is refused" "uniquely named" check <<<"${rds/name: main/name: ${long}b}"
fails "a name that ends with a hyphen is refused" "uniquely named" check <<<"${rds/name: main/name: main-}"
fails "another kind is refused" "kinds \[RdsPostgres S3Bucket DynamoTable\]" check <<<"$(printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: x\n')"
fails "another API group is refused" "kinds \[RdsPostgres S3Bucket DynamoTable\]" check <<<"${rds/kro.run/rds.services.k8s.aws}"
fails "another namespace is refused" "namespace app-todo-dev" check <<<"${rds/name: main/name: main
  namespace: app-other-dev}"
fails "a duplicate name is refused" "uniquely named" check <<<"$(printf '%s\n---\n%s' "$rds" "$rds")"
fails "a manifest that is not YAML is refused" "" check <<<"kind: [unclosed"

if command -v argo >/dev/null; then
  # One run: resource-sync and app-remove reference env-cleanup templates.
  if argo lint --offline charts/delivery/files/{app-build,app-deploy,app-remove,app-deploy-eb,app-remove-eb,resource-sync,env-cleanup}.yaml >/dev/null; then
    pass "argo lint accepts the WorkflowTemplates"; else fail "argo lint accepts the WorkflowTemplates"; fi
else
  echo "skip argo lint (argo CLI not installed)"
fi

if [ "$failures" -gt 0 ]; then echo "$failures check(s) failed"; exit 1; fi
echo "all checks passed"
