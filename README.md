<h1 align="center">asr-transcription</h1>

<p align="center">
  <b>简体中文</b> | <a href="./README_EN.md">English</a>
  <br><br>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.13.x-3776ab.svg" alt="Python 3.13.x"></a>
  <img src="https://img.shields.io/badge/platform-Windows%2011%20%C2%B7%20verified-lightgrey.svg" alt="Platform: Windows 11, verified">
  <img src="https://img.shields.io/badge/tests-1381%20offline%20assertions-brightgreen.svg" alt="1381 offline assertions">
</p>

给 Agent 加载的中文音视频转写技能包：**听**（语音转写）+ **看**（字幕轨抽取 / 画面 OCR）+ **互相校验**，附 1381 条离线断言。

> [!NOTE]
> 本仓库由 AI 辅助开发，脚本与数字都经人工实测复核，过程记录在 [`ROADMAP.md`](ROADMAP.md)。未验证的平台不写成「已支持」，见[已知限制](#已知限制)。

```mermaid
flowchart LR
    A["素材 mp4 / mkv"] -->|"听：FFmpeg"| B["16 kHz 单声道 WAV"]
    A -->|"看：有没有字幕轨"| S["extract_subs.py<br/>外挂 srt / 内嵌文本轨"]
    A -->|"看：只有画面"| O["ocr_subs.py<br/>PP-OCR 逐帧读"]
    B --> C["transcribe.py<br/>faster-whisper turbo · GPU"]
    C -->|"错误确属 CUDA"| C2["回退 CPU int8"]
    C --> W["听：JSON + MD"]
    C2 --> W
    S --> W2["看：JSON + MD"]
    O --> W3["看：JSON + MD"]
    W --> M["merge_sources.py<br/>听看互校"]
    W2 --> M
    W3 --> M
    M --> R["只列不一致的段落"]
    R --> E["qc_check.py + 人工三查"]
    B -.->|可选| H["SenseVoice q8 GGUF<br/>异源第二意见"]
```

装好依赖后，一条音频三步走：

```powershell
python -m pip install "faster-whisper==1.2.1" "ctranslate2==4.8.1" "av<19"
python scripts/transcribe.py --wav "01.wav" --model "<模型目录>" --out "out/01.json" --tag "01"
python scripts/qc_check.py "out/01.json"
```

完整前置条件与自检步骤见[快速开始](#快速开始)。

## 目录

- [为什么做这个项目](#为什么做这个项目)
- [两种能力：听，和看](#两种能力听和看)
- [快速开始](#快速开始)
- [用法](#用法)
- [配置](#配置)
- [项目结构](#项目结构)
- [开发](#开发)
- [常见问题](#常见问题)
- [已知限制](#已知限制)
- [第三方项目与许可证](#第三方项目与许可证)
- [如何贡献](#如何贡献)
- [许可证](#许可证)

## 为什么做这个项目

起点是真实的转写任务，不是先有框架再找场景。要把一批中文口播视频转成逐字稿，换过几套方案之后，失败很少来自模型本身：

- 开头把提示语原文念进正文
- 结尾凭空生成「谢谢观看」这类片尾话术
- 中间整段消失，只看输出看不出来
- GPU 的 cuBLAS / cuDNN DLL 找不到，而文件其实就在磁盘上
- `--json` 重定向到文件，编码变成 GBK，不再是合法 JSON

这些问题换模型解决不了。它们属于流程：环境怎么锁、参数怎么填、结果怎么验、数字能不能外推。所以这个仓库的重心是流程，不是模型。

脚本负责能自动化的四件事：环境自检、单条转写、批量转写、结果质检。判断留给人和 Agent：开头结尾要回听，长间隙要对照，没有标准答案就不报准确率。

三条铁律写在 `SKILL.md` 里，也写在这里：

1. **转完必做三查**：开头 30 秒、结尾 30 秒、任意相邻两段间隔 > 8 秒。四个数字指标全过也不能免。
2. **没有可信标准答案就不报准确率**：人工稿或官方字幕才算字错率；画面字幕 OCR 只能当旁证；差异度不是准确率。
3. **同源两版不等于两双眼睛**：turbo 是 large-v3 的解码器剪枝版，共享编码器，错误高度相关。要独立第二意见，得换架构。

它不是什么：不训练模型、不发布权重、不做云端服务、不含任何音频素材。仓库里只有流程、脚本和归属登记。

## 两种能力：听，和看

两条取字通路，错的地方不一样：

| | 听（`transcribe.py`） | 看（`extract_subs.py` / `ocr_subs.py`） |
|---|---|---|
| 输入 | 音轨 | 字幕轨、或画面像素 |
| 强项 | 覆盖全片、有断句、标点自然 | 专名、术语、数字通常更准；标点是作者写的 |
| 典型错误 | 同音字：`RAG`→`RAW`、`Rerank 精排`→`RUNK 金牌` | 形近字、漏字：`懵逼`→`憎逼`；把标题、水印当成字幕 |

同一个视频里的真实对照（`懂王AI面试合集` 第 66 条，183 秒）：

- 听：`TOPK出牌, RUNK金牌, 大模型` ／ 看：`Top K粗排, Rerank精排, 大模型` —— 这一句看对了
- 听：`蒙逼了` ／ 看：`憎逼了` —— 这一句听对了

所以不该二选一，也不该把两份揉成一份。正确用法是三步：

1. **听为主**：先出带时间戳的逐字稿。它覆盖全片、有断句，是最接近"稿子"的东西。
2. **看为辅**：有字幕轨就抽，没有就 OCR。它是独立的一份证据，不是听写的补丁。
3. **互校为终**：`merge_sources.py` 逐段对齐，只列出两边说法不一致、以及只有一边有内容的地方。人只看这几段，不用通读两份稿子。

互校还会顺手回答两个问题：字幕整体比语音晚了几秒（剪辑过的短视频很常见），以及哪些数字、术语两边写法不同。

## 快速开始

### 前置要求

| 项 | 要求 |
|---|---|
| 系统 | Windows 11（唯一实测平台，其他平台见[已知限制](#已验证的平台)） |
| Python | 3.13.x（2026-11-06 实测 3.13.14） |
| FFmpeg | `ffmpeg` 与 `ffprobe` 都在 `PATH` |
| GPU（可选） | NVIDIA，CUDA 12.x / cuDNN 9，显存 ≥ 8 GB；没有就加 `--device cpu` |
| 模型 | 一个 CTranslate2 目录，含 5 个文件：`config.json` / `model.bin` / `preprocessor_config.json` / `tokenizer.json` / `vocabulary.json` |
| 磁盘 | turbo 约 1.51 GiB、large-v3 约 2.88 GiB |

模型权重**不在本仓库**，需要自己下载（Hugging Face 或镜像）。没有模型时，`check_env.py` 与六套测试仍可运行。

### 安装

```powershell
git clone https://github.com/ChenChen913/asr-transcription-skill.git "$env:USERPROFILE\.workbuddy\skills\asr-transcription"
cd "$env:USERPROFILE\.workbuddy\skills\asr-transcription"
python -m pip install "faster-whisper==1.2.1" "ctranslate2==4.8.1" "av<19"
```

装完先自检。`check_env.py` 只读，不写文件；`run_smoke.py` 不需要模型、GPU 或网络：

```powershell
python scripts/check_env.py --models "<模型目录>"
python tests/run_smoke.py
```

### 跑通第一条

```powershell
# 1. 统一抽成 16 kHz 单声道 PCM WAV
ffmpeg -y -i "<素材>.mp4" -vn -ac 1 -ar 16000 -c:a pcm_s16le "01.wav"

# 2. 转写；GPU 失败且确属 CUDA 错误时自动回退 CPU
python scripts/transcribe.py --wav "01.wav" --model "<模型目录>" --out "out/01.json" --tag "01"

# 3. 质检
python scripts/qc_check.py "out/01.json"
```

跑完看三处：`out/01.json`（结构化）、`out/01.md`（带时间戳的可读稿）、JSON 里的 `device` 字段（`cuda` 还是 `cpu`）。**不要只看命令行的 `--device` 入参**，回退过的任务实际设备在输出里。

### 不想用 Skill 机制

脚本都是普通 Python 3 程序，把仓库放在任意目录，直接 `python scripts/transcribe.py ...` 就能用，不需要 Agent 运行时。依赖只有两处：`transcribe.py` 要 faster-whisper / CTranslate2 + 模型，`ocr_subs.py` 要 `rapidocr_onnxruntime` 与 `onnxruntime`；其余五个脚本只用标准库。没装时会明确告诉你装什么，不会静默降级。

想在其他 Agent 工具里按 Skill 加载，按各自文档规定的目录与元数据放置。**本仓库只实测过 WorkBuddy 用户级 Skill，不声称对其他工具已兼容。**

## 用法

### 自动化到哪一步

| 环节 | 归属 | 载体 |
|---|---|---|
| 环境自检 | 已自动化 | `scripts/check_env.py` |
| 字幕轨抽取（看） | 已自动化 | `scripts/extract_subs.py` |
| 画面硬字幕 OCR（看） | 已自动化，需 OCR 运行时 | `scripts/ocr_subs.py` |
| 单条转写（听） | 已自动化 | `scripts/transcribe.py` |
| 批量转写 | 已自动化 | `scripts/batch_transcribe.py` |
| 结果质检 | 已自动化 | `scripts/qc_check.py` |
| 听看互校 | 已自动化 | `scripts/merge_sources.py` |
| 回归测试 | 已自动化 | `tests/` 下六套 |
| 抽音频、异源对照、差异对齐、精编、留痕 | Agent 工作流 | Skill 指导 Agent 调命令、判读结果；没有对应脚本 |
| 首末段回听、建术语表、提供标准答案、商用许可核对 | 手动 | 需要人工判断 |

> 「文档写了完整流程」不等于「工具已经实现」。SenseVoice q8 GGUF 只有命令、没有封装脚本，是可选的独立对照方案。

### 字幕轨抽取

素材自带字幕时先走这一步：不推理、不加载模型，几秒钟出稿。

```powershell
python scripts/extract_subs.py --input "01.mp4" --out-dir "out" --list   # 只列轨，不抽
python scripts/extract_subs.py --input "01.mp4" --out-dir "out"          # 外挂 srt 优先，其次内嵌文本轨
python scripts/extract_subs.py --input "01.mp4" --out-dir "out" --lang zh
```

产物与转写**同 schema**（`out/01.json` + `out/01.md`），另存一份 `out/01.srt`，JSON 里 `"engine": "subtitle-track"`。只认**文本**轨：PGS / VobSub 这类图形轨会明确拒绝并提示需要 OCR，烧进画面的硬字幕不在此列。它会连容器时长一起 probe，所以外挂字幕只覆盖部分时长时不会把视频时长报短。退出码 `3` 表示一份都没抽出来，此时照常转写。

> 字幕是字幕，不是逐字稿：作者会省语气词、合并长句、自带断句。产物 MD 头部写明这一点，`qc_check.py` 也会跳过字/段密度两条警告——但重叠、长间隙、片尾话术照旧检查。

### 画面硬字幕（OCR）

字幕已经烧进像素、又没有文本字幕轨时走这一步。要先装 OCR 运行时（约 13 MB 模型随 wheel 一起装，不用另外下载）：

```text
python -m pip install rapidocr_onnxruntime onnxruntime
```

```text
# 先探一下有没有硬字幕（16 帧，几秒钟）
python scripts/ocr_subs.py --input "01.mp4" --list

# 有就逐帧读，产出同 schema 的 JSON + MD
python scripts/ocr_subs.py --input "01.mp4" --out-dir "out" --fps 1.0
```

字幕带的位置不是猜的，是**从 OCR 自己的框反推**出来的，三步：

1. 先按字号裁一次：只留高度在本片 60 分位以上的框。屏幕录制里几十行界面小字在这一步就没了，而且用的是本片自己的分位数，不用为不同分辨率调阈值。
2. 再为每个框的中心 ±5% 开一个窗打分：`支持度 × min(不同文本数, 8) × 字号权重`。文字从不变化的窗（台标、水印、固定标题）得分趋近 0，自然出局。
3. 最后收紧到字幕尺寸的框（`h ≥ 0.6 × 中位行高`），上下各留 5% 画面高度的空白再裁剪。

第 3 步那点空白不是余量，是**必要条件**：PP-OCR 的检测器需要行上下有上下文，裁剪线压在字形描边上时它会把一整行读成碎片——实测同一帧 `Topk要用最小的值获得` 在 1.2% 留白下读成 `doL`，5% 留白下读对。

所以黄色字幕不需要任何特殊处理——它只是字，不是颜色问题。

**但字幕带里往往不止一行。** 找对位置只解决了「哪一片区域有字」，没解决「哪一行在说话」。真实素材里另外两行很常见，而且都会把稿子写坏：

- **双语字幕**：译文印在口播下面，两行同步变化，图里没有任何线索能说哪行是说的。按 `--language` 取（默认 `zh`）——保留中文、丢掉译文。不丢的代价很具体：实测字数 692 → 2816，而且 `merge_sources.py` 的相似度被英文拖到几乎每一对都判「不一致」。
- **固定文案**：要点条、章节点、流程图常压在字幕下方。它们和字幕的字号一样、位置也一样，唯一的区别是**换得慢**——口播每秒换一行，要点条十几秒才换。所以按「一次停留连续多少帧几乎不变」剔除，实测口播 2 帧、要点条 9–17 帧。删的是长停留，不是整个位置：同一位置上章节点停 17 秒、中间闪过一句台词，台词会留下（实测 `发票要寄到上海办公室` 就是这样保住的）。两个保险：只有别的行换得快 2.5 倍以上才动手，且不低于 5 秒的绝对下限；整片都在慢换的（比如歌词）一行不动。**每帧都在变的图表仍然分不开**——它的节奏和对话一样快，25 条素材里只剩 7 条这样的残留。

剔除和取舍都会写进产物的头部与 `params`，不会悄悄少字。

默认只读画面下半幅（`--region 0.50,0.995`）、1 fps 采样。代价是慢：约 0.38 秒一帧，3 分钟视频约 1 分钟，比转写慢。所以它是**按需的第二意见**，不是默认路径。退出码 `3` 表示没有一个文件找到可读的字幕带。

> 旧的纯 Pillow 方案（亮度 + 低饱和 + 行投影）已放弃：同一批素材上，白衬衫所在的行得分 117.8，比真实字幕带还高；而且"饱和度 ≤ 70"会把黄字直接排除。取舍记录见 `ROADMAP.md` 的 v0.7。


### 单条转写

```powershell
python scripts/transcribe.py `
    --wav "01.wav" --model "<模型目录>" --out "out/01.json" --tag "01" `
    --device cuda --compute float16
```

回退有前提：只有错误信息命中 `cuda / cublas / cudnn / cudart / nvml / gpu / out of memory / no kernel image` 才换 CPU。模型文件损坏、音频解不出、参数不合法不回退——换 CPU 一样失败，回退只会把真正的错误埋掉。禁用回退用 `--no-fallback`。

| 退出码 | 含义 |
|---|---|
| `0` | 成功 |
| `2` | 输入或模型目录不可用 |
| `3` | 当前设备失败，且未回退 |
| `4` | GPU 与 CPU 都失败 |

### 批量转写

一个文件夹，或者多个：

```powershell
# 先看计划：不加载模型，不写文件
python scripts/batch_transcribe.py --input "<目录A>" "<目录B>" --out-dir "out" --dry-run

# 再开跑
python scripts/batch_transcribe.py --input "<目录A>" "<目录B>" --out-dir "out" --model "<模型目录>"
```

它只做四件事：

1. **串行**：一次一个模型实例。GPU 与 CPU 方案不并发，避免资源读数互相污染。
2. **跳过已完成**：判据是一组成套产物（JSON + MD），不是单个文件。
3. **失败隔离**：单文件非零退出或超时只记一笔，不影响其余。
4. **每文件独立产物**：与单跑同 schema，可直接喂给 `qc_check.py`。

产物布局：

```text
out/
├── <key>.json          每个输入一份，结构化
├── <key>.md            同名可读稿
├── <key>.json.prev     只有重跑中或重跑失败时才存在，不算完成
├── <key>.md.prev
└── _batch/summary.json 计数 + 每文件结果，每处理完一个就落盘
```

两个容易踩的点：

- **`json` 与 `md` 是一组**。跳过判据要求两份都完整，`--force` 也把两份一起改名成 `.prev`。写入端先改名 MD、后改名 JSON，所以「有 JSON ⇒ 有 MD」，被 kill 在半路的任务下一轮会补跑，不会被永久跳过。
- **只吃 PCM WAV**。`--pattern` 只筛文件名，不增加解码能力。匹配到非 `.wav` 时，开跑前打印 `[WARN]` 与一句 FFmpeg 转换命令，但不阻断——真正的解码错误由 `transcribe.py` 报。

退出码：`0` 全部成功｜`1` 有失败或超时｜`2` 用法或匹配错误。

### 质检

```powershell
python scripts/qc_check.py "out/*.json"
python scripts/qc_check.py "out/*.json" --json      # 严格 JSON，可进 CI
```

下面是一次实跑输出（合成样例，用于展示分级）：

```text
--- demo.json [REVIEW]
    (62.40s, 34 chars, 3 segs)
    punct/100=11.76  chars/min=32.7  segs/min=2.9  maxgap=10.50s  tail=38.40s  overrun=-38.40s
    [RED/REVIEW] LONG GAP 10.50s at 9.6-20.1s
    [warn] tail gap 38.40s > 10.0s (possible truncation)
    [warn] char density 32.7/min outside (200.0, 450.0)
```

| 标记 | 含义 | 动作 |
|---|---|---|
| `[ERROR]` / `"errors"` | 确定的输入数据问题：字段缺失、非数字、`NaN` / `Infinity`、负数、乱序、`duration` 无效 | 修输入或产出脚本，**不要当成「稿子转错了」** |
| ↳ 时间轴失效 | 负起点 / `end < start` / 乱序：数值能读，但时间轴不成立 | 三个时间指标置 `null`、`has_timestamps=false`、`note` 写 `INVALID TIMELINE`；`chars` 等文本指标照常 |
| `[RED/REVIEW]` / `"red"` | 风险信号：重叠、空文本、越界、长间隙、提示语回音、无时间戳 | 人工复核对应区间，**不等于判错** |
| `[warn]` | 粗筛指标越界：字符密度、段数、尾部空 | 参考，不单独触发退出码 |

阈值严格大于：`gap > 8.00 s`（8.00 不报，8.01 报）、`overrun > 0.50 s`。用 `--gap` / `--tail` 调整。有 ERROR 或 RED 时，文本与 `--json` 模式都返回退出码 1。

### 听看互校

两份稿子都出来之后，不要人工从头比对：

```text
python scripts/merge_sources.py --heard "out/01.json" --seen "out/01-ocr.json" --out "out/01.merge.md"
```

它按时间对齐、逐段算相似度，把结果分成五类：一致 / 接近 / 不一致 / 只有语音 / 只有字幕。MD 只列**需要人看的**那几段，按可疑程度排序，每段给出两边的原文，以及只在一边出现的术语和数字。同名 `.json` 里有全量结果。

四个刻意的设计：

- **不产出"合并稿"**。两份都不可靠，混起来只会更不可靠，而且无法再验证。
- **数字不一致直接判"不一致"**，哪怕整句相似度 0.95——`K=50` 与 `K=60` 的差别，同音字和字形都看不出来。
- **先测时间偏移**。短视频的字幕常比语音晚一两秒，不校正的话每一段都会被误判成"不一致"。偏移会写进报告，但**两边原始时间一律不改**。
- **断句不同不算矛盾**。一条字幕常盖住两段语音，按整句相似度算会打折，看起来像"说错了"。脚本改为看短的那边有多少字能在长的那边找到，命中 ≥ 80% 就降级成"接近"并注明原因；但如果少的是数字，仍然判"不一致"。


### 输出格式

`transcribe.py` 的 JSON 只有段级时间戳，没有词级 `words`：

| 字段 | 含义 |
|---|---|
| `tag` / `engine` / `model` | 任务标识、引擎、模型目录名 |
| `device` / `compute` | 实际使用的设备与精度；回退后这里写 `cpu` |
| `duration` | 音频时长（秒） |
| `segments[]` | 每段 `s` / `e` / `t`，即起止秒与文本 |
| `stats` | `chars_per_min` / `segs_per_min` / `punct_per_100` / `max_gap_s` / `tail_gap_s` |
| `load_s` / `asr_s` / `rtf_x` | 加载耗时 / 纯推理耗时 / 实时倍率。`asr_s` 不含加载 |
| `fallback` / `fallback_load_s` | 非空表示回退过，含原始异常；后者是失败那次已完成的加载耗时 |
| `error` | 失败原因，成功时为 `null` |

### 转完必做三查

| 查什么 | 怎么判 |
|---|---|
| 开头 30 秒 | 有没有把提示语原文念进正文；片头话术有没有混进来；真实开场是不是被整段吃掉 |
| 结尾 30 秒 | 有没有凭空冒出「谢谢大家 / 感谢观看」；最后一段的结束时间有没有超过音频总时长 |
| 中间长间隙 | 相邻两段间隔 > 8 秒就回听，或拿异源方案对照该区间 |

「尾部空」算出负数不是合格，是多出来的幻觉段。这三项是硬检查，不是参考值。

## 配置

本仓库**没有配置文件，也没有 API key 或账号**。所有参数走命令行。脚本唯一读取的环境变量是 `PATH`（`transcribe.py` 用它找 CUDA DLL 目录）。

### extract_subs.py

| 参数 | 默认 | 说明 |
|---|---|---|
| `--input` | 必填 | 一个或多个媒体文件 / 目录 |
| `--out-dir` | 必填 | 产物目录（`<key>.json` / `.md` / `.srt`） |
| `--stream` | 自动 | 取第 N 条字幕流（从 0 数，按人眼顺序） |
| `--lang` | 自动 | 只接受该语言的文本轨；找不到就拒绝，**不静默换轨** |
| `--no-sidecar` | 关 | 忽略同目录同名 `.srt/.ass/.ssa/.vtt` |
| `--recursive` | 关 | 递归子目录 |
| `--list` | 关 | 只列字幕轨，不抽 |
| `--force` | 关 | 已有产物也重抽 |
| `--timeout` | `120` | 单次 ffmpeg / ffprobe 超时，秒 |
| `--ffmpeg` / `--ffprobe` | PATH | 指定可执行文件 |

### ocr_subs.py

| 参数 | 默认 | 说明 |
|---|---|---|
| `--input` | 必填 | 一个或多个视频文件 / 目录 |
| `--out-dir` | `--list` 时免 | 产物目录（`<key>.json` / `.md`） |
| `--fps` | `1.0` | 每秒采样帧数；耗时与之严格成正比 |
| `--region` | `0.50,0.995` | 纵向搜索窗口，按画面高度的比例 |
| `--min-score` | `0.5` | 低于此置信度的行直接丢弃 |
| `--language` | `zh` | 双语字幕保留哪一行：`zh` 留中文丢译文，`en` 反过来。一处都不匹配时全部保留，不会产出空稿 |
| `--no-band` | 关 | 跳过字幕带定位，读整个 `--region`（只用于调试，会读进标题和水印） |
| `--calib-frames` | `16` | 用于定位字幕带的抽样帧数 |
| `--max-frames` | `900` | 采样帧上限；超出会自动降 fps 并在输出里说明 |
| `--list` | 关 | 只探测有没有硬字幕，不写产物 |
| `--recursive` / `--force` / `--timeout` / `--ffmpeg` / `--ffprobe` | 同 `extract_subs.py` | 目录递归、重跑、超时、可执行文件 |

### merge_sources.py

| 参数 | 默认 | 说明 |
|---|---|---|
| `--heard` | 必填 | 听的那份 JSON（`transcribe.py` 产物） |
| `--seen` | 必填 | 看的那份 JSON（`extract_subs.py` 或 `ocr_subs.py` 产物） |
| `--out` | 必填 | 报告 `.md` 路径；同名 `.json` 写在旁边 |
| `--agree` | `0.85` | 相似度 ≥ 此值算一致 |
| `--near` | `0.55` | 相似度 ≥ 此值算接近，低于则不一致 |
| `--limit` | `40` | MD 最多列多少段（JSON 里是全量） |
| `--title` | 取 heard 的 tag | 报告标题 |

### transcribe.py

| 参数 | 默认 | 说明 |
|---|---|---|
| `--wav` | 必填 | 16 kHz 单声道 PCM WAV |
| `--model` | 必填 | CTranslate2 目录（5 个文件） |
| `--out` | 必填 | 输出 `.json` 路径；`.md` 写在旁边 |
| `--tag` | 空 | 任务标识，缺省取输出文件名 |
| `--device` | `cuda` | `cuda` 或 `cpu` |
| `--compute` | 自动 | `cuda` 下 `float16`，`cpu` 下 `int8` |
| `--language` | `zh` | 识别语言 |
| `--beam-size` | `5` | 束搜索宽度 |
| `--min-silence` | `300` | VAD（语音活动检测）最小静音，毫秒 |
| `--no-vad` | 关 | 关闭 VAD |
| `--no-prompt` | 关 | 关闭 `initial_prompt`。**只用于消融，不要用于生产**：关掉后标点密度几乎归零 |
| `--hallucination-silence-threshold` | 未设 | 幻觉静音阈值，透传给 faster-whisper |
| `--cpu-threads` | `8` | CPU 线程数 |
| `--no-fallback` | 关 | 禁止回退，原样报 CUDA 错误 |

### batch_transcribe.py

| 参数 | 默认 | 说明 |
|---|---|---|
| `--input` | 必填 | 一个或多个目录，也可显式给文件 |
| `--out-dir` | 必填 | 产物目录，`_batch/summary.json` 也在这里 |
| `--model` | 空 | `--dry-run` 时可省 |
| `--pattern` | `*.wav` | 文件名模式，大小写不敏感；只筛名字，不加解码器 |
| `--recursive` | 关 | 递归子目录 |
| `--force` | 关 | 忽略已有产物重跑 |
| `--dry-run` | 关 | 只打印计划 |
| `--timeout` | `1800` | 单文件超时，秒 |
| `--device` / `--compute` / `--language` | 同单条 | 透传给 `transcribe.py` |

### qc_check.py 与 check_env.py

| 参数 | 默认 | 说明 |
|---|---|---|
| `qc_check.py files...` | 必填 | JSON 文件或 glob |
| `qc_check.py --gap` | `8.0` | 长间隙阈值，严格大于 |
| `qc_check.py --tail` | `10.0` | 尾部空告警阈值 |
| `qc_check.py --json` | 关 | 机器可读输出 |
| `check_env.py --models` | 无 | 要核验的模型目录，可多个 |
| `check_env.py --json` | 关 | 机器可读输出 |

## 项目结构

```text
asr-transcription/
├── SKILL.md                  # 主文件（106 行）：触发条件 + 流程骨架 + 路由表 + 三条铁律
├── README.md / README_EN.md  # 本文件与英文版
├── ROADMAP.md                # 版本记录、条件触发项、红线、维护自检
├── NOTICE.md                 # 授权范围与第三方归属声明
├── LICENSE                   # MIT
├── references/               # 10 个文件，按问题路由，一次只读一个
│   ├── 01-selection.md       # 选型、五问决策、参考量级
│   ├── 02-workflow.md        # 标准流程、素材来源、批处理工程
│   ├── 03-parameters.md      # 参数、GPU DLL、回退、版本锁
│   ├── 04-quality.md         # 质检、三查、参照物、幻觉形态
│   ├── 05-vocabulary.md      # 专有名词
│   ├── 06-environment.md     # 环境、资源护栏、安装下载
│   ├── 07-pitfalls.md        # 踩坑清单
│   ├── 08-methodology.md     # 方法论底线、未决事项
│   ├── 09-attribution.md     # 第三方归属（本文表格的完整版）
│   └── 10-commands.md        # 命令速查（bash + PowerShell）
├── scripts/
│   ├── extract_subs.py       # 看：抽文本字幕轨，产出同 schema 的 JSON + MD
│   ├── ocr_subs.py           # 看：画面硬字幕 OCR（PP-OCR，逐帧读下半幅）
│   ├── merge_sources.py      # 听看互校，只列不一致的段落
│   ├── transcribe.py         # 听：转写，产出 JSON + MD
│   ├── batch_transcribe.py   # 串行批处理
│   ├── qc_check.py           # 输入校验 + 四项粗筛 + 三查
│   └── check_env.py          # 只读环境自检
├── tests/                    # 六套零依赖测试，共 1381 条断言
└── docs/
    ├── 转写经验_通用版.md      # 未拆分的完整原文；与 references 冲突时以 references 为准
    ├── 字幕识别与OCR_调研与设计.md  # 三条通路与实测数据；字幕轨 → v0.6，硬字幕 → v0.7
    └── 参考项目评估_native-subtitle-quote-image.md  # 上游项目可移植技术评估（字幕带定位、帧差异度）
```

三级加载：`name` + `description` 常驻 → 触发后读 `SKILL.md` → 只按需读一个 `references/` 文件。脚本可以直接执行，不必读进上下文。

## 开发

### 测试

```powershell
python -m compileall -q scripts tests
python tests/run_smoke.py            # 质检契约：37 类输入 × 文本/JSON
python tests/run_transcribe_unit.py  # 回退决策、耗时口径、产物原子写入
python tests/run_batch_unit.py       # 批处理规划、跳过判据、端到端集成
python tests/run_subs_unit.py        # 字幕解析、选轨、外挂回落
python tests/run_ocr_unit.py         # 文本清洗、字幕带定位、cue 合并
python tests/run_merge_unit.py       # 对齐、相似度分级、时间偏移、报告结构
```

实跑结果（2026-11-06）：

```text
37 cases x (text + --json), 1026 assertions, 0 failures.
43 assertions, 0 failures.
102 assertions, 0 failures.
79 assertions, 0 failures.
73 assertions, 0 failures.
58 assertions, 0 failures.
```

六套都不需要网络、模型、GPU 或 ffmpeg。批处理的端到端用例用一个假 `transcribe.py` 驱动 `main()`，覆盖成功、失败、跳过、修复后重试、半成品产物；字幕单测把 `ffprobe` 指向不存在的程序，验证外挂字幕在无探针时仍能成稿；OCR 与互校两套只测纯函数，所以装不装 OCR 运行时都能跑。

### Windows 上的「假绿」陷阱

测试 harness 会给子进程注入 `PYTHONUTF8=1` 与 `PYTHONIOENCODING=utf-8`，否则中文 Windows 控制台按 GBK 解码，测试会红在编码上而不是逻辑上。代价是：**在本机跑绿，不代表干净环境跑绿**。

涉及控制台或子进程的改动，提交前清空这两个变量再跑一遍：

```powershell
Remove-Item Env:PYTHONUTF8, Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
python tests/run_smoke.py
```

### 改动红线

1. **不破坏 `--json` 现有键**：`error` / `red` / `warn` / `duration` / `has_timestamps` / `note` 与全部指标键保留，新字段只能是附加。
2. **不引入新依赖、不联网、不上传素材**：测试必须零依赖、可离线重复。
3. **不放音频、模型权重、逐字稿、凭据或本机绝对路径进仓库**。
4. **`SKILL.md` 保持精简**（106 行）：新增细节下放 `references/`，架构说明放 `README.md`，版本规划放 `ROADMAP.md`。
5. **ERROR ≠ 判错**：ERROR 是输入数据问题，RED 是需人工复核。两者都不能写成「稿子转错了」。

细节见 [`ROADMAP.md`](ROADMAP.md) 第三节与 [`references/08-methodology.md`](references/08-methodology.md)。

## 常见问题

**Q：`check_env.py` 说 DLL 文件存在，但一跑就报 `cublas64_12.dll not found`？**
A：文件存在不等于能加载。cuBLAS / cuDNN 通常在**用户级** `site-packages` 下，不在解释器目录。先 `python -c "import site; print(site.getusersitepackages())"` 确认路径，再看 `check_env.py` 报的目录是否进了 `PATH`。看完仍不行，就真跑一句推理验证，别只看文件列表。

**Q：`pip install av` 装了 19.x，`decode_audio` 就报错？**
A：锁到 `av<19`。faster-whisper 1.2.1 这个发布版没带 PyAV 19 的适配（上游主分支已修，但没随这个 release 发布）。同理，Python 3.14 不在支持范围。

**Q：为什么没有 `--word-timestamps`？**
A：`transcribe.py` 输出只有段级 `s` / `e` / `t`，从来不写 `words`。留一个只影响解码、不改变输出的开关会让人以为拿到了词级时间戳，所以移除。词级、说话人分离、SRT 导出都在 `ROADMAP.md` 的 C3，未排期。

**Q：`--json` 重定向到文件，用别的工具读不了？**
A：v0.4.3 起，非 TTY 输出强制 UTF-8。旧版本在中文 Windows 上是 GBK 字节，不是合法 JSON。升级到 v0.4.3 或更新即可；TTY 下仍用本机代码页，屏幕显示不受影响。

**Q：批量跑到一半被我 Ctrl+C 了，重跑会不会重新转一遍？**
A：不会。判据是产物本身：`<key>.json` 可解析、无 `error`、含 `segments` 与 `duration`，且 `<key>.md` 存在非空。已完成的两份都在，直接跳过；只写了 JSON 没写 MD 的会补跑。

**Q：某个文件明明失败了，重跑却一直跳过？**
A：v0.5.1 已修。现在 `--force` 会把 JSON 和 MD 一起改名成 `.prev`，失败后活路径上没有旧产物，下一轮会重试。如果你还在 v0.5 或更早，先删掉那份旧 `.json` 和 `.md`。

**Q：能不能给一个准确率？**
A：不能。准确率要有真人逐字校对的标准答案做分母。没有就只报「两边哪里不一样」加人工抽听。画面字幕 OCR 只能当旁证，不能当逐字真值。

**Q：支持 mp3 / m4a 吗？**
A：不支持直接喂。先 `ffmpeg -i in.m4a -vn -ac 1 -ar 16000 -c:a pcm_s16le out.wav`。`--pattern "*.m4a"` 只会把文件收进来，然后在解码时失败。

## 已知限制

### 已验证的平台

| 环境 | 状态 | 说明 |
|---|---|---|
| Windows 11 + Python 3.13 + NVIDIA GPU（CUDA 12.x / cuDNN 9，≥ 8 GB 显存） | **已实测** | 全部脚本、主力链路、质检回归均在此配置跑通 |
| 同机纯 CPU（`--device cpu --compute int8`） | **已实测单条与批量** | 速度约 2.6–2.9×；质检回归集以 GPU 批次为主 |
| Windows 10 / 其他 NVIDIA 显卡 / 显存 < 6 GB | 未验证 | 按 `references/06-environment.md` 自行复核；显存不足改用 turbo 或 CPU |
| Linux / macOS / AMD / Intel GPU | 未验证，不建议照抄 | DLL 目录、PowerShell 示例、用户级 `site-packages` 路径都是 Windows 特定 |
| Python 3.14 / PyAV 19 / torch 系大模型 | 不在支持范围 | 版本锁见 `references/03-parameters.md` |

### 能力边界

- **只有段级时间戳**。没有词级 `words`，没有说话人分离，不能直接导出 SRT / VTT（字幕轨抽取产出的 `.srt` 是源文件自带的，不是转写生成的）。
- **"看"的能力分两级，都有边界**。有文本字幕轨或外挂 `.srt/.ass/.ssa/.vtt` 时 `extract_subs.py` 零推理直接抽（`engine=subtitle-track`）；没有就走 `ocr_subs.py` 逐帧 OCR（`engine=ocr`）。**图形字幕轨（PGS/VobSub/DVB）、竖排、花字、被遮挡的字仍然读不了**；OCR 只读画面下半幅，字幕在顶部就找不到。压在字幕**同一行**上的叠字也分不开——按纵向位置切只能分开不同行，分不开同一行里的两段文字；能被分开的是"另起一行、换得比口播慢"的那类固定文案（要点条、章节点、流程图），按变化节奏剔除，6 条素材共剔除 50 段；但逐帧都在变的图表仍会残留（25 条里 7 条）。它比转写慢（约 0.38 秒一帧），且需要额外装 OCR 运行时——没装时该脚本明确报错退出，另外五个脚本不受影响。
- **字号太小就读不准**。640×360 这类素材上一行字只有十几像素，同一条字幕在两种留白下会读出不同结果（实测一致率 0.815）；它的产物仍然可用，但错字率明显高于竖屏 720p/1080p。
- **互校只对齐，不裁决**。`merge_sources.py` 给出"哪几段两边不一致"，谁对谁错要回到音频和画面判断。它不产出合并稿，也不自动改任何一边。
- **单文件失败不落盘**。直接调 `transcribe.py` 失败时没有产物，只能靠退出码判断。批量场景已覆盖（`_batch/summary.json` 记退出码与错误尾部），单文件场景仍是 `ROADMAP.md` 的 C4。
- **不报准确率**。没有人工标准答案就不报，这是硬规则，不是谦虚。
- **SenseVoice 只当第二意见**。它输出没有可用时间戳，中英混读的专名明显弱于主力，不能单独出正式稿。
- **模型要自己下**。仓库不含权重、不含音频、不含任何凭据。
- **没有目录级汇总报表**。`_batch/summary.json` 是逐文件结果，不做跨目录统计。

### 性能数字的边界

| 方案 | 实时倍率 | 运行内存增量 | 显存增量 | 能单独出正式稿 |
|---|---|---|---|---|
| turbo / GPU | 约 19× | 约 1.2–1.6 GB | 约 1.9–2.1 GB | 是 |
| large-v3 / GPU | 约 6× | 约 3.0–3.1 GB | 约 3.8–3.9 GB | 能，但代价大，只做疑难复核 |
| turbo / CPU | 约 2.6–2.9× | 约 1.4–1.5 GB | 0 | 慢 |
| SenseVoice q8 GGUF / CPU | 约 27–28× | 约 0.36–0.37 GB | 0 | 预览 / 对照 / 漏段线索 |

出处：2026-11-05 单机实测，**4 段**中文短口播（1.31 / 1.50 / 2.12 / 3.90 分钟），**9 种配置共 28 次运行**，波动约 ±30%。样本量是个位数，只能判断相对关系，**不是跨机型基准**，长音频、会议、朗读都不能外推。完整表与口径见 `references/01-selection.md` §1.7。

## 第三方项目与许可证

本仓库不训练、不发布任何模型，只在流程里调用下列项目。**代码许可与模型权重许可是两件事**，商用前以当次官方仓库和模型卡为准。完整登记（含核实日期与局限）见 [`references/09-attribution.md`](references/09-attribution.md)。

### 实际采用的主链路

| 项目 / 模型 | 维护方 | 地址 | 许可证 | 用在哪 |
|---|---|---|---|---|
| OpenAI Whisper（`large-v3`、`large-v3-turbo` 权重） | OpenAI | https://github.com/openai/whisper | MIT | 主力模型权重；turbo 是 large-v3 的解码器剪枝版 |
| faster-whisper | SYSTRAN | https://github.com/SYSTRAN/faster-whisper | MIT | 主引擎与全部转写参数 |
| CTranslate2 | OpenNMT | https://github.com/OpenNMT/CTranslate2 | MIT | faster-whisper 的推理后端 |
| PyAV（`av`） | PyAV-Org | https://github.com/PyAV-Org/PyAV | BSD-3-Clause | 音频解码；依赖锁 `av<19` |
| CUDA / cuBLAS / cuDNN | NVIDIA | https://developer.nvidia.com/ | NVIDIA 专有 | GPU 推理算子库 |

### 补充与对照（实际参测）

| 项目 / 模型 | 维护方 | 地址 | 许可证 | 用在哪 |
|---|---|---|---|---|
| SenseVoice / `SenseVoiceSmall` | QwenAudio（原 FunAudioLLM，仓库已迁移） | https://github.com/QwenAudio/SenseVoice | **代码 MIT / 权重 `license: other`（FunASR Model License）** | 纯 CPU 的异源补充方案 |
| FunASR | modelscope（阿里达摩院语音团队） | https://github.com/modelscope/FunASR | MIT | GGUF 运行时、VAD、标点、热词流水线 |
| FunASR llama.cpp runtime | modelscope/FunASR 发布 | https://github.com/modelscope/FunASR/releases | 随 FunASR（MIT） | `llama-funasr-sensevoice.exe` 等 Windows 二进制 |
| llama.cpp / GGUF | ggml-org | https://github.com/ggml-org/llama.cpp | MIT | q8 量化模型与运行时基础 |
| sherpa-onnx | k2-fsa | https://github.com/k2-fsa/sherpa-onnx | Apache-2.0 | SenseVoice / Paraformer 的 int8 ONNX 对照 |
| Paraformer | modelscope/FunASR（阿里达摩院） | https://github.com/modelscope/FunASR | 代码 MIT / 权重按模型卡 | 「无标点」对照，不推荐单独使用 |

### 仅在选型中提及、未采用

| 项目 | 维护方 | 地址 | 许可证 |
|---|---|---|---|
| Qwen3-ASR | QwenLM（阿里云通义千问团队） | https://github.com/QwenLM/Qwen3-ASR | Apache-2.0 |
| WhisperX | `m-bain`（个人项目） | https://github.com/m-bain/whisperX | BSD-2-Clause |
| whisper.cpp | ggml-org | https://github.com/ggml-org/whisper.cpp | MIT |
| PyTorch / Transformers / vLLM | 各自开源组织 | 官方文档 | 各项目许可；本仓库明确**不安装** |

### 看：字幕与 OCR

| 项目 / 模型 | 维护方 | 地址 | 许可证 | 用在哪 |
|---|---|---|---|---|
| RapidOCR | RapidAI | https://github.com/RapidAI/RapidOCR | Apache-2.0 | `ocr_subs.py` 的检测 + 识别封装，含模型 |
| PP-OCRv3（`ch_PP-OCRv3_det` / `_rec` / `ch_ppocr_mobile_v2.0_cls`） | 百度 PaddleOCR 团队 | https://github.com/PaddlePaddle/PaddleOCR | Apache-2.0 | 随 RapidOCR wheel 分发的三个 ONNX，合计 13.08 MB |

这三个 ONNX 是**权重**，按上面的 Apache-2.0 使用；RapidOCR 的封装代码同为 Apache-2.0。仓库自身不重分发任何模型文件，`pip install` 时装上。

### 通用工具与平台

| 项目 | 维护方 | 地址 | 许可证 | 用途 |
|---|---|---|---|---|
| FFmpeg / ffprobe | FFmpeg 项目 | https://www.ffmpeg.org/ | LGPL / GPL，依构建选项 | 抽音频、素材体检 |
| ONNX Runtime | Microsoft | https://github.com/microsoft/onnxruntime | MIT | ONNX 对照方案的推理后端 |
| NumPy | NumPy 社区 | https://numpy.org/ | BSD-3-Clause | 随依赖带入；OCR 路径逐帧处理图像 |
| Pillow | Python Pillow 团队 | https://python-pillow.org/ | MIT-CMU | 抽帧后的裁剪与缩放（`ocr_subs.py`） |
| Hugging Face Hub / `hf-mirror.com` | Hugging Face / 第三方镜像 | https://huggingface.co/ | 平台服务，非依赖 | 模型下载 |

### 许可边界

- `LICENSE` 的 MIT **只覆盖本仓库原创内容**：文档、脚本、Skill 结构与组织方式。
- 第三方模型权重、推理引擎与运行时**不在授权范围内**，权利归各自维护方。
- 本仓库**不包含**任何权重文件（`*.bin` / `*.gguf` / `*.onnx`）、音频素材、转写产物或凭据。
- 转载或再分发请保留 `LICENSE` 与 `references/09-attribution.md` 的署名。

署名口径以官方仓库 owner 为准；归属会迁移（SenseVoice 已从 `FunAudioLLM` 迁到 `QwenAudio`），用前请复核。核实日期 2026-11-05，来源为各项目 GitHub REST API 与官方模型卡。**SenseVoiceSmall 权重许可本次 HF API 抓取失败，沿用了同日早前的核实结果，商用前必须再打开模型卡确认。**

## 如何贡献

本仓库由 ChenChen913 个人维护，没有商业支持。

- **报问题、提需求**：到 [Issues](https://github.com/ChenChen913/asr-transcription-skill/issues)。请附命令、退出码、错误原文，以及 `check_env.py` 的输出（去掉本机路径）。
- **PR**：欢迎，但没有固定流程，也不保证及时处理。改动前先开 Issue 说清要解决什么，避免大改被拒。
- **提交前**：六套测试全绿，并在清空 `PYTHONUTF8` / `PYTHONIOENCODING` 的环境复跑一遍（见[开发](#开发)）。
- **不要提交**：模型权重、音频素材、逐字稿、凭据、本机绝对路径。
- **不要为了接更多模型而加功能**。这个仓库的目标是转写质量与可靠工作流，不是模型数量。

## 许可证

[MIT](LICENSE) © 2026 ChenChen913

`LICENSE` 只含标准 MIT 正文，授权边界与第三方归属写在 [`NOTICE.md`](NOTICE.md)。第三方项目的权利归各自维护方，本仓库不对它们主张任何权利。
