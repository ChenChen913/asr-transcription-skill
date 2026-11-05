---
name: asr-transcription
description: This skill should be used when converting audio or video into readable Chinese transcripts on a local or offline Windows machine — choosing an ASR engine (faster-whisper turbo / large-v3 / SenseVoice GGUF), setting transcription parameters, building a batch pipeline, checking transcripts for hallucinations and missing segments, diagnosing GPU/DLL/encoding failures, or verifying model licenses and attribution. Trigger phrases include 转写、语音转文字、逐字稿、ASR、Whisper、SenseVoice、质检、幻觉、漏段、标点.
agent_created: true
---

# 音视频转写成可读文稿

## 目的

把多轮实测沉淀成可复用的执行流程：选方案 → 准备环境 → 统一抽音频 → 转写 → 交叉核对 → 质检 → 精编 → 留痕。核心结论是**保留现有主力方案，另加一个纯 CPU 的异源方案做快速预览和第二意见**；两者都不完美，必须靠质检流程兜底。

## 何时使用

- 把一批视频或音频转成中文逐字稿
- 在 Whisper 系与中文轻量模型之间选型，或评估是否值得补第二套引擎
- 配置 GPU 推理、下载模型、编写批处理脚本
- 排查异常结果：开头复读提示语、结尾冒出片尾话术、中间整段丢失、全文无标点
- 判断某份结果能不能宣称「准确率提升」
- 核对第三方模型或项目的归属与许可证

## 渐进式加载（三级）

1. 本文件的 `name` + `description` 常驻上下文，决定是否触发。
2. 触发后只读本文件（< 5k 词）：拿流程骨架与路由表。
3. **按需**再读 `references/` 下的单个文件；不要一次全读。长文件用 grep 定位，脚本可直接执行而不必读进上下文。

## 工作流骨架

| 步 | 动作 | 需要时读取 |
|---|---|---|
| 0 | 回答开测四问：本轮要回答什么、判分依据在不在、口径红线是否定好、产物会不会覆盖旧结果 | `references/02-workflow.md` |
| 1 | 环境自检（显卡、模型目录、CUDA DLL、ffmpeg、版本锁） | `references/06-environment.md`、`scripts/check_env.py` |
| 2 | 用 FFmpeg 统一抽 16 kHz 单声道 WAV，所有方案共用同一份输入 | `references/02-workflow.md` |
| 3 | 主力转写：faster-whisper + turbo / GPU，参数照抄 | `references/01-selection.md`、`references/03-parameters.md` |
| 4 | 可选：异源轻量方案跑一遍，作为快速预览 / 第二意见 / 漏段线索 | `references/01-selection.md`、`references/03-parameters.md` |
| 5 | 对齐两份文本，人工只看分歧处和长间隙区间 | `references/02-workflow.md` |
| 6 | 执行「转完必做三查」与质检（可跑 `scripts/qc_check.py`） | `references/04-quality.md` |
| 7 | 精编：术语表、标点、数字规整；留痕并复核许可证 | `references/05-vocabulary.md`、`references/09-attribution.md` |

## 决策路由

| 遇到什么 | 读取 |
|---|---|
| 选哪套方案、速度与资源大概什么量级 | `references/01-selection.md` |
| 流程怎么排、断点续跑、产物命名 | `references/02-workflow.md` |
| 参数怎么填、GPU DLL 报错、CPU 回退、版本锁 | `references/03-parameters.md` |
| 质检、四个指标、标准答案、抽帧边界、幻觉形态 | `references/04-quality.md` |
| 英文缩写 / 产品名 / 人名听错 | `references/05-vocabulary.md` |
| 环境核查、内存显存红线、安装下载、许可证口径 | `references/06-environment.md` |
| 任何「跑不起来 / 结果不对」的排查 | `references/07-pitfalls.md` |
| 结论能不能写、哪些不能外推 | `references/08-methodology.md` |
| 某个模型是谁做的、什么许可证 | `references/09-attribution.md` |
| 直接抄命令 | `references/10-commands.md` |
| 阅读未拆分的完整原文 | `docs/转写经验_通用版.md` |

## 三条铁律

1. **转完必做三查**：开头 30 秒（提示语回音 / 真实开场被吃掉）、结尾 30 秒（片尾话术 / 末段结束时间越过音频时长）、中间任意 > 8 秒的长间隙。三项都是硬检查；四个数字指标全过也不能免。注意「尾部空」算出负数时不是合格，而是多出来的幻觉段。
2. **没有可信标准答案就不报准确率**：有人工稿或官方字幕才算字错率；画面字幕 OCR 只能做旁证且必须先过质量门槛；差异度不是准确率。
3. **同源两版不等于两双眼睛**：turbo 与 large-v3 共享编码器，一致只降低风险；要独立第二意见必须换不同架构的引擎。

## 硬约束

- 离线优先；不上传素材、不引入 torch 系重依赖（本机可用运行内存常低于 6 GB）。
- 一次只加载一个模型，串行执行；GPU 与纯 CPU 方案不要并发，否则资源读数互相污染。
- 峰值资源必须连续采样（前后各采一次只能看到残留）。
- 不改原始输出；重跑写新目录；每个数字标出处，推测标推测。
- 现成环境先自检再决定是否安装，别重复下载模型。
- 关闭 `initial_prompt` 会让标点密度几乎归零，不要关；`word_timestamps` 默认 `False`，输出只有段级起止时间。

## 可执行脚本

- `scripts/check_env.py`：只读环境自检，输出版本、显卡、磁盘、模型目录状态。
- `scripts/qc_check.py`：读取转写 JSON，执行四项粗筛与三查硬规则，输出标红项；支持 `--json` 机读。

> 第三方项目、维护方、许可证见 `references/09-attribution.md`；本机实测配置见 `README.md`。换机器时先按 `references/06-environment.md` 复核，不要照抄任何数字。
