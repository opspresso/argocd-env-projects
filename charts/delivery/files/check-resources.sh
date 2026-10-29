# Checks the namespaced resources that Agent Delivery rendered for one environment and prints them as YAML.
# Usage: sh check-resources.sh <namespace> "<kind> ..." < manifest.yaml > resources.yaml
# Every document must be a uniquely named kro.run instance of an allowed kind in <namespace> (or with no namespace,
# which Argo CD sets to the Application's namespace). An empty manifest prints nothing.
set -eu
namespace=$1
kinds=$2
# yq reads a pipe: given no stdin it would not read the manifest at all.
manifest=$(cat)
[ -n "$(printf '%s' "$manifest" | tr -d '[:space:]')" ] || exit 0
documents=$(printf '%s\n' "$manifest" | yq -o json -I 0 'select(. != null)')
[ -n "$documents" ] || exit 0
printf '%s\n' "$documents" | jq -e -s --arg namespace "$namespace" --arg kinds "$kinds" '
  ($kinds | split(" ")) as $allowed
  | (map(.metadata.name) | length) == (map(.metadata.name) | unique | length)
  and all(.[];
    type == "object"
    and (.apiVersion | type) == "string" and (.apiVersion | startswith("kro.run/"))
    and ((.kind as $kind | $allowed | index($kind)) != null)
    and ((.metadata.name // "") | test("^[a-z][a-z0-9-]{0,62}$"))
    and ((.metadata.namespace // $namespace) == $namespace))' >/dev/null || {
  echo "The manifest must hold only uniquely named kro.run objects of kinds [$kinds] in namespace $namespace" >&2
  exit 1
}
printf '%s\n' "$documents" | yq -p json -o yaml '.'
