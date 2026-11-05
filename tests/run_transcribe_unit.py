# -*- coding: utf-8 -*-
"""Offline unit tests for scripts/transcribe.py.

Covers what a real run cannot exercise cheaply: the GPU -> CPU fallback
decision, the timing split (asr_s must never contain model loading), and the
DLL handle lifetime. build_model / transcribe are monkeypatched, so there is
no network, no model weight, no GPU and no ffmpeg dependency.

Run:  python tests/run_transcribe_unit.py
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC = importlib.util.spec_from_file_location(
    "tr_module", os.path.join(ROOT, "scripts", "transcribe.py"))
tr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tr)

LOAD_SLEEP = 0.20
INFER_SLEEP = 0.30
# Generous bounds: Windows timer granularity varies, but 0.20 s is far enough
# from both ends to tell "inference only" from "load + inference".
ASR_MAX = 0.45
LOAD_MIN, LOAD_MAX = 0.12, 0.40

checks = [0]
failures = []


def ok(cond, label):
    checks[0] += 1
    if not cond:
        failures.append(label)


class FakeSeg:
    def __init__(self, s, e, t):
        self.start, self.end, self.text = s, e, t


class FakeInfo:
    language = "zh"
    duration = 3.0


def make_wav(path, seconds=3.0):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))


def run_main(argv, build, infer):
    """Run tr.main() with patched model layer; returns (exit_code, stderr)."""
    tr.build_model = build
    tr.transcribe = infer
    old_argv, old_err = sys.argv, sys.stderr
    sys.argv = ["transcribe.py"] + argv
    buf = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(io.StringIO()):
            tr.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    finally:
        sys.argv = old_argv
        sys.stderr = old_err
    return code, buf.getvalue()


def case_normal(tmp):
    calls = {"load": [], "infer": 0}

    def build(model_dir, device, compute, threads):
        calls["load"].append(device)
        time.sleep(LOAD_SLEEP)
        return object()

    def infer(model, wav, a, prompt):
        calls["infer"] += 1
        time.sleep(INFER_SLEEP)
        return [FakeSeg(0.0, 2.0, "正常文本内容，用于校验。")], FakeInfo()

    out = os.path.join(tmp, "normal.json")
    code, err = run_main(["--wav", WAV, "--model", MODEL, "--out", out], build, infer)
    d = json.load(open(out, encoding="utf-8"))
    ok(code == 0, "normal: exit 0, got %s (%s)" % (code, err[:80]))
    ok(d["device"] == "cuda" and d["compute"] == "float16", "normal: cuda/float16")
    ok(d["fallback"] is None and d["fallback_load_s"] is None, "normal: no fallback fields")
    ok(LOAD_MIN <= d["load_s"] <= LOAD_MAX, "normal: load_s ~%.2f, got %s" % (LOAD_SLEEP, d["load_s"]))
    ok(INFER_SLEEP - 0.05 <= d["asr_s"] <= ASR_MAX, "normal: asr_s ~%.2f, got %s" % (INFER_SLEEP, d["asr_s"]))
    ok(abs(d["rtf_x"] - 3.0 / d["asr_s"]) < 0.05, "normal: rtf_x = duration/asr_s")
    ok(calls["load"] == ["cuda"] and calls["infer"] == 1, "normal: one attempt")


def case_gpu_load_failure(tmp):
    """CUDA cannot initialise -> one CPU attempt, timing must restart there."""
    calls = {"load": [], "infer": 0}

    def build(model_dir, device, compute, threads):
        calls["load"].append(device)
        if device == "cuda":
            raise RuntimeError("CUDA failed with error no CUDA-capable device is detected")
        time.sleep(LOAD_SLEEP)
        return object()

    def infer(model, wav, a, prompt):
        calls["infer"] += 1
        time.sleep(INFER_SLEEP)
        return [FakeSeg(0.0, 2.0, "回退后的文本。")], FakeInfo()

    out = os.path.join(tmp, "fb_load.json")
    code, err = run_main(["--wav", WAV, "--model", MODEL, "--out", out], build, infer)
    d = json.load(open(out, encoding="utf-8"))
    ok(code == 0, "fb_load: exit 0, got %s (%s)" % (code, err[:80]))
    ok(d["device"] == "cpu" and d["compute"] == "int8", "fb_load: cpu/int8 recorded")
    ok("no CUDA-capable device" in (d["fallback"] or ""), "fb_load: original error kept")
    ok(d["fallback_load_s"] is None, "fb_load: no GPU load time to report")
    ok(d["asr_s"] <= ASR_MAX, "fb_load: asr_s excludes CPU load, got %s" % d["asr_s"])
    ok(calls["load"] == ["cuda", "cpu"], "fb_load: cuda then cpu, got %s" % calls["load"])
    ok("retrying on CPU" in err, "fb_load: warning printed")


def case_gpu_infer_failure(tmp):
    """GPU loads fine but inference dies -> GPU load time must be preserved."""
    calls = {"infer": 0}

    def build(model_dir, device, compute, threads):
        time.sleep(LOAD_SLEEP)
        return object()

    def infer(model, wav, a, prompt):
        calls["infer"] += 1
        if calls["infer"] == 1:
            raise RuntimeError("CUDA out of memory: tried to allocate 2.00 GiB")
        time.sleep(INFER_SLEEP)
        return [FakeSeg(0.0, 2.0, "OOM 回退文本。")], FakeInfo()

    out = os.path.join(tmp, "fb_infer.json")
    code, err = run_main(["--wav", WAV, "--model", MODEL, "--out", out], build, infer)
    d = json.load(open(out, encoding="utf-8"))
    ok(code == 0, "fb_infer: exit 0, got %s (%s)" % (code, err[:80]))
    ok(d["device"] == "cpu", "fb_infer: cpu recorded")
    ok("out of memory" in (d["fallback"] or ""), "fb_infer: OOM kept as reason")
    ok(d["fallback_load_s"] is not None and d["fallback_load_s"] >= LOAD_MIN,
       "fb_infer: fallback_load_s = GPU load, got %s" % d["fallback_load_s"])
    ok(d["asr_s"] <= ASR_MAX, "fb_infer: asr_s is CPU inference only, got %s" % d["asr_s"])
    ok(calls["infer"] == 2, "fb_infer: two inference attempts")


def case_no_fallback_on_model_error(tmp):
    """A broken model file is not a GPU problem: no retry, no bogus JSON."""
    calls = {"load": 0}

    def build(model_dir, device, compute, threads):
        calls["load"] += 1
        raise RuntimeError("Unsupported model binary version. binary version v544501614")

    def infer(model, wav, a, prompt):  # pragma: no cover - must not run
        raise AssertionError("inference must not start")

    out = os.path.join(tmp, "bad_model.json")
    code, err = run_main(["--wav", WAV, "--model", MODEL, "--out", out], build, infer)
    ok(code == 3, "bad_model: exit 3, got %s" % code)
    ok(calls["load"] == 1, "bad_model: no CPU retry, got %d builds" % calls["load"])
    ok(not os.path.exists(out), "bad_model: no output file written")
    ok("Unsupported model binary version" in err, "bad_model: real error shown")


def case_no_fallback_flag(tmp):
    """--no-fallback turns a CUDA error into a hard failure."""
    calls = {"load": 0}

    def build(model_dir, device, compute, threads):
        calls["load"] += 1
        raise RuntimeError("CUDA driver version is insufficient for CUDA runtime version")

    def infer(model, wav, a, prompt):  # pragma: no cover
        raise AssertionError("inference must not start")

    out = os.path.join(tmp, "nf.json")
    code, err = run_main(["--wav", WAV, "--model", MODEL, "--out", out,
                          "--no-fallback"], build, infer)
    ok(code == 3, "no_fallback: exit 3, got %s" % code)
    ok(calls["load"] == 1, "no_fallback: single attempt, got %d" % calls["load"])
    ok(not os.path.exists(out), "no_fallback: no output file")


def case_both_fail(tmp):
    """When CPU also fails, both errors must survive in the message."""
    def build(model_dir, device, compute, threads):
        if device == "cuda":
            raise RuntimeError("CUDA failed with error unknown error")
        raise RuntimeError("model.bin is truncated")

    def infer(model, wav, a, prompt):  # pragma: no cover
        raise AssertionError("inference must not start")

    out = os.path.join(tmp, "both.json")
    code, err = run_main(["--wav", WAV, "--model", MODEL, "--out", out], build, infer)
    ok(code == 4, "both: exit 4, got %s" % code)
    ok("GPU:" in err and "CPU:" in err, "both: both sides reported")
    ok("CUDA failed" in err and "truncated" in err, "both: both messages kept")


def case_bad_wav(tmp):
    """An unreadable WAV must exit 2 with a message, not a traceback."""
    bad = os.path.join(tmp, "bad.wav")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("not a wav")

    def build(*a, **k):  # pragma: no cover
        raise AssertionError("must fail before model load")

    def infer(*a, **k):  # pragma: no cover
        raise AssertionError("must fail before inference")

    out = os.path.join(tmp, "bad_wav.json")
    code, err = run_main(["--wav", bad, "--model", MODEL, "--out", out], build, infer)
    ok(code == 2, "bad_wav: exit 2, got %s" % code)
    ok("cannot read WAV" in err, "bad_wav: readable message")
    ok("Traceback" not in err, "bad_wav: no traceback")


def case_dll_handles():
    """The DLL directory handles must stay alive after prepare_cuda_dlls()."""
    dirs = list(tr.cuda_dll_dirs())
    if not dirs:
        print("  (skip) no nvidia pip DLL dirs on this machine")
        return
    before = len(tr._DLL_HANDLES)
    got = tr.prepare_cuda_dlls()
    ok(len(tr._DLL_HANDLES) == before + len(got),
       "dll: every directory produced a live handle")
    ok(len(got) == len(dirs), "dll: all dirs reported")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="tr_unit_") as tmp:
        WAV = os.path.join(tmp, "a.wav")
        make_wav(WAV)
        MODEL = os.path.join(tmp, "model")
        os.makedirs(MODEL, exist_ok=True)
        open(os.path.join(MODEL, "model.bin"), "wb").close()

        case_normal(tmp)
        case_gpu_load_failure(tmp)
        case_gpu_infer_failure(tmp)
        case_no_fallback_on_model_error(tmp)
        case_no_fallback_flag(tmp)
        case_both_fail(tmp)
        case_bad_wav(tmp)
        case_dll_handles()

    print()
    print("%d assertions, %d failures." % (checks[0], len(failures)))
    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("All assertions passed. No network / model weight / GPU required.")
