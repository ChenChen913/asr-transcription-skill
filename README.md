# asr-transcription · 音视频转写成可读文稿 Skill

一个可随时调用的 WorkBuddy Skill：把「选方案 → 装环境 → 抽音频 → 转写 → 交叉核对 → 质检 → 精编」的完整经验，做成主文件精简、细节按需加载的技能包。

- 版本：v0.5（2026-11-06）｜仓库已公开｜许可证 **MIT**（见 `LICENSE`）；授权范围与第三方归属见 `NOTICE.md`
- 内容来源：本机多轮实测沉淀的《转写经验_通用版》（完整原文见 `docs/转写经验_通用版.md`）
- 适用：本地 / 离线、Windows、中文为主且含英文术语的视频转写
- 版本规划与已修问题见 `ROADMAP.md`

---

## 0. Quick Start（最短路径）

以下 6 步只覆盖「跑通第一条转写」。命令中的 `<模型目录>` 指含 5 个 CTranslate2 文件的目录（见 §4），`<编号>.wav` 是 16 kHz 单声道音频。**本机已装好环境的可跳过第 2 步。**

```bash
# 1. 检查 Python（建议 3.13.x）
python -V

# 2. 安装依赖（本机已就绪则跳过；av 必须 < 19）
python -m pip install "faster-whisper==1.2.1" "ctranslate2==4.8.1" "av<19"

# 3. 环境诊断（可传多个模型目录，只读、不写文件）
python scripts/check_env.py --models "<模型目录>"

# 4. 从视频抽统一音频；已有 WAV 可跳过
ffmpeg -y -i "<素材>.mp4" -vn -ac 1 -ar 16000 -c:a pcm_s16le "<编号>.wav"

# 5. 转写一条（GPU 失败自动回退 CPU）
python scripts/transcribe.py --wav "<编号>.wav" --model "<模型目录>" \
    --out "out/A1_<编号>.json" --tag "A1_<编号>"

# 6. 质检 + 自检测试
python scripts/qc_check.py "out/A1_<编号>.json"
python tests/run_smoke.py          # 质检契约：33 类输入 / 883 条断言
python tests/run_transcribe_unit.py  # 回退决策、耗时口径、产物原子写入：43 条断言
python tests/run_batch_unit.py     # 批处理规划 / 跳过判据 / 端到端：102 条断言
```

**整文件夹跑**（串行、跳过已完成、失败隔离）：

```bash
python scripts/batch_transcribe.py --input "<素材目录>" ["<素材目录2>"] \
    --out-dir "out" --model "<模型目录>"
python scripts/batch_transcribe.py ... --dry-run     # 只看计划，不执行，不需要模型
python scripts/batch_transcribe.py ... --force       # 忽略已有输出重跑
```

每个输入得到一份独立的 `<key>.json` + `.md`（与单跑同 schema），两者是**一组**：跳过判据要求两份都完整，所以只写完 JSON 就被中断的文件会下一轮自动补跑。汇总在 `out/_batch/summary.json`；**退出码 0 全成、1 有失败或超时、2 用法/匹配错误**。重跑同一条命令即只重试失败项。`--pattern` 只筛文件名，不增加解码能力：输入必须是 PCM WAV，其他格式先 FFmpeg 转，匹配到非 `.wav` 时开跑前会打 `[WARN]`。

**输出**：`out/A1_<编号>.json`（结构化：`duration` / `device` / `segments[{s,e,t}]` / `stats`）+ 同名 `.md`（带时间戳可读版）。`qc_check.py` 退出码 0 = 无需复核，1 = 有 ERROR 或 RED/REVIEW；**RED 表示「需人工复核」，不等于判错**。

**排查入口**：GPU / DLL 报错 → `references/03-parameters.md`；装不上 / 下载慢 → `references/06-environment.md`；结果不对 → `references/07-pitfalls.md`；质检阈值与 ERROR/REVIEW 含义 → `references/04-quality.md`。

---

## 1. 借鉴与采用的开源项目

本 Skill 本身不训练、不发布任何模型，只在流程中调用下列第三方项目。**代码许可与模型权重许可必须分开看**，商用前以当次官方仓库和模型卡为准。完整归属（含用途、核实日期）见 `references/09-attribution.md`。

### 实际采用的链路

| 项目 / 模型 | 维护方 / 作者 | 官方地址 | 许可证 | 本 Skill 用到 |
|---|---|---|---|---|
| OpenAI Whisper（`large-v3`、`large-v3-turbo` 权重） | OpenAI | https://github.com/openai/whisper | MIT | 主力模型权重；turbo 是 large-v3 的解码器剪枝版 |
| faster-whisper | SYSTRAN | https://github.com/SYSTRAN/faster-whisper | MIT | 主引擎与全部转写参数 |
| CTranslate2 | OpenNMT | https://github.com/OpenNMT/CTranslate2 | MIT | faster-whisper 的推理后端 |
| PyAV | PyAV-Org | https://github.com/PyAV-Org/PyAV | BSD-3-Clause | 音频解码依赖（需锁 `av<19`） |
| CUDA / cuBLAS / cuDNN | NVIDIA | https://developer.nvidia.com/ | NVIDIA 专有 | GPU 推理算子库 |
| SenseVoice / `SenseVoiceSmall` | QwenAudio（原 FunAudioLLM，仓库已迁移） | https://github.com/QwenAudio/SenseVoice | **代码 MIT / 权重 `license: other`（FunASR Model License）** | 纯 CPU 的异源补充方案 |
| FunASR | modelscope（阿里 ModelScope / 达摩院语音团队） | https://github.com/modelscope/FunASR | MIT | GGUF 独立运行时、VAD、标点、热词流水线 |
| FunASR llama.cpp runtime | modelscope/FunASR 发布 | https://github.com/modelscope/FunASR/releases | 随 FunASR（MIT） | `llama-funasr-sensevoice.exe` 等 Windows 二进制 |
| llama.cpp / GGUF | ggml-org | https://github.com/ggml-org/llama.cpp | MIT | q8 量化模型与运行时基础 |
| sherpa-onnx | k2-fsa | https://github.com/k2-fsa/sherpa-onnx | Apache-2.0 | SenseVoice / Paraformer 的 int8 ONNX 对照方案 |
| Paraformer | modelscope/FunASR（阿里达摩院） | https://github.com/modelscope/FunASR | 代码 MIT / 权重按模型卡 | 「无标点」对照方案（不推荐独立使用） |

### 仅在选型中提及、未采用

| 项目 | 维护方 / 作者 | 官方地址 | 许可证 |
|---|---|---|---|
| Qwen3-ASR | QwenLM（阿里云通义千问团队） | https://github.com/QwenLM/Qwen3-ASR | Apache-2.0 |
| WhisperX | `m-bain`（个人项目） | https://github.com/m-bain/whisperX | BSD-2-Clause |
| whisper.cpp | ggml-org | https://github.com/ggml-org/whisper.cpp | MIT |

### 通用工具与平台

| 项目 | 维护方 | 官方地址 | 许可证 | 用途 |
|---|---|---|---|---|
| FFmpeg / ffprobe | FFmpeg 项目 | https://ffmpeg.org/ | LGPL / GPL，依构建选项而定 | 抽音频、素材体检 |
| ONNX Runtime | Microsoft | https://github.com/microsoft/onnxruntime | MIT | ONNX 与 OCR 推理后端 |
| NumPy | NumPy 社区 | https://numpy.org/ | BSD-3-Clause | 随依赖带入 |
| RapidOCR | RapidAI | https://github.com/RapidAI/RapidOCR | Apache-2.0 | 可选的关键帧 OCR 旁证 |
| Hugging Face Hub / `hf-mirror.com` | Hugging Face / 第三方镜像 | https://huggingface.co/ | 平台服务，非依赖 | 模型下载镜像 |

> 未列出论文作者名单，一律以官方仓库 owner（组织或个人账号）署名，避免写错。仓库归属会迁移，用前请复核。核实日期 2026-11-05，来源为各项目 GitHub REST API 与官方模型卡；SenseVoiceSmall 权重许可沿用同日早前模型卡核实结果，本次 HF API 抓取失败，商用前必须复核。

---

## 2. 本机环境配置（实测快照）

以下为 **2026-11-05** 实测值，用于解释本 Skill 里的速度、内存、显存数字是怎么来的。**这些是环境快照，不是 Skill 的硬性要求**；换机器请按 `references/06-environment.md` 逐条复核，不要照抄。

> 本仓库已公开。下表只是**示例环境快照**，用于说明数字来源，不含用户名、绝对路径或任何凭据；读者请替换为自己的配置。

| 项 | 实测值 |
|---|---|
| 操作系统 | Microsoft Windows 11 家庭版 中文版，build 26300 |
| CPU | AMD Ryzen 7 7840H with Radeon 780M Graphics，8 核 / 16 线程 |
| 运行内存 | 总计 **15.19 GB**；实测可用 **6.00 GB**（历史批次可用 4.16–4.50 GB，是主要瓶颈） |
| 显卡 / 显存 | NVIDIA GeForce RTX 4060 Laptop GPU，**8188 MiB**；实测空闲 6808 MiB |
| 驱动 / CUDA | 驱动 560.94；CUDA 12.6；识别到 1 块 CUDA 设备 |
| 磁盘 | C 盘 200.0 GB 总 / **25.7 GB 可用**；D 盘 274.7 GB 总 / **22.4 GB 可用** |
| Python | 3.13.3 |
| 关键依赖 | faster-whisper 1.2.1、CTranslate2 4.8.1、onnxruntime 1.24.2、av 18.0.0、numpy 2.3.3 |
| FFmpeg | N-125829（2026-07-28 构建） |
| 已装模型 | turbo 约 1.51 GiB、large-v3 约 2.88 GiB，合计 4.39 GiB（目录 5 文件口径） |
| torch | **未安装**（本链路刻意避开） |

**由这套配置得出的关键约束**：可用运行内存常低于 6 GB，比显存更早成为瓶颈；因此排除一切需要 torch / vLLM 的重型方案，所有引擎串行执行、一次只加载一个模型，并对峰值内存设熔断线。

---

## 3. 已验证范围与依赖要求

**这是给新用户的判断依据：先看自己属不属于「已验证」，再决定要不要照着做。**

| 环境 | 状态 | 说明 |
|---|---|---|
| Windows 11 + Python 3.13 + NVIDIA GPU（CUDA 12.x / cuDNN 9）+ ≥8 GB 显存 | ✅ **已实际验证** | 全部脚本、主力链路、质检流程均在本配置跑通 |
| 同机 **纯 CPU**（`--device cpu` 或 GPU 初始化失败后回退） | ⚠️ 理论可用，仅部分验证 | `transcribe.py` 有自动回退，实测约 2.6–2.9× 实时；但 CUDA DLL 前置、质检回归集均以 GPU 批次为主 |
| Windows 10 / 其他 NVIDIA 显卡 / 显存 < 6 GB | ⚠️ 理论可能支持，**未验证** | 需自行按 `references/06-environment.md` 复核；显存不足时改用 turbo / CPU |
| Linux / macOS / AMD 或 Intel GPU | ❌ **未验证，不建议照抄** | 脚本里的 DLL 目录、PowerShell 示例、用户级 site-packages 路径均为 Windows 特定 |
| Python 3.14+ / PyAV ≥ 19 / 装了 torch 系大模型 | ❌ 当前不支持 | faster-whisper 1.2.1 发布版未含 av19 / 3.14 适配；torch 会挤爆可用内存 |

### 关键依赖版本

| 组件 | 本机实测 | 要求 / 备注 |
|---|---|---|
| Python | 3.13.3 | 3.13.x；**不要升 3.14** |
| faster-whisper | 1.2.1 | 最新正式 release 仍是 1.2.1；仓库本身仍活跃，**「release 滞后」≠「项目停更」** |
| PyAV (`av`) | 18.0.0 | **必须 `av<19`**，19.0.0 会破坏 `decode_audio` |
| CTranslate2 | 4.8.1 | 4.8.x；上游已更新到 4.8.5，本机未升级 |
| onnxruntime | 1.24.2 | 仅异源 ONNX 方案需要 |
| CUDA / cuBLAS / cuDNN | 12.6 / 12.x / 9.x | GPU 推理必需；DLL 文件存在 ≠ 能加载，务必真跑一句推理 |
| FFmpeg / ffprobe | N-125829 | 需在 PATH |
| torch | 未安装 | **刻意不装**，装了内存不够 |

### CPU 回退：触发条件与判据

- **触发（已收窄）**：只有报错信息看起来**确实是 GPU / CUDA 问题**才回退——命中 `cuda / cublas / cudnn / cudart / nvml / gpu / out of memory / no kernel image`。典型：驱动版本不够、找不到 cuBLAS/cuDNN、显存不足。
- **不回退**：模型文件损坏或版本不符、音视频解不出、参数不合法、其他程序错误。这些换 CPU 一样失败，回退只会把真正的错误埋掉、白等一遍。
- **两个错误都保留**：回退后 CPU 也失败时，GPU 与 CPU 两侧的错误一起打印；GPU 原始异常同时写进输出的 `fallback` 字段。
- **性能影响**：从约 19× 实时降到约 2.6–2.9×，一条 3 分钟音频约需 1 分钟以上。倍率出处：4 段样本（1.31–3.90 分钟）/ 9 配置共 28 次运行 / 2026-11-05 单机实测，见 `references/01-selection.md` §1.7。
- **怎么判断实际用了哪个**：读输出 JSON 的 `device`（`cuda` / `cpu`）、`fallback`（非空即回退过，含原始异常）、`fallback_load_s`（失败那次尝试已完成的模型加载耗时，未完成则为 `null`）。**不要只看命令行 `--device` 入参**。
- **耗时口径**：`load_s` / `asr_s` 只描述**最终成功的设备**；`asr_s` 是纯推理，**不含模型加载**；`rtf_x = duration / asr_s`。
- 显式只要 CPU：`--device cpu --compute int8`；排查显卡问题时禁用回退：`--no-fallback`。
- **退出码**：`0` 成功｜`2` 输入或模型目录不可用｜`3` 当前设备失败且未回退｜`4` GPU 与 CPU 都失败。

---

## 4. 目录结构（渐进式加载）

```
asr-transcription/
├── SKILL.md                  # 主文件：流程骨架 + 路由表 + 铁律（精简）
├── README.md                 # 本文件：Quick Start + 开源归属 + 环境 + 使用说明
├── ROADMAP.md                # 版本规划、已修问题、后续条件触发项
├── LICENSE                   # 本 Skill 自身：MIT（纯标准正文，便于平台识别）
├── NOTICE.md                 # 授权范围：哪些是本仓库的、哪些属于第三方
├── .gitignore
├── references/               # 按需加载的详细参考，SKILL.md 里按问题路由
│   ├── 01-selection.md       # 速查 + 方案选型 + 五问决策
│   ├── 02-workflow.md        # 标准流程、素材来源、批处理工程
│   ├── 03-parameters.md      # 转写参数、GPU DLL、回退、补充方案
│   ├── 04-quality.md         # 质检、三查、参照物、抽帧边界、幻觉形态
│   ├── 05-vocabulary.md      # 专有名词
│   ├── 06-environment.md     # 环境、资源护栏、安装下载
│   ├── 07-pitfalls.md        # 踩坑清单
│   ├── 08-methodology.md     # 方法论底线、未决事项
│   ├── 09-attribution.md     # 第三方归属与许可证
│   └── 10-commands.md        # 命令速查（bash + PowerShell）
├── scripts/
│   ├── transcribe.py         # 真正执行转写，产出统一 JSON + MD
│   ├── batch_transcribe.py   # 串行批处理：跳过已完成 / 失败隔离 / 每文件独立 JSON
│   ├── check_env.py          # 只读环境自检（含 CUDA DLL 文件检查）
│   └── qc_check.py           # 输入校验(ERROR) + 四项粗筛 + 三查(RED/REVIEW)
│                             #   时间轴无效(负值/倒序/乱序) → 时间指标置 null
├── tests/
│   ├── run_smoke.py          # 零依赖回归：质检契约 33 类输入 / 883 条断言
│   ├── run_transcribe_unit.py # 零依赖单测：回退 / 耗时口径 / 产物原子写入 / DLL
│   └── run_batch_unit.py     # 零依赖单测 + 端到端（假 transcribe.py）
└── docs/
    └── 转写经验_通用版.md     # 完整原文归档；运行时以 references 为准
```

三级加载：`name` + `description` 常驻 → 触发后读 `SKILL.md` → 只按需要读某个 `references/` 文件。脚本可直接执行而不必读进上下文。

---

## 5. 能力归属（自动化到哪一步）

| 环节 | 归属 | 具体载体 |
|---|---|---|
| 环境自检 | ✅ 已自动化 | `scripts/check_env.py`（打印版本 / 显卡 / DLL / 磁盘 / 模型目录） |
| 主力转写 | ✅ 已自动化 | `scripts/transcribe.py`（GPU→CPU 回退、统一 JSON + MD） |
| 结果质检 | ✅ 已自动化 | `scripts/qc_check.py`（ERROR / RED 分级、退出码可进 CI） |
| 批量转写 | ✅ 已自动化 | `scripts/batch_transcribe.py`（串行、跳过已完成、失败隔离、每文件独立 JSON+MD + `_batch/summary.json`） |
| 回归测试 | ✅ 已自动化 | `tests/run_smoke.py` + `tests/run_transcribe_unit.py` + `tests/run_batch_unit.py`（不依赖网络 / 模型 / GPU） |
| 抽音频、异源方案对照、差异对齐、精编、留痕 | 🤝 **Agent 工作流** | Skill 指导 Agent 调命令、判读结果、组织人工复核；**没有对应脚本** |
| 首末段回听、建术语表、提供标准答案、商用许可核对 | 👤 手动 | 需人工判断 |

> **SenseVoice q8 GGUF 当前状态**：可选的独立对照方案，命令见 `references/03-parameters.md` / `10-commands.md`；需要自行下载 FunASR GGUF 运行时与权重，**仓库不含这些二进制，也没有封装脚本**。它输出没有可用时间戳，只能当纯文本第二意见。
>
> **批处理**已不再是文档约定：`scripts/batch_transcribe.py` 只做四件事——**串行**（一次一个模型实例）、**跳过已完成**、**失败隔离**（单文件非零退出或超时不影响其余）、**每文件独立 JSON**。没有任务队列、没有并发、没有第二份进度文件：**输出产物本身就是状态**。
>
> 一个任务是**一组成套产物**（`<key>.json` + `<key>.md`），所以跳过判据也是这一组：JSON 可解析、无 `error`、含 `segments`/`duration`，且 MD 存在非空。写入端配合这一点——`transcribe.py` 先把两份都写到 `.tmp`，**先改名 MD、后改名 JSON**，因此「有 JSON ⇒ 有 MD」，被中断的文件下一轮一定会补跑而不是被永久跳过。`--force` 同样把两份一起改名成 `<key>.json.prev` / `<key>.md.prev`：只挪 JSON 会把旧逐字稿留在原位，而 `.md` 恰恰是人会读的那份。工程要点见 `references/02-workflow.md` §2.2。

---

## 6. 安装与使用

### 通用约定

- 本 Skill 遵循标准 Skill 结构：`SKILL.md`（YAML frontmatter 含 `name` / `description`）+ `references/` + `scripts/`；主文件精简、细节按需加载。只要某工具支持这一约定，原则上可放置使用。
- ✅ **已实际验证**：WorkBuddy 用户级 Skill（`~/.workbuddy/skills/asr-transcription/`）。
- ⚠️ **未实测的其他 Agent / CLI**：按各自官方文档规定的 Skill 目录与元数据格式放置。**本仓库不声称对这些工具已兼容**，安装后请自行验证。
- 脚本是普通 Python 3 脚本，也可以脱离 Skill 机制直接按 §0 命令行调用。

### 安装后自检清单

1. Skill 是否被目标工具识别（能触发转写类提问）？
2. `scripts/transcribe.py`、`check_env.py`、`qc_check.py` 路径是否正确、能否 `--help`？
3. `python -c "import faster_whisper, ctranslate2, av"` 是否无报错？
4. `python tests/run_smoke.py` 与 `python tests/run_transcribe_unit.py` 是否全过（不需要模型）？
5. 能否用一条自己的 WAV 完成一次最小转写并读到 `device` 字段？

安装失败时按上述顺序定位：Skill 未被识别 → 工具约定问题；`--help` 失败 → 脚本路径问题；import 失败 → 运行依赖问题。

### 触发方式

直接描述任务，例如「把这一批视频转成逐字稿」「转写结果开头不对，帮我排查」「要不要补一个中文识别模型」。命令见 §0 与 `references/10-commands.md`（含 Windows PowerShell 写法）。

### 质检结果怎么读

| 标记 | 含义 | 动作 |
|---|---|---|
| `[ERROR]` / `"errors"` | 确定的输入数据问题（字段缺失、非数字、**非有限数值 NaN/Infinity**、负数、乱序、duration 无效） | 修输入或产出脚本；**不要当成"稿子转错了"** |
| ↳ 时间轴失效 | 负数起点 / `end < start` / 乱序：数值能读，但时间轴不成立 | 三个时间指标置 `null`、`has_timestamps=false`（`note` 写 `INVALID TIMELINE`）；`chars` 等文本指标照常可用 |
| `[RED/REVIEW]` / `"red"` | 风险信号（重叠、空文本、越界、长间隙、提示语回音、无时间戳） | 人工复核对应区间；**不等于判错** |
| `[warn]` | 粗筛指标越界（密度、段数、尾部空） | 参考，不单独触发退出码 |

阈值严格大于：`gap > 8.00 s`（8.00 不报、8.01 报）、`overrun > 0.50 s`。可用 `--gap` / `--tail` 调整。文本与 `--json` 模式都在有 ERROR 或 RED 时返回退出码 1。

任一时间戳无效（缺失、非数字、NaN/Infinity）时，`max_gap_s` / `tail_gap_s` / `tail_overrun_s` 输出 `null`、`note` 写明原因，**不会用 0.00 之类的占位值编数字**；文本类指标照常输出。`--json` 始终是严格 JSON，不会出现 `NaN` / `Infinity`。

---

## 7. 边界与免责

- 本仓库**不含**任何模型权重、测试音视频、逐字稿、访问令牌或本机绝对路径形式的凭据；只含方法、流程与可复用脚本。
- 速度与资源数字是本机实测的量级参考，随机器、素材、后台负载波动，不构成性能承诺；适用范围见 §3，**未验证的平台不写成已支持**。
- 没有人工标准答案时不报准确率；画面字幕 OCR 只能做旁证，不能当逐字真值。
- 本 Skill 自身的文档与脚本采用 **MIT**（见 `LICENSE`）；`LICENSE` 只含标准 MIT 正文，授权边界与第三方归属写在 `NOTICE.md`。这不改变任何第三方项目的许可。
- 所有第三方项目的权利归各自维护方；本仓库不对它们主张任何权利。商用前请逐项核对代码许可与权重许可。
