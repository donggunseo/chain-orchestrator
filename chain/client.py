"""Console-free native Episode start for backend integrations.

Signal delivery is only transport success. Read snapshot ingress_receipts,
event_receipts and activity_progress for engine acceptance/application/results.
Authentication/authorization and HTTP/Push endpoints belong to the backend.
"""
from temporalio.common import WorkflowIDReusePolicy

from .execution import WORKFLOW_TYPE, build_start


async def start_episode(client, configuration, initial, *, engine_run_id):
    start = build_start(configuration, initial, engine_run_id=engine_run_id)
    return await client.start_workflow(WORKFLOW_TYPE, start, id=engine_run_id,
        task_queue=start["run_manifest"]["task_queue"],
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
