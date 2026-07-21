import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
GENERATOR = ROOT / "tools" / "bhom_proto_generator.py"
SCHEMA_ROOT = ROOT / "tools" / "bhom" / "tests" / "fixtures" / "schemas"
LOCK = ROOT / "tools" / "bhom" / "tests" / "fixtures" / "schema.lock.json"
CATALOG = ROOT / "tools" / "bhom" / "projection_catalog.json"
GOLDEN = ROOT / "tools" / "bhom" / "tests" / "golden" / "bar_structure.proto"
FLOOR_GOLDEN = ROOT / "tools" / "bhom" / "tests" / "golden" / "floor_physical.proto"
BAR_FLOOR_MANIFEST_GOLDEN = ROOT / "tools" / "bhom" / "tests" / "golden" / "bar_floor_manifest.json"
FLOOR_FIXTURE = ROOT / "tools" / "bhom" / "tests" / "fixtures" / "floor_polygon.json"
FLOOR_NURBS_FIXTURE = ROOT / "tools" / "bhom" / "tests" / "fixtures" / "floor_nurbs_surface.json"


def entry_args(entries):
    args = []
    for entry in entries:
        args.extend(["--entry", entry])
    return args


def run_generator(*extra_args, schema_root=SCHEMA_ROOT, lock=LOCK, entries=None):
    entries = entries or ["Structure_oM/Elements/Bar.json"]
    return subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "generate",
            "--schema-root",
            str(schema_root),
            *entry_args(entries),
            "--lock",
            str(lock),
            "--catalog",
            str(CATALOG),
            *extra_args,
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )


def run_check(*extra_args, schema_root=SCHEMA_ROOT, lock=LOCK, entries=None):
    entries = entries or ["Structure_oM/Elements/Bar.json"]
    return subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "check",
            "--schema-root",
            str(schema_root),
            *entry_args(entries),
            "--lock",
            str(lock),
            "--catalog",
            str(CATALOG),
            *extra_args,
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )


def run_committed_check(*extra_args, schema_root=SCHEMA_ROOT, lock=LOCK, entries=None, registry_root):
    entries = entries or ["Structure_oM/Elements/Bar.json"]
    return subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "check-committed",
            "--schema-root",
            str(schema_root),
            *entry_args(entries),
            "--lock",
            str(lock),
            "--catalog",
            str(CATALOG),
            "--registry-root",
            str(registry_root),
            *extra_args,
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )


def run_fixture_check(fixture):
    return subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "check-fixture",
            "--fixture",
            str(fixture),
            "--catalog",
            str(CATALOG),
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )


class BHoMProtoGeneratorTests(unittest.TestCase):
    def test_dry_run_matches_golden_deterministically(self):
        first = run_generator("--dry-run")
        second = run_generator("--dry-run")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.stdout, GOLDEN.read_text(encoding="utf-8"))
        self.assertIn("package thirdparty.bhom.generated.structure.v1;", first.stdout)
        self.assertIn("package thirdparty.bhom.generated.spatial.v1;", first.stdout)
        self.assertIn("motif.data.geometry.v1.Point3 position", first.stdout)
        self.assertIn("google.protobuf.Value section_property", first.stdout)
        self.assertIn("message RectangleProfile", first.stdout)
        self.assertIn("message CircleProfile", first.stdout)
        self.assertIn("message TubeProfile", first.stdout)
        self.assertIn("message BoxProfile", first.stdout)
        self.assertIn("message AngleProfile", first.stdout)
        self.assertIn("repeated google.protobuf.Value edges", first.stdout)
        self.assertNotIn("package thirdparty.bhom.generated.physical.v1;", first.stdout)
        self.assertNotIn("component =", first.stdout)

    def test_floor_only_output_is_deterministic(self):
        entries = ["Physical_oM/Elements/Floor.json"]
        first = run_generator("--dry-run", entries=entries)
        second = run_generator("--dry-run", entries=entries)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.stdout, FLOOR_GOLDEN.read_text(encoding="utf-8"))
        self.assertIn("package thirdparty.bhom.generated.physical.v1;", first.stdout)
        self.assertIn("thirdparty.bhom.generated.geometry.v1.PlanarSurface location", first.stdout)
        self.assertIn("oneof external_boundary", first.stdout)
        self.assertNotIn("component =", first.stdout)

    def test_floor_check_verifies_locked_output_hashes(self):
        entries = ["Physical_oM/Elements/Floor.json"]
        result = run_check("--skip-buf", entries=entries)

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(
            report["outputs"],
            [
                {
                    "content_sha256": "bfd4c4f8e196eeee7bdd8fe00255f9cd397cfa9c35242516867f605a42528e31",
                    "path": "thirdparty/bhom/generated/geometry/v1/geometry.proto",
                },
                {
                    "content_sha256": "ebfc11c4d25cc5540c8f1a1769549c99b1cfd3431f7a300ceb59557261e9fba7",
                    "path": "thirdparty/bhom/generated/physical/v1/physical.proto",
                },
            ],
        )

    def test_check_verifies_manifest_hash(self):
        result = run_check("--skip-buf")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(
            [output["path"] for output in report["outputs"]],
            [
                "thirdparty/bhom/generated/spatial/v1/spatial.proto",
                "thirdparty/bhom/generated/structure/v1/structure.proto",
            ],
        )
        self.assertEqual(
            report["outputs"][0]["content_sha256"],
            "e11ae3c40980499ea5c7b29c106c4e33fbb4f9eece72b2f504f7836edbcd0532",
        )

    def test_check_committed_detects_registry_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_root = Path(tmp) / "proto"
            generated = run_generator("--out", str(registry_root))
            self.assertEqual(generated.returncode, 0, generated.stderr)

            matching = run_committed_check(registry_root=registry_root)
            self.assertEqual(matching.returncode, 0, matching.stderr)
            self.assertEqual(json.loads(matching.stdout)["status"], "passed")

            spatial = registry_root / "thirdparty/bhom/generated/spatial/v1/spatial.proto"
            spatial.write_text(spatial.read_text(encoding="utf-8") + "// drift\n", encoding="utf-8")
            drifted = run_committed_check(registry_root=registry_root)

        self.assertEqual(drifted.returncode, 2)
        self.assertIn("committed generated output differs", drifted.stderr)

    def test_batch_produces_domain_bundles_and_combined_manifest(self):
        entries = ["Structure_oM/Elements/Bar.json", "Physical_oM/Elements/Floor.json"]
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.json"
            result = run_generator("--dry-run", "--manifest", str(manifest), entries=entries)

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(manifest.read_text(encoding="utf-8"))

        self.assertEqual(report, json.loads(BAR_FLOOR_MANIFEST_GOLDEN.read_text(encoding="utf-8")))
        output_paths = [item["path"] for item in report["outputs"]]
        self.assertEqual(
            output_paths,
            [
                "thirdparty/bhom/generated/geometry/v1/geometry.proto",
                "thirdparty/bhom/generated/physical/v1/physical.proto",
                "thirdparty/bhom/generated/spatial/v1/spatial.proto",
                "thirdparty/bhom/generated/structure/v1/structure.proto",
            ],
        )
        root_paths = [item["entry_schema_path"] for item in report["roots"]]
        self.assertEqual(root_paths, ["Physical_oM/Elements/Floor.json", "Structure_oM/Elements/Bar.json"])
        self.assertIn("package thirdparty.bhom.generated.structure.v1;", result.stdout)
        self.assertIn("package thirdparty.bhom.generated.physical.v1;", result.stdout)

        unsupported_by_root = {
            item["entry_schema_path"]: "\n".join(item["unsupported"])
            for item in report["roots"]
        }
        self.assertIn("Floor non-planar ISurface", unsupported_by_root["Physical_oM/Elements/Floor.json"])
        self.assertIn("Floor openings", unsupported_by_root["Physical_oM/Elements/Floor.json"])
        self.assertIn(
            "Structure_oM/SectionProperties/ISectionProperty.json",
            unsupported_by_root["Structure_oM/Elements/Bar.json"],
        )
        self.assertIn("Bar Box hollow/radii projection", unsupported_by_root["Structure_oM/Elements/Bar.json"])
        self.assertIn("Bar Angle root/toe radii projection", unsupported_by_root["Structure_oM/Elements/Bar.json"])
        self.assertIn("Bar unsupported profile types", unsupported_by_root["Structure_oM/Elements/Bar.json"])
        self.assertIn("Bar raw profile Edges", unsupported_by_root["Structure_oM/Elements/Bar.json"])
        self.assertNotIn("NurbsSurface", unsupported_by_root["Structure_oM/Elements/Bar.json"])

    def test_batch_root_order_does_not_change_output_or_manifest_order(self):
        entries = ["Structure_oM/Elements/Bar.json", "Physical_oM/Elements/Floor.json"]
        reversed_entries = list(reversed(entries))
        with tempfile.TemporaryDirectory() as tmp:
            first_manifest = Path(tmp) / "first.json"
            second_manifest = Path(tmp) / "second.json"
            first = run_generator("--dry-run", "--manifest", str(first_manifest), entries=entries)
            second = run_generator("--dry-run", "--manifest", str(second_manifest), entries=reversed_entries)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(first.stdout, second.stdout)
            self.assertEqual(
                json.loads(first_manifest.read_text(encoding="utf-8")),
                json.loads(second_manifest.read_text(encoding="utf-8")),
            )

    def test_floor_fixture_support_boundary_rejects_nurbs_surface(self):
        supported = run_fixture_check(FLOOR_FIXTURE)
        unsupported = run_fixture_check(FLOOR_NURBS_FIXTURE)

        self.assertEqual(supported.returncode, 0, supported.stderr)
        self.assertEqual(json.loads(supported.stdout)["status"], "supported")
        self.assertEqual(unsupported.returncode, 2)
        report = json.loads(unsupported.stdout)
        self.assertEqual(report["status"], "unsupported")
        self.assertIn("BH.oM.Geometry.NurbsSurface", "\n".join(report["unsupported"]))

    def test_schema_drift_fails_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp) / "schemas"
            shutil.copytree(SCHEMA_ROOT, tmp_root)
            bar_path = tmp_root / "Structure_oM" / "Elements" / "Bar.json"
            data = json.loads(bar_path.read_text(encoding="utf-8"))
            data["properties"]["Unexpected"] = {"type": "string"}
            bar_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

            result = run_generator("--dry-run", schema_root=tmp_root)

        self.assertEqual(result.returncode, 2)
        self.assertIn("schema drift for Structure_oM/Elements/Bar.json", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
