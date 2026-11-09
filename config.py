# -*- coding: utf-8 -*-
"""
项目配置：模型与调用参数、六要素字段契约、输出路径。

【重要】API Key 等敏感信息一律从环境变量读取，禁止写死在本文件里，
否则会被提交到 git 仓库。本地开发请在本目录新建 .env（已被 .gitignore 忽略）：

    LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
    LLM_BASE_URL=http://127.0.0.1:7863/v1
    LLM_MODEL_NAME=cn:hy4free
    LLM_RUN_TIMES=3

本项目的任务：从一条「医保基金飞行检查通报（虚构脱敏）」里抽取六要素，输出严格 JSON。
六要素、枚举闭集、未提及占位符统一定义在本文件，供 prompts.py（拼提示词）、
mock_data.py（人工标注）、schema.py（校验）、evaluate.py（评估）共用，
避免「提示词里写一套、校验时又写一套」造成漂移。

若只想验证流程（不调用真实模型），给 main.py 加 --dry-run 即可，无需任何配置。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass  # 未安装 python-dotenv 时退化成只读系统环境变量

# ---------- 六要素字段契约（顺序固定；CSV / JSON / 提示词 / 校验共用） ----------
SIX_FIELDS = ["机构", "时间", "地区", "违规类型", "金额", "结果"]

UNMENTIONED = "未提及"  # 原文没有该信息时的唯一占位符

# 原文摘录类字段：值必须是原文里出现过的片段，禁止改写 / 换算 / 推断
VERBATIM_FIELDS = ["机构", "时间", "地区", "金额"]
# 枚举类字段：值必须逐字命中下面的闭集（或 UNMENTIONED）
ENUM_FIELDS = ["违规类型", "结果"]

# 违规类型闭集（演示用子集，可按业务扩展；扩展时同步改 prompts.py 的归并说明）
VIOLATION_TYPES = [
    "重复收费",
    "串换项目",
    "虚构医疗服务",
    "超标准收费",
    "分解收费",
    "过度诊疗",
    "诱导住院",
    "冒名就医",
    "重复入院",
]

# 处理结果闭集
RESULTS = [
    "追回医保基金",
    "追回并罚款",
    "责令整改",
    "暂停医保结算",
    "解除医保协议",
    "移送司法机关",
    "通报批评",
]

ENUMS = {
    "违规类型": VIOLATION_TYPES,
    "结果": RESULTS,
}

# ---------- 模型与凭据 ----------
API_KEY = os.getenv("LLM_API_KEY", "").strip()
BASE_URL = os.getenv("LLM_BASE_URL", "").strip() or None
MODEL_NAME = os.getenv("LLM_MODEL_NAME", "cn:hy4free").strip()

# ---------- 调用参数 ----------
TEMPERATURE = 0.0          # 任务要求：固定为 0，尽量消除随机性
# 给足输出预算：推理型模型（如本网关的 cn:hy4free，实际后端 hy4）会先把
# token 消耗在 reasoning_tokens 上，512 时会出现 finish_reason=length、
# message.content='' 的「空正文」现象，导致校验器只看到空响应却无 error。
# 4096 对普通模型只是上限（实际只生成几十 token），无副作用；兼容推理型模型。
MAX_TOKENS = 4096
REQUEST_TIMEOUT = 60       # 秒
MAX_RETRIES = 3
RETRY_BACKOFF = 2          # 退避基数：1s、2s、4s
RUN_TIMES = int(os.getenv("LLM_RUN_TIMES", "3"))

# ---------- 输出 ----------
OUTPUT_DIR = BASE_DIR / "results"
OUTPUT_CSV = OUTPUT_DIR / "result.csv"
EVALUATION_CSV = OUTPUT_DIR / "evaluation.csv"
REVIEW_CSV = OUTPUT_DIR / "review_auto.csv"
HUMAN_REVIEW_MD = BASE_DIR / "人工核对表.md"
CSV_ENCODING = "utf-8-sig"  # 带 BOM，Excel 直接打开中文不乱码

# result.csv：每条用例 × 每次运行一行
CSV_FIELDS = [
    "case_id", "run_index", "source_text", "model", "raw_response",
    *SIX_FIELDS, "format_ok", "latency_ms", "timestamp", "error",
]

# evaluation.csv：每条用例一行（三次结果对比 + 与人工标注对照）
EVAL_FIELDS = [
    "case_id", "valid_runs", "status", "consistency_rate", "diff_fields",
    "field_agreement", "consensus", "gold", "match_gold", "field_vs_gold",
    "format_ok_runs", "missing_fields", "suspected_fabrication", "raw_variants",
]

# review_auto.csv：逐次运行的机器核对明细（人工核对表的数据来源）
REVIEW_FIELDS = [
    "case_id", "run_index", "format_ok", "format_issues", "missing_fields",
    "invalid_enum", "nonverbatim_fields", "suspected_fabrication", "raw_response",
]


def validate_config() -> None:
    """在真正调用模型前检查必要配置，给出可读的报错。"""
    missing = []
    if not API_KEY:
        missing.append("LLM_API_KEY")
    if not MODEL_NAME:
        missing.append("LLM_MODEL_NAME")
    if missing:
        raise RuntimeError(
            "缺少配置项：" + "、".join(missing)
            + "。请在本目录创建 .env 文件（格式见 config.py 顶部注释）或设置系统环境变量；"
              "若只想验证流程，可加 --dry-run 参数。"
        )
