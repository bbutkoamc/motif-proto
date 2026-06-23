# motif-protobuf

Protobuf schemas for the Motif platform.

## Layout

Everything lives in a single [buf v2 module](https://buf.build/docs/configuration/v2/buf-yaml)
rooted at `proto/`. Files live in the directory that mirrors their package name,
relative to that root — e.g. package `motif.extension.v1` is at
`proto/motif/extension/v1/`, and `motif.data.ai.v1` is at
`proto/motif/data/ai/v1/`.

One module (rather than one per package) keeps the schemas on a single buf
repository. Add a new package by creating its package-mirrored directory under
`proto/` (e.g. `proto/motif/data/<area>/v1/`) — no `buf.yaml` change is
required.

## Custom options

Motif's custom protobuf options use a **single extension number per extendee**,
pointing at a wrapper message. New options are added as *fields* on the wrapper
message, so a new extension number is never required.

Today only `MessageOptions` is extended:

```proto
// motif.extension.v1
extend google.protobuf.MessageOptions {
  optional MotifMessageOptions message = 50000;
}
```

`50000` is a temporary internal-range placeholder, to be replaced with a single
public extension number assigned by the protobuf maintainers. Because extension
numbers are scoped per extendee, the same number can be reused if another
descriptor type (e.g. `FieldOptions`) ever needs its own wrapper.

Usage:

```proto
// A component (entity) type.
message User {
  option (motif.extension.v1.message).component = 86;
}

// A relationship type. Endpoints are fields of type EntityReference, identified
// structurally by their type — no field option needed. A relationship has at
// least one EntityReference, and at most one ModelReference (present when its
// endpoints resolve against an external model).
message AssignedTo {
  option (motif.extension.v1.message).relationship = 42;

  // The entity this relationship points to.
  EntityReference assignee = 1;

  // Optional. When set, the assignee resolves against this model rather than the
  // relationship's own model.
  ModelReference model = 2;
}
```

## Code generation

`buf.gen.yaml` generates bindings into `gen/` (gitignored, not committed).
Wiring those bindings into a consuming project — and consuming the
options-bearing `FileDescriptorSet` (`gen/descriptor.fdset`) via reflection — is
handled per-consumer.
