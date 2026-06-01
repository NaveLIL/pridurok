"""Run practical quality checks for Pridurok responses."""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from dataclasses import dataclass
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm

llm.config.TEMPERATURE = 0.0


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    checks: list[dict[str, Any]]
    reply: str


def _classify_error(exc: Exception) -> str:
    text = str(exc).lower()
    if any(token in text for token in ("timeout", "timed out", "connection", "reset", "network", "unreachable", "503", "502", "504")):
        return "transient_network"
    if any(token in text for token in ("rate limit", "out of credits", "insufficient credits", "429", "402")):
        return "provider_limit"
    return "llm_error"


def _contains_any(text: str, needles: list[str]) -> bool:
    low = text.lower()
    return any(n.lower() in low for n in needles)


def _contains_none(text: str, needles: list[str]) -> bool:
    low = text.lower()
    return all(n.lower() not in low for n in needles)


async def _gen_reply(case: dict[str, Any]) -> str:
    history = case.get("history", [])
    prompt = case["prompt"]
    user_name = case.get("user_name", "User")
    user_context = case.get("user_context", "")
    channel_context = case.get("channel_context", "")

    chunks: list[str] = []
    async for delta in llm.stream_reply(
        history=history,
        user_prompt=prompt,
        user_name=user_name,
        user_context=user_context,
        channel_context=channel_context,
    ):
        chunks.append(delta)

    return "".join(chunks).strip()


async def _gen_reply_with_retry(case: dict[str, Any], retries: int) -> tuple[str, Exception | None, int]:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await _gen_reply(case), None, attempt + 1
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= retries:
                break
            await asyncio.sleep(0.75 * (attempt + 1))
    return "", last_exc, retries + 1


def _evaluate_case(case: dict[str, Any], reply: str) -> CaseResult:
    checks: list[dict[str, Any]] = []

    must_include_any = case.get("must_include_any", [])
    if must_include_any:
        ok = _contains_any(reply, must_include_any)
        checks.append(
            {
                "name": "must_include_any",
                "ok": ok,
                "details": must_include_any,
            }
        )

    forbid_contains = case.get("forbid_contains", [])
    if forbid_contains:
        ok = _contains_none(reply, forbid_contains)
        checks.append(
            {
                "name": "forbid_contains",
                "ok": ok,
                "details": forbid_contains,
            }
        )

    min_len = case.get("min_len")
    if min_len is not None:
        ok = len(reply) >= int(min_len)
        checks.append(
            {
                "name": "min_len",
                "ok": ok,
                "details": {"actual": len(reply), "expected": int(min_len)},
            }
        )

    max_len = case.get("max_len")
    if max_len is not None:
        ok = len(reply) <= int(max_len)
        checks.append(
            {
                "name": "max_len",
                "ok": ok,
                "details": {"actual": len(reply), "expected": int(max_len)},
            }
        )

    passed = all(c["ok"] for c in checks) if checks else True
    return CaseResult(case_id=case["id"], passed=passed, checks=checks, reply=reply)


def _load_cases(cases_arg: str) -> tuple[list[dict[str, Any]], list[str]]:
    files = [chunk.strip() for chunk in cases_arg.split(",") if chunk.strip()]
    all_cases: list[dict[str, Any]] = []
    loaded_files: list[str] = []
    for file_name in files:
        path = pathlib.Path(file_name)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"Cases file must contain a list: {path}")
        all_cases.extend(data)
        loaded_files.append(str(path))
    return all_cases, loaded_files


async def run_eval(cases_arg: str, out_path: pathlib.Path | None, retries: int) -> int:
    cases, loaded_files = _load_cases(cases_arg)

    results: list[CaseResult] = []
    network_failures = 0
    for case in cases:
        reply, exc, attempts = await _gen_reply_with_retry(case, retries)
        if exc is not None:
            err_type = _classify_error(exc)
            if err_type == "transient_network":
                network_failures += 1
            results.append(
                CaseResult(
                    case_id=case["id"],
                    passed=False,
                    checks=[
                        {
                            "name": "llm_call",
                            "ok": False,
                            "details": {
                                "type": err_type,
                                "attempts": attempts,
                                "error": str(exc),
                            },
                        }
                    ],
                    reply="",
                )
            )
            continue

        results.append(_evaluate_case(case, reply))

    passed_cases = sum(1 for r in results if r.passed)
    total_cases = len(results)
    score = (passed_cases / total_cases * 100.0) if total_cases else 0.0

    report = {
        "summary": {
            "passed_cases": passed_cases,
            "total_cases": total_cases,
            "score": round(score, 2),
        },
        "results": [
            {
                "case_id": r.case_id,
                "passed": r.passed,
                "checks": r.checks,
                "reply": r.reply,
            }
            for r in results
        ],
    }

    print(f"Loaded cases from: {', '.join(loaded_files)}")
    print(f"Eval score: {passed_cases}/{total_cases} ({score:.1f}%)")
    if network_failures:
        print(f"Transient network/provider failures: {network_failures}")
    for r in results:
        state = "PASS" if r.passed else "FAIL"
        print(f"\n[{state}] {r.case_id}")
        for ch in r.checks:
            c_state = "ok" if ch["ok"] else "bad"
            print(f"  - {ch['name']}: {c_state}")
        preview = r.reply.replace("\n", " ")
        if len(preview) > 220:
            preview = preview[:217] + "..."
        print(f"  reply: {preview}")

    if out_path:
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nReport saved: {out_path}")

    if network_failures and passed_cases != total_cases:
        return 3
    return 0 if passed_cases == total_cases else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate chat quality for Pridurok bot.")
    parser.add_argument(
        "--cases",
        default="scripts/eval_cases.json",
        help="Path to JSON file with evaluation cases.",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Optional path to save JSON report.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="Retry count for transient LLM/network failures.",
    )
    args = parser.parse_args()

    out_path = pathlib.Path(args.out) if args.out else None

    raise SystemExit(asyncio.run(run_eval(args.cases, out_path, max(0, args.retries))))


if __name__ == "__main__":
    main()
