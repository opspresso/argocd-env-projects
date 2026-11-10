# Shared by the steps of the VM Target Workflows (app-deploy-eb, app-remove-eb). Source it with `.`.
# Checks the parameters, reads the Target JSON and assumes the Target's deploy role. The step's ServiceAccount
# (delivery-target) may assume only roles named agent-runway-target-deploy and agent-runway-target-bake
# (terraform-env-demo demo/4-role), and the session tags limit the deploy role to this Project environment's stack.

# A Deployment step names a Project environment; the image bake step names none (PROJECT unset).
if [ -n "${PROJECT:-}" ]; then
  printf '%s' "$PROJECT" | grep -qE '^[a-z][a-z0-9-]{1,30}[a-z0-9]$' || { echo "Invalid project $PROJECT"; exit 1; }
  printf '%s' "$ENVIRONMENT" | grep -qE '^[a-z][a-z0-9]{1,11}$' || { echo "Invalid environment $ENVIRONMENT"; exit 1; }
fi
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

# Exports the credentials of the Target's deploy role (or, with ROLE_FIELD=bakeRole, its image bake role) for the rest
# of the step.
assume_target() {
  field_name=${ROLE_FIELD:-deployRole}
  suffix=deploy
  [ "$field_name" = bakeRole ] && suffix=bake
  role=$(field "$field_name")
  printf '%s' "$role" | grep -qE "^arn:aws:iam::[0-9]{12}:role/agent-runway-target-${suffix}\$" \
    || { echo "Role $role is not an Agent Runway Target ${suffix} role"; exit 1; }
  if [ -n "${PROJECT:-}" ]; then
    session=$(aws sts assume-role --role-arn "$role" --role-session-name "$1" --duration-seconds 3600 \
      --tags "Key=delivery-project,Value=${PROJECT}" "Key=delivery-env,Value=${ENVIRONMENT}" --output json)
  else
    session=$(aws sts assume-role --role-arn "$role" --role-session-name "$1" --duration-seconds 3600 --output json)
  fi
  AWS_ACCESS_KEY_ID=$(printf '%s' "$session" | jq -r .Credentials.AccessKeyId)
  AWS_SECRET_ACCESS_KEY=$(printf '%s' "$session" | jq -r .Credentials.SecretAccessKey)
  AWS_SESSION_TOKEN=$(printf '%s' "$session" | jq -r .Credentials.SessionToken)
  AWS_DEFAULT_REGION=$(field region)
  AWS_REGION=$AWS_DEFAULT_REGION
  export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_DEFAULT_REGION AWS_REGION
  unset AWS_ROLE_ARN AWS_WEB_IDENTITY_TOKEN_FILE AWS_CONTAINER_CREDENTIALS_FULL_URI AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE
}
