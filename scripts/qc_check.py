# -*- coding: utf-8 -*-
"""Quality check for transcription JSON files (read-only).

Two severity classes (semantics explicit, legacy key names preserved):

  ERROR  (field "errors") -- deterministic data/schema problems. The input
      cannot be trusted: missing, non-numeric, or non-finite (NaN / +-Inf)
      timestamps, negative start, end < start, out-of-order segments,
      missing/invalid duration, missing or malformed segments, unparseable
      JSON. Always a non-zero exit.
  REVIEW (field "red") -- risk signals needing a human look, NOT proof of a
      transcription error: overlapping segments, empty text, hallucinated
      tail, long gaps, prompt echoes, no real timestamps. Also non-zero exit
      so it can gate CI. "warn" stays for coarse metric outliers only.

Compatibility: legacy keys (file / error / red / warn / duration /
has_timestamps / note / chars / segments / punct_per_100 / chars_per_min /
segs_per_min / max_gap_s / tail_gap_s / tail_overrun_s) are preserved;
status / errors / thresholds are additive. Exit code is 1 whenever any file
has ERROR or RED, in both text and --json modes.

Fixed thresholds (STRICTLY greater-than; equality does NOT trigger):
    long gap     : gap > 8.00 s      (8.00 passes, 8.01 flags)
    tail overrun : last_end - duration > 0.50 s
    tail gap     : duration - last_end > 10.00 s (warning only)
    overlap      : overlap > 0.001 s (review only; 0 in 6682 real segment pairs)

When duration is missing or <= 0, or ANY segment timestamp is unusable, the
timestamp-dependent checks are NOT run and the report says so explicitly
("无法检查 / cannot check"); those metrics are reported as null instead of a
misleading 0.00. Non-finite numbers (NaN / +-Inf) and ints too large for a
float are ERROR, never silently accepted.

"Usable" means BOTH numeric and logically valid. A timestamp that parses is
not automatically usable: a negative start, end < start, or out-of-order
segments make gap/tail arithmetic meaningless (out-of-order input once
produced max_gap_s = -25.00, a physically impossible value). Such files get
note "INVALID TIMELINE" and all three time metrics null, while text-derived
metrics (chars / punct_per_100 / chars_per_min) remain valid. --json output is
always strict JSON (non-finite values are emitted as null, never NaN/Infinity).

Two producer baselines. Files carrying "engine": "subtitle-track" were written
by scripts/extract_subs.py from a real subtitle track, not by ASR. Subtitle
wording is condensed on purpose, so chars/min and segments/min sit well below
the ASR bands; only those two density warnings are skipped for them, and the
report says "source": "subtitle-track". Every other check - overlaps, long
gaps, tail, prompt echoes - is source-independent and still runs. Density is
skipped, not relaxed: inventing a second numeric band would imply a precision
no measurement here supports.

Inputs WITHOUT real timestamps (single placeholder segment covering the whole
audio, or "timestamps": false) are flagged: tail/gap metrics are N/A.

Expected JSON shape (faster-whisper style, produced by scripts/transcribe.py):
    {"duration": 115.24, "timestamps": true,
     "segments": [{"s": 0.0, "e": 3.2, "t": "..."}, ...]}
Also accepts {"start","end","text"} keys and a top-level "text" fallback.
"""
import argparse
import glob
import json
import math
import os
import re
import sys
import unicodedata

# Strong prompt echoes are high-confidence hallucination signals. Closing
# boilerplate in the head is only a warning: it can be genuine content too.
HEAD_STRONG = ["请使用简体中文", "并加上标点符号"]
HEAD_WEAK = ["请不吝点赞", "订阅", "转发", "打赏", "明镜", "字幕由"]
TAIL_KEYWORDS = ["谢谢大家", "感谢观看", "请不吝点赞", "订阅", "转发",
                 "打赏", "明镜", "字幕由"]
NO_TS_NOTE = ("NO TIMESTAMPS: tail/gap checks not applicable; "
              "read the ending manually")
DUR_BAD_NOTE = ("INVALID/MISSING DURATION: 无法检查 (cannot check) "
                "tail/gap/overrun checks; metrics reported as null")
TS_BAD_NOTE = ("INVALID TIMESTAMPS: 时间类指标未计算 (gap/tail/overrun = N/A); "
               "fix the segment timestamps and re-run")
TIMELINE_BAD_NOTE = ("INVALID TIMELINE: 时间轴逻辑无效 (negative start / end < start / "
                     "out of order); gap/tail/overrun = N/A; text metrics still valid")
PUNCT = r"[，。！？、,.!?;；:：]"
DEFAULT_GAP = 8.0            # strictly greater-than
DEFAULT_TAIL = 10.0          # strictly greater-than, warning only
OVERRUN_LIMIT = 0.5          # strictly greater-than
OVERLAP_EPS = 0.001          # ignore float noise, review only
DEFAULT_CPM = (200.0, 450.0)
DEFAULT_SPM = (15.0, 40.0)
_MISSING = object()


def is_number(v):
    """JSON numbers only. bool is a subclass of int -> reject. Strings rejected."""
    return (isinstance(v, (int, float)) and not isinstance(v, bool))


def is_finite_number(v):
    """A number usable in time arithmetic: finite and float-representable.

    JSON permits NaN / Infinity in Python's parser (allow_nan), so type checks
    alone let them slip through every comparison and turn a broken file into a
    silent "OK". Ints above ~1e308 are equally unusable: float() overflows.
    """
    if not is_number(v):
        return False
    try:
        return math.isfinite(v)
    except (OverflowError, ValueError):
        return False


def brief(v, limit=40):
    """Short repr for diagnostics; 10**400 must not flood the report."""
    s = repr(v)
    return s if len(s) <= limit else s[:limit - 3] + "..."


def blank_row(name, msg):
    """A fatal/error row that still carries every legacy key.

    "source" is "unknown" here rather than "asr": these rows are produced
    before (or instead of) a successful parse, so claiming a producer would be
    a guess. The key still exists, so JSON consumers never need a branch.
    """
    return {
        "file": name, "status": "ERROR", "error": msg, "errors": [msg],
        "source": "unknown",
        "red": [], "warn": [], "note": "",
        "duration": None, "chars": 0, "segments": 0,
        "punct_per_100": 0.0, "chars_per_min": 0.0, "segs_per_min": 0.0,
        "max_gap_s": None, "tail_gap_s": None, "tail_overrun_s": None,
        "has_timestamps": False, "thresholds": {},
    }


def result_with_errors(name, errors):
    """Preserve the specific validation errors instead of a generic fatal message."""
    row = blank_row(name, "; ".join(errors))
    row["errors"] = list(errors)
    return row


def validate(d):
    """Return (segs, errors, review, dur_ok, dur, ts_ok, ts_problem).

    No exceptions on malformed input: every problem becomes a readable ERROR.

    Two separate notions, deliberately kept apart:
      * numeric readability -- every timestamp parsed to a finite float;
      * timeline validity  -- start >= 0, end >= start, segments in order.
    ts_ok is True only when BOTH hold. A file whose timestamps merely parse is
    still unusable for gap/tail arithmetic if the timeline is nonsense: a
    negative start invents a gap, end < start makes the segment run backwards,
    and out-of-order segments yield a NEGATIVE max_gap (seen: -25.00 s). So
    callers must skip every time-dependent metric unless ts_ok is True.
    ts_problem is "", "numeric", "timeline" or "numeric+timeline"; it only
    selects the wording of the note.
    """
    errors, review = [], []
    raw_dur = d.get("duration", _MISSING)
    dur = None
    if raw_dur is _MISSING or raw_dur is None:
        errors.append("duration missing: %s" % DUR_BAD_NOTE)
    elif not is_number(raw_dur):
        errors.append("duration not numeric (%s): %s"
                      % (brief(raw_dur), DUR_BAD_NOTE))
    elif not is_finite_number(raw_dur):
        errors.append("duration not finite (%s): %s"
                      % (brief(raw_dur), DUR_BAD_NOTE))
    elif float(raw_dur) <= 0:
        errors.append("duration must be > 0 (got %s): %s"
                      % (brief(raw_dur), DUR_BAD_NOTE))
    else:
        dur = float(raw_dur)
    dur_ok = dur is not None

    raw = d.get("segments", _MISSING)
    segs = []
    ts_ok = True          # every timestamp parsed to a finite float
    timeline_bad = False  # parsed fine, but the timeline is logically invalid
    if raw is _MISSING or raw is None:
        txt = d.get("text")
        if isinstance(txt, str) and txt.strip():
            segs = [{"s": 0.0, "e": dur if dur_ok else 0.0, "t": txt}]
            review.append("text-only fallback used: no structured segments (review)")
        else:
            errors.append("MISSING segments: no transcript data")
    elif not isinstance(raw, list):
        errors.append("segments must be a list, got %s" % type(raw).__name__)
    elif not raw:
        errors.append("segments is empty: no transcript data")
    else:
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                errors.append("segment[%d] is not an object (%s)"
                              % (i, type(item).__name__))
                ts_ok = False
                continue
            sv = item.get("s", item.get("start", _MISSING))
            ev = item.get("e", item.get("end", _MISSING))
            label = "segment[%d]" % i
            s = e = None
            if sv is _MISSING or sv is None:
                errors.append("%s start missing" % label)
            elif not is_number(sv):
                errors.append("%s start not numeric (%s)" % (label, brief(sv)))
            elif not is_finite_number(sv):
                errors.append("%s start not finite (%s)" % (label, brief(sv)))
            else:
                s = float(sv)
            if ev is _MISSING or ev is None:
                errors.append("%s end missing" % label)
            elif not is_number(ev):
                errors.append("%s end not numeric (%s)" % (label, brief(ev)))
            elif not is_finite_number(ev):
                errors.append("%s end not finite (%s)" % (label, brief(ev)))
            else:
                e = float(ev)
            t = item.get("t", item.get("text", ""))
            t = "" if t is None else str(t)
            if s is None or e is None:
                ts_ok = False
            if s is not None and e is not None:
                if s < 0:
                    errors.append("%s start < 0 (%.2f)" % (label, s))
                    timeline_bad = True
                if e < s:
                    errors.append("%s end < start (%.2f < %.2f)" % (label, e, s))
                    timeline_bad = True
            if not t.strip():
                review.append("%s empty/whitespace-only text (review)" % label)
            # Placeholders exist only to keep the text usable; they are never
            # fed into gap/tail/order statistics (ts_ok gates those).
            segs.append({"s": s if s is not None else 0.0,
                         "e": e if e is not None else (s if s is not None else 0.0),
                         "t": t})
        if ts_ok:
            for i in range(1, len(segs)):
                prev, cur = segs[i - 1], segs[i]
                if cur["s"] < prev["s"] - OVERLAP_EPS:
                    errors.append("segments out of order at index %d (%.2f < %.2f)"
                                  % (i, cur["s"], prev["s"]))
                    timeline_bad = True
                ov = prev["e"] - cur["s"]
                # Overlap is only meaningful when the two segments are in order;
                # out-of-order segments already produce an ERROR above.
                if cur["s"] >= prev["s"] - OVERLAP_EPS and ov > OVERLAP_EPS:
                    review.append("overlapping segments at index %d (%.2fs overlap, review)"
                                  % (i, ov))
    problem = "+".join([p for p in ("" if ts_ok else "numeric",
                                    "timeline" if timeline_bad else "") if p])
    return segs, errors, review, dur_ok, dur, ts_ok and not timeline_bad, problem


def check(path, gap_limit, tail_limit):
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict):
        return blank_row(os.path.basename(path), "top-level JSON must be an object")
    segs, errors, red, dur_ok, dur, ts_ok, ts_problem = validate(d)
    if not segs:
        return result_with_errors(os.path.basename(path),
                                  errors or ["no usable segments"])

    # Two producers, two baselines. A subtitle track is not a verbatim
    # transcript: it drops filler words, merges speech into reading-sized cues
    # and arrives with its own punctuation, so chars/min and segments/min land
    # far below the ASR bands. Measuring it with the ASR ruler flags every
    # single subtitle file, and an always-on warning teaches people to ignore
    # warnings. Only the density pair is skipped - overlaps, gaps, tail and
    # prompt echoes are source-independent and still apply.
    source = str(d.get("engine") or "asr")
    is_subs = source == "subtitle-track"

    text = "".join(s["t"] for s in segs)
    norm = unicodedata.normalize("NFKC", text)
    minutes = dur / 60.0 if dur_ok else 0
    time_ok = dur_ok and ts_ok          # every time-based metric needs both

    gaps = [(segs[i + 1]["s"] - segs[i]["e"], segs[i]["e"], segs[i + 1]["s"])
            for i in range(len(segs) - 1)] if time_ok else []
    max_gap, gap_at = (max(gaps)[0], max(gaps)[1:]) if gaps else (0.0, (0, 0))
    tail_gap = (dur - segs[-1]["e"]) if time_ok else None
    overrun = (segs[-1]["e"] - dur) if time_ok else None

    # Explicit declaration wins. Otherwise only a single segment that starts at
    # 0 and covers the full duration *exactly* counts as a placeholder; a
    # tolerance of 0.5 s wrongly swallowed slight-but-real tail overruns.
    ts_flag = d.get("timestamps", _MISSING)
    if not ts_ok:
        no_ts = False                   # separate failure mode, reported below
    elif ts_flag is False:
        no_ts = True
    elif ts_flag is True:
        no_ts = False
    else:
        no_ts = (len(segs) == 1 and dur_ok
                 and abs(segs[0]["s"]) < 0.01
                 and abs(segs[0]["e"] - dur) < 0.05)

    warn = []
    # Numeric failure is the more fundamental one, so it wins the wording.
    ts_note = "" if ts_ok else (TIMELINE_BAD_NOTE
                                if ts_problem == "timeline" else TS_BAD_NOTE)
    if ts_note:
        warn.append(ts_note)
    if no_ts:
        red.append(NO_TS_NOTE)
        warn.append("segment count / gaps / tail are not meaningful here")
    if not dur_ok:
        warn.append(DUR_BAD_NOTE)

    head = [s for s in segs if s["s"] < 30]
    # "Tail" is a TIME WINDOW, not a fixed segment count. With only two long
    # segments, segs[-2:] covers the whole audio and misreads an opening phrase
    # as a closing one. Flag closing boilerplate only in the final 30 seconds.
    if time_ok:
        tail = [s for s in segs if s["s"] >= max(0.0, dur - 30)] or segs[-1:]
    else:
        tail = segs[-1:]
    for kw in HEAD_STRONG:
        if any(kw in s["t"] for s in head):
            red.append("head prompt echo (review): " + kw)
    for kw in HEAD_WEAK:
        if any(kw in s["t"] for s in head):
            warn.append("closing phrase in first 30s (verify manually): " + kw)
    for kw in TAIL_KEYWORDS:
        if any(kw in s["t"] for s in tail):
            red.append("tail closing phrase (review): " + kw)

    # Duration-dependent hard checks: skipped entirely (and reported) when
    # duration or any timestamp is invalid, so a broken input can never look
    # "clean" and no metric is computed from a placeholder.
    if time_ok and not no_ts:
        if overrun > OVERRUN_LIMIT:
            red.append("TAIL OVERRUN +%.2fs | NEGATIVE TAIL GAP "
                       "(hallucinated tail, NOT a pass): last segment ends after audio"
                       % overrun)
        if max_gap > gap_limit:
            red.append("LONG GAP %.2fs at %.1f-%.1fs"
                       % (max_gap, gap_at[0], gap_at[1]))
        if tail_gap > tail_limit:
            warn.append("tail gap %.2fs > %.1fs (possible truncation)"
                        % (tail_gap, tail_limit))

    cpm = len(norm) / minutes if minutes else 0
    spm = len(segs) / minutes if minutes else 0
    if dur_ok and not is_subs and not (DEFAULT_CPM[0] <= cpm <= DEFAULT_CPM[1]):
        warn.append("char density %.1f/min outside %s" % (cpm, DEFAULT_CPM))
    if (time_ok and not no_ts and not is_subs
            and not (DEFAULT_SPM[0] <= spm <= DEFAULT_SPM[1])):
        warn.append("segments %.1f/min outside %s (check split style)"
                    % (spm, DEFAULT_SPM))

    status = "ERROR" if errors else ("REVIEW" if red else "OK")
    if no_ts:
        note = NO_TS_NOTE
    elif not ts_ok:
        note = ts_note
    elif not dur_ok:
        note = DUR_BAD_NOTE
    else:
        note = ""
    return {
        "file": os.path.basename(path), "status": status,
        "source": source,
        "error": "; ".join(errors) if errors else None,   # legacy key
        "errors": errors, "red": red, "warn": warn,
        "duration": round(dur, 2) if dur_ok else None,
        "chars": len(norm), "segments": len(segs),
        "punct_per_100": round(len(re.findall(PUNCT, norm)) / max(len(norm), 1) * 100, 2),
        "chars_per_min": round(cpm, 1), "segs_per_min": round(spm, 1),
        "max_gap_s": round(max_gap, 2) if time_ok and not no_ts else None,
        "tail_gap_s": round(tail_gap, 2) if time_ok and not no_ts else None,
        "tail_overrun_s": round(overrun, 2) if time_ok and not no_ts else None,
        "has_timestamps": ts_ok and not no_ts,
        "note": note,
        "thresholds": {"gap_gt": gap_limit, "overrun_gt": OVERRUN_LIMIT,
                       "tail_gap_gt": tail_limit, "overlap_gt": OVERLAP_EPS},
    }


def sanitize(obj):
    """Recursively replace non-finite floats with None.

    json.dumps writes NaN/Infinity by default, which is NOT valid JSON and
    breaks strict consumers (jq, JS, Go). Validation should have removed them
    already; this is the last line of defence for --json.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    return obj


def main():
    ap = argparse.ArgumentParser(
        description="Transcript QC: input validation (ERROR) + 4 coarse metrics + 3 hard checks (REVIEW)")
    ap.add_argument("files", nargs="+", help="JSON files or glob patterns")
    ap.add_argument("--gap", type=float, default=DEFAULT_GAP,
                    help="long-gap threshold in s, strictly greater-than (default 8.0)")
    ap.add_argument("--tail", type=float, default=DEFAULT_TAIL,
                    help="tail-gap warn threshold in s (default 10.0)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args()

    paths = []
    for p in a.files:
        paths += sorted(glob.glob(p)) if any(c in p for c in "*?[") else [p]
    if not paths:
        print("No input files matched.", file=sys.stderr)
        sys.exit(2)
    rows = []
    for p in paths:
        if not os.path.isfile(p):
            rows.append(blank_row(p, "file not found"))
            continue
        try:
            rows.append(check(p, a.gap, a.tail))
        except json.JSONDecodeError as e:
            rows.append(blank_row(os.path.basename(p), "invalid JSON: %s" % e))
        except Exception as e:
            rows.append(blank_row(os.path.basename(p),
                                  "%s: %s" % (type(e).__name__, str(e)[:160])))

    n_bad = sum(1 for r in rows if r.get("errors") or r.get("red"))
    n_err = sum(1 for r in rows if r.get("errors"))
    n_red = sum(1 for r in rows if r.get("red"))
    if a.json:
        # JSON is a byte format: RFC 8259 requires UTF-8 (or UTF-16/32), and
        # every consumer (json.load / jq / JS / Go) assumes it. On a zh-CN
        # Windows the pipe/file default is cp936, so a redirected --json used
        # to be GBK: json.load(encoding="utf-8") failed and jq rejected it.
        # Only touch non-TTY streams; a human-facing console keeps its own
        # code page so the Chinese diagnostics still render.
        try:
            if not sys.stdout.isatty():
                sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(json.dumps(sanitize(rows), ensure_ascii=False, indent=2,
                         allow_nan=False))
        sys.exit(1 if n_bad else 0)

    for r in rows:
        print("--- %s [%s]" % (r["file"], r["status"]))
        for x in r.get("errors", []):
            print("    [ERROR] " + x)
        if r.get("duration") is not None:
            print("    (%.2fs, %d chars, %d segs)"
                  % (r["duration"], r["chars"], r["segments"]))
            if r["has_timestamps"]:
                print("    punct/100=%.2f  chars/min=%.1f  segs/min=%.1f  "
                      "maxgap=%.2fs  tail=%.2fs  overrun=%.2fs"
                      % (r["punct_per_100"], r["chars_per_min"], r["segs_per_min"],
                         r["max_gap_s"], r["tail_gap_s"], r["tail_overrun_s"]))
            else:
                note = r.get("note", "")
                if note.startswith("INVALID TIMESTAMPS"):
                    why = "invalid timestamps"
                elif note.startswith("INVALID TIMELINE"):
                    why = "invalid timeline"
                else:
                    why = "no timestamps"
                print("    punct/100=%.2f  chars/min=%.1f | "
                      "maxgap/tail/overrun = N/A (%s)"
                      % (r["punct_per_100"], r["chars_per_min"], why))
        if r.get("note"):
            print("    [note] " + r["note"])
        for x in r.get("red", []):
            print("    [RED/REVIEW] " + x)
        for x in r.get("warn", []):
            print("    [warn] " + x)
        if r["status"] == "OK" and not r.get("warn"):
            print("    OK (coarse checks only; still inspect head/tail manually)")
    print()
    print("%d file(s): %d ERROR, %d with RED/REVIEW. "
          "ERROR = deterministic input problem; RED = needs human review, not proven wrong."
          % (len(rows), n_err, n_red))
    print("Thresholds are strict: gap > %.2fs, overrun > %.2fs (equality passes). "
          "Manually inspect first/last 30 s and any long-gap interval."
          % (a.gap, OVERRUN_LIMIT))
    sys.exit(1 if n_bad else 0)


if __name__ == "__main__":
    main()
