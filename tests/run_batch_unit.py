# -*- coding: utf-8 -*-
"""Offline unit + integration tests for scripts/batch_transcribe.py.

Covers the parts a real run cannot cheaply exercise: key derivation and
collision handling, the "is this pair of outputs done" rule, the skip-forever
trap after a failed --force, the .md half of every output, the command it hands
to transcribe.py, and the summary shape/encoding.

The integration cases drive main() end to end with a *fake* transcribe.py
(monkeypatched into the module), so the loop, the skip rule, failure isolation
and --force staging are all exercised without a model, GPU, network or ffmpeg.
The fake writes whatever the test asks for - including a JSON with no .md,
which is exactly the half-deliverable a killed real run leaves behind.

Run:  python tests/run_batch_unit.py
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BATCH = os.path.join(ROOT, "scripts", "batch_transcribe.py")
SPEC = importlib.util.spec_from_file_location("batch_module", BATCH)
bt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bt)

checks = [0]
failures = []


def ok(cond, label):
    checks[0] += 1
    if not cond:
        failures.append(label)


def eq(got, want, label):
    ok(got == want, "%s: got %r want %r" % (label, got, want))


def touch(path, payload=None):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        if payload is None:
            fh.write("x")
        elif isinstance(payload, str):
            fh.write(payload)
        else:
            json.dump(payload, fh, ensure_ascii=False)


GOOD = {"duration": 12.5, "segments": [{"s": 0.0, "e": 12.5, "t": "你好"}],
        "text": "你好", "error": None, "device": "cuda", "rtf_x": 18.0,
        "load_s": 1.1, "asr_s": 0.7, "fallback": None}


# ------------------------------------------------------------------ keys ---

def case_clean_key():
    eq(bt.clean_key("01"), "01", "plain stem unchanged")
    eq(bt.clean_key("sub/02"), "sub__02", "slash becomes __")
    eq(bt.clean_key("sub\\02"), "sub__02", "backslash becomes __")
    eq(bt.clean_key('a:b*c?d"e<f>g|h'), "a_b_c_d_e_f_g_h", "illegal chars replaced")
    eq(bt.clean_key("   "), "clip", "blank falls back to clip")
    eq(bt.clean_key("会话 01"), "会话 01", "chinese and spaces preserved")
    eq(bt.clean_key("name."), "name", "trailing dot stripped")
    ok(":" not in bt.clean_key('x:y'), "no colon survives (windows filename)")


def case_key_for():
    root = os.path.join(tempfile.gettempdir(), "broot")
    os.makedirs(root, exist_ok=True)          # a real dir, so relative paths apply
    eq(bt.key_for(os.path.join(root, "a.wav"), root), "a", "single level")
    eq(bt.key_for(os.path.join(root, "d", "a.wav"), root), "d__a", "nested")
    one = os.path.join(root, "solo.wav")
    eq(bt.key_for(one, one), "solo", "explicit file uses its own stem")
    shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------- walking ---

def case_walk_and_collect(tmp):
    src = os.path.join(tmp, "in")
    touch(os.path.join(src, "01.wav"))
    touch(os.path.join(src, "02.WAV"))                 # case-insensitive
    touch(os.path.join(src, "03.mp3"))
    touch(os.path.join(src, "sub", "04.wav"))

    flat = bt.walk_files(src, ["*.wav"], False)
    eq(len(flat), 2, "non-recursive finds 2 wav (upper-case .WAV included)")
    deep = bt.walk_files(src, ["*.wav"], True)
    eq(len(deep), 3, "recursive finds 3")

    items = bt.collect([src], ["*.wav"], False, os.path.join(tmp, "out"))
    eq([os.path.basename(i["src"]) for i in items], ["01.wav", "02.WAV"],
       "flat plan sorted by name")
    eq([i["key"] for i in items], ["01", "02"], "keys without extension")

    whole = bt.collect([src], ["*.wav"], True, os.path.join(tmp, "out"))
    eq(sorted(i["key"] for i in whole), ["01", "02", "sub__04"],
       "nested key keeps its folder")

    # Same file reachable twice -> one entry.
    twice = bt.collect([src, os.path.join(src, "sub")], ["*.wav"], True,
                       os.path.join(tmp, "out"))
    eq(len(twice), 3, "de-duplicated when reached through two inputs")

    # The out dir lives inside the input here: it must not be re-ingested.
    out_in = os.path.join(src, "out")
    touch(os.path.join(out_in, "01.wav"))
    after = bt.collect([src], ["*.wav"], True, out_in)
    ok(all("out" not in i["src"] for i in after), "out-dir contents are ignored")

    # Explicit files bypass the pattern.
    one = bt.collect([os.path.join(src, "03.mp3")], ["*.wav"], False,
                     os.path.join(tmp, "out"))
    eq([i["key"] for i in one], ["03"], "explicit file is accepted regardless of pattern")


def case_collisions(tmp):
    a, b = os.path.join(tmp, "A"), os.path.join(tmp, "B")
    touch(os.path.join(a, "01.wav"))
    touch(os.path.join(b, "01.wav"))
    items = bt.collect([a, b], ["*.wav"], False, os.path.join(tmp, "out"))
    keys = sorted(i["key"] for i in items)
    eq(keys, ["A__01", "B__01"], "same name in two roots gets a folder prefix")

    # Same folder name in two places: still unique.
    c = os.path.join(tmp, "x", "same")
    d = os.path.join(tmp, "y", "same")
    touch(os.path.join(c, "01.wav"))
    touch(os.path.join(d, "01.wav"))
    items = bt.collect([c, d], ["*.wav"], False, os.path.join(tmp, "out"))
    keys = [i["key"] for i in items]
    eq(len(keys), len(set(keys)), "identical folder names still unique")
    ok(all(k.startswith("same__01") for k in keys), "suffix keeps the readable part")


# ------------------------------------------------------------- done rule ---

def case_is_done(tmp):
    p = os.path.join(tmp, "d.json")
    m = os.path.join(tmp, "d.md")
    ok(not bt.is_done(p)[0], "absent -> not done")

    open(p, "w").close()
    ok(not bt.is_done(p)[0], "empty file -> not done")

    touch(p, '{"duration": 3.0, "segments": [')
    done, why = bt.is_done(p)
    ok(not done and "unreadable" in why, "truncated json -> not done")

    touch(p, {"duration": 3.0, "segments": [], "error": "boom"})
    ok(not bt.is_done(p)[0], "recorded error -> not done")

    touch(p, {"text": "hello"})
    ok(not bt.is_done(p)[0], "no segments/duration -> not done")

    touch(p, [1, 2, 3])
    ok(not bt.is_done(p)[0], "not an object -> not done")

    # The pair rule: a valid JSON with no transcript beside it is half a result.
    touch(p, {"duration": 5.0, "segments": [], "text": "", "error": None})
    done, why = bt.is_done(p)
    ok(not done, "json without md -> not done")
    ok("markdown" in why, "missing md is named in the reason, got %r" % why)

    touch(m, "")
    ok(not bt.is_done(p)[0], "empty md -> not done")

    touch(m, "# 会话 01\n")
    ok(bt.is_done(p)[0], "json + non-empty md -> done")

    touch(p, GOOD)
    ok(bt.is_done(p)[0], "well-formed pair -> done")
    ok(bt.is_done(p, m)[0], "explicit md path accepted")
    ok(not bt.is_done(p, os.path.join(tmp, "nope.md"))[0],
       "explicit md path is honoured, not derived")


def case_prev_rotation(tmp):
    p = os.path.join(tmp, "r.json")
    m = os.path.join(tmp, "r.md")
    touch(p, GOOD)
    touch(m, "# 旧稿\n")
    bt.stage_aside(p)
    ok(not os.path.exists(p) and os.path.exists(p + ".prev"),
       "stage_aside moves the old json away")
    ok(not os.path.exists(m) and os.path.exists(m + ".prev"),
       "stage_aside moves the old md too (the human-readable half)")
    # While .prev exists and r.json does not, the file is retried - not skipped.
    ok(not bt.is_done(p)[0], "staged file is not 'done'")
    bt.drop_prev(p)
    ok(not os.path.exists(p + ".prev") and not os.path.exists(m + ".prev"),
       "drop_prev removes both backups")

    if os.path.exists(p):
        os.remove(p)
    eq(bt.stage_aside(p), None, "staging nothing is not an error")
    ok(not os.path.exists(p), "stage_aside is a no-op when there is no output")


def case_stage_aside_failure(tmp):
    # Fail exactly the second rename: JSON staged, .md backup refused. The
    # first half has already moved and must be rolled back.
    real_replace = bt.os.replace

    def flaky(src, dst, **kw):
        if str(dst).endswith(".md.prev"):
            raise OSError("simulated: .md backup refused")
        return real_replace(src, dst, **kw)

    p = os.path.join(tmp, "s.json")
    m = os.path.join(tmp, "s.md")
    touch(p, GOOD)
    touch(m, "# 旧稿\n")
    bt.os.replace = flaky
    try:
        err = bt.stage_aside(p)
    finally:
        bt.os.replace = real_replace
    ok(err and "could not stage" in err, "a failed backup is reported, not swallowed")
    ok("s.md" in (err or ""), "the error names the file that failed")
    ok(os.path.exists(p), "the already-moved JSON is rolled back")
    with open(p, encoding="utf-8") as fh:
        eq(json.load(fh), GOOD,
           "the rolled-back JSON still has its original content")
    ok(os.path.exists(m), "the .md was never moved")
    ok(not os.path.exists(p + ".prev"), "no half-staged .prev is left behind")

    # run_one must stop before invoking transcribe.py when staging fails.
    wav = os.path.join(tmp, "in.wav")
    touch(wav, "RIFF")
    out = os.path.join(tmp, "out2")
    os.makedirs(out)
    q = os.path.join(out, "q.json")
    touch(q, GOOD)
    touch(os.path.join(out, "q.md"), "# 旧稿\n")
    opts = {"out_dir": out, "timeout": 10, "force": True,
            "python": sys.executable, "script": "never-called.py",
            "model": "M", "device": "cpu", "compute": None, "language": "zh"}
    real_run, called = bt.subprocess.run, []

    def spy(cmd, **kw):
        called.append(cmd)
        return bt.subprocess.CompletedProcess(cmd, 0, b"", b"")

    bt.os.replace, bt.subprocess.run = flaky, spy
    try:
        rec = bt.run_one(wav, q, "q", opts)
    finally:
        bt.os.replace, bt.subprocess.run = real_replace, real_run
    eq(rec["status"], "failed", "a staging failure fails the task")
    ok("stage" in rec["error"], "the failure reason reaches the summary")
    eq(called, [], "transcribe.py is never started after a staging failure")
    ok(os.path.exists(q), "the old transcript is still in place")


def case_needs_extract():
    ok(bt.needs_extract("a.mp4"), "mp4 has to be decoded")
    ok(bt.needs_extract("a.MOV"), "the extension check is case-insensitive")
    ok(bt.needs_extract("a.m4a"), "m4a has to be decoded")
    ok(bt.needs_extract("a.flac"), "flac has to be decoded")
    ok(not bt.needs_extract("a.wav"), "wav is passed through untouched")
    ok(not bt.needs_extract("a.WAV"), "uppercase .WAV is still a wav")
    ok(not bt.needs_extract("noext"), "an unknown extension is left to transcribe.py")
    items = [{"src": "a.wav"}, {"src": "b.WAV"}, {"src": "c.m4a"}, {"src": "d.mp3"}]
    eq([os.path.basename(x) for x in bt.planned_extract(items)], ["c.m4a", "d.mp3"],
       "planned_extract lists only the inputs that will be decoded")
    eq(bt.planned_extract([]), [], "an empty plan has nothing to decode")


def case_extract_audio(tmp):
    import wave as _wave

    def make_wav(p, rate=16000, channels=1, width=2, seconds=0.25):
        with _wave.open(p, "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(width)
            w.setframerate(rate)
            w.writeframes(b"\0" * int(seconds * rate) * width * channels)

    good = os.path.join(tmp, "good.wav")
    make_wav(good)
    with open(good, "rb") as fh:
        data = fh.read()
    eq(bt.wav_ok(good), None, "a complete 16 kHz mono WAV passes validation")

    junk = os.path.join(tmp, "junk.wav")
    touch(junk, "R" * 100)
    ok(bt.wav_ok(junk), "a >44-byte non-WAV is not a valid cache")
    slow = os.path.join(tmp, "slow.wav")
    make_wav(slow, rate=8000)
    ok("16000" in (bt.wav_ok(slow) or ""), "a wrong sample rate is refused")
    stereo = os.path.join(tmp, "stereo.wav")
    make_wav(stereo, channels=2)
    ok("channels" in (bt.wav_ok(stereo) or ""), "a wrong channel count is refused")
    trunc = os.path.join(tmp, "trunc.wav")
    with open(trunc, "wb") as fh:
        fh.write(data[:len(data) // 2])
    ok("truncated" in (bt.wav_ok(trunc) or ""),
       "a WAV whose data chunk ends early is refused")

    # A WAV input never touches ffmpeg, so the path comes straight back.
    wav = os.path.join(tmp, "in.wav")
    touch(wav, "RIFF")
    eq(bt.extract_audio(wav, "in", os.path.join(tmp, "out"), 10), (wav, None),
       "a WAV is returned untouched")

    out = os.path.join(tmp, "out")
    real_ffmpeg, real_run = bt.FFMPEG, bt.subprocess.run
    try:
        # No ffmpeg at all: a VALID cache is reused, a missing one fails loudly.
        bt.FFMPEG = "definitely-not-ffmpeg-xyz"
        cached = bt.audio_cache_path(out, "cached")
        os.makedirs(os.path.dirname(cached), exist_ok=True)
        shutil.copyfile(good, cached)
        eq(bt.extract_audio(os.path.join(tmp, "x.mp4"), "cached", out, 10),
           (cached, None), "a valid existing decode is reused")

        got, err = bt.extract_audio(os.path.join(tmp, "y.mp4"), "fresh", out, 10)
        eq(got, None, "no ffmpeg means no output path")
        ok("ffmpeg not found" in (err or ""), "the error says ffmpeg is missing")
        ok("y.mp4" in (err or ""), "the error names the input")

        def ok_run(cmd, **kw):
            shutil.copyfile(good, cmd[-1])
            return bt.subprocess.CompletedProcess(cmd, 0, b"", b"")

        def parts(d):
            return [n for n in os.listdir(d) if ".part-" in n]

        # A corrupt cache must be re-decoded, not trusted because it is big.
        broken = bt.audio_cache_path(out, "broken")
        with open(broken, "wb") as fh:
            fh.write(b"R" * 4096)
        bt.subprocess.run = ok_run
        got, err = bt.extract_audio(os.path.join(tmp, "z.mp4"), "broken", out, 10)
        eq(got, broken, "a corrupt cache triggers a fresh decode")
        eq(err, None, "and the fresh decode succeeds")
        eq(bt.wav_ok(broken), None, "the cache is now a valid WAV")
        ok(not parts(os.path.dirname(broken)), "no .part file survives success")

        # ffmpeg fails after writing a half file: nothing reusable may remain.
        def fail_run(cmd, **kw):
            with open(cmd[-1], "wb") as fh:
                fh.write(data[:len(data) // 2])
            return bt.subprocess.CompletedProcess(cmd, 1, b"", b"decode error")

        bt.subprocess.run = fail_run
        got, err = bt.extract_audio(os.path.join(tmp, "a.mp4"), "fail", out, 10)
        eq(got, None, "a failed decode returns no path")
        ok("ffmpeg failed" in (err or ""), "the error reports ffmpeg's failure")
        ok(not os.path.exists(bt.audio_cache_path(out, "fail")),
           "no cache is published after a failed decode")
        ok(not parts(os.path.dirname(broken)), "the .part file is cleaned up")

        # Timeout is the most likely way to leave a large truncated WAV behind.
        def timeout_run(cmd, **kw):
            with open(cmd[-1], "wb") as fh:
                fh.write(b"R" * 10000)
            raise bt.subprocess.TimeoutExpired(cmd, 10)

        bt.subprocess.run = timeout_run
        got, err = bt.extract_audio(os.path.join(tmp, "b.mp4"), "slow", out, 10)
        eq(got, None, "a timed-out decode returns no path")
        ok("timed out" in (err or ""), "the error says it timed out")
        ok(not os.path.exists(bt.audio_cache_path(out, "slow")),
           "no cache is published after a timeout")
        ok(not parts(os.path.dirname(broken)), "the .part file is cleaned up")

        # ffmpeg says success but writes garbage: validation must catch it.
        def liar_run(cmd, **kw):
            shutil.copyfile(trunc, cmd[-1])
            return bt.subprocess.CompletedProcess(cmd, 0, b"", b"")

        bt.subprocess.run = liar_run
        got, err = bt.extract_audio(os.path.join(tmp, "c.mp4"), "liar", out, 10)
        eq(got, None, "exit 0 with a truncated WAV is still a failure")
        ok("incomplete WAV" in (err or ""), "the error names the bad output")
        ok(not os.path.exists(bt.audio_cache_path(out, "liar")),
           "and no truncated cache is published")
    finally:
        bt.FFMPEG = real_ffmpeg
        bt.subprocess.run = real_run


# ---------------------------------------------------------------- command ---

def case_build_cmd():
    opts = {"model": "M", "device": "cuda", "compute": None, "language": "zh"}
    cmd = bt.build_cmd("PY", "SCRIPT", "in.wav", "out.json", "k1", opts)
    eq(cmd[0], "PY", "python comes first")
    eq(cmd[1:3], ["-X", "utf8"], "-X utf8 is explicit, never inherited")
    ok("--compute" not in cmd, "compute omitted when not set")
    eq(cmd[cmd.index("--tag") + 1], "k1", "tag is the output key")
    eq(cmd[cmd.index("--wav") + 1], "in.wav", "wav passed through")

    opts["compute"] = "int8"
    cmd = bt.build_cmd("PY", "SCRIPT", "in.wav", "out.json", "k1", opts)
    eq(cmd[cmd.index("--compute") + 1], "int8", "compute passed when set")

    env = bt.child_env()
    eq(env.get("PYTHONUTF8"), "1", "child gets PYTHONUTF8")
    eq(env.get("PYTHONIOENCODING"), "utf-8", "child gets PYTHONIOENCODING")


# ---------------------------------------------------------------- summary ---

def case_summary(tmp):
    recs = [
        {"src": "a.wav", "key": "a", "out_json": os.path.join(tmp, "a.json"),
         "status": "ok", "exit_code": 0, "wall_s": 2.0, "audio_s": 10.0,
         "device": "cuda", "rtf_x": 18.0, "fallback": False, "error": ""},
        {"src": "b.wav", "key": "b", "out_json": os.path.join(tmp, "b.json"),
         "status": "skipped", "exit_code": None, "wall_s": 0.0, "error": ""},
        {"src": "c.wav", "key": "c", "out_json": os.path.join(tmp, "c.json"),
         "status": "failed", "exit_code": 3, "wall_s": 1.0,
         "error": "CUDA error"},
    ]
    eq(bt.counts_of(recs),
       {"total": 3, "ok": 1, "skipped": 1, "failed": 1, "timeout": 0},
       "counts")

    meta = {"started_at": "t0", "finished_at": "t1", "model": "turbo",
            "device": "cuda", "compute": None, "language": "zh",
            "pattern": ["*.wav"], "recursive": False, "out_dir": tmp}
    data = bt.payload(recs, meta, True, 3.5)
    eq(data["counts"]["failed"], 1, "payload carries counts")
    eq(len(data["results"]), 3, "payload carries every file")
    eq(data["results"][0]["out_json"], "a.json", "out_json is relative to out_dir")
    ok("src" in data["results"][1], "skipped entries keep their source")
    eq(data["finished"], True, "finished flag")

    path = os.path.join(tmp, "_batch", "summary.json")
    bt.write_json_atomic(path, data)
    raw = open(path, "rb").read()
    ok(raw.startswith(b"{"), "summary starts with a brace")
    try:
        json.loads(raw.decode("utf-8"))      # cp936 output would fail here
        ok(True, "summary is valid utf-8 json")
    except Exception as exc:
        ok(False, "summary is valid utf-8 json (%s)" % exc)
    ok(not os.path.exists(path + ".tmp"), "no temp file left behind")
    # Chinese must survive the round trip (utf-8, not cp936).
    data["results"][0]["error"] = "错误"
    bt.write_json_atomic(path, data)
    ok("错误" in open(path, encoding="utf-8").read(), "chinese written as utf-8")


# -------------------------------------------------------------------- cli ---

def run_cli(args, cwd):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-X", "utf8", BATCH] + args,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=cwd, env=env)


def case_cli(tmp):
    src = os.path.join(tmp, "cli_in")
    touch(os.path.join(src, "01.wav"))
    touch(os.path.join(src, "02.wav"))
    out = os.path.join(tmp, "cli_out")

    r = run_cli(["--input", src, "--out-dir", out, "--dry-run"], tmp)
    eq(r.returncode, 0, "dry-run exits 0 without --model")
    ok("2 to run" in r.stdout, "dry-run reports the plan")
    ok("dry run: nothing executed" in r.stdout, "dry-run says so")
    ok(not os.path.exists(out), "dry-run creates nothing")

    # Pretend one file is already done -> it must be skipped, the other planned.
    bt.write_json_atomic(os.path.join(out, "01.json"), GOOD)
    touch(os.path.join(out, "01.md"), "# 01\n")
    r = run_cli(["--input", src, "--out-dir", out, "--dry-run"], tmp)
    ok("1 to run, 1 already done" in r.stdout, "existing pair is skipped")

    # Same JSON, no md -> half a deliverable, so it must be planned again.
    os.remove(os.path.join(out, "01.md"))
    r = run_cli(["--input", src, "--out-dir", out, "--dry-run"], tmp)
    ok("2 to run, 0 already done" in r.stdout,
       "json without md is planned again, not skipped")

    r = run_cli(["--input", src, "--out-dir", out, "--dry-run", "--force"], tmp)
    ok("2 to run, 0 already done" in r.stdout, "--force ignores existing output")

    r = run_cli(["--input", src, "--out-dir", out], tmp)
    eq(r.returncode, 2, "missing --model is a usage error")

    empty = os.path.join(tmp, "empty")
    os.makedirs(empty, exist_ok=True)
    r = run_cli(["--input", empty, "--out-dir", out, "--dry-run"], tmp)
    eq(r.returncode, 2, "no match exits 2")
    ok("[ERROR] no file matched" in r.stdout, "no match explains itself")


# ------------------------------------------------------------ integration ---

# Stands in for transcribe.py. The content of the "wav" is the instruction, so
# a test can flip a single file from ok -> fail by rewriting four bytes.
FAKE_TRANSCRIBE = '''# -*- coding: utf-8 -*-
import argparse, json, os, sys
ap = argparse.ArgumentParser()
for flag in ("--wav", "--model", "--out", "--tag", "--device", "--compute",
             "--language"):
    ap.add_argument(flag)
a = ap.parse_args()
mode = open(a.wav, encoding="utf-8").read().strip()
if mode == "fail":
    sys.stderr.write("CUDA error: no CUDA-capable device is detected\\n")
    sys.exit(3)
if mode == "jsononly":
    json.dump({"duration": 4.0, "segments": [], "text": "", "error": None},
              open(a.out, "w", encoding="utf-8"))
    sys.exit(0)
json.dump({"duration": 4.0, "segments": [{"s": 0.0, "e": 4.0, "t": "你好"}],
           "text": "你好", "error": None, "device": "cuda", "rtf_x": 9.0,
           "load_s": 1.0, "asr_s": 0.4, "fallback": None},
          open(a.out, "w", encoding="utf-8"))
open(os.path.splitext(a.out)[0] + ".md", "w", encoding="utf-8").write("# t\\n")
sys.exit(0)
'''


def run_main(argv, script):
    """Call bt.main() in-process; returns (exit_code, captured stdout)."""
    old_argv, old_script, old_out = sys.argv, bt.TRANSCRIBE, sys.stdout
    buf = io.StringIO()
    code = 0
    bt.TRANSCRIBE = script
    sys.argv = ["batch_transcribe.py"] + argv
    try:
        sys.stdout = buf
        try:
            bt.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    finally:
        sys.stdout = old_out
        sys.argv = old_argv
        bt.TRANSCRIBE = old_script
    return code, buf.getvalue()


def case_integration(tmp):
    fake = os.path.join(tmp, "fake_transcribe.py")
    with open(fake, "w", encoding="utf-8") as fh:
        fh.write(FAKE_TRANSCRIBE)
    src = os.path.join(tmp, "batch_in")
    touch(os.path.join(src, "01.wav"), "ok")
    touch(os.path.join(src, "02bad.wav"), "fail")
    touch(os.path.join(src, "03.wav"), "ok")
    out = os.path.join(tmp, "batch_out")
    run = lambda extra=None: run_main(
        ["--input", src, "--out-dir", out, "--model", "M"] + (extra or []), fake)

    # 1) ok / fail / ok - the failure must not stop the batch.
    code, log = run()
    eq(code, 1, "one failed file -> exit 1")
    ok(os.path.exists(os.path.join(out, "01.md")),
       "successful file gets its md")
    ok(os.path.exists(os.path.join(out, "03.json")) and
       os.path.exists(os.path.join(out, "03.md")),
       "the file after the failure still ran")
    ok(not os.path.exists(os.path.join(out, "02bad.json")),
       "failed file leaves no json")
    ok("FAILED" in log and "no CUDA-capable device" in log,
       "failure reason surfaces in the log")
    summary = json.load(open(os.path.join(out, "_batch", "summary.json"),
                             encoding="utf-8"))
    eq(summary["counts"],
       {"total": 3, "ok": 2, "skipped": 0, "failed": 1, "timeout": 0},
       "summary counts after a partial batch")
    eq(summary["finished"], True, "summary marked finished")

    # 2) second run: finished pairs are skipped, only the failure retries.
    code, log = run()
    eq(code, 1, "unfixed failure still exits 1")
    ok("1 to run, 2 already done" in log, "finished pairs are skipped on re-run")

    # 3) fix the bad file -> only it runs, and the batch is clean.
    touch(os.path.join(src, "02bad.wav"), "ok")
    code, log = run()
    eq(code, 0, "after the fix the batch exits 0")
    ok("1 to run, 2 already done" in log, "only the fixed file is retried")
    ok(os.path.exists(os.path.join(out, "02bad.md")), "fixed file gets its md")

    # 4) JSON written, MD never written: half a deliverable, never "done".
    src2 = os.path.join(tmp, "batch_in2")
    touch(os.path.join(src2, "j.wav"), "jsononly")
    out2 = os.path.join(tmp, "batch_out2")
    run2 = lambda: run_main(["--input", src2, "--out-dir", out2,
                             "--model", "M"], fake)
    code, log = run2()
    eq(code, 1, "json-only output counts as a failure, not ok")
    ok("output is incomplete" in log, "reason names the incomplete pair")
    ok(os.path.exists(os.path.join(out2, "j.json")), "the orphan json is kept")
    code, log = run2()
    ok("1 to run, 0 already done" in log,
       "json without md is retried, never skipped")

    # 5) --force that fails must move BOTH halves aside: a stale .md left at
    #    the live path is the one thing a reader would mistake for this run.
    touch(os.path.join(src, "01.wav"), "fail")
    code, log = run(["--force"])
    eq(code, 1, "--force with a failure exits 1")
    ok(not os.path.exists(os.path.join(out, "01.json")),
       "no live json after a failed re-run")
    ok(not os.path.exists(os.path.join(out, "01.md")),
       "no live md after a failed re-run (stale transcript is not left behind)")
    ok(os.path.exists(os.path.join(out, "01.json.prev")), "old json staged")
    ok(os.path.exists(os.path.join(out, "01.md.prev")), "old md staged")

    # 6) a staged file is retried, and success clears both backups.
    touch(os.path.join(src, "01.wav"), "ok")
    code, log = run()
    eq(code, 0, "retry after staging succeeds")
    ok("1 to run, 2 already done" in log, "the staged file is retried")
    ok(not os.path.exists(os.path.join(out, "01.json.prev")),
       "json backup dropped on success")
    ok(not os.path.exists(os.path.join(out, "01.md.prev")),
       "md backup dropped on success")

    # 7) nothing left to do.
    code, log = run()
    eq(code, 0, "a fully finished batch still exits 0")
    ok("0 to run, 3 already done" in log, "everything is done at the end")


def case_decode_notice(tmp):
    fake = os.path.join(tmp, "fake_transcribe.py")
    with open(fake, "w", encoding="utf-8") as fh:
        fh.write(FAKE_TRANSCRIBE)
    src = os.path.join(tmp, "mix")
    touch(os.path.join(src, "a.wav"), "ok")
    touch(os.path.join(src, "b.m4a"), "ok")
    code, log = run_main(["--input", src, "--out-dir", os.path.join(tmp, "mo"),
                          "--pattern", "*.wav", "*.m4a", "--dry-run"], fake)
    eq(code, 0, "dry-run still exits 0")
    ok("[INFO]" in log, "a non-WAV match is announced, not rejected")
    ok("b.m4a" in log, "the notice names the file")
    ok("[decode]" in log, "the per-file plan marks what will be decoded")
    ok("1 of 2" in log, "the notice counts them")


def main():
    print("batch_transcribe.py - offline unit tests")
    print("=" * 62)
    case_clean_key()
    case_key_for()
    case_build_cmd()
    case_needs_extract()
    with tempfile.TemporaryDirectory() as tmp:
        case_walk_and_collect(tmp)
        case_collisions(tmp)
        case_is_done(tmp)
        case_prev_rotation(tmp)
        case_stage_aside_failure(tmp)
        case_summary(tmp)
        case_cli(tmp)
        case_integration(tmp)
        case_extract_audio(tmp)
        case_decode_notice(tmp)

    print()
    print("%d assertions, %d failures." % (checks[0], len(failures)))
    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("All assertions passed. No model / GPU / network / ffmpeg required.")


if __name__ == "__main__":
    main()
