# Shared by the steps of the VM Target Workflows (app-deploy-eb, app-remove-eb). Source it with `.`.
# Checks the parameters, reads the Target JSON and assumes the Target's deploy role. The step's ServiceAccount
# (delivery-target) may assume only roles named agent-runway-target-deploy (terraform-env-demo demo/4-role), and the
# session tags limit the deploy role to this Project environment's stack.
# A Target in another AWS account has externalId (agent-platform docs/DELIVERY_V4.md D4), which that account's trust
# policy requires.

printf '%s' "$PROJECT" | grep -qE '^[a-z][a-z0-9-]{1,30}[a-z0-9]$' || { echo "Invalid project $PROJECT"; exit 1; }
printf '%s' "$ENVIRONMENT" | grep -qE '^[a-z][a-z0-9]{1,11}$' || { echo "Invalid environment $ENVIRONMENT"; exit 1; }
printf '%s' "$TARGET" | jq -e --arg kind "$TARGET_KIND" '.kind == $kind' >/dev/null \
  || { echo "Target is not of kind $TARGET_KIND"; exit 1; }

# One string field of the Target; a missing field stops the step.
field() {
  printf '%s' "$TARGET" | jq -er --arg key "$1" '.[$key] | strings' || { echo "Target field $1 is missing" >&2; exit 1; }
}

# Stops the step unless every named field is a string (list fields: a non-empty list). Call it first: a failed
# `$(field ...)` inside a command argument does not stop a `set -e` shell.
require() {
  for key in "$@"; do (field "$key") >/dev/null 2>&1 || (list "$key") >/dev/null 2>&1 || { echo "Target field $key is missing"; exit 1; }; done
}

# A list field of the Target as CloudFormation's comma-delimited list.
list() {
  printf '%s' "$TARGET" | jq -er --arg key "$1" '.[$key] | arrays | select(length > 0) | join(",")' \
    || { echo "Target field $1 must be a non-empty list" >&2; exit 1; }
}

# Exports the credentials of the Target's deploy role for the rest of the step.
assume_target() {
  role=$(field deployRole)
  printf '%s' "$role" | grep -qE '^arn:aws:iam::[0-9]{12}:role/agent-runway-target-deploy$' \
    || { echo "Role $role is not an Agent Runway Target deploy role"; exit 1; }
  external_id=$(printf '%s' "$TARGET" \
    | jq -er '.externalId // "" | strings | select(. == "" or test("\\A[A-Za-z0-9+=,.@:/-]{2,1224}\\z"))') \
    || { echo "Target field externalId is not an external ID"; exit 1; }
  set -- --role-arn "$role" --role-session-name "$1" --duration-seconds 3600
  [ -n "$external_id" ] && set -- "$@" --external-id "$external_id"
  set -- "$@" --tags "Key=delivery-project,Value=${PROJECT}" "Key=delivery-env,Value=${ENVIRONMENT}"
  session=$(aws sts assume-role "$@" --output json)
  AWS_ACCESS_KEY_ID=$(printf '%s' "$session" | jq -r .Credentials.AccessKeyId)
  AWS_SECRET_ACCESS_KEY=$(printf '%s' "$session" | jq -r .Credentials.SecretAccessKey)
  AWS_SESSION_TOKEN=$(printf '%s' "$session" | jq -r .Credentials.SessionToken)
  AWS_DEFAULT_REGION=$(field region)
  AWS_REGION=$AWS_DEFAULT_REGION
  export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_DEFAULT_REGION AWS_REGION
  unset AWS_ROLE_ARN AWS_WEB_IDENTITY_TOKEN_FILE AWS_CONTAINER_CREDENTIALS_FULL_URI AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE
}

# The instance type of this Project environment. Agent Runway resolves the Project's size into
# application.environment.instanceType (agent-platform docs/DELIVERY_V4.md D3).
instance_type() {
  printf '%s' "$APPLICATION" | jq -er '.environment.instanceType | strings | select(test("\\A[a-z][a-z0-9-]*\\.[a-z0-9]+\\z"))' \
    || { echo "application field environment.instanceType is missing or is not an instance type" >&2; exit 1; }
}

# How this Project environment replaces a running Release, as the Elastic Beanstalk deployment policy. Agent Runway sets
# application.environment.rollout: "all-at-once" in a sandbox environment (every instance takes the new Release at
# once; the app is down meanwhile), "immutable" or nothing elsewhere (new instances replace the old ones only when
# they pass the health check).
rollout() {
  case "$(printf '%s' "$APPLICATION" | jq -r '.environment.rollout // "immutable" | tostring')" in
    immutable) echo Immutable ;;
    all-at-once) echo AllAtOnce ;;
    *) echo "application field environment.rollout must be immutable or all-at-once" >&2; exit 1 ;;
  esac
}

# Writes the Target's install script (agent-platform docs/DELIVERY_V4.md D5) to the file $1. The file is empty when
# the Target has none. The script is at most 16 KiB and starts with #!/bin/bash or #!/bin/sh.
install_script() {
  printf '%s' "$TARGET" | jq -e '.installScript // "" | strings' >/dev/null \
    || { echo "Target field installScript must be a string"; exit 1; }
  printf '%s' "$TARGET" | jq -j '.installScript // ""' > "$1"
  [ -s "$1" ] || return 0
  [ $(( $(wc -c < "$1") )) -le 16384 ] || { echo "Target field installScript exceeds 16 KiB"; exit 1; }
  head -n 1 "$1" | grep -qE '^#!/bin/(bash|sh)( .*)?$' \
    || { echo "Target field installScript must start with #!/bin/bash or #!/bin/sh"; exit 1; }
}
