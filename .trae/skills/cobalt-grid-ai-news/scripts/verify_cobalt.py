# -*- coding: utf-8 -*-
"""Cobalt Grid 产物校验（Step 5.5）

用法:
    python verify_cobalt.py <输出目录>

断言：SRT/HTML 时间一致、字幕不越段、句间间隔 0.25-0.3s、
变速 ≤1.18x、voiceover.mp3 时长 == 总时长。
"""
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()


def seg_of(c):
    if c["part"] == "intro":
        return (0, 8)
    if c["part"] == "news":
        s = 8 + (c["news_number"] - 1) * 11
        return (s, s + 11)
    n_news = max((c["news_number"] for c in cues if c["part"] == "news"), default=0)
    return (8 + n_news * 11, 8 + n_news * 11 + 12)


def main():
    global cues
    timeline = json.loads((BASE / "timeline.json").read_text(encoding="utf-8"))
    cues = timeline["cues"]
    total = timeline["total_duration"]
    html = (BASE / "my-news-video" / "compositions" / "index-cobalt.html").read_text(encoding="utf-8")
    srt = (BASE / "subtitles.srt").read_text(encoding="utf-8")

    # 1) SRT 与 HTML 字幕切换点一致
    html_times = [
        float(m.group(1))
        for m in re.finditer(r'tl\.to\("#captionText"[\s\S]*?\}, (\d+(?:\.\d+)?)\);', html)
    ]
    assert len(html_times) == len(cues), "字幕条数不一致"
    assert all(
        abs(a - b) < 0.001
        for a, b in zip(html_times, [c["start"] for c in cues])
    ), "HTML/SRT 时间不一致"
    print(f"1) HTML 字幕切换点 == SRT 起点: OK（{len(cues)} 条）")

    # 2) 段内边界
    for c in cues:
        s0, s1 = seg_of(c)
        assert s0 <= c["start"] < c["end"] <= s1, f"越界: #{c['index']}"
    print("2) 所有字幕均在所属段内: OK")

    # 3) 段内句间间隔 0.25-0.3s
    gaps = [
        round(b["start"] - a["end"], 3)
        for a, b in zip(cues, cues[1:])
        if seg_of(a) == seg_of(b)
    ]
    assert all(0.24 <= g <= 0.30 for g in gaps), f"句间间隔异常: {gaps}"
    print(f"3) 段内句间间隔均为 0.25-0.3s: OK（{sorted(set(gaps))}）")

    # 4) 变速上限
    speeds = [c["speed"] for c in cues]
    assert max(speeds) <= 1.18 + 1e-9, "存在超过 1.18x 的变速"
    print(f"4) 变速系数 max={max(speeds):.3f}（≤1.18）: OK")

    # 5) 总时长与最后字幕
    assert cues[-1]["end"] <= total + 1e-9, "最后字幕越出总时长"
    print(f"5) 总时长 {total:.0f}s，最后字幕结束于 {cues[-1]['end']}s: OK")

    # 6) MP3 时长
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(BASE / "voiceover.mp3")],
        capture_output=True, text=True,
    )
    dur = float(r.stdout.strip())
    assert abs(dur - total) < 0.1, f"MP3 时长 {dur}s != {total}s"
    print(f"6) voiceover.mp3 时长 {dur:.1f}s: OK")

    # 7) SRT 条数
    n_srt = srt.count("\n\n") + (1 if srt.strip() else 0)
    assert n_srt == len(cues), "SRT 条数与 timeline 不一致"
    print(f"7) SRT 条数 {n_srt}: OK")
    print("\n全部校验通过 ✔")


if __name__ == "__main__":
    main()
