# 医保飞检通报「六要素」结构化抽取 — 稳定性测试

从一条「医保基金飞行检查通报（虚构脱敏）」里抽取固定的六个要素，输出严格 JSON；
用 `temperature=0` 跑三遍，对比三次结果是否一致，并逐条人工核对
**格式是否合规 / 字段是否缺失 / 有无编造**。

---

## 一、六要素与判定契约

| 字段 | 类型 | 取值规则 |
| --- | --- | --- |
| 机构 | 原文摘录 | 必须是通报原文里出现过的连续片段，逐字照抄；或 `未提及` |
| 时间 | 原文摘录 | 同上（如 `2024年3月`、`近日`，不得换算） |
| 地区 | 原文摘录 | 同上（如 `华东某省`；机构名里的「某市」不算地区） |
| 违规类型 | 枚举 | 闭集：`重复收费 / 串换项目 / 虚构医疗服务 / 超标准收费 / 分解收费 / 过度诊疗 / 诱导住院 / 冒名就医 / 重复入院`；或 `未提及` |
| 金额 | 原文摘录 | 必须是原文片段（如 `约 12.6 万元`）；或 `未提及` |
| 结果 | 枚举 | 闭集：`追回医保基金 / 追回并罚款 / 责令整改 / 暂停医保结算 / 解除医保协议 / 移送司法机关 / 通报批评`；或 `未提及` |

三条硬规则写死在 [prompts.py](prompts.py) 的系统提示里，并由 [schema.py](schema.py) 用同一套常量校验：

1. **格式要求** — 只输出一个纯 JSON 对象，六个键逐字固定、值一律字符串，不包裹 ```json、不加解释、不加多余字段。
2. **枚举值** — 两个枚举字段只能逐字取闭集内的值；原文里的近义表述（串换药品耗材→串换项目、限期整改→责令整改、重复检查→过度诊疗 等）按提示里的归并表映射。
3. **「未提及」规则** — 原文没明确出现的信息，唯一合法写法是 `未提及`；严禁推断、补全、换算、编造。写成「无 / 未提供 / 不详 / N/A / 未知 / 空串」一律判为格式不合规。

> 这些常量集中在 [config.py](config.py)（`SIX_FIELDS` / `ENUMS` / `UNMENTIONED` / `VERBATIM_FIELDS`），
> 提示词、测试数据标注、校验器、评估器共用，**不会出现「提示词一套、校验另一套」的漂移**。

## 二、目录结构

```
medical_insurance_test/
├── config.py          # 配置 + 六要素/枚举/占位符契约（单一事实来源）
├── prompts.py         # 系统提示（格式/枚举/未提及规则）+ 2 条 few-shot
├── mock_data.py       # 10 条脱敏飞检记录 + gold 六要素人工标注
├── schema.py          # 响应解析与校验：格式 / 缺失 / 枚举 / 原文摘录 / 编造嫌疑
├── main.py            # 批量脚本：for 循环 10 条 × N 次，temperature=0，写 CSV
├── evaluate.py        # 三遍一致性对比 + 生成人工核对表
├── .env.example       # 环境变量模板
├── .gitignore         # 忽略 .env / results/ / *.csv / legacy_qa/ 等
├── requirements.txt   # 依赖
├── 人工核对表.md       # ★ 任务 6 交付物（机器预填 + 人工复核栏），放在根目录不忽略
├── legacy_qa/         # 重构前「问答版」源码备份（本地留存，已忽略）
└── results/           # 运行产物（已忽略）
    ├── result.csv          # 第 1 遍（或 --times 3 单文件）
    ├── result2.csv         # 第 2 遍
    ├── result3.csv         # 第 3 遍
    ├── evaluation.csv      # 一致性评估
    └── review_auto.csv     # 逐次机器核对明细
```

## 三、数据来源说明

本仓库**不含任何真实机构、真实患者、真实个人信息**，也不直接复制真实通报原文。

外部搜索通道在本环境不可用（`DEEPSEEK_API_KEY` 未配置，国家医保局站点返回 404，
Bing 检索结果被过滤且无关），因此 10 条记录是依据国家医保局及各地医保部门公开
飞行检查通报中**反复出现的典型问题类型**（重复收费、串换项目、虚构医疗服务、
超标准收费、分解收费、过度诊疗、诱导住院、冒名就医、重复入院等）**自行改写**的教学样本：

- 机构名泛化为「某市某医院 / 某定点医疗机构」；
- 地区泛化为「华东某省 / 华北某省」等；
- 时间、金额均为虚构数值；
- 每条标注 `据公开通报改写·虚构脱敏`。

其中 `FJ003 / FJ004 / FJ008 / FJ010` 故意缺字段，`FJ010` 极简稀疏（只有机构+违规类型），
专门用来检验模型是否会**脑补**出没写的信息。

## 四、快速开始

```powershell
# 0) 安装依赖
python -m pip install -r requirements.txt

# 1) 复制模板为 .env，填上真实 API Key 和「服务端真实存在的模型 id」
Copy-Item .env.example .env

# 2) 核对模型名（很关键，见下方警告）
python main.py --list-models

# 3) 验证全流程（零依赖、零 token，不调用模型）
python main.py --dry-run --times 3
python evaluate.py

# 4) 真实跑三遍（temperature 固定为 0）
python main.py --times 1 --output result.csv
python main.py --times 1 --output result2.csv
python main.py --times 1 --output result3.csv

# 5) 三次一致性对比 + 生成人工核对表
python evaluate.py
# → results/evaluation.csv、results/review_auto.csv、人工核对表.md
```

> PowerShell 控制台中文可能乱码，先执行 `$env:PYTHONIOENCODING='utf-8'`。
> CSV 用 `utf-8-sig`（带 BOM），Excel 直接打开中文不乱码。

### ⚠️ 模型名必须填服务端真实存在的 id

部分网关对「模型 id 不存在」**不返回 404**，而是含糊返回
`503 all accounts are temporarily unavailable, please retry later`，
导致重试全部无效。`main.py` 启动时会调用 `/models` 做预检并秒级报错：

```
配置错误：服务端不存在模型 'deepseek-chat'。
  可用模型（共 35 个，列出前 15 个）： ...
```

本环境网关的模型 id 全部带 `global:` 前缀（`global:default-model` / `global:hy4free` / …）。
**换 `LLM_BASE_URL` 后必须重新核对模型 id**。真·临时故障由 `MAX_RETRIES=3` +
退避 1s/2s/4s 兜底，失败写 `error` 列，评估标 `运行次数不足`，不污染一致性统计。

### ⚠️ 推理模型与 `MAX_TOKENS`

`hy4free` / `hy4*` 系列是**推理型**模型，会先把 token 消耗在 `reasoning_tokens` 上。
若 `config.MAX_TOKENS` 太小（如 512），会出现 `finish_reason=length`、
`message.content=''` 的「调用成功却无正文」——CSV 里 `raw_response` 和 `error` 都为空的幽灵行。
`config.MAX_TOKENS` 已设为 `4096` 兼容推理型模型（普通模型只生成几十 token，无副作用）；
`main.py` 另外对空正文做硬检测并转成可重试错误，在 `error` 列留下诊断线索。
若仍出现该现象，优先调大 `MAX_TOKENS`，或改用 `global:default-model` / `*-flash` 等非推理模型。

## 五、参数

| 参数 | 说明 |
| --- | --- |
| `--times N` | 每个用例运行次数，默认取 `config.RUN_TIMES`（3） |
| `--dry-run` | 不调模型，用本地确定性桩；桩里故意在 FJ010 第 3 次编造时间、FJ003 第 2 次写枚举外的值，用来验证「编造/不一致」判定链路 |
| `--cases FJ001,FJ010` | 只跑指定用例 |
| `--output result2.csv` | 输出文件名（位于 `results/` 下），配合 `--times 1` 跑三遍 |
| `--list-models` | 只打印服务端可用模型 id 后退出 |
| `--skip-preflight` | 跳过模型预检（服务不提供 `/models` 时用） |
| `evaluate.py --inputs ...` | 显式指定多个结果 CSV |
| `evaluate.py --expected-runs 3` | 期望每用例运行次数（默认 3） |

## 六、评估与人工核对口径

| 检查项 | 判定规则 |
| --- | --- |
| **三次一致** | 同一条记录三次运行的六字段值完全相同；否则列出差异字段、逐字段一致率、多数投票 `consensus` |
| **格式是否合规** | 纯 JSON 对象 + 键集合恰为六要素 + 值全为字符串 + 无代码块/多余文本 + 占位符只写 `未提及` |
| **字段是否缺失** | 缺键、空字符串，或用了「无 / 未提供 / 不详 / N/A / 未知」等非标准占位写法 |
| **有无编造** | ① 原文摘录字段的值不是原文连续片段（改写/换算/脑补）；② 枚举字段不在闭集内；③ 人工标注为 `未提及` 的字段却被填了具体值 |
| **与标注一致** | `consensus` 与 `mock_data.py` 里的人工标注 `gold` 逐字段比对 |

`evaluate.py` 会输出：

- `results/evaluation.csv` — 每条记录一行：有效轮次、状态、一致率、差异字段、逐字段一致率、consensus、与 gold 对比、编造嫌疑；
- `results/review_auto.csv` — 每条记录 × 每次运行一行：格式/缺失/枚举/非原文摘录/编造嫌疑/原始输出；
- **`人工核对表.md`** — 任务 6 交付物：判定口径说明、10 条逐条汇总表（三列检查项 + 人工复核结论/复核人/备注空栏）、逐次明细、汇总统计、人工复核签署栏。

> 核对表由机器**预填**，最终以人工复核签署为准。核对重点是 `FJ010`（极简样本是否脑补）、
> `FJ004`（机构名里的「某市」有没有被拿来补「地区」）、`FJ003`（金额缺失有没有被编造）。

## 七、安全说明

- `LLM_API_KEY` 只从 `.env` / 环境变量读取，`.env`、`.env.*`、`*.pem`、`*.key`、`secrets/` 均被 `.gitignore` 忽略；
- 运行产物 `results/`、`*.csv` 均忽略；`legacy_qa/`（旧版备份）也忽略；
- `人工核对表.md` 是交付物，故意**不忽略**；
- 提交前建议自查：`git status --short` 与 `git check-ignore -v .env results/result.csv`。
