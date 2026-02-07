# 📺 IPTVFlow — 智能 IPTV 直播源清洗与验证系统

[![daily_IPTV_FlowList_refresh.yml](https://github.com/fqw000/IPTVFlow/actions/workflows/daily_IPTV_FlowList_refresh.yml.yml/badge.svg)](https://github.com/fqw000/IPTVFlow/actions)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue)
![License](https://img.shields.io/github/license/your-username/IPTVFlow)

> **自动聚合 + 智能去重 + 两阶段测速 + FFmpeg 流验证 + OCR 软错误检测 + 分组输出**

本项目可从多个远程/本地源自动拉取 IPTV 直播列表，经过**深度清洗、主机级测速、频道级回检**，输出高质量、分组清晰、带台标的 M3U 播放列表，并生成详细 Markdown 报告。支持每日自动更新！
> 💡 **无需手动维护**｜**每日自动刷新**｜**支持推送通知**｜**开源**

---

## ✨ 核心特性

### 🌐 **双模源加载**
- 支持远程 URL 列表（`config/remote_sources.txt`）
- 支持本地 `sources/` 目录下的 `.m3u` / `.txt` 文件

### 🔍 **智能清洗**
- 自动识别 M3U/TXT 格式并标准化
- 频道名称清洗
- 自定义分组

### ⚡ **两阶段深度验证**
1. **初筛**：使用代表性 `.m3u8` 快速探测
2. **回查**：对失败主机尝试最多 3 个备选流
- 输出完整测速排名（含延迟、流类型、URL）

### 🛡️ **深度流验证**
| 技术 | 作用 | 触发条件 |
|------|------|--------|
| **FFmpeg (`ffprobe`)** | 验证非 `.m3u8` 链接是否含有效音视频流 | 系统安装 FFmpeg |
| **Tesseract OCR** | 截图识别“登录墙”、“区域限制”等软错误画面 | 安装 Tesseract + Python 依赖 |

> 💡 即使未安装，脚本仍可运行（仅跳过深度验证）

### 🧹 **黑名单过滤**
- 支持 `config/blackHost_list.txt` 按 `host:port` 屏蔽劣质源

### 📊 **结构化输出**
- **`output/live.m3u`**：按预设顺序分组（央视 → 卫视 → 影视 → 体育...）
- **`output/report.md`**：含源加载详情、分组统计、可用/无源频道清单
- 自动推送 **Bark 通知**（需配置密钥）

---

## 🛠️ 依赖要求

### 基础依赖（必须）
- Python ≥ 3.7
- `requests`

### 可选依赖（推荐安装以启用 v1.1 增强功能）
| 功能 | 系统包 | Python 包 |
|------|--------|----------|
| **FFmpeg 流验证** | `ffmpeg` | 无需额外 Python 包 |
| **OCR 软错误检测** | `tesseract-ocr`<br>`libtesseract-dev` | `pytesseract`<br>`Pillow` |

---

## ▶️ 本地运行指南

### 1. 克隆仓库
```bash
git clone https://github.com/your-username/iptv-flow.git
cd iptv-flow
```
### 2. 安装依赖
```bash
# 基础
pip install requests
```

### **（可选）启用 FFmpeg + OCR**
```bash
sudo apt-get install -y ffmpeg tesseract-ocr libtesseract-dev  # Ubuntu/Debian
pip install pytesseract Pillow
```
### 3. 配置源文件
- **远程源列表**：编辑 config/remote_sources.txt（每行一个 M3U/TXT URL）
- **黑名单**：编辑 config/blackHost_list.txt（每行 host:port，如 example.com:8080）
- **Bark 推送**：在 Config.BARK_DEVICE_KEY 配置或环境变量 BARK_DEVICE_KEY 中填入密钥
### 4. 运行脚本
```bash
python iptvflow.py
```
### 5. 调试模式（保留中间文件）
```bash
DEBUG=1 python iptvflow.py
```
调试文件将生成为 debug_*.m3u、debug_*.json、debug_*.md，便于排查问题。

---

## 🤖 GitHub Actions 自动化
项目已集成 每日自动更新（北京时间 09:15）：

✅ 自动拉取最新源

✅ 执行深度清洗与验证

✅ 提交 live.m3u 和 report.md 到仓库

注意：确保 iptvflow.py 文件名与 Workflow 中一致！

---
## 📂 目录结构
```text
.
├── iptvflow.py                 # 主程序
├── config/
│   ├── remote_sources.txt      # 远程源列表
│   └── blackHost_list.txt      # 主机黑名单
├── local_playlists/                       # 本地源目录（可选）
│   ├── local1.txt
│   └── local2.m3u
├── output/
│   ├── live.m3u                # 最终播放列表（自动生成）
│   └── report.md               # 清洗报告（自动生成）
└── .github/workflows/
    └── daily-update.yml        # 自动化工作流
```

---
## 📣 分组规则说明

频道按以下优先级匹配分组（正则匹配）：

1. 🧸 儿童动画：含“少儿”、“迪士尼”、“CCTV14”等
2. 🇨🇳 央视频道：含“CCTV”、“CGTN”、“风云”等
3. 📺 卫视频道：含“卫视”、“湖南”、“浙江”等
4. 🇭🇰 港澳台频道：含“TVB”、“凤凰”、“东森”等
5. ⚽ 体育频道：含“CCTV5”、“NBA”、“足球”等
6. 🎬 影视频道：含“电影”、“HBO”、“CHC”等
7. 🌍 国际频道：
- 显式关键词（BBC、CNN、NHK...）
- 兜底规则：全英文且不含 cctv/cgtn
8. 🎓 教育频道：含“教育”、“学堂”、“国学”等
9. 📺 其他频道：未匹配项
> 分组顺序可在 Config.GROUP_OUTPUT_ORDER 中调整

---

## ❓ 常见问题

### Q：为什么我的频道被归类到“其他频道”？
A：检查 normalize_channel_name() 是否正确清洗了名称，并确认 GROUP_RULES 覆盖了关键词。
### Q：如何添加新分组？
A：在 Config.GROUP_RULES 开头添加新规则（顺序敏感！），并在 GROUP_OUTPUT_ORDER 中指定排序。
### Q：OCR 检测太慢，能关闭吗？
A：卸载 Tesseract 或不安装 pytesseract，脚本将自动跳过 OCR 步骤。

---

## x🙏 致谢
- **频道 Logo 来源**：[alantang1977/iptv_api](https://github.com/alantang1977/iptv_api?spm=5176.28103460.0.0.14e67551Qg8I0u)
- **EPG 与 TVG 数据支持**：[live.fanmingming.com](https://live.fanmingming.com/?spm=5176.28103460.0.0.14e67551Qg8I0u)
- **参考项目**：[iptv-org/iptv](https://github.com/iptv-org/iptv?spm=5176.28103460.0.0.14e67551Qg8I0u)

- **访问统计**：![Visitor Count](https://visitor-badge.laobi.icu/badge?page_id=yfqw000.IPTVFlow)

### 感谢所有公开 IPTV 源的贡献者

---
#### ⚠️ 法律声明：本项目仅提供技术工具，用于学习与研究目的。请确保你使用的直播源符合当地法律法规，尊重版权与知识产权。作者不对任何非法使用行为负责。

> ## Enjoy your clean IPTV experience! 📺✨
>

