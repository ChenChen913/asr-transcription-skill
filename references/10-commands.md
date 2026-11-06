# 命令速查

> 按需加载参考；内容对应完整版《转写经验》的 附录。总览与路由见 [SKILL.md](../SKILL.md)。

---

## 附录 B：命令速查

```bash
# 环境自检（退出码 0 = 必需项齐全，1 = 缺依赖 / 缺 ffmpeg / 指定模型不完整，可当门禁）
python scripts/check_env.py --models "<模型目录>"
nvidia-smi
python -c "import faster_whisper, ctranslate2 as c; print(faster_whisper.__version__, c.get_cuda_device_count())"
ffmpeg -version && ffprobe -version

# 素材体检（必须有 audio stream）
ffprobe -v quiet -print_format json -show_format -show_streams "<素材>"

# 统一抽取 16 kHz 单声道 WAV（多方案共用同一份）
ffmpeg -y -i "<素材>" -vn -ac 1 -ar 16000 -c:a pcm_s16le "<输出>/<编号>.wav"

# 隔离安装（两个包一起装，装完先冒烟）
pip install --no-cache-dir --no-deps --target "<工作区>/pylibs" "<外壳包>" "<核心包>"
PYTHONPATH="<工作区>/pylibs" python -c "import <包>; print(<包>.__version__)"

# 补充方案（工作目录切到程序目录，用相对路径）
cd "<可执行文件目录>" && ./<exe> -m <模型.gguf> --vad <vad.gguf> -a "<相对路径>/<编号>.wav"
```

> 交付时建议同时产出：逐次结果表（CSV）、汇总报告（MD）、原始输出目录、以及一份「数据出处」清单，把每个数字钉到具体文件或命令。

### 先看有没有字幕轨（省掉一整轮转写）

```bash
python scripts/extract_subs.py --input "<素材>" --out-dir "out" --list   # 只列轨，不抽
python scripts/extract_subs.py --input "<素材>" --out-dir "out"          # 外挂 srt 优先，其次内嵌文本轨
python scripts/extract_subs.py --input "<素材>" --out-dir "out" --lang zh
python scripts/extract_subs.py --input "素材A" "素材B" --out-dir "out" --recursive
```

> 只处理**文本**字幕轨（subrip/ass/mov_text/webvtt 等）；PGS/VobSub 这类图形轨会明确拒绝并提示需要 OCR。退出码 `0` 抽到了、`2` 参数/输入有问题、`3` 一份都没抽出来（此时照常转写）。产物是同样的 `<key>.json` + `<key>.md`，外加一份 `<key>.srt` 原始字幕留档；JSON 里 `"engine": "subtitle-track"`，`qc_check.py` 会据此跳过字/段密度警告。

### 没有字幕轨：读画面里的硬字幕

```bash
python -m pip install -r requirements-ocr.txt               # 一次性，13 MB 模型随 wheel
python scripts/ocr_subs.py --input "<素材>" --list                        # 先探有没有会变的字幕带
python scripts/ocr_subs.py --input "<素材>" --out-dir "out" --fps 1.0     # 逐帧读
python scripts/ocr_subs.py --input "<素材>" --out-dir "out" --fps 0.5     # 长视频降采样，省一半时间
python scripts/ocr_subs.py --input "<素材>" --out-dir "out" --region 0.55,0.95   # 字幕更靠下时收窄
```

> 只读画面下半幅、只认会变化的字幕带（台标/水印/固定标题会被排除）；带内的双语译文按 `--language` 只留主语言，另起一行且换得慢的固定文案（要点条、章节点）按变化节奏剔除。约 0.38 s/帧，比转写慢，按需跑。退出码 `0` 读出了、`2` 参数或 OCR 运行时装不上、`3` 没有一个文件找到可读的字幕带（回落到转写）。产物 JSON 里 `"engine": "ocr"`，`qc_check.py` 与字幕轨用同一把尺子。

### 听看互校：只列分歧

```bash
python scripts/merge_sources.py --heard "out/01.json" --seen "out/01-ocr.json" --out "out/01.merge.md"
python scripts/merge_sources.py --heard "out/01.json" --seen "out/01.srt.json" --out "out/01.merge.md" --limit 20
```

> 两路按时间对齐（先测偏移，字幕常比语音晚 1–2 s），逐段算相似度，只把「不一致 / 接近 / 只有一边有」的段落列出来。**不产出合并稿、不裁决谁对、不改任何一边的时间戳**。数字不一致一律判「不一致」，哪怕整句相似度 0.95。

### 用 Skill 自带脚本跑一条

```bash
python scripts/transcribe.py --wav "<编号>.wav" --model "<模型目录>" --out "out/<配置>_<编号>.json" --tag "<配置>_<编号>"
python scripts/qc_check.py "out/<配置>_<编号>.json"          # 人读
python scripts/qc_check.py "out/<配置>_<编号>.json" --json   # 机读，可接 CI
python tests/run_smoke.py
```

> `qc_check.py` 退出码 1 = 有 ERROR 或 RED/REVIEW（**需人工复核，不等于判错**），0 = 无需复核。阈值严格大于：`gap > 8.00 s`、`overrun > 0.50 s`；可用 `--gap 10` / `--tail 15` 调整。分级含义见 `04-quality.md` §5.2.1。

### 整文件夹批量跑（串行）

```bash
python scripts/batch_transcribe.py --input "素材A" --out-dir "out" --model "<模型目录>"
python scripts/batch_transcribe.py --input "素材A" "素材B" --out-dir "out" --model "<模型目录>"
python scripts/batch_transcribe.py --input "素材A" --out-dir "out" --model "<模型目录>" --recursive
python scripts/batch_transcribe.py --input "素材A" --out-dir "out" --model "<模型目录>" --dry-run
python scripts/batch_transcribe.py --input "素材A" --out-dir "out" --model "<模型目录>" --force
python scripts/qc_check.py "out/*.json"        # 摘要不在 out/ 根下，不会被误报
```

> 默认收 WAV 与常见视频/音频容器（`.mp4 .mov .mkv .m4a .mp3 .flac` 等）。**非 WAV 会先用 ffmpeg 解成 16 kHz 单声道**，缓存在 `<out-dir>/_batch/audio/`，重跑复用、`--force` 才重抽；缓存不是"文件大于 44 字节就信"——先写 `.part-<pid>.wav`，用 `wave` 校验 16 kHz / 单声道 / 16 bit / 末尾一帧可读，通过才原子替换，失败与超时都清理临时文件，残缺缓存会触发重解码。纯 WAV 批次完全不调用 ffmpeg。匹配到非 WAV 而 PATH 里没有 ffmpeg 时，开跑**之前**就以退出码 2 停下，不会逐个文件失败。子目录要 `--recursive`。重跑同一条命令只重试失败项；`--timeout` 默认 1800 s/文件，也用于单次 ffmpeg 解码。
>
> 每个输入产出的是**一组成套产物**：`<key>.json` + `<key>.md`。跳过判据是这一组都完整（JSON 可解析且含 `segments`/`duration`，MD 存在且非空），所以中途被 Ctrl-C / 断电打断、只留下 JSON 的文件，下一轮会自动补跑，不会被当成「已完成」。

### Windows PowerShell 写法

> 上面的代码块是 bash 语法。Windows PowerShell 5.1 不支持 `VAR=value cmd` 和 `&&`；对应写法如下。

```powershell
$env:PYTHONPATH = "<工作区>\pylibs"
python -c "import <包>; print(<包>.__version__)"
Set-Location "<可执行文件目录>"
.\<exe> -m <模型.gguf> --vad <vad.gguf> -a "<相对路径>\<编号>.wav"
```

> PowerShell 7 支持 `&&`；5.1 请拆成两行。复杂逻辑一律落成 `.py` 脚本再跑，别在命令行里拼 JSON 或嵌套多层引号。

---
