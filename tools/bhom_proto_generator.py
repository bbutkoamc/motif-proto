#!/usr/bin/env python3
"""Small BHoM JSON Schema to proto scaffold.

This is intentionally a first-slice generator: it locks schema inputs, crawls
only the supported Bar closure, maps canonical geometry to Motif types, and
records unsupported polymorphism instead of inferring Motif ECS semantics.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse


GENERATOR_NAME = "motif-bhom-proto-generator"
GENERATOR_VERSION = "0.1.0"
RAW_HOST = "raw.githubusercontent.com"
UPSTREAM_OWNER_REPO = ("BHoM", "BHoM_JSONSchema")


class GeneratorError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class SchemaDoc:
    path: str
    schema: Dict[str, Any]
    sha256: str
    schema_id: str

    @property
    def title(self) -> str:
        return str(self.schema.get("title") or Path(self.path).stem)

    @property
    def bhom_type(self) -> str:
        prop_type = self.schema.get("properties", {}).get("_t", {})
        const = prop_type.get("const")
        if isinstance(const, str):
            return const
        return path_to_bhom_type(self.path)

    @property
    def package(self) -> str:
        return package_for_bhom_type(self.bhom_type)

    @property
    def proto_name(self) -> str:
        return sanitize_identifier(self.bhom_type.rsplit(".", 1)[-1], upper=True)

    @property
    def enum_values(self) -> List[str]:
        return enum_values(self.schema)

    @property
    def is_enum(self) -> bool:
        return bool(self.enum_values)

    @property
    def is_object(self) -> bool:
        return isinstance(self.schema.get("properties"), dict) and not self.is_enum


@dataclasses.dataclass
class FieldSpec:
    name: str
    proto_type: str
    repeated: bool = False
    import_path: Optional[str] = None
    unsupported: Optional[str] = None
    oneof_options: Optional[List["OneofOption"]] = None


@dataclasses.dataclass(frozen=True)
class OneofOption:
    name: str
    proto_type: str
    import_path: Optional[str] = None


def read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        raise GeneratorError(f"missing schema file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GeneratorError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GeneratorError(f"schema must be a JSON object: {path}")
    return data


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_lock(path: Path) -> Dict[str, Any]:
    lock = read_json(path)
    source = lock.get("source", {})
    if not source.get("repository_url") or not source.get("commit"):
        raise GeneratorError("lock file must pin source.repository_url and source.commit")
    generator = lock.get("generator", {})
    if generator.get("name") != GENERATOR_NAME or generator.get("version") != GENERATOR_VERSION:
        raise GeneratorError(
            "lock file generator does not match "
            f"{GENERATOR_NAME} {GENERATOR_VERSION}: {generator}"
        )
    return lock


def load_catalog(path: Path) -> Dict[str, Any]:
    catalog = read_json(path)
    if catalog.get("version") != 1:
        raise GeneratorError("projection catalog version must be 1")
    return catalog


def locked_schemas(lock: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    schemas = lock.get("schemas", [])
    if not isinstance(schemas, list):
        raise GeneratorError("lock schemas must be a list")
    result: Dict[str, Dict[str, Any]] = {}
    for schema in schemas:
        result[schema["path"]] = schema
    return result


def load_doc(schema_root: Path, rel_path: str, lock_index: Dict[str, Dict[str, Any]]) -> SchemaDoc:
    path = schema_root / rel_path
    schema = read_json(path)
    digest = sha256_file(path)
    schema_id = str(schema.get("$id") or "")
    locked = lock_index.get(rel_path)
    if locked:
        if schema_id != locked.get("id"):
            raise GeneratorError(
                f"schema drift for {rel_path}: locked $id {locked.get('id')!r}, found {schema_id!r}"
            )
        if digest != locked.get("sha256"):
            raise GeneratorError(
                f"schema drift for {rel_path}: locked sha256 {locked.get('sha256')}, found {digest}"
            )
    return SchemaDoc(path=rel_path, schema=schema, sha256=digest, schema_id=schema_id)


def resolve_ref_path(ref: str) -> str:
    ref_without_fragment = ref.split("#", 1)[0]
    parsed = urlparse(ref_without_fragment)
    if parsed.scheme in ("http", "https"):
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.netloc != RAW_HOST or len(parts) < 4:
            raise GeneratorError(f"unsupported external $ref: {ref}")
        owner, repo, _revision = parts[:3]
        if (owner, repo) != UPSTREAM_OWNER_REPO:
            raise GeneratorError(f"unsupported upstream $ref repository: {ref}")
        return "/".join(parts[3:])
    return ref_without_fragment.lstrip("/")


def iter_refs(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str):
            yield ref
        for child in value.values():
            yield from iter_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_refs(child)


def iter_doc_refs(doc: SchemaDoc, catalog: Dict[str, Any]) -> Iterable[str]:
    """Yield refs reachable under the catalog's declared field support.

    Preserved JSON fields intentionally do not expand an upstream polymorphic
    branch. Explicit message and oneof overrides contribute their selected
    refs, which makes a declared root closure independent of incidental
    interface traversal.
    """
    properties = doc.schema.get("properties")
    if not isinstance(properties, dict):
        yield from iter_refs(doc.schema)
        return

    field_overrides: Dict[str, Any] = catalog.get("field_projection_overrides", {})
    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            continue
        override = field_overrides.get(f"{doc.path}.{prop_name}")
        if override:
            kind = override.get("kind")
            if kind == "message_ref":
                yield str(override["ref"])
                continue
            if kind == "oneof_refs":
                yield from (str(ref) for ref in override.get("refs", []))
                continue
            if kind == "preserved_json":
                continue
        yield from iter_refs(prop_schema)


def crawl_supported(
    schema_root: Path,
    entry: str,
    lock: Dict[str, Any],
    catalog: Dict[str, Any],
) -> Tuple[Dict[str, SchemaDoc], List[Dict[str, str]]]:
    supported: Set[str] = set(catalog.get("supported_schema_paths", []))
    supported.update(catalog.get("interface_schema_paths", []))
    native_mappings: Dict[str, Any] = catalog.get("native_type_mappings", {})
    lock_index = locked_schemas(lock)
    docs: Dict[str, SchemaDoc] = {}
    unsupported: List[Dict[str, str]] = []
    root_closure = catalog.get("root_supported_schema_paths", {})
    additional_paths = root_closure.get(entry, [])
    if not isinstance(additional_paths, list) or not all(
        isinstance(path, str) for path in additional_paths
    ):
        raise GeneratorError(f"root supported schema paths for {entry} must be a list of paths")
    # A root may declare a bounded, source-fidelity companion closure that is
    # not directly reachable through a polymorphic JSON Schema interface. Keep
    # that closure catalog-owned and sorted so a batch root is reproducible.
    queue: List[str] = [entry, *sorted(set(additional_paths))]

    while queue:
        rel_path = queue.pop(0)
        if rel_path in docs:
            continue
        doc = load_doc(schema_root, rel_path, lock_index)
        docs[rel_path] = doc

        for ref in sorted(set(iter_doc_refs(doc, catalog))):
            ref_path = resolve_ref_path(ref)
            if ref_path in native_mappings:
                if ref_path not in docs:
                    queue.append(ref_path)
                continue
            if ref_path in supported:
                if ref_path not in docs:
                    queue.append(ref_path)
                continue
            unsupported.append(
                {
                    "from_schema": rel_path,
                    "ref": ref_path,
                    "reason": "ref outside first-slice supported schema closure",
                }
            )

    return docs, sorted(unsupported, key=lambda item: (item["from_schema"], item["ref"]))


def canonical_entries(entries: Sequence[str]) -> List[str]:
    return sorted(dict.fromkeys(entries))


def crawl_batch(
    schema_root: Path,
    entries: Sequence[str],
    lock: Dict[str, Any],
    catalog: Dict[str, Any],
) -> Tuple[Dict[str, SchemaDoc], Dict[str, Set[str]], Dict[str, List[str]]]:
    docs: Dict[str, SchemaDoc] = {}
    docs_by_root: Dict[str, Set[str]] = {}
    unsupported_by_root: Dict[str, List[str]] = {}
    for entry in canonical_entries(entries):
        entry_docs, entry_unsupported = crawl_supported(schema_root, entry, lock, catalog)
        docs.update(entry_docs)
        docs_by_root[entry] = set(entry_docs)
        unsupported_by_root[entry] = [
            f"{item['from_schema']} -> {item['ref']}: {item['reason']}"
            for item in entry_unsupported
        ]
    return docs, docs_by_root, unsupported_by_root


def path_to_bhom_type(path: str) -> str:
    parts = Path(path).with_suffix("").parts
    if not parts:
        raise GeneratorError(f"cannot derive BHoM type from path: {path}")
    domain = parts[0].replace("_oM", "")
    tail = list(parts[1:])
    if domain == "BHoM":
        return "BH.oM.Base." + ".".join(tail)
    return "BH.oM." + ".".join([domain] + tail)


def package_for_bhom_type(bhom_type: str) -> str:
    parts = bhom_type.split(".")
    if len(parts) < 4 or parts[:2] != ["BH", "oM"]:
        raise GeneratorError(f"cannot map BHoM type to package: {bhom_type}")
    domain = re.sub(r"[^a-z0-9]+", "_", parts[2].lower()).strip("_")
    return f"thirdparty.bhom.data.{domain}.v1"


def generated_package_for_doc(doc: SchemaDoc, catalog: Dict[str, Any]) -> str:
    base_package = doc.package
    ownership = catalog.get("ownership_model", {})
    generated_prefix = ownership.get("generated_package_prefix", "thirdparty.bhom.generated")
    parts = base_package.split(".")
    if parts[:3] != ["thirdparty", "bhom", "data"] or parts[-1] != "v1":
        raise GeneratorError(f"unsupported generated base package: {base_package}")
    return ".".join([generated_prefix, parts[3], "v1"])


def generated_type_for_ref(
    ref_path: str,
    current_doc: SchemaDoc,
    docs: Dict[str, SchemaDoc],
    catalog: Dict[str, Any],
) -> Tuple[str, Optional[str]]:
    ref_doc = docs.get(ref_path)
    if not ref_doc:
        raise GeneratorError(f"projection override references unloaded schema: {ref_path}")
    current_package = generated_package_for_doc(current_doc, catalog)
    ref_package = generated_package_for_doc(ref_doc, catalog)
    if current_package == ref_package:
        return ref_doc.proto_name, None
    return f"{ref_package}.{ref_doc.proto_name}", package_output_path(ref_package, catalog)


def package_output_path(package: str, catalog: Dict[str, Any]) -> str:
    parts = package.split(".")
    ownership = catalog.get("ownership_model", {})
    generated_prefix = ownership.get("generated_package_prefix", "thirdparty.bhom.generated").split(".")
    output_root = ownership.get("generated_output_root", "thirdparty/bhom/generated").strip("/")
    if parts[: len(generated_prefix)] != generated_prefix or parts[-1] != "v1":
        raise GeneratorError(f"unsupported output package: {package}")
    domain = parts[len(generated_prefix)]
    return f"{output_root}/{domain}/v1/{domain}.proto"


def sanitize_identifier(value: str, upper: bool = False) -> str:
    pieces = re.split(r"[^A-Za-z0-9]+", value)
    cleaned = "".join(piece[:1].upper() + piece[1:] for piece in pieces if piece)
    if not cleaned:
        raise GeneratorError(f"cannot sanitize identifier: {value!r}")
    if cleaned[0].isdigit():
        cleaned = "_" + cleaned
    return cleaned if upper else cleaned[:1].lower() + cleaned[1:]


def proto_field_name(name: str) -> str:
    special = {
        "_t": "bhom_type",
        "_bhomVersion": "bhom_version",
        "BHoM_Guid": "bhom_guid",
    }
    if name in special:
        return special[name]
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return value or name.lower()


def enum_values(schema: Dict[str, Any]) -> List[str]:
    candidates: Sequence[Any] = []
    if isinstance(schema.get("enum"), list):
        candidates = schema["enum"]
    else:
        for key in ("anyOf", "oneOf"):
            for option in schema.get(key, []) or []:
                if isinstance(option, dict) and isinstance(option.get("enum"), list):
                    candidates = option["enum"]
                    break
            if candidates:
                break
    return [str(item) for item in candidates]


def enum_value_name(enum_name: str, value: str) -> str:
    prefix = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", enum_name)
    prefix = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", prefix).upper()
    body = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    body = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", body).upper()
    body = re.sub(r"[^A-Z0-9]+", "_", body).strip("_")
    return f"{prefix}_{body}"


def property_type(
    doc: SchemaDoc,
    prop_name: str,
    prop_schema: Dict[str, Any],
    docs: Dict[str, SchemaDoc],
    catalog: Dict[str, Any],
) -> FieldSpec:
    native_mappings: Dict[str, Any] = catalog.get("native_type_mappings", {})
    field_overrides: Dict[str, Any] = catalog.get("field_projection_overrides", {})
    field_name = proto_field_name(prop_name)
    override = field_overrides.get(f"{doc.path}.{prop_name}")

    if override:
        kind = override.get("kind")
        if kind == "message_ref":
            proto_type, import_path = generated_type_for_ref(str(override["ref"]), doc, docs, catalog)
            return FieldSpec(field_name, proto_type, import_path=import_path)
        if kind == "oneof_refs":
            options: List[OneofOption] = []
            option_names = override.get("field_names", {})
            for ref_path in override.get("refs", []):
                proto_type, import_path = generated_type_for_ref(str(ref_path), doc, docs, catalog)
                ref_doc = docs[str(ref_path)]
                option_name = option_names.get(ref_path) or f"{field_name}_{proto_field_name(ref_doc.proto_name)}"
                options.append(OneofOption(option_name, proto_type, import_path))
            return FieldSpec(field_name, "", oneof_options=options)
        if kind == "preserved_json":
            return FieldSpec(
                field_name,
                "google.protobuf.Value",
                repeated=bool(override.get("repeated")),
                import_path="google/protobuf/struct.proto",
                unsupported=str(override.get("reason", "Unsupported BHoM branch; preserved as JSON value.")),
            )
        raise GeneratorError(f"unknown field projection override kind {kind!r} for {doc.path}.{prop_name}")

    if "$ref" in prop_schema:
        ref_path = resolve_ref_path(str(prop_schema["$ref"]))
        if ref_path in native_mappings:
            mapping = native_mappings[ref_path]
            return FieldSpec(field_name, mapping["proto_type"], import_path=mapping.get("import"))
        if ref_path in docs and docs[ref_path].is_enum:
            return FieldSpec(field_name, docs[ref_path].proto_name)
        if ref_path in docs and docs[ref_path].is_object:
            proto_type, import_path = generated_type_for_ref(ref_path, doc, docs, catalog)
            return FieldSpec(field_name, proto_type, import_path=import_path)
        return FieldSpec(
            field_name,
            "google.protobuf.Value",
            import_path="google/protobuf/struct.proto",
            unsupported=f"Unsupported BHoM ref {ref_path}; preserved as JSON value.",
        )

    schema_type = prop_schema.get("type")
    types = [schema_type] if isinstance(schema_type, str) else list(schema_type or [])
    non_null_types = [item for item in types if item != "null"]
    scalar_type = non_null_types[0] if non_null_types else None

    if scalar_type == "array":
        item_schema = prop_schema.get("items", {})
        item = property_type(doc, prop_name, item_schema, docs, catalog)
        item.name = field_name
        item.repeated = True
        return item
    if scalar_type == "object":
        wrapped_values = prop_schema.get("properties", {}).get("_v")
        if isinstance(wrapped_values, dict):
            unwrapped = property_type(doc, prop_name, wrapped_values, docs, catalog)
            unwrapped.name = field_name
            return unwrapped
        return FieldSpec(field_name, "google.protobuf.Struct", import_path="google/protobuf/struct.proto")
    if scalar_type == "number":
        return FieldSpec(field_name, "double")
    if scalar_type == "integer":
        return FieldSpec(field_name, "int32")
    if scalar_type == "boolean":
        return FieldSpec(field_name, "bool")
    if scalar_type == "string":
        return FieldSpec(field_name, "string")
    if "oneOf" in prop_schema or "anyOf" in prop_schema:
        return FieldSpec(
            field_name,
            "google.protobuf.Value",
            import_path="google/protobuf/struct.proto",
            unsupported="Unsupported JSON Schema union; preserved as JSON value.",
        )

    return FieldSpec(
        field_name,
        "google.protobuf.Value",
        import_path="google/protobuf/struct.proto",
        unsupported="Unsupported JSON Schema field shape; preserved as JSON value.",
    )


def render_enum(doc: SchemaDoc) -> str:
    lines = [f"enum {doc.proto_name} {{", f"  {enum_value_name(doc.proto_name, 'UNSPECIFIED')} = 0;"]
    for index, value in enumerate(doc.enum_values, start=1):
        lines.append(f"  {enum_value_name(doc.proto_name, value)} = {index};")
    lines.append("}")
    return "\n".join(lines)


def render_message(doc: SchemaDoc, docs: Dict[str, SchemaDoc], catalog: Dict[str, Any]) -> Tuple[str, Set[str], List[str]]:
    imports: Set[str] = set()
    unsupported_lines: List[str] = []
    lines = [
        f"// BHoM source type: {doc.bhom_type}",
        f"// BHoM schema: {doc.path}",
        f"message {doc.proto_name} {{",
    ]
    properties = doc.schema.get("properties", {})
    field_number = 1
    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            raise GeneratorError(f"property {doc.path}.{prop_name} must be an object")
        field = property_type(doc, prop_name, prop_schema, docs, catalog)
        if field.import_path:
            imports.add(field.import_path)
        if field.oneof_options:
            lines.append(f"  oneof {field.name} {{")
            for option in field.oneof_options:
                if option.import_path:
                    imports.add(option.import_path)
                lines.append(f"    {option.proto_type} {option.name} = {field_number};")
                field_number += 1
            lines.append("  }")
            continue
        if field.unsupported:
            lines.append(f"  // {field.unsupported}")
            unsupported_lines.append(f"{doc.proto_name}.{field.name}: {field.unsupported}")
        elif prop_name == "_t":
            lines.append("  // BHoM concrete type discriminator.")
        elif prop_name == "_bhomVersion":
            lines.append("  // Source BHoM version metadata.")
        repeated = "repeated " if field.repeated else ""
        lines.append(f"  {repeated}{field.proto_type} {field.name} = {field_number};")
        field_number += 1
    lines.append("}")
    return "\n".join(lines), imports, unsupported_lines


def render_proto(
    entries: Sequence[str],
    docs: Dict[str, SchemaDoc],
    docs_by_root: Dict[str, Set[str]],
    catalog: Dict[str, Any],
) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    native_paths = set((catalog.get("native_type_mappings") or {}).keys())
    interface_paths = set(catalog.get("interface_schema_paths", []))
    roots = canonical_entries(entries)
    generated_docs = [
        doc
        for path, doc in docs.items()
        if path not in native_paths and path not in interface_paths and (doc.is_object or doc.is_enum)
    ]
    doc_packages = {doc.path: generated_package_for_doc(doc, catalog) for doc in generated_docs}
    packages = sorted(set(doc_packages.values()))
    outputs: Dict[str, str] = {}
    render_unsupported_by_root: Dict[str, List[str]] = {entry: [] for entry in roots}

    for package in packages:
        package_docs = sorted(
            [doc for doc in generated_docs if doc_packages[doc.path] == package],
            key=lambda doc: (
                0 if doc.path in roots else 1,
                roots.index(doc.path) if doc.path in roots else len(roots),
                doc.proto_name,
            ),
        )
        imports: Set[str] = set()
        body_parts: List[str] = []
        for doc in package_docs:
            if doc.is_enum:
                body_parts.append(render_enum(doc))
            else:
                rendered, doc_imports, unsupported = render_message(doc, docs, catalog)
                imports.update(doc_imports)
                for entry in roots:
                    if doc.path in docs_by_root.get(entry, set()):
                        render_unsupported_by_root[entry].extend(
                            [f"{doc.path}: {line}" for line in unsupported]
                        )
                body_parts.append(rendered)

        lines = [
            'syntax = "proto3";',
            "",
            "// Generated by motif-bhom-proto-generator 0.1.0; DO NOT EDIT.",
            "// Source schemas are pinned in tools/bhom/schema.lock.json.",
            f"package {package};",
            "",
        ]
        for import_path in sorted(imports):
            lines.append(f'import "{import_path}";')
        if imports:
            lines.append("")
        lines.append("\n\n".join(body_parts))
        lines.append("")
        outputs[package_output_path(package, catalog)] = "\n".join(lines)

    return outputs, {
        entry: sorted(set(lines))
        for entry, lines in render_unsupported_by_root.items()
    }


def write_outputs(out_dir: Path, outputs: Dict[str, str]) -> None:
    for rel_path, content in outputs.items():
        path = out_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def write_manifest(
    path: Path,
    lock: Dict[str, Any],
    entries: Sequence[str],
    outputs: Dict[str, str],
    unsupported_by_root: Dict[str, List[str]],
) -> None:
    roots = canonical_entries(entries)
    manifest = {
        "source": lock["source"],
        "generator": lock["generator"],
        "roots": [
            {
                "entry_schema_path": entry,
                "unsupported": unsupported_by_root.get(entry, []),
            }
            for entry in roots
        ],
        "outputs": [
            {
                "path": rel_path,
                "content_sha256": sha256_text(content),
            }
            for rel_path, content in sorted(outputs.items())
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_outputs(
    schema_root: Path,
    entries: Sequence[str],
    lock_path: Path,
    catalog_path: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, str], Dict[str, List[str]]]:
    lock = load_lock(lock_path)
    catalog = load_catalog(catalog_path)
    roots = canonical_entries(entries)
    docs, docs_by_root, unsupported_by_root = crawl_batch(schema_root, roots, lock, catalog)
    outputs, render_unsupported_by_root = render_proto(roots, docs, docs_by_root, catalog)
    for entry in roots:
        unsupported_by_root[entry] = sorted(
            set(
                unsupported_by_root.get(entry, [])
                + render_unsupported_by_root.get(entry, [])
                + catalog_deferred_branches(catalog, entry)
            )
        )
    return lock, catalog, outputs, unsupported_by_root


def catalog_deferred_branches(catalog: Dict[str, Any], entry: str) -> List[str]:
    deferred = []
    for branch in catalog.get("deferred_branches", []):
        if isinstance(branch, dict):
            applies_to = branch.get("applies_to", [])
            if applies_to and entry not in applies_to:
                continue
            name = branch.get("name", "unnamed")
            reason = branch.get("reason", "deferred")
            deferred.append(f"{name}: {reason}")
        else:
            deferred.append(str(branch))
    return sorted(deferred)


def combined_output(outputs: Dict[str, str]) -> str:
    return "\n".join(outputs[path] for path in sorted(outputs))


def verify_lock_output_hash(lock: Dict[str, Any], entries: Sequence[str], outputs: Dict[str, str]) -> None:
    actual_by_path = {path: sha256_text(content) for path, content in outputs.items()}
    for entry in canonical_entries(entries):
        expected_outputs = [
            output for output in lock.get("outputs", []) if output.get("entry_schema_path") == entry
        ]
        if not expected_outputs:
            raise GeneratorError(f"lock file has no output record for entry schema {entry}")
        for expected in expected_outputs:
            path = expected.get("path")
            expected_hash = expected.get("content_sha256")
            actual_hash = actual_by_path.get(path)
            if actual_hash != expected_hash:
                raise GeneratorError(
                    f"output hash mismatch for {path}: locked {expected_hash}, generated {actual_hash}"
                )


def verify_committed_outputs(registry_root: Path, outputs: Dict[str, str]) -> None:
    differences: List[str] = []
    for rel_path, generated in sorted(outputs.items()):
        committed_path = registry_root / rel_path
        try:
            committed = committed_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            differences.append(f"missing committed generated output: {committed_path}")
            continue
        if committed != generated:
            differences.append(
                "committed generated output differs: "
                f"{committed_path} (committed {sha256_text(committed)}, "
                f"generated {sha256_text(generated)})"
            )
    if differences:
        raise GeneratorError("\n".join(differences))


def command_generate(args: argparse.Namespace) -> int:
    lock, _catalog, outputs, unsupported_by_root = build_outputs(args.schema_root, args.entry, args.lock, args.catalog)

    if args.out:
        write_outputs(args.out, outputs)
    if args.manifest:
        write_manifest(args.manifest, lock, args.entry, outputs, unsupported_by_root)
    if args.check_golden:
        golden = args.check_golden.read_text(encoding="utf-8")
        actual = combined_output(outputs)
        if actual != golden:
            raise GeneratorError(f"generated output differs from golden: {args.check_golden}")
    if args.dry_run or not args.out:
        for index, rel_path in enumerate(sorted(outputs)):
            if len(outputs) > 1:
                if index:
                    print()
                print(f"// FILE: {rel_path}")
            print(outputs[rel_path], end="")
    return 0


def buf_command() -> Optional[List[str]]:
    if shutil.which("mise"):
        result = subprocess.run(
            ["mise", "which", "buf"],
            cwd=str(Path.cwd()),
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return [result.stdout.strip()]
    if shutil.which("buf"):
        return ["buf"]
    return None


def copy_minimal_buf_workspace(workspace: Path, outputs: Dict[str, str]) -> None:
    proto_root = workspace / "proto"
    for rel_path, content in outputs.items():
        out_path = proto_root / rel_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")

    geometry_src = Path("proto/motif/data/geometry/v1/geometry.proto")
    geometry_dst = proto_root / "motif/data/geometry/v1/geometry.proto"
    geometry_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(geometry_src, geometry_dst)

    (workspace / "buf.yaml").write_text(
        "\n".join(
            [
                "version: v2",
                "modules:",
                "  - path: proto",
                "lint:",
                "  use:",
                "    - STANDARD",
                "",
            ]
        ),
        encoding="utf-8",
    )


def run_buf_checks(workspace: Path) -> List[Dict[str, Any]]:
    command = buf_command()
    if command is None:
        return [
            {
                "command": "buf",
                "status": "skipped",
                "reason": "buf and mise were not found on PATH",
            }
        ]

    checks: List[Dict[str, Any]] = []
    commands = (
        ["format", "-d", "--exit-code"],
        ["lint"],
        ["build"],
    )
    for args in commands:
        full_command = command + args
        result = subprocess.run(
            full_command,
            cwd=str(workspace),
            env={**os.environ, "MISE_CACHE_DIR": str(Path(tempfile.gettempdir()) / "motif-bhom-mise-cache")},
            text=True,
            capture_output=True,
            check=False,
        )
        checks.append(
            {
                "command": " ".join(full_command),
                "status": "passed" if result.returncode == 0 else "failed",
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        if result.returncode != 0:
            raise GeneratorError(
                f"Buf validation failed for {' '.join(full_command)}:\n{result.stdout}{result.stderr}"
            )
    return checks


def command_check(args: argparse.Namespace) -> int:
    lock, _catalog, outputs, unsupported_by_root = build_outputs(args.schema_root, args.entry, args.lock, args.catalog)
    verify_lock_output_hash(lock, args.entry, outputs)

    if args.golden:
        golden = args.golden.read_text(encoding="utf-8")
        actual = combined_output(outputs)
        if actual != golden:
            raise GeneratorError(f"generated output differs from golden: {args.golden}")

    with tempfile.TemporaryDirectory(prefix="motif-bhom-check-") as tmp:
        workspace = Path(tmp)
        copy_minimal_buf_workspace(workspace, outputs)
        buf_checks = [] if args.skip_buf else run_buf_checks(workspace)

    report = {
        "status": "passed",
        "source": lock["source"],
        "generator": lock["generator"],
        "outputs": [
            {"path": path, "content_sha256": sha256_text(content)}
            for path, content in sorted(outputs.items())
        ],
        "roots": [
            {
                "entry_schema_path": entry,
                "unsupported": unsupported_by_root.get(entry, []),
            }
            for entry in canonical_entries(args.entry)
        ],
        "buf_checks": buf_checks,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def command_check_committed(args: argparse.Namespace) -> int:
    lock = load_lock(args.lock)

    def check(schema_root: Path) -> Dict[str, Any]:
        _lock, _catalog, outputs, unsupported_by_root = build_outputs(
            schema_root, args.entry, args.lock, args.catalog
        )
        verify_lock_output_hash(lock, args.entry, outputs)
        verify_committed_outputs(args.registry_root, outputs)
        return {
            "status": "passed",
            "source": lock["source"],
            "generator": lock["generator"],
            "registry_root": str(args.registry_root),
            "outputs": [
                {"path": path, "content_sha256": sha256_text(content)}
                for path, content in sorted(outputs.items())
            ],
            "roots": [
                {
                    "entry_schema_path": entry,
                    "unsupported": unsupported_by_root.get(entry, []),
                }
                for entry in canonical_entries(args.entry)
            ],
        }

    if args.fetch_locked:
        with tempfile.TemporaryDirectory(prefix="motif-bhom-locked-") as tmp:
            fetch_locked_schemas(lock, Path(tmp), overwrite=False)
            report = check(Path(tmp))
    else:
        if args.schema_root is None:
            raise GeneratorError("--schema-root is required unless --fetch-locked is set")
        report = check(args.schema_root)

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def point_key(point: Any) -> Optional[Tuple[Any, Any, Any]]:
    if not isinstance(point, dict):
        return None
    return (point.get("X"), point.get("Y"), point.get("Z"))


def command_check_fixture(args: argparse.Namespace) -> int:
    _catalog = load_catalog(args.catalog)
    fixture = read_json(args.fixture)
    unsupported: List[str] = []

    if fixture.get("_t") != "BH.oM.Physical.Elements.Floor":
        unsupported.append(f"fixture root type {fixture.get('_t')!r} is not BH.oM.Physical.Elements.Floor")

    location = fixture.get("Location")
    if not isinstance(location, dict):
        unsupported.append("Floor.Location is missing or not an object")
    elif location.get("_t") != "BH.oM.Geometry.PlanarSurface":
        unsupported.append(
            "unsupported Floor.Location surface type "
            f"{location.get('_t')!r}; supported: BH.oM.Geometry.PlanarSurface"
        )
    else:
        internal = location.get("InternalBoundaries")
        if internal not in (None, []):
            unsupported.append("PlanarSurface.InternalBoundaries are deferred for Floor")

        boundary = location.get("ExternalBoundary")
        if not isinstance(boundary, dict):
            unsupported.append("PlanarSurface.ExternalBoundary is missing or not an object")
        else:
            boundary_type = boundary.get("_t")
            if boundary_type == "BH.oM.Geometry.Polygon":
                vertices = boundary.get("Vertices", {}).get("_v")
                if not isinstance(vertices, list) or len(vertices) < 3:
                    unsupported.append("Polygon ExternalBoundary requires at least three vertices")
            elif boundary_type == "BH.oM.Geometry.Polyline":
                points = boundary.get("ControlPoints")
                if not isinstance(points, list) or len(points) < 4:
                    unsupported.append("Polyline ExternalBoundary requires at least four closed control points")
                elif point_key(points[0]) != point_key(points[-1]):
                    unsupported.append("Polyline ExternalBoundary must be closed")
            else:
                unsupported.append(
                    "unsupported PlanarSurface.ExternalBoundary curve type "
                    f"{boundary_type!r}; supported: BH.oM.Geometry.Polygon, BH.oM.Geometry.Polyline"
                )

    report = {
        "fixture": str(args.fixture),
        "status": "unsupported" if unsupported else "supported",
        "unsupported": unsupported,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 2 if unsupported else 0


def fetch_locked_schemas(lock: Dict[str, Any], schema_root: Path, overwrite: bool) -> None:
    repo = lock["source"]["repository_url"].rstrip("/")
    commit = lock["source"]["commit"]
    if repo != "https://github.com/BHoM/BHoM_JSONSchema":
        raise GeneratorError(f"unsupported source repository URL for fetch: {repo}")
    for schema in lock.get("schemas", []):
        rel_path = schema["path"]
        url = f"https://raw.githubusercontent.com/BHoM/BHoM_JSONSchema/{commit}/{rel_path}"
        out_path = schema_root / rel_path
        if out_path.exists() and not overwrite:
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url) as response:
            out_path.write_bytes(response.read())


def command_fetch_locked(args: argparse.Namespace) -> int:
    fetch_locked_schemas(load_lock(args.lock), args.schema_root, args.overwrite)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate BHoM-shaped proto from locked schemas")
    generate.add_argument("--schema-root", type=Path, required=True)
    generate.add_argument("--entry", action="append", required=True)
    generate.add_argument("--lock", type=Path, required=True)
    generate.add_argument("--catalog", type=Path, required=True)
    generate.add_argument("--dry-run", action="store_true")
    generate.add_argument("--out", type=Path)
    generate.add_argument("--manifest", type=Path)
    generate.add_argument("--check-golden", type=Path)
    generate.set_defaults(func=command_generate)

    check = sub.add_parser("check", help="verify deterministic generation and temp Buf buildability")
    check.add_argument("--schema-root", type=Path, required=True)
    check.add_argument("--entry", action="append", required=True)
    check.add_argument("--lock", type=Path, required=True)
    check.add_argument("--catalog", type=Path, required=True)
    check.add_argument("--golden", type=Path)
    check.add_argument("--skip-buf", action="store_true")
    check.set_defaults(func=command_check)

    committed = sub.add_parser(
        "check-committed",
        help="verify committed generated DTO files match the pinned generator output",
    )
    committed.add_argument("--schema-root", type=Path)
    committed.add_argument("--fetch-locked", action="store_true")
    committed.add_argument("--entry", action="append", required=True)
    committed.add_argument("--lock", type=Path, required=True)
    committed.add_argument("--catalog", type=Path, required=True)
    committed.add_argument("--registry-root", type=Path, default=Path("proto"))
    committed.set_defaults(func=command_check_committed)

    check_fixture = sub.add_parser("check-fixture", help="check a BHoM JSON fixture against supported projection closure")
    check_fixture.add_argument("--fixture", type=Path, required=True)
    check_fixture.add_argument("--catalog", type=Path, required=True)
    check_fixture.set_defaults(func=command_check_fixture)

    fetch = sub.add_parser("fetch-locked", help="fetch locked schemas from the pinned upstream commit")
    fetch.add_argument("--lock", type=Path, required=True)
    fetch.add_argument("--schema-root", type=Path, required=True)
    fetch.add_argument("--overwrite", action="store_true")
    fetch.set_defaults(func=command_fetch_locked)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except GeneratorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
