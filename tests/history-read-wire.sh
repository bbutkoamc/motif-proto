#!/bin/sh
set -eu

script_dir=$(dirname "$0")
repo_root=$(cd "$script_dir/.." && pwd)
cd "$repo_root"

scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT HUP INT TERM

message=motif.api.v1.ListChangesetsResponse
fixture_dir="$repo_root/tests/fixtures"

buf build .git#ref=refs/remotes/origin/main --path proto/motif/api/v1/persistence.proto -o "$scratch/base.binpb"
buf build --path proto/motif/api/v1/persistence.proto -o "$scratch/current.binpb"

# Existing list fields retain their exact wire representation without the new field.
buf convert "$scratch/base.binpb" --type "$message" --from "$fixture_dir/changeset-list-legacy.json" --to "$scratch/legacy-base.binpb"
buf convert "$scratch/current.binpb" --type "$message" --from "$fixture_dir/changeset-list-legacy.json" --to "$scratch/legacy-current.binpb"
cmp "$scratch/legacy-base.binpb" "$scratch/legacy-current.binpb"

# Cover absent enrichment, actual PENDING and FAILED rows, COMPLETE no-op stages,
# and the RevitCategory/OTHER/UNAVAILABLE category oneof.
buf convert "$scratch/current.binpb" --type "$message" --validate --from "$fixture_dir/changeset-list-semantic-states.json" --to "$scratch/semantic.binpb"
buf convert "$scratch/current.binpb" --type "$message" --from "$scratch/semantic.binpb" --to "$scratch/semantic-roundtrip.json"
jq -e '
  (.changesets[0].hasDerived == true) and
  (.changesets[0] | has("stageSummaries") | not) and
  ([.changesets[1].stageSummaries[].state] == ["STATE_PENDING", "STATE_PENDING"]) and
  ([.changesets[1].stageSummaries[] | (.changeCounts // [] | length)] == [0, 0]) and
  ([.changesets[2].stageSummaries[].state] == ["STATE_FAILED", "STATE_FAILED"]) and
  ([.changesets[2].stageSummaries[] | (.changeCounts // [] | length)] == [0, 0]) and
  ([.changesets[3].stageSummaries[].stage] == ["STAGE_CLIENT", "STAGE_DERIVED"]) and
  ([.changesets[3].stageSummaries[].state] == ["STATE_COMPLETE", "STATE_COMPLETE"]) and
  ([.changesets[3].stageSummaries[] | (.changeCounts // [] | length)] == [0, 0]) and
  (.changesets[4].stageSummaries[0].changeCounts[0].revitCategory == "REVIT_CATEGORY_WALLS") and
  (.changesets[4].stageSummaries[0].changeCounts[1].categoryFallback == "CATEGORY_FALLBACK_OTHER") and
  (.changesets[4].stageSummaries[0].changeCounts[2].categoryFallback == "CATEGORY_FALLBACK_UNAVAILABLE") and
  ([.changesets[4].stageSummaries[0].changeCounts[].verb] == ["VERB_CREATE", "VERB_UPDATE", "VERB_DELETE"])
' "$scratch/semantic-roundtrip.json" >/dev/null

# The descriptor preserves an unknown future enum numerically; consumers must not
# reinterpret it as COMPLETE.
jq '.changesets[1].stageSummaries[0].state = 17' "$fixture_dir/changeset-list-semantic-states.json" > "$scratch/unknown-state.json"
buf convert "$scratch/current.binpb" --type "$message" --from "$scratch/unknown-state.json" --to "$scratch/unknown-state.binpb"
buf convert "$scratch/current.binpb" --type "$message" --from "$scratch/unknown-state.binpb" --to "$scratch/unknown-state-roundtrip.json"
jq -e '.changesets[1].stageSummaries[0].state == 17' "$scratch/unknown-state-roundtrip.json" >/dev/null
buf convert "$scratch/current.binpb" --type "$message" --from "$scratch/unknown-state-roundtrip.json" --to "$scratch/unknown-state-reencoded.binpb"
cmp "$scratch/unknown-state.binpb" "$scratch/unknown-state-reencoded.binpb"

# Reject invalid persisted shapes at the schema validation boundary.
expect_invalid() {
  fixture=$1
  if buf convert "$scratch/current.binpb" --type "$message" --validate --from "$fixture" --to "$scratch/invalid.binpb" >/dev/null 2>&1; then
    echo "expected validation failure for $fixture" >&2
    exit 1
  fi
}

jq '.changesets[1].stageSummaries[0].changeCounts = [{"verb":"VERB_CREATE","count":1,"revitCategory":"REVIT_CATEGORY_WALLS"}]' \
  "$fixture_dir/changeset-list-semantic-states.json" > "$scratch/pending-with-counts.json"
expect_invalid "$scratch/pending-with-counts.json"

jq 'del(.changesets[1].stageSummaries[1])' \
  "$fixture_dir/changeset-list-semantic-states.json" > "$scratch/missing-applicable-stage.json"
expect_invalid "$scratch/missing-applicable-stage.json"

jq '.changesets[4].stageSummaries[0].changeCounts += [.changesets[4].stageSummaries[0].changeCounts[0]]' \
  "$fixture_dir/changeset-list-semantic-states.json" > "$scratch/duplicate-category-bucket.json"
expect_invalid "$scratch/duplicate-category-bucket.json"

jq '.changesets[4].stageSummaries[0].changeCounts[0].count = 0' \
  "$fixture_dir/changeset-list-semantic-states.json" > "$scratch/zero-count.json"
expect_invalid "$scratch/zero-count.json"

echo "History read wire checks passed."
