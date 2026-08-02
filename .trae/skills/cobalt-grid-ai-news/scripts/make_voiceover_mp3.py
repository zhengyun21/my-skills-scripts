# -*- coding: utf-8 -*-
"""音频驱动时间轴生成器（Cobalt Grid AI News）

用法:
    python make_voiceover_mp3.py <输出目录>

输出目录必须包含 news-data.json（Step 2 产物）。
流程：edge-tts 逐条合成字幕语音 -> ffprobe 实测时长 -> 按规则排布
timeline.json -> 按时间轴拼接 voiceover.mp3（句间仅保留 0.25-0.3s 呼吸）。
"""
import json
import subprocess
import sys
from pathlib import Path

BASE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
DATA = BASE / "news-data.json"
TMP = BASE / "tmp_tts"
OUT = BASE / "voiceover.mp3"
TIMELINE = BASE / "timeline.json"

VOICE = "zh-CN-YunyangNeural"  # 新闻男声
RATE = "+12%"                  # 整体口播提速 12%（约 5 字/秒，中等偏快）
SR = 44100

INTRO_DUR = 8.0
NEWS_DUR = 11.0                # 每条新闻展示窗口
OUTRO_DUR = 12.0
INTRO_START = 0.35             # 段内第一条字幕起点
INTRO_GAP = 0.25               # 开场句间呼吸
NEWS_GAP = 0.28                # 新闻/结尾句间呼吸
SPEED_CAP = 1.18               # 变速上限（优先用 TTS rate，通常无需变速）
NEWS_TAIL_MAX = 1.5            # 段尾展示余量上限
NEWS_END_MARGIN = 0.2          # 段尾安全边距


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"命令失败: {' '.join(cmd)}\n{r.stderr[-2000:]}")
    return r


def probe_duration(path: Path) -> float:
    r = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    return float(r.stdout.strip())


def load_captions(data):
    """按播放顺序返回字幕：[(part, news_number, text)]"""
    items = []
    for i, c in enumerate(data["intro"]["captions"], 1):
        items.append(("intro", None, c))
    for n in data["news"]:
        for c in n["captions"]:
            items.append(("news", n["number"], c))
    for c in data["outroCaptions"]:
        items.append(("outro", None, c))
    return items


def synthesize(idx, text, tmp):
    raw = tmp / f"cue_{idx:02d}.mp3"
    run([
        "edge-tts", "--voice", VOICE, "--rate", RATE, "--text", text,
        "--write-media", str(raw),
    ])
    return str(raw)


def layout_pair(seg_start, d1, d2, seg_dur):
    """新闻/结尾：字幕1 = 段首+0.35s；字幕2 = 字幕1语音结束+0.28s。
    超限则段内均匀提速（上限 1.18x）；段尾展示余量 ≤1.5s；绝不越段。"""
    gap = NEWS_GAP
    avail_audio = seg_dur - NEWS_END_MARGIN - INTRO_START - gap
    total = d1 + d2
    speed = max(1.0, total / avail_audio)
    speed = min(speed, SPEED_CAP)
    d1a, d2a = d1 / speed, d2 / speed
    if seg_start + INTRO_START + d1a + gap + d2a > seg_start + seg_dur - NEWS_END_MARGIN:
        speed = total / avail_audio  # 兜底：保证语音完整
        d1a, d2a = d1 / speed, d2 / speed
    s1 = seg_start + INTRO_START
    e1 = s1 + d1a
    s2 = e1 + gap
    e2_audio = s2 + d2a
    display_end2 = max(e2_audio, seg_start + seg_dur - NEWS_TAIL_MAX)
    display_end2 = min(display_end2, seg_start + seg_dur - NEWS_END_MARGIN)
    return [
        {"start": s1, "end": e1, "audio_duration": d1, "speed": speed},
        {"start": s2, "end": max(display_end2, s2 + d2a),
         "audio_duration": d2, "speed": speed},
    ]


def layout_intro(d1, d2, d3):
    """开场：3 条字幕连续排布、间隔 0.25s，整段均匀提速至 ≤1.18x 并占满 8s。"""
    gap = INTRO_GAP
    avail_audio = INTRO_DUR - INTRO_START - 2 * gap
    total = d1 + d2 + d3
    speed = max(1.0, total / avail_audio)
    speed = min(speed, SPEED_CAP)
    da = [x / speed for x in (d1, d2, d3)]
    if INTRO_START + sum(da) + 2 * gap > INTRO_DUR:
        speed = total / avail_audio  # 兜底
        da = [x / speed for x in (d1, d2, d3)]
    s1 = INTRO_START
    e1 = s1 + da[0]
    s2 = e1 + gap
    e2 = s2 + da[1]
    s3 = e2 + gap
    return [
        {"start": s1, "end": e1, "audio_duration": d1, "speed": speed},
        {"start": s2, "end": e2, "audio_duration": d2, "speed": speed},
        {"start": s3, "end": INTRO_DUR, "audio_duration": d3, "speed": speed},
    ]


def main():
    TMP.mkdir(exist_ok=True)
    data = json.loads(DATA.read_text(encoding="utf-8"))
    captions = load_captions(data)
    total = INTRO_DUR + len(data["news"]) * NEWS_DUR + OUTRO_DUR
    print(f"共 {len(captions)} 条字幕，总时长 {total:.0f}s")

    # 1) 合成 + 实测
    measured = []
    for idx, (part, nnum, text) in enumerate(captions, 1):
        raw = synthesize(idx, text, TMP)
        dur = probe_duration(raw)
        measured.append({"part": part, "news_number": nnum, "text": text,
                         "natural": dur, "raw": raw})
        print(f"[{idx:02d}] {part:>5} {dur:5.2f}s  {text[:24]}")

    # 2) 排布时间轴
    cues = []
    intro_durs = [m["natural"] for m in measured if m["part"] == "intro"]
    for m, c in zip([x for x in measured if x["part"] == "intro"],
                    layout_intro(*intro_durs)):
        cues.append({**m, **c})
    news_map = {}
    for m in measured:
        if m["part"] == "news":
            news_map.setdefault(m["news_number"], []).append(m)
    for num in sorted(news_map):
        a, b = news_map[num]
        seg_start = INTRO_DUR + (num - 1) * NEWS_DUR
        l1, l2 = layout_pair(seg_start, a["natural"], b["natural"], NEWS_DUR)
        cues.append({**a, **l1})
        cues.append({**b, **l2})
    outro_ms = [m for m in measured if m["part"] == "outro"]
    outro_start = INTRO_DUR + len(news_map) * NEWS_DUR
    l1, l2 = layout_pair(outro_start, outro_ms[0]["natural"],
                         outro_ms[1]["natural"], OUTRO_DUR)
    cues.append({**outro_ms[0], **l1})
    cues.append({**outro_ms[1], **l2})

    cues.sort(key=lambda c: c["start"])
    for i, c in enumerate(cues, 1):
        c["index"] = i
        c["audio_after"] = round(c["natural"] / c["speed"], 3)
        c["start"] = round(c["start"], 3)
        c["end"] = round(c["end"], 3)

    timeline = {
        "voice": VOICE,
        "rate": RATE,
        "target_cps": 4.5,
        "total_duration": total,
        "intro": {"duration": INTRO_DUR, "start": INTRO_START, "gap": INTRO_GAP},
        "news": {"duration": NEWS_DUR, "gap": NEWS_GAP,
                 "start_offset": INTRO_DUR},
        "outro": {"duration": OUTRO_DUR},
        "speed_cap": SPEED_CAP,
        "cues": cues,
    }
    TIMELINE.write_text(json.dumps(timeline, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"timeline -> {TIMELINE}")

    # 3) 变速 + 静音补齐 + 拼接
    seg_paths = []
    cursor = 0.0
    for c in cues:
        gap = c["start"] - cursor
        if gap > 0.005:
            seg = TMP / f"gap_{c['index']:02d}.wav"
            run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", f"anullsrc=r={SR}:cl=stereo",
                "-t", f"{gap:.3f}", "-c:a", "pcm_s16le", str(seg),
            ])
            seg_paths.append(seg)
        display_dur = c["end"] - c["start"]
        fit = TMP / f"cue_{c['index']:02d}_fit.wav"
        af = f"aresample={SR},aformat=sample_fmts=s16:channel_layouts=stereo"
        if c["speed"] > 1.001:
            af += f",atempo={c['speed']:.4f}"
        af += f",apad=whole_dur={display_dur:.3f}"
        run([
            "ffmpeg", "-y", "-loglevel", "error", "-i", c["raw"],
            "-af", af, "-t", f"{display_dur:.3f}", "-c:a", "pcm_s16le",
            str(fit),
        ])
        seg_paths.append(fit)
        cursor = c["end"]

    tail = total - cursor
    if tail > 0.005:
        seg = TMP / "tail.wav"
        run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"anullsrc=r={SR}:cl=stereo",
            "-t", f"{tail:.3f}", "-c:a", "pcm_s16le", str(seg),
        ])
        seg_paths.append(seg)

    list_file = TMP / "concat.txt"
    list_file.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in seg_paths),
        encoding="utf-8",
    )
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c:a", "libmp3lame", "-b:a", "192k", str(OUT),
    ])
    final_dur = probe_duration(OUT)
    print(f"MP3 -> {OUT}  ({final_dur:.1f}s)")
    if abs(final_dur - total) > 0.1:
        raise RuntimeError(f"MP3 时长 {final_dur}s 与预期 {total}s 不符")


if __name__ == "__main__":
    main()
