"""Compile authoring inputs; this command never starts a Workflow or Worker."""
import argparse
import json
import os
from pathlib import Path
import sys

from .configuration import load_configuration
from .errors import ConfigError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clinical", type=Path)
    for name in ("site", "safety", "deployment", "metadata", "installations"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--installation-root", type=Path)
    parser.add_argument("--validation-only", action="store_true",
                        help="Allow known unimplemented bindings, reported as blockers")
    parser.add_argument("--output-dir", type=Path, help="New private directory; existing paths are never overwritten")
    args = parser.parse_args(argv)
    options = {name + "_path": getattr(args, name) for name in ("site", "safety", "deployment", "metadata")}
    options.update(installations=args.installations, validation_only=args.validation_only)
    if args.installation_root:
        options["installation_root"] = args.installation_root
    try:
        result = load_configuration(args.clinical, **options)
        if "public_definition" not in result:
            raise ConfigError("CLI_FORMAT_UNSUPPORTED", "$", "This compilation command requires chain-clinical/v1")
        manifest = result["manifest"]
        if args.output_dir:
            try:
                args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
                for filename, key in (("definition.json", "public_definition"), ("configuration-manifest.json", "manifest"),
                                      ("worker-private.json", "worker"), ("artifact-evidence-private.json", "artifact_evidence")):
                    target = args.output_dir / filename
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w", encoding="utf8") as stream:
                        json.dump(result[key], stream, ensure_ascii=False, indent=2, sort_keys=True)
                        stream.write("\n")
            except OSError as error:
                raise ConfigError("OUTPUT_WRITE_FAILED", "output_dir", "A new private output directory is required") from error
        print(json.dumps({"status": "CONFIGURATION_VALIDATED", "scope": "CONFIGURATION_INSPECTION",
                          "bindings_validated": manifest["bindings_validated"],
                          "configuration_hash": manifest["configuration_hash"],
                          "binding_blockers": manifest["binding_blockers"],
                          "execution_entrypoint": "chain.client.start_episode"}, ensure_ascii=False, indent=2))
        return 0
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
