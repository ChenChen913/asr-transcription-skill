# -*- coding: utf-8 -*-
"""Dependency-free regression tests for scripts/qc_check.py.

Every case maps to a concrete risk (a real past bug or an input-validity
boundary). Each case is run in BOTH text mode and --json mode; we assert the
exit code, the severity class, the diagnostic keyword, the absence of false
positives, and the stability of the legacy JSON keys.

No network, no model weights, no GPU, no third-party packages.

Run:  python tests/run_smoke.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
QC = os.path.join(ROOT, "scripts", "qc_check.py")
PY = sys.executable

# Keys that external callers already depend on. Adding fields is fine;
# removing/renaming any of these is a breaking change.
LEGACY_KEYS = ["file", "error", "red", "warn", "duration", "has_timestamps",
               "note", "chars", "segments", "punct_per_100", "chars_per_min",
               "segs_per_min", "max_gap_s", "tail_gap_s", "tail_overrun_s"]
NEW_KEYS = ["status", "errors", "thresholds"]


def seg(s, e, t):
    return {"s": s, "e": e, "t": t}


def doc(duration, segments, timestamps=None):
    d = {"duration": duration, "segments": segments}
    if timestamps is not None:
        d["timestamps"] = timestamps
    return d


def normal_doc():
    segs = [seg(i * 5.0, i * 5.0 + 4.5, "这是第%d句正常内容，包含足够的文字。" % i)
            for i in range(12)]
    return doc(60.0, segs, True)


# name, risk, raw payload (dict / raw string / None=missing file),
# expected exit code, expected severity, keywords that MUST appear,
# keywords that must NOT appear
CASES = [
    # ---- original 5 smoke cases (kept) ----
    ("normal", "clean transcript must pass with exit 0",
     normal_doc(), 0, "OK", [], ["LONG GAP", "TAIL OVERRUN"]),
    ("no_timestamps", "pure-text engine output must not fake 0.00 tail/gap",
     doc(105.49, [seg(0.0, 105.49, "整段纯文本，没有真实时间信息。")], False),
     1, "REVIEW", ["NO TIMESTAMPS"], []),
    ("tail_overrun", "hallucinated closing segment past audio end",
     doc(115.24, [seg(0.0, 110.0, "正常正文内容。"), seg(114.66, 144.64, "谢谢大家")]),
     1, "REVIEW", ["TAIL OVERRUN"], []),
    ("long_gap", "17 s of real speech silently dropped",
     doc(84.68, [seg(0.0, 43.28, "前半段内容。"), seg(60.94, 84.0, "后半段内容。")]),
     1, "REVIEW", ["LONG GAP"], []),
    ("prompt_echo", "initial_prompt read into the transcript",
     doc(105.49, [seg(0.0, 29.98, "请使用简体中文，并加上标点符号。"),
                  seg(30.0, 105.0, "后续真实内容。")]),
     1, "REVIEW", ["head prompt echo"], []),

    # ---- P0: strict threshold boundaries (strictly greater-than) ----
    ("gap_exact_8", "gap == 8.00 s must NOT trigger (equality passes)",
     doc(60.0, [seg(0, 10, "前"), seg(18, 59, "后")]),
     0, "OK", [], ["LONG GAP"]),
    ("gap_8_01", "gap 8.01 s must trigger",
     doc(60.0, [seg(0, 10, "前"), seg(18.01, 59, "后")]),
     1, "REVIEW", ["LONG GAP"], []),
    ("overrun_0_4", "0.40 s tail overrun is within tolerance",
     doc(60.0, [seg(0, 60.4, "正文")]), 0, "OK", [], ["TAIL OVERRUN", "NO TIMESTAMPS"]),
    ("overrun_0_6", "0.60 s tail overrun must trigger",
     doc(60.0, [seg(0, 60.6, "正文")]), 1, "REVIEW", ["TAIL OVERRUN"], []),

    # ---- P0: deterministic ERROR inputs (must be non-zero exit) ----
    ("missing_start", "segment without start must be diagnosed, not defaulted to 0",
     {"duration": 60, "segments": [{"e": 5, "t": "你好"}]},
     1, "ERROR", ["start missing"], []),
    ("non_numeric", "string timestamp must be ERROR, not a Python traceback",
     {"duration": 60, "segments": [{"s": "abc", "e": 5, "t": "你好"}]},
     1, "ERROR", ["not numeric"], ["Traceback"]),
    ("negative_start", "negative start time",
     {"duration": 60, "segments": [seg(-3, 5, "你好")]},
     1, "ERROR", ["start < 0"], []),
    ("end_before_start", "end earlier than start",
     {"duration": 60, "segments": [seg(10, 5, "你好")]},
     1, "ERROR", ["end < start"], []),
    ("out_of_order", "segments not sorted by time",
     {"duration": 60, "segments": [seg(20, 25, "后"), seg(0, 5, "先")]},
     1, "ERROR", ["out of order"], ["overlapping"]),
    ("segments_badtype", "segments is an object instead of a list",
     {"duration": 60, "segments": {"a": 1}},
     1, "ERROR", ["segments must be a list"], []),
    ("segments_empty", "segments is an empty list",
     {"duration": 60, "segments": []},
     1, "ERROR", ["segments is empty"], []),
    ("no_duration", "missing duration -> dependent checks explicitly skipped",
     {"segments": [seg(0, 30, "正文")]},
     1, "ERROR", ["duration missing", "无法检查"], ["TAIL OVERRUN"]),
    ("zero_duration", "duration == 0 must not divide by zero or fake a pass",
     {"duration": 0, "segments": [seg(0, 30, "正文")]},
     1, "ERROR", ["duration must be > 0"], ["TAIL OVERRUN"]),
    ("bad_json", "corrupt JSON must give a readable error, not a traceback",
     "{not json", 1, "ERROR", ["invalid JSON"], ["Traceback"]),
    ("empty_json", "empty object",
     {}, 1, "ERROR", ["duration missing", "MISSING segments"], []),
    ("not_found", "missing file must be non-zero exit",
     None, 1, "ERROR", ["file not found"], []),

    # ---- P0: non-finite numbers must never pass as OK ----
    ("dur_nan", "NaN duration used to return status OK with exit 0",
     {"duration": float("nan"), "timestamps": True,
      "segments": [seg(0, 5, "正文内容足够长的一句话。")]},
     1, "ERROR", ["duration not finite"], ["Traceback"]),
    ("dur_inf", "infinite duration used to emit inf metrics and pass",
     {"duration": float("inf"), "timestamps": True,
      "segments": [seg(0, 5, "正文内容足够长的一句话。")]},
     1, "ERROR", ["duration not finite"], ["Traceback"]),
    ("dur_neg_inf", "-Infinity duration",
     {"duration": float("-inf"), "timestamps": True,
      "segments": [seg(0, 5, "正文内容足够长的一句话。")]},
     1, "ERROR", ["duration not finite"], []),
    ("start_nan", "NaN segment start used to pass as OK with fake gap metrics",
     {"duration": 60.0, "timestamps": True,
      "segments": [seg(float("nan"), 5, "正文内容够长的一句话。"),
                   seg(6, 9, "后续内容也够长。")]},
     1, "ERROR", ["start not finite"], ["Traceback"]),
    ("mid_start_nan", "NaN in the middle used to leak NaN into --json output",
     {"duration": 60.0, "timestamps": True,
      "segments": [seg(0, 3, "甲乙丙丁戊己庚辛。"),
                   seg(float("nan"), 5, "壬癸子丑寅卯辰。"),
                   seg(8.5, 10, "巳午未申酉戌亥。")]},
     1, "ERROR", ["start not finite"], ["Traceback"]),
    ("end_inf", "Infinity end used to be REVIEW with '+infs' instead of ERROR",
     {"duration": 60.0, "timestamps": True,
      "segments": [seg(0, float("inf"), "正文内容够长的一句话。")]},
     1, "ERROR", ["end not finite"], ["TAIL OVERRUN"]),
    ("huge_int", "int beyond float range must give a field error, not OverflowError",
     {"duration": 10 ** 400,
      "segments": [seg(0, 5, "正文内容足够长的一句话。")]},
     1, "ERROR", ["not finite"], ["OverflowError"]),

    # ---- P0: REVIEW signals (risk, not proven error) ----
    ("overlap", "adjacent segments overlap -> ask a human, do not call it a bug",
     {"duration": 60, "segments": [seg(0, 10, "前"), seg(8, 20, "后")]},
     1, "REVIEW", ["overlapping segments"], ["out of order"]),
    ("empty_text", "whitespace-only segment",
     {"duration": 60, "segments": [seg(0, 5, "   "), seg(5, 59, "正文")]},
     1, "REVIEW", ["empty/whitespace-only"], []),
    ("text_fallback", "top-level text with no segments is usable but flagged",
     {"duration": 60, "text": "只有纯文本没有分段。"},
     1, "REVIEW", ["text-only fallback"], []),

    # ---- text normalization / false-positive guards ----
    ("nfkc_punct", "U+FE50 small comma must survive normalization",
     {"duration": 60, "timestamps": True,
      "segments": [seg(0, 59, "你好\ufe50世界\ufe51后面还有内容") ]},
     0, "OK", [], ["LONG GAP"]),
    ("closing_word_in_head_is_warn", "genuine '订阅' in head is a warning, not RED",
     {"duration": 60, "timestamps": True,
      "segments": [seg(0, 30, "今天我们讲讲订阅这个词的用法，内容比较长。"),
                   seg(30, 59, "后面还有很多正常的内容要继续讲下去。")]},
     0, "OK", [], ["head prompt echo"]),
]


def run(path, json_mode=False):
    cmd = [PY, QC, path] + (["--json"] if json_mode else [])
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return r.returncode, r.stdout + r.stderr


def _reject_constant(token):
    """json.loads accepts NaN/Infinity by default; strict JSON does not."""
    raise ValueError("non-standard JSON constant: " + token)


# Cases whose timestamp fields are unusable: no time metric may be reported.
TS_INVALID_CASES = ("start_nan", "mid_start_nan", "end_inf",
                    "missing_start", "non_numeric")

# Timestamps parse to finite floats, but the timeline itself is nonsense.
# These used to be ERROR *and* still report time metrics: out-of-order input
# produced max_gap_s = -25.00 / tail_overrun_s = -25.00 with
# has_timestamps = true. Now every time metric must be null while the
# text-derived metrics stay usable.
TIMELINE_INVALID_CASES = ("negative_start", "end_before_start", "out_of_order")


def main():
    checks = [0]
    failures = []

    def assert_that(cond, label):
        checks[0] += 1
        if not cond:
            failures.append(label)

    with tempfile.TemporaryDirectory(prefix="qc_smoke_") as tmp:
        for name, risk, payload, exp_rc, exp_status, must, must_not in CASES:
            if payload is None:
                path = os.path.join(tmp, "definitely_missing_" + name + ".json")
            else:
                path = os.path.join(tmp, name + ".json")
                with open(path, "w", encoding="utf-8") as f:
                    if isinstance(payload, str):
                        f.write(payload)
                    else:
                        json.dump(payload, f, ensure_ascii=False)

            for mode in (False, True):
                rc, out = run(path, json_mode=mode)
                tag = "%-28s [%s]" % (name, "json" if mode else "text")
                assert_that(rc == exp_rc,
                            "%s exit=%d expected=%d" % (tag, rc, exp_rc))
                for kw in must:
                    assert_that(kw in out, "%s missing diagnostic %r" % (tag, kw))
                for kw in must_not:
                    assert_that(kw not in out, "%s false positive %r" % (tag, kw))
                if not mode and name in TIMELINE_INVALID_CASES:
                    # Text mode must not print fabricated gap/tail numbers.
                    assert_that("N/A (invalid timeline)" in out,
                                "%s text mode must say why the metrics are N/A" % tag)
                if mode:
                    try:
                        rows = json.loads(out, parse_constant=_reject_constant)
                        row = rows[0]
                        assert_that(row.get("status") == exp_status,
                                    "%s status=%r expected=%r"
                                    % (tag, row.get("status"), exp_status))
                        for k in LEGACY_KEYS + NEW_KEYS:
                            assert_that(k in row, "%s legacy/new key %r lost" % (tag, k))
                        if exp_status == "ERROR":
                            assert_that(len(row.get("errors", [])) > 0,
                                        "%s ERROR must carry errors[]" % tag)
                        if exp_status == "REVIEW":
                            assert_that(len(row.get("red", [])) > 0,
                                        "%s REVIEW must carry red[]" % tag)
                        if name in TS_INVALID_CASES + TIMELINE_INVALID_CASES:
                            assert_that(row.get("max_gap_s") is None
                                        and row.get("tail_gap_s") is None
                                        and row.get("tail_overrun_s") is None,
                                        "%s time metrics must be null, not fabricated" % tag)
                            assert_that(row.get("has_timestamps") is False,
                                        "%s has_timestamps must be False" % tag)
                        if name in TIMELINE_INVALID_CASES:
                            assert_that(row.get("note", "").startswith("INVALID TIMELINE"),
                                        "%s note must name the timeline problem, got %r"
                                        % (tag, row.get("note", "")[:40]))
                            # The text is still readable; do not throw it away.
                            assert_that(row.get("chars", 0) > 0,
                                        "%s text metrics must survive" % tag)
                            assert_that(row.get("chars_per_min") is not None
                                        and row.get("punct_per_100") is not None,
                                        "%s text-derived metrics must be present" % tag)
                        if name == "no_timestamps":
                            assert_that(row.get("max_gap_s") is None
                                        and row.get("tail_overrun_s") is None,
                                        "%s gap/tail must be N/A" % tag)
                        if name in ("no_duration", "zero_duration"):
                            assert_that(row.get("duration") is None,
                                        "%s duration must be null" % tag)
                            assert_that(row.get("tail_overrun_s") is None,
                                        "%s overrun must be null, not 0.00" % tag)
                        if name == "nfkc_punct":
                            assert_that(row.get("punct_per_100", 0) > 0,
                                        "%s U+FE50 punctuation must be counted" % tag)
                        th = row.get("thresholds", {})
                        if th:
                            assert_that(th.get("gap_gt") == 8.0
                                        and th.get("overrun_gt") == 0.5,
                                        "%s thresholds changed" % tag)
                    except (ValueError, IndexError, KeyError) as e:
                        assert_that(False, "%s JSON parse failed: %s" % (tag, e))
            print("%-30s rc=%d status=%-6s | %s" % (name, exp_rc, exp_status, risk))

    print()
    print("%d cases x (text + --json), %d assertions, %d failures."
          % (len(CASES), checks[0], len(failures)))
    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("All assertions passed. No network / model / GPU required.")


if __name__ == "__main__":
    main()
