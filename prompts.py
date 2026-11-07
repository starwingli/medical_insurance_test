# -*- coding: utf-8 -*-
"""
提示词：医保飞检通报「六要素」结构化抽取。

系统提示里必须同时写清三件事（任务要求）：
  1. 格式要求 —— 只输出一个 JSON 对象，六个字段、字段名逐字固定、值一律字符串，
     不包裹 ```json、不加解释、不加多余字段。
  2. 枚举值 —— 「违规类型」「结果」只能取闭集里的值（闭集来自 config.ENUMS），
     不得自造新词、不得写近义表述。
  3. 「未提及」规则 —— 原文没有出现的信息，唯一合法写法是 未提及；
     严禁推断、补全、换算、编造。

另外补充两条防编造约束：
  - 机构/时间/地区/金额 必须是原文里出现过的连续片段（逐字摘录），
    禁止把「3.6万元」改写成「36000元」、把「某市」补成具体城市。
  - 原文提到但不在闭集里的违规/处理表述，按「最接近的闭集值」归并；
    原文完全没提某字段，才填 未提及。

这些常量都从 config.py 读取并在下面渲染进提示词，
避免「提示词写一套、校验写另一套」的漂移。
"""
from __future__ import annotations

import json

import config

# ---------- 系统提示（动态渲染，枚举/字段/占位符均取自 config.py） ----------
_FIELD_LINES = []
for _f in config.SIX_FIELDS:
    if _f in config.ENUM_FIELDS:
        _opts = " / ".join(config.ENUMS[_f])
        _FIELD_LINES.append(f'  "{_f}": "枚举值，只能取：{_opts}；或 {config.UNMENTIONED}"')
    elif _f in config.VERBATIM_FIELDS:
        _FIELD_LINES.append(f'  "{_f}": "原文逐字摘录的片段；或 {config.UNMENTIONED}"')
    else:
        _FIELD_LINES.append(f'  "{_f}": "字符串；或 {config.UNMENTIONED}"')
_FIELD_BLOCK = ",\n".join(_FIELD_LINES)

_VIOLATION_LIST = "、".join(config.VIOLATION_TYPES)
_RESULT_LIST = "、".join(config.RESULTS)

SYSTEM_PROMPT = f"""你是一名医保基金监管领域的信息抽取助手。
你的任务：从用户给出的「一条医保基金飞行检查通报（已经脱敏、可能不完整）」中，抽取固定的六个要素，输出严格的 JSON。

【输出格式要求 —— 必须全部满足】
1. 只输出一个 JSON 对象，不要输出任何解释、前后缀、注释或 Markdown 代码块（不要写 ```json）。
2. 对象必须且只能包含以下六个键，键名逐字固定、顺序不限：
{", ".join(config.SIX_FIELDS)}
3. 每个键的值都必须是「字符串」，不能是数字、数组、对象、null 或布尔值。
4. 不要输出这六个键之外的任何字段。
5. 输出必须是合法 JSON（双引号、逗号、花括号齐全），可被 json.loads 直接解析。

【字段取值规则】
{_FIELD_BLOCK}

【枚举闭集（逐字匹配，不得自造新词、不得用近义词替换）】
- 违规类型 只能取：{_VIOLATION_LIST}
- 结果 只能取：{_RESULT_LIST}
- 若原文出现了闭集之外的表述，请归并为语义最接近的闭集值；例如：
  「串换药品耗材」「串换诊疗项目」→ 串换项目；
  「重复检查」「过度检查」「无指征检查」→ 过度诊疗；
  「分解住院」「短时间重复入院」→ 重复入院；
  「罚款」「处违约金」→ 追回并罚款；
  「约谈」「限期整改」「要求整改」→ 责令整改。
- 若原文只描述违规事实、完全没有提到任何处理结果，结果 填 {config.UNMENTIONED}。

【「未提及」规则 —— 最重要】
- 只要该信息没有在原文里明确出现，必须填唯一占位符「{config.UNMENTIONED}」。
- 严禁根据常识、上下文、机构类型去推断或补全（例如原文只写「某市」，不得补出具体省市名；
  原文没写金额，不得估算；原文没写处理决定，不得假设「责令整改」）。
- {config.UNMENTIONED} 必须原样写这四个字，不要写成「无」「未提供」「不详」「N/A」「未知」或留空字符串。

【原文摘录字段的防编造约束】
- 机构 / 时间 / 地区 / 金额 四个字段，值必须是原文中出现过的「连续片段」，逐字照抄。
- 禁止改写：不得把「3.6万元」写成「36000元」或「3.6 万元」，不得把「二〇二三年」写成「2023年」，
  不得把「华东某省」扩写成具体省份，不得给机构名加/减「市」「院」等字。
- 若原文里该信息是泛化/脱敏写法（如「某市某区人民医院」「华东某省」「约 12 万元」），
  就原样摘录这个泛化片段，不要试图还原成真实名称。
- 一条通报里出现多个机构/金额时，取与违规事实直接相关的那个主要片段。

【输出示例骨架（字段名与值类型示意，实际值必须来自原文）】
{{"机构":"...","时间":"...","地区":"...","违规类型":"...","金额":"...","结果":"..."}}

请严格遵守：宁可填 {config.UNMENTIONED}，也绝不编造。"""


# ---------- 2 条 few-shot 示例（任务要求） ----------
# 示例 1：六要素齐全，演示完整抽取 + 枚举归并 + 逐字摘录。
# 示例 2：信息不全，演示「未提及」规则（缺 金额 / 结果 / 地区）。
FEW_SHOT_EXAMPLES = [
    {
        "case_id": "DEMO1",
        "source_text": (
            "2024年3月，华东某省医保局在对某市第一人民医院开展飞行检查时发现，"
            "该院2023年度存在串换药品耗材、将自费项目串换为医保目录内项目计费的行为，"
            "涉及医保基金约 12.6 万元。当地医保部门已追回违规使用的医保基金，并按协议处以等额违约金，"
            "同时要求该院限期整改。"
        ),
        "output": {
            "机构": "某市第一人民医院",
            "时间": "2024年3月",
            "地区": "华东某省",
            "违规类型": "串换项目",
            "金额": "约 12.6 万元",
            "结果": "追回并罚款",
        },
    },
    {
        "case_id": "DEMO2",
        "source_text": (
            "近日，某市医保分局对某镇中心卫生院进行现场检查，"
            "查实该院存在重复收费问题，已要求相关科室作出书面说明。"
        ),
        "output": {
            "机构": "某镇中心卫生院",
            "时间": "近日",
            "地区": "未提及",
            "违规类型": "重复收费",
            "金额": "未提及",
            "结果": "未提及",
        },
    },
]


USER_PROMPT_TEMPLATE = "请从下面这条医保飞检通报中抽取六要素，只输出 JSON：\n\n{source_text}"


def build_messages(case: dict, include_few_shot: bool = True) -> list[dict]:
    """
    组装 OpenAI chat 格式的 messages。

    case 至少包含 source_text 键（见 mock_data.MOCK_CASES）。
    include_few_shot=True 时，在系统提示之后插入 2 条 few-shot 的 user/assistant 对话。
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if include_few_shot:
        for ex in FEW_SHOT_EXAMPLES:
            messages.append({
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(source_text=ex["source_text"]),
            })
            # assistant 回复必须是「纯 JSON 字符串」，与真实输出格式完全一致
            messages.append({
                "role": "assistant",
                "content": json.dumps(ex["output"], ensure_ascii=False),
            })
    messages.append({
        "role": "user",
        "content": USER_PROMPT_TEMPLATE.format(source_text=case["source_text"]),
    })
    return messages
