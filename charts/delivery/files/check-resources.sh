# Checks the namespaced resources that Agent Delivery rendered for one environment and prints them as YAML.
# Usage: sh check-resources.sh <namespace> "<kind> ..." < manifest.yaml > resources.yaml
# Every document must be a uniquely named kro.run instance of an allowed kind in <namespace> (or with no namespace,
# which Argo CD sets to the Application's namespace). Removing every resource deletes the environment's databases, so
# it needs the explicit document {apiVersion: v1, kind: List, items: []}, which prints nothing. An empty manifest is
# refused: a lost or truncated parameter must not remove anything.
set -eu
namespace=$1
kinds=$2
fail() { echo "$1" >&2; exit 1; }
# yq reads a pipe: given no stdin it would not read the manifest at all.
manifest=$(cat)
[ -n "$(printf '%s' "$manifest" | tr -d '[:space:]')" ] || fail "The manifest is empty. Send {apiVersion: v1, kind: List, items: []} to remove every resource."
documents=$(printf '%s\n' "$manifest" | yq -o json -I 0 'select(. != null)')
[ -n "$documents" ] || fail "The manifest has no document. Send {apiVersion: v1, kind: List, items: []} to remove every resource."
# Agent Delivery sends exactly {"apiVersion":"v1","kind":"List","items":[]} (one JSON document) for zero resources.
if printf '%s\n' "$documents" | jq -e -s 'any(.[]; .kind == "List")' >/dev/null; then
  printf '%s\n' "$documents" | jq -e -s '. == [{"apiVersion": "v1", "kind": "List", "items": []}]' >/dev/null \
    || fail 'A List may only be the single document {"apiVersion":"v1","kind":"List","items":[]} that removes every resource.'
  exit 0
fi
printf '%s\n' "$documents" | jq -e -s --arg namespace "$namespace" --arg kinds "$kinds" '
  ($kinds | split(" ")) as $allowed
  | (map(.metadata.name) | length) == (map(.metadata.name) | unique | length)
  and all(.[];
    type == "object"
    and (.apiVersion | type) == "string" and (.apiVersion | startswith("kro.run/"))
    and ((.kind as $kind | $allowed | index($kind)) != null)
    and ((.metadata.name // "") | test("^[a-z]([a-z0-9-]{0,48}[a-z0-9])?$"))
    and ((.metadata.namespace // $namespace) == $namespace))' >/dev/null \
  || fail "The manifest must hold only uniquely named kro.run objects of kinds [$kinds] in namespace $namespace"
printf '%s\n' "$documents" | yq -p json -o yaml '.'
