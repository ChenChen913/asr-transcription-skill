---
name: asr-transcription
description: This skill should be used when converting audio or video into readable Chinese transcripts on a local or offline Windows machine — extracting an embedded text subtitle track, reading burned-in subtitles with OCR, cross-checking what was heard against what was shown, choosing an ASR engine (faster-whisper turbo / large-v3 / SenseVoice GGUF), setting transcription parameters, building a batch pipeline, checking transcripts for hallucinations and missing segments, diagnosing GPU/DLL/encoding failures, or verifying model licenses and attribution. Trigger phrases include 转写、语音转文字、逐字稿、ASR、Whisper、SenseVoice、质检、幻觉、漏段、标点、字幕轨、内嵌字幕、硬字幕、OCR 字幕、听看对照、互校.
agent_created: true
---

# 音视频转写成可读文稿

## 目的

把多轮实测沉淀成可复用的执行流程：选方案 → 准备环境 → 统一抽音频 → 转写 → 交叉核对 → 质检 → 精编 → 留痕。核心结论是**两条通路、互相校验**：

- **听**（ASR）：覆盖全片、有断句，但同音字会错（`RAG`→`RAW`）。
- **看**（字幕轨 / 画面 OCR）：专名和数字常常更准，但会认错形近字（`懵逼`→`憎逼`），也可能根本没有字幕。
- 两者都不完美，所以既不能二选一，也不能揉成一份；正确做法是**逐段对照、只看分歧**。

主力方案仍是 faster-whisper turbo / GPU，另加一个纯 CPU 的异源方案做快速预览和第二意见；全部靠质检流程兜底。

## 何时使用

- 把一批视频或音频转成中文逐字稿
- 视频自带字幕轨，或字幕烧在画面里，想先试试不靠听写能拿到多少
- 已有语音稿和字幕稿，想知道哪几段两边说法不一致
- 在 Whisper 系与中文轻量模型之间选型，或评估是否值得补第二套引擎
- 配置 GPU 推理、下载模型、编写批处理脚本
- 排查异常结果：开头复读提示语、结尾冒出片尾话术、中间整段丢失、全文无标点
- 判断某份结果能不能宣称「准确率提升」
- 核对第三方模型或项目的归属与许可证

## 渐进式加载（三级）

1. 本文件的 `name` + `description` 常驻上下文，决定是否触发。
2. 触发后只读本文件（< 5k 词）：拿流程骨架与路由表。
3. **按需**再读 `references/` 下的单个文件；不要一次全读。长文件用 grep 定位，脚本可直接执行而不必读进上下文。

## 能力归属（别高估自动化）

| 类别 | 含义 | 对应环节 |
|---|---|---|
| **已自动化** | 脚本直接产出结果 | `check_env.py` 自检、`extract_subs.py` 抽字幕轨、`ocr_subs.py` 画面 OCR、`transcribe.py` 转写、`batch_transcribe.py` 批量转写、`merge_sources.py` 听看互校、`qc_check.py` 质检、`run_smoke.py` 等测试 |
| **Agent 工作流** | Skill 指导 Agent 调命令、判读结果、组织复核 | 抽音频、异源方案对照、精编留痕 |
| **手动** | 必须人工判断 | 首末段回听、**判定互校分歧里谁对**、建术语表、提供标准答案、商用许可核对 |

> 「文档写了完整流程」≠「工具已自动实现」。**SenseVoice q8 GGUF 只有文档命令、没有对应脚本**，是可选的独立对照方案。批处理已有脚本（`batch_transcribe.py`），但它只做串行四件事，不排队、不并发。详见 README「自动化到哪一步」表。

## 工作流骨架

| 步 | 动作 | 谁来做 | 需要时读取 |
|---|---|---|---|
| 0 | 回答开测四问：本轮要回答什么、判分依据在不在、口径红线是否定好、产物会不会覆盖旧结果 | 人工 / Agent | `references/02-workflow.md` |
| 1 | 环境自检（显卡、模型目录、CUDA DLL 文件、ffmpeg、版本锁），并**跑一句真实推理**确认 DLL 能加载 | 脚本 + 人工确认 | `references/06-environment.md`、`scripts/check_env.py` |
| 2a | **先探字幕轨**：有文本字幕轨（或同目录有 srt）就直接抽，省掉 2b–5；只认文本轨，PGS/硬字幕不在此列 | 脚本 | `references/02-workflow.md` §2.0、`scripts/extract_subs.py` |
| 2b | 用 FFmpeg 统一抽 16 kHz 单声道 WAV，所有方案共用同一份输入 | Agent 命令 | `references/02-workflow.md` |
| 2c | **没有字幕轨就 OCR**：`ocr_subs.py` 先探有没有会变的字幕带，有才逐帧读；带内的固定文案按变化节奏剔除、双语按 `--language` 取主语言；字幕在顶部、竖排、花字读不到 | 脚本 | `references/02-workflow.md` §2.1、`scripts/ocr_subs.py` |
| 3 | 主力转写：直接跑 `scripts/transcribe.py`（faster-whisper + turbo / GPU），GPU 失败自动回退 CPU，产出 qc_check 可读的 JSON + MD | 脚本 | `references/01-selection.md`、`references/03-parameters.md` |
| 4 | 可选：异源轻量方案跑一遍，作为快速预览 / 第二意见 / 漏段线索（**无脚本**） | Agent 命令 | `references/01-selection.md`、`references/03-parameters.md` |
| 5 | 对齐全片：有"看"的产物就跑 `scripts/merge_sources.py`，只把分歧段交给人工 | 脚本 + 人工 | `references/02-workflow.md` §2.2 |
| 6 | 执行「转完必做三查」与质检（`scripts/qc_check.py`） | 脚本 + 人工 | `references/04-quality.md` |
| 7 | 精编：术语表、标点、数字规整；留痕并复核许可证 | 人工 | `references/05-vocabulary.md`、`references/09-attribution.md` |

## 方案分层与决策入口

| 层 | 方案 | 什么时候用 |
|---|---|---|
| **零成本前置** | 内嵌文本字幕轨 / 同目录 srt（`scripts/extract_subs.py`） | 素材自带字幕——不推理、不花钱、不会听错人名，先试这个 |
| **看：画面 OCR** | PP-OCR 逐帧读下半幅，一行一行读（`scripts/ocr_subs.py`） | 没有字幕轨、字幕烧在画面里；约 0.38 s/帧，比转写慢，按需跑 |
| **默认推荐** | faster-whisper + turbo / GPU（`scripts/transcribe.py`） | 符合已验证环境、要速度与稳定 |
| **备用 / 可选** | turbo / CPU、large-v3 / GPU、SenseVoice q8 GGUF / CPU | 显卡被占、疑难片段复核、需要异源第二意见 |
| **实验性** | Qwen3-ASR、FunASR Python 版、WhisperX、说话人分离 | 资源充足且明确要扩展；**本机未验证** |

> 五个判断问题（有无 GPU / 资源够不够 / 要快还是要准 / 要不要异源交叉 / 主方案失败怎么办）见 `references/01-selection.md`。**turbo 与 large-v3 同源，不算独立异源验证。**

## 决策路由

| 遇到什么 | 读取 |
|---|---|
| 选哪套方案、速度与资源大概什么量级、五问决策 | `references/01-selection.md` |
| 流程怎么排、断点续跑、产物命名 | `references/02-workflow.md` |
| 参数怎么填、GPU DLL 报错、CPU 回退、版本锁 | `references/03-parameters.md` |
| 质检、四个指标、标准答案、抽帧边界、幻觉形态 | `references/04-quality.md` |
| 英文缩写 / 产品名 / 人名听错 | `references/05-vocabulary.md` |
| 环境核查、内存显存红线、安装下载、许可证口径 | `references/06-environment.md` |
| 任何「跑不起来 / 结果不对」的排查 | `references/07-pitfalls.md` |
| 结论能不能写、哪些不能外推 | `references/08-methodology.md` |
| 某个模型是谁做的、什么许可证 | `references/09-attribution.md` |
| 要真正跑一条转写、或查看输出 JSON 结构 | `scripts/transcribe.py` |
| 素材可能自带字幕、想省掉转写 | `scripts/extract_subs.py`、`references/02-workflow.md` §2.0 |
| 字幕烧在画面里，想先探一下有没有 | `scripts/ocr_subs.py --list` |
| 已经有语音稿和字幕稿，想知道哪里不一致 | `scripts/merge_sources.py`、`references/02-workflow.md` §2.2 |
| 一次跑一整个文件夹（或多目录） | `scripts/batch_transcribe.py`（先 `--dry-run` 看计划） |
| 质检报了 ERROR / RED，想知道区别和阈值 | `references/04-quality.md`、`scripts/qc_check.py` |
| 我的环境算不算已验证、依赖版本要求 | `README.md`「已知限制」、`references/06-environment.md` |
| 直接抄命令 | `references/10-commands.md` |
| 阅读未拆分的完整原文 | `docs/转写经验_通用版.md` |

## 三条铁律

1. **转完必做三查**：开头 30 秒（提示语回音 / 真实开场被吃掉）、结尾 30 秒（片尾话术 / 末段结束时间越过音频时长）、中间任意 > 8 秒的长间隙。三项都是硬检查；四个数字指标全过也不能免。注意「尾部空」算出负数时不是合格，而是多出来的幻觉段。
2. **没有可信标准答案就不报准确率**：有人工稿、官方字幕或**内嵌文本字幕轨**（`extract_subs.py` 抽出来的才算）才能算字错率；**画面 OCR 稿是另一份证据，不是标准答案**，两路一致也不等于准确率；差异度不是准确率。
3. **同源两版不等于两双眼睛**：turbo 与 large-v3 共享编码器，一致只降低风险；要独立第二意见必须换不同架构的引擎。**听与看才是真正的两双眼睛**——它们读的是两种完全不同的信号。

## 硬约束

- 离线优先；不上传素材、不引入 torch 系重依赖（本机可用运行内存常低于 6 GB）。
- 一次只加载一个模型，串行执行；GPU 与纯 CPU 方案不要并发，否则资源读数互相污染。
- 峰值资源必须连续采样（前后各采一次只能看到残留）。
- 不改原始输出；重跑写新目录；每个数字标出处，推测标推测。
- 现成环境先自检再决定是否安装，别重复下载模型。
- 关闭 `initial_prompt` 会让标点密度几乎归零，不要关；输出只有**段级**起止时间（`s/e/t`），没有词级 `words`，`transcribe.py` 因此不开放 `--word-timestamps`。
- `qc_check.py` 分两级：**ERROR** = 确定的数据/格式错误（含 `NaN`/`Infinity` 等非有限数值，必须修输入），**RED/REVIEW** = 需人工复核的风险信号（不等于判错）。时间戳**能解析不等于时间轴有效**：负起点 / `end < start` / 乱序会让三个时间指标置 `null`、`has_timestamps=false`，文本指标保留。两者在文本与 `--json` 模式都返回退出码 1；阈值严格大于（`gap > 8.00 s`、`overrun > 0.50 s`），等于阈值不触发。
- 四个数字指标全过也不能替代三查；RED 只说明「去看一眼」。

## 可执行脚本

- `scripts/extract_subs.py`：**抽内嵌文本字幕轨**（或同目录 `.srt/.ass/.ssa/.vtt`），产出与转写同 schema 的 JSON + MD，外加 `<key>.srt` 留档。零推理、秒级，`"engine": "subtitle-track"`。只认文本轨：PGS/VobSub 会拒绝并提示需要 OCR，硬字幕不在此列；时间轴原样保留，重叠只在 `stats.overlaps` 报数。退出码 0/2/3（3 = 一份都没抽出来，转写照常）。
- `scripts/ocr_subs.py`：**画面硬字幕 OCR**，`"engine": "ocr"`，产出同上 schema。先抽样定位字幕带（按字号分位数保留大字，再按中心开窗评分，**不做单链聚类**——屏幕录制里界面文字会把整个下半屏连成一条；裁剪比判定窗口多留 5% 上下空白，检测器需要这个上下文），探不到字幕带就明确说没有，不硬读。定位之后**按物理行读**：双语字幕按 `--language` 只留主语言（否则字数翻倍、互校相似度被译文拖垮），另起一行且换得比口播慢的固定文案（要点条、章节点、流程图）按「一次停留连续多少帧几乎不变」剔除——实测口播 2 帧、要点条 9–17 帧；删的是长停留而非整个位置，同位置闪过的台词会留下。逐帧都在变的图表仍分不开。剔除与取舍都写进产物头部。需要 `rapidocr_onnxruntime` + `onnxruntime`（13 MB 模型随 wheel），未装时明确报错退出，不影响其他脚本。约 0.38 s/帧。
- `scripts/merge_sources.py`：**听看互校**。逐段对齐两份 JSON，算相似度，分成 一致 / 接近 / 不一致 / 只有语音 / 只有字幕 五类，MD 只列需要人看的段落。会先测两路时间偏移（字幕比语音晚几秒很常见）并按此对齐，但**不改任何一边的原始时间**；数字或术语只在一边出现时会升级为"不一致"。**不产出合并稿，也不裁决谁对。**
- `scripts/transcribe.py`：**真正执行转写**，输出统一 JSON + 带时间戳 MD（两份各写 `.tmp` 后原子改名，先 MD 后 JSON，所以「有 JSON ⇒ 有 MD」）；仅当错误确属 GPU/CUDA 时才回退 CPU（模型坏、音频坏直接报错，`--no-fallback` 可禁用回退）；产物可直接喂给 qc_check。读输出里的 `device` 字段判断实际用的设备，别只看命令行入参。
- `scripts/check_env.py`：只读环境自检，输出版本、显卡、CUDA DLL 文件、磁盘、模型目录状态。
- `scripts/batch_transcribe.py`：多目录/多模式**串行**批处理，跳过已完成、失败隔离，每文件产出与单跑同 schema 的 JSON + MD，汇总写 `_batch/summary.json`；退出码 0/1/2。**不做并发、队列、重试调度**——状态就是那些输出产物本身。「已完成」要求 JSON + MD **两份都完整**；`--force` 也把两份一起挪成 `.prev`。只吃 PCM WAV：`--pattern` 只筛文件名，匹配到非 `.wav` 会在开跑前告警。
- `scripts/qc_check.py`：输入校验（ERROR）+ 四项粗筛 + 三查硬规则（RED/REVIEW）；无真实时间戳或时间戳无效时时间类指标为 `null` 并标 N/A，duration 无效时明确报「无法检查」；`--json` 始终是严格 JSON。
- `tests/run_smoke.py`：零依赖回归测试，37 类输入 × 文本/JSON 两种模式，1026 条断言；`tests/run_transcribe_unit.py`：回退决策、耗时口径、产物原子写入，43 条断言；`tests/run_batch_unit.py`：批处理键名/碰撞/跳过判据/摘要编码，外加用假 `transcribe.py` 驱动的端到端集成（成功+失败+跳过+修复重试+半成品产物），102 条断言；`tests/run_subs_unit.py`：字幕轨与外挂字幕解析（BOM/CRLF/GB18030/UTF-16/坐标后缀/纯数字正文）、选轨与语言拒绝、无 ffprobe 时的外挂回落，79 条断言；`tests/run_ocr_unit.py`：文本清洗、物理行切分、双语取主语言、字幕带（静态水印排除、两行合并、屏幕录制不串联、分辨率无关）、固定文案按停留时长剔除、cue 合并与置信度取舍，73 条断言；`tests/run_merge_unit.py`：中点对齐、相似度分级、数字/术语升级、时间偏移检测、断句覆盖、报告结构，58 条断言。都不需要网络 / 模型 / GPU，OCR 那套连 OCR 运行时都不需要。

> 第三方项目、维护方、许可证见 `references/09-attribution.md`；本机实测环境快照见 `references/06-environment.md` §7.1.1，已验证范围见 `README.md`「已知限制」。换机器时先按 `references/06-environment.md` 复核，不要照抄任何数字。版本规划见 `ROADMAP.md`。
