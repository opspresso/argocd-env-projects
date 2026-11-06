# Commits to the GitOps repository with the deploy key, which can write to that repository only.
# Inputs (environment): SOURCE and TARGET, SUBJECT and BODY (commit message),
# and GITOPS_REPOSITORY, GITOPS_BRANCH, GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL from ConfigMap delivery-settings.
# A file SOURCE replaces the file TARGET, and an empty SOURCE removes it. A directory SOURCE is the whole resource
# declaration of the environment directory TARGET: it replaces resources.yaml and resources/, and nothing else there.
# The environment directory is removed in two commits, so its Application prunes the workload and the resources
# while it still exists:
# 1. A content commit never removes the directory. When no app.yaml and no resource remain, it keeps resources.yaml
#    with no resource, so Argo CD prunes under a comparable Application.
# 2. PRUNE_EMPTY=1 with TARGET=projects/<slug>/<env> (the cleanup steps) removes that directory only when it is still
#    empty, so the projects ApplicationSet deletes the already empty Application and its namespace. A directory that
#    holds app.yaml or a resource (for example from a deployment that raced in) stays.
set -eu
# ssh refuses a group-readable key, and the Secret volume is group-readable for fsGroup.
install -m 0600 /var/run/delivery/deploy-key/key "$HOME/key"
export GIT_SSH_COMMAND="ssh -i $HOME/key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/etc/delivery/known_hosts"
git config --global user.name "$GIT_AUTHOR_NAME"
git config --global user.email "$GIT_AUTHOR_EMAIL"
git clone -q --depth 20 --branch "$GITOPS_BRANCH" "git@github.com:${GITOPS_REPOSITORY}.git" /tmp/gitops
cd /tmp/gitops
# True when the environment directory $1 holds no app.yaml and no resource: resources/ is empty and resources.yaml
# has no line other than comments, blank lines and document separators.
empty_environment() {
  [ ! -f "$1/app.yaml" ] && [ -z "$(ls -A "$1/resources" 2>/dev/null)" ] \
    && ! grep -qvE '^[[:space:]]*(#.*|---[[:space:]]*)?$' "$1/resources.yaml" 2>/dev/null
}
# Returns 1 when the target already has this content, so a resubmitted Workflow succeeds without a new commit.
commit() {
  if [ "${PRUNE_EMPTY:-}" = 1 ]; then
    if [ ! -d "$TARGET" ]; then return 1; fi
    if ! empty_environment "$TARGET"; then echo "$TARGET is not empty"; return 1; fi
    rm -rf "$TARGET"
    git add -A
    git diff --cached --quiet && return 1
    git commit -q -m "$SUBJECT" -m "$BODY"
    return 0
  fi
  if [ -z "$SOURCE" ]; then
    directory=$(dirname "$TARGET")
    rm -f "$TARGET"
  elif [ -d "$SOURCE" ]; then
    directory=$TARGET
    mkdir -p "$TARGET"
    rm -rf "$TARGET/resources.yaml" "$TARGET/resources"
    cp -R "$SOURCE/." "$TARGET/"
  else
    directory=$(dirname "$TARGET")
    mkdir -p "$directory"
    cp "$SOURCE" "$TARGET"
  fi
  # Git keeps no empty directory: without a file the ApplicationSet would delete the Application in this commit.
  if empty_environment "$directory" && [ ! -f "$directory/resources.yaml" ]; then
    printf '%s\n' "# No resources. The cleanup step removes this directory once Argo CD pruned it." > "$directory/resources.yaml"
  fi
  # The whole fresh clone: a directory that is already gone is not a pathspec git add accepts.
  git add -A
  git diff --cached --quiet && return 1
  git commit -q -m "$SUBJECT" -m "$BODY"
}
if ! commit; then echo "$TARGET already has this content"; exit 0; fi
# Other Workflows commit to the same branch: only a push that lost that race is retried on top of theirs. GitHub reports
# a race either as a non-fast-forward rejection or, when both pushes arrive together, as "cannot lock ref".
# Any other failure, such as a refused key, ends the step with Git's own message.
attempt=1
until git push -q origin "HEAD:${GITOPS_BRANCH}" 2>/tmp/push.err; do
  cat /tmp/push.err
  grep -qE "\[rejected\]|non-fast-forward|fetch first|cannot lock ref" /tmp/push.err || exit 1
  if [ "$attempt" -ge 5 ]; then echo "Could not push after $attempt attempts"; exit 1; fi
  attempt=$((attempt + 1))
  sleep $((attempt * 2))
  git fetch -q origin "$GITOPS_BRANCH"
  git reset -q --hard "origin/${GITOPS_BRANCH}"
  if ! commit; then echo "$TARGET already has this content"; exit 0; fi
done
echo "Committed $TARGET at $(git rev-parse HEAD)"
