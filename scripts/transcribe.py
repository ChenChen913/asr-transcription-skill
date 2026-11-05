# -*- coding: utf-8 -*-
"""Transcribe one 16 kHz mono WAV into the JSON/MD schema consumed by qc_check.py.

Reads only the input WAV and model directory; writes one .json and one .md.
A CUDA failure falls back to CPU int8 only when the error is actually GPU
related (cuda / cublas / cudnn / nvml / out of memory / ...). Broken input,
broken model files and bad parameters fail fast with the original error instead
of being retried on CPU. The device actually used is recorded in the output.

Exit codes: 0 ok | 2 unusable input/model | 3 failed on requested device |
4 both GPU and CPU attempts failed.

Example:
  python scripts/transcribe.py --wav audio/S1.wav \
      --model D:/hf-models/faster-whisper-large-v3-turbo --out out/A1_S1.json
  python scripts/qc_check.py out/A1_S1.json
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
import wave

DEFAULT_PROMPT = "以下是普通话的句子，请使用简体中文，并加上标点符号。"

# A substring match on "type name + message". Deliberately tight: falling back
# to CPU never fixes a missing model file or an undecodable WAV, it only doubles
# the wait and buries the real error.
GPU_ERROR_HINTS = ("cuda", "cublas", "cudnn", "cudart", "nvml",
                   "no kernel image", "out of memory", "gpu")

# os.add_dll_directory() returns a handle whose close() removes the directory
# again. Dropping the return value closes it immediately, so the directories
# must be kept alive for the whole process.
_DLL_HANDLES = []


def cuda_dll_dirs():
    """Yield cublas/cudnn bin dirs that exist in user or system site-packages."""
    import site
    bases = []
    for getter in ("getusersitepackages", "getsitepackages"):
        try:
            v = getattr(site, getter)()
            bases += v if isinstance(v, list) else [v]
        except Exception:
            pass
    seen = set()
    for b in bases:
        for sub in ("cublas", "cudnn"):
            d = os.path.join(b, "nvidia", sub, "bin")
            if os.path.isdir(d) and d not in seen:
                seen.add(d)
                yield d


def prepare_cuda_dlls():
    """Make nvidia pip DLLs findable and KEEP the directory handles alive.

    Two mechanisms are kept on purpose: the handle covers LoadLibraryEx with
    LOAD_LIBRARY_SEARCH_* flags, PATH covers plain LoadLibrary lookups
    (ctranslate2 uses the latter on Windows, so removing PATH is not an option).
    """
    found = []
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    for d in cuda_dll_dirs():
        if hasattr(os, "add_dll_directory"):
            try:
                _DLL_HANDLES.append(os.add_dll_directory(d))
            except OSError as e:
                print("warn: add_dll_directory failed for %s: %s" % (d, e),
                      file=sys.stderr)
        if d not in path_parts:
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            path_parts.insert(0, d)
        found.append(d)
    return found


def wav_duration(path):
    with wave.open(path, "rb") as f:
        return f.getnframes() / float(f.getframerate())


def read_duration(path):
    """Fail fast with a readable message instead of a traceback."""
    try:
        duration = wav_duration(path)
    except Exception as e:
        print("cannot read WAV %s: %s: %s" % (path, type(e).__name__, e),
              file=sys.stderr)
        sys.exit(2)
    if duration <= 0:
        print("WAV has zero length: %s" % path, file=sys.stderr)
        sys.exit(2)
    return duration


def punct_per_100(text):
    n = len(re.findall(r"[，。！？、,.!?;；:：]", unicodedata.normalize("NFKC", text)))
    return round(n / max(len(text), 1) * 100, 2)


def build_model(model_dir, device, compute, cpu_threads):
    from faster_whisper import WhisperModel
    return WhisperModel(model_dir, device=device, compute_type=compute,
                        cpu_threads=cpu_threads)


def transcribe(model, wav, a, prompt):
    kwargs = dict(
        language=a.language,
        beam_size=a.beam_size,
        vad_filter=not a.no_vad,
        vad_parameters={"min_silence_duration_ms": a.min_silence},
        condition_on_previous_text=False,
        initial_prompt=prompt,
        temperature=[0.0, 0.2, 0.4],
        word_timestamps=a.word_timestamps,
    )
    if a.hallucination_silence_threshold is not None:
        kwargs["hallucination_silence_threshold"] = a.hallucination_silence_threshold
    segments, info = model.transcribe(wav, **kwargs)
    segs = list(segments)  # force the generator so inference errors surface here
    return segs, info


def looks_like_gpu_failure(e):
    msg = ("%s %s" % (type(e).__name__, e)).lower()
    return any(h in msg for h in GPU_ERROR_HINTS)


def run_attempt(model_dir, wav, a, prompt, device, compute, timings):
    """One full attempt; timings captures each stage that actually completed.

    Keeping the two stages apart is what makes asr_s a pure inference number:
    a fallback must not hide the CPU model load inside the real-time factor.
    """
    t0 = time.time()
    model = build_model(model_dir, device, compute, a.cpu_threads)
    timings["load_s"] = round(time.time() - t0, 2)
    t1 = time.time()
    segs, info = transcribe(model, wav, a, prompt)
    timings["asr_s"] = round(time.time() - t1, 2)
    return segs, info


def main():
    ap = argparse.ArgumentParser(description="Transcribe one WAV into qc_check-compatible JSON + MD")
    ap.add_argument("--wav", required=True)
    ap.add_argument("--model", required=True, help="CTranslate2 model directory (5 files)")
    ap.add_argument("--out", required=True, help="output .json path; .md written alongside")
    ap.add_argument("--tag", default="")
    ap.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    ap.add_argument("--compute", default=None, help="default: float16 on cuda, int8 on cpu")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--beam-size", type=int, default=5)
    ap.add_argument("--min-silence", type=int, default=300)
    ap.add_argument("--no-vad", action="store_true")
    ap.add_argument("--no-prompt", action="store_true", help="ablation only; not for production")
    ap.add_argument("--hallucination-silence-threshold", type=float, default=None)
    ap.add_argument("--word-timestamps", action="store_true")
    ap.add_argument("--cpu-threads", type=int, default=8)
    ap.add_argument("--no-fallback", action="store_true",
                    help="never retry on CPU; report the CUDA error as-is")
    a = ap.parse_args()

    if not os.path.isfile(a.wav):
        print("WAV not found: " + a.wav, file=sys.stderr)
        sys.exit(2)
    if not os.path.isfile(os.path.join(a.model, "model.bin")):
        print("Model dir missing model.bin: " + a.model, file=sys.stderr)
        sys.exit(2)

    prompt = None if a.no_prompt else DEFAULT_PROMPT
    device = a.device
    compute = a.compute or ("float16" if device == "cuda" else "int8")
    dll_dirs = prepare_cuda_dlls() if device == "cuda" else []
    duration = read_duration(a.wav)

    fallback = None
    fallback_load_s = None
    gpu_timings = {}
    try:
        segs, info = run_attempt(a.model, a.wav, a, prompt, device, compute,
                                 gpu_timings)
        load_s, asr_s = gpu_timings["load_s"], gpu_timings["asr_s"]
    except Exception as e:
        gpu_error = "%s: %s" % (type(e).__name__, str(e)[:300])
        if device != "cuda" or a.no_fallback or not looks_like_gpu_failure(e):
            # Not a GPU problem (or fallback disabled): retrying on CPU would
            # waste the same minutes and hide the real cause.
            print("transcription failed on %s\n  %s" % (device, gpu_error),
                  file=sys.stderr)
            sys.exit(3)
        print("GPU attempt failed, retrying on CPU\n  %s" % gpu_error,
              file=sys.stderr)
        fallback_load_s = gpu_timings.get("load_s")
        device, compute = "cpu", "int8"
        cpu_timings = {}
        try:
            segs, info = run_attempt(a.model, a.wav, a, prompt, device, compute,
                                     cpu_timings)
        except Exception as e2:
            print("both attempts failed\n  GPU: %s\n  CPU: %s: %s"
                  % (gpu_error, type(e2).__name__, e2), file=sys.stderr)
            sys.exit(4)
        load_s, asr_s = cpu_timings["load_s"], cpu_timings["asr_s"]
        fallback = gpu_error

    items = []
    for s in segs:
        items.append({"s": round(float(s.start), 2), "e": round(float(s.end), 2),
                      "t": s.text})
    text = "".join(x["t"] for x in items)
    minutes = duration / 60.0 if duration else 0
    gaps = [(items[i + 1]["s"] - items[i]["e"]) for i in range(len(items) - 1)]
    stats = {
        "chars_per_min": round(len(text) / minutes, 1) if minutes else 0,
        "segs_per_min": round(len(items) / minutes, 1) if minutes else 0,
        "punct_per_100": punct_per_100(text),
        "max_gap_s": round(max(gaps), 2) if gaps else 0.0,
        "tail_gap_s": round(duration - items[-1]["e"], 2) if items else None,
    }

    out = {
        "tag": a.tag or os.path.splitext(os.path.basename(a.out))[0],
        "engine": "faster-whisper", "model": os.path.basename(a.model.rstrip("/\\")),
        "device": device, "compute": compute, "wav": os.path.basename(a.wav),
        "duration": round(duration, 2), "language": a.language,
        "params": {"beam_size": a.beam_size, "vad_filter": not a.no_vad,
                   "min_silence_duration_ms": a.min_silence,
                   "condition_on_previous_text": False,
                   "initial_prompt": prompt,
                   "temperature": [0.0, 0.2, 0.4],
                   "word_timestamps": a.word_timestamps,
                   "hallucination_silence_threshold": a.hallucination_silence_threshold,
                   "no_fallback": a.no_fallback},
        "timestamps": True, "segments": items, "text": text, "chars": len(text),
        # load_s / asr_s always describe the device that finally succeeded;
        # asr_s is pure inference, never model loading.
        "stats": stats, "load_s": load_s, "asr_s": asr_s,
        "rtf_x": round(duration / max(asr_s, 1e-6), 2),
        "cuda_dll_dirs": dll_dirs, "fallback": fallback,
        "fallback_load_s": fallback_load_s, "error": None,
    }

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    md_path = os.path.splitext(a.out)[0] + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# %s\n\n- 设备: %s / %s | 时长: %.2fs | 实时倍率: %s×\n\n"
                % (out["tag"], device, compute, duration, out["rtf_x"]))
        for x in items:
            f.write("[%.2f - %.2f] %s\n\n" % (x["s"], x["e"], x["t"]))

    print("OK %s | %s/%s | %.2fs audio in %.2fs (%.2fx) | %d segs / %d chars%s"
          % (out["tag"], device, compute, duration, asr_s, out["rtf_x"],
             len(items), len(text), " | CPU FALLBACK" if fallback else ""))
    print("   json:", a.out)
    print("   md  :", md_path)
    print("   next: python scripts/qc_check.py \"%s\"" % a.out)


if __name__ == "__main__":
    main()
