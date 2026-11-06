# facts.md

> README 的唯一事实来源。A 部分由脚本扫描与实跑得到，B 部分来自作者既有文档（ROADMAP / docs / references）并经本人确认。
> 生成日期 2026-11-06。数字后面都附了产生它的命令。

## A. 可自动提取的事实

1. **项目类型**：Agent Skill 包（三级渐进加载：`SKILL.md` + `references/`），附 7 个可脱离 Skill 机制独立运行的 Python CLI 脚本。分类按 07-插件（宿主兼容范围）+ 10-AI 小项目（外部依赖、能力边界）。
2. **主语言 / 运行时**：Python 3；文档中文。运行时下限 Python 3.13.x。`python -V` → `Python 3.13.14`。
3. **安装命令**（源码目录实测路径 `~/.workbuddy/skills/asr-transcription`，WorkBuddy 用户级 Skill 目录）：
   `git clone https://github.com/ChenChen913/asr-transcription-skill.git "$env:USERPROFILE\.workbuddy\skills\asr-transcription"`
4. **依赖安装**：`python -m pip install -r requirements.txt`（听），OCR 另加 `-r requirements-ocr.txt`。
   `python -m pip show faster-whisper ctranslate2 av rapidocr-onnxruntime onnxruntime` → 1.2.1 / 4.8.1 / 18.0.0 / 1.2.3 / 1.24.2。
5. **运行命令**：`python scripts/transcribe.py --wav <wav> --model <ct2-dir> --out <json> [--tag]`。
6. **测试命令**：
   - `python tests/run_smoke.py` → `40 cases x (text + --json), 1104 assertions, 0 failures.`
   - `python tests/run_transcribe_unit.py` → `43 assertions, 0 failures.`
   - `python tests/run_subs_unit.py` → `79 assertions, 0 failures.`
   - `python tests/run_batch_unit.py` → `145 assertions, 0 failures.`
   - `python tests/run_ocr_unit.py` → `106 assertions, 0 failures.`
   - `python tests/run_merge_unit.py` → `74 assertions, 0 failures.`
   - 合计 **1551** 条断言；全部零依赖、不联网、不需要模型/GPU。
7. **构建命令**：无构建步骤。语法检查 `python -m compileall -q scripts tests`。
8. **目录结构**（`git ls-files | wc -l` → 26 个受版本控制文件）：
   - `SKILL.md`（126 行）— 主文件：触发条件 + 流程骨架 + 路由表 + 三条铁律
   - `references/`（10 个文件）— 按问题路由的细节，逐个 grep 定位
   - `scripts/`（7 个）— `transcribe.py` / `batch_transcribe.py` / `qc_check.py` / `check_env.py` / `extract_subs.py` / `ocr_subs.py` / `merge_sources.py`
   - `tests/`（6 个）— 上述六套断言
   - `docs/转写经验_通用版.md` — 未拆分的完整原文归档
   - `ROADMAP.md` — 版本记录、条件触发项、红线
   - `NOTICE.md` — 授权范围与第三方归属声明
   - `requirements.txt` / `requirements-ocr.txt` — 锁版本（听 / 看）
9. **主要依赖**（最多 10 个）：
   | 依赖 | 版本 | 用途 |
   |---|---|---|
   | Python | 3.13.x | 运行时 |
   | faster-whisper | 1.2.1 | 主转写引擎 |
   | CTranslate2 | 4.8.1 | 推理后端 |
   | PyAV (`av`) | 18.0.0（`av<19`） | 音频解码 |
   | FFmpeg / ffprobe | N-125829（外部，需在 PATH） | 抽音频 |
   | NVIDIA CUDA / cuBLAS / cuDNN | 12.6 / 12.x / 9.x（可选） | GPU 算子库 |
   | RapidOCR (`rapidocr-onnxruntime`) | 1.2.3（可选） | 画面 OCR 引擎 |
   | onnxruntime | 1.24.2（可选） | OCR / ONNX 推理后端 |
   | torch | 不安装 | 刻意排除的重依赖 |
10. **环境变量 / 配置项**：**无业务环境变量**。`grep -rn "os.getenv\|os.environ" scripts/*.py` 只命中 `PATH`（`transcribe.py:68,77` 用于找 CUDA DLL 目录）与 `batch_transcribe.py:189`（复制子进程环境）。所有参数走命令行，无配置文件、无 API key。
11. **CLI 参数**（`--help` 逐字核对，2026-11-06）：
    - `transcribe.py`：`--wav`* `--model`* `--out`* `--tag`(默认 `""`) `--device {cuda,cpu}`(cuda) `--compute`(cuda=float16 / cpu=int8) `--language`(zh) `--beam-size`(5) `--min-silence`(300) `--no-vad` `--no-prompt` `--hallucination-silence-threshold` `--cpu-threads`(8) `--no-fallback`
    - `batch_transcribe.py`：`--input`* `--out-dir`* `--model`（`--dry-run` 时可省）`--pattern`(默认 WAV + 常见视频/音频容器，非 WAV 自动 ffmpeg 解码) `--recursive` `--force` `--dry-run` `--timeout`(1800 s/文件) `--device` `--compute` `--language`
    - `qc_check.py`：`files...`* `--gap`(8.0) `--tail`(10.0) `--json`
    - `check_env.py`：`--models` `--json`
    - `extract_subs.py`：`--input`* `--out-dir`* `--stream` `--lang` `--no-sidecar` `--recursive` `--list` `--force` `--timeout` `--ffmpeg` `--ffprobe`
    - `ocr_subs.py`：`--input`* `--out-dir` `--fps`(1.0) `--region`(0.50,0.995) `--min-score`(0.5) `--language`(zh) `--no-band` `--calib-frames` `--max-frames` `--recursive` `--list` `--force` `--timeout` `--ffmpeg` `--ffprobe`
    - `merge_sources.py`：`--heard`* `--seen`* `--out`* `--agree`(0.85) `--near`(0.55) `--limit`(40) `--title`
12. **退出码**（逐条读 `sys.exit`）：
    - `transcribe.py`：0 成功｜2 输入/模型目录不可用｜3 当前设备失败且未回退｜4 GPU 与 CPU 都失败
    - `qc_check.py`：0 无 ERROR 且无 RED｜1 有 ERROR 或 RED｜2 用法错误
    - `batch_transcribe.py`：0 全部成功｜1 有失败或超时｜2 用法、匹配错误，或匹配到非 WAV 但 PATH 里没有 ffmpeg
    - `check_env.py`：0 必需项齐全｜1 缺依赖 / 缺 ffmpeg / 指定模型不完整（可作 CI 门禁）
    - `extract_subs.py`：0 至少抽出一份｜3 一份都没抽出来
13. **模型目录**：必须是含 5 个文件的 CTranslate2 目录。`ls /d/hf-models/faster-whisper-large-v3-turbo | wc -l` → 5：`config.json` / `model.bin` / `preprocessor_config.json` / `tokenizer.json` / `vocabulary.json`；`model.bin` 1,617,884,929 B（约 1.51 GiB）。
14. **已有文档**：`SKILL.md`、`references/01–10`、`ROADMAP.md`、`NOTICE.md`、`docs/转写经验_通用版.md`、`README.md`（本次重写）。
15. **仓库**：`https://github.com/ChenChen913/asr-transcription-skill`，分支 `master`，公开，26 个文件。
16. **许可证**：MIT，`Copyright (c) 2026 ChenChen913`（`LICENSE` 第 3 行）。
17. **性能数字的出处**：4 段中文短口播（1.31 / 1.50 / 2.12 / 3.90 分钟）、9 种配置共 28 次运行、2026-11-05 单机、波动约 ±30%（`references/01-selection.md` §1.7）。
    关键值：turbo/GPU 约 19×、turbo/CPU 约 2.6–2.9×、large-v3/GPU 约 6×、SenseVoice q8 GGUF/CPU 约 27–28×。
18. **演示素材**：仓库内**没有**截图或 GIF（`find . -iname "*.png" -o -iname "*.gif"` → 0）。README 改用 Mermaid 流程图 + 实际终端输出替代。

## B. 需人工补充（已由作者既有文档提取并经本人确认）

- **一句话描述（中文）**：给 Agent 加载的中文音视频转写技能包：听写、字幕与画面 OCR、两路互校、质检脚本与第三方归属，附 1551 条离线断言。
- **一句话描述（英文）**：An agent-loadable Chinese ASR skill: hearing, subtitles, on-screen OCR, cross-checking, QC scripts and attribution, with 1551 offline assertions.
- **为什么做这个项目**：来源 `ROADMAP.md` v0.1 条目 + `SKILL.md`「目的」+ `README.md` 旧版第 1 节。核心：多轮实测沉淀，失败多来自流程而非模型。
- **目标用户**：要把中文口播视频转成可复核稿件的 Windows 用户（有 Python 基础、能读命令行）；以及接手的 Agent。
- **与同类方案的差异**：不做模型封装或云服务；把「质检分级 + 人工三查 + 归属登记」写进流程；没有可信标准答案就不报准确率。
- **已知限制**：`README.md` 旧版 §3 已验证范围矩阵 + `ROADMAP.md` 阶段 C（C1 无标准答案、C3 词级/说话人/SRT 未做、C4 单文件失败无产物）。
- **演示素材路径**：无截图/GIF；使用 Mermaid + 实跑终端输出。
- **贡献口径（本人确认 2026-11-06）**：接受 Issue；PR 欢迎但无固定流程。
- **定位口径（本人确认 2026-11-06）**：技能包 + 可独立运行的脚本。

## 核对记录

- [x] 所有命令实际执行过（测试六套、`--help` 七份、`pip show`、`ls` 模型目录）
- [x] 所有环境变量在代码中能搜到（仅 `PATH` / `PYTHONUTF8` / `PYTHONIOENCODING`，无业务变量）
- [x] 目录结构与实际一致（26 个受版本控制文件）
- [x] 无臆造项；查不到的写「未验证」，不补全

## 已知偏差（写作时保留，交付时说明）

1. **一句话描述**：本仓库没有 `package.json`。等价字段是 `SKILL.md` frontmatter 的 `description`，但那是 400+ 字符的触发语句，不适合做 < 120 字符的一句话。README 采用上面这句，语义与 `description` 一致，未逐字照抄。
2. **章节顺序**：在标准模板的「已知限制」与「如何贡献」之间插入「第三方项目与许可证」。插入不改变原有章节的相对顺序；原因是作者要求与 `NOTICE.md` 的授权声明必须有可见入口。
