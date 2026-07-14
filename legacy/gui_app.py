"""视频号评论区管理 - Tkinter 桌面 UI。

布局:顶部工具栏(启停/间隔/抓取/导出) | 左侧(账号管理+关键字) | 右侧(评论列表+手动回复)
后端 AccountManager 跑独立 asyncio loop(线程),GUI 用 run_coroutine_threadsafe 调用。
"""
import json
import csv
import asyncio
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime
from loguru import logger
from storage import Storage
from account_manager import AccountManager

COLORS = {
    "bg": "#1e1e2e", "bg_card": "#2a2a3c", "bg_light": "#353548",
    "text": "#e0e0e0", "text_dim": "#8888aa", "accent": "#7c83ff",
    "border": "#3a3a4e", "success": "#5fd97a", "warning": "#ffb454", "danger": "#ff5c5c",
}
FONTS = {
    "title": ("Microsoft YaHei", 16, "bold"),
    "heading": ("Microsoft YaHei", 12, "bold"),
    "body": ("Microsoft YaHei", 10),
    "mono": ("Consolas", 10),
}


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("视频号评论区管理")
        self.root.geometry("1280x780")
        self.root.configure(bg=COLORS["bg"])
        self.config = self._load_config()
        self.storage = Storage(self.config.get("db_path", "./data/comments.db"))
        self.account_mgr = AccountManager(self.config, self.storage)
        self.loop = None
        self._backend_thread = None
        self._running = False
        self._setup_style()
        self._build_ui()
        self._refresh_accounts()
        self.root.after(2000, self._poll)

    def _load_config(self):
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"accounts": [], "fetch_interval_sec": 300,
                    "auto_reply": {"enabled": False, "rules": []}, "db_path": "./data/comments.db"}

    def _save_config(self):
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", background=COLORS["bg_card"], foreground=COLORS["text"],
                        fieldbackground=COLORS["bg_card"], rowheight=28, font=FONTS["body"])
        style.map("Treeview", background=[("selected", COLORS["accent"])],
                  foreground=[("selected", "#ffffff")])
        style.configure("TButton", font=FONTS["body"])
        style.configure("TEntry", fieldbackground=COLORS["bg_light"], foreground=COLORS["text"])

    def _build_ui(self):
        # 顶部工具栏
        toolbar = tk.Frame(self.root, bg=COLORS["bg_card"], padx=12, pady=8)
        toolbar.pack(fill=tk.X, padx=8, pady=(8, 4))
        tk.Label(toolbar, text="📝 视频号评论区管理", font=FONTS["heading"],
                 bg=COLORS["bg_card"], fg=COLORS["accent"]).pack(side=tk.LEFT)
        self.btn_start = ttk.Button(toolbar, text="启动", command=self._start)
        self.btn_start.pack(side=tk.LEFT, padx=(20, 4))
        self.btn_stop = ttk.Button(toolbar, text="停止", command=self._stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="立即抓取", command=self._fetch_now).pack(side=tk.LEFT, padx=4)
        tk.Label(toolbar, text="间隔(秒):", font=FONTS["body"],
                 bg=COLORS["bg_card"], fg=COLORS["text_dim"]).pack(side=tk.LEFT, padx=(15, 4))
        self.interval_var = tk.StringVar(value=str(self.config.get("fetch_interval_sec", 300)))
        ttk.Entry(toolbar, textvariable=self.interval_var, width=6).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="保存设置", command=self._save_settings).pack(side=tk.LEFT, padx=8)
        ttk.Button(toolbar, text="导出CSV", command=self._export_csv).pack(side=tk.RIGHT)
        self.status_lbl = tk.Label(toolbar, text="未启动", font=FONTS["body"],
                                   bg=COLORS["bg_card"], fg=COLORS["text_dim"])
        self.status_lbl.pack(side=tk.RIGHT, padx=8)

        # 左侧面板
        left = tk.Frame(self.root, bg=COLORS["bg_card"], padx=10, pady=10, width=330)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 4), pady=4)
        left.pack_propagate(False)
        self._build_account_panel(left)
        self._build_keyword_panel(left)

        # 右侧
        right = tk.Frame(self.root, bg=COLORS["bg"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 8), pady=4)
        self._build_comment_list(right)
        self._build_reply_bar(right)

    def _build_account_panel(self, parent):
        tk.Label(parent, text="账号管理", font=FONTS["heading"],
                 bg=COLORS["bg_card"], fg=COLORS["accent"]).pack(anchor="w")
        self.acct_tree = ttk.Treeview(parent, columns=("name", "status"), show="headings", height=6)
        self.acct_tree.heading("name", text="账号")
        self.acct_tree.heading("status", text="状态")
        self.acct_tree.column("name", width=190)
        self.acct_tree.column("status", width=80)
        self.acct_tree.pack(fill=tk.X, pady=(4, 4))
        btns = tk.Frame(parent, bg=COLORS["bg_card"])
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="添加账号", command=self._add_account).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="删除", command=self._del_account).pack(side=tk.LEFT, padx=2)

    def _build_keyword_panel(self, parent):
        tk.Label(parent, text="关键字自动回复(一行一个:关键字|回复内容)",
                 font=FONTS["heading"], bg=COLORS["bg_card"], fg=COLORS["accent"]).pack(anchor="w", pady=(12, 0))
        self.auto_var = tk.BooleanVar(value=self.config.get("auto_reply", {}).get("enabled", False))
        ttk.Checkbutton(parent, text="启用自动回复", variable=self.auto_var).pack(anchor="w", pady=(4, 4))
        self.kw_text = tk.Text(parent, height=12, font=FONTS["mono"],
                               bg=COLORS["bg_light"], fg=COLORS["text"], relief="flat")
        self.kw_text.pack(fill=tk.BOTH, expand=True, pady=(0, 4))
        for r in self.config.get("auto_reply", {}).get("rules", []):
            self.kw_text.insert(tk.END, f"{r.get('keyword', '')}|{r.get('reply', '')}\n")
        ttk.Button(parent, text="保存关键字", command=self._save_keywords).pack(anchor="w")

    def _build_comment_list(self, parent):
        cols = ("account", "video", "user", "content", "time", "replied")
        self.tree = ttk.Treeview(parent, columns=cols, show="headings", height=20)
        for c, t, w in [("account", "账号", 80), ("video", "视频", 180), ("user", "用户", 100),
                        ("content", "评论内容", 300), ("time", "时间", 120), ("replied", "已回", 50)]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        vsb.pack(fill=tk.Y, side=tk.RIGHT)

    def _build_reply_bar(self, parent):
        bar = tk.Frame(parent, bg=COLORS["bg_card"], padx=10, pady=8)
        bar.pack(fill=tk.X, pady=(4, 0))
        tk.Label(bar, text="手动回复:", font=FONTS["body"],
                 bg=COLORS["bg_card"], fg=COLORS["text_dim"]).pack(side=tk.LEFT)
        self.reply_entry = ttk.Entry(bar, font=FONTS["body"])
        self.reply_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        self.reply_entry.bind("<Return>", lambda e: self._manual_reply())
        ttk.Button(bar, text="回复", command=self._manual_reply).pack(side=tk.LEFT)
        ttk.Button(bar, text="🗑 删除评论", command=self._delete_comment).pack(side=tk.LEFT, padx=8)

    # ---------- 后端 ----------
    def _start(self):
        if self._running:
            return
        self._running = True
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.status_lbl.config(text="启动中...", fg=COLORS["warning"])
        self.loop = asyncio.new_event_loop()

        def runner():
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(self.account_mgr.start(headless=False))
                self.loop.run_forever()
            except Exception as e:
                logger.error(f"后端异常: {e}")

        self._backend_thread = threading.Thread(target=runner, daemon=True)
        self._backend_thread.start()
        self.status_lbl.config(text="运行中", fg=COLORS["success"])

    def _stop(self):
        if not self._running:
            return
        self._running = False
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.account_mgr.stop(), self.loop)
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.status_lbl.config(text="已停止", fg=COLORS["danger"])

    def _fetch_now(self):
        if not self._running or not self.loop:
            messagebox.showwarning("提示", "请先启动")
            return
        asyncio.run_coroutine_threadsafe(self.account_mgr.fetch_all_once(), self.loop)
        self.status_lbl.config(text="抓取中...", fg=COLORS["warning"])

    def _save_settings(self):
        try:
            self.config["fetch_interval_sec"] = int(self.interval_var.get())
        except ValueError:
            messagebox.showwarning("提示", "间隔需为数字")
            return
        self._save_config()
        messagebox.showinfo("提示", "已保存")

    def _save_keywords(self):
        text = self.kw_text.get("1.0", tk.END).strip()
        rules = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                kw, rep = line.split("|", 1)
                rules.append({"keyword": kw.strip(), "reply": rep.strip()})
            else:
                rules.append({"keyword": line, "reply": ""})
        self.config.setdefault("auto_reply", {})["enabled"] = bool(self.auto_var.get())
        self.config["auto_reply"]["rules"] = rules
        self._save_config()
        for w in self.account_mgr.workers.values():
            w.auto_reply.auto_config = self.config["auto_reply"]
        messagebox.showinfo("提示", "关键字已保存")

    def _add_account(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("添加账号")
        dlg.geometry("480x480")
        fields = {}
        for key, label in [("id", "账号ID(英文)"), ("name", "名称"),
                           ("_aid", "_aid"), ("_log_finder_id", "_log_finder_id")]:
            tk.Label(dlg, text=label + ":").pack(anchor="w", padx=12, pady=(8, 0))
            e = ttk.Entry(dlg)
            e.pack(fill=tk.X, padx=12)
            fields[key] = e
        # 自动评论配置(新视频发布后自动发+置顶)
        tk.Label(dlg, text="自动评论内容(检测到新视频自动发+置顶):",
                 font=FONTS["body"]).pack(anchor="w", padx=12, pady=(12, 0))
        ac_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(dlg, text="启用自动评论", variable=ac_var).pack(anchor="w", padx=12)
        ac_entry = ttk.Entry(dlg)
        ac_entry.pack(fill=tk.X, padx=12)

        def save():
            acc_id = fields["id"].get().strip()
            if not acc_id:
                messagebox.showwarning("提示", "账号ID必填", parent=dlg)
                return
            acc = {
                "id": acc_id,
                "name": fields["name"].get().strip() or acc_id,
                "_aid": fields["_aid"].get().strip(),
                "_log_finder_id": fields["_log_finder_id"].get().strip(),
                "profile_dir": f"./profiles/{acc_id}",
                "auto_comment_enabled": bool(ac_var.get()),
                "auto_comment_content": ac_entry.get().strip(),
            }
            self.config["accounts"].append(acc)
            self._save_config()
            dlg.destroy()
            self._refresh_accounts()
            messagebox.showinfo("提示", "账号已添加,启动后扫码登录(首次)")

        ttk.Button(dlg, text="保存", command=save).pack(pady=12)

    def _del_account(self):
        sel = self.acct_tree.selection()
        if not sel:
            return
        idx = self.acct_tree.index(sel[0])
        if 0 <= idx < len(self.config["accounts"]):
            del self.config["accounts"][idx]
            self._save_config()
            self._refresh_accounts()

    def _refresh_accounts(self):
        if not hasattr(self, "acct_tree"):
            return
        self.acct_tree.delete(*self.acct_tree.get_children())
        for acc in self.config.get("accounts", []):
            w = self.account_mgr.workers.get(acc["id"])
            st = "在线" if (w and w.logged_in) else "未启动"
            self.acct_tree.insert("", "end", values=(acc.get("name", acc["id"]), st))

    def _manual_reply(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请选择一条评论")
            return
        content = self.reply_entry.get().strip()
        if not content:
            messagebox.showwarning("提示", "请输入回复内容")
            return
        tags = self.tree.item(sel[0])["tags"]
        if len(tags) < 2:
            messagebox.showwarning("提示", "评论信息缺失")
            return
        cid, acc_id = tags[0], tags[1]
        w = self.account_mgr.get_worker(acc_id)
        if not w or not w.logged_in:
            messagebox.showwarning("提示", "账号未启动")
            return

        async def do_reply():
            resp = await w.api.reply_comment(str(cid), content)
            if resp and not resp.get("__err"):
                self.storage.mark_replied(str(cid))
                self.reply_entry.delete(0, tk.END)
                logger.info(f"手动回复 {cid}: {content}")
            else:
                logger.error(f"手动回复失败: {resp}")

        asyncio.run_coroutine_threadsafe(do_reply(), self.loop)
        messagebox.showinfo("提示", "回复已发送")

    def _delete_comment(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请选择一条评论")
            return
        tags = self.tree.item(sel[0])["tags"]
        if len(tags) < 3:
            messagebox.showwarning("提示", "评论信息缺失")
            return
        cid, acc_id, exp = tags[0], tags[1], tags[2]
        vals = self.tree.item(sel[0])["values"]
        nick = vals[2] if len(vals) > 2 else ""
        content = vals[3] if len(vals) > 3 else ""
        if not messagebox.askyesno("确认删除", f"确定删除 {nick} 的评论?\n{content}"):
            return
        w = self.account_mgr.get_worker(acc_id)
        if not w or not w.logged_in:
            messagebox.showwarning("提示", "账号未启动")
            return

        async def do_del():
            resp = await w.api.delete_comment(str(exp), str(cid))
            if resp and not resp.get("__err"):
                logger.info(f"删除评论 {cid}")
                self.storage.delete_comment(str(cid))
            else:
                logger.error(f"删除失败: {resp}")

        asyncio.run_coroutine_threadsafe(do_del(), self.loop)
        messagebox.showinfo("提示", "删除已发送")

    def _export_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        rows = self.storage.recent_comments(limit=10000)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            wtr = csv.writer(f)
            wtr.writerow(["账号", "视频ID", "评论ID", "用户", "内容", "时间", "点赞", "已回"])
            for r in rows:
                t = datetime.fromtimestamp(r[6]).strftime("%Y-%m-%d %H:%M:%S") if r[6] else ""
                wtr.writerow([r[0], r[1], r[2], r[3], r[4], t, r[7], "是" if r[8] else "否"])
        messagebox.showinfo("提示", f"已导出 {path}")

    def _poll(self):
        if hasattr(self, "tree"):
            self.tree.delete(*self.tree.get_children())
            rows = self.storage.recent_comments(limit=500)
            for r in rows:
                acc, exp, cid, nick, content, head, ct, like, replied = r
                t = datetime.fromtimestamp(ct).strftime("%m-%d %H:%M") if ct else ""
                self.tree.insert("", "end",
                                 values=(acc, (exp or "")[:24], nick, (content or "")[:60], t, "是" if replied else ""),
                                 tags=(str(cid), str(acc), str(exp or "")))
            self._refresh_accounts()
        self.root.after(2000, self._poll)

    def run(self):
        self.root.mainloop()


def main():
    app = App()
    app.run()


if __name__ == "__main__":
    main()
