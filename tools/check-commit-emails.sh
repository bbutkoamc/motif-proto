#!/bin/sh
# Fail if any commit in a range carries a Motif-internal email address.
#
# This repository is public, so author and committer identities and the whole
# commit message — including trailers that editors and AI tools append on their
# own — are world-readable. Commit with a GitHub `users.noreply.github.com`
# address, or any public address you are happy to share, so an internal address
# is never published.
#
# Only the internal domains match, so an external contributor committing under
# their own public address is never blocked. On a match the check prints a fixed
# message and nothing more: echoing the offending address into a public CI log
# would republish the very thing the check exists to keep private.
#
# Range resolution, first match wins:
#   $1 · $COMMIT_RANGE · origin/main..HEAD
#
# Run via `mise run check-commit-emails` (part of `mise run lint`, and run in CI
# against the commits a pull request adds).

set -eu

range="${1:-${COMMIT_RANGE:-origin/main..HEAD}}"

# %an/%ae/%cn/%ce are the author and committer identities — the name fields are
# included because some tooling sets the name to an email address. %B is the
# subject plus body, where `Co-authored-by:` and similar trailers live.
commits=$(git log "$range" --format='%an%n%ae%n%cn%n%ce%n%B')

if printf '%s\n' "$commits" | grep -Eiq '@([a-z0-9-]+\.)*(motif\.io|ambr\.net)'; then
  cat >&2 <<MSG
Motif internal email addresses must avoid being leaked in commit messages

Set a public commit email, then rewrite the commits that carry the internal one
so it is not published. See "Commit and PR hygiene" in AGENTS.md. Inspect the
range locally with:

  git log --format=fuller $range
MSG
  exit 1
fi
