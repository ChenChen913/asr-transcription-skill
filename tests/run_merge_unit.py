# -*- coding: utf-8 -*-
"""Offline unit tests for scripts/merge_sources.py.

Pure Python: alignment, similarity flags, token diff, lag detection and the
report body. No audio, no video, no OCR, no network.

Run:  python tests/run_merge_unit.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC = importlib.util.spec_from_file_location(
    "merge_module", os.path.join(ROOT, "scripts", "merge_sources.py"))
mg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mg)

checks = [0]
failures = []


def ok(cond, label):
    checks[0] += 1
    if not cond:
        failures.append(label)


def eq(got, want, label):
    ok(got == want, "%s (got %r, want %r)" % (label, got, want))


def close(got, want, tol, label):
    ok(abs(got - want) <= tol, "%s (got %r, want %r+-%r)" % (label, got, want, tol))


def seg(s, e, t):
    return {"s": s, "e": e, "t": t}


# ------------------------------------------------------------------- basics ---

def case_norm_and_tokens():
    eq(mg.norm_key("Top K，你好"), "topk你好", "norm_key drops punctuation and case")
    eq(mg.tokens("RAG 和 TopK，K=50"), ["rag", "topk", "k", "50"],
       "latin words and numbers are extracted")
    eq(mg.tokens("没有英文"), [], "pure Chinese yields no tokens")


def case_ratio():
    eq(mg.ratio("你好世界", "你好世界"), 1.0, "identical -> 1.0")
    eq(mg.ratio("", ""), 1.0, "both empty -> 1.0")
    eq(mg.ratio("", "有字"), 0.0, "one empty -> 0.0")
    close(mg.ratio("你好世界", "你好"), 0.667, 0.01, "prefix overlap is partial")


# ------------------------------------------------------------------ aligning ---

def case_align_by_midpoint():
    heard = [seg(0, 4, "第一段"), seg(4, 8, "第二段")]
    seen = [seg(0.5, 2.0, "第一段上"), seg(5.0, 7.0, "第二段上")]
    buckets, orphans = mg.align(heard, seen)
    eq([c["t"] for c in buckets[0]], ["第一段上"], "cue lands in the first segment")
    eq([c["t"] for c in buckets[1]], ["第二段上"], "cue lands in the second segment")
    eq(orphans, [], "nothing orphaned")


def case_align_no_double_quoting():
    # A 2 s cue straddling an ASR boundary is quoted once, by its midpoint.
    heard = [seg(0, 3, "甲"), seg(3, 6, "乙")]
    seen = [seg(2.0, 4.0, "跨界的一句话")]
    buckets, _ = mg.align(heard, seen)
    eq(len(buckets[0]) + len(buckets[1]), 1, "a straddling cue is assigned once")
    eq(len(buckets[1]), 1, "and to the segment that owns its midpoint")


def case_align_orphan_in_silence():
    heard = [seg(0, 2, "甲"), seg(10, 12, "乙")]
    seen = [seg(5.0, 6.0, "字幕还在说话")]
    buckets, orphans = mg.align(heard, seen)
    eq([c["t"] for c in orphans], ["字幕还在说话"], "cue in a gap becomes seen-only")
    eq(sum(len(v) for v in buckets.values()), 0, "and is not quoted anywhere")


def case_align_sorts_cues():
    heard = [seg(0, 10, "甲")]
    seen = [seg(6, 7, "后"), seg(1, 2, "前")]
    buckets, _ = mg.align(heard, seen)
    eq([c["t"] for c in buckets[0]], ["前", "后"], "cues are kept in time order")


# ----------------------------------------------------------------- comparing ---

def case_flags():
    h = seg(0, 3, "面试官问你Rag中的Top K和Top N")
    p = mg.compare(h, [seg(0, 3, "面试官问你Rag中的Top K和Top N")], 0.55, 0.85)
    eq(p["flag"], "agree", "identical text agrees")
    eq(p["ratio"], 1.0, "ratio is 1.0")

    p = mg.compare(h, [seg(0, 3, "面试官问你RAW中的TOPK和TOPN")], 0.55, 0.85)
    ok(p["flag"] in ("near", "differ"), "spelling drift is flagged")
    ok("raw" in p["only_seen"] and "rag" in p["only_heard"],
       "token diff names both spellings")

    p = mg.compare(h, [seg(0, 3, "完全不相干的一句话内容")], 0.55, 0.85)
    eq(p["flag"], "differ", "unrelated text is differ")

    p = mg.compare(h, [], 0.55, 0.85)
    eq(p["flag"], "heard-only", "no cue -> heard-only")
    eq(p["ratio"], None, "no ratio when there is nothing to compare")


def case_term_promotion():
    # The reason this report exists: a single wrong letter scores ~0.95.
    h = seg(0, 3, "面试官问你RAG中的Top K和Top N")
    p = mg.compare(h, [seg(0, 3, "面试官问你RAW中的Top K和Top N")], 0.55, 0.85)
    ok(p["ratio"] >= 0.85, "character similarity alone would call this agreement")
    eq(p["flag"], "near", "a differing term still reaches the review list")
    # ...and a differing number is the strongest signal of all.
    p = mg.compare(seg(0, 3, "我们K是50"), [seg(0, 3, "我们K是60")], 0.55, 0.85)
    eq(p["flag"], "differ", "a differing number is differ even at high similarity")
    # Spacing inside a term is not a difference.
    p = mg.compare(seg(0, 3, "TopK和TopN"), [seg(0, 3, "Top K和Top N")], 0.55, 0.85)
    eq(p["only_heard"] + p["only_seen"], [], "TopK and Top K are one token")
    # A number in a line with no subtitle at all is a coverage gap, not a
    # contradiction: promotion must not overwrite "heard-only".
    p = mg.compare(seg(0, 3, "你的N到底是5还是10"), [], 0.55, 0.85)
    eq(p["flag"], "heard-only",
       "a number with no subtitle on screen stays heard-only")


def case_compare_numbers():
    p = mg.compare(seg(0, 2, "我们K是50"), [seg(0, 2, "我们K是60")], 0.55, 0.85)
    ok("50" in p["only_heard"] and "60" in p["only_seen"],
       "a differing number is surfaced on both sides")


def case_cue_covers_segment():
    # One subtitle cue usually covers two spoken sentences. Comparing half a
    # sentence against the whole cue must read as "断句不同", not "说错了".
    p = mg.compare(seg(0, 3, "获得最大的招回率"),
                   [seg(0, 3, "Topk要用最小的值获得最大的召回率")], 0.55, 0.85)
    ok(p["ratio"] < 0.55, "character ratio alone calls this a contradiction")
    eq(p["flag"], "near", "a cue that contains the segment is only 'near'")
    ok("断句" in p["note"], "and the report says why")
    # Same for a short tail fragment, which scores even worse.
    p = mg.compare(seg(0, 3, "他问"), [seg(0, 3, "假设用户他问")], 0.55, 0.85)
    eq(p["flag"], "near", "a short fragment inside the cue is not a conflict")
    # The escape hatch must not hide a real disagreement sitting in the cue.
    p = mg.compare(seg(0, 3, "我们K是50"),
                   [seg(0, 3, "我们K是60然后取最小的值")], 0.55, 0.85)
    eq(p["flag"], "differ", "a differing number still wins over the note")
    eq(p["note"], "", "and no segmentation note is attached")


# -------------------------------------------------------------------- timing ---

def case_lag_detection():
    # Ten 1 s segments; each subtitle appears 1 s after the line is spoken.
    abc = "甲乙丙丁戊己庚辛壬癸"
    heard = [seg(i, i + 1, abc[i]) for i in range(10)]
    seen = [seg(i + 1, i + 1.5, abc[i]) for i in range(10)]
    eq(mg.mean_ratio_at(heard, seen, 0.0) < 0.1, True,
       "without correction nothing lines up")
    close(mg.mean_ratio_at(heard, seen, -1.0), 1.0, 0.001,
          "shifting the subtitle back one second lines it up")
    lag, base, best = mg.best_lag(heard, seen)
    eq(lag, -1.0, "the late subtitle is detected as -1 s")
    ok(best > base, "corrected alignment scores higher")


def case_lag_none():
    heard = [seg(i, i + 1, "第%d句" % i) for i in range(10)]
    seen = [seg(i + 0.2, i + 0.8, "第%d句" % i) for i in range(10)]
    lag, _, _ = mg.best_lag(heard, seen)
    eq(lag, 0.0, "a well-synced clip reports no offset")


# --------------------------------------------------------------------- load ---

def case_load(tmp):
    good = os.path.join(tmp, "good.json")
    with open(good, "w", encoding="utf-8") as fh:
        json.dump({"duration": 10, "segments": [
            {"s": 0, "e": 2, "t": "甲"}, {"s": 2, "e": 4, "t": "乙"}]}, fh, ensure_ascii=False)
    got = mg.load(good, "heard")
    eq(len(got["segs"]), 2, "segments loaded")
    eq(got["engine"], "heard", "missing engine falls back to the side name")
    eq(got["tag"], "good", "missing tag falls back to the file stem")

    alt = os.path.join(tmp, "alt.json")
    with open(alt, "w", encoding="utf-8") as fh:
        json.dump({"segments": [{"start": 0, "end": 1, "text": "甲"}]}, fh, ensure_ascii=False)
    try:
        mg.load(alt, "seen")
        ok(False, "an unknown segment schema must abort, not read as empty")
    except SystemExit:
        ok(True, "an unknown segment schema aborts loudly")

    bad = os.path.join(tmp, "bad.json")
    with open(bad, "w", encoding="utf-8") as fh:
        json.dump({"segments": []}, fh)
    try:
        mg.load(bad, "heard")
        ok(False, "empty segments must abort")
    except SystemExit:
        ok(True, "empty segments abort with a message")


# ------------------------------------------------------------------- report ---

def case_mmss():
    eq(mg.mmss(0), "0:00", "zero")
    eq(mg.mmss(75.4), "1:15", "rounds to the nearest second")
    eq(mg.mmss(3599), "59:59", "stays in minutes past an hour")


def case_md():
    meta = {"title": "测试", "duration": 30.0,
            "heard_tag": "a", "heard_engine": "asr", "heard_segs": 2, "heard_chars": 20,
            "seen_tag": "b", "seen_engine": "ocr", "seen_segs": 2, "seen_chars": 20,
            "counts": {"agree": 1, "near": 0, "differ": 1, "heard-only": 0,
                       "seen-only": 1},
            "median": 0.7, "mean": 0.7, "agree": 0.85, "near": 0.55,
            "limit": 40, "lag": -2.0}
    pairs = [{"s": 0, "e": 2, "ratio": 1.0, "flag": "agree", "heard": "甲", "seen": "甲",
              "only_heard": [], "only_seen": []},
             {"s": 2, "e": 4, "ratio": 0.3, "flag": "differ", "heard": "听到的",
              "seen": "看到的", "only_heard": ["raw"], "only_seen": ["rag"]}]
    orphans = [{"s": 10, "e": 11, "t": "只有字幕"}]
    text = "".join(mg.build_md(meta, pairs, orphans, "测试结论"))
    ok("字幕整体比语音晚 2.0s" in text, "a negative lag is reported as late")
    ok("| 一致 | 1 |" in text, "counts table lists agreement")
    ok("### 0:02–0:04" in text, "flagged block carries its time")
    ok("- 听：听到的" in text and "- 看：看到的" in text, "both readings are quoted")
    ok("只在听里出现：raw" in text, "token diff is listed")
    ok("只有字幕的片段（1 段）" in text, "seen-only section appears")
    ok("测试结论" in text, "verdict is carried through")
    ok("### 0:00–0:02" not in text, "agreeing pairs stay out of the review list")


def case_md_limits():
    meta = {"title": "t", "duration": 99.0,
            "heard_tag": "a", "heard_engine": "asr", "heard_segs": 60, "heard_chars": 60,
            "seen_tag": "b", "seen_engine": "ocr", "seen_segs": 60, "seen_chars": 60,
            "counts": {"agree": 0, "near": 60, "differ": 0, "heard-only": 0,
                       "seen-only": 0},
            "median": 0.6, "mean": 0.6, "agree": 0.85, "near": 0.55,
            "limit": 5, "lag": 0.0}
    pairs = [{"s": i, "e": i + 1, "ratio": 0.6, "flag": "near", "heard": "甲", "seen": "乙",
              "only_heard": [], "only_seen": []} for i in range(60)]
    text = "".join(mg.build_md(meta, pairs, [], "v"))
    eq(text.count("### "), 5, "only --limit blocks are printed")
    ok("还有 55 段未列出" in text, "the remainder is announced")
    ok("两路起点一致" in text, "zero lag says so")


def case_cjk_share():
    eq(round(mg.cjk_share("我们在圣赫利尔夫人的晚宴上"), 2), 1.0,
       "Chinese is fully CJK")
    ok(mg.cjk_share("Are we terribly old?") < 0.05,
       "English is almost entirely Latin")
    ok(mg.language_mismatch("Are we terribly old? " * 20,
                            "我们在圣赫利尔夫人的晚宴上" * 20),
       "English audio + Chinese subtitles is a language mismatch")
    ok(not mg.language_mismatch("我们在圣赫利尔夫人的晚宴上" * 20,
                                "我们正在全线溃败是真的吗" * 20),
       "two Chinese transcripts are not a mismatch")
    ok(not mg.language_mismatch("Are we old?", "我们老了吗"),
       "too little text to judge - do not guess the language")
    eq(mg.cjk_share(""), 0.0, "empty text is not CJK")
    eq(mg.cjk_share("123！！"), 0.0, "digits and punctuation are not letters")


def main():
    tmp = tempfile.mkdtemp(prefix="merge-unit-")
    try:
        case_cjk_share()
        case_norm_and_tokens()
        case_ratio()
        case_align_by_midpoint()
        case_align_no_double_quoting()
        case_align_orphan_in_silence()
        case_align_sorts_cues()
        case_flags()
        case_term_promotion()
        case_compare_numbers()
        case_cue_covers_segment()
        case_lag_detection()
        case_lag_none()
        case_load(tmp)
        case_mmss()
        case_md()
        case_md_limits()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("%d assertions, %d failures." % (checks[0], len(failures)))
    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("All assertions passed. No media / OCR / network required.")


if __name__ == "__main__":
    main()
