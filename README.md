# argocd-env-projects

Agent Delivery가 배포하는 앱의 GitOps 저장소입니다. Studio Agent가 Agent Delivery MCP로 시작한 Delivery가 이 저장소에
배포 값을 기록하고, Argo CD가 그 값을 EKS `eks-demo`에 동기화합니다.
전체 계약은 [agent-platform `docs/APP_DELIVERY.md`](https://github.com/opspresso/agent-platform/blob/main/docs/APP_DELIVERY.md)에 있습니다.

## 구성

| 경로 | 내용 | 쓰는 주체 |
| --- | --- | --- |
| `charts/app/` | 앱 Environment 하나의 wrapper chart. [opspresso/helm-charts](https://github.com/opspresso/helm-charts)의 `app` chart(argocd-env-apps의 sample-node·agent-*와 같은 chart)의 설정으로 Deployment·Service·HTTPRoute·HPA·PDB·SecretStore·ExternalSecret·NetworkPolicy·ResourceQuota를 만든다. `app.raw`에는 namespace·host·image digest를 검사하는 guard만 둔다 | 운영자(PR) |
| `charts/delivery/` | namespace `app-delivery`의 Argo Workflows `app-delivery` WorkflowTemplate, 단계별 ServiceAccount·RBAC, 설정 | 운영자(PR) |
| `projects/<slug>/<env>.yaml` | Environment 하나의 `app` chart 값: 이미지 digest, 복제 수, port, env, probe, 자원. deploy 단계가 `charts/delivery/files/render.jq`로 Agent Delivery의 선언에서 만든다 | Delivery workflow만 |
| `scripts/test.sh` | 두 chart를 Argo CD와 같은 parameter로 렌더링하고 계약을 검사한다 | |

Argo CD 객체는 [argocd-env-addons](https://github.com/opspresso/argocd-env-addons)의 `charts/argo-cd`가 선언합니다.

- AppProject `projects`: `app-*` namespace(단, `app-delivery` 제외)에 `charts/app`이 만드는 종류만 허용합니다.
- AppProject `delivery`: `app-delivery` namespace와 `delivery-verify` ClusterRole만 허용합니다.
- Application `app-delivery`: `charts/delivery`를 동기화합니다.
- ApplicationSet `projects`: `projects/*/dev.yaml`·`projects/*/prod.yaml`마다 Application `app-<slug>-<env>`를 만듭니다.
  Project·Environment·namespace·도메인은 파일 경로와 클러스터 설정에서 정합니다. 파일 내용으로 바꿀 수 없습니다.

## Delivery 흐름

```mermaid
flowchart LR
  agent["Studio Agent"] -- "delivery_start" --> delivery["Agent Delivery"]
  delivery -- "Workflow 제출" --> wf["Argo Workflows<br/>app-delivery"]
  wf -- "checkout·test·build" --> ecr["ECR projects/&lt;slug&gt;"]
  wf -- "deploy: commit" --> repo["argocd-env-projects<br/>projects/&lt;slug&gt;/&lt;env&gt;.yaml"]
  repo --> argocd["Argo CD<br/>ApplicationSet projects"]
  argocd --> app["app-&lt;slug&gt;-&lt;env&gt;<br/>Deployment·HTTPRoute"]
  wf -- "verify: health 2xx" --> app
```

| 단계 | ServiceAccount | 하는 일 |
| --- | --- | --- |
| `checkout` | `delivery-git` | 공개 앱 저장소를 요청한 ref로 인증 없이 clone하고 commit SHA를 기록한다 |
| `check-application` | `delivery-runner` | `application.yaml`이 Agent Delivery에 적용한 파일과 같은지 SHA-256으로 확인한다 |
| `test` | `delivery-runner` | `runtime.test.image`에서 `runtime.test.command`를 실행한다. 없으면 건너뛴다 |
| `registry` | `delivery-registry` | ECR repository를 만들고, 같은 commit 이미지가 있으면 재사용한다. 없으면 그 Project에만 push할 수 있는 credential을 만든다 |
| `build` | `delivery-runner` | kaniko로 linux/arm64 이미지를 빌드해 commit SHA tag로 push한다. user namespace·privileged가 필요 없다 |
| `deploy` | `delivery-git` | `projects/<slug>/<env>.yaml`에 이미지 digest와 선언을 commit한다. 이 저장소에만 쓰기 권한이 있는 deploy key(SSM `/k8s/common/app-delivery/gitops-deploy-key`)로 push한다 |
| `verify` | `delivery-verify` | rollout 완료와 `https://<host><health>`의 2xx 응답을 기다린다 |

`test`·`build`는 앱 코드를 실행합니다. 이 Pod는 Git 자격 증명·AWS·Kubernetes 권한을 받지 않고, NetworkPolicy
`delivery-runner`로 DNS와 공인 인터넷에만 연결합니다.

## 앱 주소

| Environment | 주소 |
| --- | --- |
| `prod` | `https://<slug>.apps.rovr.dev` |
| `dev` | `https://<slug>-dev.apps.rovr.dev` |

인증서와 DNS는 terraform-env-demo `demo/3-alb`가, push 역할은 `demo/8-app-delivery`가 만듭니다.

## 변경과 검증

chart를 바꾸면 다음을 실행합니다. Helm 4와 Python `pyyaml`이 필요합니다.

```bash
./scripts/test.sh
```

- `projects/`는 직접 고치지 않습니다. 값을 바꾸려면 앱 저장소의 `application.yaml`을 고치고 Agent Delivery에 다시 적용한 뒤 새 Delivery를 시작합니다.
- 앱을 내리려면 `projects/<slug>/<env>.yaml`을 지우는 PR을 병합합니다. Argo CD가 Application과 그 리소스를 지웁니다.
  namespace `app-<slug>-<env>`는 남으므로 확인한 뒤 지웁니다.
- 실패한 Delivery의 Pod와 로그는 7일, 성공한 Workflow는 1일 동안 남습니다.
