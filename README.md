# asr-transcription · 音视频转写成可读文稿 Skill

一个可随时调用的 WorkBuddy Skill：把「选方案 → 装环境 → 抽音频 → 转写 → 交叉核对 → 质检 → 精编」的完整经验，做成主文件精简、细节按需加载的技能包。

- 版本：v0.2（2026-11-05）｜仓库已公开
- 内容来源：本机多轮实测沉淀的《转写经验_通用版_2026-11-05.md》（完整原文见 `docs/转写经验_通用版.md`）
- 适用：本地 / 离线、Windows、中文为主且含英文术语的视频转写

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
| 已装模型 | `D:\hf-models\faster-whisper-large-v3-turbo`（1.51 GiB）、`faster-whisper-large-v3`（2.88 GiB），合计 4.39 GiB |
| torch | **未安装**（本链路刻意避开） |

**由这套配置得出的关键约束**：可用运行内存常低于 6 GB，比显存更早成为瓶颈；因此排除一切需要 torch / vLLM 的重型方案，所有引擎串行执行、一次只加载一个模型，并对峰值内存设熔断线。

---

## 3. 目录结构（渐进式加载）

```
asr-transcription/
├── SKILL.md                  # 主文件：只放流程骨架 + 路由表 + 三条铁律（< 5k 词）
├── README.md                 # 本文件：开源归属 + 本机配置 + 使用说明
├── references/               # 按需加载的详细参考，SKILL.md 里按问题路由
│   ├── 01-selection.md       # 速查 + 方案选型
│   ├── 02-workflow.md        # 标准流程、素材来源、批处理工程
│   ├── 03-parameters.md      # 转写参数、GPU DLL、回退、补充方案
│   ├── 04-quality.md         # 质检、三查、参照物、抽帧边界、幻觉形态
│   ├── 05-vocabulary.md      # 专有名词
│   ├── 06-environment.md     # 环境、资源护栏、安装下载
│   ├── 07-pitfalls.md        # 踩坑清单
│   ├── 08-methodology.md     # 方法论底线、未决事项
│   ├── 09-attribution.md     # 第三方归属与许可证
│   └── 10-commands.md        # 命令速查
├── scripts/
│   ├── transcribe.py         # 真正执行转写，产出统一 JSON + MD
│   ├── check_env.py          # 只读环境自检（含 CUDA DLL 文件检查）
│   └── qc_check.py           # 质检（四项粗筛 + 三查 + 无时间戳识别）
├── tests/
│   └── run_smoke.py          # 零依赖冒烟测试（10 个断言）
├── LICENSE                   # 本 Skill 自身：MIT
├── .gitignore
└── docs/
    └── 转写经验_通用版.md     # 完整原文归档；运行时以 references 为准
```

三级加载：`name` + `description` 常驻 → 触发后读 `SKILL.md` → 只按需要读某个 `references/` 文件。脚本可直接执行而不必读进上下文。

---

## 4. 使用方式

- **WorkBuddy 用户级 Skill**：把整个目录放到 `~/.workbuddy/skills/asr-transcription/`（本机已安装在该位置）。
- 触发方式：直接描述任务，例如「把这一批视频转成逐字稿」「转写结果开头不对，帮我排查」「要不要补一个中文识别模型」。
- 命令行（Windows 示例；bash 写法见 `references/10-commands.md`）：
  ```bash
  python scripts/check_env.py --models D:/hf-models/faster-whisper-large-v3-turbo
  python scripts/transcribe.py --wav audio/S1.wav --model D:/hf-models/faster-whisper-large-v3-turbo --out out/A1_S1.json
  python scripts/qc_check.py out/A1_S1.json
  python tests/run_smoke.py
  ```
- `qc_check.py` 的 RED 表示「需人工复核」，不等于已判错；文本与 `--json` 模式都在有 RED 时返回退出码 1，可直接进 CI。

---

## 5. 边界与免责

- 本仓库**不含**任何模型权重、测试音视频、逐字稿、访问令牌或本机绝对路径形式的凭据；只含方法、流程与可复用脚本。
- 速度与资源数字是本机实测的量级参考，随机器、素材、后台负载波动，不构成性能承诺。
- 没有人工标准答案时不报准确率；画面字幕 OCR 只能做旁证，不能当逐字真值。
- 本 Skill 自身的文档与脚本采用 **MIT**（见 `LICENSE`）；这不改变任何第三方项目的许可。
- 所有第三方项目的权利归各自维护方；本仓库不对它们主张任何权利。商用前请逐项核对代码许可与权重许可。
