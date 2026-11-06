<h1 align="center">asr-transcription</h1>

<p align="center">
  <a href="./README.md">简体中文</a> | <b>English</b>
  <br><br>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.13.x-3776ab.svg" alt="Python 3.13.x"></a>
  <img src="https://img.shields.io/badge/platform-Windows%2011%20%C2%B7%20verified-lightgrey.svg" alt="Platform: Windows 11, verified">
  <img src="https://img.shields.io/badge/tests-1028%20offline%20assertions-brightgreen.svg" alt="1028 offline assertions">
</p>

An agent-loadable Chinese ASR skill: engine choice, parameters, QC scripts, attribution, 1028 offline assertions.

> [!NOTE]
> This repository is AI-assisted. Every script and number has been verified by hand, and the process is recorded in [`ROADMAP.md`](ROADMAP.md). Platforms that were not tested are never described as supported — see [Known limitations](#known-limitations).

```mermaid
flowchart LR
    A["mp4 / mkv source"] -->|FFmpeg| B["16 kHz mono WAV"]
    B --> C["transcribe.py<br/>faster-whisper turbo · GPU"]
    C -->|"error is genuinely CUDA"| C2["fall back to CPU int8"]
    C --> D["out/01.json + out/01.md"]
    C2 --> D
    D --> E["qc_check.py<br/>ERROR / REVIEW"]
    E --> F["manual checks<br/>first 30 s · last 30 s · long gaps"]
    F --> G["edit and keep a record"]
    B -.->|optional| H["SenseVoice q8 GGUF<br/>independent second opinion"]
```

With the dependencies installed, one audio file takes three commands:

```powershell
python -m pip install "faster-whisper==1.2.1" "ctranslate2==4.8.1" "av<19"
python scripts/transcribe.py --wav "01.wav" --model "<model-dir>" --out "out/01.json" --tag "01"
python scripts/qc_check.py "out/01.json"
```

Full prerequisites and the environment check are in [Quick start](#quick-start).

## Table of contents

- [Why this project](#why-this-project)
- [Quick start](#quick-start)
- [Usage](#usage)
- [Configuration](#configuration)
- [Project structure](#project-structure)
- [Development](#development)
- [FAQ](#faq)
- [Known limitations](#known-limitations)
- [Third-party projects and licenses](#third-party-projects-and-licenses)
- [Contributing](#contributing)
- [License](#license)

## Why this project

It started with real transcription work, not with a framework looking for a use case. Turning a batch of Chinese talking-head videos into verbatim transcripts, and trying several stacks along the way, made one thing clear: failures rarely come from the model itself.

- The opening prompt gets read aloud into the transcript
- The ending invents sign-off lines such as "thanks for watching"
- A whole passage in the middle disappears, invisible in the output
- cuBLAS / cuDNN DLLs cannot be found even though the files are on disk
- `--json` redirected to a file becomes GBK bytes, no longer valid JSON

Switching models does not fix any of these. They are process problems: how versions are pinned, how parameters are set, how output is verified, and whether numbers can be generalised. That is why this repository is about process, not models.

The scripts automate four things: environment check, single-file transcription, batch transcription, and transcript QC. Judgement stays with the human and the agent: listen to the first and last 30 seconds, cross-check long gaps, and never claim an accuracy figure without a reference transcript.

Three rules are written into `SKILL.md`, and repeated here:

1. **Always run the three checks after transcribing**: the first 30 seconds, the last 30 seconds, and any gap longer than 8 seconds. Passing all four numeric metrics does not exempt you.
2. **Never report accuracy without a trustworthy reference**: character error rate needs a human transcript or official subtitles. On-screen subtitle OCR is supporting evidence only. A difference score is not accuracy.
3. **Two versions of the same family are not two pairs of eyes**: turbo is a decoder-pruned large-v3 and shares the encoder, so their errors correlate. An independent second opinion requires a different architecture.

What it is not: it does not train models, publish weights, run a cloud service, or contain any audio. The repository holds process, scripts, and attribution.

## Quick start

### Prerequisites

| Item | Requirement |
|---|---|
| OS | Windows 11 (the only tested platform; other platforms in [Known limitations](#verified-platforms)) |
| Python | 3.13.x (3.13.14 measured on 2026-11-06) |
| FFmpeg | `ffmpeg` and `ffprobe` on `PATH` |
| GPU (optional) | NVIDIA, CUDA 12.x / cuDNN 9, at least 8 GB VRAM; without one, add `--device cpu` |
| Model | A CTranslate2 directory with 5 files: `config.json` / `model.bin` / `preprocessor_config.json` / `tokenizer.json` / `vocabulary.json` |
| Disk | turbo about 1.51 GiB, large-v3 about 2.88 GiB |

Model weights are **not** in this repository; download them yourself (Hugging Face or a mirror). Without a model, `check_env.py` and all three test suites still run.

### Install

```powershell
git clone https://github.com/ChenChen913/asr-transcription-skill.git "$env:USERPROFILE\.workbuddy\skills\asr-transcription"
cd "$env:USERPROFILE\.workbuddy\skills\asr-transcription"
python -m pip install "faster-whisper==1.2.1" "ctranslate2==4.8.1" "av<19"
```

Then check the environment. `check_env.py` is read-only and writes nothing; `run_smoke.py` needs no model, GPU, or network:

```powershell
python scripts/check_env.py --models "<model-dir>"
python tests/run_smoke.py
```

### Run your first transcript

```powershell
# 1. Normalise to 16 kHz mono PCM WAV
ffmpeg -y -i "<source>.mp4" -vn -ac 1 -ar 16000 -c:a pcm_s16le "01.wav"

# 2. Transcribe; falls back to CPU only when the error is genuinely CUDA
python scripts/transcribe.py --wav "01.wav" --model "<model-dir>" --out "out/01.json" --tag "01"

# 3. QC
python scripts/qc_check.py "out/01.json"
```

Look at three things afterwards: `out/01.json` (structured), `out/01.md` (timestamped readable version), and the `device` field in the JSON (`cuda` or `cpu`). **Do not trust the `--device` flag alone** — the device that actually ran is recorded in the output.

### Without the Skill mechanism

The four scripts are plain Python 3. Put the repository anywhere and call `python scripts/transcribe.py ...`; no agent runtime required.

To load it as a Skill in another agent tool, follow that tool's documented directory and metadata conventions. **Only the WorkBuddy user-level Skill path has been tested here; this repository does not claim compatibility with other tools.**

## Usage

### How much is automated

| Step | Owner | Where |
|---|---|---|
| Environment check | Automated | `scripts/check_env.py` |
| Single-file transcription | Automated | `scripts/transcribe.py` |
| Batch transcription | Automated | `scripts/batch_transcribe.py` |
| Transcript QC | Automated | `scripts/qc_check.py` |
| Regression tests | Automated | the three suites under `tests/` |
| Audio extraction, cross-engine comparison, diff alignment, editing, record keeping | Agent workflow | The Skill tells the agent which command to run and how to read the result; no script |
| Listening to first/last 30 s, glossary, reference transcript, licence review | Manual | Requires human judgement |

> "The workflow is documented" does not mean "the tool implements it". SenseVoice q8 GGUF has commands but no wrapper script; it is an optional independent comparison.

### Single file

```powershell
python scripts/transcribe.py `
    --wav "01.wav" --model "<model-dir>" --out "out/01.json" --tag "01" `
    --device cuda --compute float16
```

The fallback has a precondition: it only switches to CPU when the error matches `cuda / cublas / cudnn / cudart / nvml / gpu / out of memory / no kernel image`. A corrupt model, an undecodable file, or an invalid argument does not trigger it — CPU would fail the same way and the real error would be buried. Disable fallback with `--no-fallback`.

| Exit code | Meaning |
|---|---|
| `0` | Success |
| `2` | Input or model directory unusable |
| `3` | The current device failed and no fallback was attempted |
| `4` | Both GPU and CPU failed |

### Batch

One folder, or several:

```powershell
# Plan first: no model loaded, nothing written
python scripts/batch_transcribe.py --input "<dir-a>" "<dir-b>" --out-dir "out" --dry-run

# Then run
python scripts/batch_transcribe.py --input "<dir-a>" "<dir-b>" --out-dir "out" --model "<model-dir>"
```

It does exactly four things:

1. **Serial**: one model instance at a time. GPU and CPU runs never overlap, so resource readings stay clean.
2. **Skips finished files**: the unit is the pair of artefacts (JSON + MD), not a single file.
3. **Isolates failures**: a non-zero exit or a timeout on one file is recorded and the batch continues.
4. **One output pair per input**: same schema as a single run, ready for `qc_check.py`.

Layout of `--out-dir`:

```text
out/
├── <key>.json          one structured transcript per input
├── <key>.md            the readable version
├── <key>.json.prev     only while a re-run is in flight, or if it failed
├── <key>.md.prev
└── _batch/summary.json counts + per-file outcome, rewritten after every file
```

Two things that bite:

- **`json` and `md` are one deliverable.** The skip rule requires both to be complete, and `--force` moves both aside as `.prev`. The writer renames the MD before the JSON, so "JSON exists ⇒ MD exists"; a task killed halfway is retried next run instead of being skipped forever.
- **PCM WAV only.** `--pattern` selects file names, not decoders. When a non-`.wav` file matches, the batch prints a `[WARN]` plus an FFmpeg conversion command before starting, and does not block — the real decode error comes from `transcribe.py`.

Exit codes: `0` all succeeded | `1` failures or timeouts | `2` usage or match error.

### QC

```powershell
python scripts/qc_check.py "out/*.json"
python scripts/qc_check.py "out/*.json" --json      # strict JSON, CI-friendly
```

A real run (synthetic sample, shown for the grading):

```text
--- demo.json [REVIEW]
    (62.40s, 34 chars, 3 segs)
    punct/100=11.76  chars/min=32.7  segs/min=2.9  maxgap=10.50s  tail=38.40s  overrun=-38.40s
    [RED/REVIEW] LONG GAP 10.50s at 9.6-20.1s
    [warn] tail gap 38.40s > 10.0s (possible truncation)
    [warn] char density 32.7/min outside (200.0, 450.0)
```

| Marker | Meaning | Action |
|---|---|---|
| `[ERROR]` / `"errors"` | A deterministic input problem: missing field, non-numeric, `NaN` / `Infinity`, negative value, out-of-order segments, invalid `duration` | Fix the input or the producing script. **This is not "the transcript is wrong"** |
| ↳ invalid timeline | Negative start / `end < start` / out of order: parseable numbers, but no valid timeline | The three time metrics become `null`, `has_timestamps=false`, `note` says `INVALID TIMELINE`; text metrics still work |
| `[RED/REVIEW]` / `"red"` | Risk signal: overlap, empty text, overrun, long gap, prompt echo, no timestamps | Inspect that interval by hand. **Not proof of an error** |
| `[warn]` | A coarse metric is out of range: character density, segment count, tail gap | Reference only; does not affect the exit code |

Thresholds are strict: `gap > 8.00 s` (8.00 passes, 8.01 fails) and `overrun > 0.50 s`. Adjust with `--gap` / `--tail`. In both text and `--json` mode, the exit code is 1 when there is any ERROR or RED.

### Output format

The JSON from `transcribe.py` has segment-level timestamps only, no word-level `words`:

| Field | Meaning |
|---|---|
| `tag` / `engine` / `model` | Task label, engine, model directory name |
| `device` / `compute` | Device and precision actually used; `cpu` after a fallback |
| `duration` | Audio duration in seconds |
| `segments[]` | `s` / `e` / `t` per segment: start, end, text |
| `stats` | `chars_per_min` / `segs_per_min` / `punct_per_100` / `max_gap_s` / `tail_gap_s` |
| `load_s` / `asr_s` / `rtf_x` | Load time / pure inference time / real-time factor. `asr_s` excludes loading |
| `fallback` / `fallback_load_s` | Non-null means a fallback happened, with the original exception; the latter is the load time already spent on the failed attempt |
| `error` | Failure reason, `null` on success |

### The three post-run checks

| Check | How to judge |
|---|---|
| First 30 s | Is the prompt read aloud into the text? Did an intro line slip in? Was the real opening swallowed whole? |
| Last 30 s | Any invented "thanks for watching"? Does the last segment end after the audio duration? |
| Long gaps | Any gap > 8 seconds: listen back, or compare that interval against the other engine |

A negative "tail gap" is not a pass; it means hallucinated segments were added. These are hard checks, not reference values.

## Configuration

There is **no config file, no API key, and no account**. Every parameter is a CLI flag. The only environment variable the scripts read is `PATH` (`transcribe.py` uses it to locate CUDA DLL directories).

### transcribe.py

| Flag | Default | Meaning |
|---|---|---|
| `--wav` | required | 16 kHz mono PCM WAV |
| `--model` | required | CTranslate2 directory (5 files) |
| `--out` | required | Output `.json` path; the `.md` is written next to it |
| `--tag` | empty | Task label; defaults to the output file name |
| `--device` | `cuda` | `cuda` or `cpu` |
| `--compute` | automatic | `float16` on `cuda`, `int8` on `cpu` |
| `--language` | `zh` | Recognition language |
| `--beam-size` | `5` | Beam width |
| `--min-silence` | `300` | VAD (voice activity detection) minimum silence, ms |
| `--no-vad` | off | Disable VAD |
| `--no-prompt` | off | Disable `initial_prompt`. **Ablation only, never for production**: punctuation density collapses without it |
| `--hallucination-silence-threshold` | unset | Passed through to faster-whisper |
| `--cpu-threads` | `8` | CPU thread count |
| `--no-fallback` | off | Never fall back; report the CUDA error as-is |

### batch_transcribe.py

| Flag | Default | Meaning |
|---|---|---|
| `--input` | required | One or more directories, or explicit files |
| `--out-dir` | required | Output directory; also holds `_batch/summary.json` |
| `--model` | empty | May be omitted with `--dry-run` |
| `--pattern` | `*.wav` | File-name pattern, case-insensitive; names only, no extra decoders |
| `--recursive` | off | Descend into subdirectories |
| `--force` | off | Re-run even when output exists |
| `--dry-run` | off | Print the plan only |
| `--timeout` | `1800` | Per-file timeout in seconds |
| `--device` / `--compute` / `--language` | same as single run | Passed through to `transcribe.py` |

### qc_check.py and check_env.py

| Flag | Default | Meaning |
|---|---|---|
| `qc_check.py files...` | required | JSON files or glob patterns |
| `qc_check.py --gap` | `8.0` | Long-gap threshold, strictly greater-than |
| `qc_check.py --tail` | `10.0` | Tail-gap warning threshold |
| `qc_check.py --json` | off | Machine-readable output |
| `check_env.py --models` | none | Model directories to verify, one or more |
| `check_env.py --json` | off | Machine-readable output |

## Project structure

```text
asr-transcription/
├── SKILL.md                  # entry point (106 lines): trigger, workflow, routing, three rules
├── README.md / README_EN.md  # this file and the Chinese original
├── ROADMAP.md                # version log, deferred items, red lines, release checks
├── NOTICE.md                 # scope of the licence and third-party attribution
├── LICENSE                   # MIT
├── references/               # 10 files, routed by problem, one at a time
│   ├── 01-selection.md       # engine choice, five questions, reference numbers
│   ├── 02-workflow.md        # standard workflow, sources, batch engineering
│   ├── 03-parameters.md      # parameters, GPU DLLs, fallback, version pins
│   ├── 04-quality.md         # QC, three checks, references, hallucination shapes
│   ├── 05-vocabulary.md      # proper nouns
│   ├── 06-environment.md     # environment, resource guardrails, installs
│   ├── 07-pitfalls.md        # pitfall list
│   ├── 08-methodology.md     # methodology limits, open questions
│   ├── 09-attribution.md     # full third-party attribution
│   └── 10-commands.md        # command cheat sheet (bash + PowerShell)
├── scripts/
│   ├── transcribe.py         # transcription, JSON + MD
│   ├── batch_transcribe.py   # serial batch runner
│   ├── qc_check.py           # validation + four coarse metrics + three checks
│   └── check_env.py          # read-only environment check
├── tests/                    # three zero-dependency suites, 1028 assertions
└── docs/
    ├── 转写经验_通用版.md      # the original long-form notes; references win on conflict
    ├── 字幕识别与OCR_调研与设计.md  # unimplemented design: three routes, measurements, red-line conflicts
    └── 参考项目评估_native-subtitle-quote-image.md  # upstream evaluation: band detection, frame diffing
```

Three loading levels: `name` + `description` always resident → `SKILL.md` on trigger → one `references/` file on demand. Scripts execute directly; they never need to enter the context window.

## Development

### Tests

```powershell
python -m compileall -q scripts tests
python tests/run_smoke.py            # QC contract: 33 inputs x text/JSON
python tests/run_transcribe_unit.py  # fallback decision, timing semantics, atomic output
python tests/run_batch_unit.py       # batch planning, skip rule, end-to-end
```

Actual output (2026-11-06):

```text
33 cases x (text + --json), 883 assertions, 0 failures.
43 assertions, 0 failures.
102 assertions, 0 failures.
```

None of the three needs network, a model, a GPU, or ffmpeg. The batch end-to-end case drives `main()` with a fake `transcribe.py`, covering success, failure, skipping, retry after a fix, and half-written artefacts.

### The "false green" trap on Windows

The test harness injects `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` into child processes. Without them, a Chinese Windows console decodes as GBK and tests fail on encoding rather than logic. The price: **green here does not mean green in a clean environment**.

For any change touching the console or child processes, clear both variables and run again before committing:

```powershell
Remove-Item Env:PYTHONUTF8, Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
python tests/run_smoke.py
```

### Red lines

1. **Never break existing `--json` keys**: `error` / `red` / `warn` / `duration` / `has_timestamps` / `note` and all metric keys stay. New fields are additive only.
2. **No new dependencies, no network, no uploading material**: tests must stay zero-dependency and repeatable offline.
3. **No audio, model weights, transcripts, credentials, or absolute local paths in the repository.**
4. **Keep `SKILL.md` lean** (106 lines): details go to `references/`, architecture to `README.md`, planning to `ROADMAP.md`.
5. **ERROR ≠ wrong transcript**: ERROR is an input-data problem; RED needs human review. Neither may be described as "the transcription was wrong".

More in [`ROADMAP.md`](ROADMAP.md) section 3 and [`references/08-methodology.md`](references/08-methodology.md).

## FAQ

**Q: `check_env.py` sees the DLLs, but running fails with `cublas64_12.dll not found`.**
A: Existing files do not mean loadable. cuBLAS / cuDNN usually live under the **user-level** `site-packages`, not the interpreter directory. Check `python -c "import site; print(site.getusersitepackages())"`, then confirm that directory is on `PATH`. If it still fails, run one real inference instead of trusting a file listing.

**Q: `pip install av` pulled 19.x and `decode_audio` now fails.**
A: Pin `av<19`. faster-whisper 1.2.1 does not ship PyAV 19 support (upstream main has the fix, but it is not in that release). Python 3.14 is out of scope for the same reason.

**Q: Why is there no `--word-timestamps`?**
A: `transcribe.py` emits segment-level `s` / `e` / `t` only and never writes `words`. A flag that changes decoding but not output would make people believe they have word timestamps, so it was removed. Word timestamps, speaker diarisation, and SRT export are item C3 in `ROADMAP.md`, unscheduled.

**Q: `--json` redirected to a file is unreadable by other tools.**
A: Since v0.4.3, non-TTY output is forced to UTF-8. Older versions emitted GBK bytes on Chinese Windows, which is not valid JSON. Upgrade to v0.4.3 or later; TTY output still uses the local code page, so the screen is unaffected.

**Q: I hit Ctrl+C halfway through a batch. Will a re-run transcribe everything again?**
A: No. The rule is the artefacts themselves: `<key>.json` parses, has no `error`, contains `segments` and `duration`, and `<key>.md` exists and is non-empty. Finished pairs are skipped; a file with JSON but no MD is retried.

**Q: One file failed, yet later runs keep skipping it.**
A: Fixed in v0.5.1. `--force` now moves both the JSON and the MD aside as `.prev`, so a failed run leaves no stale artefacts and the next run retries. On v0.5 or older, delete the stale `.json` and `.md` once.

**Q: Can you give me an accuracy number?**
A: No. Accuracy needs a human-verified reference transcript as the denominator. Without one, this project reports where two engines disagree plus manual spot checks. On-screen subtitle OCR is supporting evidence only, never ground truth.

**Q: Does it accept mp3 / m4a?**
A: Not directly. Run `ffmpeg -i in.m4a -vn -ac 1 -ar 16000 -c:a pcm_s16le out.wav` first. `--pattern "*.m4a"` only collects the files; decoding then fails.

## Known limitations

### Verified platforms

| Environment | Status | Notes |
|---|---|---|
| Windows 11 + Python 3.13 + NVIDIA GPU (CUDA 12.x / cuDNN 9, ≥ 8 GB VRAM) | **Tested** | All scripts, the main pipeline, and the QC regression ran here |
| Same machine, CPU only (`--device cpu --compute int8`) | **Tested, single and batch** | About 2.6–2.9x real time; the QC regression set is GPU-heavy |
| Windows 10 / other NVIDIA GPUs / < 6 GB VRAM | Not tested | Verify against `references/06-environment.md`; use turbo or CPU when VRAM is short |
| Linux / macOS / AMD or Intel GPU | Not tested, do not copy the steps | DLL directories, PowerShell examples, and user-level `site-packages` paths are Windows-specific |
| Python 3.14 / PyAV 19 / torch-based stacks | Out of scope | Version pins in `references/03-parameters.md` |

### Capability boundaries

- **Segment-level timestamps only.** No word-level `words`, no speaker diarisation, no direct SRT / VTT export.
- **No subtitle extraction.** Neither subtitle tracks nor on-screen text are read; video is transcribed from its audio track only. The feasibility study, measurements and implementation plan live in [`docs/字幕识别与OCR_调研与设计.md`](docs/字幕识别与OCR_调研与设计.md) — **not implemented**.
- **Single-file failures leave no artefact.** Calling `transcribe.py` directly on a failing input produces nothing; only the exit code tells you. Batch covers it (`_batch/summary.json` records exit code and error tail); the single-file case is still C4 in `ROADMAP.md`.
- **No accuracy claims.** Without a human reference there is no number. That is a rule, not modesty.
- **SenseVoice is a second opinion only.** Its output has no usable timestamps, and it is clearly weaker on mixed Chinese/English proper nouns. It cannot produce a final transcript alone.
- **You download the models.** The repository contains no weights, no audio, and no credentials.
- **No directory-level report.** `_batch/summary.json` is per file; there is no cross-directory rollup.

### Limits of the performance numbers

| Setup | Real-time factor | RAM increase | VRAM increase | Final transcript alone |
|---|---|---|---|---|
| turbo / GPU | about 19x | about 1.2–1.6 GB | about 1.9–2.1 GB | Yes |
| large-v3 / GPU | about 6x | about 3.0–3.1 GB | about 3.8–3.9 GB | Yes, but expensive; review only |
| turbo / CPU | about 2.6–2.9x | about 1.4–1.5 GB | 0 | Slow |
| SenseVoice q8 GGUF / CPU | about 27–28x | about 0.36–0.37 GB | 0 | Preview / comparison / missing-segment hints |

Source: one machine, 2026-11-05, **4 clips** of Chinese speech (1.31 / 1.50 / 2.12 / 3.90 minutes), **9 configurations, 28 runs**, roughly ±30% variance. The sample is in the single digits, so these numbers indicate relative order only. **They are not a cross-machine benchmark** and do not extrapolate to long recordings, meetings, or read-aloud material. Full table and definitions in `references/01-selection.md` section 1.7.

## Third-party projects and licenses

This repository trains and ships no models. It calls the projects below. **Code licences and model-weight licences are separate concerns**; check the current upstream repository and model card before commercial use. The full register, with verification dates and caveats, is in [`references/09-attribution.md`](references/09-attribution.md).

### Main pipeline

| Project / model | Maintainer | URL | Licence | Used for |
|---|---|---|---|---|
| OpenAI Whisper (`large-v3`, `large-v3-turbo` weights) | OpenAI | https://github.com/openai/whisper | MIT | Main model weights; turbo is a decoder-pruned large-v3 |
| faster-whisper | SYSTRAN | https://github.com/SYSTRAN/faster-whisper | MIT | Main engine and all transcription parameters |
| CTranslate2 | OpenNMT | https://github.com/OpenNMT/CTranslate2 | MIT | Inference backend for faster-whisper |
| PyAV (`av`) | PyAV-Org | https://github.com/PyAV-Org/PyAV | BSD-3-Clause | Audio decoding; pinned to `av<19` |
| CUDA / cuBLAS / cuDNN | NVIDIA | https://developer.nvidia.com/ | NVIDIA proprietary | GPU operator libraries |

### Supplementary and comparison (actually tested)

| Project / model | Maintainer | URL | Licence | Used for |
|---|---|---|---|---|
| SenseVoice / `SenseVoiceSmall` | QwenAudio (formerly FunAudioLLM; repo moved) | https://github.com/QwenAudio/SenseVoice | **Code MIT / weights `license: other` (FunASR Model License)** | CPU-only independent second opinion |
| FunASR | modelscope (Alibaba DAMO Speech) | https://github.com/modelscope/FunASR | MIT | GGUF runtime, VAD, punctuation, hotwords |
| FunASR llama.cpp runtime | modelscope/FunASR releases | https://github.com/modelscope/FunASR/releases | Ships with FunASR (MIT) | Windows binaries such as `llama-funasr-sensevoice.exe` |
| llama.cpp / GGUF | ggml-org | https://github.com/ggml-org/llama.cpp | MIT | q8 quantised model and runtime |
| sherpa-onnx | k2-fsa | https://github.com/k2-fsa/sherpa-onnx | Apache-2.0 | int8 ONNX comparison for SenseVoice / Paraformer |
| Paraformer | modelscope/FunASR (Alibaba DAMO) | https://github.com/modelscope/FunASR | Code MIT / weights per model card | "No punctuation" comparison; not recommended alone |

### Mentioned during selection, not adopted

| Project | Maintainer | URL | Licence |
|---|---|---|---|
| Qwen3-ASR | QwenLM (Alibaba Cloud Qwen team) | https://github.com/QwenLM/Qwen3-ASR | Apache-2.0 |
| WhisperX | `m-bain` (individual) | https://github.com/m-bain/whisperX | BSD-2-Clause |
| whisper.cpp | ggml-org | https://github.com/ggml-org/whisper.cpp | MIT |
| PyTorch / Transformers / vLLM | their respective organisations | official docs | per project; explicitly **not installed** here |

### General tools and platforms

| Project | Maintainer | URL | Licence | Used for |
|---|---|---|---|---|
| FFmpeg / ffprobe | FFmpeg project | https://www.ffmpeg.org/ | LGPL / GPL depending on build | Audio extraction, source inspection |
| ONNX Runtime | Microsoft | https://github.com/microsoft/onnxruntime | MIT | Backend for the ONNX comparison |
| NumPy | NumPy community | https://numpy.org/ | BSD-3-Clause | Pulled in as a dependency |
| RapidOCR | RapidAI | https://github.com/RapidAI/RapidOCR | Apache-2.0 | Optional keyframe OCR cross-check |
| Hugging Face Hub / `hf-mirror.com` | Hugging Face / third-party mirror | https://huggingface.co/ | platform service, not a dependency | Model downloads |

### Licence boundary

- The MIT in `LICENSE` covers **only original content here**: documentation, scripts, and the Skill structure.
- Third-party model weights, engines, and runtimes are **not** covered; their rights stay with their maintainers.
- This repository contains **no** weight files (`*.bin` / `*.gguf` / `*.onnx`), audio, transcripts, or credentials.
- When redistributing, keep `LICENSE` and the attribution in `references/09-attribution.md`.

Attribution follows the official repository owner. Ownership moves (SenseVoice went from `FunAudioLLM` to `QwenAudio`), so re-check before use. Verified 2026-11-05 against the GitHub REST API and official model cards. **The SenseVoiceSmall weight licence could not be re-fetched from the HF API in that pass and reuses an earlier same-day check; open the model card before commercial use.**

## Contributing

Maintained by ChenChen913 as a personal project, with no commercial support.

- **Bugs and requests**: open an [issue](https://github.com/ChenChen913/asr-transcription-skill/issues). Include the command, exit code, the original error, and `check_env.py` output with local paths removed.
- **Pull requests**: welcome, but there is no fixed process and no promise of a timely review. Open an issue first so a large change does not get rejected on scope.
- **Before submitting**: all three suites green, and green again with `PYTHONUTF8` / `PYTHONIOENCODING` cleared (see [Development](#development)).
- **Do not commit**: model weights, audio, transcripts, credentials, or absolute local paths.
- **Do not add features just to support more models.** The goal is transcript quality and a reliable workflow, not model count.

## License

[MIT](LICENSE) © 2026 ChenChen913

`LICENSE` contains the standard MIT text only; the licence boundary and third-party attribution live in [`NOTICE.md`](NOTICE.md). Third-party projects remain the property of their maintainers, and this repository claims no rights over them.
