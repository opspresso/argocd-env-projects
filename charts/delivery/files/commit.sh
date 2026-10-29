# Commits one file to the GitOps repository with the deploy key, which can write to that repository only.
# Inputs (environment): SOURCE (the file to write), TARGET (its path in the repository), SUBJECT and BODY (commit message),
# and GITOPS_REPOSITORY, GITOPS_BRANCH, GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL from ConfigMap delivery-settings.
set -eu
# ssh refuses a group-readable key, and the Secret volume is group-readable for fsGroup.
install -m 0600 /var/run/delivery/deploy-key/key "$HOME/key"
export GIT_SSH_COMMAND="ssh -i $HOME/key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/etc/delivery/known_hosts"
git config --global user.name "$GIT_AUTHOR_NAME"
git config --global user.email "$GIT_AUTHOR_EMAIL"
git clone -q --depth 20 --branch "$GITOPS_BRANCH" "git@github.com:${GITOPS_REPOSITORY}.git" /tmp/gitops
cd /tmp/gitops
# Returns 1 when the file already has this content, so a resubmitted Workflow succeeds without a new commit.
commit() {
  mkdir -p "$(dirname "$TARGET")"
  cp "$SOURCE" "$TARGET"
  git add "$TARGET"
  git diff --cached --quiet && return 1
  git commit -q -m "$SUBJECT" -m "$BODY"
}
if ! commit; then echo "$TARGET already has this content"; exit 0; fi
# Other Workflows commit to the same branch: only a rejected (non-fast-forward) push is retried on top of theirs.
# Any other failure, such as a refused key, ends the step with Git's own message.
attempt=1
until git push -q origin "HEAD:${GITOPS_BRANCH}" 2>/tmp/push.err; do
  cat /tmp/push.err
  grep -qE "\[rejected\]|non-fast-forward|fetch first" /tmp/push.err || exit 1
  if [ "$attempt" -ge 5 ]; then echo "Could not push after $attempt attempts"; exit 1; fi
  attempt=$((attempt + 1))
  sleep $((attempt * 2))
  git fetch -q origin "$GITOPS_BRANCH"
  git reset -q --hard "origin/${GITOPS_BRANCH}"
  if ! commit; then echo "$TARGET already has this content"; exit 0; fi
done
echo "Committed $TARGET at $(git rev-parse HEAD)"
