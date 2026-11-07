# -*- coding: utf-8 -*-
"""
响应校验：把模型的原始输出解析成六要素，逐项检查格式 / 枚举 / 原文摘录 / 编造嫌疑。

单独成模块的原因：main.py（写 result.csv）、evaluate.py（对比与人工核对表）、
测试脚本都要用同一套判定逻辑，避免口径不一致。

判定口径（与 prompts.SYSTEM_PROMPT、config.py 的契约完全对齐）：
  format_ok —— 合法纯 JSON 对象、且键集合恰为六要素、且每个值都是字符串。
  missing_fields —— 缺键 / 空字符串 / 用了非标准占位符（无、未提供、N/A、未知…）。
  invalid_enum —— 枚举类字段取值不在 config.ENUMS 闭集内。
  nonverbatim_fields —— 原文摘录类字段的值不是原文连续片段。
  suspected_fabrication —— 上面两类里「凭空造出一个值」的字段；
     另外 evaluate.py 会用 gold 补一条：gold=未提及 而模型填了具体值，也算编造。
"""
from __future__ import annotations

import json
import re

import config

# 常见的「不规范占位写法」：出现这些应判为格式不合规（规则要求只能写 未提及）
_BAD_PLACEHOLDERS = {"", "无", "未提供", "不详", "未知", "N/A", "n/a", "NA", "None", "null", "-", "—"}


def extract_json(text: str) -> tuple[dict | None, str]:
    """
    从模型原始输出里尽力解析出第一个 JSON 对象。
    返回 (parsed_dict_or_None, matched_json_text)。
    容忍 ```json 代码块包裹；解析失败则返回 (None, "")。
    """
    if not text:
        return None, ""
    s = text.strip()
    # 去掉 Markdown 代码块围栏
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.S | re.I)
    if fenced:
        s = fenced.group(1).strip()
    # 首选：整段就是 JSON
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj, s
    except (ValueError, TypeError):
        pass
    # 兜底：截取第一个 {...} 块
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj, m.group(0)
        except (ValueError, TypeError):
            pass
    return None, ""


def validate_response(raw: str, source_text: str) -> dict:
    """
    校验一条模型输出。返回结构见模块顶部说明。
    """
    issues: list[str] = []
    missing: list[str] = []
    invalid_enum: list[str] = []
    nonverbatim: list[str] = []
    fabrication: list[str] = []

    obj, matched = extract_json(raw)
    if obj is None:
        issues.append("无法解析为合法 JSON 对象")
        return {
            "values": {f: "" for f in config.SIX_FIELDS},
            "format_ok": False,
            "format_issues": issues,
            "missing_fields": list(config.SIX_FIELDS),
            "invalid_enum": [],
            "nonverbatim_fields": [],
            "suspected_fabrication": [],
        }

    # 代码块 / JSON 之外的多余文本
    stripped = raw.strip()
    if fenced := re.match(r"^```(?:json)?\s*.*?\s*```$", stripped, re.S | re.I):
        issues.append("输出被 Markdown 代码块(```json)包裹")
    elif matched and stripped != matched:
        issues.append("JSON 之外存在多余的解释性文本")

    # 键集合
    keys = set(obj.keys())
    expected = set(config.SIX_FIELDS)
    if keys != expected:
        extra = sorted(keys - expected)
        lack = sorted(expected - keys)
        if extra:
            issues.append(f"存在多余字段：{extra}")
        if lack:
            issues.append(f"缺少字段：{lack}")

    values: dict[str, str] = {}
    for f in config.SIX_FIELDS:
        if f not in obj:
            values[f] = ""
            missing.append(f)
            continue
        v = obj[f]
        if not isinstance(v, str):
            issues.append(f"{f} 的值类型应为字符串，实际为 {type(v).__name__}")
            v = "" if v is None else str(v)
        values[f] = v

        if v in _BAD_PLACEHOLDERS or v.strip() == "":
            if v == "" or v.strip() == "":
                issues.append(f"{f} 为空字符串，应填 {config.UNMENTIONED}")
            else:
                issues.append(f"{f} 使用了非标准占位写法 {v!r}，应填 {config.UNMENTIONED}")
            missing.append(f)

        # 枚举闭集
        if f in config.ENUM_FIELDS and v not in _BAD_PLACEHOLDERS and v.strip() != "":
            if v != config.UNMENTIONED and v not in config.ENUMS[f]:
                invalid_enum.append(f)
                issues.append(f"{f}={v!r} 不在枚举闭集 {config.ENUMS[f]} 内")
                fabrication.append(f)

        # 原文逐字摘录
        if f in config.VERBATIM_FIELDS and v not in _BAD_PLACEHOLDERS and v.strip() != "":
            if v != config.UNMENTIONED and v not in source_text:
                nonverbatim.append(f)
                issues.append(f"{f}={v!r} 不是原文连续片段（疑似改写/编造）")
                fabrication.append(f)

    return {
        "values": values,
        "format_ok": len(issues) == 0,
        "format_issues": issues,
        "missing_fields": sorted(set(missing)),
        "invalid_enum": sorted(set(invalid_enum)),
        "nonverbatim_fields": sorted(set(nonverbatim)),
        "suspected_fabrication": sorted(set(fabrication)),
    }
