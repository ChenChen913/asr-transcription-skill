#!/usr/bin/env python3
"""Put two readings of the same clip side by side - and only flag the gaps.

The idea
--------
Speech recognition and on-screen subtitles fail in different directions.

    heard  "TOPK出牌, RUNK金牌, 大麻仙"     (correct: 蒙逼了)
    seen   "Top K粗排, Rerank精排, 大模型"  (correct: Rerank)

Neither source is right, and neither is wrong often enough to throw away. What
a human actually needs is not a third "merged" transcript - that would blend two
unreliable things into one unverifiable thing - but a short list of the places
where the two disagree, so attention goes there and nowhere else.

So this script produces a *report*: aligned pairs, a similarity score, the
tokens that exist on only one side, and a summary that says whether the two
sources are even talking about the same stretch of time.

It never rewrites either transcript, and it never elects a winner. Editing a
transcript is a judgement call that needs the audio; the point here is to make
that judgement cheap.
"""
import argparse
import json
import math
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_subs import punct_per_100, write_pair   # noqa: F401  (schema parity)

from difflib import SequenceMatcher

TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#._-]*|\d+(?:\.\d+)?")
PUNCT_BODY = r"，。！？、,.!?;；:：\"'“”‘’（）()\[\]【】…—\-~～"


CJK_CHARS = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def cjk_share(s):
    """Fraction of characters written in a CJK script.

    Only used to explain a near-zero score: on 至暗时刻 the heard side was
    English and the seen side Chinese subtitles, so every pair "disagreed"
    even though both were perfectly good transcripts of the same 10 minutes.
    Comparing them is meaningless, and the old message told the user to check
    whether they were the same file - which sent them looking for the wrong bug.
    """
    body = [c for c in str(s or "") if c.isalpha()]
    if not body:
        return 0.0
    return sum(1 for c in body if CJK_CHARS.match(c)) / float(len(body))


def language_mismatch(heard_text, seen_text):
    """True when one side is Latin-dominant and the other CJK-dominant.

    Both sides need 200+ letters: a short Chinese line with three English
    terms inside it can otherwise look "Latin-dominant" by accident. Over a
    whole transcript that averages out (至暗时刻: heard 4% CJK, seen 96%).
    """
    if min(len(str(heard_text or "")), len(str(seen_text or ""))) < 200:
        return False
    h, s = cjk_share(heard_text), cjk_share(seen_text)
    return (h < 0.20 and s > 0.60) or (s < 0.20 and h > 0.60)


def norm_key(s):
    """Comparison form only: no case, spaces, or punctuation."""
    s = re.sub("[%s]" % PUNCT_BODY, "", str(s or "")).lower()
    return re.sub(r"\s+", "", s)


def tokens(s):
    """Latin words and numbers, with internal spacing removed first.

    "Top K" and "TOPK" are the same product to a reader, and a naive scan calls
    them different tokens; on a Chinese transcript that false positive would
    fire on every jargon word. Squashing whitespace first removes it without
    weakening the real signal ("RAG" vs "RAW" still differs).
    """
    squashed = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s or "")))
    return [t.lower().rstrip(".") for t in TOKEN.findall(squashed)]


def ratio(a, b):
    ka, kb = norm_key(a), norm_key(b)
    if not ka and not kb:
        return 1.0
    if not ka or not kb:
        return 0.0
    return SequenceMatcher(None, ka, kb).ratio()


def covered(a, b):
    """How much of the *shorter* side can be found in the longer one.

    Cue boundaries and ASR boundaries do not agree: one subtitle often covers
    two spoken sentences, so an ASR segment is compared against a cue that also
    carries its neighbour. The character ratio punishes that twice and the pair
    lands in the review list looking like a contradiction when nothing is
    actually wrong. Coverage separates "断句不同" from "说法不同".
    """
    ka, kb = norm_key(a), norm_key(b)
    short, long_ = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    if not short:
        return 0.0
    pool = {}
    for ch in long_:
        pool[ch] = pool.get(ch, 0) + 1
    hit = 0
    for ch in short:
        if pool.get(ch, 0) > 0:
            pool[ch] -= 1
            hit += 1
    return hit / float(len(short))


def validate_timeline(items):
    """Return a readable reason to reject this timeline, or None.

    qc_check.py established the rule for this project: timestamps that *parse*
    are not necessarily a valid timeline. merge_sources used to repair them
    quietly - `max(e, s)` clamped a backwards segment and `sort()` reordered
    out-of-order input - so a broken file produced a confident-looking report.
    Neither is done here any more: a report built on a repaired timeline is
    not evidence about the original files.

    Overlaps are allowed. They are a REVIEW in qc_check (subtitle tracks do
    overlap), and alignment already has best_lag() for genuine offsets. Only
    conditions qc_check calls an ERROR are refused here.
    """
    def number(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    prev_s = None
    for i, x in enumerate(items):
        if not isinstance(x, dict):
            return "segment[%d] is not an object" % i
        s, e = x.get("s"), x.get("e")
        if not number(s) or not number(e):
            return "segment[%d] has a missing or non-numeric s/e" % i
        s, e = float(s), float(e)
        if not (math.isfinite(s) and math.isfinite(e)):
            return "segment[%d] has a non-finite timestamp" % i
        if s < 0:
            return "segment[%d] start < 0 (%.2f)" % (i, s)
        if e < s:
            return "segment[%d] end < start (%.2f < %.2f)" % (i, e, s)
        if prev_s is not None and s < prev_s - 0.001:
            return ("segments out of order at index %d (%.2f < %.2f)"
                    % (i, s, prev_s))
        prev_s = s
    return None


def load(path, side):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    segs = data.get("segments")
    if not isinstance(segs, list) or not segs:
        raise SystemExit("[ERROR] %s has no usable segments" % path)
    problem = validate_timeline(segs)
    if problem:
        raise SystemExit(
            "[ERROR] %s has an invalid timeline: %s\n"
            "  Run: python scripts/qc_check.py \"%s\"\n"
            "  merge_sources does not clamp, reorder or otherwise repair "
            "timestamps; fix the source first." % (path, problem, path))
    clean = []
    for x in segs:
        t = str(x.get("t") or x.get("text") or "").strip()
        if t:
            # Keep the values as read. Sorting here used to hide out-of-order
            # input instead of reporting it.
            clean.append({"s": float(x["s"]), "e": float(x["e"]), "t": t})
    if not clean:
        raise SystemExit("[ERROR] %s has no segment with text" % path)
    try:
        dur = float(data.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return {"path": path, "tag": data.get("tag") or os.path.splitext(os.path.basename(path))[0],
            "engine": data.get("engine") or side, "duration": dur, "segs": clean}


def overlap(a, b):
    return max(0.0, min(a["e"], b["e"]) - max(a["s"], b["s"]))


def align(heard, seen):
    """Each seen cue belongs to the heard segment that contains its midpoint.

    Overlap-based assignment smears text across neighbours: an ASR segment can
    be 3 s long while cues are 2 s, so one cue legitimately straddles two
    segments and would be quoted twice. The cue's own midpoint is the one
    unambiguous fact, so it decides - and a cue landing in a silence keeps no
    owner, which is exactly the 'seen-only' case worth reporting.
    """
    buckets = {i: [] for i in range(len(heard))}
    orphans = []
    for cue in seen:
        mid = (cue["s"] + cue["e"]) / 2.0
        best, best_dist = None, float("inf")
        for i, h in enumerate(heard):
            inside = h["s"] <= mid <= h["e"]
            # Half-open on purpose: a cue ending exactly on a boundary belongs
            # to the segment it starts in, not to both.
            if inside and (mid < h["e"] or i == len(heard) - 1):
                best, best_dist = i, 0.0
                break
            dist = min(abs(mid - h["s"]), abs(mid - h["e"]))
            if dist < best_dist:
                best, best_dist = i, dist
        if best is None or best_dist > 1.2:
            orphans.append(cue)
        else:
            buckets[best].append(cue)
    for i in buckets:
        buckets[i].sort(key=lambda c: c["s"])
    return buckets, orphans


def mean_ratio_at(heard, seen, lag, sample=4):
    """Mean similarity when the seen side is shifted by `lag` seconds."""
    shifted = [{"s": c["s"] + lag, "e": c["e"] + lag, "t": c["t"]} for c in seen]
    buckets, _ = align(heard, shifted)
    got = []
    for i in range(0, len(heard), sample):
        cues = buckets[i]
        if cues:
            got.append(ratio(heard[i]["t"], " ".join(c["t"] for c in cues)))
    return sum(got) / len(got) if got else 0.0


def best_lag(heard, seen, cands=(-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0)):
    """Does the subtitle track sit later or earlier than the speech?

    Edited short videos routinely show a line *after* it is spoken. Reporting
    that as a wall of conflicts would bury the one thing worth knowing, so the
    offset is measured first and the alignment is done with it applied.
    """
    base = mean_ratio_at(heard, seen, 0.0)
    best, best_r = 0.0, base
    for lag in cands:
        if lag == 0.0:
            continue
        r = mean_ratio_at(heard, seen, lag)
        if r > best_r + 0.02:
            best, best_r = lag, r
    return best, round(base, 3), round(best_r, 3)


def compare(h, cues, near, agree):
    heard_text = h["t"]
    seen_text = " ".join(c["t"] for c in cues).strip()
    note = ""
    if not seen_text:
        flag, r = "heard-only", None
    else:
        r = round(ratio(heard_text, seen_text), 3)
        flag = "agree" if r >= agree else ("near" if r >= near else "differ")
        # One cue spanning two spoken sentences is a segmentation difference,
        # not a contradiction. Say so instead of flagging it red. The 2-char
        # floor only keeps a lone "的" from explaining itself away.
        if flag == "differ" and min(len(norm_key(heard_text)),
                                    len(norm_key(seen_text))) >= 2:
            if covered(heard_text, seen_text) >= 0.8:
                flag, note = "near", "字幕整条盖住了这段，只是断句不同"
    ht, st = set(tokens(heard_text)), set(tokens(seen_text))
    only_heard, only_seen = sorted(ht - st), sorted(st - ht)
    # Character similarity is the wrong ruler for the errors that matter most:
    # one wrong letter inside a Chinese sentence still scores 0.95, yet "RAG"
    # heard as "RAW" is the whole reason to read this report. A differing
    # number or term therefore promotes the pair into the review list.
    #
    # `seen_text` guards the promotion: with no subtitle on screen at all there
    # is nothing to disagree with, and a number in the audio ("N 是 5 还是 10")
    # would otherwise relabel a plain coverage gap as a contradiction.
    if seen_text and (only_heard or only_seen):
        numeric = any(t.replace(".", "").isdigit() for t in only_heard + only_seen)
        if numeric:
            flag, note = "differ", ""   # a number is never "close enough"
        elif flag == "agree":
            flag = "near"
    return {"s": round(h["s"], 2), "e": round(h["e"], 2),
            "heard": heard_text, "seen": seen_text, "ratio": r, "flag": flag,
            "note": note,
            "only_heard": only_heard, "only_seen": only_seen}


def mmss(t):
    t = max(0.0, float(t))
    return "%d:%02d" % (int(t // 60), int(round(t % 60)))


def build_md(meta, pairs, orphans, verdict):
    out = ["# 听看对照：%s\n\n" % meta["title"],
           "- 听（%s，%s）：%d 段 / %d 字\n"
           % (meta["heard_tag"], meta["heard_engine"],
              meta["heard_segs"], meta["heard_chars"]),
           "- 看（%s，%s）：%d 段 / %d 字\n"
           % (meta["seen_tag"], meta["seen_engine"],
              meta["seen_segs"], meta["seen_chars"]),
           "- 时长 %.2fs；逐段相似度中位数 %s，均值 %s\n"
           % (meta["duration"], meta["median"], meta["mean"])]
    if meta["lag"]:
        # lag is what was added to the seen side to line it up: a negative lag
        # means the subtitle arrived later than the speech it belongs to.
        out.append("- 时间轴：字幕整体比语音%s %.1fs，已按此对齐（两边原始时间都没改）\n"
                   % ("早" if meta["lag"] > 0 else "晚", abs(meta["lag"])))
    else:
        out.append("- 时间轴：两路起点一致，无需偏移\n")
    out.append("\n> 这份文件**不是改好的稿子**，是一张对照表：只列出两路说法不一致、"
               "以及只有一路有内容的地方。谁对谁错要回到音频和画面判断，脚本不做裁决。\n\n")

    counts = meta["counts"]
    out.append("## 结论\n\n")
    out.append("| 判断 | 段数 | 含义 |\n|---|---|---|\n")
    out.append("| 一致 | %d | 相似度 ≥ %.2f，可以不看 |\n" % (counts["agree"], meta["agree"]))
    out.append("| 接近 | %d | %.2f–%.2f，多半是同音字或断句差异 |\n"
               % (counts["near"], meta["near"], meta["agree"]))
    out.append("| 不一致 | %d | < %.2f，必须人看 |\n"
               % (counts["differ"], meta["near"]))
    out.append("| 只有语音 | %d | 这段有说话，画面没有字幕 |\n" % counts["heard-only"])
    out.append("| 只有字幕 | %d | 字幕有字，语音里没有对应内容 |\n\n" % counts["seen-only"])
    out.append("%s\n\n" % verdict)

    flagged = [p for p in pairs if p["flag"] in ("differ", "near")]
    flagged.sort(key=lambda p: (p["ratio"] if p["ratio"] is not None else 0))
    out.append("## 需要核对（%d 段，按可疑程度排序）\n\n" % len(flagged))
    if not flagged:
        out.append("两路完全对得上，没有需要人工介入的地方。\n\n")
    for p in flagged[:meta["limit"]]:
        out.append("### %s–%s  相似度 %.2f  %s\n"
                   % (mmss(p["s"]), mmss(p["e"]), p["ratio"] or 0,
                      "⚠ 不一致" if p["flag"] == "differ" else "≈ 接近"))
        out.append("- 听：%s\n" % p["heard"])
        out.append("- 看：%s\n" % p["seen"])
        if p.get("note"):
            out.append("- 说明：%s\n" % p["note"])
        if p["only_heard"]:
            out.append("- 只在听里出现：%s\n" % "、".join(p["only_heard"][:8]))
        if p["only_seen"]:
            out.append("- 只在看里出现：%s\n" % "、".join(p["only_seen"][:8]))
        out.append("\n")
    if len(flagged) > meta["limit"]:
        out.append("（还有 %d 段未列出，见同名 .json）\n\n"
                   % (len(flagged) - meta["limit"]))

    heard_only = [p for p in pairs if p["flag"] == "heard-only"]
    if heard_only:
        out.append("## 只有语音的时段（%d 段）\n\n" % len(heard_only))
        spans = ["%s–%s" % (mmss(p["s"]), mmss(p["e"])) for p in heard_only[:24]]
        out.append("、".join(spans))
        if len(heard_only) > 24:
            out.append(" 等 %d 段" % len(heard_only))
        out.append("\n\n这些段落画面上没有字幕。正常（字幕本来就不覆盖每一秒），"
                   "但如果某段正是关键结论，值得单独听一遍。\n\n")

    if orphans:
        out.append("## 只有字幕的片段（%d 段）\n\n" % len(orphans))
        for c in orphans[:20]:
            start = c.get("src_s", c["s"])
            out.append("- %s–%s  %s\n" % (mmss(start),
                                           mmss(start + (c["e"] - c["s"])), c["t"]))
        if len(orphans) > 20:
            out.append("- （其余 %d 段见 .json）\n" % (len(orphans) - 20))
        out.append("\n语音里没有对应内容，通常是标题、水印，或字幕比声音早出现。\n")
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Align a heard transcript with a seen (OCR / subtitle) one "
                    "and report only where they disagree.")
    ap.add_argument("--heard", "--a", dest="heard", required=True,
                    help="JSON from transcribe.py (or any side you call 'heard')")
    ap.add_argument("--seen", "--b", dest="seen", required=True,
                    help="JSON from ocr_subs.py or extract_subs.py")
    ap.add_argument("--out", required=True, help="path of the .md report")
    ap.add_argument("--agree", type=float, default=0.85,
                    help="similarity at or above this counts as agreement (default 0.85)")
    ap.add_argument("--near", type=float, default=0.55,
                    help="similarity at or above this is 'near' (default 0.55)")
    ap.add_argument("--limit", type=int, default=40,
                    help="max flagged blocks in the MD (default 40; JSON has all)")
    ap.add_argument("--title", default=None, help="report title, defaults to the heard tag")
    a = ap.parse_args()

    heard = load(a.heard, "heard")
    seen = load(a.seen, "seen")

    lag, base_r, best_r = best_lag(heard["segs"], seen["segs"])
    if lag:
        seen["segs"] = [dict(c, s=c["s"] + lag, e=c["e"] + lag, src_s=c["s"])
                        for c in seen["segs"]]
    buckets, orphans = align(heard["segs"], seen["segs"])

    pairs = [compare(h, buckets[i], a.near, a.agree)
             for i, h in enumerate(heard["segs"])]
    diff_pairs = [p for p in pairs if p["ratio"] is not None]
    ratios = sorted(p["ratio"] for p in diff_pairs)
    median = ratios[len(ratios) // 2] if ratios else 0.0
    mean = round(sum(ratios) / len(ratios), 3) if ratios else 0.0

    counts = {k: sum(1 for p in pairs if p["flag"] == k)
              for k in ("agree", "near", "differ", "heard-only")}
    counts["seen-only"] = len(orphans)

    duration = max([heard["duration"], seen["duration"]]
                   + [p["e"] for p in pairs] + [c["e"] for c in orphans])
    heard_chars = sum(len(p["heard"]) for p in pairs)
    seen_chars = sum(len(p["seen"]) for p in pairs)

    # A report full of low scores usually means misalignment, not disagreement.
    # Rule out different languages first: that is a reason the comparison is
    # invalid, not a reason either transcript is wrong.
    heard_text = "".join(c["t"] for c in heard["segs"])
    seen_text = "".join(c["t"] for c in seen["segs"])
    heard_cjk = cjk_share(heard_text)
    seen_cjk = cjk_share(seen_text)
    different_scripts = language_mismatch(heard_text, seen_text)
    if len(diff_pairs) >= 8 and mean < 0.30 and different_scripts:
        verdict = ("⚠ 两路不是同一种文字（听的一方 CJK 占 %.0f%%，看的一方占 %.0f%%）："
                   "例如英文原声配中文字幕。这不是错稿，是两路根本无法逐字对照；"
                   "请各自单独使用，不要按下面的分歧去改任何一份。"
                   % (heard_cjk * 100, seen_cjk * 100))
    elif len(diff_pairs) >= 8 and mean < 0.30:
        verdict = ("⚠ 平均相似度只有 %.2f：两路可能不是同一段内容，或者时间轴起点不同。"
                   "先确认两边是不是同一个文件，再看下面的对照。" % mean)
    elif counts["differ"] == 0 and counts["near"] == 0:
        verdict = "✅ 逐段都对得上，两路互为印证。可以直接用听的那份进行精编。"
    elif mean >= 0.70:
        verdict = ("✅ 整体吻合（均值 %.2f）。%d 段不一致集中在少数位置，"
                   "大概率是同音字、专名或数字，重点看下面这几段即可。" % (mean, counts["differ"]))
    else:
        verdict = ("整体只有 %.2f 相似：字幕可能被大幅改写（不是逐字稿），"
                   "也可能有段落错位。先看“只有语音”的时段是否成片出现。" % mean)

    meta = {"title": a.title or heard["tag"], "duration": duration,
            "heard_tag": heard["tag"], "heard_engine": heard["engine"],
            "seen_tag": seen["tag"], "seen_engine": seen["engine"],
            "heard_segs": len(heard["segs"]), "seen_segs": len(seen["segs"]),
            "heard_chars": heard_chars, "seen_chars": seen_chars,
            "counts": counts, "median": round(median, 3), "mean": mean,
            "agree": a.agree, "near": a.near, "limit": max(a.limit, 1),
            "lag": lag}

    stem = os.path.splitext(a.out)[0]
    md_path = stem + ".md"
    json_path = stem + ".json"
    os.makedirs(os.path.dirname(os.path.abspath(md_path)), exist_ok=True)
    data = {"title": meta["title"], "duration": round(duration, 2),
            "heard": {"file": os.path.basename(heard["path"]), "tag": heard["tag"],
                      "engine": heard["engine"], "segments": len(heard["segs"]),
                      "chars": heard_chars},
            "seen": {"file": os.path.basename(seen["path"]), "tag": seen["tag"],
                     "engine": seen["engine"], "segments": len(seen["segs"]),
                     "chars": seen_chars},
            "counts": counts, "ratio_median": meta["median"], "ratio_mean": mean,
            "timing": {"lag_s": lag, "ratio_at_zero": base_r, "ratio_at_lag": best_r},
            "thresholds": {"agree": a.agree, "near": a.near},
            "pairs": pairs,
            "seen_only": [{"s": round(c["s"], 2), "e": round(c["e"], 2), "t": c["t"]}
                          for c in orphans]}
    write_pair(json_path, data, md_path, build_md(meta, pairs, orphans, verdict))

    print("heard   : %s  %d segs / %d chars" % (heard["tag"], len(heard["segs"]), heard_chars))
    print("seen    : %s  %d segs / %d chars" % (seen["tag"], len(seen["segs"]), seen_chars))
    print("aligned : %d pairs | agree %d | near %d | differ %d | heard-only %d | seen-only %d"
          % (len(pairs), counts["agree"], counts["near"], counts["differ"],
             counts["heard-only"], counts["seen-only"]))
    print("similar : median %.3f | mean %.3f" % (median, mean))
    print("report  : %s" % os.path.abspath(md_path))
    print(verdict)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure") and not sys.stdout.isatty():
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    main()
