# -*- coding: utf-8 -*-
"""Quality check for transcription JSON files (read-only).

Implements the four coarse metrics plus the three mandatory hard checks:
  - head: prompt echo / opening lost in the first 30 s
  - tail: last segment ends after audio duration, or closing boilerplate
  - gap: any adjacent-segment gap > 8 s

Also normalizes with NFKC before counting punctuation, because U+FE50 / U+FE51
variants otherwise make a punctuated transcript look like it has zero punctuation.

Expected JSON shape (faster-whisper style):
    {"duration": 115.24, "segments": [{"s": 0.0, "e": 3.2, "t": "..."}, ...]}
Also accepts {"start","end","text"} keys and a top-level "text" fallback.
"""
import argparse
import glob
import json
import os
import re
import sys
import unicodedata

HEAD_KEYWORDS = ["请使用简体中文", "并加上标点符号", "请不吝点赞", "订阅",
                 "转发", "打赏", "明镜", "字幕由"]
TAIL_KEYWORDS = ["谢谢大家", "感谢观看", "请不吝点赞", "订阅", "转发",
                 "打赏", "明镜", "字幕由"]
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
        return {"file": os.path.basename(path), "error": "no segments"}
    dur = float(d.get("duration") or segs[-1]["e"])
    text = "".join(s["t"] for s in segs)
    norm = unicodedata.normalize("NFKC", text)
    minutes = dur / 60.0 if dur else 0

    gaps = [(segs[i + 1]["s"] - segs[i]["e"], segs[i]["e"], segs[i + 1]["s"])
            for i in range(len(segs) - 1)]
    max_gap, gap_at = (max(gaps)[0], max(gaps)[1:]) if gaps else (0.0, (0, 0))
    tail_gap = dur - segs[-1]["e"]
    overrun = segs[-1]["e"] - dur

    head = [s for s in segs if s["s"] < 30]
    tail = segs[-2:]
    hits = []
    for kw in HEAD_KEYWORDS:
        if any(kw in s["t"] for s in head):
            hits.append("head keyword: " + kw)
    for kw in TAIL_KEYWORDS:
        if any(kw in s["t"] for s in tail):
            hits.append("tail keyword: " + kw)

    red = []
    if overrun > 0.5:
        red.append("TAIL OVERRUN +%.2fs (last segment ends after audio)" % overrun)
    if tail_gap < -0.5:
        red.append("NEGATIVE TAIL GAP (hallucinated tail, not a pass)")
    if max_gap > gap_limit:
        red.append("LONG GAP %.2fs at %.1f-%.1fs" % (max_gap, gap_at[0], gap_at[1]))
    red += hits

    cpm = len(norm) / minutes if minutes else 0
    spm = len(segs) / minutes if minutes else 0
    warn = []
    if not (DEFAULT_CPM[0] <= cpm <= DEFAULT_CPM[1]):
        warn.append("char density %.1f/min outside %s" % (cpm, DEFAULT_CPM))
    if tail_gap > tail_limit:
        warn.append("tail gap %.2fs > %.1fs (possible truncation)" % (tail_gap, tail_limit))
    if not (DEFAULT_SPM[0] <= spm <= DEFAULT_SPM[1]):
        warn.append("segments %.1f/min outside %s (check split style)" % (spm, DEFAULT_SPM))

    return {
        "file": os.path.basename(path), "duration": round(dur, 2),
        "chars": len(norm), "segments": len(segs),
        "punct_per_100": round(len(re.findall(PUNCT, norm)) / max(len(norm), 1) * 100, 2),
        "chars_per_min": round(cpm, 1), "segs_per_min": round(spm, 1),
        "max_gap_s": round(max_gap, 2), "tail_gap_s": round(tail_gap, 2),
        "tail_overrun_s": round(overrun, 2),
        "red": red, "warn": warn,
        "note": "no word-level timestamps" if d.get("timestamps") is False else "",
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
            rows.append({"file": p, "error": "not found"})
            continue
        try:
            rows.append(check(p, a.gap, a.tail))
        except Exception as e:
            rows.append({"file": os.path.basename(p), "error": str(e)})

    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    n_red = 0
    for r in rows:
        print("---", r["file"], end=" ")
        if r.get("error"):
            print("ERROR:", r["error"])
            continue
        print("(%.2fs, %d chars, %d segs)" % (r["duration"], r["chars"], r["segments"]))
        print("    punct/100=%.2f  chars/min=%.1f  segs/min=%.1f  maxgap=%.2fs  tail=%.2fs  overrun=%.2fs"
              % (r["punct_per_100"], r["chars_per_min"], r["segs_per_min"],
                 r["max_gap_s"], r["tail_gap_s"], r["tail_overrun_s"]))
        for x in r["red"]:
            print("    [RED] " + x)
        for x in r["warn"]:
            print("    [warn] " + x)
        if not r["red"] and not r["warn"]:
            print("    OK (coarse checks only; still inspect head/tail manually)")
        n_red += 1 if r["red"] else 0
    print()
    print("%d file(s), %d with RED flags. Coarse metrics never prove correctness;"
          % (len(rows), n_red))
    print("manually inspect the first/last 30 s and any long-gap interval.")
    sys.exit(1 if n_red else 0)


if __name__ == "__main__":
    main()
