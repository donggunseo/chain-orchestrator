"""Freeze one inspected native Worker artifact into a new private directory.

Copies versioned Python/Schema and explicitly approved module files, never a
workspace, virtualenv, initial patient record, History, receipt DB or .env tree.
It does not publish, deploy, switch admission, import plugins or start Temporal.
"""
import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))
from chain.configuration import _compiler_pin, load_configuration
from chain.execution import WORKFLOW_TYPE, _validate_configuration, queue_for_configuration

LOADER = '''"""Load a frozen artifact in an isolated Worker process, before project imports."""
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys


def _hash(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf8")).hexdigest()


def load(*, expected_artifact_id=None):
    root = Path(__file__).resolve().parent
    manifest = json.loads((root/"artifact-manifest.json").read_text(encoding="utf8"))
    if manifest.get("artifact_schema") != "chain-fact-artifact/v1":
        raise ValueError("ARTIFACT_SCHEMA_UNSUPPORTED")
    identifier = manifest.get("artifact_id")
    if _hash({k:v for k,v in manifest.items() if k != "artifact_id"}) != identifier:
        raise ValueError("ARTIFACT_MANIFEST_MISMATCH")
    if expected_artifact_id is not None and identifier != expected_artifact_id:
        raise ValueError("ARTIFACT_APPROVAL_MISMATCH")
    for name, digest in manifest["files"].items():
        path = root/name
        if (Path(name).is_absolute() or ".." in Path(name).parts or not path.is_file()
                or not path.resolve().is_relative_to(root) or path.is_symlink()
                or "sha256:"+hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise ValueError("ARTIFACT_FILE_MISMATCH: "+name)
    environment = json.loads((root/"environment.json").read_text(encoding="utf8"))
    if environment["python"] != ".".join(map(str,sys.version_info[:3])):
        raise ValueError("ARTIFACT_PYTHON_MISMATCH")
    for name, expected in environment["packages"].items():
        if version(name) != expected:
            raise ValueError("ARTIFACT_PACKAGE_MISMATCH: "+name)
    for name, module in tuple(sys.modules.items()):
        if name.split(".")[0] in {"chain", "demo"} and getattr(module,"__file__",None):
            if not Path(module.__file__).resolve().is_relative_to(root):
                raise ValueError("ARTIFACT_PROCESS_ISOLATION_REQUIRED")
    sys.path.insert(0,str(root))
    from chain.configuration import _compiler_pin
    from chain.execution import _validate_configuration, queue_for_configuration
    configuration = json.loads((root/"configuration-private.json").read_text(encoding="utf8"))
    _validate_configuration(configuration)
    if configuration["manifest"]["compiler"]["implementation_hash"] != _compiler_pin()[0]:
        raise ValueError("ARTIFACT_IMPLEMENTATION_MISMATCH")
    if (configuration["manifest"]["configuration_hash"] != manifest["configuration_hash"]
            or queue_for_configuration(configuration) != manifest["task_queue"]):
        raise ValueError("ARTIFACT_CONFIGURATION_MISMATCH")
    return configuration, root
'''


def _hash(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf8")).hexdigest()


def freeze(configuration, *, installation_root, output_dir):
    if set(configuration) != {"public_definition", "manifest", "worker", "artifact_evidence"}:
        raise ValueError("ARTIFACT_CONFIGURATION_FIELDS")
    _validate_configuration(configuration)
    if configuration["manifest"]["compiler"]["implementation_hash"] != _compiler_pin()[0]:
        raise ValueError("ARTIFACT_CORE_CHANGED")
    installation_root, output = Path(installation_root).resolve(), Path(output_dir)
    if output.exists():
        raise FileExistsError("ARTIFACT_OUTPUT_EXISTS")
    selected = {}

    def select(name, origin, expected=None):
        relative = Path(name)
        if (relative.is_absolute() or ".." in relative.parts or any(part.startswith(".") for part in relative.parts)
                or relative.suffix.lower() not in {".py", ".json", ".yaml", ".yml", ".txt", ".md"}):
            raise ValueError("ARTIFACT_FILE_NOT_ALLOWED: " + name)
        path = origin / relative
        if not path.is_file() or not path.resolve().is_relative_to(origin) or path.is_symlink():
            raise ValueError("ARTIFACT_FILE_OUTSIDE_INSTALLATION: " + name)
        data = path.read_bytes()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if expected is not None and digest != expected:
            raise ValueError("ARTIFACT_APPROVED_FILE_CHANGED: " + name)
        if name in selected and selected[name] != data:
            raise ValueError("ARTIFACT_FILE_COLLISION: " + name)
        selected[name] = data

    for folder in ("chain",):
        for path in sorted((SOURCE_ROOT / folder).rglob("*.py")):
            select(str(path.relative_to(SOURCE_ROOT)), SOURCE_ROOT)
    for path in sorted((SOURCE_ROOT / "chain/resources").glob("*.json")):
        select(str(path.relative_to(SOURCE_ROOT)), SOURCE_ROOT)
    bindings = configuration["worker"]["bindings"]
    implementations = list(bindings["agents"].values()) + list(bindings["sources"].values()) + list(bindings["effects"].values())
    if bindings["notifier"] is not None:
        implementations.append(bindings["notifier"])
    for binding in implementations:
        for name, expected in binding["descriptor"]["files"].items():
            select(name, installation_root, expected)
    # The packaged synthetic providers use this namespace. Its initializers
    # have no runtime setup; include them explicitly, rather than a demo tree.
    if any(name.startswith("demo/") for name in selected):
        for name in ("demo/__init__.py",):
            select(name, SOURCE_ROOT)
    selected["configuration-private.json"] = (json.dumps(configuration, ensure_ascii=False, sort_keys=True, indent=2,
        allow_nan=False) + "\n").encode("utf8")
    packages = {name: version(name) for name in ("temporalio", "PyYAML", "nexus-rpc", "protobuf", "types-protobuf", "typing-extensions")}
    selected["environment.json"] = (json.dumps({"python": ".".join(map(str, sys.version_info[:3])),
        "packages": packages}, sort_keys=True, indent=2) + "\n").encode()
    selected["requirements-frozen.txt"] = ("\n".join(name + "==" + value for name, value in sorted(packages.items())) + "\n").encode()
    selected["artifact_loader.py"] = LOADER.encode("utf8")
    manifest = {"artifact_schema": "chain-fact-artifact/v1", "workflow_type": WORKFLOW_TYPE,
        "configuration_hash": configuration["manifest"]["configuration_hash"],
        "task_queue": queue_for_configuration(configuration),
        "implementation_hash": configuration["manifest"]["compiler"]["implementation_hash"],
        "admission_switched": False, "services_exported": False,
        "files": {name: "sha256:" + hashlib.sha256(data).hexdigest() for name, data in sorted(selected.items())}}
    manifest["artifact_id"] = _hash(manifest)
    output.mkdir(mode=0o700, parents=True)
    for name, data in selected.items():
        path = output / name
        parent = output
        for component in Path(name).parts[:-1]:
            parent = parent / component
            parent.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    with (output / "artifact-manifest.json").open("x", encoding="utf8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    (output / "artifact-manifest.json").chmod(0o600)
    checked = subprocess.run([sys.executable, "-I", "-B", "-c",
        "import sys; sys.path.insert(0,sys.argv[1]); import artifact_loader; artifact_loader.load(expected_artifact_id=sys.argv[2])",
        str(output.resolve()), manifest["artifact_id"]], cwd=output, capture_output=True, text=True, timeout=30)
    if checked.returncode:
        raise ValueError("ARTIFACT_ISOLATED_VALIDATION_FAILED: " + checked.stderr)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("clinical", "site", "safety", "deployment", "metadata", "installed", "installation-root", "output-dir"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    try:
        configuration = load_configuration(args.clinical, site_path=args.site, safety_path=args.safety,
            deployment_path=args.deployment, metadata_path=args.metadata, installations=args.installed,
            installation_root=args.installation_root)
        manifest = freeze(configuration, installation_root=args.installation_root, output_dir=args.output_dir)
        print(json.dumps({"status": "ARTIFACT_FROZEN", "artifact_id": manifest["artifact_id"],
            "workflow_type": manifest["workflow_type"], "task_queue": manifest["task_queue"],
            "admission_switched": False}, ensure_ascii=False))
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
