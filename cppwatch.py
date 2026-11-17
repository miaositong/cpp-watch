#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CPP 摊位审核状态监控（GitHub Actions 版）
- 每天/每小时读一次摊主中心页面，看「摊位审核状态」是否变了
- 变了才发通知；没变保持沉默
- 登录态失效时，只在早班那一轮（北京时间 8 点场）发一条提示
"""

import os
import re
import json
import ssl
import smtplib
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.header import Header

TARGET_URL = "https://www.allcpp.cn/mng/apply.do?t=1&pageNo=1"
STATE_PATH = "state.json"
BJT = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")


def log(msg):
    print(f"[{datetime.now(BJT).strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def strip_tags(html):
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<br\s*/?>", "\n", html)
    html = re.sub(r"(?s)</(p|div|li|tr|h\d)>", "\n", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = (html.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">"))
    html = re.sub(r"[ \t\u3000]+", " ", html)
    html = re.sub(r"\n\s*\n+", "\n", html)
    return html.strip()


def fetch_page(cookie):
    req = urllib.request.Request(TARGET_URL, headers={
        "Cookie": cookie,
        "User-Agent": UA,
        "Referer": "https://www.allcpp.cn/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.status, raw.decode(charset, errors="replace")


STATUS_RE = re.compile(r"摊位审核状态[：:]\s*([^\n|]{1,40})")
THEME_RE = re.compile(r"摊位主题[：:]\s*([^\n|]{1,60})")
NAME_RE = re.compile(r"摊位名[：:]\s*([^\n|]{1,60})")
COUNT_RE = re.compile(r"已申摊位[：:]\s*([^\n|]{1,20})")


def parse(text):
    out = {}
    for key, rx in (("status", STATUS_RE), ("theme", THEME_RE),
                    ("shop", NAME_RE), ("count", COUNT_RE)):
        m = rx.search(text)
        if m:
            out[key] = m.group(1).strip()
    return out


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"状态文件读取失败，按无基线处理：{e}")
    return {}


def save_state(state, snap):
    state.update(snap)
    state["checked_at"] = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    log(f"状态已写回：{json.dumps(snap, ensure_ascii=False)}")


def send_mail(subject, body):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT") or 465)
    user = os.environ.get("SMTP_USER")
    pwd = os.environ.get("SMTP_PASS")
    to = os.environ.get("MAIL_TO") or user
    if not (host and user and pwd and to):
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as s:
                s.login(user, pwd)
                s.sendmail(user, [to], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(user, pwd)
                s.sendmail(user, [to], msg.as_string())
        log("邮件通知已发送")
        return True
    except Exception as e:
        log(f"邮件通知失败：{e}")
        return False


def send_pushplus(subject, body):
    token = os.environ.get("PUSHPLUS_TOKEN")
    if not token:
        return False
    payload = json.dumps({"token": token, "title": subject, "content": body,
                          "template": "txt"}).encode()
    req = urllib.request.Request("https://www.pushplus.plus/send", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            log(f"PushPlus 返回：{r.read().decode('utf-8', 'ignore')[:200]}")
        return True
    except Exception as e:
        log(f"PushPlus 通知失败：{e}")
        return False


def notify(subject, body):
    ok = False
    if os.environ.get("PUSHPLUS_TOKEN"):
        ok = send_pushplus(subject, body) or ok
    if os.environ.get("SMTP_HOST"):
        ok = send_mail(subject, body) or ok
    if not ok:
        log("没有任何可用的通知通道（缺 PUSHPLUS_TOKEN 或 SMTP_* 配置），本次只打印结果")
    return ok


def main():
    now = datetime.now(BJT)
    is_morning = now.hour == 8

    cookie = os.environ.get("CPP_COOKIE", "").strip()
    if not cookie:
        log("缺少 CPP_COOKIE")
        if is_morning:
            notify("CPP 监控：cookie 未配置", "仓库里还没设置 CPP_COOKIE，监控没在跑。")
        return

    try:
        code, html = fetch_page(cookie)
    except urllib.error.HTTPError as e:
        log(f"HTTP {e.code}")
        if is_morning:
            notify("CPP 监控：登录态失效",
                   f"摊主中心返回 HTTP {e.code}，cookie 可能过期了。\n"
                   "请重新取一次 cookie 更新仓库里的 CPP_COOKIE。")
        return
    except Exception as e:
        log(f"请求异常：{e}")
        if is_morning:
            notify("CPP 监控：请求失败", f"连不上 CPP 页面：{e}")
        return

    text = strip_tags(html)
    snap = parse(text)

    if not snap.get("status"):
        log("页面上找不到「摊位审核状态」，判断为登录态失效")
        if is_morning:
            notify("CPP 监控：登录态失效",
                   "读不到摊位审核状态，估计 cookie 过期了。\n"
                   "请重新取 cookie，更新仓库里的 CPP_COOKIE。")
        return

    log(f"读到：{json.dumps(snap, ensure_ascii=False)}")

    state = load_state()
    old_status = state.get("status")
    old_theme = state.get("theme")
    old_shop = state.get("shop")

    if not old_status:
        log("首次运行，建立基线，不发送消息")
        save_state(state, snap)
        return

    changed = []
    if snap.get("status") and snap["status"] != old_status:
        changed.append(f"审核状态：{old_status} → {snap['status']}")
    if snap.get("theme") and old_theme and snap["theme"] != old_theme:
        changed.append(f"摊位主题：{old_theme} → {snap['theme']}")
    if snap.get("shop") and old_shop and snap["shop"] != old_shop:
        changed.append(f"摊位名：{old_shop} → {snap['shop']}")

    if not changed:
        log("无变动，保持静默")
        save_state(state, snap)
        return

    body = "\n".join(changed)
    body += (f"\n\n当前：{snap.get('shop', '')} / 主题 {snap.get('theme', '')}"
             f" / 已申 {snap.get('count', '')}")
    body += f"\n检查时间：{now.strftime('%Y-%m-%d %H:%M')}"
    log("状态有变动，发送通知：\n" + body)
    notify("CPP 摊位审核状态有变", body)
    save_state(state, snap)


if __name__ == "__main__":
    main()
