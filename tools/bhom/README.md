# BHoM Proto Generator Scaffold

This directory contains the reproducible preparation slice for generating
BHoM-shaped protobuf definitions from pinned BHoM JSON Schemas.

The generator consumes JSON Schema files from
`https://github.com/BHoM/BHoM_JSONSchema` at the immutable commit recorded in
`schema.lock.json`. The moving `develop` branch is recorded only in upstream
schema `$id` values; generation must use the pinned commit.

Current scope is intentionally narrow:

- entry schemas: `Structure_oM/Elements/Bar.json` and
  `Physical_oM/Elements/Floor.json`
- generation accepts an ordered batch of roots with repeated `--entry` flags;
  output bundle and manifest ordering are deterministic and independent of root
  input order
- Bar supported closure: Bar, Node, BarFEAType, Point, and source-fidelity
  Spatial ShapeProfiles RectangleProfile, CircleProfile, TubeProfile,
  BoxProfile, AngleProfile, and ShapeType. The profile DTOs are a declared
  batch companion closure because Bar.SectionProperty is an upstream
  polymorphic interface and remains source-preserved JSON.
- Floor supported closure: Floor -> PlanarSurface -> Polygon or closed Polyline
  -> Point
- Point maps to canonical `motif.data.geometry.v1.Point3`
- unsupported interfaces and polymorphic fields are preserved as
  `google.protobuf.Value` and listed in the manifest
- projection/type-kind decisions live in `projection_catalog.json`, not in the
  generated proto shape

## Ownership Boundary

Generated source-fidelity BHoM types live in `thirdparty.bhom.generated.<domain>.v1`
packages under `thirdparty/bhom/generated/...`.

The existing handwritten `thirdparty.bhom.data.structure.v1.Bar` remains Raven's native
editable projection component with Motif component ID `137`. The generator does
not own that message, does not register generated messages as Motif components,
and does not infer ECS behavior from BHoM JSON Schema.

Imported BHoM entities use the shared handwritten
`thirdparty.bhom.data.base.v1.BHoMSource` provenance/source component with component ID
`151`. Round-trip export starts from `BHoMSource.source_payload_json` and
overwrites only the native projection-owned Bar fields: `Start.Position`,
`End.Position`, and `OrientationAngle`. Unknown/unmapped fields, `BHoM_Guid`,
`_t`, and `_bhomVersion` stay owned by the source payload. Motif-authored bars
without `BHoMSource` keep the existing native export path.

The Bar compatibility contract is recorded in `bar_compatibility.json`.

The Bar profile DTOs preserve their BHoM scalar fields and metadata under
`thirdparty.bhom.generated.spatial.v1`; they have no component IDs. Raw profile `Edges`
remain repeated JSON values. Box hollow/radii semantics, Angle root/toe radius
semantics, and all non-listed upstream profile branches are deferred to a
future Raven projection slice and remain source-preserved through `BHoMSource`.

## Floor Support Boundary

Generated `thirdparty.bhom.generated.physical.v1.Floor` is a source-fidelity schema seed
with no Motif component ID. It is not a handwritten
`thirdparty.bhom.data.physical.v1.Floor` component.

This migration preserves only the generated source-fidelity Floor scaffold. It
does not select or imply a Raven projection target:

- `Floor.Location` is represented by `PlanarSurface`
- `PlanarSurface.ExternalBoundary` is represented by `Polygon` or closed
  `Polyline`
- `Point` coordinates map to `motif.data.geometry.v1.Point3`

Deferred/source-preserved Floor branches are recorded per root in generated
manifests and in `projection_catalog.json`: non-planar `ISurface` variants
including `NurbsSurface`, curved/NURBS boundary curves, internal boundaries,
openings, construction/material polymorphism, offsets, fragments, and
`CustomData`. These remain owned by `thirdparty.bhom.data.base.v1.BHoMSource` until a later
explicit projection decision.

Example dry run after fetching or checking out the locked schemas:

```sh
python3 tools/bhom_proto_generator.py generate \
  --schema-root /path/to/BHoM_JSONSchema \
  --entry Structure_oM/Elements/Bar.json \
  --entry Physical_oM/Elements/Floor.json \
  --lock tools/bhom/schema.lock.json \
  --catalog tools/bhom/projection_catalog.json \
  --dry-run
```

Deterministic check against the locked schemas:

```sh
python3 tools/bhom_proto_generator.py check \
  --schema-root /tmp/bhom-jsonschema-locked \
  --entry Structure_oM/Elements/Bar.json \
  --entry Physical_oM/Elements/Floor.json \
  --lock tools/bhom/schema.lock.json \
  --catalog tools/bhom/projection_catalog.json \
  --skip-buf
```

## Committed Registry Output

The Bar/profile batch is materialized under `proto/thirdparty/bhom/generated/` and is
checked against the pinned source lock in CI. Do not hand-edit those files;
regenerate them from the locked schemas and commit the resulting diff.

```sh
python3 tools/bhom_proto_generator.py check-committed \
  --fetch-locked \
  --entry Structure_oM/Elements/Bar.json \
  --lock tools/bhom/schema.lock.json \
  --catalog tools/bhom/projection_catalog.json \
  --registry-root proto
```

To fetch only the locked first-slice schemas into a local cache:

```sh
python3 tools/bhom_proto_generator.py fetch-locked \
  --lock tools/bhom/schema.lock.json \
  --schema-root /tmp/bhom-jsonschema-locked
```
