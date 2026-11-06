#!/usr/bin/env python3
"""Read burned-in ("hard") subtitles out of the picture, frame by frame.

Why this file exists
--------------------
`extract_subs.py` handles a *text subtitle track*: ffmpeg demuxes it, nothing is
inferred, nothing is misheard. Most downloaded clips - Douyin, screen
recordings, re-encodes, anything that went through a phone - carry no such
track. The words exist only as pixels, and only OCR can reach them.

This script reads those pixels with PP-OCR (RapidOCR + onnxruntime) and writes
the same JSON + MD shape as the other producers, with `engine="ocr"`.

What it deliberately does NOT do
- It never touches the audio. Pair it with `transcribe.py` and reconcile the
  two with `merge_sources.py`; neither source is treated as truth.
- It never "repairs" a line with language priors. A plausible-looking fix is
  unverifiable, so what OCR read is what is written, plus a confidence score.

The band is a by-product, not a guess
-------------------------------------
The original plan located the subtitle band with pure Pillow heuristics (bright,
low-saturation pixels projected onto rows). Measured on real clips it failed in
both directions: a white shirt scored 117.8 while the real subtitle band scored
lower, and the saturation gate excluded yellow text outright. OCR already
returns a box for every line it reads, so the band is derived from those boxes:
cluster the line centres, drop clusters whose text never changes (a logo or a
station bug reads the same in every frame), then take the lowest surviving
cluster. Yellow text is not special-cased anywhere - it is simply text.

Cost
----
About 0.2-0.3 s per sampled frame on CPU (640 px wide, band cropped), so a
3-minute clip at 1 fps takes roughly 40-60 s. That is slower than ASR on the
same clip, which is why this is an explicit second opinion rather than a
default: run it when the words on screen matter (names, numbers, jargon).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Output contract (atomic pair, "json implies md") is shared with the other
# producers on purpose: one schema, one set of invariants, three sources.
from extract_subs import is_done, punct_per_100, sort_and_measure, write_pair

try:
    import numpy as np
    from PIL import Image
    _IMAGING = True
except Exception:                                   # pragma: no cover
    _IMAGING = False

SAMPLE_W = 640                 # recognition width; wider measured slower, not better
REGION = (0.50, 0.995)         # only the lower half can hold a subtitle
# The detector needs vertical context around a line: a crop that starts on the
# glyph outline makes PP-OCR return a fragment of it ("Topk要用最小的值获得" came
# back as "doL" with 1.2% padding, and read perfectly with 5%). Padding costs
# nothing and a stray title is still rejected by the size filter below.
BAND_PAD = 0.05
MIN_SUPPORT = 0.25             # a band must show up in >=25% of the sampled frames
MIN_DISTINCT = 2               # ...and its text must change: kills logos/titles
MAX_LINE_H = 0.12              # a subtitle line is never 12% of the frame height
MAX_BAND_H = 0.16              # two lines plus a little slack; taller is a paragraph
HALF_BAND = 0.05               # half-height of the window scored around each centre
CALIB_FRAMES = 16
MAX_FRAMES = 900
CJK = ("\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
       "\u3040-\u30ff\uac00-\ud7af")
# Class *body*, not a whole class: these pieces get spliced into other classes.
PUNCT_BODY = r"，。！？、,.!?;；:：\"'“”‘’（）()\[\]【】…—\-~～"


# ------------------------------------------------------------------- text ---

def tighten(s):
    """One line, no stray gaps. PP-OCR loves to insert spaces between hanzi."""
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    s = re.sub(r"(?<=[%s]) (?=[%s])" % (CJK, CJK), "", s)
    s = re.sub(r"(?<=[%s]) (?=[%s])" % (CJK, PUNCT_BODY), "", s)
    s = re.sub(r"(?<=[%s]) (?=[%s])" % (PUNCT_BODY, CJK), "", s)
    return s


def norm_key(s):
    """Comparison-only form: NFKC, case-folded, no spaces, no punctuation.

    Never used for output - NFKC turns a full-width Chinese comma into an ASCII
    one, which would silently rewrite the subtitle's own punctuation.
    """
    s = unicodedata.normalize("NFKC", str(s or "")).lower()
    s = re.sub("[%s]" % PUNCT_BODY, "", s)
    return re.sub(r"\s+", "", s)


def similar(a, b, floor=0.82):
    """Same cue read twice by a noisy OCR?

    Two failure modes look different but mean the same thing: a frame caught
    mid-fade reads *fewer* characters ("最大的召回率" for "doL最大的召回率"), and
    a frame caught mid-change reads a different line entirely. Containment
    catches the first, ratio catches the second.
    """
    ka, kb = norm_key(a), norm_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    short, long_ = sorted((ka, kb), key=len)
    if short in long_ and len(short) >= 0.5 * len(long_):
        return True
    return SequenceMatcher(None, ka, kb).ratio() >= floor


# --------------------------------------------------------------- ffmpeg ------

def run(cmd, timeout):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("%s not found" % cmd[0])
    except subprocess.TimeoutExpired:
        raise RuntimeError("%s timed out after %ss" % (cmd[0], timeout))
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    return p.returncode, out, err


def probe_media(path, ffprobe, timeout):
    """Container duration + the video stream's pixel size (OCR needs both)."""
    rc, out, err = run([ffprobe, "-v", "error", "-show_entries",
                        "format=duration:stream=codec_type,width,height",
                        "-of", "json", path], timeout)
    if rc != 0:
        raise RuntimeError((err.strip() or "ffprobe exit %d" % rc)[:300])
    try:
        data = json.loads(out or "{}")
    except ValueError as exc:
        raise RuntimeError("ffprobe returned unreadable JSON: %s" % exc)
    try:
        duration = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = 0.0
    width = height = 0
    for st in data.get("streams") or []:
        if st.get("codec_type") == "video":
            width, height = int(st.get("width") or 0), int(st.get("height") or 0)
            break
    return {"duration": duration, "width": width, "height": height}


def extract_frames(path, out_dir, fps, ffmpeg, timeout, width=SAMPLE_W):
    """One sequential pass -> numbered PNGs. Per-frame seeking is 10x slower."""
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, "f%05d.png")
    vf = "fps=%.6f,scale=%d:-2" % (max(fps, 0.001), width)
    rc, _, err = run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                      "-y", "-i", path, "-vf", vf, pattern], timeout)
    if rc != 0:
        raise RuntimeError("ffmpeg frame extraction failed: %s"
                           % (err.strip()[:300] or "exit %d" % rc))
    names = sorted(n for n in os.listdir(out_dir) if n.endswith(".png"))
    return [os.path.join(out_dir, n) for n in names]


# ------------------------------------------------------------------ vision ---

def load_engine():
    if not _IMAGING:
        raise RuntimeError("numpy and Pillow are required: "
                           "python -m pip install numpy pillow")
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception as exc:
        raise RuntimeError(
            "RapidOCR is not installed (%s).\n"
            "  python -m pip install rapidocr_onnxruntime onnxruntime\n"
            "The PP-OCR models ship inside the wheel (~13 MB); nothing else "
            "to download." % exc)
    # use_cls=False: the angle classifier exists for rotated phone photos, and
    # subtitle lines are horizontal by construction.
    return RapidOCR(use_cls=False)


def ocr_boxes(engine, img, top_f, bot_f, min_score):
    """Read one frame region; return boxes in *frame* pixel coordinates."""
    W, H = img.size
    top = max(0, int(H * top_f))
    bot = min(H, int(H * bot_f))
    if bot - top < 8:
        return []
    crop = img.crop((0, top, W, bot))
    arr = np.asarray(crop)
    if arr.ndim == 3:
        arr = arr[:, :, ::-1]          # RGB -> BGR, RapidOCR's convention
    res, _ = engine(arr)
    boxes = []
    for item in res or []:
        try:
            pts, txt, score = item[0], item[1], float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        txt = tighten(txt)
        if not txt or score < min_score:
            continue
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        boxes.append({"text": txt, "score": score,
                      "x0": min(xs), "x1": max(xs),
                      "y0": min(ys) + top, "y1": max(ys) + top})
    return boxes


def lines_of(boxes, height, bucket=None):
    """Split one frame's boxes into physical lines, top to bottom.

    `bucket` is the height of one y-slot as a fraction of the frame, and it has
    to be shared by every frame. Deriving it per frame looks harmless and is
    not: a frame carrying one line and a frame carrying four get different
    buckets, the same subtitle lands in different slots, and the per-line
    statistics below never accumulate on anything.
    """
    if not boxes:
        return []
    if not height:
        return []
    if bucket is None:
        heights = sorted(b["y1"] - b["y0"] for b in boxes)
        bucket = max(max(heights[len(heights) // 2], 6.0) * 0.5 / float(height), 0.004)
    groups = {}
    for b in boxes:
        cy = (b["y0"] + b["y1"]) / 2.0 / float(height)
        groups.setdefault(int(round(cy / bucket)), []).append(b)
    out = []
    for key, items in groups.items():
        items.sort(key=lambda b: b["x0"])
        text = tighten(" ".join(b["text"] for b in items))
        if not text:
            continue
        scores = sorted(b["score"] for b in items)
        out.append({"key": key, "text": text, "conf": scores[len(scores) // 2],
                    "cy": sum((b["y0"] + b["y1"]) / 2.0 for b in items)
                    / len(items) / float(height)})
    out.sort(key=lambda g: g["cy"])
    return out


def _script_hits(text):
    cjk = sum(1 for ch in text
              if "\u3400" <= ch <= "\u4dbf" or "\u4e00" <= ch <= "\u9fff")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return cjk, latin


def pick_lines(lines, language):
    """Keep the lines written in the language being transcribed.

    A bilingual subtitle prints the translation under the dialogue, and both
    change at the same cadence, so nothing in the picture says which one was
    spoken - the flag does. Dropping the translation is not tidiness: keeping it
    doubled the character count and dragged the heard/seen similarity low enough
    that almost every pair in `merge_sources.py` looked contradictory.

    The test is deliberately crude (does the line contain a character from the
    target script) rather than a ratio: a real subtitle can be half technical
    terms, and "Top K 和 Top N 到底如何取值" is more Latin letters than hanzi.
    If no line matches, everything is kept - a wrong `--language` must never
    produce an empty transcript.
    """
    if len(lines) < 2 or not language:
        return lines, []
    want_cjk = str(language).lower().startswith(("zh", "ja", "ko", "cmn", "yue"))
    kept = []
    for g in lines:
        cjk, latin = _script_hits(g["text"])
        if (cjk > 0) if want_cjk else (latin > 0):
            kept.append(g)
    if not kept or len(kept) == len(lines):
        return lines, []
    dropped = [g for g in lines if all(g is not k for k in kept)]
    return kept, dropped


def reading_of(boxes, height, language=None, bucket=None):
    """Join the boxes of one frame into one line of text, top-to-bottom."""
    lines = lines_of(boxes, height, bucket)
    if not lines:
        return "", 0.0
    lines, _ = pick_lines(lines, language)
    text = tighten(" ".join(g["text"] for g in lines))
    scores = sorted(g["conf"] for g in lines)
    return text, scores[len(scores) // 2]


def furniture_texts(frame_lines, fps):
    """Which on-screen texts are set-dressing rather than dialogue.

    Calibrating the band answers "where is there text". It cannot answer "which
    of these lines is being spoken", and on a clip with a bullet block under the
    subtitles the two answers differ: every cue came back carrying the same 28
    characters of slide copy, tripling the transcript.

    Dialogue is replaced every second or two; a chapter label, a bullet block or
    a flowchart sits still for ten or twenty. At one sample per second that is
    one number per run - how many consecutive frames show the *same* text.
    Measured on real clips: dialogue 2 frames, the block under it 9 to 17.

    The unit is the run, not the y-slot. A slot near the bottom is often reused:
    one clip put chapter cards there, and between two cards it also flashed real
    example sentences for a second. Dropping the whole slot lost that dialogue;
    dropping only runs longer than the floor keeps it.

    Two guards keep this from eating real subtitles: a run is only dropped when
    something else on screen changes at least 2.5x faster, and never below a
    five-second floor. A clip whose every line is held for ten seconds has no
    fast line, so nothing is dropped.

    Returns (texts_to_drop, report). The report feeds the header and `params`,
    so nothing disappears quietly.
    """
    n = len(frame_lines)
    if n < 2:
        return set(), {}
    keys = sorted({g["key"] for lines in frame_lines for g in lines})
    if len(keys) < 2:
        return set(), {}
    seq = {k: [] for k in keys}
    for lines in frame_lines:
        here = {g["key"]: g["text"] for g in lines}
        for k in keys:
            seq[k].append(norm_key(here.get(k, "")))

    stats = {}
    runs = []
    for k, texts in seq.items():
        present = sum(1 for t in texts if t)
        if not present:
            continue
        i, m = 0, len(texts)
        best = 0
        while i < m:
            if not texts[i]:
                i += 1
                continue
            # Similar, not identical. OCR jitters by a character or two on a
            # card that never changed, and exact equality split a 9-frame card
            # into runs of 2 - which then looked exactly like dialogue. The bar
            # is 0.9, not the 0.82 used for cue merging: here a false match
            # glues two different subtitle lines into one run, and a long enough
            # chain of them would be dropped as furniture.
            j = i
            while j + 1 < m and texts[j + 1] and similar(texts[j], texts[j + 1], 0.9):
                j += 1
            runs.append((k, i, j))
            best = max(best, j - i + 1)
            i = j + 1
        stats[k] = {"run": best, "present": present, "share": present / float(n)}
    if len(stats) < 2:
        return set(), {}

    steady = [v["run"] for v in stats.values() if v["share"] >= 0.2]
    if not steady:
        return set(), {}
    floor = max(5.0 * max(fps, 0.01), 2.5 * min(steady))

    drop = set()
    worst = None
    for k, i, j in runs:
        if j - i + 1 <= floor:
            continue
        drop.update(t for t in seq[k][i:j + 1] if t)
        if worst is None or (j - i + 1) > worst["run"]:
            worst = {"run": j - i + 1, "slot": k, "frame": i}
    # Never return "everything is furniture": that would empty the transcript.
    on_screen = {t for texts in seq.values() for t in texts if t}
    if not drop or not (on_screen - drop):
        return set(), {}

    sample = ""
    for g in frame_lines[worst["frame"]]:
        if g["key"] == worst["slot"]:
            sample = g["text"]
            break
    report = {"texts": len(drop), "slots": len(stats),
              "run_frames": worst["run"],
              "run_seconds": round(worst["run"] / max(fps, 0.01), 1),
              "sample": sample}
    return drop, report


def calibrate(samples, height_of):
    """Pick the subtitle band from the boxes OCR already returned.

    `samples` is a list of {"boxes": [...]} dicts, one per sampled frame.
    Returns None when nothing looks like a changing subtitle band, which is a
    real answer (a logo, a burned-in title card, or no text at all).

    Two steps, both learned the hard way from real clips:

    1. **Keep the larger text.** A screen recording carries dozens of UI lines
       per frame; subtitles are set bigger than body copy. Keeping only boxes
       at or above the 60th height percentile of *this* video is scale-free -
       no absolute pixel threshold to tune per resolution.
    2. **Score a window around each centre, never chain.** Grouping boxes that
       merely sit near each other is single-linkage clustering: on a page of UI
       text there is always a box within the gap, so the whole lower half merged
       into one 49%-tall "band" and those files were rejected outright.
    """
    items = []
    for idx, s in enumerate(samples):
        H = float(height_of[idx]) or 1.0
        for b in s["boxes"]:
            items.append({"cy": (b["y0"] + b["y1"]) / 2.0 / H,
                          "y0": b["y0"] / H, "y1": b["y1"] / H,
                          "h": (b["y1"] - b["y0"]) / H,
                          "key": norm_key(b["text"]), "frame": idx})
    if not items:
        return None

    heights = sorted(x["h"] for x in items)
    # The epsilon matters: heights are differences of floats, so two visually
    # identical lines can differ in the last bit and a strict >= cut would drop
    # half of them. "Same size" must mean the same thing on both sides.
    cut = max(0.012, heights[int(len(heights) * 0.6)] - 1e-6)
    big = [x for x in items if x["h"] >= cut]
    if not big:
        return None

    n = max(len(samples), 1)
    best, best_score = None, 0.0
    for c in sorted({round(x["cy"], 3) for x in big}):
        sel = [x for x in big if abs(x["cy"] - c) <= HALF_BAND]
        support = len({x["frame"] for x in sel}) / float(n)
        distinct = len({x["key"] for x in sel})
        sizes = sorted(x["h"] for x in sel)
        med_h = sizes[len(sizes) // 2]
        if support < MIN_SUPPORT or distinct < MIN_DISTINCT or med_h > MAX_LINE_H:
            continue
        # Tighten to the lines that are actually subtitle-sized; a stray header
        # at the edge of the window must not widen the band.
        core = [x for x in sel if x["h"] >= 0.6 * med_h]
        y0 = min(x["y0"] for x in core)
        y1 = max(x["y1"] for x in core)
        if y1 - y0 > MAX_BAND_H:
            continue
        weight = min(max(med_h / 0.03, 0.7), 1.6)
        score = support * min(distinct, 8) * weight
        # Two ranges on purpose. `crop` is what gets handed to the detector and
        # includes the padding it needs; `core` is what counts as a subtitle
        # line. Keeping them apart means a wider crop cannot smuggle an overlay
        # ("95.9万", a legal banner) into the transcript.
        cand = {"y0": max(0.0, y0 - BAND_PAD), "y1": min(1.0, y1 + BAND_PAD),
                "core": [round(y0, 4), round(y1, 4)],
                "support": round(support, 3), "distinct": distinct,
                "lines": len(sel), "mean_h": round(med_h, 4),
                "size_floor": round(0.5 * med_h, 4),
                "sample": sel[0]["key"]}
        if best is None or score > best_score + 1e-9 or (
                abs(score - best_score) <= 1e-9 and cand["y1"] > best["y1"]):
            best, best_score = cand, score
    return best


def build_segments(readings, half_span, duration, min_dur=0.25):
    """Turn per-frame readings into cues.

    Adjacent samples that read the same thing are one cue. The longer of the two
    texts wins, because a frame caught mid-transition often loses a line.
    """
    segs = []
    for t, text, conf in readings:
        if not text:
            continue
        start = max(0.0, t - half_span)
        end = min(duration, t + half_span) if duration else t + half_span
        if segs and similar(segs[-1]["t"], text) and start <= segs[-1]["e"] + 0.05:
            prev = segs[-1]
            longer = len(norm_key(text)) > len(norm_key(prev["t"]))
            # A frame caught mid-transition reads half a line; a confident frame
            # reads the line. Confidence wins, length only breaks near-ties.
            if conf > prev["c"] + 0.05 or (longer and conf >= prev["c"] - 0.05):
                prev["t"] = text
                prev["c"] = round(conf, 3)
            prev["e"] = round(max(prev["e"], end), 2)
            continue
        if segs and start < segs[-1]["e"]:
            start = segs[-1]["e"]        # one cue at a time, no overlap
        segs.append({"s": round(start, 2), "e": round(end, 2), "t": text,
                     "c": round(conf, 3)})
    out = []
    for x in segs:
        if x["e"] - x["s"] >= min_dur:
            out.append(x)
    return out


# ------------------------------------------------------------------ output ---

def build_md(tag, meta, segs, stats):
    head = ["# %s\n\n" % tag,
            "- 来源: %s | 时长: %.2fs | %d 条 / %d 字\n"
            % (meta["source_label"], meta["duration"], len(segs), meta["chars"]),
            "- 这是从**画面像素**里认出来的字，不是语音识别结果：字幕可能被作者"
            "随手写错、可能与口播不一致、也可能只是标题或水印。\n",
            "- 采样 %.1f fps，共 %d 帧；字幕带 y=%.3f–%.3f，命中 %d 帧（%.0f%%）。\n"
            % (meta["fps"], meta["frames"], meta["band"][0], meta["band"][1],
               meta["hits"], meta["hits"] / max(meta["frames"], 1) * 100)]
    if meta["low_conf"]:
        head.append("- 有 %d 条的识别置信度低于 0.60，行尾标了 `[?]`，建议对照听一遍。\n"
                    % meta["low_conf"])
    if meta.get("translations"):
        head.append("- 双语字幕：只保留 %s 的行，略去 %d 行译文/外文行"
                    "（换 `--language` 可切换）。\n"
                    % (meta.get("language") or "目标语言", meta["translations"]))
    if meta.get("furniture"):
        f = meta["furniture"]
        head.append("- 字幕带里另有固定文案（最长的一段连续 %d 帧、约 %.0f 秒一字不变，"
                    "例如「%s」），已剔除 %d 段，未计入正文。\n"
                    % (f["run_frames"], f["run_seconds"], f.get("sample") or "",
                       f["texts"]))
    if meta["duration"] and stats["coverage"] is not None and stats["coverage"] < 0.5:
        head.append("- 只覆盖了 %.0f%% 的时长：中间可能没有字幕，也可能漏读了。\n"
                    % (stats["coverage"] * 100))
    head.append("\n")
    body = []
    for x in segs:
        mark = "  [?]" if x.get("c", 1.0) < 0.6 else ""
        body.append("[%.2f - %.2f] %s%s\n\n" % (x["s"], x["e"], x["t"], mark))
    return head + body


MEDIA_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm",
              ".ts", ".m4v", ".mpg", ".mpeg", ".rmvb", ".3gp")


def collect(inputs, recursive):
    """Folder expansion keeps media only; a file you name yourself is taken
    as-is, because refusing it would be second-guessing an explicit request."""
    files = []
    for p in inputs:
        if os.path.isdir(p):
            if recursive:
                for dirpath, dirnames, names in os.walk(p):
                    dirnames.sort()
                    files += [os.path.join(dirpath, n) for n in sorted(names)
                              if n.lower().endswith(MEDIA_EXTS)]
            else:
                files += [os.path.join(p, n) for n in sorted(os.listdir(p))
                          if os.path.isfile(os.path.join(p, n))
                          and n.lower().endswith(MEDIA_EXTS)]
        else:
            files.append(p)
    seen, out = set(), []
    for f in files:
        real = os.path.realpath(f)
        if real in seen:
            continue
        seen.add(real)
        out.append(f)
    return out


def read_video(path, engine, cfg, progress=None):
    """Sample, calibrate, then read every sample inside the band."""
    info = probe_media(path, cfg["ffprobe"], cfg["timeout"])
    duration = info["duration"]
    if duration <= 0:
        return {"error": "cannot read duration"}
    fps = cfg["fps"]
    note = ""
    if duration * fps > cfg["max_frames"]:
        fps = cfg["max_frames"] / duration
        note = ("sampling reduced to %.2f fps to stay under %d frames"
                % (fps, cfg["max_frames"]))
    tmp = tempfile.mkdtemp(prefix="ocr-subs-")
    try:
        frames = extract_frames(path, tmp, fps, cfg["ffmpeg"], cfg["timeout"])
        if not frames:
            return {"error": "no frame decoded"}
        heights, readings = [], []
        half = 0.5 / fps
        step = max(1, len(frames) // cfg["calib_frames"])
        cal_idx = list(range(0, len(frames), step))[:cfg["calib_frames"]]
        boxes_at = {}
        for i in cal_idx:
            img = Image.open(frames[i])
            heights.append(img.size[1])
            boxes_at[i] = ocr_boxes(engine, img, cfg["region"][0],
                                    cfg["region"][1], cfg["min_score"])
            img.close()
        band = calibrate([{"boxes": boxes_at[i]} for i in cal_idx], heights)
        if band is None and not cfg["no_band"]:
            return {"error": "no changing subtitle band found",
                    "band": None, "duration": duration, "info": info,
                    "frames": len(frames), "fps": fps}
        top, bot = ((cfg["region"][0], cfg["region"][1]) if band is None
                    else (band["y0"], band["y1"]))
        keep_top, keep_bot = (top, bot) if band is None else band["core"]
        # One slot size for every frame, taken from the band's own median line
        # height. See lines_of() for what goes wrong when it is derived per frame.
        bucket = max(band["mean_h"] * 0.5, 0.004) if band else None
        per_frame = []
        for idx, fp in enumerate(frames):
            img = Image.open(fp)
            H = img.size[1]
            # Always re-read through the crop: the calibration pass used a much
            # taller region, and detection is scale-sensitive enough that reusing
            # those boxes made the calibration frames read differently from the
            # rest (one 6 s cue came back as "doL 最大的召回率" while its
            # neighbours read the full line).
            boxes = ocr_boxes(engine, img, top, bot, cfg["min_score"])
            # Re-filter through the band: the calibration pass used the wider
            # region, and anything outside the band is a title or a watermark.
            inside = [b for b in boxes
                      if keep_top - 0.01 <= ((b["y0"] + b["y1"]) / 2.0 / H)
                      <= keep_bot + 0.01]
            # ...and drop text that is clearly smaller than this clip's own
            # subtitles, which is what keeps UI lines out of a screen recording.
            floor = band.get("size_floor") if band else None
            if floor:
                inside = [b for b in inside if (b["y1"] - b["y0"]) / H >= floor]
            per_frame.append(lines_of(inside, H, bucket))
            img.close()
            if progress and (idx % 20 == 0 or idx == len(frames) - 1):
                progress(idx + 1, len(frames))

        # Which slots are dialogue, and which are slide copy that happens to sit
        # inside the band. Skipped without a band: the slot size is per frame
        # then, so the runs would not line up.
        drop, furn = furniture_texts(per_frame, fps) if bucket else (set(), {})
        translations = 0
        for idx, lines in enumerate(per_frame):
            if drop:
                lines = [g for g in lines if norm_key(g["text"]) not in drop]
            kept, rest = pick_lines(lines, cfg["language"])
            translations += len(rest)
            scores = sorted(g["conf"] for g in kept)
            readings.append((idx / fps,
                             tighten(" ".join(g["text"] for g in kept)),
                             scores[len(scores) // 2] if scores else 0.0))
        segs = build_segments(readings, half, duration)
        return {"segments": segs, "duration": duration, "duration_source": "container",
                "band": band, "band_used": [round(top, 4), round(bot, 4)],
                "frames": len(frames), "fps": round(fps, 3), "note": note,
                "hits": sum(1 for r in readings if r[1]), "info": info,
                "furniture": furn, "translations": translations}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def process_one(path, engine, opts):
    rec = {"src": path, "status": "failed", "reason": "", "segments": 0,
           "chars": 0, "band": None, "out_json": ""}
    if not os.path.isfile(path):
        rec["reason"] = "input not found"
        return rec
    key = opts["key_of"](path)
    out_json = os.path.join(opts["out_dir"], key + ".json")
    out_md = os.path.splitext(out_json)[0] + ".md"
    rec["out_json"] = out_json
    if not opts["force"] and is_done(out_json, out_md):
        rec["status"] = "skipped"
        rec["reason"] = "already transcribed"
        return rec

    def progress(done, total):
        # A carriage return is invisible once output is piped to a file, so the
        # non-tty path emits one line per checkpoint instead of one endless one.
        if sys.stdout.isatty():
            sys.stdout.write("\r    reading frames %d/%d" % (done, total))
        else:
            sys.stdout.write("    frames %d/%d\n" % (done, total))
        sys.stdout.flush()

    got = read_video(path, engine, opts, progress)
    if progress and "frames" in got and sys.stdout.isatty():
        sys.stdout.write("\r" + " " * 40 + "\r")
    if got.get("error"):
        rec["reason"] = got["error"]
        rec["band"] = got.get("band")
        return rec

    segs = got["segments"]
    if not segs:
        rec["reason"] = "band found but nothing readable inside it"
        return rec

    duration = got["duration"]
    stats = sort_and_measure(segs, duration)
    body = "".join(x["t"] for x in segs)
    minutes = duration / 60.0
    stats.update({
        "chars_per_min": round(len(body) / minutes, 1) if minutes else 0,
        "segs_per_min": round(len(segs) / minutes, 1) if minutes else 0,
        "punct_per_100": punct_per_100(body),
        "tail_gap_s": round(duration - segs[-1]["e"], 2),
        "coverage": round(stats["covered_s"] / duration, 3) if duration else None,
    })
    low = sum(1 for x in segs if x.get("c", 1.0) < 0.6)
    label = "画面硬字幕（OCR 逐帧识别）"
    meta = {"source_label": label, "duration": duration, "chars": len(body),
            "fps": got["fps"], "frames": got["frames"],
            "band": got["band_used"], "hits": got["hits"], "low_conf": low,
            "furniture": got.get("furniture") or {},
            "translations": got.get("translations", 0),
            "language": opts["language"]}
    data = {
        "tag": key, "engine": "ocr", "source_track": None, "sidecar": None,
        "source_label": label,
        "device": "cpu", "compute": "onnxruntime",
        "wav": None, "video": os.path.basename(path),
        "duration": round(duration, 2), "duration_source": got["duration_source"],
        "language": opts["language"],
        "params": {"ocr": "RapidOCR / PP-OCR (onnxruntime)", "fps": got["fps"],
                   "frames": got["frames"], "sample_width": SAMPLE_W,
                   "region": list(opts["region"]), "band": got["band_used"],
                   "min_score": opts["min_score"], "note": got["note"],
                   "furniture": got.get("furniture") or {},
                   "translations_dropped": got.get("translations", 0)},
        "timestamps": True, "segments": segs, "text": body, "chars": len(body),
        "stats": stats, "error": None,
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_json)), exist_ok=True)
    write_pair(out_json, data, out_md, build_md(key, meta, segs, stats))
    rec.update({"status": "ok", "segments": len(segs), "chars": len(body),
                "band": got["band_used"], "reason": got["note"]})
    return rec


def list_one(path, engine, opts):
    """Cheap answer to 'does this clip carry burned-in subtitles at all?'"""
    print(path)
    try:
        info = probe_media(path, opts["ffprobe"], opts["timeout"])
    except RuntimeError as exc:
        print("  [ERROR] %s" % exc)
        return
    duration = info["duration"]
    if duration <= 0:
        print("  [ERROR] cannot read duration")
        return
    n = opts["calib_frames"]
    fps = min(max(n / duration, 0.05), 2.0)
    tmp = tempfile.mkdtemp(prefix="ocr-list-")
    try:
        frames = extract_frames(path, tmp, fps, opts["ffmpeg"], opts["timeout"])
        heights, samples, texts = [], [], []
        for fp in frames:
            img = Image.open(fp)
            heights.append(img.size[1])
            boxes = ocr_boxes(engine, img, opts["region"][0],
                              opts["region"][1], opts["min_score"])
            samples.append({"boxes": boxes})
            got, _ = reading_of(boxes, img.size[1], opts["language"])
            if got:
                texts.append(got)
            img.close()
        band = calibrate(samples, heights)
        print("  video   : %dx%d, %.2fs, %d sampled frames"
              % (info["width"], info["height"], duration, len(frames)))
        if band:
            print("  band    : y=%.3f-%.3f | present in %.0f%% of frames | "
                  "%d distinct lines | line height %.1f%%"
                  % (band["y0"], band["y1"], band["support"] * 100,
                     band["distinct"], band["mean_h"] * 100))
            print("  verdict : burned-in subtitles")
            for t in texts[:3]:
                print("  sample  : %s" % t[:44])
        elif texts:
            print("  band    : none (static text only, or no lower-band text)")
            print("  verdict : on-screen text exists but does not change like a subtitle")
            print("  sample  : %s" % texts[0][:44])
        else:
            print("  band    : none")
            print("  verdict : no on-screen text in the lower half")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(
        description="Read burned-in subtitles with PP-OCR into the same JSON + "
                    "MD schema as transcribe.py (engine=ocr).")
    ap.add_argument("--input", nargs="+", required=True,
                    help="video file(s) or folder(s)")
    ap.add_argument("--out-dir", default=None, help="where <key>.json/.md go")
    ap.add_argument("--fps", type=float, default=1.0,
                    help="sampled frames per second (default 1.0; subtitles rarely "
                         "change faster, and cost is linear in this number)")
    ap.add_argument("--region", default="%.3f,%.3f" % REGION,
                    help="vertical search window as fractions, default 0.50,0.995")
    ap.add_argument("--min-score", type=float, default=0.5,
                    help="drop OCR lines below this confidence (default 0.5)")
    ap.add_argument("--language", default="zh",
                    help="which line to keep when the subtitle is bilingual: "
                         "zh keeps the Chinese line and drops the translation, "
                         "en the other way round")
    ap.add_argument("--no-band", action="store_true",
                    help="skip band detection and read the whole --region "
                         "(debugging; pulls in titles and watermarks)")
    ap.add_argument("--calib-frames", type=int, default=CALIB_FRAMES,
                    help="frames used to locate the band (default 16)")
    ap.add_argument("--max-frames", type=int, default=MAX_FRAMES,
                    help="hard cap on sampled frames (default 900)")
    ap.add_argument("--recursive", action="store_true", help="descend into subfolders")
    ap.add_argument("--list", action="store_true",
                    help="only probe: is there a burned-in subtitle band?")
    ap.add_argument("--force", action="store_true", help="re-read even if output exists")
    ap.add_argument("--timeout", type=int, default=600, help="per ffmpeg/ffprobe call, seconds")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    a = ap.parse_args()

    try:
        parts = [float(x) for x in a.region.split(",")]
        if len(parts) != 2 or not 0 <= parts[0] < parts[1] <= 1:
            raise ValueError
    except ValueError:
        print("[ERROR] --region must be two fractions like 0.50,0.995")
        sys.exit(2)

    for tool in (a.ffmpeg, a.ffprobe):
        if shutil.which(tool) is None and not os.path.isfile(tool):
            print("[ERROR] %s not found on PATH; pass --ffmpeg/--ffprobe" % tool)
            sys.exit(2)
    try:
        engine = load_engine()
    except RuntimeError as exc:
        print("[ERROR] %s" % exc)
        sys.exit(2)

    files = collect(a.input, a.recursive)
    if not files:
        print("[ERROR] no input file matched: %s" % ", ".join(a.input))
        sys.exit(2)

    opts = {"fps": max(a.fps, 0.01), "region": (parts[0], parts[1]),
            "min_score": a.min_score, "language": a.language,
            "no_band": a.no_band, "calib_frames": max(a.calib_frames, 2),
            "max_frames": max(a.max_frames, 10), "timeout": a.timeout,
            "ffmpeg": a.ffmpeg, "ffprobe": a.ffprobe}

    if a.list:
        for f in files:
            list_one(f, engine, opts)
        return

    if not a.out_dir:
        print("[ERROR] --out-dir is required unless --list is used")
        sys.exit(2)
    try:
        os.makedirs(a.out_dir, exist_ok=True)
    except OSError as exc:
        print("[ERROR] cannot create --out-dir %s: %s" % (a.out_dir, exc))
        sys.exit(2)

    out_real = os.path.realpath(a.out_dir)

    used = {}

    def key_of(path):
        """Unique output stem. Two folders can both hold a 01.mp4, and letting
        the second one silently 'skip' because the first wrote 01.json would
        drop a file without saying so."""
        stem = re.sub(r'[<>:"|?*\\]', "_",
                      os.path.splitext(os.path.basename(path))[0]).strip().rstrip(".") or "clip"
        key, n = stem, 2
        real = os.path.realpath(path)
        while key in used and used[key] != real:
            key = "%s-%d" % (stem, n)
            n += 1
        used[key] = real
        return key

    opts.update({"out_dir": a.out_dir, "force": a.force, "key_of": key_of})

    records, total = [], len(files)
    print("out-dir : %s" % os.path.abspath(a.out_dir))
    print("files   : %d | sampling %.2f fps | min score %.2f"
          % (total, opts["fps"], opts["min_score"]))
    for idx, f in enumerate(files, 1):
        if os.path.realpath(f).startswith(out_real + os.sep):
            continue
        rec = process_one(f, engine, opts)
        records.append(rec)
        name = os.path.basename(f)
        if len(name) > 34:
            name = name[:31] + "..."
        if rec["status"] == "ok":
            band = rec["band"] or [0, 0]
            print("[%*d/%d] ok       %-34s %3d cues / %5d chars  band %.3f-%.3f"
                  % (len(str(total)), idx, total, name, rec["segments"],
                     rec["chars"], band[0], band[1]))
        elif rec["status"] == "skipped":
            print("[%*d/%d] skipped  %-34s %s"
                  % (len(str(total)), idx, total, name, rec["reason"]))
        else:
            print("[%*d/%d] NO-OCR   %-34s %s"
                  % (len(str(total)), idx, total, name, rec["reason"]))

    ok = sum(1 for r in records if r["status"] == "ok")
    skipped = sum(1 for r in records if r["status"] == "skipped")
    bad = len(records) - ok - skipped
    print("")
    print("done: %d read, %d skipped, %d without a readable subtitle band"
          % (ok, skipped, bad))
    if bad:
        print("next: those clips either have no burned-in subtitles or put them "
              "outside the lower half; transcribe the audio instead.")
    sys.exit(0 if records and bad < len(records) else 3)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure") and not sys.stdout.isatty():
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    main()
