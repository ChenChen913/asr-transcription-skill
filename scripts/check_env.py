# -*- coding: utf-8 -*-
"""Read-only environment check for the asr-transcription skill.

Prints Python/dependency versions, CUDA device count, GPU info, disk space,
ffmpeg availability and model-directory completeness. Never writes files.
"""
import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys

MODULES = ["faster_whisper", "ctranslate2", "onnxruntime", "av", "numpy",
           "torch", "sherpa_onnx", "rapidocr_onnxruntime"]
MODEL_FILES = ["config.json", "preprocessor_config.json", "tokenizer.json",
               "vocabulary.json", "model.bin"]


def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def cuda_dlls():
    """Locate cublas/cudnn bin dirs and report which key DLL files exist."""
    import site
    bases = []
    for getter in ("getusersitepackages", "getsitepackages"):
        try:
            v = getattr(site, getter)()
            bases += v if isinstance(v, list) else [v]
        except Exception:
            pass
    want = {"cublas": ["cublas64_12.dll", "cublasLt64_12.dll"],
            "cudnn": ["cudnn64_9.dll", "cudnn_ops64_9.dll"]}
    found = {}
    for b in bases:
        for sub, files in want.items():
            d = os.path.join(b, "nvidia", sub, "bin")
            if os.path.isdir(d):
                found[sub] = {"dir": d,
                              "dlls": {f: os.path.isfile(os.path.join(d, f)) for f in files}}
    return found


def main():
    ap = argparse.ArgumentParser(description="Read-only ASR environment check")
    ap.add_argument("--models", nargs="*", default=[],
                    help="model directories to verify (expects the 5 CT2 files)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args()

    out = {"python": sys.version.split()[0], "executable": sys.executable,
           "modules": {}, "cuda_devices": None, "gpu": "", "ffmpeg": "",
           "ffprobe": "", "disks": {}, "models": {}}

    for name in MODULES:
        try:
            m = importlib.import_module(name)
            out["modules"][name] = getattr(m, "__version__", "installed")
        except Exception:
            out["modules"][name] = None
    try:
        import ctranslate2 as c
        out["cuda_devices"] = c.get_cuda_device_count()
    except Exception:
        pass

    out["gpu"] = run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version",
                      "--format=csv,noheader"])
    out["cuda_dlls"] = cuda_dlls()
    try:
        import ctranslate2 as c
        out["cuda_compute_types"] = list(c.get_supported_compute_types("cuda"))
    except Exception as e:
        out["cuda_compute_types"] = "UNAVAILABLE: " + str(e)[:120]
    out["ffmpeg"] = run(["ffmpeg", "-version"]).splitlines()[:1]
    out["ffprobe"] = run(["ffprobe", "-version"]).splitlines()[:1]
    out["ffmpeg"] = out["ffmpeg"][0] if out["ffmpeg"] else ""
    out["ffprobe"] = out["ffprobe"][0] if out["ffprobe"] else ""

    for d in ["C:/", "D:/"]:
        try:
            t, u, f = shutil.disk_usage(d)
            out["disks"][d] = {"total_gb": round(t / 1024 ** 3, 1),
                               "free_gb": round(f / 1024 ** 3, 1)}
        except Exception:
            out["disks"][d] = None

    for p in a.models:
        present = [f for f in MODEL_FILES if os.path.isfile(os.path.join(p, f))]
        out["models"][p] = {"complete": len(present) == len(MODEL_FILES),
                            "found": len(present), "expected": len(MODEL_FILES),
                            "missing": [f for f in MODEL_FILES if f not in present]}

    if a.json:
        out["problems"] = problems(out, a.models)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    print("=== ASR environment (read-only) ===")
    print("Python      :", out["python"], "|", out["executable"])
    for k, v in out["modules"].items():
        print("  %-22s %s" % (k, v if v else "not installed"))
    print("CUDA devices:", out["cuda_devices"])
    print("CUDA compute types:", out["cuda_compute_types"])
    for sub, v in out["cuda_dlls"].items():
        ok = sum(v["dlls"].values())
        print("  %-7s DLL %d/%d present | %s" % (sub, ok, len(v["dlls"]), v["dir"]))
    if not out["cuda_dlls"]:
        print("  WARNING: no cublas/cudnn bin directories found")
    print("  NOTE: DLL file presence does NOT prove they load; run one real inference.")
    print("GPU         :", out["gpu"] or "nvidia-smi unavailable")
    print("ffmpeg      :", out["ffmpeg"] or "NOT FOUND")
    for d, v in out["disks"].items():
        if v:
            print("Disk %-6s total %.1f GB / free %.1f GB" % (d, v["total_gb"], v["free_gb"]))
    for p, v in out["models"].items():
        print("Model %-48s %s (%d/%d)%s" % (
            p, "OK" if v["complete"] else "INCOMPLETE", v["found"], v["expected"],
            " missing=" + ",".join(v["missing"]) if v["missing"] else ""))
    print()
    print("Reminder: pin av<19 on faster-whisper 1.2.1; verify cublas/cudnn DLLs")
    print("are reachable before inference, not just at model load time.")

    issues = problems(out, a.models)
    if issues:
        print()
        print("[FAIL] %d problem(s):" % len(issues))
        for item in issues:
            print("  - " + item)
        sys.exit(1)
    print("[OK] required pieces present (this is still not a real inference)")


def problems(out, models):
    """What would actually stop a transcription run.

    Kept separate from the printout so --json and the exit code cannot drift
    apart. A missing GPU is deliberately NOT a problem: --device cpu is a
    supported path. A missing OCR runtime is not a problem either - only
    ocr_subs.py needs it, and that script says so on its own.
    """
    issues = []
    for name in ("faster_whisper", "ctranslate2", "av"):
        if not out["modules"].get(name):
            issues.append("%s is not installed" % name)
    if not out["ffmpeg"]:
        issues.append("ffmpeg is not on PATH (needed to extract audio)")
    for path in models:
        info = out["models"].get(path) or {}
        if not info.get("complete"):
            issues.append("model %s is incomplete (%d/%d)%s" % (
                path, info.get("found", 0), info.get("expected", 5),
                " missing=" + ",".join(info["missing"]) if info.get("missing") else ""))
    return issues


if __name__ == "__main__":
    main()
