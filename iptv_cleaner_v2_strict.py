#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 直播源轻量清洗与验证系统 v3.0

设计目标：
- 只依赖 aiohttp，去掉 OCR / ffprobe / ffmpeg 等重量级外部进程
- 漏斗式验证：入口过滤 -> playlist 探测 -> 分片头校验 -> 采样测速 -> 画质提取
- 每个频道保留 top N 条不同 host 的源，供播放器 failover
- 历史缓存 + 冻结/解冻，只复测上轮可用源
- 保留上游项目的业务资产：别名映射、分组规则、频道顺序、Bark 报告

用法:
    pip install aiohttp
    python3 iptv_cleaner.py                 # 正常运行
    python3 iptv_cleaner.py --full          # 强制全量重测（忽略缓存）
    python3 iptv_cleaner.py --limit 20      # 只处理前 20 条（调试）
    python3 iptv_cleaner.py --dry-run       # 只解析不测速
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, quote

try:
    import aiohttp
    from aiohttp import ClientSession, ClientTimeout, TCPConnector
except ImportError:
    print("缺少依赖，请运行: pip install aiohttp")
    sys.exit(1)


# ============================================================
# 配置
# ============================================================

class Config:
    """所有参数可用 IPTV_ 前缀的环境变量覆盖"""

    BASE_URL_FILE = os.getenv("IPTV_BASE_URL_FILE", "config/remote_sources.txt")
    BLACKLIST_FILE = os.getenv("IPTV_BLACKLIST_FILE", "config/blackHost_list.txt")
    CHANNEL_ORDER_FILE = os.getenv("IPTV_CHANNEL_ORDER_FILE", "config/channel_order.json")
    OUTPUT_FILE = os.getenv("IPTV_OUTPUT_FILE", "output/live.m3u")
    # 备份源单独输出：主列表每频道只留 1 条，避免遥控器换台要点多次
    BACKUP_FILE = os.getenv("IPTV_BACKUP_FILE", "output/live_backup.m3u")
    WRITE_BACKUP = os.getenv("IPTV_WRITE_BACKUP", "1") == "1"
    REPORT_FILE = os.getenv("IPTV_REPORT_FILE", "output/report.md")
    HISTORY_FILE = os.getenv("IPTV_HISTORY_FILE", "output/history.json")
    LOCAL_DIR = os.getenv("IPTV_LOCAL_DIR", "local_playlists")
    BARK_DEVICE_KEY = os.getenv("BARK_DEVICE_KEY", "")

    MAX_CONCURRENT = int(os.getenv("IPTV_MAX_CONCURRENT", "80"))
    SOURCE_FETCH_CONCURRENT = int(os.getenv("IPTV_SOURCE_CONCURRENT", "10"))
    CONNECT_TIMEOUT = int(os.getenv("IPTV_CONNECT_TIMEOUT", "3"))
    READ_TIMEOUT = int(os.getenv("IPTV_READ_TIMEOUT", "5"))
    TOTAL_TIMEOUT = int(os.getenv("IPTV_TOTAL_TIMEOUT", "8"))
    PLAYLIST_TIMEOUT = int(os.getenv("IPTV_PLAYLIST_TIMEOUT", "6"))
    SEG_TIMEOUT = int(os.getenv("IPTV_SEG_TIMEOUT", "6"))

    SEGMENT_SAMPLE = int(os.getenv("IPTV_SEGMENT_SAMPLE", "3"))
    MAX_BYTES_PER_SEGMENT = int(os.getenv("IPTV_MAX_SEG_BYTES", str(512 * 1024)))
    MIN_SPEED_MBPS = float(os.getenv("IPTV_MIN_SPEED", "0.15"))
    MAX_TTFB_MS = int(os.getenv("IPTV_MAX_TTFB", "3000"))

    # --- 质量优先评分（质量分 + 速度分 + 延迟分 加权）---
    MIN_QUALITY_SCORE = float(os.getenv("IPTV_MIN_QUALITY", "20"))
    WEIGHT_QUALITY = float(os.getenv("IPTV_W_QUALITY", "0.6"))
    WEIGHT_SPEED = float(os.getenv("IPTV_W_SPEED", "0.3"))
    WEIGHT_LATENCY = float(os.getenv("IPTV_W_LATENCY", "0.1"))
    # 声明分辨率与实际码率差距过大时的额外惩罚分
    MISMATCH_PENALTY = float(os.getenv("IPTV_MISMATCH_PENALTY", "10"))

    KEEP_PER_CHANNEL = int(os.getenv("IPTV_KEEP_PER_CHANNEL", "3"))
    FREEZE_AFTER_FAILS = int(os.getenv("IPTV_FREEZE_FAILS", "3"))
    THAW_AFTER_RUNS = int(os.getenv("IPTV_THAW_RUNS", "5"))
    SKIP_VOD = os.getenv("IPTV_SKIP_VOD", "1") == "1"
    ALLOW_SAME_HOST_BACKUP = os.getenv("IPTV_ALLOW_SAME_HOST", "0") == "1"

    # --- group-title 优化 ---
    # 内容细分：把 CCTV-5/5+ 归体育、CCTV-14 归儿童（口径更准）
    GROUP_FINE_GRAINED = os.getenv("IPTV_GROUP_FINE", "1") == "1"
    # 画质后缀：group-title 追加频道最优源的画质档位，如 "央视频道·1080P"
    GROUP_SHOW_QUALITY = os.getenv("IPTV_GROUP_QUALITY", "1") == "1"

    USER_AGENT = "Mozilla/5.0 (compatible; IPTVCleaner/3.0)"
    HLS_EXTENSIONS = (".m3u8", ".m3u")
    LOGO_BASE_URL = "https://live.fanmingming.com/tv/"

    GROUP_OUTPUT_ORDER = [
        "🇨🇳 央视频道", "📺 卫视频道", "🎬 影视频道", "⚽ 体育频道",
        "🧸 儿童动画", "🌍 国际频道", "🎓 教育频道",
        "🇭🇰 港澳台频道", "📺 其他频道",
    ]


# ============================================================
# 业务资产：别名 / 分组（保留并修复顺序问题）
# ============================================================

# CCTV 编号 -> 中文名。编号修正与规范名生成只走这一张表。
CCTV_NAMES: Dict[int, str] = {
    1: "综合", 2: "财经", 3: "综艺", 4: "中文国际", 5: "体育",
    6: "电影", 7: "国防军事", 8: "电视剧", 9: "纪录", 10: "科教",
    11: "戏曲", 12: "社会与法", 13: "新闻", 14: "少儿", 15: "音乐",
    16: "奥林匹克", 17: "农业农村",
}

# 非 CCTV 的错别字 / 简写 -> 标准名
ALIAS_MAP: Dict[str, str] = {
    "安微卫视": "安徽卫视",
    "内蒙卫视": "内蒙古卫视",
    "凤凰中文": "凤凰卫视中文台",
    "凤凰中文台": "凤凰卫视中文台",
    "凤凰卫视中文": "凤凰卫视中文台",
    "凤凰资讯": "凤凰卫视资讯台",
    "凤凰咨询": "凤凰卫视资讯台",
    "凤凰卫视资讯": "凤凰卫视资讯台",
    "BTV北京卫视": "北京卫视",
    "上海东方卫视": "东方卫视",
    "SCTV康巴卫视": "康巴卫视",
    "TVBJade": "TVB翡翠台",
    "TVBPearl": "TVB明珠台",
}

# 分组规则：地区词优先于通用词，避免“香港卫视/凤凰卫视”被误判为卫视
GROUP_RULES: List[Tuple[str, str]] = [
    # 儿童动画：放在最前，避免“少儿/卡通”被“卫视/央视”等通用词抢先匹配
    # 中英文关键词尽量齐全；英文统一由 re.IGNORECASE 兜底
    (
        r"("
        r"哈哈炫动|炫动卡通|卡酷|嘉佳|优漫|金鹰卡通|动漫秀场|"
        r"少儿|儿童|幼教|幼儿|亲子|育儿|宝宝|宝贝|童话|童年|青少|"
        r"贝瓦|巧虎|新科动漫|小猪佩奇|汪汪队|海底小纵队|米老鼠|迪士尼|"
        r"熊出没|猫和老鼠|哆啦A梦|喜羊羊|大头儿子|葫芦娃|黑猫警长|"
        r"舒克贝塔|奥特曼|巴啦啦|铠甲勇士|"
        r"动画|动漫|卡通|"
        r"CCTV-?14|"
        r"cartoon|disney|nickelodeon|nick\s*jr|kids|kika|cbbc|cbeebies|"
        r"babytv|baby\s*tv|pbs\s*kids|boomerang|tooncast"
        r")",
        "🧸 儿童动画",
    ),
    (r"(央视|CCTV|CGTN|风云音乐|第一剧场|怀旧剧场|女性时尚|风云足球|世界地理|兵器科技|电视指南)", "🇨🇳 央视频道"),
    (r"(翡翠|明珠|凤凰|鳳凰|莲花|TVB|华语|中天|东森|年代|民视|三立|台视|美亚|美亞|千禧|无线|無線|VIUTV|HOY|RTHK|靖天|星卫|香港|澳门|澳门|台湾|港澳)", "🇭🇰 港澳台频道"),
    (r"(卫视|湖南|浙江|江苏|北京|广东|深圳|东方|安徽|山东|河南|湖北|四川|辽宁|东南|天津|内蒙古|云南)", "📺 卫视频道"),
    (r"(体育|高尔夫|足球|NBA|英超|西甲|欧冠)", "⚽ 体育频道"),
    (r"(电影|影院|CHC|HBO|AXN|TCM|佳片)", "🎬 影视频道"),
    (r"(教育|课堂|空中|大学|学习|国学|书画|考试|中学|学堂)", "🎓 教育频道"),
    (r"(AMC|BET|Discovery|CBS|cine|CNN|epix|espn|fox|american|boomerang|cnbc|entertainment|fuse|fx|国家地理|Animal Planet|BBC|NHK|DW|France24|Al Jazeera)", "🌍 国际频道"),
    (r"^[A-Za-z0-9\s\-\+\&\.\'\!\(\)]+$", "🌍 国际频道"),
]

# 后缀清理：循环剥除直到稳定
QUALITY_SUFFIXES = ("高清", "超高清", "蓝光", "标清", "4K", "8K", "FHD", "UHD", "HD", "SD")
REDUNDANT_SUFFIXES = ("频道", "直播", "官方", "正版", "流畅", "备用", "测试", "咪咕", "极速", "真")


# ============================================================
# 基础工具
# ============================================================

def beijing_time_str() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


def setup_logger(debug: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def normalize_channel_name(name: str) -> str:
    """
    把任意频道名映射到唯一规范名。规范格式：
    - CCTV:  "CCTV-1 综合" / "CCTV-5+ 体育赛事" / "CCTV-4K 超高清"
    - 卫视:  "湖南卫视"
    - 其他:  清理后的原名
    规则顺序固定，输出可重复；不使用子串包含匹配。
    """
    if not name or not isinstance(name, str):
        return "Unknown"

    s = name.strip()
    if not s:
        return "Unknown"

    # 1. 去括号内容
    s = re.sub(r"[\(（【\[][^)）】\]]*[\)）】\]]", "", s)

    # 2. 统一分隔符为空格（连字符在此统一处理，不做粗暴截取）
    s = re.sub(r"[\s\-\u00b7\u2022_\|]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    # 3. 中文之间的空格合并（凤凰卫视 中文台 -> 凤凰卫视中文台）
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", s)

    # 4. 前缀大小写归一
    s = re.sub(r"(?i)^cctv\b", "CCTV", s)
    s = re.sub(r"(?i)^cgtn\b", "CGTN", s)

    # 5. CCTV 系列
    m = re.match(r"^CCTV\s*(\d+)\s*([Kk]|\+)?\s*(.*)$", s)
    if m:
        num = int(m.group(1))
        marker = (m.group(2) or "").upper()
        rest = m.group(3).strip()

        if num == 4 and ("美洲" in rest or "北美" in rest):
            return "CCTV-4 美洲"
        if num == 4 and "欧洲" in rest:
            return "CCTV-4 欧洲"
        if num == 5 and marker == "+":
            return "CCTV-5+ 体育赛事"
        if num == 4 and marker == "K":
            return "CCTV-4K 超高清"

        cn = CCTV_NAMES.get(num)
        if cn:
            return f"CCTV-{num} {cn}"

    if re.match(r"^CCTV\s*4\s*K\b", s, re.I) or s.upper().startswith("CCTV4K"):
        return "CCTV-4K 超高清"

    # 6. CGTN
    if s.upper().startswith("CGTN"):
        return re.sub(r"(?i)^cgtn\s*", "CGTN ", s).strip()

    # 7. 非 CCTV：循环剥除质量后缀与冗余后缀（直到稳定）
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"(?:%s)$" % "|".join(QUALITY_SUFFIXES), "", s, flags=re.I).strip()
        s = re.sub(r"(?:%s)$" % "|".join(REDUNDANT_SUFFIXES), "", s).strip()
        s = re.sub(r"\s+", " ", s).strip()

    # 8. 别名精确映射
    s = ALIAS_MAP.get(s, s)

    return s or "Unknown"


def guess_group(title: str) -> str:
    for pat, grp in GROUP_RULES:
        if re.search(pat, title, re.IGNORECASE):
            return grp
    return "📺 其他频道"


def guess_group_optimized(title: str, cfg: Config) -> str:
    """
    精细化分组：在通用规则前先按频道号判断央视子类。
    - CCTV-5 / CCTV-5+ -> 体育频道
    - CCTV-14 -> 儿童动画（已在通用规则首位）
    - CCTV-4 中文国际 -> 国际频道（央视频道组内更精确的语义）
    其余沿用通用规则。GROUP_FINE_GRAINED=0 时退回通用分组。
    """
    if not cfg.GROUP_FINE_GRAINED:
        return guess_group(title)

    if re.match(r"^CCTV-5(\+|\s|$)", title, re.I):
        return "⚽ 体育频道"
    if re.match(r"^CCTV-4\s+中文国际", title):
        return "🌍 国际频道"
    # CETV 是中国教育电视台，不能因纯英文被兜底到国际频道
    if re.match(r"^CETV\b", title, re.I):
        return "🎓 教育频道"
    return guess_group(title)


def group_title_with_quality(group: str, tier: str, cfg: Config) -> str:
    """给 group-title 追加画质档位后缀，便于播放器按清晰度筛选"""
    if not cfg.GROUP_SHOW_QUALITY or not tier or tier == "未知":
        return group
    return f"{group}·{tier}"


def make_tvg_id(name: str) -> str:
    """生成不含中文的 tvg-id，避免 EPG 匹配失败"""
    m = re.match(r"^(CCTV)-(\d+)", name)
    if m:
        return f"CCTV-{m.group(2)}"
    ascii_part = re.sub(r"[^A-Za-z0-9]+", "", name)
    return ascii_part or ("ch" + hashlib.md5(name.encode("utf-8")).hexdigest()[:8])


def make_logo_url(name: str, cfg: Config) -> str:
    """
    生成台标 URL。fanmingming 台标库的命名规则（实测）：
    - CCTV 系列无横杠: CCTV1.png / CCTV14.png（CCTV-14.png 是 404）
    - 中文频道直接用中文名: 卡酷少儿.png / 湖南卫视.png（拼音 hash 是 404）
    文件名需做 URL 编码，否则中文名在部分播放器中失效。
    """
    m = re.match(r"^CCTV-(\d+)", name)
    if m:
        filename = f"CCTV{int(m.group(1))}.png"
    elif name.upper().startswith("CCTV-4K"):
        filename = "CCTV4K.png"
    else:
        # 中文频道名直接用；去掉空格，保留中文/字母/数字
        filename = re.sub(r"\s+", "", name) + ".png"
    return f"{cfg.LOGO_BASE_URL}{quote(filename)}"


def clean_url(raw: str) -> str:
    return re.sub(r"[\$•].*|\s+.*", "", raw.strip()).rstrip("/?&")


def is_valid_url(u: str) -> bool:
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def get_host_key(u: str) -> Optional[str]:
    try:
        p = urlparse(u)
        port = p.port or (443 if p.scheme == "https" else 80)
        return f"{p.hostname}:{port}" if p.hostname else None
    except Exception:
        return None


def natural_sort_key(s: str) -> Tuple[Any, ...]:
    def conv(t: str) -> Any:
        return int(t) if t.isdigit() else t.lower()
    return tuple(conv(p) for p in re.split(r"(\d+)", s))


def load_blacklist(path: str) -> Tuple[Set[str], Set[str]]:
    hosts, host_ports = set(), set()
    if not os.path.exists(path):
        return hosts, host_ports
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                entry = raw.lower()
                if ":" in entry:
                    h, p = entry.split(":", 1)
                    if h and p.isdigit():
                        host_ports.add(f"{h.strip()}:{p}")
                else:
                    hosts.add(entry)
    except Exception as exc:
        logging.warning("读取黑名单失败: %s", exc)
    return hosts, host_ports


def is_blocked(url: str, hosts: Set[str], host_ports: Set[str]) -> bool:
    try:
        p = urlparse(url)
        h = (p.hostname or "").lower()
        if not h:
            return False
        port = p.port or (443 if p.scheme == "https" else 80)
        return f"{h}:{port}" in host_ports or h in hosts
    except Exception:
        return False


def load_channel_order(path: str) -> Dict[str, List[str]]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {k: v if isinstance(v, list) else [] for k, v in data.items()}
    except Exception as exc:
        logging.warning("频道顺序配置加载失败: %s", exc)
        return {}


# ============================================================
# M3U / TXT 解析
# ============================================================

def txt2m3u_content(txt: str) -> str:
    lines = txt.splitlines()
    group = ""
    out = ['#EXTM3U x-tvg-url="https://live.fanmingming.com/e.xml"']
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith(",#genre#"):
            group = line[:-8].strip()
        elif line.count(",") == 1:
            ch_name, url = [x.strip() for x in line.split(",", 1)]
            if not url.startswith(("http://", "https://")):
                continue
            tid = re.sub(r"[\s\-]", "", ch_name)
            out.append(
                f'#EXTINF:-1 tvg-id="{tid}" tvg-name="{tid}" '
                f'tvg-logo="{Config.LOGO_BASE_URL}{tid}.png" '
                f'group-title="{group}",{ch_name}'
            )
            out.append(url)
    return "\n".join(out)


def detect_and_convert(content: str) -> str:
    stripped = content.strip()
    if not stripped:
        return ""
    head = stripped[:300].upper()
    if "#EXTM3U" in head and "#EXTINF" in head:
        return stripped
    return txt2m3u_content(stripped)


def parse_m3u(content: str) -> List[Dict[str, str]]:
    channels: List[Dict[str, str]] = []
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    i = 0
    while i < len(lines):
        if lines[i].upper().startswith("#EXTINF:"):
            name = lines[i].split(",", 1)[1] if "," in lines[i] else "Unknown"
            if i + 1 < len(lines) and lines[i + 1] and not lines[i + 1].startswith("#"):
                url = clean_url(lines[i + 1])
                if is_valid_url(url):
                    channels.append({"name": name, "url": url})
                i += 2
                continue
        i += 1
    return channels


def parse_hls(text: str, base: str) -> Dict[str, Any]:
    """轻量 HLS 解析：不依赖 m3u8 库，只提取需要的字段"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines or not lines[0].upper().startswith("#EXTM3U"):
        raise ValueError("not an m3u8 playlist")

    is_vod = any("#EXT-X-ENDLIST" in l for l in lines)
    variants: List[Tuple[int, Optional[str], str]] = []
    segments: List[str] = []
    durations: List[float] = []
    pending: Optional[Tuple[int, Optional[str]]] = None
    last_duration = 0.0

    for line in lines[1:]:
        if line.startswith("#EXT-X-STREAM-INF"):
            bm = re.search(r"BANDWIDTH=(\d+)", line)
            rm = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            pending = (
                int(bm.group(1)) if bm else 0,
                f"{rm.group(1)}x{rm.group(2)}" if rm else None,
            )
        elif line.startswith("#EXTINF"):
            dm = re.search(r"#EXTINF:([\d.]+)", line)
            last_duration = float(dm.group(1)) if dm else 0.0
        elif line.startswith("#"):
            continue
        else:
            u = urljoin(base, line)
            if pending:
                variants.append((pending[0], pending[1], u))
                pending = None
            else:
                segments.append(u)
                durations.append(last_duration)
                last_duration = 0.0

    return {
        "is_vod": is_vod,
        "variants": variants,
        "segments": segments,
        "durations": durations,
    }


# ============================================================
# 分片头校验 + H.264 SPS 分辨率解析（纯 Python，无外部进程）
# ============================================================

class _BitReader:
    def __init__(self, data: bytes):
        self.d = data
        self.p = 0

    def u(self, n: int) -> int:
        v = 0
        for _ in range(n):
            if self.p >= len(self.d) * 8:
                raise ValueError("bit overflow")
            v = (v << 1) | ((self.d[self.p >> 3] >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 32:
                raise ValueError("bad exp-golomb")
        return (1 << zeros) - 1 + (self.u(zeros) if zeros else 0)


def _unescape_rbsp(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data) - 2:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3:
            out += data[i:i + 2]
            i += 3
        else:
            out.append(data[i])
            i += 1
    out += data[i:]
    return bytes(out)


_HIGH_PROFILES = {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}


def parse_h264_sps(data: bytes) -> Optional[Tuple[int, int]]:
    """best-effort：从分片头找 SPS 并解析 (宽, 高)，失败返回 None"""
    payload = None
    for pat in (b"\x00\x00\x00\x01\x67", b"\x00\x00\x01\x67"):
        i = data.find(pat)
        if i >= 0:
            payload = data[i + len(pat): i + len(pat) + 64]
            break
    if not payload:
        return None

    rbsp = _unescape_rbsp(payload)
    br = _BitReader(rbsp)
    try:
        profile = br.u(8)
        br.u(8)
        br.u(8)
        br.ue()
        if profile in _HIGH_PROFILES:
            chroma = br.ue()
            if chroma == 3:
                br.u(1)
            br.ue()
            br.ue()
            br.u(1)
            if br.u(1):
                return None
        br.ue()  # log2_max_frame_num_minus4
        poc_type = br.ue()
        if poc_type == 0:
            br.ue()
        elif poc_type == 1:
            return None
        br.ue()  # max_num_ref_frames
        br.u(1)   # gaps_in_frame_num_value_allowed
        w_mbs = br.ue()
        h_map = br.ue()
        frame_only = br.u(1)
        w = (w_mbs + 1) * 16
        h = ((2 - frame_only) * (h_map + 1)) * 16
        if 16 <= w <= 8192 and 16 <= h <= 8192:
            return (w, h)
    except Exception:
        return None
    return None


def check_segment_header(data: bytes) -> Tuple[bool, Optional[str]]:
    """校验分片是否真实视频数据，返回 (有效, 分辨率)"""
    if not data:
        return False, None

    # MPEG-TS: 0x47 同步字节，每 188 字节重复
    if data[0] == 0x47:
        if len(data) >= 189 and data[188] != 0x47:
            return False, None
        sps = parse_h264_sps(data[:2048])
        return True, (f"{sps[0]}x{sps[1]}" if sps else None)

    # fMP4: ftyp box
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return True, None

    return False, None


# ============================================================
# 质量评分：分辨率 + 真实码率（木桶效应，防假高清）
# ============================================================

# 分辨率档位 -> (最低像素值, 分数, 档位名)
RESOLUTION_TIERS: List[Tuple[int, float, str]] = [
    (3840 * 2160, 100.0, "4K"),
    (1920 * 1080, 92.0, "1080P"),
    (1280 * 720, 75.0, "720P"),
    (720 * 480, 55.0, "480P"),
]

# 码率档位 -> (最低 Mbps, 分数, 档位名)。取自真实分片字节/时长，不信任 manifest 声明
BITRATE_TIERS: List[Tuple[float, float, str]] = [
    (15.0, 100.0, "4K"),
    (4.0, 92.0, "1080P"),
    (1.5, 75.0, "720P"),
    (0.8, 55.0, "480P"),
    (0.0, 25.0, "SD"),
]

UNKNOWN_QUALITY_SCORE = 40.0  # 指标缺失时的中性分，避免误杀也避免虚高


def resolution_tier(resolution: Optional[str]) -> Tuple[float, str]:
    """把 '1920x1080' 映射到 (分数, 档位名)"""
    if not resolution:
        return UNKNOWN_QUALITY_SCORE, "未知"
    m = re.search(r"(\d{3,4})[xX](\d{3,4})", resolution)
    if not m:
        return UNKNOWN_QUALITY_SCORE, "未知"
    pixels = int(m.group(1)) * int(m.group(2))
    for threshold, score, name in RESOLUTION_TIERS:
        if pixels >= threshold * 0.9:  # 允许 10% 容差（如 1920x1080 的轻微裁剪）
            return score, name
    return 35.0, "SD"


def bitrate_tier(bitrate_mbps: float) -> Tuple[float, str]:
    """把真实码率映射到 (分数, 档位名)"""
    if bitrate_mbps <= 0:
        return UNKNOWN_QUALITY_SCORE, "未知"
    for threshold, score, name in BITRATE_TIERS:
        if bitrate_mbps >= threshold:
            return score, name
    return 25.0, "SD"


def evaluate_quality(
    resolution: Optional[str], bitrate_mbps: float, penalty: float = 10.0
) -> Tuple[float, str]:
    """
    质量分（0-100）+ 档位名。
    核心：分辨率分与码率分取较低者（木桶效应）。
    - '1920x1080' 但实测 0.3Mbps 的假高清 -> 码率档 SD -> 质量分 25
    - 两者档位差 >= 2 档，再扣 penalty（声明与事实严重不符）
    - 只有一个指标时，取该指标分但打折，避免过度自信
    """
    r_score, r_name = resolution_tier(resolution)
    b_score, b_name = bitrate_tier(bitrate_mbps)

    names = [n for n in (r_name, b_name) if n != "未知"]

    if r_name != "未知" and b_name != "未知":
        score = min(r_score, b_score)
        if abs(r_score - b_score) >= 30:
            score = max(0.0, score - penalty)
        tier = r_name if r_score <= b_score else b_name
    elif r_name != "未知":
        score = r_score * 0.9
        tier = r_name
    elif b_name != "未知":
        score = b_score * 0.9
        tier = b_name
    else:
        score = UNKNOWN_QUALITY_SCORE
        tier = "未知"

    return round(score, 1), tier


def speed_score(speed_mbps: float) -> float:
    """速度分（0-100）：0.15MB/s 起评，2MB/s 满分"""
    if speed_mbps <= 0.15:
        return 0.0
    if speed_mbps >= 2.0:
        return 100.0
    return round((speed_mbps - 0.15) / (2.0 - 0.15) * 100, 1)


def latency_score(ttfb_ms: int) -> float:
    """延迟分（0-100）：100ms 满分，3000ms 归零"""
    if ttfb_ms < 0:
        return 0.0
    if ttfb_ms <= 100:
        return 100.0
    if ttfb_ms >= 3000:
        return 0.0
    return round((3000 - ttfb_ms) / (3000 - 100) * 100, 1)


def composite_score(item: Dict[str, Any], cfg: Config) -> Dict[str, float]:
    """计算综合分及各分项，供排序与报告使用"""
    q = float(item.get("quality", 0))
    s = speed_score(float(item.get("speed", 0)))
    l = latency_score(int(item.get("ttfb", -1)))
    total = (
        cfg.WEIGHT_QUALITY * q
        + cfg.WEIGHT_SPEED * s
        + cfg.WEIGHT_LATENCY * l
    )
    return {
        "quality": q,
        "speed_score": s,
        "latency_score": l,
        "total": round(total, 2),
    }


# ============================================================
# 验证结果 / 历史缓存
# ============================================================

@dataclass
class ProbeResult:
    url: str
    alive: bool = False
    speed: float = 0.0
    ttfb_ms: int = -1
    resolution: Optional[str] = None
    bitrate_mbps: float = 0.0
    quality: float = 0.0
    quality_tier: str = ""
    stage: str = ""
    reason: str = ""


class History:
    """历史结果 + 冻结/解冻。连续失败 N 轮后冻结 M 轮不再测。"""

    def __init__(self, path: str, cfg: Config):
        self.path = path
        self.cfg = cfg
        self.data: Dict[str, Any] = {"version": 1, "run": 0, "updated": "", "urls": {}}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and isinstance(loaded.get("urls"), dict):
                    self.data = loaded
            except Exception as exc:
                logging.warning("历史缓存读取失败，将重建: %s", exc)

    def begin_run(self) -> None:
        self.data["run"] = int(self.data.get("run", 0)) + 1

    def is_frozen(self, url: str) -> bool:
        rec = self.data["urls"].get(url)
        if not rec:
            return False
        until = int(rec.get("frozen_until_run", 0))
        return until > int(self.data.get("run", 0))

    def hint(self, url: str) -> Optional[float]:
        rec = self.data["urls"].get(url)
        return float(rec.get("speed", 0)) if rec else None

    def record(self, url: str, result: ProbeResult) -> None:
        rec = self.data["urls"].setdefault(
            url, {"fails": 0, "oks": 0, "last_ok": "", "speed": 0.0, "quality": 0.0}
        )
        if result.alive:
            rec["fails"] = 0
            rec["oks"] = int(rec.get("oks", 0)) + 1
            rec["last_ok"] = beijing_time_str()
            rec["speed"] = round(result.speed, 3)
            rec["quality"] = result.quality
            rec.pop("frozen_until_run", None)
        else:
            rec["fails"] = int(rec.get("fails", 0)) + 1
            if rec["fails"] >= self.cfg.FREEZE_AFTER_FAILS:
                rec["frozen_until_run"] = (
                    int(self.data.get("run", 0)) + self.cfg.THAW_AFTER_RUNS
                )

    def save(self) -> None:
        self.data["updated"] = beijing_time_str()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)


# ============================================================
# 漏斗验证核心
# ============================================================

async def _fetch_playlist(
    url: str, session: ClientSession, cfg: Config
) -> Tuple[Optional[Dict[str, Any]], ProbeResult]:
    """L1: 拉 playlist，校验 HTTP / Content-Type / m3u8 结构 / VOD"""
    result = ProbeResult(url=url)
    headers = {"User-Agent": cfg.USER_AGENT, "Accept": "*/*"}
    timeout = ClientTimeout(
        total=cfg.PLAYLIST_TIMEOUT,
        connect=cfg.CONNECT_TIMEOUT,
        sock_read=cfg.READ_TIMEOUT,
    )
    try:
        t0 = time.monotonic()
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            result.ttfb_ms = int(round((time.monotonic() - t0) * 1000))
            if resp.status != 200:
                result.stage, result.reason = "L1", f"HTTP {resp.status}"
                return None, result
            ctype = resp.headers.get("Content-Type", "").lower()
            if "html" in ctype or "json" in ctype:
                result.stage, result.reason = "L1", f"fake content: {ctype}"
                return None, result
            text = await resp.text(errors="ignore")

        if not text.lstrip().upper().startswith("#EXTM3U"):
            result.stage, result.reason = "L1", "not m3u8"
            return None, result

        info = parse_hls(text, url)
        if info["is_vod"] and cfg.SKIP_VOD:
            result.stage, result.reason = "L1", "VOD skipped"
            return None, result
        if not info["segments"] and not info["variants"]:
            result.stage, result.reason = "L1", "empty playlist"
            return None, result
        return info, result
    except asyncio.TimeoutError:
        result.stage, result.reason = "L1", "playlist timeout"
    except Exception as exc:
        result.stage, result.reason = "L1", f"playlist error: {type(exc).__name__}"
    return None, result


async def _probe_segment(
    url: str, session: ClientSession, cfg: Config
) -> Tuple[bool, int, float, Optional[str], str]:
    """L2+L3: 分片头校验 + 采样下载测速
    返回 (有效, 字节数, 耗时秒, 分辨率, 失败原因)
    """
    headers = {"User-Agent": cfg.USER_AGENT, "Accept": "*/*"}
    timeout = ClientTimeout(
        total=cfg.SEG_TIMEOUT,
        connect=cfg.CONNECT_TIMEOUT,
        sock_read=cfg.READ_TIMEOUT,
    )
    try:
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                return False, 0, 0, None, f"HTTP {resp.status}"
            ctype = resp.headers.get("Content-Type", "").lower()
            if "html" in ctype:
                return False, 0, 0, None, "html segment"

            header = b""
            checked = False
            total = 0
            start = time.monotonic()

            async for chunk in resp.content.iter_chunked(4096):
                if not chunk:
                    continue
                total += len(chunk)
                if not checked:
                    header += chunk
                    if len(header) >= 1024:
                        checked = True
                        ok, res = check_segment_header(header[:1024])
                        if not ok:
                            return False, 0, 0, None, "bad segment header"
                if total >= cfg.MAX_BYTES_PER_SEGMENT:
                    break

            elapsed = time.monotonic() - start

            if not checked:
                ok, res = check_segment_header(header)
                if not ok:
                    return False, 0, 0, None, "bad/short header"
            else:
                _, res = check_segment_header(header[:1024])

            if total <= 0 or elapsed <= 0:
                return False, 0, 0, None, "no data"
            return True, total, elapsed, res, ""
    except asyncio.TimeoutError:
        return False, 0, 0, None, "segment timeout"
    except Exception as exc:
        return False, 0, 0, None, f"segment error: {type(exc).__name__}"


async def probe_source(
    url: str,
    session: ClientSession,
    semaphore: asyncio.Semaphore,
    cfg: Config,
) -> ProbeResult:
    """对单个 URL 执行完整漏斗验证"""
    result = ProbeResult(url=url)
    async with semaphore:
        is_hls = url.lower().split("?")[0].endswith(cfg.HLS_EXTENSIONS)
        segments: List[str] = []
        manifest_res: Optional[str] = None

        if is_hls:
            info, l1 = await _fetch_playlist(url, session, cfg)
            result.ttfb_ms = l1.ttfb_ms
            if info is None:
                result.stage, result.reason = l1.stage, l1.reason
                return result

            if info["segments"]:
                segments = info["segments"][:cfg.SEGMENT_SAMPLE]
            elif info["variants"]:
                best = max(info["variants"], key=lambda v: v[0])
                manifest_res = best[1]
                sub_info, sub_l1 = await _fetch_playlist(best[2], session, cfg)
                if sub_info is None:
                    result.stage, result.reason = sub_l1.stage, sub_l1.reason
                    return result
                segments = sub_info["segments"][:cfg.SEGMENT_SAMPLE]
        else:
            # 非 HLS：把自身当作一个分片样本
            segments = [url]

        if not segments:
            result.stage, result.reason = "L2", "no segments"
            return result

        # L2+L3: 并发采样，速度用 sum(bytes)/sum(elapsed)（等价串行吞吐）
        tasks = [_probe_segment(s, session, cfg) for s in segments]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        total_bytes = 0
        total_time = 0.0
        resolution = manifest_res
        failures: List[str] = []
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                failures.append(type(outcome).__name__)
                continue
            ok, size, elapsed, res, reason = outcome
            if not ok:
                failures.append(reason)
                continue
            total_bytes += size
            total_time += elapsed
            if resolution is None and res:
                resolution = res

        if not total_bytes or total_time <= 0:
            result.stage = "L2"
            result.reason = failures[0] if failures else "all segments failed"
            return result

        speed = total_bytes / total_time / 1024 / 1024

        # L4: 用 manifest 声明时长估算真实码率
        if is_hls and info and info.get("durations"):
            durs = info["durations"][:len(segments)]
            dsum = sum(d for d in durs if d > 0)
            if dsum > 0:
                result.bitrate_mbps = round(total_bytes * 8 / dsum / 1_000_000, 2)

        result.resolution = resolution
        result.speed = round(speed, 3)
        result.quality, result.quality_tier = evaluate_quality(
            resolution, result.bitrate_mbps, cfg.MISMATCH_PENALTY
        )

        if speed < cfg.MIN_SPEED_MBPS:
            result.stage, result.reason = "L3", f"too slow {speed:.2f}MB/s"
            return result
        if result.ttfb_ms > cfg.MAX_TTFB_MS:
            result.stage, result.reason = "L3", f"high ttfb {result.ttfb_ms}ms"
            return result

        result.alive = True
        result.reason = "ok"
        return result


async def probe_all(
    channels: List[Dict[str, str]],
    cfg: Config,
    history: History,
    ignore_cache: bool = False,
) -> Tuple[List[Dict[str, Any]], Counter]:
    """并发验证所有频道，返回带测速结果的列表和淘汰阶段统计"""
    connector = TCPConnector(ssl=False, limit=cfg.MAX_CONCURRENT)
    sem = asyncio.Semaphore(cfg.MAX_CONCURRENT)
    stage_counter: Counter = Counter()

    pending: List[Tuple[Dict[str, str], bool]] = []
    for ch in channels:
        frozen = history.is_frozen(ch["url"]) and not ignore_cache
        if frozen:
            stage_counter["CACHE_FROZEN"] += 1
            continue
        pending.append((ch, ignore_cache))

    async with ClientSession(connector=connector, trust_env=True) as session:
        tasks = [probe_source(ch["url"], session, sem, cfg) for ch, _ in pending]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    enriched: List[Dict[str, Any]] = []
    for (ch, _), res in zip(pending, results):
        if isinstance(res, Exception):
            res = ProbeResult(url=ch["url"], stage="EXC", reason=type(res).__name__)
        stage_counter[res.stage or "OK"] += 1
        history.record(ch["url"], res)
        if res.alive:
            item = dict(ch)
            item.update({
                "speed": res.speed,
                "ttfb": res.ttfb_ms,
                "resolution": res.resolution,
                "bitrate": res.bitrate_mbps,
                "quality": res.quality,
                "quality_tier": res.quality_tier,
            })
            enriched.append(item)
    return enriched, stage_counter


# ============================================================
# 聚合 / 排序 / 输出
# ============================================================

def rank_and_select(
    results: List[Dict[str, Any]], cfg: Config
) -> Tuple[List[Dict[str, Any]], int, List[Dict[str, Any]]]:
    """
    质量优先筛选 + 综合分排序；每频道保留 top N 且优先不同 host。
    1) 硬门槛：质量分低于 MIN_QUALITY_SCORE 的源直接淘汰（画质太差不保留）
    2) 软排序：综合分 = 质量*0.6 + 速度*0.3 + 延迟*0.1
    3) 同一频道内取 top N 不同 host，作为主源/备份
    返回 (选中列表, 重复源淘汰数, 被质量门槛淘汰的列表)
    """
    # 1) 质量硬门槛
    passed: List[Dict[str, Any]] = []
    quality_rejected: List[Dict[str, Any]] = []
    for item in results:
        if float(item.get("quality", 0)) < cfg.MIN_QUALITY_SCORE:
            item = dict(item)
            item["reject_reason"] = f"质量分 {item.get('quality', 0)} < {cfg.MIN_QUALITY_SCORE}"
            quality_rejected.append(item)
        else:
            passed.append(item)

    # 2) 计算综合分并排序
    for item in passed:
        item["score_detail"] = composite_score(item, cfg)
    passed.sort(key=lambda x: (-x["score_detail"]["total"], -x.get("speed", 0), x.get("ttfb", 9999)))

    # 3) 按频道分组，取 top N 不同 host
    by_name: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in passed:
        by_name[item["name"]].append(item)

    selected: List[Dict[str, Any]] = []
    dropped = 0
    for items in by_name.values():
        chosen: List[Dict[str, Any]] = []
        hosts: Set[str] = set()
        for item in items:
            host = get_host_key(item["url"]) or ""
            if not cfg.ALLOW_SAME_HOST_BACKUP and host in hosts:
                continue
            item["backup_index"] = len(chosen)  # 频道内独立编号：0=主源, 1/2=备份
            chosen.append(item)
            hosts.add(host)
            if len(chosen) >= cfg.KEEP_PER_CHANNEL:
                break
        # 统一按差额计数，避免与循环内的判断重复累加
        dropped += len(items) - len(chosen)
        selected.extend(chosen)

    selected.sort(key=lambda x: (x["name"], -x["score_detail"]["total"]))
    return selected, dropped, quality_rejected


def sort_channels_with_predefined(
    channels: List[Dict[str, Any]], predefined: List[str]
) -> List[Dict[str, Any]]:
    by_name: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in channels:
        by_name[item["name"]].append(item)

    ordered: List[Dict[str, Any]] = []
    for name in predefined:
        if name in by_name:
            ordered.extend(by_name.pop(name))
    for name in sorted(by_name.keys(), key=natural_sort_key):
        ordered.extend(by_name[name])
    return ordered


def write_outputs(
    channels: List[Dict[str, Any]],
    cfg: Config,
    stage_counter: Counter,
    stats: Dict[str, Any],
    dropped: int,
    quality_rejected: List[Dict[str, Any]],
) -> None:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in channels:
        grouped[guess_group_optimized(item["name"], cfg)].append(item)

    order = load_channel_order(cfg.CHANNEL_ORDER_FILE)
    for g, chs in grouped.items():
        grouped[g] = sort_channels_with_predefined(chs, order.get(g, []))

    ordered_groups: List[Tuple[str, List[Dict[str, Any]]]] = []
    for g in cfg.GROUP_OUTPUT_ORDER:
        if g in grouped:
            ordered_groups.append((g, grouped.pop(g)))
    for g, chs in grouped.items():
        ordered_groups.append((g, chs))

    # 每个频道的最优画质档位（该频道所有备份源共用同一后缀，避免频道被拆散）
    best_tier: Dict[str, str] = {}
    for item in channels:
        name, tier = item["name"], item.get("quality_tier", "")
        if name not in best_tier or tier == "4K":
            best_tier[name] = tier

    def write_playlist(path: str, items: List[Dict[str, Any]], *, backups: bool) -> None:
        """backups=False: 主列表每频道仅主源(换台一次到位); True: 全部源"""
        with open(path, "w", encoding="utf-8") as f:
            f.write('#EXTM3U x-tvg-url="https://live.fanmingming.com/e.xml"\n')
            f.write(f"# Generated at {beijing_time_str()} by IPTVCleaner v3.0\n")
            if backups:
                f.write("# 备份源列表：同频道多线路，播放失败时可手动切换；不影响主列表换台\n")
            else:
                f.write("# 主列表：每频道仅 1 条最优源，换台一次到位；备份见 live_backup.m3u\n")
            for group_name, chs in ordered_groups:
                for item in chs:
                    idx = item.get("backup_index", 0)
                    if backups and idx == 0:
                        continue  # 备份文件只放备源，避免与主列表重复
                    if not backups and idx != 0:
                        continue  # 主列表只放主源
                    tid = make_tvg_id(item["name"])
                    logo = make_logo_url(item["name"], cfg)
                    item_gt = group_title_with_quality(
                        group_name, best_tier.get(item["name"], ""), cfg
                    )
                    label = item["name"] if idx == 0 else f'{item["name"]} [备{idx}]'
                    detail = item.get("score_detail", {})
                    comment = (
                        f'# {item["name"]} | 质量{item.get("quality", 0)} '
                        f'({item.get("quality_tier", "未知")}) | '
                        f'{item.get("speed", 0)}MB/s | 码率{item.get("bitrate", 0)}Mbps | '
                        f'综合{detail.get("total", 0)}\n'
                    )
                    f.write(comment)
                    f.write(
                        f'#EXTINF:-1 tvg-id="{tid}" tvg-name="{tid}" '
                        f'tvg-logo="{logo}" group-title="{item_gt}",{label}\n'
                    )
                    f.write(f'{item["url"]}\n')

    os.makedirs(os.path.dirname(cfg.OUTPUT_FILE) or ".", exist_ok=True)
    write_playlist(cfg.OUTPUT_FILE, channels, backups=False)
    if cfg.WRITE_BACKUP and cfg.BACKUP_FILE:
        os.makedirs(os.path.dirname(cfg.BACKUP_FILE) or ".", exist_ok=True)
        write_playlist(cfg.BACKUP_FILE, channels, backups=True)

    report = [
        "# IPTV 清洗报告 v3.0 (轻量漏斗版)",
        f"> 时间: {beijing_time_str()}",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 原始频道 | {stats['raw']} |",
        f"| 过滤后 | {stats['filtered']} |",
        f"| 测速有效 | {stats['alive']} |",
        f"| 筛选后有效源 | {stats['final']} |",
        f"| 主列表频道数 | {stats['main']}（每频道 1 条） |",
        f"| 备份源数 | {stats['backup']}（单独文件） |",
        f"| 重复源淘汰 | {dropped} |",
        f"| 质量门槛淘汰 | {len(quality_rejected)} |",
        f"| 存活率 | {stats['rate']:.1f}% |",
        "",
        f"> 评分权重: 质量 {cfg.WEIGHT_QUALITY:.0%} + 速度 {cfg.WEIGHT_SPEED:.0%} + 延迟 {cfg.WEIGHT_LATENCY:.0%}；质量门槛 ≥ {cfg.MIN_QUALITY_SCORE}",
        "",
        "## 验证漏斗统计",
        "",
        "| 阶段 | 数量 |",
        "|---|---|",
    ]
    labels = {
        "CACHE_FROZEN": "缓存冻结（跳过）",
        "L1": "L1 playlist 淘汰",
        "L2": "L2 分片头淘汰",
        "L3": "L3 速度/延迟淘汰",
        "EXC": "异常",
        "OK": "通过",
    }
    for k, v in sorted(stage_counter.items(), key=lambda x: -x[1]):
        report.append(f"| {labels.get(k, k)} | {v} |")

    # 画质档位分布
    tier_counter = Counter(item.get("quality_tier", "未知") for item in channels)
    report += ["", "## 画质档位分布", ""]
    for tier, count in sorted(
        tier_counter.items(),
        key=lambda x: -next((t[2] for t in RESOLUTION_TIERS if t[2] == x[0]), 0),
    ):
        report.append(f"- **{tier}**: {count}")

    report += ["", "## 高质量源 Top 10", "", "| 频道 | 档位 | 质量 | 速度 | 码率 | 综合分 |", "|---|---|---|---|---|---|"]
    top = sorted(channels, key=lambda x: -x.get("score_detail", {}).get("total", 0))[:10]
    for item in top:
        d = item.get("score_detail", {})
        report.append(
            f'| {item["name"]} | {item.get("quality_tier", "未知")} | '
            f'{item.get("quality", 0)} | {item.get("speed", 0)}MB/s | '
            f'{item.get("bitrate", 0)}Mbps | {d.get("total", 0)} |'
        )

    if quality_rejected:
        report += ["", "## 质量门槛淘汰（Top 10）", "", "| 频道 | 质量分 | 速度 | 原因 |", "|---|---|---|---|"]
        for item in quality_rejected[:10]:
            report.append(
                f'| {item["name"]} | {item.get("quality", 0)} | '
                f'{item.get("speed", 0)}MB/s | {item.get("reject_reason", "")} |'
            )

    report += ["", "## 分组统计", ""]
    for g, chs in sorted(ordered_groups, key=lambda x: -len(x[1])):
        report.append(f"- **{g}**: {len(chs)}")

    with open(cfg.REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")

    logging.info("输出已保存: %s, %s", cfg.OUTPUT_FILE, cfg.REPORT_FILE)


def send_bark(report_path: str, device_key: str) -> None:
    if not device_key:
        return
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as exc:
        logging.error("Bark 读取报告失败: %s", exc)
        return

    if len(content) > 1500:
        content = content[:1500] + "\n\n...(内容过长，请查看完整报告)"

    payload = {
        "device_key": device_key,
        "title": "IPTV 清洗报告",
        "markdown": content,
        "group": "IPTV",
    }
    try:
        timeout = ClientTimeout(total=10)
        async def _post():
            async with ClientSession(timeout=timeout) as s:
                async with s.post("https://api.day.app/push", json=payload) as r:
                    return r.status
        status = asyncio.run(_post())
        if status == 200:
            logging.info("Bark 报告已发送")
        else:
            logging.warning("Bark 发送失败: HTTP %s", status)
    except Exception as exc:
        logging.error("Bark 发送异常: %s", exc)


# ============================================================
# 源加载 + 主流程
# ============================================================

async def load_sources(cfg: Config) -> Tuple[str, List[Dict[str, Any]]]:
    """并发拉取远程源 + 本地源，合并为 M3U 文本"""
    if not os.path.exists(cfg.BASE_URL_FILE):
        raise FileNotFoundError(f"源配置不存在: {cfg.BASE_URL_FILE}")

    with open(cfg.BASE_URL_FILE, "r", encoding="utf-8") as f:
        urls = [
            line.strip() for line in f
            if line.strip() and not line.strip().startswith("#")
            and line.strip().startswith(("http://", "https://"))
        ]

    details: List[Dict[str, Any]] = []
    chunks: List[str] = []
    sem = asyncio.Semaphore(cfg.SOURCE_FETCH_CONCURRENT)
    headers = {"User-Agent": cfg.USER_AGENT}

    async def fetch(url: str) -> Tuple[str, Dict[str, Any]]:
        detail: Dict[str, Any] = {"type": "remote", "url": url, "success": False}
        async with sem:
            try:
                timeout = ClientTimeout(total=15, connect=cfg.CONNECT_TIMEOUT)
                async with ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status != 200:
                            detail["error"] = f"HTTP {resp.status}"
                            return "", detail
                        text = await resp.text(errors="ignore")
                converted = detect_and_convert(text)
                if converted.strip():
                    detail.update({"success": True, "chars": len(converted)})
                    return converted, detail
            except Exception as exc:
                detail["error"] = type(exc).__name__
        return "", detail

    outcomes = await asyncio.gather(*(fetch(u) for u in urls))
    for text, detail in outcomes:
        details.append(detail)
        if text:
            chunks.append(text)

    if os.path.isdir(cfg.LOCAL_DIR):
        for fname in sorted(os.listdir(cfg.LOCAL_DIR)):
            if not fname.lower().endswith((".txt", ".m3u")):
                continue
            path = os.path.join(cfg.LOCAL_DIR, fname)
            detail = {"type": "local", "path": path, "success": False}
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = f.read()
                text = txt2m3u_content(raw) if fname.lower().endswith(".txt") else raw
                if text.strip():
                    chunks.append(text)
                    detail["success"] = True
            except Exception as exc:
                detail["error"] = type(exc).__name__
            details.append(detail)

    return "\n".join(chunks), details


async def async_main(args: argparse.Namespace, cfg: Config) -> None:
    logging.info("=" * 60)
    logging.info("IPTV 清洗系统 v3.0 (轻量漏斗 + 备份源 + 历史缓存)")
    logging.info("=" * 60)

    history = History(cfg.HISTORY_FILE, cfg)
    if not args.full:
        history.begin_run()

    raw_content, source_details = await load_sources(cfg)
    raw_channels = parse_m3u(raw_content)
    logging.info("解析到 %d 条原始记录", len(raw_channels))

    host_bl, port_bl = load_blacklist(cfg.BLACKLIST_FILE)
    seen: Set[str] = set()
    filtered: List[Dict[str, str]] = []
    for ch in raw_channels:
        url = ch["url"]
        if not is_valid_url(url) or url in seen:
            continue
        if is_blocked(url, host_bl, port_bl):
            continue
        seen.add(url)
        filtered.append({
            "name": normalize_channel_name(ch["name"]),
            "url": url,
            "original": ch["name"],
        })

    logging.info("过滤后 %d 条，唯一主机 %d 个",
                 len(filtered), len({get_host_key(c["url"]) for c in filtered}))

    if args.limit:
        filtered = filtered[:args.limit]
        logging.info("调试模式：只处理前 %d 条", len(filtered))

    if args.dry_run:
        logging.info("dry-run 模式，跳过测速")
        return

    t0 = time.monotonic()
    enriched, counter = await probe_all(filtered, cfg, history, args.full)
    elapsed = time.monotonic() - t0
    logging.info("测速完成，耗时 %.1fs，漏斗统计 %s", elapsed, dict(counter))

    selected, dropped, quality_rejected = rank_and_select(enriched, cfg)
    main_count = sum(1 for x in selected if x.get("backup_index", 0) == 0)
    logging.info(
        "筛选完成：主列表 %d 个频道 + 备份源 %d 条，重复源淘汰 %d 条，质量门槛淘汰 %d 条",
        main_count, len(selected) - main_count, dropped, len(quality_rejected),
    )

    stats = {
        "raw": len(raw_channels),
        "filtered": len(filtered),
        "alive": len(enriched),
        "final": len(selected),
        "main": main_count,
        "backup": len(selected) - main_count,
        "rate": (len(enriched) / len(filtered) * 100) if filtered else 0,
    }
    write_outputs(selected, cfg, counter, stats, dropped, quality_rejected)
    history.save()

    if cfg.BARK_DEVICE_KEY:
        send_bark(cfg.REPORT_FILE, cfg.BARK_DEVICE_KEY)


def main() -> None:
    parser = argparse.ArgumentParser(description="轻量 IPTV 源清洗与验证")
    parser.add_argument("--full", action="store_true", help="强制全量重测，忽略缓存")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（调试）")
    parser.add_argument("--dry-run", action="store_true", help="只解析不测速")
    parser.add_argument("--config-dir", default="", help="配置文件目录（覆盖默认路径）")
    parser.add_argument("--debug", action="store_true", help="调试日志")
    args = parser.parse_args()

    setup_logger(args.debug)
    cfg = Config()
    if args.config_dir:
        base = args.config_dir.rstrip("/")
        cfg.BASE_URL_FILE = f"{base}/remote_sources.txt"
        cfg.BLACKLIST_FILE = f"{base}/blackHost_list.txt"
        cfg.CHANNEL_ORDER_FILE = f"{base}/channel_order.json"
        cfg.HISTORY_FILE = f"{base}/../output/history.json"
        cfg.OUTPUT_FILE = f"{base}/../output/live.m3u"
        cfg.REPORT_FILE = f"{base}/../output/report.md"

    try:
        asyncio.run(async_main(args, cfg))
    except KeyboardInterrupt:
        logging.info("用户中断")
    except Exception as exc:
        logging.exception("致命错误: %s", exc)


if __name__ == "__main__":
    main()
