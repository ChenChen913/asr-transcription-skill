# -*- coding: utf-8 -*-
"""Quality check for transcription JSON files (read-only).

Implements the four coarse metrics plus the three mandatory hard checks:
  - head: prompt echo / closing boilerplate in the first 30 s
  - tail: last segment ends after audio duration, or closing boilerplate
  - gap: any adjacent-segment gap > 8 s

Also normalizes with NFKC before counting punctuation, because U+FE50 / U+FE51
variants otherwise make a punctuated transcript look like it has zero punctuation.

Inputs WITHOUT real timestamps (e.g. a single placeholder segment covering the
whole audio, or "timestamps": false) are flagged explicitly: tail/gap metrics
are reported as N/A instead of a misleading 0.00.

Expected JSON shape (faster-whisper style, produced by scripts/transcribe.py):
    {"duration": 115.24, "timestamps": true,
     "segments": [{"s": 0.0, "e": 3.2, "t": "..."}, ...]}
Also accepts {"start","end","text"} keys and a top-level "text" fallback.

RED means "requires human review", NOT "proven wrong".
Exit code is 1 when any file has RED, in both text and --json modes.
"""
import argparse
import glob
import json
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
PUNCT = r"[，。！？、,.!?;；:：]"
DEFAULT_GAP = 8.0
DEFAULT_TAIL = 10.0
DEFAULT_CPM = (200.0, 450.0)
DEFAULT_SPM = (15.0, 40.0)


def load_segments(data):
    segs = []
    for s in data.get("segments", []):
        start = s.get("s", s.get("start", 0.0))
        end = s.get("e", s.get("end", start))
        text = s.get("t", s.get("text", ""))
        segs.append({"s": float(start), "e": float(end), "t": str(text)})
    if not segs and data.get("text"):
        dur = float(data.get("duration") or 0)
        segs = [{"s": 0.0, "e": dur, "t": data["text"]}]
    return segs


def check(path, gap_limit, tail_limit):
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    segs = load_segments(d)
    if not segs:
        return {"file": os.path.basename(path), "error": "no segments",
                "red": [], "warn": [], "has_timestamps": False, "note": ""}
    dur = float(d.get("duration") or segs[-1]["e"])
    text = "".join(s["t"] for s in segs)
    norm = unicodedata.normalize("NFKC", text)
    minutes = dur / 60.0 if dur else 0

    gaps = [(segs[i + 1]["s"] - segs[i]["e"], segs[i]["e"], segs[i + 1]["s"])
            for i in range(len(segs) - 1)]
    max_gap, gap_at = (max(gaps)[0], max(gaps)[1:]) if gaps else (0.0, (0, 0))
    tail_gap = dur - segs[-1]["e"]
    overrun = segs[-1]["e"] - dur

    no_ts = (d.get("timestamps") is False) or (
        len(segs) == 1 and abs(segs[0]["s"]) < 0.01
        and abs(segs[0]["e"] - dur) < 0.5)

    head = [s for s in segs if s["s"] < 30]
    tail = segs[-2:]
    red, warn = [], []
    if no_ts:
        red.append(NO_TS_NOTE)
        warn.append("segment count / gaps / tail are not meaningful here")
    for kw in HEAD_STRONG:
        if any(kw in s["t"] for s in head):
            red.append("head prompt echo (review): " + kw)
    for kw in HEAD_WEAK:
        if any(kw in s["t"] for s in head):
            warn.append("closing phrase in first 30s (verify manually): " + kw)
    for kw in TAIL_KEYWORDS:
        if any(kw in s["t"] for s in tail):
            red.append("tail closing phrase (review): " + kw)

    if not no_ts:
        if overrun > 0.5:
            red.append("TAIL OVERRUN +%.2fs (last segment ends after audio)" % overrun)
        if tail_gap < -0.5:
            red.append("NEGATIVE TAIL GAP (hallucinated tail, not a pass)")
        if max_gap > gap_limit:
            red.append("LONG GAP %.2fs at %.1f-%.1fs" % (max_gap, gap_at[0], gap_at[1]))
        if tail_gap > tail_limit:
            warn.append("tail gap %.2fs > %.1fs (possible truncation)" % (tail_gap, tail_limit))

    cpm = len(norm) / minutes if minutes else 0
    spm = len(segs) / minutes if minutes else 0
    if not (DEFAULT_CPM[0] <= cpm <= DEFAULT_CPM[1]):
        warn.append("char density %.1f/min outside %s" % (cpm, DEFAULT_CPM))
    if not no_ts and not (DEFAULT_SPM[0] <= spm <= DEFAULT_SPM[1]):
        warn.append("segments %.1f/min outside %s (check split style)" % (spm, DEFAULT_SPM))

    return {
        "file": os.path.basename(path), "duration": round(dur, 2),
        "chars": len(norm), "segments": len(segs),
        "punct_per_100": round(len(re.findall(PUNCT, norm)) / max(len(norm), 1) * 100, 2),
        "chars_per_min": round(cpm, 1), "segs_per_min": round(spm, 1),
        "max_gap_s": None if no_ts else round(max_gap, 2),
        "tail_gap_s": None if no_ts else round(tail_gap, 2),
        "tail_overrun_s": None if no_ts else round(overrun, 2),
        "has_timestamps": not no_ts,
        "red": red, "warn": warn,
        "note": NO_TS_NOTE if no_ts else "",
    }


def main():
    ap = argparse.ArgumentParser(description="Transcript QC: 4 metrics + 3 hard checks")
    ap.add_argument("files", nargs="+", help="JSON files or glob patterns")
    ap.add_argument("--gap", type=float, default=DEFAULT_GAP, help="max gap threshold (s)")
    ap.add_argument("--tail", type=float, default=DEFAULT_TAIL, help="tail gap threshold (s)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args()

    paths = []
    for p in a.files:
        paths += sorted(glob.glob(p)) if any(c in p for c in "*?[") else [p]
    rows = []
    for p in paths:
        if not os.path.isfile(p):
            rows.append({"file": p, "error": "not found", "red": [], "warn": []})
            continue
        try:
            rows.append(check(p, a.gap, a.tail))
        except Exception as e:
            rows.append({"file": os.path.basename(p), "error": str(e),
                         "red": [], "warn": [], "has_timestamps": False, "note": ""})

    n_red = sum(1 for r in rows if r.get("red"))
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        sys.exit(1 if n_red else 0)

    for r in rows:
        print("---", r["file"], end=" ")
        if r.get("error"):
            print("ERROR:", r["error"])
            continue
        print("(%.2fs, %d chars, %d segs)" % (r["duration"], r["chars"], r["segments"]))
        if r["has_timestamps"]:
            print("    punct/100=%.2f  chars/min=%.1f  segs/min=%.1f  maxgap=%.2fs  tail=%.2fs  overrun=%.2fs"
                  % (r["punct_per_100"], r["chars_per_min"], r["segs_per_min"],
                     r["max_gap_s"], r["tail_gap_s"], r["tail_overrun_s"]))
        else:
            print("    punct/100=%.2f  chars/min=%.1f | maxgap/tail/overrun = N/A (no timestamps)"
                  % (r["punct_per_100"], r["chars_per_min"]))
        if r["note"]:
            print("    [note] " + r["note"])
        for x in r["red"]:
            print("    [RED] " + x)
        for x in r["warn"]:
            print("    [warn] " + x)
        if not r["red"] and not r["warn"]:
            print("    OK (coarse checks only; still inspect head/tail manually)")
    print()
    print("%d file(s), %d with RED flags. RED = needs human review, not proven wrong;"
          % (len(rows), n_red))
    print("manually inspect the first/last 30 s and any long-gap interval.")
    sys.exit(1 if n_red else 0)


if __name__ == "__main__":
    main()
