---
name: "cobalt-grid-ai-news"
description: "端到端生成 Cobalt Grid 风格（奶油纸色+电钴蓝+透视网格）的 AI 行业每日新闻 9:16 竖屏视频：自动搜索当日热点、撰写口播稿、生成 HyperFrames HTML、渲染导出 MP4。Invoke when user asks to create an AI daily news video in Cobalt Grid style, or wants a news/daily briefing/listicle video with the cream+cobalt aesthetic."
---

# Cobalt Grid AI News Video

端到端生成 Cobalt Grid 风格的 AI 行业每日新闻竖屏视频。

## 风格概述

**Cobalt Grid** 是一种双色 risograph 风格设计体系：

- **配色**：奶油纸色 `#f5f0e1`（背景） + 电钴蓝 `#1e3aeb`（唯一墨水色）
- **字体**：Newsreader（衬线大标题） + DM Mono（等宽标签） + 思源黑体/微软雅黑（中文正文）
- **背景**：SVG 透视网格（17条水平线 + 19条会聚线，消失点在画面底部下方），带顶部渐变褪色
- **装饰**：顶部+底部 2px 钴蓝横线、右侧阶梯装饰、进度条
- **输出**：9:16 竖屏 1080×1920，适配小红书/抖音

**适用场景**：每日 AI 资讯、行业周报、产品清单、热点盘点、科技速览

---

## 完整工作流（5 步）

### Step 1：搜索当日 AI 新闻

覆盖以下 5 个维度，按重要性排序输出 10-20 条：

| 维度 | 说明 |
|------|------|
| **产品发布** | 重要产品发布或功能更新（OpenAI、Google、Anthropic、DeepSeek、月之暗面、字节、XAI、智谱、阿里、Minimax、英伟达、腾讯、微软、小米、快手等） |
| **融资并购** | 融资事件、行业并购、估值变化 |
| **技术突破** | 技术突破、重要论文发布、新模型发布 |
| **行业标准** | AI 安全框架、数据治理标准、监管政策进展 |
| **行业动态** | 企业战略、市场趋势、行业会议 |

**每条新闻输出格式**：
- **标题**：一句话新闻标题
- **摘要**：2-3 句完整描述（5W1H）
- **来源**：信息来源（媒体/官方渠道）
- **分类**：从上述 5 个维度选择
- **标签**：2-4 个关键词（公司名、产品名、核心概念）

**附加输出**：2-3 句"今日趋势点评"（从当日新闻中提炼宏观趋势）

---

### Step 2：整理成结构化数据

将搜索结果整理成标准 JSON 格式，参考 [news-data-schema.json](file:///d:/codex-project/temp-folder/.trae/skills/cobalt-grid-ai-news/templates/news-data-schema.json)。

**数据结构速查**：

```
{
  date: "YYYY.MM.DD",
  title: "AI行业热点新闻",
  intro: { meta, titleLine1, titleLine2, subtitle, captions[3] },
  news: [
    {
      number, category, categoryEn,
      titlePart1, highlight, titlePart2,
      summary,
      tags[2-4], accentTagIndex,
      captions[2]
    },
    ...
  ],
  trendPoints[3],
  outroCaptions[2]
}
```

**关键时间计算公式**：
- Intro 时长：8s
- 每条新闻时长：12s
- Outro 时长：12s
- 总时长 = 8 + N × 12 + 12
- 第 i 条新闻开始时间 = 8 + (i-1) × 12
- Outro 开始时间 = 8 + N × 12

---

### Step 3：撰写口播稿

参考 [voiceover-template.md](file:///d:/codex-project/temp-folder/.trae/skills/cobalt-grid-ai-news/templates/voiceover-template.md)。

**结构**：
- **开场（10s）**：问候 + 日期 + 新闻数量 + 时长预告
- **每条新闻（~10s）**：导语 + 核心信息 + 一句话点评
- **结尾（10s）**：三条趋势 + 关注引导

**字幕原则**：
- 每条字幕 15-25 个字，不超过 2 行
- 口语化，像聊天一样
- 每条新闻 2 条字幕，分别在 0.5s 和 6.0s 出现
- 公司名、产品名、数字等关键信息必须出现在字幕中

---

### Step 4：生成 HyperFrames HTML

使用 [composition-template.html](file:///d:/codex-project/temp-folder/.trae/skills/cobalt-grid-ai-news/templates/composition-template.html) 作为骨架。

#### 4.1 项目初始化

在当前目录创建 HyperFrames 项目结构：

```bash
mkdir -p my-news-video/compositions
mkdir -p my-news-video/renders
```

将模板复制到 `compositions/index-cobalt.html`。

#### 4.2 填充 Intro 部分

替换模板中的 `{{...}}` 占位符：
- `{{DATE}}` → 日期
- `{{INTRO_META}}` → Intro 标签
- `{{INTRO_TITLE_LINE1}}` / `{{INTRO_TITLE_LINE2}}` → 主标题两行
- `{{INTRO_SUBTITLE}}` → 副标题
- `{{NEWS_COUNT}}` → 新闻总数
- `{{READ_TIME}}` → 阅读时长（总时长/60）
- Intro 字幕 3 条

#### 4.3 生成每条新闻 section

**对每条新闻，复制模板中的 NEWS SECTION TEMPLATE 注释块**，然后替换：

| 占位符 | 说明 |
|--------|------|
| `{{NUMBER}}` | 新闻序号 1-N |
| `{{NUMBER_PADDED}}` | 两位数序号 01, 02... |
| `{{START}}` | 开始时间（8 + (i-1)*12） |
| `{{CATEGORY}}` / `{{CATEGORY_EN}}` | 分类中/英文 |
| `{{TITLE_PART1}}` | 标题前半部分 |
| `{{HIGHLIGHT}}` | 高亮关键词（斜体+下划线） |
| `{{TITLE_PART2}}` | 标题后半部分（可空） |
| `{{SUMMARY}}` | 新闻正文 |
| `{{TAG1}}` / `{{TAG2}}` / `{{TAG3}}` | 标签（第一个自动高亮） |
| `{{NEWS_COUNT}}` | 新闻总数 |

**同时在 GSAP 时间线中添加对应动画**，复制 NEWS ANIMATION TEMPLATE 注释块，替换相同的占位符。

#### 4.4 填充 Outro 部分

- `{{OUTRO_START}}` → Outro 开始时间（8 + N*12）
- `{{TREND_POINT1}}` / `{{TREND_POINT2}}` / `{{TREND_POINT3}}` → 三条趋势
- Outro 字幕 2 条

#### 4.5 更新总时长

- `{{TOTAL_DURATION}}` → 8 + N*12 + 12

---

### Step 5：渲染导出视频

#### 5.1 环境检查

```bash
cd my-news-video
npx hyperframes doctor
```

#### 5.2 Windows 环境配置（必要时）

**浏览器路径**（Chrome 被策略禁用时）：
```powershell
$env:HYPERFRAMES_BROWSER_PATH = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
```

#### 5.3 渲染命令

```bash
npx hyperframes render `
  -c compositions/index-cobalt.html `
  -o renders/ai-news-YYYYMMDD-cobalt.mp4 `
  --quality high `
  --resolution portrait `
  --no-browser-gpu `
  -w 2 `
  --debug
```

**参数说明**：
- `--no-browser-gpu`：禁用 GPU 渲染，避免 Chrome GPU 错误
- `-w 2`：双工作进程，触发磁盘编码模式
- `--debug`：保留工作目录，防止帧文件丢失

#### 5.4 故障排查

**问题 1：浏览器启动失败 / GPU 错误**
→ 使用 Edge 浏览器（见 5.2）+ `--no-browser-gpu`

**问题 2：帧文件丢失 / "No such file or directory"**
→ 使用 `--debug` 模式；若仍失败，手动从 debug 目录编码：
```powershell
# 找到 debug 目录中的帧文件，复制到项目内
Copy-Item "$debugDir\captured-frames\*.jpg" "frames\"
# 使用系统 FFmpeg 编码
& "C:\path\to\ffmpeg.exe" -y -framerate 30 -i "frames\frame_%06d.jpg" `
  -c:v libx264 -pix_fmt yuv420p -crf 18 -preset medium "renders\output.mp4"
```

**问题 3：FFmpeg 编码失败**
→ 使用系统安装的完整 FFmpeg（而非 HyperFrames 内置的精简版）

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

## 完整示例：从 3 条新闻到最终代码

假设我们有 3 条新闻，总时长 = 8 + 3×12 + 12 = 56 秒。

### 数据准备

```json
{
  "date": "2026.07.25",
  "title": "AI行业热点新闻",
  "intro": {
    "meta": "AI INDUSTRY DAILY BRIEFING",
    "titleLine1": "AI Industry",
    "titleLine2": "Daily News",
    "subtitle": "2026 年 7 月 25 日 · 3 条精选 · 1 分钟掌握行业脉搏",
    "captions": ["大家好，欢迎来到今天的AI热点速递", "3条精选，1分钟带你看完", "开始吧"]
  },
  "news": [
    {
      "number": 1,
      "category": "产品发布", "categoryEn": "PRODUCT",
      "titlePart1": "Anthropic 发布 ", "highlight": "Claude Opus 5", "titlePart2": "",
      "summary": "Anthropic 发布 Claude Opus 5，性能接近 Fable 5，价格约为一半。",
      "tags": ["ANTHROPIC", "OPUS 5", "价格减半"],
      "accentTagIndex": 0,
      "captions": ["Anthropic发布Claude Opus 5，价格减半", "高端模型性价比时代到来"]
    }
  ],
  "trendPoints": ["趋势1...", "趋势2...", "趋势3..."],
  "outroCaptions": ["今日三大趋势", "关注我，每天掌握AI动态"]
}
```

### HTML 组装要点

1. Intro 部分：替换所有 `{{...}}` 占位符
2. 对每条新闻：
   - 复制 NEWS SECTION 模板 → 替换占位符 → 插入到 Intro 和 Outro 之间
   - 复制 GSAP 动画模板 → 替换占位符 → 插入到 Intro 动画和 Outro 动画之间
3. Outro 部分：替换占位符
4. 更新 `{{TOTAL_DURATION}}`

### 渲染

```bash
cd my-news-video
npx hyperframes render -c compositions/index-cobalt.html -o renders/demo.mp4 --quality high --resolution portrait --no-browser-gpu -w 2 --debug
```

---

## 模板文件索引

| 文件 | 用途 |
|------|------|
| [composition-template.html](file:///d:/codex-project/temp-folder/.trae/skills/cobalt-grid-ai-news/templates/composition-template.html) | 视频 HTML 骨架（CSS + 结构 + GSAP 时间线） |
| [news-data-schema.json](file:///d:/codex-project/temp-folder/.trae/skills/cobalt-grid-ai-news/templates/news-data-schema.json) | 新闻数据格式规范（含示例和字段说明） |
| [voiceover-template.md](file:///d:/codex-project/temp-folder/.trae/skills/cobalt-grid-ai-news/templates/voiceover-template.md) | 口播稿写作模板（公式+示例+分类标签对照） |
