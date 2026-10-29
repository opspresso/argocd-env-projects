# projects

환경마다 디렉터리 하나(`<slug>/<env>/`)이며 두 파일은 workflow만 씁니다. 직접 고치지 않습니다.

| 파일 | 쓰는 단계 |
| --- | --- |
| `app.yaml` | `app-deploy` workflow의 `deploy` |
| `resources.yaml` | `resource-sync` workflow의 `write` |

값을 바꾸려면 앱 저장소의 `application.yaml`을 고치고 Agent Delivery `project_apply` 뒤에 다시 배포합니다.
리소스는 Agent Delivery에서 만들고 지웁니다.
