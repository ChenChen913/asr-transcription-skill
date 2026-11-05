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
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    print("=== ASR environment (read-only) ===")
    print("Python      :", out["python"], "|", out["executable"])
    for k, v in out["modules"].items():
        print("  %-22s %s" % (k, v if v else "not installed"))
    print("CUDA devices:", out["cuda_devices"])
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


if __name__ == "__main__":
    main()
