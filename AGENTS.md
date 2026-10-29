# AGENTS.md

Working rules for coding agents in this repository (`CLAUDE.md` is a symlink to this file).

This repository is the GitOps home of the apps that Agent Delivery deploys. The contract is
`agent-platform/docs/APP_DELIVERY.md` and `docs/DELIVERY_V2.md`; [README.md](README.md) explains the layout and the Delivery steps.

## Ownership

- `projects/<slug>/<env>/app.yaml` is written only by the `deploy` step of the `app-deploy` workflow, and
  `projects/<slug>/<env>/resources.yaml` only by the `write` step of the `resource-sync` workflow. Never edit, add or
  rename these files by hand; change the app's `application.yaml` or its resources in Agent Delivery instead.
- `charts/app` and `charts/delivery` change through reviewed pull requests.
- The Argo CD AppProjects, the `app-delivery` Application, the `projects` ApplicationSet and the allowed environment
  list (`app_delivery.environments`, `app_delivery.primary_environment`) live in
  `argocd-env-addons` (`charts/argo-cd`). Change them there.

## Invariants

- `charts/app` wraps the opspresso `app` chart (helm-charts), like argocd-env-apps sample-node and the agent-* services.
  Keep it free of its own templates. Declare tenant objects with `app` chart settings (`externalSecrets`,
  `networkPolicy`, `resourceQuota`). Only the guard, which has no `app` chart setting, lives in `app.raw.parentTemplates`.
- The ApplicationSet creates one Application per `projects/<slug>/<env>` directory of an allowed environment. It sets
  `app.tenant.*` (project, environment, environments, primaryEnvironment, domain, cluster, region,
  tenantSecretsRoleArn) and `app.routing.hosts` as Helm parameters from the directory path and the cluster. The guard
  template refuses an environment outside the list, any other namespace or host, and a workload image without a
  digest, so a values file can never move an app, add an environment or claim another host.
- `app.tenant.environments` is a space-separated string, not a list: a values file could add items to a list
  parameter.
- Without `app.yaml` the chart renders only the tenant objects (no Deployment, Service or HTTPRoute), so
  `resources.yaml` can use Secret `app-env` before the first deployment. `render.jq` turns the workload on.
- `charts/delivery/files/render.jq` is the only mapping from Agent Delivery's declaration to `app` chart values; the deploy step
  and `scripts/test.sh` both use it.
- The app chart renders only the kinds that AppProject `projects` allows. Adding a kind needs a change there too.
- `charts/delivery/files/{app-build,app-deploy,resource-sync}.yaml` are static. Argo's `{{...}}` parameters must not
  pass through Helm; cluster values reach the steps through ConfigMap `delivery-settings`, and the shared scripts
  (`render.jq`, `check-resources.sh`, `commit.sh`) through ConfigMap `delivery-scripts`.
- Keep the WorkflowTemplate names, parameters, results and DAG task names of the Workflow contract in
  `docs/DELIVERY_V2.md` (`app-build`: `checkout`, `check-application`, `test`, `registry`, `build`; `app-deploy`:
  `deploy`, `verify`; `resource-sync`: `write`). Agent Delivery submits them and maps failed tasks to its steps.
- `resource-sync` writes only kro instances of the kinds in `resourceKinds` (charts/delivery values) in the
  environment's namespace. AppProject `projects` must allow the same kinds.
- Steps that run app code (`test`, `build`) get no Git credential, AWS identity or Kubernetes permission.

## Validation

Run `./scripts/test.sh` after every chart change (Helm 4, `jq`, `yq`, Python with `pyyaml`). Review the rendered diff of both charts.
Write docs in Korean and this file in English, following ISO 24495-1 and ASD-STE100.
