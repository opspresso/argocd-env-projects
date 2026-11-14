# Renders projects/<project>/<environment>/app.yaml for charts/app (the opspresso app chart) from Agent Runway's
# normalized declaration. Inputs: $application (JSON {runtime, environment, config}), $deployment, $image (pinned by digest)
# and $host, the public host Agent Runway chose for this Project environment (<slug>.<domain> or <slug>-<environment>.<domain>;
# the guard of charts/app refuses any other).
# `config` is the environment's Variables that are not secret, as Agent Runway holds them when it submits the Deployment;
# they reach the Pods through the chart's ConfigMap. Secret Variables reach them through ExternalSecret app-env.
# `environment.architecture` (amd64 or arm64) is the architecture of the Release image: the Pods run only on nodes of
# that architecture, and amd64 Pods tolerate the taint of the amd64 NodePool. Without it the Pods have no constraint.
($application.runtime) as $r
| ($application.environment) as $e
| ($e.maxReplicas // $e.replicas) as $max
| ($application.config // {}) as $config
| { path: $r.health, port: "http" } as $probe
| ($e.architecture // null) as $arch
| if $arch == null or $arch == "amd64" or $arch == "arm64" then . else error("architecture must be amd64 or arm64, not \($arch)") end
| {
    deployment: $deployment,
    app: (
      {
        controller: { enabled: true },
        image: { reference: $image },
        replicaCount: $e.replicas,
        autoscaling: (if $max > $e.replicas then
          { enabled: true, minReplicas: $e.replicas, maxReplicas: $max,
            metrics: [{ type: "Resource", resource: { name: "cpu", target: { type: "Utilization", averageUtilization: 70 } } }] }
          else { enabled: false } end),
        pdb: { enabled: ($e.replicas > 1) },
        service: { enabled: true, targetPort: $r.port },
        routing: { hosts: [$host], httpRoute: { enabled: true } },
        env: ([{ name: "PORT", value: ($r.port | tostring) }]
          + ($e.env // {} | to_entries | sort_by(.key) | map({ name: .key, value: (.value | tostring) }))),
        startupProbe: { httpGet: $probe, periodSeconds: 5, failureThreshold: 36 },
        readinessProbe: { httpGet: $probe, periodSeconds: 10, failureThreshold: 3 },
        livenessProbe: { httpGet: { path: ($r.liveness // $r.health), port: "http" }, periodSeconds: 20, failureThreshold: 3 },
        resources: { requests: { cpu: $e.cpu, memory: $e.memory }, limits: { memory: $e.memory } },
        # Each Deployment refreshes app-env before the rollout (ExternalSecret sync-wave -1) and restarts the Pods,
        # so they read Variables created moments before, and verify can find this rollout.
        externalSecrets: { annotations: { "force-sync": $deployment } },
        # A changed value changes the chart's config checksum, so the Pods restart with it.
        configmap: (if ($config | length) > 0 then { enabled: true, data: ($config | with_entries(.value |= tostring)) } else { enabled: false } end),
        podAnnotations: { "opspresso.com/deployment": $deployment }
      }
      + (if $r.command then { command: $r.command } else {} end)
      + (if $r.args then { args: $r.args } else {} end)
      + (if $max > 1 then { topologySpreadConstraints: [{ maxSkew: 1, topologyKey: "kubernetes.io/hostname",
          whenUnsatisfiable: "ScheduleAnyway", labelSelector: { matchLabels: { "app.kubernetes.io/name": "app" } } }] } else {} end)
      + (if $arch then { nodeSelector: { "kubernetes.io/arch": $arch } } else {} end)
      + (if $arch == "amd64" then { tolerations: [{ key: "opspresso.com/arch", operator: "Equal", value: "amd64",
          effect: "NoSchedule" }] } else {} end)
    )
  }
