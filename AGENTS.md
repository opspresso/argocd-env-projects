# AGENTS.md

Working rules for coding agents in this repository (`CLAUDE.md` is a symlink to this file).

This repository is the GitOps home of the apps that Agent Broker delivers. The contract is
`agent-platform/docs/APP_DELIVERY.md`; [README.md](README.md) explains the layout and the Delivery steps.

## Ownership

- `projects/<slug>/<env>.yaml` is written only by the `deploy` step of the app-delivery workflow. Never edit,
  add or rename these files by hand; change the app's `application.yaml` and start a new Delivery instead.
- `charts/app` and `charts/delivery` change through reviewed pull requests.
- The Argo CD AppProjects, the `app-delivery` Application and the `projects` ApplicationSet live in
  `argocd-env-addons` (`charts/argo-cd`). Change them there.

## Invariants

- The ApplicationSet sets `project`, `environment`, `domain`, `cluster`, `region` and `tenantSecretsRoleArn` as Helm
  parameters from the file path and the cluster. The app chart must derive the namespace and the host from them only,
  so a values file can never move an app or claim another host.
- The app chart renders only the kinds that AppProject `projects` allows. Adding a kind needs a change there too.
- `charts/delivery/files/workflow-template.yaml` is static. Argo's `{{...}}` parameters must not pass through Helm;
  cluster values reach the steps through ConfigMap `delivery-settings`.
- Keep the DAG task names (`checkout`, `check-application`, `test`, `registry`, `build`, `deploy`, `verify`) and the
  workflow parameters stable: Agent Broker submits the template and maps failed tasks to Delivery steps.
- Steps that run app code (`test`, `build`) get no Git credential, AWS identity or Kubernetes permission.

## Validation

Run `./scripts/test.sh` after every chart change (Helm 4, Python with `pyyaml`). Review the rendered diff of both charts.
Write docs in Korean and this file in English, following ISO 24495-1 and ASD-STE100.
