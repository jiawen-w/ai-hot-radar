#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI热点雷达 · 云端自动更新脚本（GitHub Actions 运行）
=====================================================
职责：抓取 AIBase 热门条目 → 规则打分筛选 → 追加更新 AI热点雷达.html（历史累积制）→ 输出变更摘要。

设计约束：
- 零第三方依赖（仅标准库 urllib / re / json / datetime），GitHub runner 可直接运行。
- 历史累积制：RAW 数组保留全部历史条目，新热点以带 added 字段的条目插入最前；无新增时仅刷 META.updated。
- 安全追加：每条对象独立以 `,\n` 结尾，杜绝「缺逗号导致页面白屏」的历史 bug。
- 规则打分：热度 + 爆款关键词 + 新鲜度，映射到 S/A/B（S≥85 / A 70-84 / B 55-69，<55 不上榜）。
- 去重：候选与已有 RAW 标题做 4-gram 共享判定，同题合并取热度最高者。

输出：stdout 输出一行 JSON 摘要（changed / added_count / grades / titles / message），供 workflow 判断是否提交。
"""

import re
import json
import urllib.request
from datetime import datetime, timedelta, timezone

# ============ 常量 ============
CN_TZ = timezone(timedelta(hours=8))
FEED_URL = "https://news.aibase.com/zh/news"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
HTML_PATH = "AI热点雷达.html"
MAX_ITEMS = 6          # 单轮最多上榜条数（取候选分最高者）
MIN_SCORE = 55         # 低于此分不上榜
S_TH = 85
A_TH = 70
B_TH = 55
DAILY_SUMMARY_SKIP = ("AI日报", "AI 日报", "早报", "晚报", "周报")

# 爆款关键词权重（命中标题则加分，取前 3 个类别作为标签）
KEYWORDS = [
    (("开源", "open source", "open-source"), 6, ("开源", "开源")),
    (("大模型", "模型", "LLM", "Foundation Model"), 4, ("大模型", "大模型")),
    (("机器人", "人形", "具身", "humanoid", "embodied"), 5, ("人形机器人", "机器人")),
    (("安全", "泄露", "窃听", "木马", "漏洞", "黑客", "隐私"), 4, ("安全", "安全")),
    (("融资", "IPO", "收购", "裁员", "就业", "估值", "融资额"), 4, ("商业", "商业")),
    (("免费", "普惠", "开放", "全面开放"), 3, ("产品", "产品")),
    (("首个", "首次", "第一", "第一家"), 4, ("里程碑", "里程碑")),
    (("成本", "降价", "价格", "回本", "烧钱"), 3, ("商业", "商业")),
    (("DeepSeek", "OpenAI", "Anthropic", "谷歌", "Google", "微软", "Microsoft", "苹果", "Apple",
      "腾讯", "阿里", "字节", "马斯克", "Musk", "华为", "小米", "英伟达", "NVIDIA", "Meta", "亚马逊"), 5, ("巨头", "大厂")),
    (("GPT", "Claude", "Gemini", "ChatGPT", "Llama", "Qwen", "MiniMax", "Kimi", "豆包"), 4, ("大模型", "产品")),
]

# why 传播点文案（按命中类别拼装）
WHY_MAP = {
    "开源": "开源动作自带技术圈传播力",
    "大模型": "大模型话题国民关注度高",
    "人形机器人": "人形机器人是当下最强科技叙事",
    "安全": "安全/泄露话题易触发情绪共鸣",
    "商业": "融资并购叙事吃瓜与投资人通吃",
    "产品": "免费/普惠直击用户痛点",
    "里程碑": "行业首个/首次的里程碑叙事",
    "巨头": "大厂出手自带流量光环",
}
# angle 选题角度文案
ANGLE_MAP = {
    "开源": "从'为什么又开源'切入，拆解生态布局与商业化路径",
    "大模型": "从'新能力强在哪'切入，实测对比同类产品",
    "人形机器人": "从'机器人落地到哪一步'切入，拆解技术进展与商用前景",
    "安全": "从'风险有多大'切入，复盘事件并给出防护建议",
    "商业": "从'钱流向哪里'切入，分析资本风向与商业逻辑",
    "产品": "从'对普通人有什么用'切入，实测上手体验",
    "里程碑": "从'行业第一意味着什么'切入，讲清格局变化",
    "巨头": "从'大厂为什么押注'切入，拆解战略意图",
}


def log(msg):
    print("[radar]", msg, flush=True)


def fetch_feed():
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "ignore")


def clean_html(t):
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def parse_feed(data):
    """解析 AIBase 首页条目，返回 [{nid,title,sum,time,hot_k}]（按页面顺序）"""
    items = []
    for nid, body in re.findall(r'<a href="/zh/news/(\d+)"[^>]*>(.*?)</a>', data, re.S):
        alt = re.search(r'alt="([^"]+)"', body)
        title = clean_html(alt.group(1)) if alt else ""
        if not title:
            m = re.search(r'font600[^>]*>([^<]+)', body)
            title = clean_html(m.group(1)) if m else ""
        if len(title) < 8:
            continue
        # 摘要
        desc = re.search(r'min-h-\[44px\][^>]*>(.*?)</div>', body, re.S) \
            or re.search(r'<p[^>]*>(.*?)</p>', body, re.S)
        summary = clean_html(desc.group(1)) if desc else ""
        # 时间
        tm = re.search(r'icon-rili[^>]*></i>\s*([^<]{1,14}?)</div>', body)
        tstr = tm.group(1).strip() if tm else ""
        # 热度（K 或 万）
        hot = re.search(r'icon-fangwenliang1[^>]*></i>\s*([\d.]+[K万]?)', body)
        hot_str = hot.group(1).strip() if hot else "0K"
        try:
            hot_k = float(hot_str.rstrip("K"))
        except ValueError:
            hot_k = 0.0
        if "万" in hot_str:
            hot_k = float(hot_str.rstrip("万")) * 10
        items.append({"nid": nid, "title": title, "sum": summary, "time": tstr, "hot_k": hot_k})
    return items


def grams(title, n=4):
    """4-gram 中文滑窗 + 英文词，用于同题判定"""
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", title.lower())
    g = set()
    for i in range(len(t) - n + 1):
        g.add(t[i:i + n])
    for w in re.findall(r"[a-z0-9]{3,}", title.lower()):
        g.add(w)
    return g


def same_topic(a, b):
    """同题判定：1) 4-gram 共享≥3；2) 或共享主体实体词≥1 且共享动作词≥1"""
    ga, gb = grams(a), grams(b)
    if len(ga & gb) >= 3:
        return True
    ea = [e for e in ENTITIES if e.lower() in a.lower()]
    eb = [e for e in ENTITIES if e.lower() in b.lower()]
    aa = [x for x in ACTIONS if x.lower() in a.lower()]
    ab = [x for x in ACTIONS if x.lower() in b.lower()]
    return bool(set(ea) & set(eb)) and bool(set(aa) & set(ab))


# 事件主体实体（用于同题判定）
ENTITIES = ["DeepSeek", "OpenAI", "Anthropic", "Claude", "ChatGPT", "Gemini", "GPT",
            "MiniMax", "蚂蚁", "腾讯", "阿里", "字节", "谷歌", "微软", "苹果", "Meta",
            "荣耀", "奇瑞", "山姆", "迪士尼", "Gartner", "Mastra", "Strata", "亚马逊",
            "商汤", "百度", "华为", "小米", "京东", "网易", "录音门", "元宝", "WorkBuddy",
            "Sora", "Gemma", "Llama", "Qwen", "Kimi"]
# 动作/事件词（用于同题判定）
ACTIONS = ["开源", "发布", "推出", "上线", "开放", "官宣", "融资", "收购", "裁员",
           "升级", "更新", "模型", "投资", "IPO", "亮相", "登场", "开源框架", "测试",
           "接入", "回本", "复刻", "准则"]


def rule_score(title, hot_k, time_str):
    """规则打分：热度基准 + 新鲜度 + 关键词，返回 (score, tags, why, angle)"""
    base = 55 + 1.8 * (hot_k - 5.0)          # 5K→55，10K→64，12K→67.6
    base = max(50.0, min(80.0, base))

    time_bonus = 3.0 if "刚刚" in time_str else 0.0

    kw_bonus = 0.0
    cats = []
    for words, w, cat in KEYWORDS:
        if any(wd.lower() in title.lower() for wd in words):
            kw_bonus += w
            if cat not in cats:
                cats.append(cat)
    kw_bonus = min(kw_bonus, 18.0)

    score = base + time_bonus + kw_bonus
    score = max(50.0, min(92.0, round(score)))

    if len(cats) >= 3:
        cats = cats[:3]
    tags = [c[0] for c in cats] or ["AI"]
    why_parts = [WHY_MAP.get(c, "") for c in cats if c in WHY_MAP]
    why = ("、".join(why_parts) if why_parts else "话题自带讨论度") + \
          f"，页面热度 {hot_k:.1f}K 印证关注度。"
    angles = [ANGLE_MAP.get(c, "") for c in cats if c in ANGLE_MAP]
    angle = angles[0] if angles else "从'这事为什么值得关注'切入，讲清背景、影响与下一步。"
    return score, tags, why, angle


def grade_of(score):
    if score >= S_TH:
        return "S"
    if score >= A_TH:
        return "A"
    if score >= B_TH:
        return "B"
    return None


def load_existing_titles(html):
    # 兼容两种格式：新批次 title:"..."（无空格）与历史批次 title: "..."（有空格）
    return re.findall(r'title\s*:\s*"([^"]+)"', html)


def build_items(html_path):
    """抓取 → 去重 → 打分 → 返回上榜条目列表（含 why/angle/sum 兜底）"""
    log("抓取信源: " + FEED_URL)
    data = fetch_feed()
    items = parse_feed(data)
    log(f"解析到 {len(items)} 条候选")

    # 过滤日报汇总类
    items = [it for it in items if not any(k in it["title"] for k in DAILY_SUMMARY_SKIP)]

    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    existing = load_existing_titles(html)
    log(f"已有条目 {len(existing)} 条")

    # 与已有条目去重
    fresh = []
    for it in items:
        dup = any(same_topic(it["title"], et) for et in existing)
        if not dup:
            fresh.append(it)
    log(f"去重后剩余 {len(fresh)} 条")

    # 同轮内同题合并（保留热度最高者）
    merged = []
    for it in fresh:
        hit = None
        for m in merged:
            if same_topic(it["title"], m["title"]):
                hit = m
                break
        if hit is None:
            merged.append(dict(it))
        elif it["hot_k"] > hit["hot_k"]:
            hit.update(it)
    log(f"同题合并后 {len(merged)} 条")

    # 打分筛选
    ranked = []
    for it in merged:
        score, tags, why, angle = rule_score(it["title"], it["hot_k"], it["time"])
        grade = grade_of(score)
        if grade is None:
            continue
        it["score"] = score
        it["grade"] = grade
        it["tags"] = tags
        it["why"] = why
        it["angle"] = angle
        ranked.append(it)

    ranked.sort(key=lambda x: (x["score"], x["hot_k"]), reverse=True)
    # 多源热议标记：热度 ≥ 8K
    for it in ranked:
        it["hot"] = it["hot_k"] >= 8.0
    ranked = ranked[:MAX_ITEMS]
    log(f"上榜 {len(ranked)} 条: " + ", ".join(f"{i['grade']}{i['score']}" for i in ranked))
    return ranked


def js_obj(d):
    line = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
    line = re.sub(r'"(\w+)":', r"\1:", line)
    return "  " + line


def update_html(html_path, new_items, ts_str):
    with open(html_path, encoding="utf-8") as f:
        s = f.read()

    date_str = ts_str.split(" ")[0]
    meta_new = f'const META = {{ date: "{date_str}", updated: "{ts_str}" }};'
    s = re.sub(r"const META = \{[^}]*\};", meta_new, s, count=1)

    if new_items:
        block = ",\n".join(js_obj(dict(
            added=ts_str[5:], grade=it["grade"], score=it["score"], title=it["title"],
            source="AIBase · 自动", lang="cn", hot=it["hot"], sum=it["sum"] or "（自动收录）",
            why=it["why"], angle=it["angle"], tags=it["tags"],
        )) for it in new_items)
        anchor = "const RAW = [\n"
        assert anchor in s
        s = s.replace(anchor, anchor + block + ",\n", 1)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(s)
    return s


def self_check(html):
    """校验：无缺逗号、条目数、字段完整"""
    m = re.search(r"const RAW = \[(.*?)\n\];", html, re.S)
    if not m:
        return False, "RAW 数组结构异常"
    body = m.group(1)
    lines = body.split("\n")
    for i, line in enumerate(lines):
        st = line.strip()
        if not st:
            continue
        if st.endswith("}") and not st.endswith("},"):
            if i < len(lines) - 1 and lines[i + 1].strip().startswith("{"):
                return False, f"缺逗号: 第{i}行"
    return True, "OK"


def main():
    try:
        now = datetime.now(CN_TZ)
        ts = now.strftime("%Y-%m-%d %H:%M")
        log("开始更新，时间: " + ts)

        new_items = build_items(HTML_PATH)

        if not new_items:
            # 无新增：完全不动文件，避免刷时间戳导致 git 刷屏与 Pages 频繁重建
            print(json.dumps({"changed": False, "added_count": 0, "grades": [],
                              "titles": [], "message": "今日无新增值得写热点", "check": "SKIP", "ok": True}))
            return

        updated = update_html(HTML_PATH, new_items, ts)
        ok, msg = self_check(updated)
        grades = [it["grade"] for it in new_items]
        titles = [it["title"] for it in new_items]
        print(json.dumps({"changed": True, "added_count": len(new_items), "grades": grades,
                          "titles": titles, "message": f"新增{len(new_items)}条", "check": msg, "ok": ok}))
    except Exception as e:
        print(json.dumps({"changed": False, "added_count": 0, "grades": [], "titles": [],
                          "message": f"ERROR: {e}", "ok": False}))
        raise


if __name__ == "__main__":
    main()
