"""Distinct native Fact Worker; never registers the v0.3 Workflow or queue."""
import argparse
import asyncio
import json
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import FactActivities
from .configuration import load_configuration
from .execution import queue_for_configuration
from .temporal_workflow import FactEpisodeWorkflow
from .worker_runtime import FactWorkerRuntime


def create_worker(client, configuration, *, installation_root, services=None, provider_factories=None):
    """Bind one installed artifact to its exact queue; no live YAML reload."""
    runtime = FactWorkerRuntime(configuration, installation_root=installation_root,
        services=services, provider_factories=provider_factories)
    activities = FactActivities(runtime)
    return Worker(client, task_queue=queue_for_configuration(configuration),
        workflows=[FactEpisodeWorkflow], activities=activities.registered)


async def serve(args):
    inventory = json.loads(Path(args.installed).read_text(encoding="utf8"))
    configuration = load_configuration(args.clinical, site_path=args.site, safety_path=args.safety,
        deployment_path=args.deployment, metadata_path=args.metadata,
        installation_root=args.installation_root, installations=inventory)
    client = await Client.connect(args.address, namespace=args.namespace)
    worker = create_worker(client, configuration, installation_root=args.installation_root)
    print("ChainFactEpisodeV1 Worker ready / " + queue_for_configuration(configuration), flush=True)
    await worker.run()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("clinical", "site", "safety", "deployment", "metadata", "installed", "installation-root"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--address", default="localhost:7233")
    parser.add_argument("--namespace", default="default")
    asyncio.run(serve(parser.parse_args()))


if __name__ == "__main__":
    main()
