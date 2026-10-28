"""Run the native fact-engine synthetic demo; with the CHAIN v0.4 runtime."""
import argparse
import asyncio
import copy
from datetime import datetime, timezone
import json
import hashlib
import math
from pathlib import Path
import sys
from threading import Event

import yaml
from temporalio.service import RPCError

from demo.io import load_initial
from chain.compiler import canonical_hash
from chain.configuration import _read_document
from chain.errors import ConfigError
from chain.runtime_contracts import validate_initial
from demo.input import StdinReader
from demo.output import DemoOutput
from demo.simulator.clock import ManualClock

from .console import serve_console
from .driver import consume, done, drive, load_driver, load_recorded
from .hospital import FactHospital
from .package import CONFIG, EPISODE, ROOT, load_package
from .presentation import FactDemoPresenter
from .runtime import DemoQueryError, FactLocalRuntime, FactTemporalRuntime
from .source_store import PublishedSourceStore

SCENARIOS = Path(__file__).parent / "scenarios"


def save(path, value):
    with Path(path).open("x", encoding="utf8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _configuration(args, output):
    clinical_path = Path(args.workflow) if args.workflow else CONFIG / "clinical.yaml"
    original = load_package(fixture_profile=args.fixture_profile, clinical_path=clinical_path)
    poll_interval = getattr(args, "test_poll_interval_s", None)
    if poll_interval is not None and (getattr(args, "test_mode", False) is not True
            or type(poll_interval) not in (int, float) or not math.isfinite(poll_interval) or poll_interval <= 0):
        raise ValueError("A finite positive test poll interval requires --test-mode")
    if args.test_timer_scale == 1 and poll_interval is None:
        return original
    documents = {}
    deployment_path = None
    for name, path, needed in (
            ("clinical", clinical_path, args.test_timer_scale != 1),
            ("deployment", CONFIG / ("deployment" + ("" if args.fixture_profile == "positive" else "-negative") + ".yaml"),
             poll_interval is not None)):
        if needed:
            document, evidence = _read_document(path)
            if evidence["byte_hash"] != original["artifact_evidence"]["sources"][name]["byte_hash"]:
                raise ConfigError("CONFIGURATION_CHANGED_DURING_PREPARATION", name,
                                  "Original configuration changed before explicit test overrides")
            documents[name] = document
    if args.test_timer_scale != 1:
        clinical = documents["clinical"]
        for timer in clinical["timers"].values():
            timer["after"]["value"] *= args.test_timer_scale
        effective = output / "clinical-effective.yaml"
        effective.write_text("# Explicit synthetic demo timer scaling; original Clinical unchanged.\n" +
            yaml.safe_dump(clinical, allow_unicode=True, sort_keys=False), encoding="utf8")
        clinical_path = effective
    if poll_interval is not None:
        deployment = documents["deployment"]
        deployment["reconciliation"]["poll_interval_s"] = poll_interval
        deployment_path = output / "deployment-effective.yaml"
        deployment_path.write_text("# Explicit synthetic demo poll interval; original Deployment unchanged.\n" +
            yaml.safe_dump(deployment, allow_unicode=True, sort_keys=False), encoding="utf8")
    return load_package(fixture_profile=args.fixture_profile, clinical_path=clinical_path,
                        deployment_path=deployment_path)


def failures(snapshot):
    return [{"execution_id": key, "code": row["result"].get("error_code"),
             "kind": "agent" if "agent_id" in row["request"] else row["request"]["kind"]}
        for key, row in snapshot.get("executions", {}).items()
        if row.get("result") and row["result"]["status"] == "FAILED"]


def full_snapshot(value):
    return (isinstance(value, dict) and value.get("query_schema") != "chain-fact-progress/v1"
        and {"termination_phase", "facts", "hitl", "executions", "tasks", "changes"} <= set(value))


def query_diagnostic(error, stage="snapshot"):
    code = (error.code if isinstance(error, DemoQueryError) else error.status.name if isinstance(error, RPCError)
            else "QUERY_TIMEOUT" if isinstance(error, TimeoutError) else type(error).__name__)
    return {"stage": error.stage if isinstance(error, DemoQueryError) else stage, "code": code}


async def run(args, *, write=print, input_fn=None):
    if not args.test_mode:
        raise ValueError("The packaged historical scenario requires --test-mode; no live-patient mode is provided")
    if args.backend != "temporal" and (args.start_local or args.address):
        raise ValueError("--start-local/--address require --backend temporal")
    if args.start_local and args.address:
        raise ValueError("--start-local and --address cannot be combined")
    if (type(args.test_timer_scale) not in (int, float) or not 0 < args.test_timer_scale <= 1
            or type(args.run_timeout) not in (int, float) or not 0 < args.run_timeout < 86400):
        raise ValueError("Use a finite positive timeout and timer scale in (0,1]")
    if args.test_poll_interval_s is not None and (type(args.test_poll_interval_s) not in (int, float)
            or not math.isfinite(args.test_poll_interval_s) or args.test_poll_interval_s <= 0):
        raise ValueError("Use a finite positive --test-poll-interval-s")
    if args.hitl == "recorded" and not args.recorded:
        raise ValueError("Recorded responses require explicit --test-mode --hitl recorded --recorded FILE")
    if args.recorded and args.hitl != "recorded":
        raise ValueError("--recorded is only used by --hitl recorded")
    output = Path(args.output_dir)
    if output.exists():
        raise FileExistsError("Refusing to overwrite output directory: " + str(output))
    initial = validate_initial(load_initial(Path(args.episode) / "initial.json"))
    # Test-side schedules and responses stay outside all public Start inputs.
    plan = load_driver(args.test_driver, test_mode=True)
    recorded = load_recorded(args.recorded, test_mode=True) if args.hitl == "recorded" else None
    bindings = json.loads((SCENARIOS / "bindings.json").read_text(encoding="utf8"))
    output.mkdir(parents=True)
    configuration = _configuration(args, output)
    write = write if isinstance(write, DemoOutput) else DemoOutput(write)
    clock = ManualClock(initial["arrival_time"], test_mode=True)
    store = PublishedSourceStore(initial, configuration["worker"]["site"])
    services = {"store": store, "notifier_path": output / "notices.sqlite",
                "effects_path": output / "effects.sqlite", "write": write, "source_calls": [], "backend_calls": []}
    runtime_type = FactTemporalRuntime if args.backend == "temporal" else FactLocalRuntime
    runtime = runtime_type(configuration, initial, services=services, installation_root=ROOT, output=output,
        address=args.address, start_local=args.start_local, write=write, timer_scale=args.test_timer_scale)
    implementation = {str(path.relative_to(ROOT)): "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in sorted(Path(__file__).parent.glob("*.py"))}
    save(output / "manifest.json", {"schema": "chain-fact-demo-run/v1", "backend": args.backend,
        "test_mode": True, "synthetic_only": True, "initial": initial,
        "configuration_manifest": configuration["manifest"], "run_manifest": runtime.start["run_manifest"],
        "driver_hash": canonical_hash(plan), "fixture_profile": args.fixture_profile,
        "demo_implementation_files": implementation, "demo_implementation_hash": canonical_hash(implementation),
        "recorded_provenance": recorded, "test_timer_scale": args.test_timer_scale,
        "source_delivery": "POLLING_ONLY", "test_poll_interval_s": args.test_poll_interval_s,
        "poll_interval_s": configuration["public_definition"]["execution"]["reconciliation"]["poll_interval_s"],
        "clock_contract": "HISTORICAL_HOSPITAL_CLOCK; ACTUAL_ENGINE_APPLIED_CLOCK", "real_delivery": False})
    save(output / "definition.json", configuration["public_definition"])
    stop, tasks, client_requests = Event(), [], []
    snapshot, progress, simulation, publications = {}, {}, {}, []
    snapshot_error = None
    outcome = {"schema": "chain-fact-demo-outcome/v1", "backend": args.backend, "status": "FAILED", "reason": None}
    hospital = None
    try:
        async with runtime:
            async with FactHospital(args.episode, output / "sources", store, clock,
                    decision_bindings=bindings["decisions"]) as hospital:
                await hospital.start(initial)
                tasks.append(asyncio.create_task(hospital.watch(runtime.progress, poll_interval=.05)))
                tasks.append(asyncio.create_task(drive(plan, hospital, runtime, clock, bindings,
                    client_requests=client_requests, write=write)))
                if recorded:
                    human = asyncio.create_task(consume(recorded, runtime, hospital, clock, plan, bindings, write=write))
                else:
                    human = asyncio.create_task(serve_console(runtime.progress, runtime.submit_decision,
                        definition=configuration["public_definition"], resume=runtime.request_hitl_resume,
                        input_fn=input_fn or StdinReader(stop, write), write=write, clock=runtime.now,
                        on_request=client_requests.append, detail=runtime.hitl_view))
                tasks.append(human)
                presenter = FactDemoPresenter(configuration["public_definition"], write)
                try:
                    async with asyncio.timeout(args.run_timeout) as run_limit:
                        while True:
                            progress = await runtime.progress()
                            presenter.observe(progress, timers=await runtime.timer_status(progress))
                            errors = failures(progress) + hospital.failures + hospital.scheduler.failures
                            if errors:
                                raise RuntimeError(json.dumps(errors, ensure_ascii=False))
                            for task in tasks:
                                if task.done() and not task.cancelled() and task.exception():
                                    raise task.exception()
                            if done(progress):
                                snapshot = await runtime.result
                                outcome["status"] = "COMPLETED"
                                break
                            if human.done() and not recorded:
                                outcome.update(status="INPUT_CLOSED", reason="Console ended; no decision or cancellation was sent")
                                break
                            if recorded and human.done() and any(q["status"] == "OPEN" for q in progress.get("hitl", {}).get("requests", {}).values()):
                                raise ValueError("Recorded responses exhausted; no additional decision is invented")
                            if runtime.result.done():
                                await runtime.result
                            await asyncio.sleep(.08)
                except TimeoutError:
                    if not run_limit.expired():
                        raise
                    outcome.update(status="TIMED_OUT", reason="Demo deadline; partial result saved")
                finally:
                    stop.set()
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    try:
                        snapshot = await runtime.snapshot()
                    except Exception as error:
                        snapshot_error = query_diagnostic(error)
                    simulation = {"manifest": hospital.manifest, "anchors": hospital.scheduler.anchors,
                        "audit": hospital.scheduler.audit, "failures": hospital.failures + hospital.scheduler.failures}
                    publications = copy.deepcopy(hospital.publications)
    except asyncio.CancelledError:
        outcome.update(status="INTERRUPTED", reason="Client interrupted; partial result saved")
        stop.set()
    except Exception as error:
        if isinstance(error, DemoQueryError):
            reason = "Query " + error.stage + " [" + error.code + "]"
        elif isinstance(error, RPCError):
            diagnostic = getattr(runtime, "query_diagnostic", None) or {"stage": "transport", "code": error.status.name}
            reason = "Query " + diagnostic["stage"] + " [" + diagnostic["code"] + "]"
        else:
            reason = f"{type(error).__name__}: {error}"
        outcome.update(status="FAILED", reason=reason)
        stop.set()
    finally:
        try:
            candidate = await runtime.snapshot()
            if full_snapshot(candidate):
                snapshot = candidate
        except Exception as error:
            snapshot_error = query_diagnostic(error)
        if not full_snapshot(snapshot) and runtime.result and runtime.result.done() and not runtime.result.cancelled():
            try:
                candidate = runtime.result.result()
                if full_snapshot(candidate):
                    snapshot = candidate
            except Exception:
                pass
        available = full_snapshot(snapshot)
        if available:
            save(output / "snapshot.json", snapshot)
        else:
            snapshot_error = snapshot_error or {"stage": "snapshot", "code": "SNAPSHOT_UNAVAILABLE"}
            if outcome["status"] == "COMPLETED" or outcome["status"] == "FAILED" and not outcome["reason"]:
                outcome.update(status="FAILED", reason="Query " + snapshot_error["stage"] + " [" + snapshot_error["code"] + "]")
        save(output / "progress.json", progress)
        save(output / "snapshot-status.json", {"status": "AVAILABLE" if available else "UNAVAILABLE",
            "diagnostic": snapshot_error})
        save(output / "simulation.json", simulation)
        save(output / "publications.json", publications)
        save(output / "source_queries.json", store.calls)
        save(output / "backend_queries.json", services["backend_calls"])
        save(output / "source_publications.json", store.publications)
        if args.expected and not available:
            save(output / "comparison.json", {"comparison_schema": "chain-fact-demo-comparison/v1",
                "status": "UNAVAILABLE", "checks_passed": False,
                "diagnostic": {"stage": "comparison", "code": "SNAPSHOT_UNAVAILABLE"}})
        elif args.expected:
            from .comparison import compare
            try:
                comparison = compare(snapshot, publications, simulation, args.expected)
                save(output / "comparison.json", comparison)
                if outcome["status"] == "COMPLETED" and not comparison["checks_passed"]:
                    outcome.update(status="COMPARISON_FAILED", reason="Post-run semantic checks failed")
            except Exception as error:
                # Preserve the first execution/query failure. Comparison is a
                # separate, post-run diagnostic and may itself be unavailable.
                code = type(error).__name__
                save(output / "comparison.json", {"comparison_schema": "chain-fact-demo-comparison/v1",
                    "status": "FAILED", "checks_passed": False,
                    "diagnostic": {"stage": "comparison", "code": code}})
                if outcome["status"] == "COMPLETED" or not outcome["reason"]:
                    outcome.update(status="FAILED", reason="Comparison [" + code + "]")
        outcome.update(state=snapshot.get("state"), finalized=done(snapshot))
        save(output / "outcome.json", outcome)
        write("RUN_OUTCOME | " + outcome["status"] + " | state=" + str(outcome["state"]) + " | " + str(output))
        if outcome["reason"]:
            write("RUN_REASON | " + outcome["reason"])
    return 0 if outcome["status"] == "COMPLETED" else 2 if outcome["status"] in {"INPUT_CLOSED", "TIMED_OUT", "INTERRUPTED"} else 1


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--episode", default=str(EPISODE))
    result.add_argument("--backend", choices=("local", "temporal"), default="local")
    result.add_argument("--start-local", action="store_true")
    result.add_argument("--address")
    result.add_argument("--test-mode", action="store_true")
    result.add_argument("--test-timer-scale", type=float, default=1)
    result.add_argument("--test-poll-interval-s", type=float,
        help="Explicit synthetic source polling interval; requires --test-mode and preserves the original Deployment")
    result.add_argument("--test-driver", default=str(ROOT / "demo/scenarios/reference.yaml"))
    result.add_argument("--hitl", choices=("console", "recorded"), default="console")
    result.add_argument("--recorded")
    result.add_argument("--expected")
    result.add_argument("--fixture-profile", choices=("positive", "negative"), default="positive")
    result.add_argument("--workflow", help="Native Clinical YAML; the other approved demo files remain pinned")
    result.add_argument("--run-timeout", type=float, default=1800)
    result.add_argument("--output-dir", required=True)
    return result


def main():
    arg_parser = parser()
    args = arg_parser.parse_args()
    try:
        with DemoOutput(lambda value: print(value, flush=True),
                        terminal_stream=sys.stdout, input_stream=sys.stdin) as write:
            code = asyncio.run(run(args, write=write))
    except (ValueError, FileExistsError, OSError) as error:
        arg_parser.error(str(error))
    raise SystemExit(code)
