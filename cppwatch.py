#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CPP 摊位审核状态监控（GitHub Actions 版 v2）

- 直接调用 allcpp 的接口读取摊位审核状态，不再依赖页面 HTML 文本
- 状态有变化才通知，没变化保持完全静默
- 登录态失效时，只在早班（北京时间 08 点那一轮）提示一次

接口来源（均需登录 cookie）：
  https://www.allcpp.cn/allcpp/event/getEventListApprove.do   → 摊位列表与审核状态
  https://www.allcpp.cn/allcpp/event/getSectionType.do        → 摊位主题名

状态映射（eventCircle.eventEnabled）：
  0 已过审 / 1 审核中 / 2 未过审 / 3 未抽中 / 4 已退摊
  若 eventEnabled==1 且 isReadyPublish 为真 → 预审通过 等待签约
主题标记（eventCircle.sectionEnabled）：
  0 (通过) / 1 (待审核) / 2 (被退审) / 3 (未通过)
"""

import json
import os
import ssl
import smtplib
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.header import Header

API_LIST = ("https://www.allcpp.cn/allcpp/event/getEventListApprove.do"
            "?eventmainid=0&pageindex=1&pagesize=20&showapprove=1"
            "&eventEnabled=1&cityId=-1&typeId=-1")
API_SECTION = "https://www.allcpp.cn/allcpp/event/getSectionType.do?sectionId="

STATE_PATH = "state.json"
BJT = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
REFERER = "https://www.allcpp.cn/mng/apply.do?t=1&pageNo=1"

TARGET_NAME = "COMICUP 33"
TARGET_CID = 2396237

STATUS_MAP = {
    0: "已过审",
    1: "审核中",
    2: "未过审",
    3: "未抽中",
    4: "已退摊",
}
SECTION_MAP = {
    0: "(通过)",
    1: "(待审核)",
    2: "(被退审)",
    3: "(未通过)",
}


def log(msg):
    now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s" % (now, msg), flush=True)


def fetch_json(url, cookie):
    req = urllib.request.Request(url, headers={
        "Cookie": cookie,
        "User-Agent": UA,
        "Referer": REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        text = raw.decode(charset, errors="replace")
    return json.loads(text)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(snap):
    data = dict(snap)
    data["checked_at"] = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_mail(subject, body):
    host = os.environ.get("SMTP_HOST")
    if not host:
        return False
    port = int(os.environ.get("SMTP_PORT") or 465)
    user = os.environ.get("SMTP_USER")
    pwd = os.environ.get("SMTP_PASS")
    to = os.environ.get("MAIL_TO") or user
    if not (user and pwd and to):
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to
    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as s:
            s.login(user, pwd)
            s.sendmail(user, [to], msg.as_string())
        log("邮件已发送")
        return True
    except Exception as e:
        log("邮件发送失败：%s" % e)
        return False


def send_pushplus(subject, body):
    token = os.environ.get("PUSHPLUS_TOKEN")
    if not token:
        return False
    payload = json.dumps({
        "token": token,
        "title": subject,
        "content": body,
        "template": "txt",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://www.pushplus.plus/send",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            result = resp.read().decode("utf-8", errors="replace")
        log("PushPlus 返回：%s" % result[:200])
        return True
    except Exception as e:
        log("PushPlus 发送失败：%s" % e)
        return False


def notify(subject, body):
    sent = False
    if send_pushplus(subject, body):
        sent = True
    if send_mail(subject, body):
        sent = True
    if not sent:
        log("没有任何可用的通知通道（缺 PUSHPLUS_TOKEN 或 SMTP_* 配置），本次只打印结果")


def pick_target(payload):
    """从接口返回里挑出目标摊位，返回 (eventCircle, item)"""
    result = (payload or {}).get("result") or {}
    items = result.get("list") or []
    for item in items:
        circle = item.get("eventCircle") or {}
        if not circle:
            continue
        if circle.get("circleId") == TARGET_CID:
            return circle, item
        name = item.get("name") or item.get("totalName") or ""
        if TARGET_NAME in name:
            return circle, item
    return None, None


def section_name(section_id, cookie):
    if not section_id:
        return ""
    try:
        payload = fetch_json(API_SECTION + str(section_id), cookie)
        inner = (payload.get("result") or {}).get("result") or {}
        return inner.get("name") or ""
    except Exception as e:
        log("取摊位主题名失败（不影响主流程）：%s" % e)
        return ""


def main():
    hour = datetime.now(BJT).hour
    cookie = os.environ.get("CPP_COOKIE", "").strip()
    if not cookie:
        log("缺少 CPP_COOKIE，监控没在跑")
        if hour == 8:
            notify("CPP 监控：cookie 没配置", "仓库里还没设置 CPP_COOKIE，监控没在跑。")
        return

    try:
        payload = fetch_json(API_LIST, cookie)
    except urllib.error.HTTPError as e:
        log("接口请求失败 HTTP %s" % e.code)
        if hour == 8:
            notify("CPP 监控：这轮没查成", "接口请求失败（HTTP %s），请在 App 里看一眼摊位状态。" % e.code)
        return
    except Exception as e:
        log("接口请求异常：%s" % e)
        if hour == 8:
            notify("CPP 监控：这轮没查成", "接口请求异常，请在 App 里看一眼摊位状态。")
        return

    circle, item = pick_target(payload)

    # 登录态失效或结构变化时，result 为空 / 列表为空
    if circle is None:
        log("接口没有返回目标摊位，可能是登录态失效")
        if hour == 8:
            notify("CPP 监控这轮登录态已过期",
                   "查不到 COMICUP 33 的摊位信息。请在 App 的「摊位管理」里看一眼，"
                   "或回来跟我说一声，重新取一次 cookie。")
        return

    enabled = circle.get("eventEnabled", -1)
    status_text = STATUS_MAP.get(enabled, "未知状态(%s)" % enabled)
    if enabled == 1 and circle.get("isReadyPublish"):
        status_text = "预审通过 等待签约：%s摊" % (circle.get("prePositionCount") or 0)

    sid = circle.get("sectionId") or 0
    theme = section_name(sid, cookie) if sid > 0 else "无专区"
    suffix = SECTION_MAP.get(circle.get("sectionEnabled"), "")
    theme_text = (theme + " " + suffix).strip()

    snap = {
        "event": TARGET_NAME,
        "cid": circle.get("circleId") or TARGET_CID,
        "status": status_text,
        "theme": theme_text,
        "shop": circle.get("positionName") or "",
        "count": str(circle.get("positionCount") or 0),
    }
    log("读到：%s | %s | 已申摊位 %s 个" % (snap["status"], snap["theme"], snap["count"]))

    old = load_state()
    if not old.get("status"):
        log("首次运行，只建立基线，不发消息")
        save_state(snap)
        return

    changed = []
    if old.get("status") != snap["status"]:
        changed.append("审核状态：%s → %s" % (old.get("status"), snap["status"]))
    if old.get("theme") != snap["theme"]:
        changed.append("摊位主题：%s → %s" % (old.get("theme"), snap["theme"]))
    if old.get("shop") != snap["shop"]:
        changed.append("摊位名：%s → %s" % (old.get("shop"), snap["shop"]))
    if old.get("count") != snap["count"]:
        changed.append("已申摊位：%s → %s 个" % (old.get("count"), snap["count"]))

    if not changed:
        log("与上次一致，保持静默")
        save_state(snap)
        return

    body = "COMICUP 33（CID %s）摊位有变动：\n%s\n\n当前：%s | %s" % (
        snap["cid"], "\n".join("- " + c for c in changed), snap["status"], snap["theme"])
    log("检测到变动：" + "；".join(changed))
    notify("CPP 摊位审核有变动：" + snap["status"], body)
    save_state(snap)


if __name__ == "__main__":
    main()
