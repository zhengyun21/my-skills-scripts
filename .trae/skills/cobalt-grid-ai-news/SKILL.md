---
name: "cobalt-grid-ai-news"
description: "端到端生成 Cobalt Grid 风格（奶油纸色+电钴蓝+透视网格）的 AI 行业每日新闻 9:16 竖屏视频：自动搜索当日热点、撰写口播稿、生成音频驱动时间轴（SRT+MP3）、组装 HyperFrames HTML、渲染导出 MP4，所有产物按当天日期归档。Invoke when user asks to create an AI daily news video in Cobalt Grid style, or wants a news/daily briefing/listicle video with the cream+cobalt aesthetic."
---

# Cobalt Grid AI News Video

端到端生成 Cobalt Grid 风格的 AI 行业每日新闻竖屏视频，并配套输出与视频完全同步的口播 MP3 和 SRT 字幕。

## 环境要求

- Node.js ≥ 18（含 npx，联网安装 HyperFrames）
- Python 3 + `edge-tts`（`pip install edge-tts`）
- FFmpeg（含 ffprobe，完整版）
- Chrome 或 Edge 浏览器

## 输出目录（所有产物按当天日期归档）

在工作区根目录创建 `YYYY-MM-DD/`（如 `2026-08-02/`），全部产物放进去：

```text
YYYY-MM-DD/
├─ news-data.json              # 新闻结构化数据（唯一内容源，手写）
├─ voiceover.md                # 口播稿
├─ sources.md                  # 新闻来源清单
├─ timeline.json               # 音频驱动时间轴（脚本生成，勿手改）
├─ subtitles.srt               # 字幕，与视频字幕/口播音频完全同步
├─ voiceover.mp3               # 口播音频（edge-tts + ffmpeg 拼接）
├─ tmp_tts/                    # 中间音频（脚本生成）
└─ my-news-video/
   ├─ compositions/index-cobalt.html
   └─ renders/ai-news-YYYYMMDD-cobalt.mp4
        └─ ai-news-YYYYMMDD-cobalt-narrated.mp4   # 合流口播版
```

---

## 完整工作流（5 步）

### Step 1：搜索当日 AI 新闻

覆盖以下 5 个维度，按重要性排序输出 10-20 条：

| 维度 | 说明 |
|------|------|
| **产品发布** | OpenAI、Google、Anthropic、DeepSeek、月之暗面、字节、xAI、智谱、阿里、MiniMax、英伟达、腾讯、微软、小米、快手等 |
| **融资并购** | 融资事件、行业并购、估值变化 |
| **技术突破** | 技术突破、重要论文发布、新模型发布 |
| **行业标准** | AI 安全框架、数据治理标准、监管政策进展 |
| **行业动态** | 企业战略、市场趋势、行业会议 |

**每条新闻输出格式**：一句话标题、2-3 句摘要（5W1H）、来源、分类、2-4 个标签。
**附加输出**：2-3 句"今日趋势点评"。

### Step 2：整理结构化数据

将搜索结果整理成 `news-data.json`，格式参考 [templates/news-data-schema.json](templates/news-data-schema.json)。

**关键时间公式（已定稿，勿改）**：
- Intro 时长：8s
- 每条新闻时长：**11s**
- Outro 时长：12s
- 总时长 = 8 + N × 11 + 12
- 第 i 条新闻开始时间 = 8 + (i-1) × 11
- Outro 开始时间 = 8 + N × 11
- `intro.subtitle` 中的"X 分 XX 秒" = round(总时长/60)，如 15 条 → "3 分 05 秒"

**开场字幕固定文案**（按日期/条数替换）：
1. 欢迎来到AI速递
2. 今天是{月}月{日}日，{N}条新闻
3. 下面给大家详细介绍

**结尾字幕固定文案**：
1. 今日三大趋势：{趋势1}、{趋势2}、{趋势3}
2. 今天资讯播放完了，下期见

### Step 3：撰写口播稿

写作规范参考 [templates/voiceover-template.md](templates/voiceover-template.md)，核心规则：

- **语速**：中等偏快（约 5 字/秒）。合成统一用 edge-tts `zh-CN-YunyangNeural --rate=+12%`，不做逐句变速
- **结构**：开场 3 条 + 每条新闻 2 条 + 结尾 2 条字幕
- **每条字幕**：15-25 个字、不超过 2 行、口语化；公司名/产品名/数字必须出现
- **字数估算**：加权字数 W = 汉字数 + 0.5×字母数字数；单条目标时长 ≈ W/4.5 秒（最终以实测 TTS 时长为准）
- 口播稿写入 `voiceover.md`，字幕文案写入 `news-data.json` 的 `captions` 字段

### Step 4：脚本生成时间轴、SRT、MP3 与 HTML

#### 4.1 生成口播音频与时间轴

```powershell
python <skill>/scripts/make_voiceover_mp3.py <YYYY-MM-DD目录>
```

脚本会：逐条用 edge-tts 合成 35 条字幕语音 → ffprobe 实测时长 → 按规则排布 `timeline.json` → 拼接 `voiceover.mp3`。

#### 4.2 生成 SRT 与 HyperFrames HTML

```powershell
python <skill>/scripts/build_cobalt.py <YYYY-MM-DD目录>
```

脚本读取 `timeline.json`（唯一时间源），生成 `subtitles.srt` 与 `my-news-video/compositions/index-cobalt.html`，并断言两者字幕切换时间逐条一致。

#### 4.3 时间轴规则（音频驱动，决策完整）

| 场景 | 规则 |
|------|------|
| 开场（8s） | 3 条字幕连续排布，句间间隔 0.25s，首条起点 0.35s，最后一条显示到 8.0s；整段均匀提速至 ≤1.18x 以恰好占满 |
| 每条新闻（11s） | 字幕1 = 段首+0.35s，展示时长=实测语音；字幕2 = 字幕1语音结束+0.28s；绝不越段 |
| 结尾（12s） | 同新闻规则 |
| 段尾 | 字幕最多停留到段尾前 1.5s |
| 变速 | 仅段内均匀，上限 1.18x；正常情况下 news/outro 均为 1.0x |

### Step 5：渲染导出 MP4 与验证

#### 5.1 环境检查

```powershell
cd <YYYY-MM-DD目录>/my-news-video
npx hyperframes doctor
```

#### 5.2 Windows 浏览器配置（必要时）

```powershell
$env:HYPERFRAMES_BROWSER_PATH = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
```

#### 5.3 渲染命令

```powershell
npx hyperframes render `
  -c compositions/index-cobalt.html `
  -o renders/ai-news-YYYYMMDD-cobalt.mp4 `
  --quality high `
  --resolution portrait `
  --no-browser-gpu `
  -w 2 `
  --debug
```

#### 5.4 合流口播（生成 narrated 版）

```powershell
ffmpeg -y -i renders/ai-news-YYYYMMDD-cobalt.mp4 -i <YYYY-MM-DD目录>/voiceover.mp3 `
  -c:v copy -c:a aac -b:a 192k -shortest renders/ai-news-YYYYMMDD-cobalt-narrated.mp4
```

#### 5.5 验证

```powershell
python <skill>/scripts/verify_cobalt.py <YYYY-MM-DD目录>
ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 renders/ai-news-YYYYMMDD-cobalt.mp4
```

verify_cobalt.py 断言：SRT/HTML 时间一致、字幕不越段、段内句间间隔 0.25-0.3s、变速 ≤1.18x、MP3 时长 = 视频时长。

可选冒烟测试：复制 `index-cobalt.html` 为 `index-smoke.html` 并把 `data-duration` 改为 6，用 `--quality draft` 渲染一遍，确认浏览器捕获与字幕切换正常后再全量渲染。

---

## 设计规范速查

### 颜色

| 用途 | 色值 |
|------|------|
| 背景纸色 | `#f5f0e1` |
| 墨水色（钴蓝） | `#1e3aeb` |
| 墨水软色（副标题） | `rgba(30, 58, 235, 0.65)` |
| 摘要文字 | `rgba(30, 58, 235, 0.9)` |
| 网格线 | `rgba(30, 58, 235, 0.38)` |

### 字号

| 元素 | 字号 | 字体 |
|------|------|------|
| Intro 主标题 | 160px | Newsreader 700 |
| 新闻编号 | 180px | Newsreader 700 |
| 新闻标题 | 84px | Newsreader 700 |
| Outro 标题 | 100px | Newsreader 700 |
| 新闻摘要 | 50px | 思源黑体 600 italic |
| Intro 副标题 | 44px | 思源黑体 400 |
| Outro 趋势文字 | 40px | 思源黑体 400 |
| 分类标签 | 28px | DM Mono 500 |
| 页眉/页脚 | 22-24px | DM Mono |
| 字幕 | 38px | 思源黑体 500 |
| 标签 Tag | 26px | DM Mono 500 |

### 间距

| 元素 | 值 |
|------|-----|
| 内容区 padding | 160px 80px 360px |
| 标题上边距 | 48px |
| 摘要上边距 | 48px |
| 标签下边距 | 20px |
| 分类标签下边距 | 8px + 2px 下划线 |
| 页眉上边距 | 60px |
| 页脚下边距 | 60px |

---

## 故障排查

- **浏览器启动失败 / GPU 错误**：设置 Edge 路径（见 5.2）+ `--no-browser-gpu`
- **字幕不切换**：确认构图已注册 GSAP TextPlugin（模板已内置，勿删）
- **帧文件丢失 / "No such file or directory"**：保留 `--debug`；若仍失败，从 debug 目录用系统 FFmpeg 手动编码
- **FFmpeg 编码失败**：使用系统安装的完整 FFmpeg（而非 HyperFrames 内置精简版）
- **edge-tts 网络失败**：重试脚本（脚本会重新合成全部字幕）

---

## 模板与脚本索引

| 文件 | 用途 |
|------|------|
| [templates/composition-template.html](templates/composition-template.html) | 视频 HTML 骨架（CSS+结构+GSAP 时间线，含 TextPlugin） |
| [templates/news-data-schema.json](templates/news-data-schema.json) | 新闻数据格式规范（含时间公式、音频规则、产物清单） |
| [templates/voiceover-template.md](templates/voiceover-template.md) | 口播稿写作模板（公式+示例+语速节奏规则+分类标签对照） |
| [scripts/make_voiceover_mp3.py](scripts/make_voiceover_mp3.py) | 合成口播、实测时长、生成 timeline.json 与 voiceover.mp3 |
| [scripts/build_cobalt.py](scripts/build_cobalt.py) | 由 news-data.json + timeline.json 生成 SRT 与 HTML |
| [scripts/verify_cobalt.py](scripts/verify_cobalt.py) | 产物一致性校验（时间轴/越段/间隔/变速/时长） |
