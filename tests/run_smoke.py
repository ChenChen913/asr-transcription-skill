# -*- coding: utf-8 -*-
"""Dependency-free smoke tests for scripts/qc_check.py.

Constructs five synthetic transcript JSON files and asserts both the RED flags
and the process exit code (text mode and --json mode).

Run:  python tests/run_smoke.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
QC = os.path.join(os.path.dirname(HERE), "scripts", "qc_check.py")
PY = sys.executable


def case(name, duration, segments, timestamps=True):
    return {"name": name,
            "data": {"duration": duration, "timestamps": timestamps,
                     "segments": [{"s": s, "e": e, "t": t} for s, e, t in segments]}}


def normal():
    segs = []
    for i in range(12):
        segs.append((i * 5.0, i * 5.0 + 4.5, "这是第%d句正常内容，包含足够的文字。" % i))
    return case("normal", 60.0, segs)


CASES = [
    (normal(), 0, []),
    (case("no_timestamps", 105.49, [(0.0, 105.49, "整段纯文本，没有真实时间信息。")], False),
     1, ["NO TIMESTAMPS"]),
    (case("tail_overrun", 115.24, [(0.0, 110.0, "正常正文内容。"),
                                  (114.66, 144.64, "谢谢大家")]),
     1, ["TAIL OVERRUN"]),
    (case("long_gap", 84.68, [(0.0, 43.28, "前半段内容。"),
                              (60.94, 84.0, "后半段内容。")]),
     1, ["LONG GAP"]),
    (case("prompt_echo", 105.49, [(0.0, 29.98, "请使用简体中文，并加上标点符号。"),
                                  (30.0, 105.0, "后续真实内容。")]),
     1, ["head prompt echo"]),
]


def run(path, json_mode=False):
    cmd = [PY, QC, path] + (["--json"] if json_mode else [])
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return r.returncode, r.stdout + r.stderr


def main():
    failures = []
    with tempfile.TemporaryDirectory(prefix="qc_smoke_") as tmp:
        for spec, expected_rc, expected_flags in CASES:
            name = spec["name"]
            p = os.path.join(tmp, name + ".json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(spec["data"], f, ensure_ascii=False)
            for mode in (False, True):
                rc, out = run(p, json_mode=mode)
                tag = "%s [%s]" % (name, "json" if mode else "text")
                if rc != expected_rc:
                    failures.append("%s: exit=%d expected=%d" % (tag, rc, expected_rc))
                for flag in expected_flags:
                    if flag not in out:
                        failures.append("%s: missing flag %r" % (tag, flag))
                if mode and expected_flags:
                    try:
                        rows = json.loads(out[:out.rfind("]") + 1])
                        if rows and rows[0].get("has_timestamps") is False and name == "no_timestamps":
                            if rows[0].get("max_gap_s") is not None:
                                failures.append("%s: gap should be N/A" % tag)
                    except Exception:
                        pass
                print("%-28s exit=%d expected=%d %s" %
                      (tag, rc, expected_rc, "OK" if rc == expected_rc else "FAIL"))
    print()
    if failures:
        print("FAILED (%d):" % len(failures))
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("All %d smoke cases passed (text + --json)." % (len(CASES) * 2))


if __name__ == "__main__":
    main()
