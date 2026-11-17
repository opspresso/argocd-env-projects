# AGENTS.md

Working rules for coding agents in this repository (`CLAUDE.md` is a symlink to this file).

This repository is the GitOps home of the apps that Agent Runway deploys. The contract is
`agent-platform/docs/APP_DELIVERY.md` and `agent-platform/docs/DELIVERY_RESOURCES.md`; [README.md](README.md) explains the layout and the Delivery steps.

## Ownership

- `projects/<slug>/<env>/app.yaml` is written only by the `deploy` step of the `app-deploy` workflow and removed only by
  the `remove` step of the `app-remove` workflow, and
  `projects/<slug>/<env>/resources.yaml` and `projects/<slug>/<env>/resources/<name>.yaml` only by the `write` step of the `resource-sync` workflow. Never edit, add or
  rename these files by hand; change the app's `application.yaml` or its resources in Agent Runway instead.
- `charts/app` and `charts/delivery` change through reviewed pull requests.
- The Argo CD AppProjects, the `app-delivery` Application and the `projects` ApplicationSet live in `argocd-env-apps`
  (`charts/agent-runway`, `env/`). Change them there. No repository lists the environments: Agent Runway owns them,
  and every `projects/<slug>/<env>` directory it writes is one.

## Invariants

- `charts/app` wraps the opspresso `app` chart (helm-charts), like argocd-env-apps sample-node and the agent-* services.
  Keep it free of its own templates. Declare tenant objects with `app` chart settings (`namespace`, `externalSecrets`,
  `networkPolicy`, `resourceQuota`). The environment namespace is one of them, so Argo CD deletes it with the
  Application; the ApplicationSet must not use `CreateNamespace`, whose namespaces Argo CD never deletes. The guard lives in `app.raw.parentTemplates`. The fixed renderer adds a separate Nexus pull ExternalSecret through `app.raw.resources`: the app chart has only one envFrom ExternalSecret and no dockerconfigjson target template.
- The ApplicationSet creates one Application per `projects/<slug>/<env>` directory. It sets
  `app.tenant.*` (project, environment, domain, cluster, region, tenantSecretsRoleArn) and the project
  and environment labels of `app.namespace` as Helm parameters from the directory path and the cluster. Agent Runway
  chooses the host of a Project environment; the `deploy` step checks its form and writes it to `app.routing.hosts`
  in `app.yaml`. The guard template refuses an environment name that is not an Agent Runway environment name (a lowercase letter, then 1 to 11 lowercase letters or digits; no hyphen, so `app-<project>-<environment>` names one Project and one environment), any other namespace, a host that is not
  `<project>.<domain>` or `<project>-<environment>.<domain>`, a workload without a host, a namespace without its tenant
  labels, and a workload image without a digest. The guard is defense in depth: a values file could set `app.raw` and turn it off. The boundary is that only
  the `deploy` step writes `app.yaml`, through the fixed `render.jq` mapping, with a deploy key that only the
  `delivery-git` steps (`deploy`, `remove`, `write`, `prune`) hold. Keep both, and never let other content reach the values files.
- `commit.sh` removes an environment directory in two commits. A content commit never removes the directory: when no
  `app.yaml` and no resource remain, it keeps an empty `resources.yaml`, so Argo CD prunes under a live Application.
  `charts/delivery/files/env-cleanup.yaml` then removes the directory (`commit.sh` `PRUNE_EMPTY=1`) only if it is
  still empty and its `live` step finds no resource object in the namespace; the ApplicationSet deletes the empty
  Application. The `cleanup` task of `app-remove` and `resource-sync` calls it right away, and CronWorkflow
  `env-cleanup` sweeps every 15 minutes under the `deploy-<slug>-<env>` mutex, because Agent Runway sends no further
  Workflow after the last removal. Lingering objects are a normal state: the Workflows succeed and the directory
  stays. Workflow results follow `remove`/`verify` and `write`. A Project leaves this repository
  when all its services and resources are removed.
- Without `app.yaml` the chart renders only the tenant objects (no Deployment, Service or HTTPRoute), so
  `resources.yaml` can use Secret `app-env` before the first deployment. `render.jq` turns the workload on.
- `charts/delivery/files/render.jq` is the only mapping from Agent Runway's declaration to `app` chart values; the deploy step
  and `scripts/test.sh` both use it.
- `render.jq` maps `environment.architecture` (`amd64` or `arm64`, the Release image's architecture) to `nodeSelector`
  `kubernetes.io/arch`; `amd64` also tolerates the amd64 NodePool taint `opspresso.com/arch=amd64:NoSchedule`. Another value
  fails the deploy step. Without the value the Pods have no scheduling constraint.
- The app chart renders only the kinds that AppProject `projects` allows. Adding a kind needs a change there too.
- `charts/delivery/files/{app-build-v2,app-deploy,app-remove,app-deploy-eb,app-remove-eb,app-deploy-vmss,app-remove-vmss,resource-sync,env-cleanup}.yaml` are static. Argo's `{{...}}` parameters must not
  pass through Helm; cluster values reach the steps through ConfigMap `delivery-settings`, and the shared scripts
  (`render.jq`, `check-resources.sh`, `commit.sh`) through ConfigMap `delivery-scripts`.
- Keep the WorkflowTemplate names, parameters, results and DAG task names of the Workflow contract in
  agent-platform `docs/APP_DELIVERY.md#workflow` (`app-build-v2`: `checkout`, `check-application`, `test`, `build`, `build-artifact`, `publish-image`, `publish-artifact`; `app-deploy`:
  `deploy`, `verify`; `app-remove`: `remove`, `verify`; `resource-sync`: `write`). Agent Runway submits them and maps failed tasks to its steps.
- `resource-sync` holds the mutex `resources-<project>-<environment>`; `app-deploy` and `app-remove` share
  `deploy-<project>-<environment>`. Argo Workflows v4 accepts only `synchronization.mutexes`.
- `resource-sync` writes only kro instances of the kinds in `resourceKinds` and ChartReleases of the charts in
  `resourceCharts` (charts/delivery values) in the environment's namespace. A ChartRelease may set only
  `passwordSecret`, `persistence`, `maxMemoryMiB` and `peer`. AppProject `projects` must allow the same kinds and the
  kinds those charts render.
- Steps that run app code (`test`, `build`, `build-artifact`) get no Git credential, AWS identity, Kubernetes token or Nexus credential. Only publisher steps receive the publish credential and mount source read-only.
- `app-deploy-eb` and `app-remove-eb` deploy to and remove from an `aws-eb` Target (agent-platform `docs/APP_DELIVERY.md#target`).
  They keep the parameters, results and DAG task names of `app-deploy` and `app-remove` and add `target`. Only their
  steps run as `delivery-target`, which may assume only the Target deploy role `agent-runway-target-deploy`
  (`target.sh` checks it). The stack (`eb-stack.yaml`) and the source bundle (`eb-bundle.py`) take every value from
  Agent Runway and the Target. Secret values never enter the bundle, the stack or the Workflow: Elastic Beanstalk resolves their SSM ARN references through the instance role when the app starts.
  The instances get public addresses only when the Target puts them in its load balancer subnets (`target.sh` `public_ip`):
  those subnets are public and have no NAT, as in the default VPC. Instances in other (private) subnets get none.
- `app-deploy-vmss` and `app-remove-vmss` deploy to and remove from an `azure-vmss` Target with the same parameters,
  results and DAG task names as the eb pair. Their steps run as `delivery-target` and sign in to Entra as
  `target.deployClientId` with a projected service-account token of audience `api://AzureADTokenExchange`
  (`target.sh` `azure_login`); `scripts/workflow_contract.py` checks the token volume. `vmss-deploy.py` and `vmss-app.json`
  take every value from Agent Runway and the Target. The steps read only the names and metadata of Key Vault secrets
  (`ar-<sha256 of the Variable path>`); the instances read the values with their own user-assigned identity, which gets
  `Key Vault Secrets User` per declared secret and `Storage Blob Data Reader` on its bundle container only. Secret
  values never enter the bundle, the template parameters, the custom data or the Workflow. The instances accept port 80
  only from this Target's Front Door profile (`X-Azure-FDID`) and the load balancer probe. Front Door and DNS changes go
  through direct ARM requests, because the deploy identity holds no deployment permission in those resource groups.
- The VM Target steps read the instance type and counts only from `application.environment` (`instanceType`,
  `replicas`, `maxReplicas`); Agent Runway resolves the Project's size into them (agent-platform
  `docs/APP_DELIVERY.md#크기`). The optional `application.environment.rollout` chooses the Elastic Beanstalk deployment policy: `immutable`
  (the default) or `all-at-once`, which Agent Runway sends only for a sandbox environment. The optional `target.externalId` (`docs/APP_DELIVERY.md#외부-계정`) goes to every `aws sts assume-role` of `target.sh`. The optional
  `target.installScript` (`docs/APP_DELIVERY.md#설치-스크립트`) runs on each instance before the app, as the prebuild hook `00-install.sh` of the EB
  bundle. A non-zero exit fails the Deployment.

## Validation

Run `./scripts/test.sh` after every chart change (Helm 4, `jq`, `yq`, Python with `pyyaml`). Review the rendered diff of both charts.
Write docs in Korean and this file in English, following ISO 24495-1 and ASD-STE100.

Native EB deploys JAR/WAR from Nexus and verifies its recorded SHA-256 and size. Its bundle contains no Docker runtime or registry credential. The selected solution stack must match the native format and Java/Tomcat version.

EB instance roles never use AWSElasticBeanstalkWebTier. New environments run only the fixed bootstrap bundle until
a separate stack update scopes S3 access to their EB environment ID and logs to their environment name.
The workflow verifies that scope before uploading or executing user code. Existing environments keep their current
version during the scope-only update. Never treat a failed stack lookup as absence.
