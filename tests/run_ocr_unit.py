# -*- coding: utf-8 -*-
"""Offline unit tests for scripts/ocr_subs.py.

No OCR engine, no ffmpeg, no model, no network: text cleaning, band selection
and cue building are pure functions over plain dicts. The one thing that cannot
be tested here is recognition quality - that needs pixels, and it is measured on
real clips instead (see ROADMAP "v0.7").

Run:  python tests/run_ocr_unit.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC = importlib.util.spec_from_file_location(
    "ocr_module", os.path.join(ROOT, "scripts", "ocr_subs.py"))
ocr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ocr)

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


def box(text, cy, h=0.03, H=1000):
    return {"text": text, "score": 0.9, "x0": 0.0, "x1": 100.0,
            "y0": (cy - h / 2.0) * H, "y1": (cy + h / 2.0) * H}


def frame(texts, H=1000, h=0.03):
    """One sampled frame holding one box per (text, centre-y) pair."""
    return {"boxes": [box(t, cy, h=h, H=H) for t, cy in texts]}


# ---------------------------------------------------------------- cleaning ---

def case_tighten():
    eq(ocr.tighten("第 三 条 数 据"), "第三条数据", "spaces between hanzi are dropped")
    eq(ocr.tighten("Top K 和 Top N"), "Top K 和 Top N", "latin words keep their space")
    eq(ocr.tighten("  你好  "), "你好", "outer whitespace stripped")
    eq(ocr.tighten("你好 ， 世界"), "你好，世界", "space before punctuation dropped")
    eq(ocr.tighten(None), "", "None becomes empty")
    # The subtitle's own punctuation must survive: it is content, not noise.
    eq(ocr.tighten("对，就是这样。"), "对，就是这样。", "full-width punctuation preserved")


def case_norm_key():
    eq(ocr.norm_key("Top K，你好"), "topk你好", "NFKC + lower + no punct/space")
    eq(ocr.norm_key("ＡＢＣ１２３"), "abc123", "full-width latin and digits folded")
    # Guard the asymmetry: norm_key may fold punctuation, tighten may not.
    eq(ocr.tighten("ＡＢＣ１２３"), "ＡＢＣ１２３", "tighten never NFKC-folds")


def case_similar():
    ok(ocr.similar("你好", "你好"), "identical text matches")
    ok(ocr.similar("你好，世界", "你好世界"), "punctuation-only difference matches")
    ok(ocr.similar("doL最大的召回率", "最大的召回率"),
       "a mid-fade partial read still matches (containment)")
    ok(not ocr.similar("你好", "再见"), "different text does not match")
    ok(not ocr.similar("好", "好啊啊啊啊啊啊啊啊"),
       "containment needs >=50% length overlap")
    ok(not ocr.similar("", "你好"), "empty never matches")


def case_lines_of():
    # Two physical lines; the upper one arrives as two boxes, out of order.
    upper = [box("把出行需求", 0.62, H=1000), box("可以帮用户", 0.62, H=1000)]
    upper[0]["x0"], upper[0]["x1"] = 200.0, 400.0       # "把出行需求" sits right
    upper[1]["x0"], upper[1]["x1"] = 0.0, 200.0         # "可以帮用户" sits left
    lines = ocr.lines_of(upper + [box("亮点1：项目来自真正的业务场景", 0.66, H=1000)], 1000)
    eq(len(lines), 2, "two y positions -> two lines")
    eq(lines[0]["text"], "可以帮用户把出行需求", "one line is read left to right")
    eq(lines[1]["text"], "亮点1：项目来自真正的业务场景", "and the lines are ordered top to bottom")
    ok(lines[0]["key"] != lines[1]["key"], "each line gets its own y slot")
    eq(ocr.lines_of([], 1000), [], "no boxes -> no lines")


def case_pick_lines():
    zh, en = "你会如何设计治理方案呢", "How would you design a governance plan?"
    lines = [{"key": 40, "text": zh, "conf": 0.9}, {"key": 44, "text": en, "conf": 0.8}]
    kept, dropped = ocr.pick_lines(lines, "zh")
    eq([g["text"] for g in kept], [zh], "zh keeps the Chinese line")
    eq([g["text"] for g in dropped], [en], "and drops the translation")

    kept, dropped = ocr.pick_lines(lines, "en")
    eq([g["text"] for g in kept], [en], "en keeps the English line")
    eq(len(dropped), 1, "and drops the Chinese one")

    # A Chinese subtitle can be mostly Latin letters; a ratio test would lose it.
    term = [{"key": 40, "text": "Top K 和 Top N 到底如何取值", "conf": 0.9},
            {"key": 44, "text": "What values should Top K and Top N take?", "conf": 0.8}]
    eq([g["text"] for g in ocr.pick_lines(term, "zh")[0]],
       ["Top K 和 Top N 到底如何取值"], "a term-heavy Chinese line still counts as Chinese")

    # A wrong --language must not empty the transcript.
    eq(len(ocr.pick_lines([{"key": 40, "text": en, "conf": 0.9}], "zh")[0]), 1,
       "nothing matches -> keep everything")
    eq(len(ocr.pick_lines(lines, None)[0]), 2, "no language -> keep everything")

    text, _ = ocr.reading_of(
        [box(zh, 0.60, H=1000), box(en, 0.64, H=1000)], 1000, "zh")
    eq(text, zh, "reading_of filters in the same breath")


def case_furniture():
    def seq(slots, n):
        """n frames; slots maps a y-slot to the text it carries each frame."""
        out = []
        for i in range(n):
            out.append([{"key": k, "text": v(i) if callable(v) else v, "conf": 0.9}
                        for k, v in slots.items()])
        return out

    # Dialogue changes every second; the bullet block under it does not.
    frames = seq({40: lambda i: "第%d句口播" % i, 44: "亮点1：项目来自真正的业务场景"}, 12)
    drop, report = ocr.furniture_texts(frames, 1.0)
    eq(drop, {ocr.norm_key("亮点1：项目来自真正的业务场景")}, "the held line is dropped")
    ok(not any(ocr.norm_key("第%d句口播" % i) in drop for i in range(12)),
       "the changing line survives")
    eq(report["run_frames"], 12, "and its run length is reported")

    # Spacing inside the block is OCR noise, not a change.
    frames = seq({40: lambda i: "第%d句" % i, 44: lambda i: "亮点 1" if i % 2 else "亮点1"}, 12)
    eq(ocr.furniture_texts(frames, 1.0)[0], {"亮点1"},
       "spacing and case do not break a run")

    # The head of a slot can be reused: long-held chapter cards, with a real
    # sentence flashed between them. Drop the runs, keep the sentence.
    script = ["章节点"] * 8 + ["发票要寄到上海办公室"] * 2 + ["章节点"] * 8
    frames = seq({40: lambda i: "第%d句" % i, 44: lambda i: script[i]}, len(script))
    drop, _ = ocr.furniture_texts(frames, 1.0)
    eq(drop, {ocr.norm_key("章节点")}, "the long run is dropped")
    ok(ocr.norm_key("发票要寄到上海办公室") not in drop,
       "a short line sharing the same slot survives")

    # A clip where everything is held: no fast line to compare against, so
    # nothing is removed. Silence is not evidence of furniture.
    frames = seq({40: "第一章", 44: "第一节"}, 12)
    eq(ocr.furniture_texts(frames, 1.0), (set(), {}), "all lines held -> drop nothing")

    # Two-line subtitles change together and must both survive. "Empty drop
    # set" is how the function says "nothing filtered" - read_video only prunes
    # when the set is non-empty.
    frames = seq({40: lambda i: "上半句%d" % i, 44: lambda i: "下半句%d" % i}, 12)
    eq(ocr.furniture_texts(frames, 1.0), (set(), {}),
       "wrapped subtitle lines both survive")

    eq(ocr.furniture_texts(seq({40: "只有一行"}, 12), 1.0), (set(), {}),
       "a single slot is never furniture")


def case_reading_of():
    # Deliberately out of reading order, and the second line is further right.
    boxes = [box("下降", 0.80), box("第三条数据它的得分陡然", 0.76)]
    text, conf = ocr.reading_of(boxes, 1000)
    eq(text, "第三条数据它的得分陡然下降", "lines are joined top-to-bottom")
    close(conf, 0.9, 0.01, "confidence is the median score")
    eq(ocr.reading_of([], 1000), ("", 0.0), "no boxes -> empty reading")


# ------------------------------------------------------------ band finding ---

def case_band_static_rejected():
    # A station logo / burned-in title card: present in every frame, never changes.
    samples = [frame([("某某出品", 0.94)]) for _ in range(10)]
    eq(ocr.calibrate(samples, [1000] * 10), None,
       "static text is not a subtitle band")


def case_band_no_text():
    eq(ocr.calibrate([frame([]) for _ in range(5)], [1000] * 5), None,
       "no boxes -> no band")


def case_band_low_support():
    samples = []
    for i in range(10):
        samples.append(frame([("一闪而过%d" % i, 0.75)]) if i < 2 else frame([]))
    eq(ocr.calibrate(samples, [1000] * 10), None,
       "support below 25%% is rejected")


def case_band_dynamic_found():
    samples = [frame([("第%d句话" % i, 0.75)]) for i in range(10)]
    band = ocr.calibrate(samples, [1000] * 10)
    ok(band is not None, "changing text in the lower half is a band")
    if band:
        close(band["y1"], 0.765 + ocr.BAND_PAD, 0.002, "band bottom includes padding")
        close(band["y0"], 0.735 - ocr.BAND_PAD, 0.002, "band top includes padding")
        eq(band["distinct"], 10, "every distinct line is counted")
        close(band["support"], 1.0, 0.001, "support is the frame fraction")
    # A crop that starts on the glyph outline makes the detector return a
    # fragment of the line: "Topk要用最小的值获得" read as "doL" at 1.2%
    # padding, and read correctly at 5%. Context is not cosmetic.
    ok(ocr.BAND_PAD >= 0.04, "the band keeps vertical context around the line")
    ok(band["core"][0] > band["y0"] and band["core"][1] < band["y1"],
       "the acceptance window stays inside the padded crop")


def case_band_prefers_lowest():
    # A changing caption at 0.20 and real subtitles at 0.78: subtitles win.
    samples = [frame([("标题%d" % i, 0.20), ("字幕第%d句" % i, 0.78)])
               for i in range(10)]
    band = ocr.calibrate(samples, [1000] * 10)
    ok(band is not None and band["y1"] > 0.75,
       "lowest changing cluster wins (got %r)" % (band and band["y1"]))


def case_band_two_lines():
    # One cue over two lines: 0.735 and 0.780 are 0.045 apart, inside the gap.
    samples = [frame([("上句第%d条" % i, 0.735), ("下句第%d条" % i, 0.780)])
               for i in range(10)]
    band = ocr.calibrate(samples, [1000] * 10)
    ok(band is not None, "two-line cue forms one band")
    if band:
        ok(band["y1"] - band["y0"] > 0.08, "band spans both lines")
        eq(band["distinct"], 20, "both lines counted as distinct text")


def case_band_no_chaining():
    # A screen recording: five small UI lines stacked 0.04 apart, plus one big
    # subtitle near the bottom. Centre-distance clustering chained all of them
    # into a single 49%-tall band and rejected the file; grouping by overlap
    # keeps the lines apart, and the larger text wins on weight.
    samples = []
    for i in range(12):
        boxes = [box("界面文字%d-%d" % (i, k), 0.60 + 0.04 * k, h=0.02)
                 for k in range(5)]
        boxes.append(box("字幕第%d句" % i, 0.90, h=0.05))
        samples.append({"boxes": boxes})
    band = ocr.calibrate(samples, [1000] * 12)
    ok(band is not None, "a screen recording still yields a band")
    if band:
        close(band["y1"], 0.925 + ocr.BAND_PAD, 0.002, "the big bottom line wins")
        ok(band["y1"] - band["y0"] < ocr.MAX_BAND_H,
           "the band does not swallow the UI text above it")


def case_band_line_too_tall():
    samples = [frame([("大字第%d行" % i, 0.75)], h=0.15) for i in range(10)]
    eq(ocr.calibrate(samples, [1000] * 10), None,
       "a line taller than 12%% of the frame is not a subtitle")


def case_band_resolution_independent():
    # Same geometry at 640x1138 and at 1920x1080 must land in the same place.
    a = [frame([("第%d句" % i, 0.75)], H=1138) for i in range(10)]
    b = [frame([("第%d句" % i, 0.75)], H=1080) for i in range(10)]
    ba, bb = ocr.calibrate(a, [1138] * 10), ocr.calibrate(b, [1080] * 10)
    ok(ba and bb and abs(ba["y0"] - bb["y0"]) < 0.002,
       "band is expressed in fractions, not pixels")


# ---------------------------------------------------------------- cue build ---

def case_segments_merge():
    readings = [(0.0, "你好", 0.9), (1.0, "你好", 0.9), (2.0, "再见", 0.9)]
    segs = ocr.build_segments(readings, 0.5, 3.0)
    eq(len(segs), 2, "repeated reading becomes one cue")
    eq((segs[0]["s"], segs[0]["e"]), (0.0, 1.5), "cue grows to cover both frames")
    eq((segs[1]["s"], segs[1]["e"]), (1.5, 2.5), "next cue starts where the last ended")


def case_segments_confidence_wins():
    low_first = [(0.0, "第一句", 0.4), (1.0, "第一句话", 0.9)]
    segs = ocr.build_segments(low_first, 0.5, 5.0)
    eq(segs[0]["t"], "第一句话", "the confident reading replaces the weak one")
    high_first = [(0.0, "第一句话", 0.9), (1.0, "第一句", 0.4)]
    segs = ocr.build_segments(high_first, 0.5, 5.0)
    eq(segs[0]["t"], "第一句话", "a weak partial read never overwrites a good one")


def case_segments_no_overlap():
    readings = [(0.0, "第一句", 0.9), (0.2, "第二句", 0.9)]
    segs = ocr.build_segments(readings, 0.5, 5.0)
    ok(all(segs[i]["e"] <= segs[i + 1]["s"] + 1e-6
           for i in range(len(segs) - 1)), "cues never overlap")


def case_segments_duration_guard():
    # A cue shorter than 0.25 s cannot be read by a human; it is sampling noise.
    segs = ocr.build_segments([(0.1, "一闪", 0.9)], 0.05, 10.0)
    eq(segs, [], "cue below the minimum duration is dropped")


def case_segments_clamped_to_media():
    segs = ocr.build_segments([(0.0, "开头", 0.9), (9.8, "结尾", 0.9)], 0.5, 10.0)
    eq(segs[0]["s"], 0.0, "start never goes negative")
    eq(segs[-1]["e"], 10.0, "end never exceeds the media duration")


def case_segments_skip_empty():
    segs = ocr.build_segments([(0.0, "", 0.0), (1.0, "有字", 0.9)], 0.5, 5.0)
    eq(len(segs), 1, "frames with no reading produce no cue")


# ------------------------------------------------------------------ output ---

def case_md():
    meta = {"source_label": "画面硬字幕（OCR 逐帧识别）", "duration": 10.0,
            "chars": 6, "fps": 1.0, "frames": 10, "band": [0.72, 0.78],
            "hits": 9, "low_conf": 1}
    segs = [{"s": 0.0, "e": 2.0, "t": "正常一句", "c": 0.9},
            {"s": 2.0, "e": 4.0, "t": "可疑一句", "c": 0.42}]
    stats = {"coverage": 0.4, "overlaps": 0}
    text = "".join(ocr.build_md("t", meta, segs, stats))
    ok("字幕带 y=0.720–0.780" in text, "header reports the band")
    ok("9 帧（90%）" in text, "header reports band hits")
    ok("可疑一句  [?]" in text, "low-confidence cue is marked")
    ok("正常一句  [?]" not in text, "confident cue is not marked")


def main():
    case_tighten()
    case_norm_key()
    case_similar()
    case_lines_of()
    case_pick_lines()
    case_furniture()
    case_reading_of()
    case_band_static_rejected()
    case_band_no_text()
    case_band_low_support()
    case_band_dynamic_found()
    case_band_prefers_lowest()
    case_band_two_lines()
    case_band_no_chaining()
    case_band_line_too_tall()
    case_band_resolution_independent()
    case_segments_merge()
    case_segments_confidence_wins()
    case_segments_no_overlap()
    case_segments_duration_guard()
    case_segments_clamped_to_media()
    case_segments_skip_empty()
    case_md()

    print()
    print("%d assertions, %d failures." % (checks[0], len(failures)))
    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("All assertions passed. No OCR engine / ffmpeg / model required.")


if __name__ == "__main__":
    main()
