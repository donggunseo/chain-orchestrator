"""Start a configured Worker with application-owned private service clients.

The operator supplies a trusted Python services factory. It is process bootstrap
code, never a function selected by an Episode, Signal, or clinical YAML.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import re
from pathlib import Path

from temporalio.client import Client

from chain import __version__
from chain.configuration import load_configuration
from chain.execution import queue_for_configuration
from chain.worker import create_worker


def load_services(reference, *, configuration, installation_root):
    """Call a synchronous factory(configuration=..., installation_root=...)."""
    if not isinstance(reference, str) or not re.fullmatch(
            r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", reference):
        raise ValueError("SERVICES_FACTORY_REFERENCE_INVALID")
    module_name, function_name = reference.split(":")
    try:
        factory = getattr(importlib.import_module(module_name), function_name)
        services = factory(configuration=configuration, installation_root=Path(installation_root).resolve())
    except Exception:
        # Factory errors may contain credentials or patient data.
        raise ValueError("SERVICES_FACTORY_FAILED") from None
    if not isinstance(services, dict):
        raise ValueError("SERVICES_FACTORY_RESULT_INVALID")
    return services


async def serve(args):
    root = Path(args.installation_root).resolve()
    configuration = load_configuration(args.clinical, site_path=args.site, safety_path=args.safety,
        deployment_path=args.deployment, metadata_path=args.metadata,
        installations=args.installed, installation_root=root)
    services = load_services(args.services_factory, configuration=configuration, installation_root=root)
    client = await Client.connect(args.address, namespace=args.namespace)
    worker = create_worker(client, configuration, installation_root=root, services=services)
    print("CHAIN " + __version__ + " Worker ready / " + queue_for_configuration(configuration), flush=True)
    await worker.run()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("clinical", "site", "safety", "deployment", "metadata", "installed", "installation-root"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--services-factory", required=True, help="Trusted application module:function")
    parser.add_argument("--address", default="localhost:7233")
    parser.add_argument("--namespace", default="default")
    args = parser.parse_args(argv)
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
