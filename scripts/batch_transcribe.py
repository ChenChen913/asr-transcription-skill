#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Serial batch transcription over one or more folders.

Four properties, and deliberately nothing else (see ROADMAP C2):

  1. serial     - one file at a time, one model instance, no workers, no queue
  2. skip done  - a file is done when its output *pair* is complete: the JSON
                  is readable and carries segments+duration, and the .md next to
                  it exists and is not empty. --force ignores that
  3. isolation  - one bad file never stops the batch; each failure is recorded
                  with its exit code and the tail of its error
  4. per file   - each input gets its own JSON + MD, produced by
                  scripts/transcribe.py (identical schema to a single run)

No database, no lock file, no retry scheduler. **The output pair is the state**:
one source of truth, so a half-finished batch cannot drift out of sync with a
separate progress file.

Layout of --out-dir:

    <key>.json / <key>.md      one transcript per input, always as a pair
    <key>.json.prev            previous version of the pair, only while a
    <key>.md.prev              re-run is in flight or if that re-run failed
                               (never counts as done, so a failure is retried
                               next time)
    _batch/summary.json        counts + per-file outcome, rewritten after every
                               file so a killed batch still leaves a record

transcribe.py reads PCM WAV only. Any other container (.mp4/.mov/.mkv/.m4a/
.mp3/.flac/...) is decoded first with ffmpeg into <out-dir>/_batch/audio/<key>.wav
and reused on retry, so one ffmpeg pass per input, not per run. A WAV input is
passed through untouched - ffmpeg is only required when a non-WAV is matched.

Exit codes: 0 all ok/skipped, 1 at least one failed or timed out, 2 bad usage or
no input matched - same spirit as qc_check.py, so a batch can gate CI.
"""

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
TRANSCRIBE = os.path.join(HERE, "transcribe.py")
SUMMARY_DIR = "_batch"
SUMMARY_NAME = "summary.json"
BAD_CHARS = r'[<>:"|?*\\]'
OK_STATES = ("ok", "skipped")

FFMPEG = "ffmpeg"
DECODE_EXT = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".wmv",
              ".m4a", ".mp3", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff"}
DEFAULT_PATTERNS = ["*.wav", "*.mp4", "*.mov", "*.mkv", "*.avi", "*.flv",
                    "*.webm", "*.m4a", "*.mp3", "*.aac", "*.flac", "*.ogg",
                    "*.opus", "*.wma"]


# ---------------------------------------------------------------- planning ---

def clean_key(text):
    """Turn a relative path into a safe, still readable output stem.

    Separators become __ BEFORE scrubbing illegal characters: a Windows
    backslash is both a separator and an illegal filename character, and
    scrubbing first would leave `sub_02` instead of `sub__02`.
    """
    key = text.replace("/", "__").replace("\\", "__")
    key = re.sub(BAD_CHARS, "_", key)
    key = key.strip().rstrip(".")
    return key or "clip"


def walk_files(root, patterns, recursive):
    """Files under root matching any pattern. An explicit file path bypasses patterns."""
    if os.path.isfile(root):
        return [root]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        if not recursive:
            del dirnames[:]
        for name in sorted(filenames):
            low = name.lower()
            if any(fnmatch.fnmatch(low, p.lower()) for p in patterns):
                found.append(os.path.join(dirpath, name))
    return found


def key_for(path, root):
    """Relative-to-root stem; subdirectories become __ so output stays flat."""
    if not os.path.isdir(root) or os.path.abspath(path) == os.path.abspath(root):
        # root was an explicit file: its own stem, never the relative path "."
        return clean_key(os.path.splitext(os.path.basename(path))[0])
    rel = os.path.relpath(path, root)
    return clean_key(os.path.splitext(rel)[0])


def collect(inputs, patterns, recursive, out_dir):
    """Return [{'src','key','root'}] sorted and de-duplicated.

    Keys are prefixed only when two different sources would collide, so the
    common single-folder case keeps clean names like `01`.
    """
    items, seen = [], set()
    out_real = os.path.realpath(out_dir)
    for root in inputs:
        for src in walk_files(root, patterns, recursive):
            real = os.path.realpath(src)
            if real in seen:
                continue
            # Never re-ingest our own output when --out-dir sits inside --input.
            if real.startswith(out_real + os.sep):
                continue
            seen.add(real)
            items.append({"src": src, "key": key_for(src, root), "root": root})

    by_key = {}
    for it in items:
        by_key.setdefault(it["key"], []).append(it)
    for key, group in by_key.items():
        if len(group) == 1:
            continue
        for it in group:
            base = os.path.basename(os.path.abspath(
                it["root"] if os.path.isdir(it["root"]) else os.path.dirname(it["root"])))
            it["key"] = clean_key("%s__%s" % (base, key))

    used = {}
    for it in items:                       # last resort: identical folder names
        if it["key"] in used:
            used[it["key"]] += 1
            it["key"] = clean_key("%s__%d" % (it["key"], used[it["key"]]))
        else:
            used[it["key"]] = 1
    return items


# -------------------------------------------------------------- done check ---

def is_done(json_path, md_path=None):
    """(done, reason). 'Done' means the previous pair of outputs is complete.

    One task produces two files (`<key>.json` + `<key>.md`), so both count. The
    JSON alone is not enough: if the process dies between the two writes, the
    JSON can be perfectly valid while the transcript a human would read is
    missing. Treating that as done would report `ok` for a half-deliverable and
    never produce the .md.

    A truncated JSON (process killed mid-write) fails to parse -> re-run. An
    empty segments list is legitimate (silent audio), so it counts as done; that
    is a quality question for qc_check.py, not a reason to transcribe again.
    """
    if not os.path.exists(json_path):
        return False, "no output yet"
    try:
        if os.path.getsize(json_path) <= 0:
            return False, "output is empty"
        with open(json_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        return False, "unreadable output (%s)" % exc
    if not isinstance(data, dict):
        return False, "output is not a JSON object"
    if data.get("error"):
        return False, "previous run recorded an error"
    if not isinstance(data.get("segments"), list) or "duration" not in data:
        return False, "output is missing segments/duration"
    md = md_path or os.path.splitext(json_path)[0] + ".md"
    if not os.path.exists(md):
        return False, "markdown missing"
    try:
        if os.path.getsize(md) <= 0:
            return False, "markdown is empty"
    except OSError as exc:
        return False, "markdown unreadable (%s)" % exc
    return True, ""


# ----------------------------------------------------------------- running ---

def build_cmd(py, script, src, out_json, key, opts):
    """Command for one file. -X utf8 is explicit: it is not inherited from the parent."""
    cmd = [py, "-X", "utf8", script,
           "--wav", src, "--model", opts["model"], "--out", out_json,
           "--tag", key, "--device", opts["device"], "--language", opts["language"]]
    if opts.get("compute"):
        cmd += ["--compute", opts["compute"]]
    return cmd


def child_env():
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def tail(text, limit=900):
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return "\n".join(lines[-12:])[-limit:]


def read_metrics(json_path):
    try:
        with open(json_path, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    return {"audio_s": d.get("duration"), "device": d.get("device"),
            "rtf_x": d.get("rtf_x"), "load_s": d.get("load_s"),
            "asr_s": d.get("asr_s"), "fallback": bool(d.get("fallback"))}


def md_for(json_path):
    return os.path.splitext(json_path)[0] + ".md"


def stage_aside(json_path):
    """Move the existing output pair to .prev; return an error string or None.

    Two failure modes this prevents:

      1. A failed --force run would leave the old JSON in place, is_done() would
         say 'done' next time, and the file would be skipped forever.
      2. If only the JSON were moved, the *stale* .md would stay behind at its
         normal path - and the .md is the file a human actually reads. After a
         failed re-run the folder would show a believable transcript with no
         JSON beside it, which looks like a successful result.

    A half-staged pair is worse than no staging at all, so a failure on the
    second file rolls the first one back. Both halves are reported: silently
    swallowing an OSError here used to let a re-run overwrite the old .md with
    no backup, losing the previous transcript while claiming success.
    """
    moved = []
    for path in (json_path, md_for(json_path)):
        if not os.path.exists(path):
            continue
        try:
            os.replace(path, path + ".prev")
        except OSError as exc:
            failed_rollback = []
            for done in reversed(moved):
                try:
                    os.replace(done + ".prev", done)
                except OSError as back:
                    failed_rollback.append("%s (%s)" % (done, back))
            msg = "could not stage %s aside: %s" % (path, exc)
            if failed_rollback:
                msg += "; ROLLBACK FAILED, restore manually: " + ", ".join(
                    failed_rollback)
            return msg
        moved.append(path)
    return None


def drop_prev(json_path):
    for path in (json_path, md_for(json_path)):
        try:
            if os.path.exists(path + ".prev"):
                os.remove(path + ".prev")
        except OSError:
            pass


def run_one(src, out_json, key, opts):
    """Transcribe a single file. Never raises: every outcome becomes a status."""
    rec = {"status": "failed", "exit_code": None, "wall_s": 0.0, "error": ""}
    if not os.path.exists(src):
        rec["error"] = "input disappeared before it was processed"
        return rec
    # Decode before stage_aside: if ffmpeg fails, the previous good pair must
    # stay where it is. Moving it aside first would leave .prev files and no
    # visible transcript, which reads as "the run deleted my work".
    wav, err = extract_audio(src, key, opts["out_dir"], opts["timeout"],
                             force=opts.get("force", False))
    if err:
        rec["error"] = err
        return rec
    stage_error = stage_aside(out_json)
    if stage_error:
        rec["error"] = stage_error
        return rec
    cmd = build_cmd(opts["python"], opts["script"], wav, out_json, key, opts)
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=opts["timeout"],
                              env=child_env())
    except subprocess.TimeoutExpired:
        rec["status"] = "timeout"
        rec["wall_s"] = round(time.time() - started, 2)
        rec["error"] = "exceeded --timeout %ss" % opts["timeout"]
        return rec
    except Exception as exc:
        rec["wall_s"] = round(time.time() - started, 2)
        rec["error"] = "could not start transcribe.py: %s" % exc
        return rec

    rec["wall_s"] = round(time.time() - started, 2)
    rec["exit_code"] = proc.returncode
    if proc.returncode == 0:
        done, why = is_done(out_json)
        if done:
            rec["status"] = "ok"
            rec.update(read_metrics(out_json))
            drop_prev(out_json)
            return rec
        # Exit 0 but half a deliverable (typically a JSON with no .md). Never
        # report that as ok: the next run sees the same incomplete pair and
        # retries, so the missing half does get produced.
        rec["error"] = "exited 0 but output is incomplete: %s" % why
        return rec
    stderr = proc.stderr.decode("utf-8", "replace")
    stdout = proc.stdout.decode("utf-8", "replace")
    rec["error"] = (tail(stderr) or tail(stdout)
                    or "exit code %d, no message" % proc.returncode)
    return rec


# ------------------------------------------------------------------ output ---

def write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def needs_extract(src):
    """True when the input is not a PCM WAV and has to be decoded first."""
    return os.path.splitext(src)[1].lower() in DECODE_EXT


def audio_cache_path(out_dir, key):
    return os.path.join(out_dir, SUMMARY_DIR, "audio", key + ".wav")


def wav_ok(path):
    """Return a reason why a decoded WAV is unusable, or None when it is sound.

    `os.path.getsize(dst) > 44` only proves that ffmpeg wrote *something*. A
    timeout or a killed decode leaves a multi-megabyte WAV with a plausible
    header and a truncated data chunk; the next run used to accept it as a
    valid cache and transcribe silence forever.

    Checks the format we asked ffmpeg for, then seeks to the last declared
    frame and reads it. `readframes()` returns short when the file ends before
    the header says it should, which is exactly the truncated-cache case. We
    deliberately do not compare against the source container's duration: that
    needs ffprobe, is unreliable for some containers, and would add a tolerance
    knob for no extra certainty about *this* file's integrity.
    """
    try:
        with wave.open(path, "rb") as w:
            if w.getnchannels() != 1:
                return "%d channels, expected 1" % w.getnchannels()
            if w.getsampwidth() != 2:
                return "%d-byte samples, expected 2" % w.getsampwidth()
            if w.getframerate() != 16000:
                return "%d Hz, expected 16000" % w.getframerate()
            frames = w.getnframes()
            if frames <= 0:
                return "no audio frames"
            w.setpos(frames - 1)
            frame_bytes = w.getnchannels() * w.getsampwidth()
            if len(w.readframes(1)) != frame_bytes:
                return "truncated data chunk (header claims %d frames)" % frames
    except (wave.Error, EOFError, OSError, ValueError) as exc:
        return "unreadable WAV: %s" % exc
    return None


def extract_audio(src, key, out_dir, timeout, force=False):
    """Return (wav_path, error). A WAV input is returned untouched.

    The decoded copy is cached under <out-dir>/_batch/audio and reused, so a
    retry after a failed transcription does not pay for ffmpeg again. --force
    re-decodes, because the source may have been replaced between runs.

    ffmpeg only ever writes to a .part temporary file. It is validated and
    atomically renamed into place on success, and removed on every failure
    path - including a timeout, where ffmpeg has usually already written a
    large, unusable file. A corrupt pre-existing cache is re-decoded rather
    than reused.

    ffmpeg is only needed when a non-WAV actually appears: a batch of WAV files
    still runs on a machine with no ffmpeg at all (the six test suites rely on
    that, and so do minimal installs).
    """
    if not needs_extract(src):
        return src, None
    dst = audio_cache_path(out_dir, key)
    if not force and os.path.exists(dst) and wav_ok(dst) is None:
        return dst, None
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    # Keep the .wav suffix: ffmpeg picks the output format from the filename,
    # and writing to "05.wav.part-1234" made it fail with "Unable to find a
    # suitable output format" before any audio was decoded. -f wav makes the
    # intent explicit as well.
    part = "%s.part-%d.wav" % (os.path.splitext(dst)[0], os.getpid())
    cmd = [FFMPEG, "-v", "error", "-y", "-i", src, "-vn",
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", part]
    try:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except FileNotFoundError:
            return None, ("ffmpeg not found on PATH - it is needed to decode %s. "
                          "Install ffmpeg, or convert to 16 kHz mono WAV first."
                          % os.path.basename(src))
        except subprocess.TimeoutExpired:
            return None, ("ffmpeg timed out after %ss on %s"
                          % (timeout, os.path.basename(src)))
        except Exception as exc:
            return None, "could not run ffmpeg: %s" % exc
        if proc.returncode != 0:
            msg = proc.stderr.decode("utf-8", "replace").strip()
            return None, "ffmpeg failed (%d) on %s: %s" % (
                proc.returncode, os.path.basename(src), tail(msg, 400))
        why = wav_ok(part)
        if why:
            return None, ("ffmpeg produced an incomplete WAV for %s: %s"
                          % (os.path.basename(src), why))
        os.replace(part, dst)
        part = None
        return dst, None
    finally:
        if part:
            try:
                if os.path.exists(part):
                    os.remove(part)
            except OSError:
                pass


def planned_extract(items):
    """Inputs that will be decoded before transcription (for the pre-run notice)."""
    return [it["src"] for it in items if needs_extract(it["src"])]


def counts_of(records):
    out = {"total": len(records), "ok": 0, "skipped": 0, "failed": 0, "timeout": 0}
    for rec in records:
        out[rec["status"]] = out.get(rec["status"], 0) + 1
    return out


def fmt_line(idx, total, rec):
    head = "[%*d/%d]" % (len(str(total)), idx, total)
    name = os.path.basename(rec["src"])
    if len(name) > 34:
        name = name[:31] + "..."
    if rec["status"] in OK_STATES:
        extra = []
        if rec.get("audio_s"):
            extra.append("audio=%.1fs" % rec["audio_s"])
        if rec.get("rtf_x"):
            extra.append("rtf=%.2fx" % rec["rtf_x"])
        if rec.get("fallback"):
            extra.append("cpu-fallback")
        return "%s %-7s %-34s %6.1fs  %s" % (head, rec["status"], name,
                                            rec["wall_s"], " ".join(extra))
    first = (rec.get("error") or "").splitlines()
    reason = first[-1].strip() if first else ""
    return "%s %-7s %-34s exit=%s  %s" % (head, rec["status"].upper(), name,
                                          rec["exit_code"], reason[:110])


def payload(records, meta, finished, elapsed):
    return {
        "schema": 1, "finished": finished,
        "started_at": meta["started_at"],
        "finished_at": meta["finished_at"] if finished else None,
        "elapsed_s": round(elapsed, 1),
        "model": meta["model"], "device": meta["device"],
        "compute": meta["compute"], "language": meta["language"],
        "pattern": meta["pattern"], "recursive": meta["recursive"],
        "out_dir": meta["out_dir"],
        "counts": counts_of(records),
        "results": [{"src": r["src"], "key": r["key"],
                     "out_json": os.path.relpath(r["out_json"], meta["out_dir"]),
                     "status": r["status"], "exit_code": r["exit_code"],
                     "skip_reason": r.get("skip_reason", ""),
                     "wall_s": r["wall_s"], "audio_s": r.get("audio_s"),
                     "device": r.get("device"), "rtf_x": r.get("rtf_x"),
                     "fallback": r.get("fallback", False),
                     "error": r["error"]} for r in records],
    }


def main():
    ap = argparse.ArgumentParser(
        description="Transcribe every audio file in one or more folders, serially.")
    ap.add_argument("--input", nargs="+", required=True,
                    help="folder(s) or explicit file(s)")
    ap.add_argument("--out-dir", required=True,
                    help="where <key>.json / <key>.md go (also holds _batch/summary.json)")
    ap.add_argument("--model", default="", help="CTranslate2 model dir (required unless --dry-run)")
    ap.add_argument("--pattern", nargs="+", default=DEFAULT_PATTERNS,
                    help="filename patterns, case-insensitive (default: WAV plus "
                         "common video/audio containers). Non-WAV matches are "
                         "decoded to 16 kHz mono WAV with ffmpeg under "
                         "<out-dir>/_batch/audio/ before transcription")
    ap.add_argument("--recursive", action="store_true", help="descend into subfolders")
    ap.add_argument("--force", action="store_true", help="re-run even if output exists")
    ap.add_argument("--dry-run", action="store_true", help="list the plan, run nothing")
    ap.add_argument("--timeout", type=int, default=1800, help="per file, seconds")
    ap.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    ap.add_argument("--compute", default=None)
    ap.add_argument("--language", default="zh")
    a = ap.parse_args()

    if not a.dry_run and not a.model:
        ap.error("--model is required (or use --dry-run to only list the plan)")
    if not a.dry_run and not os.path.exists(TRANSCRIBE):
        ap.error("transcribe.py not found next to this script: %s" % TRANSCRIBE)
    try:
        if not a.dry_run:
            os.makedirs(a.out_dir, exist_ok=True)
    except OSError as exc:
        print("[ERROR] cannot create --out-dir %s: %s" % (a.out_dir, exc))
        sys.exit(2)

    items = collect(a.input, a.pattern, a.recursive, a.out_dir)
    if not items:
        print("[ERROR] no file matched %s under: %s"
              % (" ".join(a.pattern), ", ".join(a.input)))
        sys.exit(2)

    plan = []
    for it in items:
        out_json = os.path.join(a.out_dir, it["key"] + ".json")
        done, why = (False, "forced") if a.force else is_done(out_json)
        rec = {"src": it["src"], "key": it["key"], "out_json": out_json,
               "status": "skipped" if done else "pending",
               "skip_reason": why if done else "",
               "exit_code": None, "wall_s": 0.0, "error": ""}
        if done:
            # Keep the summary informative even when nothing ran this time.
            rec.update(read_metrics(out_json))
        plan.append(rec)

    todo = [p for p in plan if p["status"] == "pending"]
    decode = planned_extract(items)
    if decode:
        print("[INFO] %d of %d matched file(s) are not WAV; they will be decoded "
              "once to 16 kHz mono WAV under %s"
              % (len(decode), len(items),
                 os.path.join(a.out_dir, SUMMARY_DIR, "audio")))
        for src in decode[:3]:
            print("       %s" % src)
        if len(decode) > 3:
            print("       ... and %d more" % (len(decode) - 3))
        if not shutil.which(FFMPEG):
            if a.dry_run:
                print("[WARN] ffmpeg is not on PATH - these files cannot be "
                      "decoded until it is installed")
            else:
                print("[ERROR] ffmpeg is not on PATH - install it, or convert "
                      "the inputs to 16 kHz mono WAV yourself")
                sys.exit(2)
    print("inputs   : %s" % ", ".join(a.input))
    print("pattern  : %s%s" % (" ".join(a.pattern),
                               "  (recursive)" if a.recursive else ""))
    print("out-dir  : %s" % os.path.abspath(a.out_dir))
    print("files    : %d matched, %d to run, %d already done"
          % (len(plan), len(todo), len(plan) - len(todo)))
    if a.dry_run:
        for p in plan:
            mark = "skip" if p["status"] == "skipped" else "run "
            note = "  [decode]" if needs_extract(p["src"]) else ""
            print("  %s  %-40s -> %s%s" % (mark, p["src"],
                                           os.path.basename(p["out_json"]), note))
        print("dry run: nothing executed.")
        return

    opts = {"model": a.model, "device": a.device, "compute": a.compute,
            "language": a.language, "timeout": a.timeout,
            "python": sys.executable, "script": TRANSCRIBE,
            "out_dir": a.out_dir, "force": a.force}
    meta = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "finished_at": None,
            "model": os.path.basename(a.model.rstrip("/\\")), "device": a.device,
            "compute": a.compute, "language": a.language, "pattern": a.pattern,
            "recursive": a.recursive, "out_dir": os.path.abspath(a.out_dir)}
    summary_path = os.path.join(a.out_dir, SUMMARY_DIR, SUMMARY_NAME)

    t0 = time.time()
    records = []
    total = len(plan)
    interactive = sys.stdout.isatty()
    for idx, p in enumerate(plan, 1):
        if p["status"] == "skipped":
            print(fmt_line(idx, total, p))
            records.append(p)
            continue
        if interactive:
            print("%s ..." % ("[%*d/%d]" % (len(str(total)), idx, total)),
                  end="", flush=True)
        p.update(run_one(p["src"], p["out_json"], p["key"], opts))
        records.append(p)
        if interactive:
            print("\r" + fmt_line(idx, total, p) + " " * 12)
        else:
            print(fmt_line(idx, total, p))
        meta["finished_at"] = None
        write_json_atomic(summary_path,
                          payload(records, meta, False, time.time() - t0))

    c = counts_of(records)
    meta["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    write_json_atomic(summary_path, payload(records, meta, True, time.time() - t0))

    print("")
    print("done: %d ok, %d skipped, %d failed, %d timeout  (%.1fs)"
          % (c["ok"], c["skipped"], c["failed"], c["timeout"], time.time() - t0))
    print("summary: %s" % summary_path)
    if c["failed"] or c["timeout"]:
        print("re-run the same command to retry only the failures "
              "(finished files are skipped).")
        sys.exit(1)
    print("next: python %s \"%s\""
          % (os.path.join(HERE, "qc_check.py"), os.path.join(a.out_dir, "*.json")))
    sys.exit(0)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure") and not sys.stdout.isatty():
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    main()
