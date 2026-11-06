# -*- coding: utf-8 -*-
"""Extract embedded text subtitles into the same JSON + MD the ASR path writes.

Why this comes before transcribe.py: an embedded text subtitle track is a
zero-cost ground truth. No inference, no GPU, no misheard names - a few seconds
to get a timestamped script. If the file has one, transcribing it is pure loss.
So the order is: sidecar file -> embedded text track -> (v0.7) burned-in OCR ->
ASR. This script owns the first two steps only.

Deliberately NOT done here, each for a reason:

  * bitmap tracks (PGS / VobSub / DVB): what comes out is a stream of images,
    not text; reading them is OCR and belongs to v0.7.
  * burned-in subtitles: those are pixels in the video frame, not a track.
  * timeline repair: overlapping cues are written exactly as the source has
    them. Rewriting a timestamp is editing evidence, and the overlap count is
    reported instead so a human can decide.
  * translation / polishing: the text is copied verbatim, including its own
    punctuation habits. It is a subtitle, not a verbatim transcript - the MD
    header says so.

Timeline precision is 2 decimals to match transcribe.py, so qc_check.py sees
the same shape and the same rounding from both producers.

Exit codes: 0 at least one script produced | 2 bad usage / unusable input |
3 nothing produced (no text track, bitmap only, or extraction failed).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata

# Codecs whose payload is text. Anything not listed here and not in
# GRAPHIC_CODECS is treated as graphic: guessing wrong on an unknown codec
# yields a garbage "transcript", while refusing only costs an ASR run.
TEXT_CODECS = {
    "subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text",
    "microdvd", "subviewer", "subviewer1", "sami", "realtext",
    "jacosub", "mpl2", "pjs", "vplayer", "stl",
}
GRAPHIC_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}

SIDECAR_EXTS = (".srt", ".ass", ".ssa", ".vtt")

# A cue start line. Trailing junk is allowed on purpose: legacy SRT sometimes
# appends "X1:100 X2:200 Y1:300 Y2:400" after the arrow, and dropping those
# cues over a coordinate suffix would silently lose lines.
CUE_TIME = re.compile(
    r"^\s*(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})")

HTML_TAG = re.compile(r"<[^>]{0,300}>")
ASS_TAG = re.compile(r"\{[^{}]{0,300}\}")

LANG_ALIASES = {
    "zh": {"zh", "chi", "zho", "chs", "cht", "sc", "tc", "chinese", "cn"},
    "en": {"en", "eng", "english"},
    "ja": {"ja", "jpn", "japanese"},
    "ko": {"ko", "kor", "korean"},
}
LANG_CANON = {"chi": "zh", "zho": "zh", "eng": "en", "jpn": "ja", "kor": "ko"}


# ----------------------------------------------------------------- plumbing ---

def run(cmd, timeout):
    """(rc, stdout, stderr) with text already decoded; never raises on rc."""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ss: %s" % (timeout, cmd[0])
    except FileNotFoundError:
        return 127, "", "not found: %s" % cmd[0]
    return (p.returncode, p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def probe(path, ffprobe, timeout):
    """Container duration + every subtitle stream, with its global index.

    The global index (0:v / 0:a / 0:s share one numbering) is what -map needs;
    the per-type ordering is kept separately for --stream, because "the second
    subtitle track" is how a human counts them.
    """
    cmd = [ffprobe, "-v", "error", "-show_entries",
           "format=duration:stream=index,codec_type,codec_name"
           ":stream_tags=language,title", "-of", "json", path]
    rc, out, err = run(cmd, timeout)
    if rc != 0:
        raise RuntimeError((err.strip() or "ffprobe exit %d" % rc)[:400])
    try:
        data = json.loads(out or "{}")
    except ValueError as exc:
        raise RuntimeError("ffprobe returned unreadable JSON: %s" % exc)

    duration = None
    raw = (data.get("format") or {}).get("duration")
    try:
        duration = float(raw)
    except (TypeError, ValueError):
        pass

    subs, seen = [], 0
    for st in data.get("streams") or []:
        if st.get("codec_type") != "subtitle":
            continue
        tags = st.get("tags") or {}
        subs.append({"order": seen, "index": st.get("index"),
                     "codec": (st.get("codec_name") or "").lower(),
                     "language": (tags.get("language") or "").lower(),
                     "title": tags.get("title") or ""})
        seen += 1
    return {"duration": duration, "subs": subs}


def classify(codec):
    if codec in TEXT_CODECS:
        return "text"
    if codec in GRAPHIC_CODECS:
        return "graphic"
    return "graphic"          # unknown -> refuse, do not produce garbage


def lang_of(track):
    return LANG_CANON.get(track["language"], track["language"])


def lang_matches(track, want):
    want = want.lower()
    got = track["language"]
    return got == want or got in LANG_ALIASES.get(want, set())


def pick(subs, want_order=None, want_lang=None):
    """(track, reason). reason is empty on success and user-facing on refusal."""
    if not subs:
        return None, "no subtitle stream in the container"
    if want_order is not None:
        if not 0 <= want_order < len(subs):
            return None, "only %d subtitle stream(s); --stream %d is out of range" % (
                len(subs), want_order)
        t = subs[want_order]
        if classify(t["codec"]) != "text":
            return None, ("subtitle stream %d is %s (bitmap images, not text); "
                          "OCR is not part of this script" % (want_order, t["codec"]))
        return t, ""
    if want_lang:
        for t in subs:
            if classify(t["codec"]) == "text" and lang_matches(t, want_lang):
                return t, ""
        have = ", ".join(sorted({lang_of(t) or "und" for t in subs})) or "none"
        return None, "no text subtitle track in language '%s' (found: %s)" % (
            want_lang, have)
    for t in subs:
        if classify(t["codec"]) == "text":
            return t, ""
    kinds = ", ".join("%s(%s)" % (t["codec"], classify(t["codec"])) for t in subs)
    return None, ("only bitmap subtitle track(s): %s; extracting them yields "
                  "images and needs OCR (v0.7)" % kinds)


# ------------------------------------------------------------------ parsing ---

def cue_seconds(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def clean_text(lines):
    """Strip markup and fold a multi-line cue into one segment line.

    Joining matters: transcribe.py emits one text line per segment, so keeping
    '\\N' or a literal newline here would make the same file render differently
    depending on which producer wrote it. A space is inserted only between two
    ASCII alphanumerics - Chinese cues should not gain spaces at the fold.
    """
    out = ""
    for raw in lines:
        line = ASS_TAG.sub("", HTML_TAG.sub("", raw))
        line = line.replace("\\N", " ").replace("\\n", " ")
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if not out:
            out = line
        elif (out[-1].isascii() and out[-1].isalnum()
              and line[0].isascii() and line[0].isalnum()):
            out += " " + line
        else:
            out += line
    return out


def parse_cues(text):
    """Parse SRT-ish text into [{'s','e','t'}]; tolerant of CRLF, BOM, no index.

    Structured as "walk to a time line, then collect until the next one"
    instead of "split on blank lines". Blank-line splitting loses every cue in
    a file that omits the separator (common in machine-generated SRT), and it
    makes the cue index indistinguishable from a one-line cue whose text is a
    number ("2024", "3:2"). Here a bare number is only dropped when it sits
    directly before the next time line, which in a well-formed file can only
    be an index.
    """
    lines = text.splitlines()
    segs = []
    i = 0
    while i < len(lines):
        m = CUE_TIME.match(lines[i].lstrip("\ufeff").strip())
        if not m:
            i += 1
            continue
        start = round(cue_seconds(*m.group(1, 2, 3, 4)), 2)
        end = round(cue_seconds(*m.group(5, 6, 7, 8)), 2)
        i += 1
        buf = []
        while i < len(lines):
            line = lines[i].strip()
            if not line or CUE_TIME.match(line):
                break
            buf.append(line)
            i += 1
        if buf and buf[-1].isdigit() and i < len(lines) \
                and CUE_TIME.match(lines[i].strip()):
            buf.pop()
        body = clean_text(buf)
        if body:
            segs.append({"s": start, "e": end, "t": body})
        # A cue that cleans down to nothing (pure markup / positioning) is
        # dropped rather than emitted as an empty segment: qc_check would flag
        # it as "empty text" on every single line.
    return segs


def sort_and_measure(segs, duration):
    """Keep source order; count what a human needs to know about the timeline."""
    overlaps, gaps, empty = 0, [], 0
    prev_end = None
    for x in segs:
        if x["e"] < x["s"]:
            x["s"], x["e"] = x["e"], x["s"]      # 0-length reversal, not a re-time
        if prev_end is not None:
            if x["s"] < prev_end - 0.001:
                overlaps += 1
            elif x["s"] - prev_end > 0:
                gaps.append(x["s"] - prev_end)
        prev_end = max(prev_end or 0, x["e"])
    return {"overlaps": overlaps,
            "max_gap_s": round(max(gaps), 2) if gaps else 0.0,
            "covered_s": round(sum(max(0.0, x["e"] - x["s"]) for x in segs), 2)}


def punct_per_100(text):
    n = len(re.findall(r"[，。！？、,.!?;；:：]", unicodedata.normalize("NFKC", text)))
    return round(n / max(len(text), 1) * 100, 2)


# ------------------------------------------------------------------- output ---

def write_pair(json_path, data, md_path, lines):
    """Same atomic-pair contract as transcribe.py: MD lands before JSON.

    The order is the invariant: "json exists" must imply "md exists", so a kill
    between the two writes can never leave a complete-looking record next to a
    missing transcript.
    """
    jtmp, mtmp = json_path + ".tmp", md_path + ".tmp"
    with open(mtmp, "w", encoding="utf-8", newline="\n") as fh:
        for line in lines:
            fh.write(line)
    with open(jtmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(mtmp, md_path)
    os.replace(jtmp, json_path)


def is_done(json_path, md_path):
    if not (os.path.exists(json_path) and os.path.exists(md_path)):
        return False
    try:
        if os.path.getsize(json_path) <= 0 or os.path.getsize(md_path) <= 0:
            return False
        with open(json_path, encoding="utf-8") as fh:
            d = json.load(fh)
        return isinstance(d, dict) and isinstance(d.get("segments"), list)
    except Exception:
        return False


def build_md(tag, meta, segs, stats):
    head = ["# %s\n\n" % tag,
            "- 来源: %s | 时长: %.2fs | %d 条 / %d 字\n"
            % (meta["source_label"], meta["duration"], len(segs), meta["chars"]),
            "- 这是源文件自带的字幕，**不是语音识别结果**：用词与标点是字幕作者的口径，"
            "可能经过润色或省略语气词。\n"]
    if stats["overlaps"]:
        head.append("- 源字幕有 %d 处时间重叠，已原样保留，未改动时间轴。\n"
                    % stats["overlaps"])
    if meta["duration"] and stats["covered_s"] / meta["duration"] < 0.5:
        head.append("- 字幕只覆盖了 %.0f%% 的时长，中间可能有空档。\n"
                    % (stats["covered_s"] / meta["duration"] * 100))
    head.append("\n")
    body = ["[%.2f - %.2f] %s\n\n" % (x["s"], x["e"], x["t"]) for x in segs]
    return head + body


def source_label(track, sidecar):
    if sidecar:
        return "外挂字幕文件 %s" % os.path.basename(sidecar)
    lang = lang_of(track) or "und"
    label = "内嵌字幕轨 #%d (%s, %s)" % (track["order"], track["codec"], lang)
    if track["title"]:
        label += " 「%s」" % track["title"]
    return label


# ---------------------------------------------------------------- one input ---

def find_sidecar(path, enabled=True):
    """Same-stem .srt/.ass/.ssa/.vtt sitting next to the media file."""
    if not enabled:
        return None
    stem = os.path.splitext(path)[0]
    for ext in SIDECAR_EXTS:
        cand = stem + ext
        if os.path.isfile(cand):
            return cand
    return None


def to_srt_text(src_path, ffmpeg, timeout):
    """Read a subtitle file as SRT text, converting ASS/VTT through ffmpeg.

    Writing our own ASS parser would mean owning style overrides, karaoke tags
    and \\pos blocks; ffmpeg already does that conversion and is a hard
    dependency of this skill anyway.
    """
    if src_path.lower().endswith((".srt",)):
        with open(src_path, "rb") as fh:
            raw = fh.read()
        # BOM first. A UTF-16 file is mostly NUL bytes, and gb18030 happily
        # decodes almost any byte pair - so trying the codecs in a fixed order
        # would turn a Notepad "Unicode" save into plausible-looking garbage
        # instead of an error. The BOM is the only unambiguous signal.
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            try:
                return raw.decode("utf-16"), ""
            except UnicodeDecodeError:
                pass
        for enc in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return raw.decode(enc), ""
            except UnicodeDecodeError:
                continue
        return None, "cannot decode subtitle file (tried utf-8 / gb18030 / utf-16)"
    tmp = src_path + ".as-srt.tmp.srt"
    rc, _, err = run([ffmpeg, "-y", "-v", "error", "-i", src_path,
                      "-c:s", "srt", "-f", "srt", tmp], timeout)
    if rc != 0:
        return None, "ffmpeg could not convert %s: %s" % (
            os.path.basename(src_path), err.strip()[:300])
    try:
        with open(tmp, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return text, ""


def extract_track(path, track, out_srt, ffmpeg, timeout):
    rc, _, err = run([ffmpeg, "-y", "-v", "error", "-i", path,
                      "-map", "0:%d" % track["index"], "-c:s", "srt",
                      "-f", "srt", out_srt], timeout)
    if rc != 0:
        return "ffmpeg could not extract stream #%d (%s): %s" % (
            track["order"], track["codec"], err.strip()[:300])
    if not os.path.exists(out_srt) or os.path.getsize(out_srt) == 0:
        return "stream #%d produced an empty subtitle file" % track["order"]
    return ""


def obtain_text(path, out_json, opts):
    """(text, track, sidecar, info, error).

    One ffprobe call serves both purposes: the real container duration and the
    stream list. Duration must come from the container even when a sidecar file
    is used - a sidecar ending at 6 s inside a 10 s video would otherwise report
    coverage 83% and a zero tail gap, both wrong.

    A broken sidecar falls through to the embedded track instead of failing the
    file: a half-downloaded .srt should not hide a perfectly good muxed track.
    """
    try:
        info = probe(path, opts["ffprobe"], opts["timeout"])
        probe_err = ""
    except RuntimeError as exc:
        info, probe_err = {"duration": None, "subs": []}, str(exc)

    notes = []
    sidecar = find_sidecar(path, not opts["no_sidecar"])
    if sidecar:
        text, err = to_srt_text(sidecar, opts["ffmpeg"], opts["timeout"])
        if err:
            notes.append("sidecar %s unusable (%s)" % (os.path.basename(sidecar), err))
        elif parse_cues(text):
            return text, None, sidecar, info, ""
        else:
            notes.append("sidecar %s had 0 usable lines" % os.path.basename(sidecar))

    track, why = pick(info["subs"], opts["stream"], opts["lang"])
    if track is None:
        notes.append(why)
        if probe_err:
            notes.append("ffprobe: " + probe_err)
        return None, None, sidecar, info, "; ".join(notes)

    out_srt = os.path.splitext(out_json)[0] + ".srt"
    err = extract_track(path, track, out_srt, opts["ffmpeg"], opts["timeout"])
    if err:
        notes.append(err)
        return None, None, sidecar, info, "; ".join(notes)
    try:
        with open(out_srt, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        notes.append("cannot read extracted subtitle: %s" % exc)
        return None, None, sidecar, info, "; ".join(notes)
    if not parse_cues(text):
        notes.append("embedded stream #%d had 0 usable lines" % track["order"])
        return None, None, sidecar, info, "; ".join(notes)
    return text, track, None, info, ""


def process_one(path, opts):
    """Return a record dict; never raises for expected failures."""
    rec = {"src": path, "status": "failed", "reason": "", "segments": 0,
           "chars": 0, "source": "", "out_json": ""}
    if not os.path.isfile(path):
        rec["reason"] = "input not found"
        return rec

    key = opts["key_of"](path)
    out_json = os.path.join(opts["out_dir"], key + ".json")
    out_md = os.path.splitext(out_json)[0] + ".md"
    rec["out_json"] = out_json

    if not opts["force"] and is_done(out_json, out_md):
        rec["status"] = "skipped"
        rec["reason"] = "already extracted"
        return rec

    text, track, sidecar, info, err = obtain_text(path, out_json, opts)
    if err:
        rec["reason"] = err
        return rec

    # obtain_text() already refused anything that parses to zero cues, so this
    # second parse is the one whose result is used.
    segs = parse_cues(text)

    duration = info.get("duration")
    duration_source = "container"
    if not duration or duration <= 0:
        duration = round(segs[-1]["e"], 2)
        duration_source = "last subtitle cue (container duration unknown)"

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

    label = source_label(track, sidecar)
    meta = {"source_label": label, "duration": duration, "chars": len(body)}
    data = {
        "tag": key, "engine": "subtitle-track",
        "source_track": (None if sidecar else
                         {"order": track["order"], "index": track["index"],
                          "codec": track["codec"], "language": lang_of(track),
                          "title": track["title"]}),
        "sidecar": os.path.basename(sidecar) if sidecar else None,
        "source_label": label,
        "device": "none", "compute": "none", "wav": os.path.basename(path),
        "duration": round(duration, 2), "duration_source": duration_source,
        "language": (lang_of(track) if track else "und"),
        "params": {"extractor": "ffmpeg -c:s srt", "timeline": "as-authored"},
        "timestamps": True, "segments": segs, "text": body, "chars": len(body),
        "stats": stats, "error": None,
    }

    os.makedirs(os.path.dirname(os.path.abspath(out_json)), exist_ok=True)
    write_pair(out_json, data, out_md, build_md(key, meta, segs, stats))

    rec.update({"status": "ok", "segments": len(segs), "chars": len(body),
                "source": label, "reason": ""})
    if sidecar:
        rec["reason"] = "sidecar"
    return rec


# ---------------------------------------------------------------------- cli ---

def collect(inputs, recursive):
    files = []
    for p in inputs:
        if os.path.isdir(p):
            if recursive:
                for dirpath, dirnames, names in os.walk(p):
                    dirnames.sort()
                    files += [os.path.join(dirpath, n) for n in sorted(names)]
            else:
                files += [os.path.join(p, n) for n in sorted(os.listdir(p))
                          if os.path.isfile(os.path.join(p, n))]
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


def main():
    ap = argparse.ArgumentParser(
        description="Extract embedded text subtitles (or a sidecar .srt) into "
                    "the same JSON + MD schema as transcribe.py.")
    ap.add_argument("--input", nargs="+", required=True,
                    help="media file(s) or folder(s)")
    ap.add_argument("--out-dir", required=True, help="where <key>.json/.md/.srt go")
    ap.add_argument("--stream", type=int, default=None,
                    help="pick the N-th subtitle stream (0-based, as a human counts them)")
    ap.add_argument("--lang", default=None,
                    help="only accept a text track in this language (zh/en/ja/ko)")
    ap.add_argument("--no-sidecar", action="store_true",
                    help="ignore same-stem .srt/.ass/.ssa/.vtt next to the media")
    ap.add_argument("--recursive", action="store_true", help="descend into subfolders")
    ap.add_argument("--list", action="store_true",
                    help="only list subtitle streams, extract nothing")
    ap.add_argument("--force", action="store_true", help="re-extract even if output exists")
    ap.add_argument("--timeout", type=int, default=120, help="per ffmpeg/ffprobe call, seconds")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    a = ap.parse_args()

    if not a.list:
        for tool in (a.ffmpeg, a.ffprobe):
            if shutil.which(tool) is None and not os.path.isfile(tool):
                print("[ERROR] %s not found on PATH; pass --ffmpeg/--ffprobe" % tool)
                sys.exit(2)

    files = collect(a.input, a.recursive)
    if not files:
        print("[ERROR] no input file matched: %s" % ", ".join(a.input))
        sys.exit(2)
    if not a.list:
        try:
            os.makedirs(a.out_dir, exist_ok=True)
        except OSError as exc:
            print("[ERROR] cannot create --out-dir %s: %s" % (a.out_dir, exc))
            sys.exit(2)

    if a.list:
        for f in files:
            print(f)
            try:
                info = probe(f, a.ffprobe, a.timeout)
            except RuntimeError as exc:
                print("  [ERROR] %s" % exc)
                continue
            side = find_sidecar(f, not a.no_sidecar)
            if side:
                print("  sidecar : %s" % side)
            if not info["subs"]:
                print("  tracks  : none")
            for t in info["subs"]:
                print("  #%d  %-22s %-7s lang=%-5s %s%s"
                      % (t["order"], t["codec"], classify(t["codec"]),
                         lang_of(t) or "und", t["title"],
                         "" if classify(t["codec"]) == "text" else "  [needs OCR]"))
        return

    out_real = os.path.realpath(a.out_dir)

    def key_of(path):
        stem = os.path.splitext(os.path.basename(path))[0]
        return re.sub(r'[<>:"|?*\\]', "_", stem).strip().rstrip(".") or "clip"

    opts = {"out_dir": a.out_dir, "stream": a.stream, "lang": a.lang,
            "no_sidecar": a.no_sidecar, "force": a.force, "timeout": a.timeout,
            "ffmpeg": a.ffmpeg, "ffprobe": a.ffprobe, "key_of": key_of}

    records, total = [], len(files)
    print("out-dir : %s" % os.path.abspath(a.out_dir))
    print("files   : %d" % total)
    for idx, f in enumerate(files, 1):
        # Never ingest our own output when --out-dir sits inside --input.
        if os.path.realpath(f).startswith(out_real + os.sep):
            continue
        rec = process_one(f, opts)
        records.append(rec)
        name = os.path.basename(f)
        if len(name) > 38:
            name = name[:35] + "..."
        if rec["status"] == "ok":
            print("[%*d/%d] ok       %-38s %4d segs / %6d chars  %s"
                  % (len(str(total)), idx, total, name, rec["segments"],
                     rec["chars"], rec["source"]))
        elif rec["status"] == "skipped":
            print("[%*d/%d] skipped  %-38s %s"
                  % (len(str(total)), idx, total, name, rec["reason"]))
        else:
            print("[%*d/%d] NO-SUB   %-38s %s"
                  % (len(str(total)), idx, total, name, rec["reason"]))

    ok = sum(1 for r in records if r["status"] == "ok")
    skipped = sum(1 for r in records if r["status"] == "skipped")
    print("")
    print("done: %d extracted, %d skipped, %d without usable subtitles"
          % (ok, skipped, len(records) - ok - skipped))
    if len(records) - ok - skipped:
        print("next: those files have no text subtitle track - convert to WAV and "
              "run scripts/transcribe.py (or wait for v0.7 burned-in OCR).")
    sys.exit(0 if records and len(records) - ok - skipped < len(records) else 3)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure") and not sys.stdout.isatty():
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    main()
