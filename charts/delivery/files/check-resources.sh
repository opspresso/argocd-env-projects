# Checks the namespaced resources that Agent Runway rendered for one environment and writes their declarations.
# Usage: sh check-resources.sh <namespace> "<kind> ..." "<chart> ..." <directory> <header> < manifest.yaml
# Every document must be uniquely named and in <namespace> (or have no namespace, which Argo CD sets to the Application's
# namespace). A kro.run instance of an allowed kind goes to <directory>/resources.yaml. A ChartRelease of an allowed chart
# goes to <directory>/resources/<name>.yaml, the values of one Application of ApplicationSet project-resources.
# Removing every resource deletes the environment's databases, so it needs the explicit document
# {apiVersion: v1, kind: List, items: []}. An empty manifest is refused: a lost or truncated parameter must not remove anything.
set -eu
namespace=$1
kinds=$2
charts=$3
directory=$4
header=$5
fail() { echo "$1" >&2; exit 1; }
# yq reads a pipe: given no stdin it would not read the manifest at all.
manifest=$(cat)
[ -n "$(printf '%s' "$manifest" | tr -d '[:space:]')" ] || fail "The manifest is empty. Send {apiVersion: v1, kind: List, items: []} to remove every resource."
documents=$(printf '%s\n' "$manifest" | yq -o json -I 0 'select(. != null)')
[ -n "$documents" ] || fail "The manifest has no document. Send {apiVersion: v1, kind: List, items: []} to remove every resource."
mkdir -p "$directory/resources"
printf '%s\n' "$header" > "$directory/resources.yaml"
# Agent Runway sends exactly {"apiVersion":"v1","kind":"List","items":[]} (one JSON document) for zero resources.
if printf '%s\n' "$documents" | jq -e -s 'any(.[]; .kind == "List")' >/dev/null; then
  printf '%s\n' "$documents" | jq -e -s '. == [{"apiVersion": "v1", "kind": "List", "items": []}]' >/dev/null \
    || fail 'A List may only be the single document {"apiVersion":"v1","kind":"List","items":[]} that removes every resource.'
  exit 0
fi
printf '%s\n' "$documents" | jq -e -s --arg namespace "$namespace" --arg kinds "$kinds" --arg charts "$charts" '
  ($kinds | split(" ")) as $allowed
  | ($charts | split(" ")) as $allowedCharts
  # The chart Service takes the resource name, and the app chart owns the name app.
  | def named: ((.metadata.name // "") | test("^[a-z]([a-z0-9-]{0,48}[a-z0-9])?$")) and ((.metadata.namespace // $namespace) == $namespace);
    def instance: (.apiVersion | type) == "string" and (.apiVersion | startswith("kro.run/")) and ((.kind as $kind | $allowed | index($kind)) != null);
    def release: .apiVersion == "runway.opspresso.com/v1alpha1" and .kind == "ChartRelease"
      and .metadata.name != "app"
      and ((.metadata.labels["opspresso.com/resource"] // "") | test("^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"))
      and ((.spec.chart as $chart | $allowedCharts | index($chart)) != null)
      and ((.spec.version // "") | test("^[0-9]+\\.[0-9]+\\.[0-9]+$"))
      and ((.spec.values | type) == "object")
      and (((.spec.values | keys) - ["passwordSecret", "persistence", "maxMemoryMiB", "peer"]) == []);
    # One Application per resource ID, so an ID may not repeat.
    (map(select(.kind == "ChartRelease") | .metadata.labels["opspresso.com/resource"]?) | length) == (map(select(.kind == "ChartRelease") | .metadata.labels["opspresso.com/resource"]?) | unique | length)
    and (map(.metadata.name) | length) == (map(.metadata.name) | unique | length)
    and all(.[]; type == "object" and named and (instance or release))' >/dev/null \
  || fail "The manifest must hold only uniquely named kro.run objects of kinds [$kinds] or ChartReleases of charts [$charts] in namespace $namespace"
instances=$(printf '%s\n' "$documents" | jq -c 'select(.kind != "ChartRelease")')
[ -z "$instances" ] || printf '%s\n' "$instances" | yq -p json -o yaml '.' >> "$directory/resources.yaml"
printf '%s\n' "$documents" | jq -c 'select(.kind == "ChartRelease")' | while IFS= read -r release; do
  name=$(printf '%s' "$release" | jq -r '.metadata.name')
  { printf '%s\n' "$header"
    printf '%s' "$release" | jq '.spec.values + {chart: .spec.chart, chartVersion: .spec.version, resourceName: .metadata.name,
      resourceId: (.metadata.labels["opspresso.com/resource"] | gsub("-"; ""))}' | yq -p json -o yaml '.'
  } > "$directory/resources/$name.yaml"
done
