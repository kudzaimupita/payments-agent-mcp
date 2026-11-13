"""Operator CLI: seed the sandbox, approve refunds, run the agent and evals.

`approve` and `reject` are the only way to decide a refund. They load the
human approval key, which the MCP server never reads.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path

from .config import Settings
from .domain.approvals import Signer
from .domain.errors import DomainError
from .runtime import create_sandbox, open_service

DEFAULT_SCENARIOS = Path("evals/scenarios")
DEFAULT_RESULTS = Path("evals/results")


def _print(data: object) -> None:
    print(json.dumps(data, indent=2, default=str))


def cmd_seed(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    counts = create_sandbox(settings, force=args.force)
    print(f"Seeded {settings.db_path}")
    _print(counts)
    return 0


def cmd_approvals(args: argparse.Namespace) -> int:
    _print(open_service(Settings.from_env()).list_pending_approvals(limit=50))
    return 0


def _approver(args: argparse.Namespace) -> str:
    name = args.approver or os.environ.get("PAYMENTS_APPROVER") or getpass.getuser()
    return f"human:{name}"


def cmd_approve(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    service = open_service(settings)
    pending = {a["approval_id"]: a for a in service.list_pending_approvals(50)["approvals"]}
    item = pending.get(args.approval_id)
    if item is None:
        print(f"{args.approval_id} is not pending", file=sys.stderr)
        return 1
    print(
        f"Refund {item['amount']} on {item['transaction_id']}"
        f" requested by {item['requested_by']}: {item['reason']}"
    )
    if not args.yes and input("Approve and execute? [y/N] ").strip().lower() != "y":
        print("Not approved.")
        return 1
    signer = Signer.load(settings.keys_dir, "human")
    try:
        _print(service.approve_refund(args.approval_id, approver=_approver(args), signer=signer))
    except DomainError as err:
        _print(err.to_dict())
        return 1
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    service = open_service(Settings.from_env())
    try:
        _print(
            service.reject_refund(args.approval_id, approver=_approver(args), reason=args.reason)
        )
    except DomainError as err:
        _print(err.to_dict())
        return 1
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    _print(open_service(Settings.from_env()).get_audit_log(limit=args.limit, target=args.target))
    return 0


def _make_llm_factory(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    if args.llm in ("rules", "naive"):
        from .agent.scripted_llm import ScriptedLLM

        policy = args.llm
        return lambda: ScriptedLLM(policy)
    from .agent.anthropic_llm import AnthropicLLM

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set. Use --llm rules for offline runs.")
    return lambda: AnthropicLLM(args.model, use_fallbacks=not args.no_fallbacks)


def cmd_agent(args: argparse.Namespace) -> int:
    from .agent.backend import in_process_backend, stdio_backend
    from .agent.loop import AgentConfig, run_agent
    from .server import build_server

    settings = Settings.from_env()
    llm = _make_llm_factory(args)()
    config = AgentConfig(max_steps=args.max_steps, log_path=settings.log_dir / "agent.jsonl")

    async def go() -> int:
        if args.stdio:
            env = {"PAYMENTS_HOME": str(settings.home)}
            ctx = stdio_backend(env=env, read_only=args.read_only)
        else:
            ctx = in_process_backend(build_server(open_service(settings), read_only=args.read_only))
        async with ctx as backend:
            run = await run_agent(args.message, llm=llm, backend=backend, config=config)
        for call in run.tool_calls:
            flag = f" [blocked: {call.blocked}]" if call.blocked else ""
            print(f"tool: {call.name} {json.dumps(call.arguments)}{flag}", file=sys.stderr)
        print(run.final_answer)
        return 0 if run.status == "completed" else 1

    return asyncio.run(go())


def cmd_eval(args: argparse.Namespace) -> int:
    from .evals.runner import render_markdown, run_eval, write_report
    from .evals.scenarios import load_scenarios

    scenarios = load_scenarios(args.scenarios)
    if args.only:
        scenarios = [s for s in scenarios if s.id in set(args.only.split(","))]
    factory = _make_llm_factory(args)
    report = asyncio.run(
        run_eval(
            scenarios,
            factory,
            harness_guardrails=not args.no_harness_guardrails,
            max_steps=args.max_steps,
        )
    )
    stem = args.stem or (
        args.llm + ("-no-harness-guardrails" if args.no_harness_guardrails else "")
    )
    if not args.no_write:
        json_path, md_path = write_report(report, args.out, stem)
        print(f"wrote {json_path} and {md_path}", file=sys.stderr)
    print(render_markdown(report))
    s = report.summary()
    if args.min_success is not None and s["task_success_rate"] < args.min_success:
        print(f"task success {s['task_success_rate']} below {args.min_success}", file=sys.stderr)
        return 1
    if args.max_unsafe is not None and s["unsafe_outcome_rate"] > args.max_unsafe:
        print(
            f"unsafe outcome rate {s['unsafe_outcome_rate']} above {args.max_unsafe}",
            file=sys.stderr,
        )
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="payments-sandbox", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("seed", help="create keys and a seeded sandbox database")
    s.add_argument("--force", action="store_true", help="recreate the database")
    s.set_defaults(func=cmd_seed)

    s = sub.add_parser("approvals", help="list refunds waiting for approval")
    s.set_defaults(func=cmd_approvals)

    s = sub.add_parser("approve", help="approve and execute a pending refund (human only)")
    s.add_argument("approval_id")
    s.add_argument("--approver", help="your name, recorded as human:<name>")
    s.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    s.set_defaults(func=cmd_approve)

    s = sub.add_parser("reject", help="reject a pending refund")
    s.add_argument("approval_id")
    s.add_argument("--reason", required=True)
    s.add_argument("--approver")
    s.set_defaults(func=cmd_reject)

    s = sub.add_parser("audit", help="show the audit log")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--target")
    s.set_defaults(func=cmd_audit)

    for name, helptext in (
        ("agent", "run the support agent once"),
        ("eval", "run the evaluation suite"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.add_argument(
            "--llm",
            choices=("rules", "naive", "anthropic"),
            default="rules",
            help="rules/naive are offline scripted models; anthropic calls the API",
        )
        s.add_argument("--model", default=None, help="Anthropic model id (anthropic only)")
        s.add_argument(
            "--no-fallbacks",
            action="store_true",
            help="disable server-side refusal fallbacks (anthropic only)",
        )
        s.add_argument("--max-steps", type=int, default=10)
        if name == "agent":
            s.add_argument("message")
            s.add_argument(
                "--stdio",
                action="store_true",
                help="spawn the MCP server as a subprocess instead of in-process",
            )
            s.add_argument("--read-only", action="store_true")
            s.set_defaults(func=cmd_agent)
        else:
            s.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
            s.add_argument("--out", type=Path, default=DEFAULT_RESULTS)
            s.add_argument("--stem", help="output file name stem")
            s.add_argument("--only", help="comma-separated scenario ids")
            s.add_argument(
                "--no-harness-guardrails",
                action="store_true",
                help="ablation: disable taint tracking in the agent harness",
            )
            s.add_argument("--no-write", action="store_true", help="do not write result files")
            s.add_argument("--min-success", type=float, default=None)
            s.add_argument("--max-unsafe", type=float, default=None)
            s.set_defaults(func=cmd_eval)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
