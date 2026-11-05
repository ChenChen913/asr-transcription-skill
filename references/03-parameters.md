# 转写参数与补充方案

> 按需加载参考；内容对应完整版《转写经验》的 3.、4.。总览与路由见 [SKILL.md](../SKILL.md)。

---

## 3. 主力转写参数（可直接抄）

| 参数 | 值 | 为什么 |
|---|---|---|
| `language` | `zh` | 避免自动识别摇摆 |
| `beam_size` | `5` | 质量与速度的平衡点 |
| `vad_filter` | `True` | 跳过长静音省时间；**但挡不住开头幻觉** |
| `min_silence_duration_ms` | `300–400` | 太小切碎句子，太大漏话 |
| `condition_on_previous_text` | `False` | 防止前一段的错误在后续段落循环放大 |
| `initial_prompt` | `以下是普通话的句子，请使用简体中文，并加上标点符号。` | **必须保留**，关掉后标点密度掉到接近 0（0–0.1/百字） |
| `temperature` | `0.0 / 0.2 / 0.4` | 是**段内**按重复率 / 置信度阈值逐级降级，不是整次转写失败后重跑 |
| `word_timestamps` | `False`（默认） | 只给段级起止时间；设 `True` 才输出词级，速度更慢、占用更高，非必要不开 |
| `device` / `compute_type` | `cuda` / `float16` | 有卡就一定用 GPU |
| 回退 | `cpu` / `int8` / `cpu_threads=8` | 写进 `try/except Exception`，见下 |

### 3.1 关于 `initial_prompt`

- 它确实在提供标点：关掉后输出标点密度从约 2–2.4/百字掉到**接近 0（0–0.1/百字）**，整篇几乎无标点。
- 但它**不是热词**，也不保证简体和断句。**「不加就标点稀疏」已有对照**（关掉后从约 2–2.4/百字掉到 0.10）；但「不加会出繁体」未做系统统计，别当结论。
- **副作用要盯**：提示语原文可能被整段复读进正文；也见过把片头话术（「请不吝点赞、订阅、转发…」）当成正文。把这些句子列进 §5 的检查关键词。
- `hallucination_silence_threshold`（1.2.1 起提供，默认 `None`）只缓解**静音段**幻觉、需配合 VAD，对真实语音段未必触发。留着可以，**别指望它替代人工检查**，也不要因为一次没看出效果就断言参数无用。

### 3.2 GPU 的隐形依赖（最大的坑）

现象：指定 `cuda` 后**模型能加载成功**，一推理就报 `Library cublas64_12.dll is not found or cannot be loaded`。

原因：CTranslate2 走 CUDA 12，运行时按名字动态加载 cublas / cudnn 的 DLL，它们不在系统 PATH 里。少任何一个都是「加载成功、推理失败」。

做法（顺序是关键）：

1. 确认两个 pip 组件已装（`nvidia-cublas-cu12`、`nvidia-cudnn-cu12`），DLL 通常在**用户级** site-packages 的 `nvidia\cublas\bin`、`nvidia\cudnn\bin`。
2. **在创建 / 推理 WhisperModel 之前**，对每个 bin 目录执行 `os.add_dll_directory(目录)`，并把目录前插进 `os.environ['PATH']`。
3. 真实要求是「**加载模型之前**」，不是字面意义的「任何 import 之前」——放在 `import faster_whisper` 之后也照样能跑通。
4. 显卡驱动只提供 CUDA 运行时，**算子库来自这两个 pip 包**，别只装驱动。

### 3.3 一定要写回退

```python
try:
    segments, info = model.transcribe(...)
    segs = list(segments)          # 强制消费生成器，让异常在这里抛出
except Exception as e:             # 不要写裸 except:
    model = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=8)
    segments, info = model.transcribe(...)
    segs = list(segments)
```

> 回退后把**实际使用的设备**写进输出，别让 JSON 里还记着 `cuda`。

**用 `scripts/transcribe.py` 时**：默认 `--device cuda`，在**构造模型或推理**任一阶段抛异常就自动改 `cpu / int8` 重跑，CPU 再失败才真正报错。

| 想知道什么 | 看输出 JSON 的哪里 |
|---|---|
| 实际跑在哪个设备 | `device` 字段（`cuda` / `cpu`）——**不要只看命令行 `--device` 入参** |
| 有没有发生过回退 | `fallback` 字段，非空即回退过，内容是原始异常摘要 |
| 速度差多少 | `rtf_x`：GPU 约 19×，CPU 约 2.6–2.9× |

显式只要 CPU：`--device cpu --compute int8`。DLL 文件在磁盘上 ≠ 能加载，环境自检通过后仍要真跑一句推理确认。

### 3.4 版本锁定

- 若锁在 faster-whisper **1.2.1 发布版**：`pin av<19`（`av` 即 PyAV-Org 的 PyAV）、暂不升 Python 3.14。上游主分支已修 PyAV 19 / 3.14 的适配，但尚未随该发布版提供——这是「发布版滞后」，不是「项目停更」。
- 想确认上游状态，查 GitHub API / PyPI 的 `pushed_at`、最新 release、issue 状态，**不要引用别人几个月前写的维护快照**。
- **「上游主分支已修」不等于「你装的发布版已修」**：版本锁定以本机 `pip show` 为准，别拿仓库状态代替安装状态。

---

---

## 4. 补充方案怎么用（SenseVoice q8 GGUF 独立运行时）

**拿到的东西**：一个 Windows 可执行文件 + 一个 q8 量化的模型文件 + 一个静音切分模型；一次性调用，无需常驻服务。

**典型调用**（注意路径写法）：

```
cd <可执行文件所在目录>
llama-funasr-sensevoice.exe -m <模型.gguf> --vad <vad.gguf> -a <相对路径/音频.wav>
```

四个必须注意的点：

1. **中文 / 非 ASCII 路径**：这类原生二进制常把路径读成乱码，表现为「手工试跑成功、批量全失败」。做法是**把工作目录切到程序所在目录，一律传相对路径**。
2. **日志和正文可能打在同一行**（形如 `正文内容[sensevoice] building graph...`）。解析时按日志标记切分、只取前面的正文，否则字数会虚高一倍。别只看「整行是否以日志开头」。
3. **没有可用时间信息**：运行时只给一个覆盖全长的整段，看着像有起止时间其实是占位。只能当纯文本参照，不能做时间轴对齐、不能出带时间码的字幕，也不能据此判断尾段越界。
4. **许可证分开看**：代码 MIT，权重是 FunASR Model License；第三方转换的 ONNX / GGUF 包可能标不同条款，逐个核对。

**定位与验收**：批量快速预览、异源第二意见。重要稿子以主力为准；涉及英文缩写、产品名、人名的段落直接采信主力输出。

---

---
