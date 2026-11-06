# -*- coding: utf-8 -*-
"""Offline unit tests for scripts/batch_transcribe.py.

Covers the parts a real run cannot cheaply exercise: key derivation and
collision handling, the "is this file already done" rule, the skip-forever trap
after a failed --force, the command it hands to transcribe.py, and the summary
shape/encoding. No model, no GPU, no network, no ffmpeg.

The CLI is exercised through --dry-run, which is the only path that needs
neither a model nor a real audio file.

Run:  python tests/run_batch_unit.py
"""
import importlib.util
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

    touch(p, {"duration": 5.0, "segments": [], "text": "", "error": None})
    ok(bt.is_done(p)[0], "silent file with empty segments counts as done")

    touch(p, GOOD)
    ok(bt.is_done(p)[0], "well-formed output -> done")


def case_prev_rotation(tmp):
    p = os.path.join(tmp, "r.json")
    touch(p, GOOD)
    bt.stage_aside(p)
    ok(not os.path.exists(p) and os.path.exists(p + ".prev"),
       "stage_aside moves the old output away")
    # While .prev exists and r.json does not, the file is retried - not skipped.
    ok(not bt.is_done(p)[0], "staged file is not 'done'")
    bt.drop_prev(p)
    ok(not os.path.exists(p + ".prev"), "drop_prev removes the backup")

    if os.path.exists(p):
        os.remove(p)
    bt.stage_aside(p)          # nothing to move: must not raise
    ok(not os.path.exists(p), "stage_aside is a no-op when there is no output")


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
    r = run_cli(["--input", src, "--out-dir", out, "--dry-run"], tmp)
    ok("1 to run, 1 already done" in r.stdout, "existing output is skipped")

    r = run_cli(["--input", src, "--out-dir", out, "--dry-run", "--force"], tmp)
    ok("2 to run, 0 already done" in r.stdout, "--force ignores existing output")

    r = run_cli(["--input", src, "--out-dir", out], tmp)
    eq(r.returncode, 2, "missing --model is a usage error")

    empty = os.path.join(tmp, "empty")
    os.makedirs(empty, exist_ok=True)
    r = run_cli(["--input", empty, "--out-dir", out, "--dry-run"], tmp)
    eq(r.returncode, 2, "no match exits 2")
    ok("[ERROR] no file matched" in r.stdout, "no match explains itself")


def main():
    print("batch_transcribe.py - offline unit tests")
    print("=" * 62)
    case_clean_key()
    case_key_for()
    case_build_cmd()
    with tempfile.TemporaryDirectory() as tmp:
        case_walk_and_collect(tmp)
        case_collisions(tmp)
        case_is_done(tmp)
        case_prev_rotation(tmp)
        case_summary(tmp)
        case_cli(tmp)

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
