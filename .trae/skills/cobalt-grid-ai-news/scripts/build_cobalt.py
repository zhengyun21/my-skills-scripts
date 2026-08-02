# -*- coding: utf-8 -*-
"""Cobalt Grid HTML/SRT 构建器

用法:
    python build_cobalt.py <输出目录> [--template <模板路径>]

输出目录必须包含 news-data.json 与 timeline.json（Step 4.1 产物）。
字幕时间轴唯一来源为 timeline.json；SRT 与 HTML 的 GSAP 字幕切换点完全一致。
"""
import json
import os
import re
import sys
from pathlib import Path

BASE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
TEMPLATE = Path(os.environ.get(
    "COBALT_TEMPLATE",
    str(Path(__file__).resolve().parents[1] / "templates" / "composition-template.html"),
))
DATA = BASE / "news-data.json"
TIMELINE = BASE / "timeline.json"
HTML_OUT = BASE / "my-news-video" / "compositions" / "index-cobalt.html"
SRT_OUT = BASE / "subtitles.srt"

INTRO_DUR = 8
NEWS_DUR = 11
OUTRO_DUR = 12


def js_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def fmt_srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_sections(news, date, news_count):
    out = []
    for item in news:
        n = item["number"]
        start = INTRO_DUR + (n - 1) * NEWS_DUR
        tags = item["tags"]
        tag_html = "".join(
            f'<span class="tag{" accent" if idx == item["accentTagIndex"] else ""}">{t}</span>'
            for idx, t in enumerate(tags)
        )
        out.append(
            f"""      <section id="news{n}" class="clip scene-wrapper" data-start="{start}" data-duration="{NEWS_DUR}" data-track-index="1">
        <div class="header-bar">
          <span>COBALT GRID / AI NEWS</span>
          <span>{date}</span>
        </div>
        <div class="news-number-large">{n:02d}</div>
        <div class="news-category">{item['category']} / {item['categoryEn']}</div>
        <h2 class="news-title">
          {item['titlePart1']}<span class="highlight">{item['highlight']}</span>{item['titlePart2']}
        </h2>
        <p class="news-summary">
          {item['summary']}
        </p>
        <div class="company-tags">
          {tag_html}
        </div>
        <div class="footer-bar">
          <span>FRAME SYSTEM — AI DAILY NEWS</span>
          <span>{n:02d} / {news_count}</span>
        </div>
      </section>"""
        )
    return "\n\n".join(out)


def build_animations(news, cue_map):
    out = []
    for item in news:
        n = item["number"]
        start = INTRO_DUR + (n - 1) * NEWS_DUR
        c1, c2 = cue_map[n]
        out.append(
            f"""      tl.from("#news{n} .news-number-large", {{ opacity: 0, x: -40, duration: 0.5, ease: "power3.out" }}, {start}.2);
      tl.from("#news{n} .news-category", {{ opacity: 0, y: 20, duration: 0.5, ease: "power3.out" }}, {start}.4);
      tl.from("#news{n} .news-title", {{ opacity: 0, y: 40, duration: 0.7, ease: "power3.out" }}, {start}.6);
      tl.from("#news{n} .news-summary", {{ opacity: 0, y: 30, duration: 0.7, ease: "power3.out" }}, {start + 1.2});
      tl.from("#news{n} .company-tags", {{ opacity: 0, y: 20, duration: 0.5, ease: "power3.out" }}, {start + 1.8});
      tl.to("#captionText", {{ text: {js_str(c1['text'])}, duration: 0.1 }}, {c1['start']});
      tl.to("#captionText", {{ text: {js_str(c2['text'])}, duration: 0.1 }}, {c2['start']});"""
        )
    return "\n".join(out)


def build_html(data, timeline):
    html = TEMPLATE.read_text(encoding="utf-8")
    # 模板需要 GSAP TextPlugin 才能切换字幕文字（已内置；兜底注入）
    if "TextPlugin.min.js" not in html:
        html = html.replace(
            '<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>',
            '<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>\n'
            '    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/TextPlugin.min.js"></script>'
        )
    if "gsap.registerPlugin(TextPlugin)" not in html:
        html = html.replace(
            "const tl = gsap.timeline({ paused: true });",
            "gsap.registerPlugin(TextPlugin);\n      const tl = gsap.timeline({ paused: true });"
        )

    cues = timeline["cues"]
    intro_cues = [c for c in cues if c["part"] == "intro"]
    outro_cues = [c for c in cues if c["part"] == "outro"]
    news_cue_map = {}
    for c in cues:
        if c["part"] == "news":
            news_cue_map.setdefault(c["news_number"], []).append(c)

    news_count = len(data["news"])
    total = INTRO_DUR + news_count * NEWS_DUR + OUTRO_DUR

    # 新闻 sections：替换模板中的 NEWS SECTION TEMPLATE 注释块
    sec_start = html.rfind(
        "      <!-- ============================================= -->",
        0, html.index("      <!-- NEWS SECTION TEMPLATE"))
    sec_end = html.index("      <!-- Outro Scene")
    html = (html[:sec_start]
            + build_sections(data["news"], data["date"], news_count)
            + "\n\n" + html[sec_end:])

    # 新闻动画：替换 NEWS ANIMATION TEMPLATE 注释块
    anim_start = html.rfind(
        "      // ======================================",
        0, html.index("      // NEWS ANIMATION TEMPLATE"))
    anim_end = html.index("      // Outro animation")
    html = (html[:anim_start]
            + build_animations(data["news"], news_cue_map)
            + "\n\n" + html[anim_end:])

    intro = data["intro"]
    replacements = {
        "{{TITLE}}": data["title"],
        "{{DATE}}": data["date"],
        "{{TOTAL_DURATION}}": str(total),
        "{{INTRO_META}}": intro["meta"],
        "{{INTRO_TITLE_LINE1}}": intro["titleLine1"],
        "{{INTRO_TITLE_LINE2}}": intro["titleLine2"],
        "{{INTRO_SUBTITLE}}": intro["subtitle"],
        "{{NEWS_COUNT}}": str(news_count),
        "{{READ_TIME}}": str(round(total / 60)),
        "{{CAPTION_DEFAULT}}": intro_cues[0]["text"],
        "{{CAPTION_INTRO_1}}": intro_cues[0]["text"],
        "{{CAPTION_INTRO_2}}": intro_cues[1]["text"],
        "{{CAPTION_INTRO_3}}": intro_cues[2]["text"],
        "{{CAPTION_INTRO_TIME_1}}": str(intro_cues[0]["start"]),
        "{{CAPTION_INTRO_TIME_2}}": str(intro_cues[1]["start"]),
        "{{CAPTION_INTRO_TIME_3}}": str(intro_cues[2]["start"]),
        "{{OUTRO_START}}": str(INTRO_DUR + news_count * NEWS_DUR),
        "{{TREND_POINT1}}": data["trendPoints"][0],
        "{{TREND_POINT2}}": data["trendPoints"][1],
        "{{TREND_POINT3}}": data["trendPoints"][2],
        "{{CAPTION_OUTRO_1}}": outro_cues[0]["text"],
        "{{CAPTION_OUTRO_2}}": outro_cues[1]["text"],
        "{{CAPTION_OUTRO_TIME_1}}": str(outro_cues[0]["start"]),
        "{{CAPTION_OUTRO_TIME_2}}": str(outro_cues[1]["start"]),
    }
    for k, v in replacements.items():
        html = html.replace(k, v)

    leftovers = re.findall(r"\{\{[^}]+\}\}", html)
    if leftovers:
        raise RuntimeError(f"模板占位符未替换: {sorted(set(leftovers))}")
    return html, total


def build_srt(timeline):
    lines = []
    for c in timeline["cues"]:
        lines.append(
            f"{c['index']}\n{fmt_srt_time(c['start'])} --> "
            f"{fmt_srt_time(c['end'])}\n{c['text']}\n"
        )
    return "\n".join(lines)


def verify_html_times(html, timeline):
    html_times = [
        float(m.group(1))
        for m in re.finditer(
            r'tl\.to\("#captionText"[\s\S]*?\}, (\d+(?:\.\d+)?)\);', html)
    ]
    srt_starts = [round(c["start"], 3) for c in timeline["cues"]]
    if len(html_times) != len(srt_starts):
        raise RuntimeError(
            f"HTML 字幕切换点 {len(html_times)} != SRT 起点 {len(srt_starts)}")
    for h, s in zip(html_times, srt_starts):
        if abs(h - s) > 0.001:
            raise RuntimeError(f"时间不一致: HTML={h} SRT={s}")
    return True


def main():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    timeline = json.loads(TIMELINE.read_text(encoding="utf-8"))
    html, total = build_html(data, timeline)
    HTML_OUT.parent.mkdir(parents=True, exist_ok=True)
    HTML_OUT.write_text(html, encoding="utf-8")
    SRT_OUT.write_text(build_srt(timeline), encoding="utf-8")
    verify_html_times(html, timeline)
    print(f"HTML -> {HTML_OUT}  (总时长 {total}s)")
    print(f"SRT  -> {SRT_OUT}  ({len(timeline['cues'])} 条，时间轴与 HTML 一致)")


if __name__ == "__main__":
    main()
