# -*- coding: utf-8 -*-
"""极简 CDP 客户端(只依赖 websocket-client),用于在已登录浏览器里读接口/抓请求。

rev: 增加事件收集(drain/collect),否则 Network.requestWillBeSent 这类事件会被丢掉。
"""
import json
import time
import urllib.request

import websocket


class CDP:
    def __init__(self, ws_url, timeout=30):
        self.ws = websocket.create_connection(ws_url, timeout=timeout,
                                              suppress_origin=True,
                                              max_size=64 * 1024 * 1024)
        self.mid = 0
        self.events = []          # 本轮收集到的事件
        self._collect = False

    # ---------- 底层 ----------
    def call(self, method, **params):
        self.mid += 1
        mid = self.mid
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result")
            if self._collect:
                self.events.append(msg)

    def drain(self, quiet=0.35, max_sec=6.0):
        """把缓冲区里待收的事件读干净(以 quiet 秒静默为界)。"""
        t0 = time.time()
        old = self.ws.gettimeout()
        try:
            while time.time() - t0 < max_sec:
                self.ws.settimeout(quiet)
                try:
                    msg = json.loads(self.ws.recv())
                except Exception:
                    break
                if self._collect:
                    self.events.append(msg)
        finally:
            try:
                self.ws.settimeout(old)
            except Exception:
                pass

    def start_collect(self):
        self.events.clear()
        self._collect = True

    def stop_collect(self):
        self._collect = False
        return list(self.events)

    # ---------- 便捷 ----------
    def eval(self, expr, await_promise=True):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=await_promise, userGesture=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"])[:400])
        return (r.get("result") or {}).get("value")

    def eval_obj(self, expr):
        """返回 JS 对象的 objectId(用于 DOM.setFileInputFiles 等需要句柄的 API)。"""
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=False,
                      awaitPromise=False, userGesture=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"])[:400])
        return (r.get("result") or {}).get("objectId")

    def click(self, x, y):
        """真实鼠标点击(比 el.click() 更接近用户,能触发 hover 菜单)。"""
        for t in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", type=t, x=x, y=y,
                      button="left", clickCount=1)

    def insert_text(self, text):
        self.call("Input.insertText", text=text)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def targets(port=9222):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as r:
        return json.loads(r.read().decode())


def pick(port=9222, match="creator.douyin.com"):
    for t in targets(port):
        if t.get("type") == "page" and match in (t.get("url") or ""):
            return t
    return None


def open_tab(url, port=9222):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{url}", method="PUT")
    return json.loads(urllib.request.urlopen(req, timeout=20).read().decode())


def close_tab(tid, port=9222):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{tid}", timeout=10).read()
    except Exception:
        pass


def api_requests(events, needles=("creator.douyin.com",)):
    """从收集到的事件里提取业务请求(GET 的 query + POST 的 postData)。"""
    out = []
    for e in events:
        if e.get("method") != "Network.requestWillBeSent":
            continue
        p = e.get("params") or {}
        req = p.get("request") or {}
        url = req.get("url") or ""
        if not any(n in url for n in needles):
            continue
        out.append({
            "reqid": p.get("requestId"),
            "method": req.get("method"),
            "url": url,
            "headers": {k: v for k, v in (req.get("headers") or {}).items()
                        if k.lower() in ("content-type", "x-secsdk-csrf-token",
                                         "x-tt-trace-id", "referer", "origin",
                                         "x-requested-with")},
            "postData": req.get("postData"),
        })
    return out
