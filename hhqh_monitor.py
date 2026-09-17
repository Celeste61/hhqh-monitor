# -*- coding: utf-8 -*-
"""
《航海王 启航》官网动态监控 -> 微信推送

监控渠道: 官网「航海日志」(公告 / 新闻 / 活动 / 资料 / 攻略)，公开接口，无需登录。

另外还预留了 TapTap、任意 RSS（可接公众号）、官方微博三个可选渠道，
默认全部关闭；如果哪天想接回来，把 config.json 里对应块的 enabled 改成 true 即可。

用法:
  python hhqh_monitor.py --dry-run     # 只抓取并打印，不发微信、不改状态
  python hhqh_monitor.py --init        # 首次运行：把当前内容记为"已读"，不推送
  python hhqh_monitor.py               # 正常巡检：有新内容就推微信
  python hhqh_monitor.py --test        # 发一条测试消息，验证推送通道

依赖: 仅标准库，Python 3.8+
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
TZ = timezone(timedelta(hours=8))

# Windows 命令行默认是 GBK，遇到表情符号会报错，这里统一切到 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA_PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
UA_WEBAPP = "V=1&PN=WebApp&LANG=zh_CN&VN_CODE=100000000&LOC=CN&PLT=PC&DS=Android"

DEFAULT_CONFIG = {
    # ---- 官网「航海日志」 ----
    "official": {
        "enabled": True,
        "game_id": "12000128",
        "per_page": 20,
    },
    # ---- TapTap 官方号 ----
    "taptap": {
        "enabled": False,
        "group_id": 53899,          # 航海王-启航 论坛分组
        "app_id": 6968,
        "only_official": True,      # 只看官方（蓝V）发布的帖子
    },
    # ---- 微信公众号（通过 RSS 中转，见 README）----
    # 例如自建 wewe-rss 后填 https://你的域名/feeds/xxxxx.rss
    "rss": {
        "enabled": False,
        "urls": [],
    },
    # ---- 官方微博（需登录 Cookie，默认关闭）----
    "weibo": {
        "enabled": False,
        "uid": "5118618742",        # 微博「航海王启航」官方号
        "cookie": "",               # 直接粘贴浏览器里复制的 Cookie（本地跑最省事）
        "cookie_env": "WEIBO_COOKIE",
    },
    # ---- 微信推送 ----
    # provider: pushplus（推荐，免费 200 条/天）| serverchan | none
    "push": {
        "provider": "pushplus",
        "token": "",                # 也可用环境变量 PUSH_TOKEN
        "token_env": "PUSH_TOKEN",
    },
    # 关键词过滤：exclude 里的词一旦命中标题就不推送（例如刷屏的"新服公告"）
    "filter": {
        "exclude_keywords": [],
        "include_keywords": [],
    },
    # 各渠道最小抓取间隔（分钟）。外层定时任务可以跑得很勤，这里做安全限速：
    # 官网是公开 JSON 接口，可以勤一点；微博带登录 Cookie，太勤容易触发风控。
    "intervals": {
        "official": 2,
        "taptap": 5,
        "weibo": 10,
        "rss": 10,
    },
    "state_keep": 400,              # 每个渠道保留多少条历史指纹用于去重
}


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def log(msg):
    print("[%s] %s" % (datetime.now(TZ).strftime("%H:%M:%S"), msg), flush=True)


def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    req.add_header("User-Agent", UA_PC)
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", "replace")


def http_post_json(url, payload, headers=None, timeout=20):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("User-Agent", UA_PC)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def ts_to_str(ts):
    try:
        return datetime.fromtimestamp(int(ts), TZ).strftime("%m-%d %H:%M")
    except Exception:
        return ""


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        log("已生成配置文件: %s" % CONFIG_PATH)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    # 补齐新增字段
    for key, value in DEFAULT_CONFIG.items():
        if key not in cfg:
            cfg[key] = value
        elif isinstance(value, dict):
            for sub, sub_value in value.items():
                cfg[key].setdefault(sub, sub_value)
    return cfg


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"seen": {}}


def save_state(state, cfg):
    keep = int(cfg.get("state_keep", 400))
    for key in list(state["seen"].keys()):
        state["seen"][key] = state["seen"][key][-keep:]
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# 各渠道抓取
# --------------------------------------------------------------------------
def fetch_official(cfg):
    conf = cfg["official"]
    url = ("https://pangu-api.deyogames.com/api/article_list"
           "?game_id=%s&page=1&per_page=%d") % (conf["game_id"], int(conf["per_page"]))
    data = json.loads(http_get(url))
    items = []
    for art in data.get("data", {}).get("article_list", []):
        cid = art.get("category_id", 1)
        items.append({
            "id": "official:%s" % art["id"],
            "source": "官网·%s" % (art.get("article_category", {}).get("name") or "资讯"),
            "title": art.get("title", "").strip(),
            "url": "https://op.5xgames.cn/home/article/%s?category=%s" % (art["id"], cid),
            "ts": int(art.get("show_time") or art.get("created_at") or 0),
        })
    return items


def fetch_taptap(cfg):
    conf = cfg["taptap"]
    url = "https://www.taptap.cn/webapiv2/feed/v7/by-group?group_id=%s" % conf["group_id"]
    data = json.loads(http_get(url, headers={"X-UA": UA_WEBAPP}))
    items = []
    for row in data.get("data", {}).get("list", []):
        if row.get("type") != "moment":
            continue
        moment = row.get("moment") or {}
        is_official = bool(moment.get("is_official"))
        if conf.get("only_official", True) and not is_official:
            continue
        topic = moment.get("topic") or {}
        title = (topic.get("title") or "").strip()
        if not title:
            summary = (topic.get("summary") or "").strip().splitlines()
            title = summary[0] if summary else "(无标题)"
        mid = moment.get("id_str") or ""
        if not mid:
            continue
        items.append({
            "id": "taptap:%s" % mid,
            "source": "TapTap·官方",
            "title": title,
            "url": "https://www.taptap.cn/moment/%s" % mid,
            "ts": int(moment.get("created_time") or 0),
        })
    return items


def fetch_weibo(cfg):
    """官方微博。微博对匿名抓取已全面拦截（实测 PC 端返回登录页、手机端返回 432），
    必须带登录后的 Cookie 才能读到时间线。Cookie 可以写在 config.json 的
    weibo.cookie 里，也可以放进环境变量（默认 WEIBO_COOKIE）。"""
    conf = cfg["weibo"]
    cookie = (conf.get("cookie") or "").strip() or \
        os.environ.get(conf.get("cookie_env", "WEIBO_COOKIE"), "").strip()
    if not cookie:
        log("跳过微博：config.json 的 weibo.cookie 和环境变量 %s 都是空的"
            % conf.get("cookie_env", "WEIBO_COOKIE"))
        return []

    uid = conf["uid"]
    headers = {
        "Cookie": cookie,
        "Referer": "https://weibo.com/u/%s" % uid,
        "X-Requested-With": "XMLHttpRequest",
    }
    token = re.search(r"XSRF-TOKEN=([^;]+)", cookie)
    if token:
        headers["X-XSRF-TOKEN"] = urllib.parse.unquote(token.group(1))

    # 通道一：PC 端时间线接口
    try:
        text = http_get("https://weibo.com/ajax/statuses/mymblog"
                        "?uid=%s&page=1&feature=0" % uid, headers=headers)
        if text.lstrip().startswith("{"):
            rows = (json.loads(text).get("data") or {}).get("list") or []
            if rows:
                return _weibo_to_items(rows, uid)
        log("微博 PC 接口没有返回内容，改试手机接口")
    except Exception as exc:
        log("微博 PC 接口失败（%s），改试手机接口" % exc)

    # 通道二：手机端时间线接口
    try:
        mobile = dict(headers)
        mobile["Cookie"] = cookie + "; WEIBOCN_FROM=1110006030"
        mobile["MWeibo-Pwa"] = "1"
        mobile["Referer"] = "https://m.weibo.cn/u/%s" % uid
        url = ("https://m.weibo.cn/api/container/getIndex"
               "?type=uid&value=%s&containerid=107603%s" % (uid, uid))
        cards = (json.loads(http_get(url, headers=mobile)).get("data") or {}).get("cards") or []
        rows = []
        for card in cards:
            if card.get("mblog"):
                rows.append(card["mblog"])
            for sub in card.get("card_group") or []:
                if sub.get("mblog"):
                    rows.append(sub["mblog"])
        if rows:
            return _weibo_to_items(rows, uid)
        log("微博手机接口也没拿到内容，Cookie 可能不完整或已过期")
    except Exception as exc:
        log("微博手机接口失败: %s" % exc)
    return []


def _weibo_title(raw):
    """微博正文第一行常常只有 #话题#，把它剥掉再取摘要。"""
    text = re.sub(r"<[^>]+>", "", raw or "")
    text = text.replace("&nbsp;", " ").replace("\u200b", "")
    cleaned = text.strip()
    while True:
        stripped = re.sub(r"^(#[^#\n]{1,30}#\s*)", "", cleaned)
        stripped = re.sub(r"^(@[^\s：:]{1,20}\s*)", "", stripped)
        if stripped == cleaned:
            break
        cleaned = stripped
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    title = lines[0] if lines else text.strip()
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) > 55:
        title = title[:55] + "…"
    return title or "(无正文)"


def _weibo_to_items(rows, uid):
    items = []
    for st in rows:
        mid = st.get("idstr") or (str(st["id"]) if st.get("id") else "")
        if not mid:
            continue
        raw = st.get("text_raw") or re.sub(r"<[^>]+>", "", st.get("text") or "")
        ts = 0
        if st.get("created_at"):
            try:
                ts = int(datetime.strptime(
                    st["created_at"], "%a %b %d %H:%M:%S %z %Y").timestamp())
            except Exception:
                ts = 0
        items.append({
            "id": "weibo:%s" % mid,
            "source": "微博·官方",
            "title": _weibo_title(raw),
            "url": "https://weibo.com/%s/%s" % (uid, st.get("mblogid") or mid),
            "ts": ts,
        })
    return items


def _rss_date(text):
    if not text:
        return 0
    text = text.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text.replace("GMT", "+0000"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            return int(dt.timestamp())
        except Exception:
            continue
    return 0


def fetch_rss(cfg):
    """任意 RSS / Atom 源，用来接微信公众号（例如自建 wewe-rss 生成的文章源）。"""
    conf = cfg["rss"]
    items = []
    for feed_url in conf.get("urls", []):
        try:
            xml_text = http_get(feed_url)
            root = ET.fromstring(xml_text.encode("utf-8"))
        except Exception as exc:
            log("RSS 抓取失败 %s (%s)" % (feed_url, exc))
            continue
        name = "RSS"
        channel = root.find("channel")
        if channel is not None:
            name = (channel.findtext("title") or "RSS").strip()
            nodes = channel.findall("item")
        else:
            ns = {"a": "http://www.w3.org/2005/Atom"}
            name = (root.findtext("a:title", default="RSS", namespaces=ns) or "RSS").strip()
            nodes = root.findall("a:entry", ns)
        for node in nodes[:20]:
            title = (node.findtext("title") or "").strip()
            link = (node.findtext("link") or "").strip()
            if not link:
                link_el = node.find("{http://www.w3.org/2005/Atom}link")
                if link_el is not None:
                    link = link_el.get("href", "")
            date = (node.findtext("pubDate") or node.findtext("pubdate")
                    or node.findtext("{http://www.w3.org/2005/Atom}updated") or "")
            guid = (node.findtext("guid") or link or title)
            if not title:
                continue
            items.append({
                "id": "rss:%s:%s" % (feed_url, guid),
                "source": name[:18],
                "title": title,
                "url": link,
                "ts": _rss_date(date),
            })
    return items


FETCHERS = [
    ("official", fetch_official),
    ("taptap", fetch_taptap),
    ("weibo", fetch_weibo),
    ("rss", fetch_rss),
]


# --------------------------------------------------------------------------
# 微信推送
# --------------------------------------------------------------------------
def build_message(items):
    groups = {}
    for item in items:
        groups.setdefault(item["source"], []).append(item)
    title = "启航动态 %d 条 · %s" % (
        len(items), datetime.now(TZ).strftime("%m-%d %H:%M"))
    chunks = []
    for source, rows in groups.items():
        chunks.append("<p><b>%s（%d）</b></p><ul>" % (html.escape(source), len(rows)))
        for row in rows:
            line = "<li><a href=\"%s\">%s</a>" % (html.escape(row["url"]),
                                                  html.escape(row["title"]))
            if row["ts"]:
                line += " <span style=\"color:#999\">%s</span>" % ts_to_str(row["ts"])
            chunks.append(line + "</li>")
        chunks.append("</ul>")
    return title, "".join(chunks)


def push(cfg, title, content):
    conf = cfg["push"]
    provider = (conf.get("provider") or "none").lower()
    token = (conf.get("token") or "").strip() or \
        os.environ.get(conf.get("token_env", "PUSH_TOKEN"), "").strip()
    if provider == "none":
        log("推送已关闭(provider=none)，本次内容未发送")
        return False
    if not token:
        log("缺少推送 token：请在 config.json 的 push.token 或环境变量 %s 中填写"
            % conf.get("token_env"))
        return False
    try:
        if provider == "pushplus":
            resp = http_post_json("https://www.pushplus.plus/send", {
                "token": token,
                "title": title,
                "content": content,
                "template": "html",
            })
        elif provider == "serverchan":
            form = urllib.parse.urlencode({"title": title, "desp": content}).encode()
            req = urllib.request.Request(
                "https://sctapi.ftqq.com/%s.send" % token, data=form)
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=20) as resp_obj:
                resp = resp_obj.read().decode("utf-8", "replace")
        else:
            log("未知推送服务: %s" % provider)
            return False
        log("推送响应: %s" % resp.strip()[:200])
        return True
    except urllib.error.HTTPError as exc:
        log("推送失败 HTTP %s: %s" % (exc.code, exc.read().decode("utf-8", "replace")[:200]))
    except Exception as exc:
        log("推送失败: %s" % exc)
    return False


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def collect(cfg, state, force=False):
    """按各渠道自己的最小间隔决定这一轮要不要抓。"""
    all_items = []
    intervals = cfg.get("intervals") or {}
    last_run = state.setdefault("last_run", {})
    now = int(time.time())
    for key, func in FETCHERS:
        conf = cfg.get(key, {})
        if not conf.get("enabled"):
            continue
        every = int(intervals.get(key, 0) or 0) * 60
        previous = int(last_run.get(key, 0) or 0)
        if every and not force and previous and now - previous < every:
            log("%s: 距上次仅 %d 秒，未到 %d 分钟间隔，本轮跳过"
                % (key, now - previous, every // 60))
            continue
        last_run[key] = now
        try:
            found = func(cfg)
            log("%s: 抓到 %d 条" % (key, len(found)))
            all_items.extend(found)
        except Exception as exc:
            log("%s: 抓取出错 -> %s" % (key, exc))
    return all_items


def apply_filter(items, cfg):
    rule = cfg.get("filter", {})
    excludes = [w for w in rule.get("exclude_keywords", []) if w]
    includes = [w for w in rule.get("include_keywords", []) if w]
    if not excludes and not includes:
        return items
    kept = []
    for item in items:
        title = item["title"]
        if includes and not any(w in title for w in includes):
            continue
        if excludes and any(w in title for w in excludes):
            continue
        kept.append(item)
    return kept


def main():
    parser = argparse.ArgumentParser(description="航海王启航动态监控")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不推送、不写状态")
    parser.add_argument("--init", action="store_true", help="首次运行，只记录不推送")
    parser.add_argument("--test", action="store_true", help="发送一条测试推送")
    parser.add_argument("--test-weibo", action="store_true",
                        help="只测试微博通道，打印最近几条，不发推送")
    parser.add_argument("--force", action="store_true",
                        help="忽略历史状态，把当前最新内容全部推送一次")
    args = parser.parse_args()

    cfg = load_config()

    if args.test:
        push(cfg, "启航监控测试消息",
             "如果你在微信里看到这条消息，说明推送通道已经打通。")
        return

    if args.test_weibo:
        rows = fetch_weibo(cfg)
        if rows:
            print("微博通道正常，拿到 %d 条：" % len(rows))
            for item in rows[:5]:
                print("  %s  %s" % (ts_to_str(item["ts"]), item["title"]))
        else:
            print("微博没有拿到内容，多半是 Cookie 不完整或已失效，往上翻看报错信息。")
        return

    state = load_state()
    first_run = not STATE_PATH.exists()

    items = collect(cfg, state, force=args.force or args.dry_run)
    items = apply_filter(items, cfg)
    if not items:
        if not args.dry_run:
            save_state(state, cfg)
        log("本次没有抓到任何内容")
        return

    new_items = items if args.force else [
        item for item in items if item["id"] not in set(state["seen"].get(item["source"], []))
    ]
    if not args.force:
        for item in items:
            state["seen"].setdefault(item["source"], [])
            if item["id"] not in state["seen"][item["source"]]:
                state["seen"][item["source"]].append(item["id"])

    new_items.sort(key=lambda x: x["ts"], reverse=True)

    if args.dry_run:
        print("\n=== 本次可推送内容（dry-run，未发送、未写状态）===")
        for item in new_items:
            print("  [%s] %s  %s" % (item["source"], ts_to_str(item["ts"]), item["title"]))
        print("=== 共 %d 条 ===" % len(new_items))
        return

    if first_run or args.init:
        save_state(state, cfg)
        log("首次运行：已记录当前 %d 条内容为基线，从下次开始推送新动态" % len(items))
        return

    save_state(state, cfg)

    if not new_items:
        log("没有新动态，保持安静")
        return

    title, content = build_message(new_items)
    log("发现 %d 条新动态：%s" % (len(new_items), title))
    push(cfg, title, content)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
