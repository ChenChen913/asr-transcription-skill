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

---
