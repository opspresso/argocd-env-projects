# Renders projects/<project>/<environment>.yaml for charts/app (the opspresso app chart) from Agent Delivery's normalized
# declaration. Inputs: $application (JSON {runtime, environment}), $delivery, $revision, $repository, $digest.
($application.runtime) as $r
| ($application.environment) as $e
| ($e.maxReplicas // $e.replicas) as $max
| { path: $r.health, port: "http" } as $probe
| {
    delivery: $delivery,
    revision: $revision,
    app: (
      {
        image: { reference: ($repository + "@" + $digest) },
        replicaCount: $e.replicas,
        autoscaling: (if $max > $e.replicas then
          { enabled: true, minReplicas: $e.replicas, maxReplicas: $max,
            metrics: [{ type: "Resource", resource: { name: "cpu", target: { type: "Utilization", averageUtilization: 70 } } }] }
          else { enabled: false } end),
        pdb: { enabled: ($e.replicas > 1) },
        service: { targetPort: $r.port },
        env: ([{ name: "PORT", value: ($r.port | tostring) }]
          + ($e.env // {} | to_entries | sort_by(.key) | map({ name: .key, value: (.value | tostring) }))),
        startupProbe: { httpGet: $probe, periodSeconds: 5, failureThreshold: 36 },
        readinessProbe: { httpGet: $probe, periodSeconds: 10, failureThreshold: 3 },
        livenessProbe: { httpGet: $probe, periodSeconds: 20, failureThreshold: 3 },
        resources: { requests: { cpu: $e.cpu, memory: $e.memory }, limits: { memory: $e.memory } },
        # Each Delivery restarts the Pods, so they read the current Variables, and verify can find this rollout.
        podAnnotations: { "opspresso.com/delivery": $delivery, "opspresso.com/revision": $revision }
      }
      + (if $r.command then { command: $r.command } else {} end)
      + (if $r.args then { args: $r.args } else {} end)
      + (if $max > 1 then { topologySpreadConstraints: [{ maxSkew: 1, topologyKey: "kubernetes.io/hostname",
          whenUnsatisfiable: "ScheduleAnyway", labelSelector: { matchLabels: { "app.kubernetes.io/name": "app" } } }] } else {} end)
    )
  }
