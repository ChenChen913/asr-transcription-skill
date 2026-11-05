# 第三方项目归属与许可证

> 按需加载参考；内容对应完整版《转写经验》的 附录。总览与路由见 [SKILL.md](../SKILL.md)。

---

## 附录 A：第三方项目与模型归属

> 本文所有方案都来自第三方开源项目 / 模型，这里逐一登记**项目名、维护方或作者、官方地址、许可证、本文用到哪部分**。核实日期 **2026-11-05**，依据是各项目 GitHub REST API 与官方模型卡。
>
> 署名口径：以官方仓库的 owner（组织或个人账号）为准；仅当仓库属于个人时列个人账号（如 WhisperX 的 `m-bain`）。括号内的人名是社区通行称谓，本次**未逐一核对论文首页**，一律以仓库 owner 账号为准。组织归属会迁移——**SenseVoice 仓库已从 `FunAudioLLM` 迁到 `QwenAudio`**，用前请复核。
>
> ⚠️ **代码许可 ≠ 权重许可**：SenseVoice、Paraformer 这类模型的代码仓库是 MIT，但权重受独立的模型许可约束；第三方转换的 ONNX / GGUF 包还可能标别的条款，逐个核对，别一律当 Apache-2.0 或 MIT。

### A.1 主力链路（实际采用）

| 项目 / 模型 | 维护方 / 作者 | 官方地址 | 许可证 | 本文用到 |
|---|---|---|---|---|
| OpenAI Whisper（含 `large-v3`、`large-v3-turbo` 模型权重） | OpenAI | `https://github.com/openai/whisper` | MIT | 两个模型本身；turbo 是 large-v3 的解码器剪枝版 |
| faster-whisper | SYSTRAN（组织） | `https://github.com/SYSTRAN/faster-whisper` | MIT | 主引擎，`WhisperModel.transcribe()` 及全部转写参数 |
| CTranslate2 | OpenNMT（组织） | `https://github.com/OpenNMT/CTranslate2` | MIT | faster-whisper 的推理后端；CUDA 下动态加载 cublas / cudnn |
| PyAV（`av` 包） | PyAV-Org | `https://github.com/PyAV-Org/PyAV` | BSD-3-Clause | faster-whisper 1.2.1 的音频解码依赖；§3.4 要求 pin `av<19` |
| NVIDIA CUDA / cuBLAS / cuDNN（`nvidia-cublas-cu12`、`nvidia-cudnn-cu12`） | NVIDIA | `https://developer.nvidia.com/` | NVIDIA 专有许可 | GPU 推理所需的算子库 DLL |

### A.2 补充链路（实际采用 / 参测）

| 项目 / 模型 | 维护方 / 作者 | 官方地址 | 许可证 | 本文用到 |
|---|---|---|---|---|
| SenseVoice / `SenseVoiceSmall` | QwenAudio（组织；原 FunAudioLLM，仓库已迁移） | `https://github.com/QwenAudio/SenseVoice`；权重 `https://huggingface.co/FunAudioLLM/SenseVoiceSmall` | **代码 MIT / 权重 `license: other`（FunASR Model License，商用需核对署名等义务）** | §1.2 补充方案；也是 int8 ONNX 对照的模型来源 |
| FunASR | modelscope（阿里 ModelScope / 达摩院语音团队） | `https://github.com/modelscope/FunASR` | MIT | §4 的 GGUF 独立运行时；也提供 VAD、标点、热词等流水线 |
| FunASR llama.cpp runtime（`runtime-llamacpp-v0.2.6`） | modelscope/FunASR 发布 | `https://github.com/modelscope/FunASR/releases/tag/runtime-llamacpp-v0.2.6` | 随 FunASR（MIT） | `llama-funasr-sensevoice.exe` 等 Windows 二进制 |
| llama.cpp / GGUF 格式 | ggml-org（组织，Georgi Gerganov 发起） | `https://github.com/ggml-org/llama.cpp` | MIT | q8 GGUF 量化模型与运行时基础 |
| sherpa-onnx | k2-fsa（组织，next-gen Kaldi 社区） | `https://github.com/k2-fsa/sherpa-onnx` | Apache-2.0 | §1.5 / §1.7 的 SenseVoice int8 ONNX、Paraformer int8 ONNX 对照方案；模型来自其 `asr-models` release |
| Paraformer | modelscope/FunASR（阿里达摩院）提出的非自回归 ASR 模型 | 模型说明见 `https://github.com/modelscope/FunASR`；ONNX 导出由 k2-fsa/sherpa-onnx 提供 | 代码 MIT / 权重按模型卡 | §1.5 的「无标点」对照方案 |

### A.3 仅在选型中提及、本文未采用

| 项目 / 模型 | 维护方 / 作者 | 官方地址 | 许可证 |
|---|---|---|---|
| Qwen3-ASR | QwenLM（阿里云通义千问团队） | `https://github.com/QwenLM/Qwen3-ASR` | Apache-2.0 |
| WhisperX | Max Bain（个人账号 `m-bain`） | `https://github.com/m-bain/whisperX` | BSD-2-Clause |
| whisper.cpp | ggml-org（原 `ggerganov`） | `https://github.com/ggml-org/whisper.cpp` | MIT |
| PyTorch / Transformers / vLLM | 各自开源组织 | 官方文档 | 本文明确**不安装**，仅作为「重依赖方案」的对比项 |

### A.4 通用工具与平台

| 项目 | 维护方 / 作者 | 官方地址 | 许可证 | 本文用到 |
|---|---|---|---|---|
| FFmpeg / ffprobe | FFmpeg 项目 | `https://ffmpeg.org/`（`https://github.com/FFmpeg/FFmpeg` 为镜像） | LGPL / GPL，**依构建选项而定**（GitHub 标 `NOASSERTION`） | §2 抽音频、§2.1 素材体检 |
| ONNX Runtime | Microsoft | `https://github.com/microsoft/onnxruntime` | MIT | sherpa-onnx 与 OCR 的推理后端 |
| NumPy | NumPy 社区 | `https://numpy.org/` | BSD-3-Clause | 随依赖带入 |
| RapidOCR | RapidAI | `https://github.com/RapidAI/RapidOCR` | Apache-2.0 | §5.2 / §5.5 的可选 OCR 旁路 |
| Hugging Face Hub / `hf-mirror.com` | Hugging Face / 第三方镜像站 | `https://huggingface.co/` | 平台服务，非本文依赖 | §7.3 下载模型时的镜像与缓存说明 |

> **免责与核对**：以上归属与许可证按 2026-11-05 的官方仓库信息登记，可能随组织调整、许可证变更而失效；商用前请以当次官方仓库和模型卡为准。本文不对上述项目主张任何权利，所有经验性结论（速度、资源、质检表现）是本文作者的实测记录，与项目官方无关。
>
> 核实局限：代码仓库信息均来自本次 GitHub REST API 一手查询；**SenseVoiceSmall 权重许可沿用同日早前对 HF 模型卡的核实结果（`license: other`），本次 HF API 抓取失败**，商用前必须打开模型卡再确认一次。Paraformer 权重许可、FFmpeg 具体构建所用条款同理按当次文件为准。

---

---
