# -*- coding: utf-8 -*-
"""Offline unit tests for scripts/extract_subs.py.

Everything here runs without ffmpeg, ffprobe, a model or a network: parsing,
stream choice and the sidecar path are pure Python. The one end-to-end case
deliberately points ffprobe at a missing binary to prove the sidecar path still
produces a transcript when the media probe is unavailable - that is also the
documented fallback for a bare .srt next to an unreadable container.

Run:  python tests/run_subs_unit.py
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
    "xs_module", os.path.join(ROOT, "scripts", "extract_subs.py"))
xs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(xs)

checks = [0]
failures = []


def ok(cond, label):
    checks[0] += 1
    if not cond:
        failures.append(label)


def eq(got, want, label):
    ok(got == want, "%s (got %r, want %r)" % (label, got, want))


def track(order, codec, lang="", index=None):
    return {"order": order, "index": index if index is not None else order + 2,
            "codec": codec, "language": lang, "title": ""}


# ------------------------------------------------------------------ codecs ---

def case_classify():
    for c in ("subrip", "srt", "ass", "mov_text", "webvtt"):
        eq(xs.classify(c), "text", "classify(%s)=text" % c)
    for c in ("hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"):
        eq(xs.classify(c), "graphic", "classify(%s)=graphic" % c)
    # Unknown must be refused, not attempted: a wrong guess writes garbage.
    eq(xs.classify("som_future_codec"), "graphic", "unknown codec treated as graphic")


# -------------------------------------------------------------------- pick ---

def case_pick():
    none, why = xs.pick([], None, None)
    ok(none is None and "no subtitle stream" in why, "no tracks -> refusal")

    subs = [track(0, "hdmv_pgs_subtitle", "zho"), track(1, "subrip", "eng"),
            track(2, "ass", "jpn")]
    t, why = xs.pick(subs, None, None)
    eq((t or {}).get("order"), 1, "default skips the bitmap track, takes first text")

    t, why = xs.pick(subs, 0, None)
    ok(t is None and "bitmap" in why, "--stream 0 pointing at PGS is refused")

    t, why = xs.pick(subs, 9, None)
    ok(t is None and "out of range" in why, "--stream out of range is refused")

    t, why = xs.pick(subs, 2, None)
    eq((t or {}).get("codec"), "ass", "--stream 2 picks the ass track")

    t, why = xs.pick(subs, None, "ja")
    eq((t or {}).get("order"), 2, "--lang ja finds the jpn track")

    t, why = xs.pick(subs, None, "zh")
    ok(t is None and "no text subtitle track in language 'zh'" in why,
       "--lang zh must not silently fall back to another language")

    only_pgs = [track(0, "hdmv_pgs_subtitle", "chi")]
    t, why = xs.pick(only_pgs, None, None)
    ok(t is None and "bitmap" in why, "bitmap-only file reports OCR is needed")

    # Language tags arrive in both ISO 639-1 and 639-2 spellings.
    eq(xs.lang_of(track(0, "srt", "chi")), "zh", "chi normalises to zh")
    eq(xs.lang_of(track(0, "srt", "zho")), "zh", "zho normalises to zh")
    ok(xs.lang_matches(track(0, "srt", "chs"), "zh"), "chs counts as zh")
    ok(not xs.lang_matches(track(0, "srt", "eng"), "zh"), "eng is not zh")


# ------------------------------------------------------------------ timing ---

def case_cue_seconds():
    eq(xs.cue_seconds("00", "00", "01", "000"), 1.0, "1.000s")
    eq(xs.cue_seconds("1", "02", "03", "4"), 3723.4, "h:m:s with 1-digit ms")
    eq(xs.cue_seconds("00", "00", "00", "50"), 0.5, "2-digit ms means 500ms")
    eq(xs.cue_seconds("00", "01", "00", "5"), 60.5,
       "1-digit ms pads on the right (5 -> 500ms), not the left")


# ------------------------------------------------------------------ parsing ---

def case_parse_basic():
    text = ("1\n00:00:01,000 --> 00:00:03,500\n第一句。\n\n"
            "2\n00:00:04,000 --> 00:00:06,000\n第二句。\n")
    segs = xs.parse_cues(text)
    eq(len(segs), 2, "basic SRT: 2 cues")
    eq(segs[0], {"s": 1.0, "e": 3.5, "t": "第一句。"}, "basic SRT: first cue")
    eq(segs[1]["e"], 6.0, "basic SRT: last end")


def case_parse_messy():
    # BOM, CRLF, dot milliseconds, trailing coordinates, missing index.
    text = ("\ufeff00:00:01.000 --> 00:00:03.000 X1:100 X2:200\r\n"
            "带坐标的老式字幕\r\n\r\n"
            "  \r\n"
            "0:00:05,00 --> 0:00:07,00\r\n"
            "第二行\r\n")
    segs = xs.parse_cues(text)
    eq(len(segs), 2, "messy SRT: both cues survive BOM/CRLF/dot/coordinates")
    eq(segs[0]["t"], "带坐标的老式字幕", "messy SRT: coordinate suffix ignored")
    eq(segs[1]["s"], 5.0, "messy SRT: 1-digit hour parsed")
    eq(segs[1]["e"], 7.0, "messy SRT: 2-digit ms parsed as 00")


def case_parse_no_blank_lines():
    text = ("1\n00:00:01,000 --> 00:00:03,000\n2024\n"
            "2\n00:00:04,000 --> 00:00:06,000\n第二句\n")
    segs = xs.parse_cues(text)
    eq(len(segs), 2, "no blank separator: 2 cues")
    eq(segs[0]["t"], "2024", "numeric cue body survives")
    eq(segs[1]["t"], "第二句", "next index is not glued to the previous body")


def case_parse_drops_empty():
    text = ("1\n00:00:01,000 --> 00:00:03,000\n{\\an8}\n\n"
            "2\n00:00:04,000 --> 00:00:06,000\n真正的内容\n")
    segs = xs.parse_cues(text)
    eq(len(segs), 1, "markup-only cue is dropped, not emitted empty")
    eq(segs[0]["t"], "真正的内容", "the real cue survives")


def case_clean_text():
    eq(xs.clean_text(["<i>斜体</i>与<font color=\"#fff\">颜色</font>"]),
       "斜体与颜色", "HTML tags stripped")
    eq(xs.clean_text(["{\\an8}{\\pos(1,2)}位置码"]), "位置码", "ASS override blocks stripped")
    eq(xs.clean_text(["上一行", "下一行"]), "上一行下一行", "Chinese lines join without a space")
    eq(xs.clean_text(["first line", "second line"]), "first line second line",
       "ASCII lines join with a space")
    eq(xs.clean_text(["换行\\N硬折行"]), "换行 硬折行", "literal \\N becomes a space")
    eq(xs.clean_text(["  多   空格  "]), "多 空格", "runs of whitespace collapse")
    eq(xs.clean_text(["{only markup}"]), "", "markup-only returns empty")


def case_measure():
    segs = [{"s": 0.0, "e": 3.0, "t": "a"}, {"s": 2.5, "e": 5.0, "t": "b"},
            {"s": 9.0, "e": 10.0, "t": "c"}]
    st = xs.sort_and_measure(segs, 10.0)
    eq(st["overlaps"], 1, "overlap counted")
    eq(st["max_gap_s"], 4.0, "gap measured from the furthest end seen so far")
    eq(st["covered_s"], 6.5, "covered seconds sum cue durations")

    st = xs.sort_and_measure([{"s": 3.0, "e": 1.0, "t": "reversed"}], 5.0)
    eq(st["overlaps"], 0, "a reversed cue is repaired, not counted as overlap")
    eq(st["covered_s"], 2.0, "reversed cue contributes a positive duration")


# ------------------------------------------------------------------ sidecar ---

def case_sidecar(tmp):
    media = os.path.join(tmp, "clip.mp4")
    open(media, "wb").close()
    ok(xs.find_sidecar(media) is None, "no sidecar -> None")

    srt = os.path.join(tmp, "clip.srt")
    with open(srt, "w", encoding="utf-8") as fh:
        fh.write("1\n00:00:01,000 --> 00:00:02,000\nhello\n")
    eq(xs.find_sidecar(media), srt, "same-stem .srt found")
    ok(xs.find_sidecar(media, enabled=False) is None, "--no-sidecar disables lookup")

    other = os.path.join(tmp, "clip2.mp4")
    open(other, "wb").close()
    ok(xs.find_sidecar(other) is None, "a different stem is not matched")


def case_sidecar_encodings(tmp):
    body = "1\n00:00:01,000 --> 00:00:02,000\n中文编码\n"

    p = os.path.join(tmp, "gbk.srt")
    with open(p, "wb") as fh:
        fh.write(body.encode("gb18030"))
    text, err = xs.to_srt_text(p, "ffmpeg-not-installed", 5)
    ok(not err and "中文编码" in (text or ""), "GB18030 sidecar decodes correctly")

    p16 = os.path.join(tmp, "utf16.srt")
    with open(p16, "wb") as fh:
        fh.write(body.encode("utf-16"))       # writes a BOM
    text, err = xs.to_srt_text(p16, "ffmpeg-not-installed", 5)
    ok(not err and "中文编码" in (text or ""),
       "UTF-16 sidecar is not mistaken for GB18030 garbage")

    p8 = os.path.join(tmp, "bom8.srt")
    with open(p8, "wb") as fh:
        fh.write(b"\xef\xbb\xbf" + body.encode("utf-8"))
    text, err = xs.to_srt_text(p8, "ffmpeg-not-installed", 5)
    ok(not err and text.startswith("1\n"), "UTF-8 BOM is stripped, not kept in the first cue")


# ------------------------------------------------------------------- output ---

def case_write_pair(tmp):
    d = os.path.join(tmp, "pair")
    os.makedirs(d)
    jp = os.path.join(d, "a.json")
    mp = os.path.join(d, "a.md")
    xs.write_pair(jp, {"segments": []}, mp, ["# a\n"])
    ok(os.path.exists(jp) and os.path.exists(mp), "both halves written")
    ok(not os.path.exists(jp + ".tmp") and not os.path.exists(mp + ".tmp"),
       "no .tmp left behind")
    with open(jp, encoding="utf-8") as fh:
        eq(json.load(fh)["segments"], [], "json payload readable")

    # The invariant is "json exists => md exists". A failing md write must
    # therefore leave no json behind.
    jp2 = os.path.join(d, "b.json")
    bad_md = os.path.join(d, "no-such-dir", "b.md")
    try:
        xs.write_pair(jp2, {"segments": []}, bad_md, ["# b\n"])
        ok(False, "write_pair must raise when the md path is unwritable")
    except OSError:
        ok(True, "write_pair raises on an unwritable md path")
    ok(not os.path.exists(jp2), "no json left behind when the md write failed")


def case_is_done(tmp):
    d = os.path.join(tmp, "done")
    os.makedirs(d)
    jp, mp = os.path.join(d, "x.json"), os.path.join(d, "x.md")
    ok(not xs.is_done(jp, mp), "nothing written -> not done")
    with open(jp, "w", encoding="utf-8") as fh:
        json.dump({"segments": []}, fh)
    ok(not xs.is_done(jp, mp), "json without md -> not done")
    open(mp, "w").close()
    ok(not xs.is_done(jp, mp), "empty md -> not done")
    with open(mp, "w", encoding="utf-8") as fh:
        fh.write("# x\n")
    ok(xs.is_done(jp, mp), "complete pair -> done")


# --------------------------------------------------------------- end to end ---

def opts_for(tmp, **kw):
    o = {"out_dir": tmp, "stream": None, "lang": None, "no_sidecar": False,
         "force": True, "timeout": 5,
         # Point at binaries that do not exist on purpose.
         "ffmpeg": "ffmpeg-does-not-exist", "ffprobe": "ffprobe-does-not-exist",
         "key_of": lambda p: os.path.splitext(os.path.basename(p))[0]}
    o.update(kw)
    return o


def case_end_to_end_sidecar(tmp):
    d = os.path.join(tmp, "e2e")
    os.makedirs(d)
    media = os.path.join(d, "talk.mp4")
    open(media, "wb").close()
    with open(os.path.join(d, "talk.srt"), "w", encoding="utf-8") as fh:
        fh.write("1\n00:00:01,000 --> 00:00:04,000\n第一句，有标点。\n\n"
                 "2\n00:00:03,500 --> 00:00:06,000\n第二句重叠。\n")

    rec = xs.process_one(media, opts_for(d))
    eq(rec["status"], "ok", "sidecar works without ffmpeg/ffprobe")
    eq(rec["segments"], 2, "sidecar end-to-end: 2 segments")

    with open(os.path.join(d, "talk.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    eq(data["engine"], "subtitle-track", "engine marks the producer")
    eq(data["sidecar"], "talk.srt", "sidecar name recorded")
    eq(data["duration_source"], "last subtitle cue (container duration unknown)",
       "duration falls back to the last cue when the container cannot be probed")
    eq(data["stats"]["overlaps"], 1, "overlap reported, timeline left untouched")
    eq(data["segments"][0]["e"], 4.0, "source end time is not rewritten")

    with open(os.path.join(d, "talk.md"), encoding="utf-8") as fh:
        md = fh.read()
    ok("不是语音识别结果" in md, "md warns that a subtitle is not a transcript")
    ok("时间重叠" in md, "md surfaces the overlap count")


def case_end_to_end_no_source(tmp):
    d = os.path.join(tmp, "none")
    os.makedirs(d)
    media = os.path.join(d, "bare.mp4")
    open(media, "wb").close()
    rec = xs.process_one(media, opts_for(d))
    ok(rec["status"] == "failed", "no sidecar + no probe -> failed, not a fake success")
    ok("ffprobe" in rec["reason"], "the refusal names the missing probe (%s)" % rec["reason"])


def case_skip_existing(tmp):
    d = os.path.join(tmp, "skip")
    os.makedirs(d)
    media = os.path.join(d, "s.mp4")
    open(media, "wb").close()
    with open(os.path.join(d, "s.srt"), "w", encoding="utf-8") as fh:
        fh.write("1\n00:00:01,000 --> 00:00:02,000\nhello\n")
    o = opts_for(d, force=False)
    eq(xs.process_one(media, o)["status"], "ok", "first run extracts")
    eq(xs.process_one(media, o)["status"], "skipped", "second run skips the finished pair")


def main():
    tmp = tempfile.mkdtemp(prefix="subs-unit-")
    try:
        case_classify()
        case_pick()
        case_cue_seconds()
        case_parse_basic()
        case_parse_messy()
        case_parse_no_blank_lines()
        case_parse_drops_empty()
        case_clean_text()
        case_measure()
        case_sidecar(tmp)
        case_sidecar_encodings(tmp)
        case_write_pair(tmp)
        case_is_done(tmp)
        case_end_to_end_sidecar(tmp)
        case_end_to_end_no_source(tmp)
        case_skip_existing(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("%d assertions, %d failures." % (checks[0], len(failures)))
    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("All assertions passed. No network / ffmpeg / model required.")


if __name__ == "__main__":
    main()
