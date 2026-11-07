# -*- coding: utf-8 -*-
"""
评估脚本：把三遍结果放一起对比，并生成「人工核对表」。

做两件事（对应任务 5 和 6）：
  5. 跑三遍 → 对比三次结果是否一致（逐字段一致率、差异字段、多数投票 consensus）。
  6. 人工核对每条 → 生成 results/review_auto.csv（逐次机器校验明细）
     与 人工核对表.md（每条记录一行：格式是否合规 / 字段是否缺失 / 有无编造，
     并留出人工复核签名栏）。

输入 CSV 支持两种跑法，自动识别：
  A) 三个独立文件：results/result.csv、result2.csv、result3.csv
     （python main.py --times 1 --output result.csv / result2.csv / result3.csv）
  B) 单文件带 run_index：results/result.csv 里 run_index=1,2,3
     （python main.py --times 3）
也可用 --inputs 显式指定任意多个 CSV。

判定口径统一走 schema.validate_response()，与系统提示词 / config.py 契约一致；
另外补一条「编造」判定：人工标注为 未提及 的字段，若模型填了具体值，视为编造。

用法：
    python evaluate.py
    python evaluate.py --inputs results/result.csv results/result2.csv results/result3.csv
    python evaluate.py --expected-runs 3
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

import config
from mock_data import get_gold
from schema import _BAD_PLACEHOLDERS, validate_response


# ---------------------------------------------------------------- 输入读取
def discover_inputs(explicit: list[str]) -> list[Path]:
    if explicit:
        return [Path(p) for p in explicit]
    trio = [config.OUTPUT_DIR / n for n in ("result.csv", "result2.csv", "result3.csv")]
    if all(p.exists() for p in trio):
        return trio
    single = config.OUTPUT_DIR / "result.csv"
    return [single] if single.exists() else []


def load_rows(paths: list[Path]) -> list[dict]:
    """读多个 CSV，每行补上来源文件标记。返回按 case_id 顺序排列的原始行。"""
    rows: list[dict] = []
    for path in paths:
        if not path.exists():
            raise SystemExit(f"找不到输入文件：{path}")
        with path.open("r", newline="", encoding=config.CSV_ENCODING) as fh:
            for row in csv.DictReader(fh):
                row["_source_file"] = path.name
                rows.append(row)
    return rows


def _is_bad(v: str) -> bool:
    return v is None or str(v).strip() == "" or str(v) in _BAD_PLACEHOLDERS


def _clean(v: str) -> str:
    return "" if v is None else str(v)


# ---------------------------------------------------------------- 单条评估
def evaluate_one(case_id: str, rows: list[dict], expected_runs: int) -> dict:
    """rows = 该用例的所有运行（跨文件 / 跨 run_index）。"""
    gold = get_gold(case_id)

    # 逐次机器校验（重新跑一遍，不信任 CSV 里存的 format_ok）
    checks = []
    for r in rows:
        raw = _clean(r.get("raw_response"))
        error = _clean(r.get("error"))
        source_text = _clean(r.get("source_text"))
        if error:
            checks.append({
                "run_index": _clean(r.get("run_index")),
                "source_file": r["_source_file"],
                "raw": raw, "error": error,
                "check": None, "valid": False,
            })
            continue
        chk = validate_response(raw, source_text) if raw else None
        if chk is None:
            checks.append({
                "run_index": _clean(r.get("run_index")),
                "source_file": r["_source_file"],
                "raw": raw, "error": error,
                "check": None, "valid": False,
            })
            continue
        # gold 补充的编造判定：标注为未提及，模型却填了具体值
        extra_fab = []
        for f in config.SIX_FIELDS:
            if gold[f] == config.UNMENTIONED:
                v = chk["values"][f]
                if not _is_bad(v) and v != config.UNMENTIONED:
                    extra_fab.append(f)
        chk["gold_fabrication"] = sorted(set(extra_fab))
        chk["suspected_fabrication"] = sorted(
            set(chk["suspected_fabrication"]) | set(extra_fab)
        )
        checks.append({
            "run_index": _clean(r.get("run_index")),
            "source_file": r["_source_file"],
            "raw": raw, "error": error,
            "check": chk, "valid": True,
        })

    valid = [c for c in checks if c["valid"] and c["check"]]
    valid_runs = len(valid)

    # ---- 一致性：以「六字段值元组」为单位比较 ----
    tuples = [tuple(c["check"]["values"][f] for f in config.SIX_FIELDS) for c in valid]
    if tuples:
        modal_tuple, modal_count = Counter(tuples).most_common(1)[0]
        consistency_rate = round(modal_count / len(tuples), 2)
    else:
        modal_tuple, consistency_rate = None, 0.0

    diff_fields = []
    field_agreement = {}
    consensus = {}
    for i, f in enumerate(config.SIX_FIELDS):
        vals = [t[i] for t in tuples]
        if not vals:
            field_agreement[f] = 0.0
            consensus[f] = config.UNMENTIONED
            continue
        top_val, top_n = Counter(vals).most_common(1)[0]
        field_agreement[f] = round(top_n / len(vals), 2)
        # 平票：最高频值出现次数不超过并列者 → 标记平票
        tied = [v for v, n in Counter(vals).items() if n == top_n]
        consensus[f] = top_val if len(tied) == 1 else "平票"
        if len(set(vals)) > 1:
            diff_fields.append(f)

    if valid_runs == 0:
        status = "无效"
    elif valid_runs < expected_runs:
        status = "运行次数不足"
    elif len(set(tuples)) <= 1:
        status = "全部一致"
    else:
        status = "不一致"

    # ---- 与人工标注对照 ----
    field_vs_gold = [f for f in config.SIX_FIELDS if consensus.get(f) != gold[f]]
    match_gold = "是" if not field_vs_gold else "否"

    format_ok_runs = sum(1 for c in valid if c["check"]["format_ok"])
    missing_fields = sorted({f for c in valid for f in c["check"]["missing_fields"]})
    suspected_fabrication = sorted({
        f for c in valid for f in c["check"]["suspected_fabrication"]
    })
    raw_variants = sorted({c["raw"] for c in checks if c["raw"]})

    return {
        "case_id": case_id,
        "valid_runs": valid_runs,
        "status": status,
        "consistency_rate": consistency_rate,
        "diff_fields": diff_fields,
        "field_agreement": field_agreement,
        "consensus": consensus,
        "gold": gold,
        "match_gold": match_gold,
        "field_vs_gold": field_vs_gold,
        "format_ok_runs": format_ok_runs,
        "missing_fields": missing_fields,
        "suspected_fabrication": suspected_fabrication,
        "raw_variants": raw_variants,
        "checks": checks,          # 供 review_auto.csv / 人工核对表使用
        "total_runs": len(rows),
    }


# ---------------------------------------------------------------- 输出：CSV
def _s(x) -> str:
    """把 list/dict 压成可读字符串，便于写进 CSV 单元格。"""
    if isinstance(x, dict):
        return " ".join(f"{k}:{v}" for k, v in x.items())
    if isinstance(x, (list, tuple, set)):
        return "、".join(str(i) for i in x) if x else "无"
    if isinstance(x, bool):
        return "是" if x else "否"
    return "" if x is None else str(x)


def write_evaluation_csv(results: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding=config.CSV_ENCODING) as fh:
        w = csv.DictWriter(fh, fieldnames=config.EVAL_FIELDS)
        w.writeheader()
        for r in results:
            w.writerow({
                "case_id": r["case_id"],
                "valid_runs": r["valid_runs"],
                "status": r["status"],
                "consistency_rate": r["consistency_rate"],
                "diff_fields": _s(r["diff_fields"]),
                "field_agreement": _s(r["field_agreement"]),
                "consensus": json.dumps(r["consensus"], ensure_ascii=False),
                "gold": json.dumps(r["gold"], ensure_ascii=False),
                "match_gold": r["match_gold"],
                "field_vs_gold": _s(r["field_vs_gold"]),
                "format_ok_runs": r["format_ok_runs"],
                "missing_fields": _s(r["missing_fields"]),
                "suspected_fabrication": _s(r["suspected_fabrication"]),
                "raw_variants": " ||| ".join(r["raw_variants"]),
            })


def write_review_csv(results: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding=config.CSV_ENCODING) as fh:
        w = csv.DictWriter(fh, fieldnames=config.REVIEW_FIELDS)
        w.writeheader()
        for r in results:
            for c in r["checks"]:
                chk = c["check"]
                w.writerow({
                    "case_id": r["case_id"],
                    "run_index": c["run_index"],
                    "format_ok": "是" if (chk and chk["format_ok"]) else "否",
                    "format_issues": _s(chk["format_issues"]) if chk else _s([f"调用失败: {c['error']}" if c["error"] else "无有效输出"]),
                    "missing_fields": _s(chk["missing_fields"]) if chk else _s(config.SIX_FIELDS),
                    "invalid_enum": _s(chk["invalid_enum"]) if chk else "无",
                    "nonverbatim_fields": _s(chk["nonverbatim_fields"]) if chk else "无",
                    "suspected_fabrication": _s(chk["suspected_fabrication"]) if chk else "无法判定",
                    "raw_response": c["raw"],
                })


# ---------------------------------------------------------------- 输出：人工核对表.md
def _md(s) -> str:
    """转义 Markdown 表格单元格里的管道符与换行。"""
    t = "" if s is None else str(s)
    return t.replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def write_human_review_md(results: list[dict], paths: list[Path], path: Path) -> None:
    lines: list[str] = []
    A = lines.append
    A("# 医保飞检通报「六要素」抽取 — 人工核对表")
    A("")
    A(f"> 生成时间：{datetime.now().isoformat(timespec='seconds')}  ")
    A(f"> 数据文件：{'、'.join(p.name for p in paths)}  ")
    A(f"> 模型：`{config.MODEL_NAME}` ｜ temperature = {config.TEMPERATURE}  ")
    A("> 本表由 `evaluate.py` 依据机器校验结果**预填**，请人工逐条复核后填写「人工复核结论」「复核人」「备注」三列。")
    A("")
    A("## 一、判定口径")
    A("")
    A("| 检查项 | 判定规则 |")
    A("| --- | --- |")
    A(f"| 格式是否合规 | 输出可被解析为纯 JSON 对象；键集合恰为六要素 {config.SIX_FIELDS}；每个值都是字符串；无 ```json 代码块、无多余解释文本；占位符只写「{config.UNMENTIONED}」 |")
    A("| 字段是否缺失 | 存在缺键、空字符串，或使用了「无 / 未提供 / 不详 / N/A / 未知」等非标准占位写法 |")
    A(f"| 有无编造 | ① 原文摘录字段（机构/时间/地区/金额）的值不是原文连续片段；② 枚举字段（违规类型/结果）不在闭集内；③ 人工标注为「{config.UNMENTIONED}」的字段却被填了具体值 |")
    A("| 三次一致 | 同一条记录三次运行的六字段值完全相同；否则列出差异字段与一致率 |")
    A("")
    A("## 二、逐条核对汇总表（每条记录一行，共 10 条）")
    A("")
    A("| 编号 | 原文（脱敏） | 三次一致 | 格式是否合规 | 字段是否缺失 | 有无编造 | 与标注一致 | 人工复核结论 | 复核人 | 备注 |")
    A("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in results:
        cid = r["case_id"]
        consistency = (
            f"{r['status']}（一致率 {r['consistency_rate']}）"
            if r["diff_fields"] else f"{r['status']}"
        )
        fmt = f"{r['format_ok_runs']}/{r['total_runs']} 合规" + (
            " ✓" if r["format_ok_runs"] == r["total_runs"] else " ✗"
        )
        missing = "无" if not r["missing_fields"] else "、".join(r["missing_fields"])
        fab = "无" if not r["suspected_fabrication"] else "有（" + "、".join(r["suspected_fabrication"]) + "）"
        gold_match = r["match_gold"] + (
            "" if r["match_gold"] == "是" else "（差异：" + "、".join(r["field_vs_gold"]) + "）"
        )
        A(f"| {cid} | {_md(_source_snippet(cid))} | {_md(consistency)} | {_md(fmt)} | "
          f"{_md(missing)} | {_md(fab)} | {_md(gold_match)} |  |  |  |")
    A("")
    A("## 三、逐次明细（每条记录 × 每次运行）")
    A("")
    A("| 编号 | 轮次 | 来源 | 格式是否合规 | 字段是否缺失 | 有无编造 | 具体说明 | 原始输出 |")
    A("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in results:
        for c in r["checks"]:
            chk = c["check"]
            if chk is None:
                A(f"| {r['case_id']} | {c['run_index']} | {c['source_file']} | 否 | 全部 | 无法判定 | "
                  f"{_md('调用失败: ' + c['error'] if c['error'] else '无有效输出')} |  |")
                continue
            notes = []
            notes.extend(chk["format_issues"])
            if chk["missing_fields"]:
                notes.append("字段缺失：" + "、".join(chk["missing_fields"]))
            if chk["invalid_enum"]:
                notes.append("枚举越界：" + "、".join(chk["invalid_enum"]))
            if chk["nonverbatim_fields"]:
                notes.append("非原文摘录：" + "、".join(chk["nonverbatim_fields"]))
            if chk.get("gold_fabrication"):
                notes.append("标注为未提及却填值：" + "、".join(chk["gold_fabrication"]))
            A(f"| {r['case_id']} | {c['run_index']} | {c['source_file']} | "
              f"{'是' if chk['format_ok'] else '否'} | "
              f"{_md('无' if not chk['missing_fields'] else '、'.join(chk['missing_fields']))} | "
              f"{_md('无' if not chk['suspected_fabrication'] else '、'.join(chk['suspected_fabrication']))} | "
              f"{_md('；'.join(notes) or '无')} | {_md(c['raw'])} |")
    A("")
    # ---- 汇总统计 ----
    total = len(results)
    all_consistent = sum(1 for r in results if r["status"] == "全部一致")
    avg_rate = round(sum(r["consistency_rate"] for r in results) / total, 4) if total else 0
    gold_hit = sum(1 for r in results if r["match_gold"] == "是")
    fully_formatted = sum(1 for r in results if r["format_ok_runs"] == r["total_runs"])
    any_missing = sum(1 for r in results if r["missing_fields"])
    any_fab = sum(1 for r in results if r["suspected_fabrication"])
    A("## 四、汇总统计")
    A("")
    A("| 指标 | 数值 |")
    A("| --- | --- |")
    A(f"| 记录数 | {total} |")
    A(f"| 三次全部一致 | {all_consistent}/{total} |")
    A(f"| 平均一致率 | {avg_rate:.2%} |")
    A(f"| 与人工标注完全一致 | {gold_hit}/{total} |")
    A(f"| 全部轮次格式合规的记录 | {fully_formatted}/{total} |")
    A(f"| 存在字段缺失的记录 | {any_missing}/{total} |")
    A(f"| 存在编造嫌疑的记录 | {any_fab}/{total} |")
    A("")
    A("## 五、人工复核签署")
    A("")
    A("- 复核人：____________　复核日期：____________")
    A("- 复核结论：□ 全部通过　□ 需修正提示词/枚举　□ 需更换模型　□ 其他：____________")
    A("- 备注：____________________________________________________")
    A("")
    A("---")
    A("")
    A("> 说明：数据为「据公开通报改写·虚构脱敏」的教学样本，不含真实机构与个人信息。"
      " 机器预填结论仅供复核参考，最终以人工签署为准。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


_CASE_TEXT = {}


def _source_snippet(case_id: str) -> str:
    if not _CASE_TEXT:
        from mock_data import MOCK_CASES
        for c in MOCK_CASES:
            _CASE_TEXT[c["case_id"]] = c["source_text"]
    text = _CASE_TEXT.get(case_id, "")
    return text if len(text) <= 60 else text[:60] + "…"


# ---------------------------------------------------------------- 主流程
def main() -> None:
    parser = argparse.ArgumentParser(description="三遍结果一致性对比 + 人工核对表生成")
    parser.add_argument("--inputs", nargs="*", default=[],
                        help="显式指定结果 CSV（可多个）；不传则自动发现")
    parser.add_argument("--expected-runs", type=int, default=3,
                        help="期望的每用例运行次数（默认 3）")
    args = parser.parse_args()

    paths = discover_inputs(args.inputs)
    if not paths:
        raise SystemExit(
            "没有找到结果 CSV。请先运行：\n"
            "  python main.py --times 3\n"
            "或分别跑三遍：\n"
            "  python main.py --times 1 --output result.csv\n"
            "  python main.py --times 1 --output result2.csv\n"
            "  python main.py --times 1 --output result3.csv"
        )

    rows = load_rows(paths)
    by_case: dict[str, list[dict]] = {}
    for row in rows:
        by_case.setdefault(_clean(row.get("case_id")), []).append(row)

    results = [evaluate_one(cid, by_case[cid], args.expected_runs) for cid in sorted(by_case)]

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    write_evaluation_csv(results, config.EVALUATION_CSV)
    write_review_csv(results, config.REVIEW_CSV)
    write_human_review_md(results, paths, config.HUMAN_REVIEW_MD)

    # ---- 控制台汇总 ----
    print(f"输入文件：{'、'.join(p.name for p in paths)}")
    print(f"共 {len(results)} 条记录，逐条状态：")
    for r in results:
        print(f"  {r['case_id']}: {r['status']:<8} 一致率={r['consistency_rate']:.2f} "
              f"格式合规={r['format_ok_runs']}/{r['total_runs']} "
              f"标注命中={r['match_gold']} "
              f"差异字段={_s(r['diff_fields'])} "
              f"编造嫌疑={_s(r['suspected_fabrication'])}")
    total = len(results)
    all_consistent = sum(1 for r in results if r["status"] == "全部一致")
    avg_rate = round(sum(r["consistency_rate"] for r in results) / total, 4) if total else 0
    gold_hit = sum(1 for r in results if r["match_gold"] == "是")
    print(f"\n汇总：三次全部一致 {all_consistent}/{total}，平均一致率 {avg_rate:.2%}，"
          f"与标注完全一致 {gold_hit}/{total}")
    print(f"已写出：\n  {config.EVALUATION_CSV}\n  {config.REVIEW_CSV}\n  {config.HUMAN_REVIEW_MD}")


if __name__ == "__main__":
    main()
