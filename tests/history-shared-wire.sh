#!/bin/sh
set -eu

script_dir=$(dirname "$0")
cd "$script_dir/.."
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT HUP INT TERM

buf build -o "$scratch/current.json"
buf build -o "$scratch/current.binpb"
event_type=motif.events.v1.HistoryEnrichmentFinished
envelope_type=motif.events.v1.NotificationEnvelope
read_type=motif.api.v1.ChangesetListItem

# The read and event fields must reference the SAME descriptor, not copies.
jq -e '
  [.file[].messageType[]? | select(.name == "ChangesetListItem" or .name == "HistoryEnrichmentFinished") |
    .field[] | select(.name == "stage_summaries") | .typeName] ==
  [".motif.data.history.v1.StageSummary", ".motif.data.history.v1.StageSummary"]
' "$scratch/current.json" >/dev/null || {
  echo "Read and event must share motif.data.history.v1.StageSummary" >&2
  exit 1
}

# Reuse the existing read example; no second hand-maintained category fixture.
jq '.changesets[4]' tests/fixtures/changeset-list-semantic-states.json > "$scratch/read.json"
jq '{modelId:"AAAAAAAAAAAAAAAAAAAAAQ==",version:.version,status:"STATUS_COMPLETE",stageSummaries}' \
  "$scratch/read.json" > "$scratch/event.json"
buf convert "$scratch/current.binpb" --type "$read_type" --validate --from "$scratch/read.json" --to "$scratch/read.binpb"
buf convert "$scratch/current.binpb" --type "$event_type" --validate --from "$scratch/event.json" --to "$scratch/event.binpb"
buf convert "$scratch/current.binpb" --type "$event_type" --from "$scratch/event.binpb" --to "$scratch/roundtrip.json"
jq -e --slurpfile read "$scratch/read.json" '.stageSummaries == $read[0].stageSummaries' "$scratch/roundtrip.json" >/dev/null

# Payload-free COMPLETE (legacy/size fallback) and FAILED remain valid.
for status in STATUS_COMPLETE STATUS_FAILED; do
  jq --arg status "$status" 'del(.stageSummaries) | .status=$status' "$scratch/event.json" > "$scratch/hint.json"
  buf convert "$scratch/current.binpb" --type "$event_type" --validate --from "$scratch/hint.json" --to "$scratch/hint.binpb"
done

expect_invalid() {
  if buf convert "$scratch/current.binpb" --type "$event_type" --validate --from "$1" --to "$scratch/invalid.binpb" >/dev/null 2>&1; then
    echo "Expected invalid event: $1" >&2
    exit 1
  fi
}
for filter in \
  '.status="STATUS_FAILED"' \
  '.stageSummaries[0].state="STATE_PENDING"' \
  '.stageSummaries[0].stage="STAGE_DERIVED"' \
  '.stageSummaries += .stageSummaries' \
  '.stageSummaries[0].changeCounts += [.stageSummaries[0].changeCounts[0]]' \
  '.stageSummaries[0].changeCounts[0].count=0' \
  '.stageSummaries[0].changeCounts[0].revitCategory=0'; do
  jq "$filter" "$scratch/event.json" > "$scratch/invalid.json"
  expect_invalid "$scratch/invalid.json"
done

# A category introduced by a newer producer survives old-client validation and
# round trips numerically; it must not become OTHER or drop the whole event.
jq '.stageSummaries[0].changeCounts[0].revitCategory=2147483647' "$scratch/event.json" > "$scratch/future.json"
buf convert "$scratch/current.binpb" --type "$event_type" --validate --from "$scratch/future.json" --to "$scratch/future.binpb"
buf convert "$scratch/current.binpb" --type "$event_type" --from "$scratch/future.binpb" --to "$scratch/future-roundtrip.json"
jq -e '.stageSummaries[0].changeCounts[0].revitCategory==2147483647' "$scratch/future-roundtrip.json" >/dev/null
buf convert "$scratch/current.binpb" --type "$event_type" --validate --from "$scratch/future-roundtrip.json" --to "$scratch/future-reencoded.binpb"
cmp "$scratch/future.binpb" "$scratch/future-reencoded.binpb"

# Enumerate the current taxonomy from its descriptor. Two stages, three verbs,
# every nonzero category + both fallbacks, max uint32 counts, max event-id length.
# This guards the complete envelope's binary/base64 and compact JSON sizes;
# runtime producers must independently check the actual transport envelope.
jq '
  [.file[] | select(.package=="motif.data.external.v1") | .enumType[]? |
   select(.name=="RevitCategory") | .value[] | select((.number // 0)>0) | .name] as $categories |
  [range(1;4) as $verb |
    ($categories[] | {verb:$verb,count:4294967295,revitCategory:.}),
    (range(1;3) | {verb:$verb,count:4294967295,categoryFallback:.})] as $counts |
  {eventId:("x"*64),occurredAt:"9999-12-31T23:59:59.999999999Z",
   content:{project:{projectId:"AAAAAAAAAAAAAAAAAAAAAg==",
     historyEnrichmentFinished:{modelId:"AAAAAAAAAAAAAAAAAAAAAQ==",version:2147483647,status:"STATUS_COMPLETE",
       stageSummaries:([10,69] | map({stage:.,state:2,changeCounts:$counts}))}}}}
' "$scratch/current.json" > "$scratch/worst.json"
buf convert "$scratch/current.binpb" --type "$envelope_type" --validate --from "$scratch/worst.json" --to "$scratch/worst.binpb"
buf convert "$scratch/current.binpb" --type "$envelope_type" --from "$scratch/worst.binpb" --to "$scratch/worst-roundtrip.json"
binary_bytes=$(wc -c < "$scratch/worst.binpb" | tr -d ' ')
base64_bytes=$((4 * ((binary_bytes + 2) / 3)))
json_bytes=$(jq -cj . "$scratch/worst-roundtrip.json" | wc -c | tr -d ' ')
buckets=$(jq '[.content.project.historyEnrichmentFinished.stageSummaries[].changeCounts[]] | length' "$scratch/worst.json")
# Conservative regression budgets, not claims about any deployed broker limit.
test "$base64_bytes" -le 65536
test "$json_bytes" -le 262144
echo "Shared history wire checks passed: buckets=$buckets binary=$binary_bytes base64=$base64_bytes compact_json=$json_bytes bytes."
