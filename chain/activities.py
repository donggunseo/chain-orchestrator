"""Temporal Activities for the distinct native-v1 Worker artifact.

Only bounded transient I/O failures are retried by the caller's pinned RetryPolicy.
Contract, approval, fixture/schema and unexpected module failures are explicit
non-retryable failures. A native FAILED Agent result is returned unchanged: it
already records failure and cannot turn into a default successful result.
"""
from __future__ import annotations

import asyncio

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .errors import ConfigError
from .execution import queue_for_configuration
from .shape import check_json


class FactActivities:
    def __init__(self, runtime):
        self.runtime = runtime
        package = getattr(runtime, "package", None)
        self._turn_binding = None if package is None else (
            package["manifest"]["configuration_hash"], queue_for_configuration(package))

    @property
    def registered(self):
        return [self.resolve_event, self.preflight, self.reconcile, self.run_agent, self.run_work,
                self.yield_turn]

    @activity.defn(name="chain.fact.yield_turn_v1")
    async def yield_turn(self, envelope: dict) -> dict:
        """A bounded execution handoff, with no provider call or clinical effect.

        Unlike a subsecond Timer this roundtrip need not wait for the server's
        timer precision. History records it; retries repeat only this pure ack.
        """
        try:
            check_json(envelope)
            info = activity.info()
            if (not isinstance(envelope, dict)
                    or set(envelope) != {"engine_run_id", "configuration_hash", "run_pin", "turn"}
                    or type(envelope["turn"]) is not int or envelope["turn"] < 1
                    or not isinstance(envelope["run_pin"], str)
                    or len(envelope["run_pin"]) != 71 or not envelope["run_pin"].startswith("sha256:")
                    or any(char not in "0123456789abcdef" for char in envelope["run_pin"][7:])
                    or self._turn_binding is None
                    or envelope["configuration_hash"] != self._turn_binding[0]
                    or envelope["engine_run_id"] != info.workflow_id
                    or info.task_queue != self._turn_binding[1]):
                raise ValueError("Pinned handoff binding differs")
        except (ConfigError, ValueError, TypeError, KeyError):
            raise ApplicationError("Invalid execution handoff binding", type="TURN_BINDING_INVALID", non_retryable=True) from None
        return {"contract_schema": "chain-fact-turn-result/v1", **envelope}

    async def _call(self, callback, envelope, *, agent_deadline=False):
        try:
            if agent_deadline:
                async with asyncio.timeout(self.runtime.agent_timeout_s(envelope)):
                    return await asyncio.to_thread(callback, envelope)
            return await asyncio.to_thread(callback, envelope)
        except ConfigError as error:
            raise ApplicationError(error.message, type=error.code, non_retryable=True) from None
        except (ConnectionError, TimeoutError, OSError):
            raise ApplicationError("Temporary installed module I/O failure", type="MODULE_IO_TRANSIENT", non_retryable=False) from None
        except (ValueError, TypeError, KeyError):
            raise ApplicationError("Installed module contract validation failed", type="MODULE_CONTRACT_INVALID", non_retryable=True) from None
        except Exception:
            # Do not expose patient data, source code or credentials in errors.
            raise ApplicationError("Installed Worker module failed", type="MODULE_EXECUTION_FAILED", non_retryable=True) from None

    @activity.defn(name="chain.fact.resolve_event_v1")
    async def resolve_event(self, envelope: dict) -> dict:
        return await self._call(self.runtime.resolve_event, envelope)

    @activity.defn(name="chain.fact.preflight_v1")
    async def preflight(self, envelope: dict) -> dict:
        return await self._call(self.runtime.preflight, envelope)

    @activity.defn(name="chain.fact.reconcile_v1")
    async def reconcile(self, envelope: dict) -> dict:
        return await self._call(self.runtime.reconcile, envelope)

    @activity.defn(name="chain.fact.run_agent_v1")
    async def run_agent(self, envelope: dict) -> dict:
        return await self._call(self.runtime.run_agent, envelope, agent_deadline=True)

    @activity.defn(name="chain.fact.run_work_v1")
    async def run_work(self, envelope: dict) -> dict:
        return await self._call(self.runtime.run_work, envelope)
