# 环境与资源

> 按需加载参考；内容对应完整版《转写经验》的 7.。总览与路由见 [SKILL.md](../SKILL.md)。

---

## 7. 环境与资源

### 7.1 依赖清单（参考版本 + 复核方式）

> 版本号只作量级参考，**用下面命令自己查，别照抄**。环境快照会漂移，写明核实日期。

| 组件 | 参考 | 复核命令 |
|---|---|---|
| Python | 3.13.x | `python -V`（PATH 里可能有多个 Python，显式写绝对路径最稳） |
| faster-whisper | 1.2.1 | `python -c "import faster_whisper; print(faster_whisper.__version__)"` |
| ctranslate2 | 4.8.x | `python -c "import ctranslate2 as c; print(c.__version__, c.get_cuda_device_count())"` |
| onnxruntime / numpy | 随依赖带入 | `pip show onnxruntime numpy` |
| cublas / cudnn DLL | 用户级 site-packages 下 | `python -c "import site; print(site.getusersitepackages())"` 后查 `nvidia\*\bin\*.dll` |
| ffmpeg / ffprobe | 已在 PATH | `ffmpeg -version` / `ffprobe -version` |
| torch | **不该装** | `python -c "import importlib.util as u; print(u.find_spec('torch'))"` → `None` |

模型体积参考（客观值，可用于估磁盘）：turbo 约 **1.51 GiB**、large-v3 约 **2.88 GiB**、合计 **4.39 GiB**（目录 5 个文件口径；单看 `model.bin` 略小）。全文体积统一用 GiB（1024³），DLL 用 MiB。

### 7.1.1 实测环境快照（示例，不是要求）

下面是 **2026-11-05** 那台机器的快照，用来说明 `01-selection.md` §1.7 与 README 里的速度、内存、显存数字是怎么来的。**换成别的机器请按 7.1 逐条复核，不要照抄任何数字**；这里不含用户名、绝对路径或凭据。

| 项 | 快照值 |
|---|---|
| 操作系统 | Windows 11 家庭版 中文版，build 26300 |
| CPU | AMD Ryzen 7 7840H with Radeon 780M Graphics，8 核 / 16 线程 |
| 运行内存 | 总计 15.19 GB；当日实测可用 6.00 GB（历史批次 4.16–4.50 GB，是主要瓶颈） |
| 显卡 / 显存 | NVIDIA GeForce RTX 4060 Laptop GPU，8188 MiB；当日空闲 6808 MiB |
| 驱动 / CUDA | 560.94 / CUDA 12.6，识别到 1 块 CUDA 设备 |
| Python | 3.13.x（快照当日 3.13.3；2026-11-06 复核为 3.13.14） |
| 关键依赖 | faster-whisper 1.2.1、CTranslate2 4.8.1、onnxruntime 1.24.2、av 18.0.0、numpy 2.3.3 |
| FFmpeg | N-125829（2026-07-28 构建） |
| 已装模型 | turbo 约 1.51 GiB、large-v3 约 2.88 GiB，合计 4.39 GiB |
| torch | 未安装（本链路刻意避开） |

由这套配置得出的关键约束：可用运行内存常低于 6 GB，比显存更早成为瓶颈。因此排除需要 torch / vLLM 的重型方案，所有引擎串行执行、一次只加载一个模型，并对峰值内存设熔断线（见 7.2）。

GPU 相关：`nvidia-smi -q` 里 `Default Power Limit` 是默认值，**别把它当当前限制**；`--query-gpu=power.limit` 可能返回 `N/A`。倍速波动受后台负载、首次加载、VAD 命中、音频内容影响，没有同步记录 `clocks.sm` / `power.draw` 就别做单因素归因。

### 7.2 资源护栏

- **串行**：一次只加载一个模型，跑完立即释放；不要同时跑两套。GPU 方案和纯 CPU 方案并发时，内存 / 显存读数会互相污染，资源结论就不可信。
- **测峰值要连续采样**：每 0.5–1 秒采一次进程树内存 + `nvidia-smi` 显存，取「全程最大值 − 基线」。**前后各采一次只能看到跑完后的残留**，测不到峰值。
- **熔断线**（按机器实际调整）：可用运行内存 < 2 GB 或可用显存 < 3 GB 直接停；单方案运行内存增量 > 1.5 GB 要重新评估。
- **磁盘预算**：下载包 + 解压后 + 运行时是**双份甚至三份**空间，别只按压缩包大小估。测完及时清掉不再需要的下载物。
- **缓存目录**：把 `TEMP` / `TMP` / `PIP_CACHE_DIR` / 模型缓存都指到工作区，避免下载途中吃掉系统盘。

### 7.3 安装与下载

- **优先国内镜像**：默认源经常几分钟无进展，换镜像后可能几秒完成。
- **隔离安装**：`pip install --no-cache-dir --no-deps --target <工作区>/pylibs <包A> <包B>` + `PYTHONPATH=<工作区>/pylibs`，不污染全局环境、删目录即回滚。
- **`--no-deps` 要手动补齐依赖**：有些 Python 包只是外壳，真正干活的在另一个核心包里。装完**先跑一次 import 冒烟测试**，不过就整组跳过，别浪费后面时间。
- **记录 URL / 版本 / 哈希**，否则下次重跑结果对不上。
- **官方源可能停滞**：大文件用镜像 + 多线程分片（Range 请求、按已有偏移续传、先写 `.partN` 再合并）。
- **不要用 HF 默认缓存目录**：Windows 未开开发者模式时建不了软链接，缓存会存两份。更省事的做法是**下载到普通目录，直接把目录路径传给模型构造函数**。
- **许可证分两列**：代码许可证 ≠ 权重许可证。「代码 MIT / 权重自定义、商用需署名」这类组合很常见，别一律当 Apache-2.0。

---

---
