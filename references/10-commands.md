# 命令速查

> 按需加载参考；内容对应完整版《转写经验》的 附录。总览与路由见 [SKILL.md](../SKILL.md)。

---

## 附录 B：命令速查

```bash
# 环境自检
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

> 默认只收 `*.wav`（流程上仍建议先用 FFmpeg 统一成 16 kHz 单声道）。子目录要 `--recursive`；确需别的扩展名用 `--pattern "*.wav" "*.m4a"`，但先拿一条试通再上批量。重跑同一条命令只重试失败项；`--timeout` 默认 1800 s/文件。

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
