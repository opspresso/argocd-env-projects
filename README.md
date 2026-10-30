# argocd-env-projects

Agent Runway가 배포하는 앱의 GitOps 저장소입니다. Studio Agent가 Agent Runway MCP로 시작한 Delivery가 이 저장소에
배포 값을 기록하고, Argo CD가 그 값을 EKS `eks-demo`에 동기화합니다.
전체 계약은 [agent-platform `docs/APP_DELIVERY.md`](https://github.com/opspresso/agent-platform/blob/main/docs/APP_DELIVERY.md)와
[`docs/DELIVERY_V2.md`](https://github.com/opspresso/agent-platform/blob/main/docs/DELIVERY_V2.md)에 있습니다.

## 구성

| 경로 | 내용 | 쓰는 주체 |
| --- | --- | --- |
| `charts/app/` | 앱 Environment 하나의 wrapper chart. [opspresso/helm-charts](https://github.com/opspresso/helm-charts)의 `app` chart(argocd-env-apps의 sample-node·agent-*와 같은 chart)의 설정으로 Deployment·Service·HTTPRoute·HPA·PDB·SecretStore·ExternalSecret·NetworkPolicy·ResourceQuota를 만든다. `app.yaml`이 없으면 Deployment·Service·HTTPRoute 없이 tenant 객체만 만든다. `app.raw`에는 환경·namespace·host·image digest를 검사하는 guard만 둔다 | 운영자(PR) |
| `charts/delivery/` | namespace `app-delivery`의 Argo Workflows WorkflowTemplate `app-build`·`app-deploy`·`resource-sync`, 단계별 ServiceAccount·RBAC, 설정 | 운영자(PR) |
| `projects/<slug>/<env>/app.yaml` | Environment 하나의 `app` chart 값: 이미지 digest, 복제 수, port, env, probe, 자원. `app-deploy`의 deploy 단계가 `charts/delivery/files/render.jq`로 Agent Runway의 선언에서 만든다 | `app-deploy` workflow만 |
| `projects/<slug>/<env>/resources.yaml` | Environment 하나의 namespace 리소스(kro 인스턴스 `RdsPostgres`·`S3Bucket`·`ValkeyCache`). Agent Runway가 렌더링한 manifest를 `resource-sync`의 write 단계가 검사해 기록한다 | `resource-sync` workflow만 |
| `scripts/test.sh` | 두 chart를 Argo CD와 같은 parameter로 렌더링하고 계약을 검사한다 | |

Argo CD 객체는 [argocd-env-addons](https://github.com/opspresso/argocd-env-addons)의 `charts/argo-cd`가 선언합니다.

- AppProject `projects`: `app-*` namespace(단, `app-delivery` 제외)에 `charts/app`이 만드는 종류와 플랫폼 kro 인스턴스 종류만 허용합니다.
- AppProject `delivery`: `app-delivery` namespace와 `delivery-verify` ClusterRole만 허용합니다.
- Application `app-delivery`: `charts/delivery`를 동기화합니다.
- ApplicationSet `projects`: 허용한 환경 목록(`app_delivery.environments`)의 `projects/*/<env>` 디렉터리마다
  Application `app-<slug>-<env>`를 만듭니다. Application은 `charts/app`(값: `app.yaml`)과 `resources.yaml`을 함께 동기화합니다.
  Project·Environment·namespace·도메인은 디렉터리 경로와 클러스터 설정에서 정하고, `charts/app`의 guard가 검사합니다.
  guard는 값 파일의 `app.raw`로 끌 수 있으므로 보조 방어입니다. 실제 경계는 `app.yaml`을 `deploy` 단계만 고정 매핑 `render.jq`로 쓰고,
  deploy key를 `delivery-git` 단계(`deploy`·`write`)만 가진다는 점입니다.

## Delivery 흐름

Agent Runway는 build 한 번의 결과를 Release로, Release를 한 환경에 배포하는 작업을 Deployment로 기록합니다.
승격은 같은 Release(같은 image digest)를 다음 환경에 배포하는 것입니다.

```mermaid
flowchart LR
  agent["Studio Agent"] -- "MCP" --> delivery["Agent Runway"]
  delivery -- "build-&lt;Release&gt;" --> build["app-build"]
  build -- "checkout·test·build" --> ecr["ECR projects/&lt;slug&gt;"]
  delivery -- "deploy-&lt;Deployment&gt;" --> deploy["app-deploy"]
  deploy -- "deploy: commit" --> repo["argocd-env-projects<br/>projects/&lt;slug&gt;/&lt;env&gt;/"]
  delivery -- "resources-&lt;Sync&gt;" --> sync["resource-sync"]
  sync -- "write: commit" --> repo
  repo --> argocd["Argo CD<br/>ApplicationSet projects"]
  argocd --> app["app-&lt;slug&gt;-&lt;env&gt;<br/>Deployment·HTTPRoute·kro 인스턴스"]
  deploy -- "verify: health 2xx" --> app
```

| WorkflowTemplate | 단계 | ServiceAccount | 하는 일 |
| --- | --- | --- | --- |
| `app-build` | `checkout` | `delivery-git` | 요청한 ref를 clone하고 commit SHA를 기록한다. Secret `<workflow>-checkout`의 token이 있으면(관리자가 허용한 비공개 저장소) github.com 요청 header로만 쓴다 |
| `app-build` | `check-application` | `delivery-runner` | `application.yaml`이 Agent Runway에 적용한 파일과 같은지 SHA-256으로 확인한다. 다르면 exit code 3이다 |
| `app-build` | `test` | `delivery-runner` | `runtime.test.image`에서 `runtime.test.command`를 실행한다. 없으면 건너뛴다 |
| `app-build` | `registry` | `delivery-registry` | ECR repository를 만들고, 같은 commit 이미지가 있으면 재사용한다. 없으면 그 Project에만 push할 수 있는 credential을 만든다 |
| `app-build` | `build` | `delivery-runner` | kaniko로 linux/arm64 이미지를 빌드해 commit SHA tag로 push한다. user namespace·privileged가 필요 없다 |
| `app-deploy` | `deploy` | `delivery-git` | 이미지가 그 Project의 digest 고정 이미지인지 확인하고 `projects/<slug>/<env>/app.yaml`에 기록한다. 이 저장소에만 쓰기 권한이 있는 deploy key(SSM `/k8s/common/app-delivery/gitops-deploy-key`)로 push한다 |
| `app-deploy` | `verify` | `delivery-verify` | rollout 완료와 `https://<host><health>`의 2xx 응답을 기다린다 |
| `resource-sync` | `write` | `delivery-git` | manifest가 허용한 kro 인스턴스 종류(`resourceKinds`)이고 그 환경 namespace에 속하는지, 이름이 `^[a-z]([a-z0-9-]{0,48}[a-z0-9])?$`이고 고유한지 확인하고 `projects/<slug>/<env>/resources.yaml`에 기록한다. 리소스를 모두 지우려면 `{apiVersion: v1, kind: List, items: []}` 문서를 보낸다. 빈 manifest는 거부한다 |

`resource-sync`와 `app-deploy`는 환경마다 mutex(`resources-<slug>-<env>`, `deploy-<slug>-<env>`)를 잡습니다. 같은 환경의 다음 Workflow는 앞 Workflow가 끝날 때까지 `Pending`으로 기다리고, 다른 환경과 `app-build`는 기다리지 않습니다.

`test`·`build`는 앱 코드를 실행합니다. 이 Pod는 Git 자격 증명·AWS·Kubernetes 권한을 받지 않고, NetworkPolicy
`delivery-runner`로 DNS와 공인 인터넷에만 연결합니다.

## 앱 주소

환경 목록과 primary 환경은 argocd-env-addons의 `app_delivery.environments`·`app_delivery.primary_environment`가 정합니다.
Agent Runway의 `DELIVERY_ENVIRONMENTS`와 같아야 합니다. 기본값은 `[dev, prod]`, primary는 `prod`입니다.

| Environment | 주소 |
| --- | --- |
| primary(기본 `prod`) | `https://<slug>.apps.rovr.dev` |
| 그 밖의 환경(예: `dev`) | `https://<slug>-<env>.apps.rovr.dev` |

인증서와 DNS는 terraform-env-demo `demo/3-alb`가, push 역할은 `demo/8-app-delivery`가 만듭니다.

## 변경과 검증

chart를 바꾸면 다음을 실행합니다. Helm 4와 Python `pyyaml`이 필요합니다.

```bash
./scripts/test.sh
```

- `projects/`는 직접 고치지 않습니다. 값을 바꾸려면 앱 저장소의 `application.yaml`을 고치고 Agent Runway에 다시 적용한 뒤 새 Delivery를 시작합니다.
- 앱을 내리려면 `projects/<slug>/<env>/` 디렉터리를 지우는 PR을 병합합니다. Argo CD가 Application과 그 리소스(kro 인스턴스 포함)를 지웁니다.
  `RdsPostgres`는 final snapshot `<DB 식별자>-final-<인스턴스 UID>`만 남기고 RDS가 삭제됩니다. `ValkeyCache`는 snapshot 없이 삭제됩니다.
  `S3Bucket`은 bucket이 비어 있을 때만 삭제됩니다. 객체가 남아 있으면 인스턴스가 `BucketNotEmpty` 메시지와 함께 남고 Application 삭제도 끝나지 않으므로,
  객체(버전 포함)를 지운 뒤 다시 확인합니다. namespace `app-<slug>-<env>`는 남으므로 확인한 뒤 지웁니다.
- 환경 하나에 `RdsPostgres`·`S3Bucket`·`ValkeyCache`는 종류마다 2개까지입니다(ResourceQuota `count/rdspostgres.kro.run`·`count/s3buckets.kro.run`·`count/valkeycaches.kro.run`).

## 환경 추가

환경 목록은 argocd-env-addons `app_delivery.environments`와 argocd-env-apps의 Agent Runway `DELIVERY_ENVIRONMENTS`가 정합니다.

1. 사전 점검: 기존 Project slug가 `-<새 환경>`으로 끝나지 않는지 확인합니다. 끝나면 그 Project의 primary host가 다른 Project의 새 환경 host와 겹칩니다.
   ```bash
   ls projects | grep -- '-<새 환경>$'   # 출력이 없어야 합니다
   ```
   출력이 있으면 환경 이름을 바꾸거나 해당 Project를 먼저 정리합니다. Agent Runway도 부팅 때 같은 충돌을 거절합니다.
2. 두 저장소에 같은 이름·순서·primary로 환경을 추가합니다. argocd-env-addons는 `argocd` Application을 수동 sync합니다.
3. 환경을 목록에서 지우면 그 환경의 Application이 지워지고, resources finalizer로 앱과 kro 인스턴스가 함께 삭제됩니다(RDS는 final snapshot만 남고, 객체가 남은 `S3Bucket`은 비울 때까지 삭제되지 않음).
- 성공·실패한 Workflow는 1일 동안 남습니다. `app-build`·`app-deploy`는 성공·실패와 관계없이 단계 Pod와 로그도 같은 기간 남아
  Agent Runway의 `deployment_logs`로 읽을 수 있습니다. `resource-sync`는 실패한 Workflow의 Pod만 남깁니다.
