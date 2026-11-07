# -*- coding: utf-8 -*-
"""
批量脚本：for 循环跑 10 条飞检记录 × N 次，temperature=0，结果写 CSV。

用法：
    python main.py                       # 默认跑 config.RUN_TIMES 次（3 次）
    python main.py --times 3             # 指定每个用例运行次数
    python main.py --dry-run --times 3   # 不调用模型，用本地确定性桩验证全流程
    python main.py --cases FJ001,FJ010   # 只跑指定用例
    python main.py --output my.csv       # 自定义输出文件名（仍写在 results/ 下）
    python main.py --list-models         # 只打印服务端可用模型 id，不跑用例

输出：results/result.csv —— 每条用例每次运行一行，含原始回复、解析后的六要素、
      格式校验结果、耗时、错误信息。
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime

import config
import prompts
from mock_data import get_cases
from schema import validate_response


def _dry_run_answer(case: dict, run_index: int) -> str:
    """
    零依赖本地桩：默认返回该用例的人工标注 gold（即"理想答案"）。
    为了让评估链路的「不一致」分支也能被验证，故意制造两处抖动：
      - FJ010 第 3 次：把本应「未提及」的时间编造成 "2024年"；
      - FJ003 第 2 次：把结果改成闭集外的 "约谈整改"（演示枚举违规）。
    这两处只在 --dry-run 出现，不影响真实调用。
    """
    answer = dict(case["gold"])
    if case["case_id"] == "FJ010" and run_index == 3:
        answer["时间"] = "2024年"
    if case["case_id"] == "FJ003" and run_index == 2:
        answer["结果"] = "约谈整改"
    return json.dumps(answer, ensure_ascii=False)


def _make_client():
    """按需构造 OpenAI 兼容客户端（延迟导入，保证 --dry-run 零依赖）。"""
    from openai import OpenAI
    return OpenAI(api_key=config.API_KEY, base_url=config.BASE_URL,
                  timeout=config.REQUEST_TIMEOUT)


def list_models(client=None) -> list[str]:
    """拉取服务端可用模型 id；接口不可用时返回空列表（表示无法预检）。"""
    try:
        return [m.id for m in (client or _make_client()).models.list()]
    except Exception:  # noqa: BLE001 - /models 不是所有兼容服务都提供
        return []


def call_llm(case: dict, run_index: int, dry_run: bool,
             client=None) -> tuple[str, int, str]:
    """返回 (原始回复文本, 耗时毫秒, 错误信息)。"""
    if dry_run:
        time.sleep(0.01)
        return _dry_run_answer(case, run_index), 10, ""

    import random

    if client is None:
        client = _make_client()
    last_error = ""
    for attempt in range(1, config.MAX_RETRIES + 1):
        started = time.perf_counter()
        try:
            resp = client.chat.completions.create(
                model=config.MODEL_NAME,
                messages=prompts.build_messages(case),
                temperature=config.TEMPERATURE,   # 任务要求固定 0
                max_tokens=config.MAX_TOKENS,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            choice = resp.choices[0]
            content = choice.message.content or ""
            if not content.strip():
                # 「调用成功却无正文」不能静默当成功：推理型模型会把 token 全花在
                # reasoning_tokens 上（finish_reason=length、content=''），CSV 里会
                # 出现 raw_response 为空且 error 也为空的幽灵行。这里转成可重试错误，
                # 并在 error 列留下诊断线索。
                usage = getattr(resp, "usage", None)
                det = getattr(usage, "completion_tokens_details", None) if usage else None
                raise RuntimeError(
                    f"空正文：finish_reason={getattr(choice, 'finish_reason', None)}, "
                    f"completion_tokens={getattr(usage, 'completion_tokens', None) if usage else None}, "
                    f"reasoning_tokens={getattr(det, 'reasoning_tokens', None) if det else None}"
                    "（推理模型预算不足？请调大 config.MAX_TOKENS 或换非推理模型）"
                )
            return content, latency_ms, ""
        except Exception as exc:  # noqa: BLE001 - 网络/限流/空正文一律重试
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF ** (attempt - 1) + random.random())
    return "", 0, last_error


def run(args: argparse.Namespace) -> None:
    cases = get_cases()
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",")}
        cases = [c for c in cases if c["case_id"] in wanted]
        if not cases:
            raise SystemExit(f"没有匹配的用例：{args.cases}")

    if not args.dry_run:
        config.validate_config()

    client = None
    if not args.dry_run and not args.skip_preflight:
        client = _make_client()
        available = list_models(client)
        # 预检：确认模型 id 在服务端存在，避免「模型名写错 → 大量无效 503」
        if available and config.MODEL_NAME not in available:
            raise SystemExit(
                f"配置错误：服务端不存在模型 '{config.MODEL_NAME}'。\n"
                f"  LLM_BASE_URL = {config.BASE_URL or '(SDK 默认)'}\n"
                f"  可用模型（共 {len(available)} 个，列出前 15 个）：\n    "
                + "\n    ".join(available[:15])
                + "\n  请修改 .env 里的 LLM_MODEL_NAME，或用 --list-models 查看完整列表；"
                  " 若确定要跳过预检可加 --skip-preflight。"
            )

    output_path = config.OUTPUT_DIR / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total = len(cases) * args.times
    done = 0
    with output_path.open("w", newline="", encoding=config.CSV_ENCODING) as fh:
        writer = csv.DictWriter(fh, fieldnames=config.CSV_FIELDS)
        writer.writeheader()
        for case in cases:                       # ← 任务 4：for 循环跑 10 条
            for run_index in range(1, args.times + 1):
                raw, latency_ms, error = call_llm(
                    case, run_index, args.dry_run, client
                )
                check = validate_response(raw, case["source_text"]) if raw else {
                    "values": {f: "" for f in config.SIX_FIELDS},
                    "format_ok": False,
                }
                writer.writerow({
                    "case_id": case["case_id"],
                    "run_index": run_index,
                    "source_text": case["source_text"],
                    "model": "dry-run-stub" if args.dry_run else config.MODEL_NAME,
                    "raw_response": raw,
                    **check["values"],
                    "format_ok": "是" if check["format_ok"] else "否",
                    "latency_ms": latency_ms,
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "error": error,
                })
                done += 1
                flag = "OK" if check["format_ok"] else "格式异常"
                print(f"[{done:>3}/{total}] {case['case_id']} run={run_index} "
                      f"{flag} "
                      f"{'ERROR: ' + error if error else f'{latency_ms}ms'}")

    print(f"\n完成，共 {done} 条 → {output_path}")
    print("下一步：python main.py --output result2.csv / result3.csv 再跑两遍，"
          "然后 python evaluate.py")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="医保飞检通报六要素抽取 - 批量运行脚本（temperature=0）"
    )
    parser.add_argument("--times", type=int, default=config.RUN_TIMES,
                        help="每个用例运行次数（默认取 config.RUN_TIMES）")
    parser.add_argument("--dry-run", action="store_true",
                        help="不调用真实模型，用本地确定性桩验证流程")
    parser.add_argument("--cases", default="",
                        help="只跑指定用例，逗号分隔，如 FJ001,FJ010")
    parser.add_argument("--output", default=config.OUTPUT_CSV.name,
                        help="输出 CSV 文件名（位于 results/ 下）")
    parser.add_argument("--list-models", action="store_true",
                        help="只打印服务端可用模型 id 后退出（用于核对配置）")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="跳过模型 id 预检（某些服务不提供 /models 接口时使用）")
    args = parser.parse_args()

    if args.list_models:
        config.validate_config()
        models = list_models()
        if not models:
            raise SystemExit("服务端未提供 /models 接口，无法列出模型；"
                             "请直接查阅服务商文档确认模型名称。")
        print(f"LLM_BASE_URL = {config.BASE_URL or '(SDK 默认)'}")
        print(f"LLM_MODEL_NAME = {config.MODEL_NAME} "
              f"{'(当前配置 ✓)' if config.MODEL_NAME in models else '(当前配置 ✗ 不存在!)'}")
        print(f"可用模型（{len(models)} 个）：")
        for m in models:
            print("  " + m)
        return

    if args.times < 1:
        parser.error("--times 必须 >= 1")
    run(args)


if __name__ == "__main__":
    main()
