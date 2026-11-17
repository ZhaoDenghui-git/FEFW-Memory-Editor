# -*- coding: utf-8 -*-
"""
内存模式面板（嵌入主编辑器 editor.py 作为第 9 个页签「内存」）

为什么单独成模块：
  · editor.py 已经有 2400 行，内存模式需要一个完整的"单位列表 + 30 格明细 + 写入按钮"面板，
    独立成模块既不打散主编辑器结构，也方便单独维护。
  · 外观完全复用主编辑器的 ttk 命名样式（Card.TFrame / CardH3.TLabel / Accent.TButton…），
    所以在深色/浅色主题切换时会自动跟着变，不需要在这里写死任何颜色。

职责边界：
  · 这里只做"人机界面"，所有地址定位与读写都走 mem_editor（L0/L1/L2 分层定位 + 快照撤销）。
  · 面板不碰存档文件本身，写内存与存档保存是两条独立路径。
"""
from __future__ import annotations

import csv
import json
import os
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, simpledialog, ttk

import mem_editor as M


# --------------------------------------------------------------------------- 未收编单位的名字
EXTRA_PATH = os.path.join(M.PROJ, "mem_units_extra.json")


def _fp_key(stats) -> str:
    """把 9 个属性组压成记账的键。

    为什么用属性组而不是槽号：槽号每次开界面都会变（实测同一人 169→207→238），
    属性组是这个人自己的数值，换槽也不变，所以拿它当"这人叫什么"的主键。
    """
    return "|".join(str(int(v)) for v in stats)


def _load_extra() -> dict:
    """读未收编单位的名字表（游戏里看到的 NPC 名字，人工补进去的）。"""
    try:
        with open(EXTRA_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_extra(data: dict):
    """写回名字表；写失败不致命（只是这次起名没记住）。"""
    try:
        with open(EXTRA_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- 全表普查
def _unit_block(rec, bag) -> dict | None:
    """判断这对原始字节是不是一条"真单位记录"：像就返回字段，不像返回 None。

    过滤条件全部来自实测，目的是把"槽号窗口越界后读到的文本/指针"挡在外面：
      · 属性组 9 个数都在 0..100，且合计 > 0（真人不会全 0）
      · 当前 HP 在 0..90、等级在 1..80（HP 允许 0：倒下的单位就是 0）
      · 背包每格 b5..b7 必须为 0（真实格子恒 0，垃圾数据常非 0）
      · 物品编号 <= 3000（名字表 2909 行）
    只要有一格不符就整块丢弃 —— 宁可漏，不可把垃圾当成"未收编队友"报给你。
    """
    if not rec or len(rec) < M.REC_LV_OFF + 2 or not bag:
        return None
    stats = [int.from_bytes(rec[i * 2:i * 2 + 2], "little") for i in range(M.REC_FP_COUNT)]
    if any(v > M.FP_FIELD_HINT * 2 for v in stats) or sum(stats) == 0:
        return None
    hp = int.from_bytes(rec[M.REC_HP_OFF:M.REC_HP_OFF + 2], "little")
    lv = int.from_bytes(rec[M.REC_LV_OFF:M.REC_LV_OFF + 2], "little")
    if not (0 <= hp <= 90) or not (0 < lv <= 80):
        return None
    codes = []
    for i in range(M.CELLS):
        cell = bag[i * M.CELL_BYTES:i * M.CELL_BYTES + M.CELL_BYTES]
        if cell[5] or cell[6] or cell[7]:
            return None                    # b5..b7 必须恒 0
        code = cell[0] | (cell[1] << 8)
        if code > 3000:
            return None
        if code:
            codes.append(code)
    self_v = int.from_bytes(rec[M.REC_SELF_OFF:M.REC_SELF_OFF + 2], "little")
    return {"stats": stats, "hp": hp, "lv": lv, "codes": codes, "self": self_v}


# --------------------------------------------------------------------------- 物品目录
@dataclass
class ItemRow:
    idx: int
    iid: str
    jp: str
    en: str
    zh: str


def load_item_catalog() -> list[ItemRow]:
    rows: list[ItemRow] = []
    path = os.path.join(M.PROJ, "物品代码对照表.csv")
    with open(path, encoding="utf-8-sig") as fp:
        for r in csv.DictReader(fp):
            v = r["内部编号b0b1"].strip()
            if not v.isdigit():
                continue
            zh = (r["中文名"] or "").strip()
            jp = (r["日文名"] or "").strip()
            if not zh and not jp:
                continue
            rows.append(ItemRow(int(v), r["物品ID"].strip(), jp, (r["英文名"] or "").strip(), zh or jp))
    rows.sort(key=lambda x: x.idx)
    return rows


# --------------------------------------------------------------------------- 面板
class MemPanel(ttk.Frame):
    """内存模式面板：显示并修改游戏进程里的背包数据。"""

    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.pid: int | None = None
        self.h = None
        self.units: list[M.SaveUnit] = []
        self.ladders: list[M.Ladder] = []
        self.cells: list[list[tuple[int, int, int, int]]] = []
        self.snap: M.Snapshot | None = None
        self.catalog: list[ItemRow] = []
        self.busy = False
        self._first_show = True
        self.poll_targets: list = []
        self.log_q: queue.Queue[tuple[str, str]] = queue.Queue()
        self._prog_target: tuple[int, str] | None = None   # 扫描线程写、主线程读（只读这一对值）
        self.poll_stop = threading.Event()
        self.poll_thread: threading.Thread | None = None
        self._auto_job = None            # 自动刷新的 after 句柄
        self._units_path = ""            # 当前锚点用的是哪个存档文件
        self._units_mtime = 0.0          # 它的修改时间（用来发现"游戏刚存过档"）
        self._last_scan_try = 0.0        # 上一次"自动扫描"的时间（冷却用）
        self._miss_count = 0             # 缓存连续落空次数（够了就自己升级成扫描）
        self._pending_units = []         # 认不出内存块的单位（刚招募的角色），1 秒重试一次
        self._pending_names = []
        self._all_units = []             # 最近一次从存档读到的完整单位列表
        self._pending_tick = 0
        self._fp_tick = 0                # 记录头快照的节拍（60 秒一次）
        self._fp_last = ""               # 上一次快照内容，没变就不写文件

        self._build()
        self._pump_log()
        self._auto_refresh()             # 1 秒一次，游戏里换了道具界面才跟得上

    # ---------------------------------------------------------------- 构建界面
    def _build(self):
        top = ttk.Frame(self, style="Card.TFrame", padding=(12, 10))
        top.pack(side="top", fill="x")

        r1 = ttk.Frame(top, style="Card.TFrame")
        r1.pack(fill="x")
        ttk.Label(r1, text="内存模式", style="CardH3.TLabel").pack(side="left")
        ttk.Label(r1, text="直接读写游戏进程里的背包，改完无需读档",
                  style="CardFaint.TLabel").pack(side="left", padx=(10, 0))
        self.lbl_state = ttk.Label(r1, text="未连接", style="CardMuted.TLabel")
        self.lbl_state.pack(side="right")

        # 第二行：存档槽 + 三个动作按钮（单独一行，避免被上行的说明文字挤掉）
        r2 = ttk.Frame(top, style="Card.TFrame")
        r2.pack(fill="x", pady=(8, 0))
        ttk.Label(r2, text="当前已加载存档", style="CardMuted.TLabel").pack(side="left")
        self.cmb_save = ttk.Combobox(r2, state="readonly", width=26)
        self.cmb_save.pack(side="left", padx=(8, 8))
        self.cmb_save.bind("<<ComboboxSelected>>", lambda e: self.connect())
        self.btn_connect = ttk.Button(r2, text="连接游戏", style="Accent.TButton",
                                      command=self.connect)
        self.btn_connect.pack(side="left")
        ttk.Button(r2, text="重新定位", style="Small.TButton",
                   command=self._relocate).pack(side="left", padx=(8, 0))
        ttk.Button(r2, text="深度扫描", style="Small.TButton",
                   command=lambda: self.connect(force_scan=True)).pack(side="left", padx=(6, 0))
        ttk.Button(r2, text="锚点串(CE)…", style="Small.TButton",
                   command=self._export_anchors).pack(side="left", padx=(6, 0))
        ttk.Button(r2, text="槽位索引…", style="Small.TButton",
                   command=self._show_slot_index).pack(side="left", padx=(6, 0))
        ttk.Button(r2, text="全表普查…", style="Small.TButton",
                   command=self._show_all_units).pack(side="left", padx=(6, 0))
        ttk.Button(r2, text="校验地址", style="Small.TButton",
                   command=self.verify_addresses).pack(side="left", padx=(6, 0))
        ttk.Label(r2, text="下拉框要选游戏里真正在读的那一号存档，锚点按它的背包内容找",
                  style="CardFaint.TLabel").pack(side="left", padx=(12, 0))

        # 日志框先 pack 到窗口底部：先于可扩展的 body，否则它的高度会被 body 吃掉
        logbox = ttk.Frame(self, style="Card.TFrame", padding=(12, 8))
        logbox.pack(side="bottom", fill="x", pady=(8, 0))
        p = self.app.p
        # 进度条：只在扫描时出现（横向铺满、高 8px），平时 pack_forget 让日志贴到底
        self.prog_box = ttk.Frame(logbox, style="Card.TFrame")
        self.prog = ttk.Progressbar(self.prog_box, mode="determinate", maximum=1000)
        self.prog.pack(side="left", fill="x", expand=True)
        self.lbl_prog = ttk.Label(self.prog_box, text="", style="CardFaint.TLabel", width=18)
        self.lbl_prog.pack(side="left", padx=(8, 0))
        self.txt_log = tk.Text(logbox, height=4, borderwidth=0, highlightthickness=0,
                               bg=p["card"], fg=p["muted"], insertbackground=p["fg"],
                               font=(self.app.mono, 9))
        self.txt_log.pack(fill="x")
        self.txt_log.configure(state="disabled")

        # 左：角色列表；右：30 格明细
        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(side="top", fill="both", expand=True, pady=(10, 0))

        side = ttk.Frame(body, style="Card.TFrame", padding=12)
        body.add(side, weight=0)
        ttk.Label(side, text="角色", style="CardH3.TLabel").pack(anchor="w")
        # 按钮先 pack 到底部，再让 Treeview 占满剩余空间（顺序反了按钮会被压成 1px）
        sacts = ttk.Frame(side, style="Card.TFrame")
        sacts.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(sacts, text="修满耐久（本角色）", style="Small.TButton",
                   command=self.repair_unit).pack(fill="x")
        ttk.Button(sacts, text="修满耐久（全员）", style="Small.TButton",
                   command=self.repair_all).pack(fill="x", pady=(6, 0))
        self.tree_units = ttk.Treeview(side, columns=("cnt",), show="tree headings",
                                       selectmode="browse", height=14)
        self.tree_units.heading("#0", text="名字", anchor="w")
        self.tree_units.heading("cnt", text="件数", anchor="e")
        self.tree_units.column("#0", width=150, minwidth=110)
        self.tree_units.column("cnt", width=52, minwidth=44, anchor="e", stretch=False)
        self.tree_units.pack(fill="both", expand=True, pady=(8, 0))
        self.tree_units.bind("<<TreeviewSelect>>", lambda e: self._show_unit())

        main = ttk.Frame(body, style="Card.TFrame", padding=12)
        body.add(main, weight=1)
        head = ttk.Frame(main, style="Card.TFrame")
        head.pack(fill="x")
        self.lbl_unit = ttk.Label(head, text="请先连接游戏", style="CardH3.TLabel")
        self.lbl_unit.pack(side="left")
        self.lbl_addr = ttk.Label(head, text="", style="CardMono.TLabel")
        self.lbl_addr.pack(side="right")

        cols = ("idx", "name", "wear", "eq")
        # 操作行同样先 pack 到底部，Treeview 再吃剩余高度
        acts = ttk.Frame(main, style="Card.TFrame")
        acts.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Button(acts, text="替换物品…", style="Small.TButton",
                   command=self.replace_selected).pack(side="left")
        ttk.Button(acts, text="武器熟练度…", style="Small.TButton",
                   command=self.edit_masteries).pack(side="left", padx=(8, 0))
        ttk.Button(acts, text="撤销上一批修改", style="Small.TButton",
                   command=self.undo).pack(side="left", padx=(8, 0))
        ttk.Button(acts, text="仅刷新显示", style="Small.TButton",
                   command=self.refresh).pack(side="left", padx=(8, 0))
        # 自动修满默认开启并记进设置文件：用户希望"开着就一直在修"，不每次都去点
        self.var_auto = tk.BooleanVar(value=self._load_flag("mem_auto_repair", True))
        ttk.Checkbutton(acts, text="战斗中自动修满耐久", variable=self.var_auto,
                        command=self._toggle_poll).pack(side="left", padx=(18, 0))
        ttk.Label(acts, text="间隔(ms)", style="CardFaint.TLabel").pack(side="left", padx=(8, 4))
        self.var_iv = tk.IntVar(value=50)
        ttk.Spinbox(acts, from_=20, to=1000, increment=10, width=6,
                    textvariable=self.var_iv).pack(side="left")
        # 默认写全部校验过的副本（都是同一份数组的真副本）；勾上则只写第一条，
        # 最保守但要求"第一条就是游戏在读的那份"。
        # 默认只写主副本：第二条梯的"第 i 个槽还是不是同一个人"没法逐格证明，
        # 而实测改一份另一份会自动同步 —— 多写一份只增加写错地方的机会，不带来收益。
        self.var_single = tk.BooleanVar(value=True)
        ttk.Checkbutton(acts, text="只写主副本（保守）", variable=self.var_single
                        ).pack(side="left", padx=(18, 0))

        self.tree_cells = ttk.Treeview(main, columns=cols, show="headings", height=14)
        for c, t, w, a in (("idx", "格", 46, "center"), ("name", "物品", 320, "w"),
                           ("wear", "耐久", 100, "center"), ("eq", "状态", 96, "center")):
            self.tree_cells.heading(c, text=t)
            self.tree_cells.column(c, width=w, anchor=a, stretch=(c == "name"))
        self.tree_cells.pack(fill="both", expand=True, pady=(8, 0))
        self.tree_cells.bind("<Double-1>", lambda e: self.replace_selected())

    # ---------------------------------------------------------------- 日志
    def log(self, msg: str, level: str = "info"):
        self.log_q.put((level, msg))

    def _pump_log(self):
        try:
            while True:
                level, msg = self.log_q.get_nowait()
                mark = {"info": "", "ok": " ✔", "warn": " ⚠", "err": " ✘"}.get(level, "")
                self.txt_log.configure(state="normal")
                self.txt_log.insert("end", f"{time.strftime('%H:%M:%S')}{mark}  {msg}\n")
                self.txt_log.see("end")
                self.txt_log.configure(state="disabled")
                if level == "err":
                    self.app.flash(msg, "error")
                elif level == "ok":
                    self.app.flash(msg, "ok")
                elif level == "warn":
                    self.app.flash(msg, "warn")
                else:
                    self.app.flash(msg, "info")
        except queue.Empty:
            pass
        self._pump_prog()
        self.after(120, self._pump_log)

    # ---------------------------------------------------------------- 扫描进度条
    def _prog_begin(self):
        """扫描开始：把进度条挂到日志框上方。"""
        self._prog_target = (0, "0.0 GB")
        self.prog_box.pack(side="top", fill="x", pady=(0, 6), before=self.txt_log)
        self.prog["value"] = 0
        self.lbl_prog.configure(text="扫描中…")

    def _prog_end(self):
        """扫描结束/失败：收起进度条，日志重新贴到底。"""
        self._prog_target = None
        try:
            self.prog_box.pack_forget()
        except Exception:
            pass

    def _prog_cb(self, done: int, total: int):
        """扫描线程回调：只存两个值，界面更新交给主线程的 _pump_log。"""
        pct = int(done * 1000 / max(1, total))
        self._prog_target = (pct, f"{done / 1e9:.1f}/{total / 1e9:.1f} GB")

    def _pump_prog(self):
        t = self._prog_target
        if not t:
            return
        if self.prog["value"] != t[0]:
            self.prog["value"] = t[0]
            self.lbl_prog.configure(text=f"{t[1]}  {t[0] / 10:.0f}%")

    def set_state(self, text: str, kind: str = "muted"):
        style = {"muted": "CardMuted.TLabel", "ok": "CardOk.TLabel",
                 "warn": "CardWarn.TLabel"}[kind]
        self.lbl_state.configure(text=text, style=style)

    # ---------------------------------------------------------------- 初始化
    def _load_catalog(self):
        """懒加载（第一次连接时才读表），避免拖慢主编辑器启动。"""
        if self.catalog:
            return
        try:
            M.load_code_table()             # 填充 物品ID ↔ 内部编号 ↔ 名字 反查表（必须）
            self.catalog = load_item_catalog()
        except Exception as e:
            self.catalog = []
            self.log(f"物品目录载入失败：{e}", "err")

    def _slot_paths(self) -> list[str]:
        """可选存档槽：优先用主编辑器已经扫描出来的槽，否则扫存档目录。"""
        paths = [p for p in getattr(self.app, "slot_paths", []) if os.path.isfile(p)]
        if paths:
            return paths
        folder = getattr(self.app, "folder", None)
        if not folder or not os.path.isdir(folder):
            try:
                import fefw_layer
                folder = fefw_layer.default_save_dir()
            except Exception:
                return []
        try:
            names = [n for n in sorted(os.listdir(folder))
                     if n.startswith(("Auto", "Manual")) and "before" not in n and "." not in n]
            return [os.path.join(folder, n) for n in names if os.path.isfile(os.path.join(folder, n))]
        except OSError:
            return []

    def auto_connect(self):
        """第一次切到本页签时自动连一次，省一步点击。"""
        if self._first_show:
            self._first_show = False
            self.connect()

    # ---------------------------------------------------------------- 连接与定位
    def connect(self, force_scan: bool = False, quiet: bool = False):
        if self.busy:
            return
        self.busy = True
        slots = self._slot_paths()
        self.cmb_save["values"] = [os.path.basename(p) for p in slots]
        if slots and not quiet:
            self.log("可选存档槽：" + "、".join(os.path.basename(p) for p in slots))
        if slots and not self.cmb_save.get():
            cur = getattr(self.app, "sv", None)
            cur_path = getattr(cur, "path", None)
            self.cmb_save.set(os.path.basename(cur_path) if cur_path in slots else os.path.basename(slots[0]))
        path = self._current_slot(slots)
        if not path:
            self.busy = False
            self.log("找不到存档槽文件", "err")
            return
        self.set_state("正在连接…", "warn")
        threading.Thread(target=self._connect_worker, args=(path, force_scan, quiet),
                         daemon=True).start()

    def _current_slot(self, slots: list[str]) -> str:
        name = self.cmb_save.get().strip()
        for p in slots:
            if os.path.basename(p) == name:
                return p
        return slots[0] if slots else ""

    def _connect_worker(self, path: str, force_scan: bool = False, quiet: bool = False):
        def say(msg: str, level: str = "info"):
            if not quiet:                   # 自动重试时不刷屏
                self.log(msg, level)
        try:
            if not self.catalog:
                self._load_catalog()        # 懒加载物品表
            self.units = M.load_save_units_stable(path)
            if not self.units:
                self.units = M.load_save_units_stable(path, require_all_known=False)
                if self.units:
                    say("存档里有物品编号不在对照表内，锚点只用已知编号", "warn")
            if not self.units:
                say("这个存档读不出物品（可能全是不在对照表内的编号）", "err")
                self.set_state("无可用单位", "warn")
                return
            say(f"存档 {os.path.basename(path)}：{len(self.units)} 个单位 —— " +
                "、".join(u.name for u in self.units))
            # 记下"这份锚点"的来历与时间：_watch_once 靠它判断游戏有没有写出更新的存档
            self._units_path = path
            try:
                self._units_mtime = os.path.getmtime(path)
            except OSError:
                self._units_mtime = 0.0

            pid = M.find_pid()
            if not pid:
                say("找不到 fefw.exe，请确认游戏正在运行", "err")
                self.set_state("未找到游戏进程", "warn")
                return
            if self.pid != pid or not self.h:      # 换了进程或还没开过句柄才重开，避免每 5 秒泄漏一个句柄
                if self.h:
                    try:
                        M.k32.CloseHandle(self.h)
                    except Exception:
                        pass
                self.pid = pid
                self.h = M.k32.OpenProcess(0x0400 | 0x0010 | 0x0020 | 0x0008, False, pid)
            if not self.h:
                say(f"OpenProcess 失败，错误码 {M.ctypes.get_last_error()}"
                    "（游戏以管理员运行时本程序也要以管理员运行）", "err")
                self.set_state("打开进程失败", "warn")
                return
            say(f"已连接 PID {pid}")

            t0 = time.perf_counter()
            ladders, how = M.try_cache(self.h, self.units)
            if ladders:
                self.ladders = ladders
                # 命中后立刻把"此刻的真实编号"写回缓存：你在游戏里用掉一本技能书、捡到一件
                # 东西，内存就和存档/旧快照不一样了，刷新一次才不会让下一轮校验落在旧内容上
                # （判据本身也带了容差，见 mem_editor 的 SEQ_TOLERANCE / _seq_distance）。
                try:
                    M.save_cache(ladders, self.h, self.units)
                except Exception:
                    pass
                # how 里已经带了条数（"L0 缓存直接命中：1 条地址梯…"），这里只补耗时，
                # 以前再拼一次"N 条地址梯"就会把条数打印两遍。
                say(f"{how}，{len(ladders)} 条可用，"
                    f"{(time.perf_counter()-t0)*1000:.0f} ms", "ok")
                self.set_state("已定位", "ok")
            elif not force_scan:
                # 没命中且用户没点「全量扫描」：不擅自跑 4 分钟的全量扫描。
                # 最常见的原因就是游戏还停在标题画面 / 读取的存档与所选存档槽不一致。
                self.ladders = []
                say(f"{how}：内存里暂时找不到这份背包。若游戏已读档，"
                    "先确认存档槽选对，再点「重新定位」（缓存+单锚点扫描，几秒）；"
                    "或者直接在游戏里存一次档，我每 5 秒会自动改用最新存档当锚点重试", "warn")
                self.set_state("未定位（请先读档）", "warn")
                self.after(0, self._refresh_all)
                self.after(300, self._maybe_autostart)
                self.after(5000, self._watch_once)      # 读档后自动重试（只走 L0/L1，不扫内存）
                return
            else:
                self.set_state("定位中，请等待…", "warn")
                say(f"{how} → 单锚点定位（只扫一个单位，命中后用 {M.STRIDE:#x} 步长反推）", "warn")
                self.after(0, self._prog_begin)
                try:
                    # 五档递进：范围由小到大，判据由严到宽。
                    # 小区域 = ≤256 MB 的映射区（背包数组实测就住在这类块里，几秒出结果）；
                    # 容差先 3（只少/多几件），不行再 10（多人来回换道具），
                    # 最后才放开扫全部映射区、再退到私有堆。
                    # 前两档带 hotspot：上次命中地址附近 ±256 MB 先扫一遍（约 0.2 s），
                    # 命中就直接定案；没命中由 locate_by_anchor 自己退回全量，不影响结果。
                    hot = M.load_hotspot()
                    tiers = [
                        (0x40000, 256 << 20, M.SEQ_TOLERANCE, "", hot),
                        (0x40000, 256 << 20, M.SEQ_TOLERANCE_MAX, "小区域容差 3 未命中 → 放宽到 10", hot),
                        (0x40000, None, M.SEQ_TOLERANCE_MAX, "小区域仍未命中 → 扩大到全部映射区", None),
                        (0x20000, None, M.SEQ_TOLERANCE_MAX, "映射区候选都不通过 → 再扫私有堆", None),
                    ]
                    ladders, cand = [], 0
                    spot_used = False
                    for only_type, size_limit, tol, msg, spot in tiers:
                        if msg:
                            say(msg, "warn")
                        ladders, cand = M.locate_by_anchor(
                            self.h, self.units, only_type=only_type,
                            progress=self._prog_cb, size_limit=size_limit, tolerance=tol,
                            hotspot=spot)
                        if ladders:
                            spot_used = bool(spot)
                            break
                finally:
                    self.after(0, self._prog_end)
                self.ladders = ladders
                if not ladders:
                    say("定位失败：内存里找不到与存档一致的物品串。"
                        "最省事的办法 —— 在游戏里存一次档（哪个槽都行），"
                        "我每 5 秒会自动改用最新存档当锚点重试定位", "err")
                    self.set_state("定位失败（请在游戏里存一次档）", "warn")
                    self.after(5000, self._watch_once)
                    return
                M.save_cache(ladders, self.h, self.units)
                dt = time.perf_counter() - t0
                tip = "（热点窗口命中，没扫全内存）" if (spot_used and dt < 8) else ""
                say(f"定位成功：{len(ladders)} 条梯（候选 {cand} 个），"
                    f"耗时 {dt:.1f} s{tip}", "ok")
                self.set_state("已定位", "ok")
            self.after(0, self._refresh_all)
            self.after(300, self._maybe_autostart)
        except Exception as e:
            self.log(f"连接异常：{e}", "err")
            self.set_state("连接失败", "warn")
        finally:
            self.busy = False

    def _page_active(self) -> bool:
        try:
            return self.app.pages.select() == str(self.app.mem_page)
        except Exception:
            return False

    def _watch_once(self):
        """停在「未定位」时每 5 秒静默重试一次 L0/L1；定位成功或用户离开本页就停。

        顺手干一件更有用的事：**盯存档文件的修改时间**。
        游戏每次存档写出来的东西就是"这一刻的真实背包"，比任何旧快照都可信。
        所以只要发现有比当前用的存档更新的槽，就切换过去拿它当锚点重试 ——
        用户什么都不用做，在游戏里按一下存档，这里下一次轮询就自己跟上了。
        """
        if self.ladders or self.busy:
            return
        if not self._page_active():
            return
        if self._adopt_newest_save():
            # 拿到了游戏刚写出的"事实背包"：这时候值得直接开扫描，而不是只试缓存。
            # 冷却 30 秒，避免自动存档每场战斗都触发一次全量扫描。
            now = time.time()
            if now - self._last_scan_try < 30:
                return
            self._last_scan_try = now
            self.connect(force_scan=True, quiet=True)
            return
        # 缓存连续落空 3 次（约 15 秒）→ 自己升一次级去扫描。
        # 冷却 60 秒：扫描一次几十秒，不能每 5 秒就重来。这条是"地址被搬走但游戏还没
        # 写新存档"时的兜底，否则会一直停在"未定位"，修满也续不上。
        self._miss_count += 1
        now = time.time()
        if self._miss_count >= 3 and now - self._last_scan_try >= 60:
            self._last_scan_try = now
            self._miss_count = 0
            self.log("缓存连续 3 次没命中 → 自动做一次单锚点扫描", "warn")
            self.connect(force_scan=True, quiet=True)
            return
        self.connect(force_scan=False, quiet=True)

    def _adopt_newest_save(self) -> bool:
        """若存在比"当前锚点存档"更新的槽，就把下拉框切过去。返回是否切换了。"""
        slots = self._slot_paths()
        if not slots:
            return False
        try:
            newest = max(slots, key=lambda p: os.path.getmtime(p))
            mt = os.path.getmtime(newest)
        except OSError:
            return False
        # 0.5 秒容差：文件系统时间精度不一，同一次写入可能只差几十毫秒
        if mt <= (self._units_mtime or 0) + 0.5:
            return False
        if os.path.basename(newest) == os.path.basename(self._units_path or ""):
            return False
        self.cmb_save.set(os.path.basename(newest))
        self.log(f"发现游戏刚存过档：{os.path.basename(newest)}"
                 f"（{time.strftime('%H:%M:%S', time.localtime(mt))}）"
                 f"→ 改用它当锚点重试定位", "ok")
        return True

    def _relocate(self):
        """重新定位：缓存 → 单锚点扫描（都在这一条路上，用户不用关心走的是哪层）。

        以前这里只走缓存，重启游戏后必然停在“未定位”，还得再点一次全量扫描。
        现在 force_scan=True 的含义就是“缓存没中就继续找”，而找的动作已经很快
        （小区域约 3 秒、全部映射区约 13 秒），所以一个按钮就够。
        """
        self.log("重新定位（缓存 L0/L1 → 单锚点扫描）")
        self.connect(force_scan=True)

    # ---------------------------------------------------------------- 校验/预览
    def verify_addresses(self):
        """只读核对：把每条梯的 base0 和逐单位比对结果打到日志里。

        用途是让你能拿这些地址去 CE 里亲自对照（格 1 是不是游戏里那件武器），
        写之前先把"地址对不对"这件事用肉眼钉死。
        """
        if not self.ladders or not self.h:
            self.log("尚未连接游戏", "warn")
            return
        out_path = os.path.join(M.PROJ, "mem_verify.txt")
        lines = [f"# 校验时间 {time.strftime('%Y-%m-%d %H:%M:%S')}  PID {self.pid}",
                 f"# 存档槽 {os.path.basename(self._current_slot(self._slot_paths()) or '')}"
                 f"（{len(self.units)} 单位）", ""]
        for n, l in enumerate(self.ladders):
            lines.append(f"[梯{n}] base0 = {l.base0:#x}   单位步长 {M.STRIDE:#x}   "
                         f"末单位 = {(l.addr(len(self.units) - 1)):#x}")
            lines += M.ladder_report(self.h, l, self.units)
            lines.append("")
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except OSError as e:
            self.log(f"写文件失败：{e}", "err")
        ok = [l for l in self.ladders if M.ladder_alive(self.h, l, self.units)]
        self.log(f"校验完成：{len(ok)}/{len(self.ladders)} 条梯逐格吻合 → {out_path}",
                 "ok" if ok else "err")
        for l in self.ladders:
            self.log(f"梯 {l.base0:#x}：" + ("吻合" if l in ok else "不吻合"), "ok" if l in ok else "err")
    def _export_anchors(self):
        """把当前存档槽的每个单位各导出一行内存搜索串，供 CE 手动搜。"""
        slots = self._slot_paths()
        path = self._current_slot(slots)
        if not path:
            self.log("没有选中存档槽", "err")
            return
        if not self.catalog:
            self._load_catalog()
        units = M.load_save_units(path, require_all_known=False)
        if not units:
            self.log("这个槽读不出单位", "err")
            return
        out_path = os.path.join(M.PROJ, "mem_anchors.txt")
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(M.anchors_text(units, path))
        except OSError as e:
            self.log(f"写文件失败：{e}", "err")
            return
        # 第一个单位的紧版串直接放剪贴板，省得再打开文件复制
        first = M.unit_pattern(units[0], tight=True)
        try:
            self.clipboard_clear()
            self.clipboard_append(first)
        except Exception:
            pass
        self.log(f"已导出 {len(units)} 条锚点串 → {out_path}（第一条已复制到剪贴板）", "ok")
        self.log(f"{units[0].name} 前 8 格：{' '.join(first.split()[:64])} …")

    def _show_slot_index(self):
        """把整张单位表（96 槽）枚举出来，用存档目录里的**所有**槽文件当名册贴名字。

        这件事不需要把谁招进队：只要某个角色在任何一份存档里出现过，就能凭背包内容
        认到槽号。认不出的（背包 0 件 = 240 B 全零块）只能显示"（未覆盖）"。
        """
        slots = self._slot_paths()
        if not slots:
            self.log("没找到存档文件，先选一个存档目录", "err")
            return
        base0 = self.ladders[0].base0 if self.ladders else None
        if base0 is None:
            self.log("先连接游戏/定位一次，再开槽位索引（需要 base0 才能按槽读）", "warn")
            return
        roster = M.build_roster(slots)
        table = M.identify_slots(self.h, base0, roster)
        # 1) 写一份文本，方便你贴到别处 / 给 CE 对照
        out_path = os.path.join(M.PROJ, "mem_slots.txt")
        lines = [f"# 槽位索引 {time.strftime('%Y-%m-%d %H:%M:%S')}  PID {self.pid}",
                 f"# base0 = {base0:#x}  步长 {M.STRIDE:#x}  名册 {len(roster)} 角色", ""]
        for k, v in sorted(table.items()):
            n = sum(1 for x in v["ids"] if x)
            a = base0 + k * M.STRIDE
            lines.append(f"槽{k:3}  {a:#x}  {n:2} 件  角色={v['name'] or '（未覆盖）'}"
                         f"  编号={[x for x in v['ids'] if x]}")
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except OSError as e:
            self.log(f"写文件失败：{e}", "err")
        # 2) 弹一个只读表格，不用去翻日志
        win = tk.Toplevel(self.app)
        win.title("槽位索引（单位表 96 槽）")
        win.geometry("760x520")
        win.configure(bg=self.app.p["card"])
        cols = ("slot", "addr", "n", "name", "ids")
        tv = ttk.Treeview(win, columns=cols, show="headings")
        for c, w, t in (("slot", 60, "槽"), ("addr", 120, "地址"),
                        ("n", 60, "件数"), ("name", 120, "角色"), ("ids", 380, "物品编号")):
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w")
        for k, v in sorted(table.items()):
            tv.insert("", "end", values=(k, hex(base0 + k * M.STRIDE),
                                         sum(1 for x in v["ids"] if x),
                                         v["name"] or "（未覆盖）",
                                         " ".join(str(x) for x in v["ids"] if x)))
        tv.pack(fill="both", expand=True, padx=8, pady=8)
        named = sum(1 for v in table.values() if v["name"])
        self.log(f"槽位索引：{len(table)} 个非空槽，认出 {named} 个角色 → {out_path}", "ok")

    def _show_all_units(self):
        """全表普查（**纯只读**）：列出 RAM 里所有"像单位记录"的块，标出已知角色。

        为什么需要这个按钮：未收编的队友 / 城内 NPC **不是常驻内存**的 ——
        游戏只在"这一屏要用到他们"时把他们装进一个临时单位池
        （打开地图、请吃饭、出击准备、进战斗都会触发），界面一关就整批释放。
        实测证据：同一批 38 条块在 45 秒内全部消失；换到地图界面后，
        另一批 31 条出现在完全不同的槽段（207–237）。
        所以想抓到"此刻有哪些人"，**得在那个界面开着的时候点这个按钮**。

        不写任何内存，只读 512 槽 × (32 B 记录头 + 240 B 背包)。
        """
        if not (self.h and self.ladders):
            self.log("先连接游戏/定位一次，再全表普查（需要 base0 才能按槽读）", "warn")
            return
        base0 = self.ladders[0].base0
        known = {tuple(u.stats or ()): u.name for u in self.units}
        extras = _load_extra()          # 之前手动起过名字的未收编单位（按属性组记账）
        rows, bad_self = [], 0
        for slot in range(M.MAX_SLOTS):
            addr = base0 + slot * M.STRIDE
            info = _unit_block(M.read_bytes(self.h, addr + M.REC_SHIFT, M.REC_LV_OFF + 2),
                               M.read_bytes(self.h, addr, M.CELLS * M.CELL_BYTES))
            if not info:
                continue
            info["slot"] = slot
            info["addr"] = addr
            info["name"] = known.get(tuple(info["stats"]), "")
            if info["self"] != slot + 1:
                bad_self += 1                  # 自身槽号对不上 → 记录可能已搬走
            rows.append(info)

        # 1) 落一份文本，方便你贴到别处 / 存档留证
        out_path = os.path.join(M.PROJ, "mem_all_units.txt")
        lines = [f"# 全表普查 {time.strftime('%Y-%m-%d %H:%M:%S')}  PID {self.pid}",
                 f"# base0 = {base0:#x}  步长 {M.STRIDE:#x}  已知 {len(self.units)} 人"
                 f"  命中 {len(rows)} 条块", ""]
        lines.append(f"{'槽':>4}  {'地址':<12}{'角色':<9}{'属性组':<38}{'HP':>3}{'LV':>3}  {'自槽号':<7}背包")
        for r in rows:
            items = "、".join(M.name_of(c) for c in r["codes"]) or "（空）"
            mark = "✔" if r["self"] == r["slot"] + 1 else "✘"
            nm = r["name"] or (extras.get(_fp_key(r["stats"])) or {}).get("name", "")
            lines.append(f"{r['slot']:>4}  {r['addr']:#012x}{'  '}"
                         f"{(nm or '（未收编）'):<9}{str(r['stats']):<38}"
                         f"{r['hp']:>3}{r['lv']:>3}  {mark}{r['self']:<6}{items}")
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            self.log(f"写文件失败：{e}", "err")

        # 2) 弹一个只读表格（关掉即散，不改任何东西；双击可以给未收编的起名字）
        win = tk.Toplevel(self.app)
        win.configure(bg=self.app.p["card"])
        cols = ("slot", "name", "stats", "hp", "lv", "self", "items")
        tv = ttk.Treeview(win, columns=cols, show="headings")
        for c, w, t in (("slot", 56, "槽"), ("name", 110, "角色"),
                        ("stats", 250, "属性组"), ("hp", 52, "HP"),
                        ("lv", 52, "LV"), ("self", 64, "自槽号"), ("items", 520, "背包")):
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w")
        bar = ttk.Frame(win, style="Card.TFrame", padding=(8, 4))
        bar.pack(side="bottom", fill="x")
        lbl = ttk.Label(bar, text="", style="CardFaint.TLabel")
        lbl.pack(side="left")

        def _render():
            """按"当前已知名字"重画表格（起名后要能立刻看到）。"""
            tv.delete(*tv.get_children())
            named_cnt = 0
            for r in rows:
                nm = r["name"] or (extras.get(_fp_key(r["stats"])) or {}).get("name", "")
                if nm:
                    named_cnt += 1
                tv.insert("", "end", values=(
                    r["slot"], nm or "（未收编）",
                    " ".join(str(v) for v in r["stats"]), r["hp"], r["lv"],
                    ("✔ " if r["self"] == r["slot"] + 1 else "✘ ") + str(r["self"]),
                    "、".join(M.name_of(c) for c in r["codes"]) or "（空）"))
            win.title(f"全表普查 — {len(rows)} 条块 / 已认 {named_cnt} 条 / "
                      f"存档已知 {len(self.units)} 人")
            lbl.configure(text="双击任意一行可以给它起名字（会记进 mem_units_extra.json，下次还在）")

        def _rename(event):
            """双击一行 → 输入名字 → 写进 mem_units_extra.json（按属性组记账，跟槽号无关）。"""
            row_id = tv.identify_row(event.y)
            if not row_id:
                return
            slot = int(tv.item(row_id, "values")[0])
            r = next(x for x in rows if x["slot"] == slot)
            key = _fp_key(r["stats"])
            cur = (extras.get(key) or {}).get("name", "")
            name = simpledialog.askstring(
                "给这个单位起名字",
                f"槽 {slot}  属性组 {r['stats']}  HP{r['hp']} LV{r['lv']}\n"
                f"背包：{'、'.join(M.name_of(c) for c in r['codes']) or '（空）'}\n\n"
                f"游戏里显示的名字：", initialvalue=cur, parent=win)
            if not name:
                return
            extras[key] = {"name": name.strip(), "hp": r["hp"], "lv": r["lv"],
                           "codes": list(r["codes"])}
            _save_extra(extras)
            _render()
            self.log(f"已记住：{name}（属性组 {r['stats']}）→ mem_units_extra.json", "ok")

        tv.bind("<Double-1>", _rename)
        tv.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        _render()
        unknown = len(rows) - sum(1 for r in rows
                                  if r["name"] or (extras.get(_fp_key(r["stats"])) or {}).get("name"))
        self.log(f"全表普查：命中 {len(rows)} 条块，其中 {unknown} 条未认名"
                 f"{'，' + str(bad_self) + ' 条自槽号对不上' if bad_self else ''} → {out_path}", "ok")

    # ---------------------------------------------------------------- 显示
    def _refresh_all(self):
        if not self.ladders:
            return
        self.cells = []
        for u in self.units:
            self.cells.append(M.read_unit(self.h, self.ladders[0].addr(u.index)) or [])
        cur = self._unit_index()
        self.tree_units.delete(*self.tree_units.get_children())
        for i, u in enumerate(self.units):
            n = sum(1 for c in self.cells[i] if c[0])
            self.tree_units.insert("", "end", iid=str(i), text=f"{u.name}", values=(f"{n} 件",))
        if self.units:
            idx = cur if cur is not None and cur < len(self.units) else 0
            self.tree_units.selection_set(str(idx))
            self.tree_units.focus(str(idx))
            self._render_unit(idx)

    def refresh(self):
        if self.ladders and self.h and not self.busy:
            self._refresh_all()
            self.log("已刷新显示", "ok")

    def _reload_units_if_save_changed(self) -> bool:
        """定位**已经成功**时，存档一更新就重读单位列表（换出战、退出战斗、招募）。

        为什么必须做：内存单位表是按角色固定序号排的，**换出战不会挪动任何块**，
        所以地址梯一直有效；但界面上的"角色列表"是从存档读的 —— 队伍变了而列表不重读，
        就会一直显示旧的那 6 个人。这里只重读存档 + 重新按角色 id 认槽，不重扫内存。
        """
        path = self._units_path or self._current_slot(self._slot_paths())
        if not path or not os.path.exists(path):
            return False
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return False
        if mt <= (self._units_mtime or 0) + 0.5:
            return False
        try:
            units = M.load_save_units_stable(path)
        except Exception:
            return False
        if not units or len(units) == len(self.units) and [u.person for u in units] == [u.person for u in self.units]:
            self._units_mtime = mt          # 内容没变（只是 mtime 变了），记下时间就走
            return False
        old_names = [u.name for u in self.units]
        self.units = units
        self._units_mtime = mt
        self._units_path = path
        # 按角色 id 从缓存里取槽位；缓存里没有的（新面孔）现场按内容认一次
        self._all_units = list(units)
        pending = self._apply_units(units)
        self._pending_names = pending
        if pending:
            self.log(f"存档变成 {len(units)} 个单位，其中 {'、'.join(pending)} 在内存表里"
                     "暂时找不到块（刚招募的角色要等游戏把他载进来），"
                     f"其余 {len(self.units)} 个照旧修满；1 秒后自动重试认槽", "warn")
        added = [u.name for u in units if u.name not in old_names]
        gone = [n for n in old_names if n not in [u.name for u in units]]
        tip = []
        if added:
            tip.append("新增 " + "、".join(added))
        if gone:
            tip.append("移出 " + "、".join(gone))
        self.log(f"存档已更新：{len(units)} 个单位"
                 + (f"（{'；'.join(tip)}）" if tip else "")
                 + "，地址梯仍然有效（换出战不挪动任何块）", "ok")
        return True

    def _auto_refresh(self):
        """每 1 秒静默重读 9 个单位 × 30 格（2160 字节），内容变了才重画。

        为什么必须自动刷：游戏自己会改背包 —— 两个角色交换道具、用掉一本书、捡到东西。
        以前只在"连接成功/写入成功"时读一次，所以交换完界面还是老样子，看着像"修改器允许但不刷新"。
        这里只在数值真的变化时重画，避免每秒把用户的选中项和滚动位置打断。
        """
        self._auto_job = None
        try:
            if self.h and self.ladders and not self.busy:
                if self._reload_units_if_save_changed():
                    return              # 单位列表刚换过，_refresh_all 已经画好了
                # 每 60 秒记一次单位记录头（属性组/最大HP/等级）到 mem_fp_history.jsonl。
                # 作用：玩家升级、转职、换装之后，直接 diff 这个文件就知道
                # "哪些字段会变、哪些不会变"，指纹的容差该给多宽就有数据依据了。
                self._fp_tick += 1
                if self._fp_tick % 60 == 0:
                    self._snapshot_fingerprints()
                # 有"还没在内存里出现"的角色（刚招募）→ 每 5 秒再认一次，
                # 游戏把他载入后立刻纳入修满，不用等下一次存档更新
                if self._pending_units:
                    self._pending_tick += 1
                    if self._pending_tick % 5 == 0:
                        left = self._apply_units(self._all_units or self.units)
                        if not left:
                            self.log("新队友已进入内存表，已自动纳入修满"
                                     "（槽位按背包内容认得）", "ok")
                new = []
                for u in self.units:
                    new.append(M.read_unit(self.h, self.ladders[0].addr(u.index)) or [])
                if new and new != self.cells:
                    first = not self.cells
                    # 只有"物品编号"变了才算真的换了道具；b2（已消耗耐久）会被
                    # 「战斗中自动修满」反复改写，那种变化不刷日志，免得日志刷屏。
                    ids_new = [[c[0] for c in row] for row in new]
                    ids_old = [[c[0] for c in row] for row in self.cells]
                    self.cells = new
                    if first:
                        self._refresh_all()
                    else:
                        cur = self._unit_index()
                        for i, u in enumerate(self.units):
                            n = sum(1 for c in new[i] if c[0])
                            if self.tree_units.exists(str(i)):
                                self.tree_units.item(str(i), values=(f"{n} 件",))
                        if cur is not None and cur < len(self.units):
                            self._render_unit(cur)
                        if ids_new != ids_old:
                            self.log("检测到内存变化（换/用了道具），已刷新显示", "ok")
        except Exception:
            pass
        finally:
            self._auto_job = self.after(1000, self._auto_refresh)

    def _unit_index(self) -> int | None:
        sel = self.tree_units.selection()
        return int(sel[0]) if sel else None

    def _show_unit(self):
        i = self._unit_index()
        if i is None or not self.ladders:
            return
        self._render_unit(i)

    def _render_unit(self, i: int):
        self.tree_cells.delete(*self.tree_cells.get_children())
        cells = self.cells[i] if i < len(self.cells) else []
        used = worn = 0
        for k in range(30):
            idx, b2, b3, b4 = cells[k] if k < len(cells) else (0, 0, 0, 0)
            if idx:
                self.tree_cells.insert("", "end", iid=str(k),
                                       values=(k + 1, M.name_of(idx),
                                               f"已消耗 {b2}" if b2 else "全新",
                                               "★ 装备中" if b3 else "未装备"))
                used += 1
                worn += 1 if b2 else 0
            else:
                self.tree_cells.insert("", "end", iid=str(k),
                                       values=(k + 1, "（空格）", "—", "—"))
        u = self.units[i]
        self.lbl_unit.configure(text=f"{u.name}    共 {used} 件    有耐久消耗 {worn} 件")
        self.lbl_addr.configure(
            text=("梯0 " + hex(self.ladders[0].addr(u.index)) +
                  ("    梯1 " + hex(self.ladders[1].addr(u.index)) if len(self.ladders) > 1 else "")))

    # ---------------------------------------------------------------- 写入操作
    def _ladders_ok(self) -> bool:
        """写之前再验一遍：地址梯必须"每个单位逐格编号都对得上"才允许动内存。

        这条是写崩过一次之后加的：以前只要结构像就认，结果往别人的内存写 b2，游戏闪退。
        校验失败就丢掉地址、回到"未定位"，让 5 秒自动重试去找正确的梯，绝不带着可疑地址写。
        """
        if not self.ladders or not self.h:
            self.log("尚未连接游戏", "warn")
            return False
        try:
            # 属性组优先、背包兜底：只要人还是那几个人，地址就算有效
            good = [l for l in self.ladders if M.ladder_alive(self.h, l, self.units)]
        except Exception as e:
            self.log(f"地址校验异常：{e}", "err")
            return False
        if not good:
            self.ladders = []
            self.set_state("地址已失效（请重新定位）", "warn")
            self.log("地址校验失败：内存里的背包和存档对不上，已拒绝写入。"
                     "请确认存档槽，再点「重新定位」或「全量扫描」", "err")
            self.after(3000, self._watch_once)
            return False
        self.ladders = good
        return True

    def _targets(self) -> list:
        """要写哪几条梯。

        默认**全部写**：能进 self.ladders 的梯都经过"逐单位逐格"校验（编号与存档一致，
        或与缓存里的编号快照一致），它们都是同一份背包的真副本 —— 只写其中一条的话，
        万一那条恰好是游戏不读的旧副本，就会出现"改了没反应"。多写一两次字节没有风险。
        勾上「只写主副本」则退回最保守的单副本写入。
        """
        if not self.ladders:
            return []
        return [self.ladders[0]] if self.var_single.get() else list(self.ladders)

    def repair_unit(self):
        i = self._unit_index()
        if i is None:
            self.log("请先选择一个角色", "warn")
            return
        if not self._ladders_ok():
            return
        self.snap = M.Snapshot(self.h)
        total = 0
        hits = []
        for l in self._targets():
            a = l.addr(i)
            cells = M.read_unit(self.h, a) or []
            worn = [k for k, c in enumerate(cells) if c[0] and c[1]]
            for k in worn:
                self.snap.remember(a + k * M.CELL_BYTES + 2, 1)
            done = M.repair_unit(self.h, a, worn, expect_stats=self.units[i].stats)
            total += len(done)
            # 写完立刻回读确认：日志里能看到"改前 → 改后"，出事也有据可查
            if done:
                back = M.read_unit(self.h, a) or []
                left = [k for k in done if k < len(back) and back[k][1]]
                hits.append(f"{a:#x} 格{','.join(str(k + 1) for k in done)}"
                            + (f"（未生效:{left}）" if left else ""))
        self.log(f"{self.units[i].name}：修满 {total} 格耐久"
                 + (f"（{'; '.join(hits)}）" if hits else ""), "ok")
        self._refresh_all()

    def repair_all(self):
        if not self._ladders_ok():
            return
        self.snap = M.Snapshot(self.h)
        total = 0
        for i in range(len(self.units)):
            for l in self._targets():
                a = l.addr(i)
                cells = M.read_unit(self.h, a) or []
                worn = [k for k, c in enumerate(cells) if c[0] and c[1]]
                for k in worn:
                    self.snap.remember(a + k * M.CELL_BYTES + 2, 1)
                total += len(M.repair_unit(self.h, a, worn,
                                           expect_stats=self.units[i].stats))
        self.log(f"全员修满：共 {total} 格", "ok")
        self._refresh_all()

    def edit_masteries(self):
        """打开"武器熟练度"编辑框（记录头 +0x2E 起 13 个 u16）。

        为什么单独做一个对话框：熟练度是 13 个并列的小数值，塞进背包那三列表格里
        既挤又容易点错；单独一列"槽号 / 当前值 / 新值"，改哪一行一眼可见。
        """
        i = self._unit_index()
        if i is None:
            self.log("请先选择一个角色", "warn")
            return
        if not self._ladders_ok():
            return
        vals = M.read_masteries(self.h, self.ladders[0].addr(i))
        if not vals:
            self.log(f"{self.units[i].name}：读不到熟练度（这一块不像单位记录）", "err")
            return
        _MasteryDialog(self.app, self.units[i].name, vals,
                       lambda changes: self._apply_masteries(i, changes))

    def _apply_masteries(self, i: int, changes: list[tuple[int, int]]) -> int:
        """把改动写进所有校验过的副本，逐条回读确认。

        changes = [(槽号, 新值)]；返回真正生效的条数。
        写之前把原值记进快照，这样「撤销上一批修改」也能把熟练度退回去。
        """
        if not changes:
            return 0
        if not self._ladders_ok():
            return 0
        # 写之前再核一次"这块还是这个人"：+0x16 自带槽号必须等于所在槽号 + 1，
        # 只要对不上就说明记录换了位置，一律不写（宁可少改一次，也不写错人）。
        slot = self.ladders[0].slot(i)
        if M.slot_self_ok(self.h, self.ladders[0].base0, slot) is False:
            self.log(f"{self.units[i].name}：记录的自带槽号对不上（槽 {slot}），已拒绝写入", "err")
            return 0
        bad = [v for _k, v in changes if not (0 <= v <= M.MASTERY_MAX)]
        if bad:
            self.log(f"熟练度超过合理上限 {M.MASTERY_MAX}：{bad}，已拒绝写入", "err")
            return 0
        self.snap = M.Snapshot(self.h)
        old = M.read_masteries(self.h, self.ladders[0].addr(i)) or []
        ok = 0
        for idx, val in changes:
            done = False
            for l in self._targets():
                a = l.addr(i)
                # 快照用的是"记录起点 + 字段偏移"，与写入口径保持一致
                at = a + M.REC_SHIFT + M.REC_MASTERY_OFF + idx * 2
                before = M.read_bytes(self.h, at, 2) or b""
                after = bytes([val & 0xFF, (val >> 8) & 0xFF])
                self.snap.remember(at, 2)
                res = M.write_mastery(self.h, a, idx, val)
                if res:
                    self.snap.note_written(at, after)
                self._audit("mastery", at, before, after, res)
                done = res or done
            if done:
                ok += 1
                was = old[idx] if idx < len(old) else "?"
                self.log(f"{self.units[i].name} 熟练度槽{idx}：{was} → {val}", "ok")
            else:
                self.log(f"{self.units[i].name} 熟练度槽{idx}：写入失败或回读不一致", "err")
        self._refresh_all()
        return ok

    def replace_selected(self):
        i = self._unit_index()
        if i is None:
            self.log("请先连接游戏并选择角色", "warn")
            return
        if not self._ladders_ok():
            return
        sel = self.tree_cells.selection()
        if not sel:
            self.log("请先在右侧选中一个格子", "warn")
            return
        if not self.catalog:
            self._load_catalog()
        if not self.catalog:
            self.log("物品目录是空的（物品代码对照表.csv 没读到），无法替换", "err")
            return
        slot = int(sel[0])
        cur = self.cells[i][slot] if i < len(self.cells) and slot < len(self.cells[i]) else (0, 0, 0, 0)
        self.log(f"选定：{self.units[i].name} 格{slot + 1}"
                 + (f"（现在 {cur[0]} {M.name_of(cur[0])}）" if cur[0] else "（现在空格）"))

        def apply(row: ItemRow):
            """把选中格整格写成「编号 + 全新未装备」。每一步都留日志，出错不再静默。"""
            try:
                self.snap = M.Snapshot(self.h)
                ok = 0
                targets = self._targets()
                for l in targets:
                    a = l.addr(i) + slot * M.CELL_BYTES
                    before = M.read_bytes(self.h, a, M.CELL_BYTES)
                    self.snap.remember(a, M.CELL_BYTES)
                    wrote = M.write_cell(self.h, a - slot * M.CELL_BYTES, slot, row.idx)
                    after = M.read_bytes(self.h, a, M.CELL_BYTES)
                    self.log(f"{a:#x}  旧 {before.hex(' ') if before else '读不到'}"
                             f" → 新 {after.hex(' ') if after else '读不到'}"
                             f"   写入={'成功' if wrote else '失败'}", "ok" if wrote else "err")
                    if wrote:
                        ok += 1
                self.log(f"{self.units[i].name} 格{slot + 1} → {row.idx} {row.zh}（全新未装备），"
                         f"写入 {ok}/{len(targets)} 份", "ok" if ok else "err")
                self._refresh_all()
            except Exception as e:                       # noqa: BLE001
                self.log(f"替换时异常：{type(e).__name__}: {e}", "err")

        try:
            _ItemDialog(self.app, self.catalog, apply)
        except Exception as e:                           # noqa: BLE001
            self.log(f"打开物品目录失败：{type(e).__name__}: {e}", "err")

    def _record_ranges(self) -> list[tuple[int, int]]:
        """当前所有"校验过的单位记录"的内存范围，供撤销时做落点校验。

        一条记录 = `背包地址 + REC_SHIFT` 起、长 `STRIDE`（0x8B0）。
        撤销只允许往这些范围里写 —— 范围外的地址意味着游戏搬过数组或释放过页面，
        那时候写进去就是往别人的结构里涂字节（20:24 那次闪退的头号嫌疑）。
        """
        out = []
        for l in self.ladders or []:
            for i in range(len(self.units)):
                a = l.addr(i) + M.REC_SHIFT
                out.append((a, a + M.STRIDE))
        return out

    def undo(self):
        if not self.snap or not self.snap.saved:
            self.log("没有可撤销的修改", "warn")
            return
        # 撤销是唯一一条"用户随时可能按"的写入路径，必须在写之前重新验地址
        if not self._ladders_ok():
            self.log("地址已失效：撤销会写到错位的地方，已拒绝。"
                     "请先点「重新定位」让地址重新校验通过，再撤销", "err")
            return
        ranges = self._record_ranges()
        # 撤销前把"当前字节"先读进内存：审计日志要能写清 before/after 才算证据
        before = {}
        for a, raw in self.snap.saved.items():
            before[a] = M.read_bytes(self.h, a, len(raw)) or b""
        n = self.snap.undo(ranges=ranges,
                           on_write=lambda a, raw, ok: self._audit(
                               "undo", a, before.get(a, b""), raw, ok))
        skipped = self.snap.skipped
        self.snap = None
        msg = f"已撤销：还原 {n} 处"
        if skipped:
            msg += f"；{skipped} 处已不在任何单位记录范围内，已跳过（没有盲写）"
        self.log(msg, "ok" if n else "warn")
        self._refresh_all()

    # ---------------------------------------------------------------- 自动修满
    def _snapshot_fingerprints(self):
        """把每个单位的记录头（属性组 / 当前 HP / 等级）记到 mem_fp_history.jsonl。

        为什么要有这个东西：我们现在把"属性组"当身份指纹，但它是"成长/基础"那一类数值，
        等级随升级变、当前 HP 一战斗就变。到底哪些字段在什么操作后会动，光靠推测不行 ——
        这里每 60 秒（内容有变化才）记一行，玩家升级/转职/换装之后直接对比两行，
        就能用实测数据定出"指纹该用多宽的容差、要不要掺别的字段"。
        2026-11-16 19:30 迪托利希转职的实测答案是：属性组 9 项一个都没动，等级也没动。
        """
        if not self.h or not self.ladders:
            return
        try:
            snap = {}
            for u in self.units:
                rec = M.read_record(self.h, self.ladders[0].addr(u.index))
                if rec:
                    snap[str(u.person)] = {"name": u.name, **rec}
            if not snap:
                return
            key = json.dumps(snap, sort_keys=True, ensure_ascii=False)
            if key == self._fp_last:
                return                     # 没变化就不写，避免把文件刷成一堆重复行
            self._fp_last = key
            path = os.path.join(M.PROJ, "mem_fp_history.jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "pid": self.pid,
                                    "base0": self.ladders[0].base0,
                                    "units": snap}, ensure_ascii=False) + "\n")
        except Exception:
            pass                           # 观测功能，绝不能因为它影响主流程

    def _audit(self, tag: str, addr: int, before: bytes, after: bytes, ok: bool):
        """把每一次内存写入落盘到 `mem_writes.jsonl`（时间 / PID / 地址 / 新旧字节 / 是否一致）。

        为什么必须落盘：面板日志只活在窗口里，游戏一崩、编辑器一关就全没了。
        20:24 与 20:36 两次闪退都因为"没写盘日志"而无法复盘到底往哪写了什么，
        这个文件就是下一次取证的全部依据 —— 编辑器和游戏谁先走的，一比对就清楚。
        """
        try:
            with open(os.path.join(M.PROJ, "mem_writes.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                                    "pid": self.pid, "tag": tag, "addr": hex(addr),
                                    "before": before.hex(), "after": after.hex(),
                                    "ok": bool(ok)}, ensure_ascii=False) + "\n")
        except Exception:
            pass                       # 审计失败绝不能影响主流程

    def _settings_path(self) -> str:
        return os.path.join(M.PROJ, "editor_settings.json")

    def _load_flag(self, key: str, default: bool) -> bool:
        """读面板开关的持久化状态（和主编辑器的 last_folder 存在同一个 json 里）。"""
        try:
            with open(self._settings_path(), encoding="utf-8") as f:
                return bool(json.load(f).get(key, default))
        except Exception:
            return default

    def _save_flag(self, key: str, value: bool) -> None:
        """只改这一个键，别把主编辑器写的其它设置（last_folder 等）覆盖掉。"""
        try:
            with open(self._settings_path(), encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data[key] = bool(value)
        try:
            with open(self._settings_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _on_poll_target_lost(self):
        """轮询发现地址失效时（在 Tk 主线程里）做的收尾：丢掉坏地址、等自动重定锚。

        开关保持勾着（用户的意思是"一直满耐久"，不是"一失效就永久关掉"）。
        """
        self.poll_thread = None
        self.ladders = []
        self.cells = []
        self.set_state("地址已失效，等待重新定锚…", "warn")
        self.log("自动修满：地址失效，已暂停。正在等新的 A 存档/缓存重定锚，"
                 "成功后会自动继续修满", "warn")
        self.after(500, self._watch_once)

    def _apply_units(self, units) -> list[str]:
        """把一份单位列表落到地址梯上，返回"还认不出槽"的名字。

        缓存里有槽的直接用；没槽的现场按背包内容认（枚举 MAX_SLOTS 个槽，读 120 KB，毫秒级）。
        认不出的（刚招募、游戏还没把这个角色的块载入内存）**不清地址梯** ——
        已经能修的那些继续修满，认不出的挂起来等 1 秒后的重试，认上了就自动纳入。
        """
        if not self.ladders:
            return [u.name for u in units]
        smap = {}
        for e in (M.load_cache().get("ladders") or []):
            smap.update({int(k): v for k, v in (e.get("slot_map") or {}).items()})
        base0 = self.ladders[0].base0
        slots = [smap.get(u.person, -1) for u in units]
        if any(s < 0 for s in slots):
            mem = M.enumerate_slots(self.h, base0, M.MAX_SLOTS)
            res = M.resolve_slots(self.h, base0, units, M.SEQ_TOLERANCE_MAX, mem)
            slots = [slots[i] if slots[i] >= 0 else res[i] for i in range(len(units))]
        kept = [u for u, s in zip(units, slots) if s >= 0]
        kept_slots = [s for s in slots if s >= 0]
        pending = [u for u, s in zip(units, slots) if s < 0]
        before = ([u.person for u in self.units], list(self.ladders[0].slots))
        after = ([u.person for u in kept], list(kept_slots))
        changed = before != after
        self.units = kept
        for l in self.ladders:
            l.slots = list(kept_slots)
            l.count = len(kept)
            l.addrs = [l.addr(u.index) for u in kept]
        if kept:
            # save_cache 是合并写：保住缓存里其它角色的槽位认知，只补/更新这一批
            M.save_cache(self.ladders, self.h, kept)
        if changed:
            self.cells = []
            self._refresh_all()
        return [u.name for u in pending]

    def _maybe_autostart(self):
        """连接成功后：开关是勾上的就自动开始修满，用户不用去点。

        这也是"改完自动帮我点上"的落地 —— 状态存在 editor_settings.json 的
        mem_auto_repair 里，默认 True，重启面板/重启游戏后自己恢复。
        """
        if self.ladders and self.h:
            self._miss_count = 0           # 定上了，把"连续落空"计数清掉
        if not self.var_auto.get() or not self.ladders or not self.h:
            return
        if self.poll_thread and self.poll_thread.is_alive():
            return
        self._toggle_poll()

    def _toggle_poll(self):
        """勾上就一直修；勾掉才停。这个状态会写进 editor_settings.json，
        下次打开面板/重启游戏后自动恢复，不用每次手动点。"""
        self._save_flag("mem_auto_repair", bool(self.var_auto.get()))
        if self.var_auto.get():
            if self.poll_thread and self.poll_thread.is_alive():
                return                     # 已经在跑了，别起第二条线程
            if not self.ladders or not self.h:
                self.var_auto.set(False)
                self.log("请先连接游戏", "warn")
                return
            if not self._ladders_ok():
                self.var_auto.set(False)
                return
            self.poll_targets = self._targets()      # 在主线程定好要写哪几条梯
            self.poll_stop.clear()
            self.poll_thread = threading.Thread(target=self._poll_worker, daemon=True)
            self.poll_thread.start()
            self.log(f"自动修满已开启（只清 b2 字节，写 {len(self.poll_targets)} 条梯）")
        else:
            self.poll_stop.set()
            self.poll_thread = None
            self.log("自动修满已关闭")

    def _poll_worker(self):
        """读整段 → 只写 b2 有变化的格子。绝不整段回写，避免覆盖游戏对 b0b1 的写入。

        另外每 1 秒（不是每次）把地址梯验一遍：游戏读档/换场景后数组会搬走，
        地址一旦对不上就立刻停手，不能让轮询往旧地址持续写。
        """
        tick = 0
        while not self.poll_stop.is_set():
            try:
                iv = max(20, int(self.var_iv.get())) / 1000.0
            except Exception:
                iv = 0.05
            try:
                if tick % 4 == 0:
                    tg = list(self.poll_targets or [])
                    # ladder_alive：先比记录头属性组（不依赖背包），背包内容只做兜底。
                    # 这样玩家刚用掉一件道具、交换了道具，也不会被误判"地址失效"。
                    if not all(M.ladder_alive(self.h, l, self.units) for l in tg):
                        # 地址失效（游戏读档/换场景把数组搬走了）：停手，但**不关开关**。
                        # 关掉开关等于用户还得回来手动点一次；这里清掉地址让 5 秒的自动
                        # 重试去重新定锚，定上了 _maybe_autostart 会自己把修满续上。
                        self.poll_stop.set()
                        self.after(0, self._on_poll_target_lost)
                        break
                for i in range(len(self.units)):
                    for l in (self.poll_targets or []):
                        a = l.addr(i)
                        raw = M.read_bytes(self.h, a, 30 * M.CELL_BYTES)
                        if not raw:
                            continue
                        worn = [k for k in range(30)
                                if (raw[k * 8] or raw[k * 8 + 1]) and raw[k * 8 + 2]]
                        if worn:
                            M.repair_unit(self.h, a, worn)
            except Exception:
                pass
            tick += 1
            self.poll_stop.wait(iv)


# --------------------------------------------------------------------------- 物品目录对话框
class _ItemDialog(tk.Toplevel):
    """搜索 + 选中放入。沿用主编辑器配色，不写死颜色。"""

    def __init__(self, app, rows: list[ItemRow], on_pick):
        super().__init__(app)
        self.title("选择物品（内存模式）")
        self.app = app
        self.rows = rows
        self.on_pick = on_pick
        self.filtered: list[ItemRow] = []

        bar = ttk.Frame(self, style="Card.TFrame", padding=(12, 10))
        bar.pack(side="top", fill="x")
        ttk.Label(bar, text="搜索", style="CardMuted.TLabel").pack(side="left")
        self.var_q = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.var_q, width=28)
        ent.pack(side="left", padx=(8, 0))
        ent.bind("<KeyRelease>", lambda e: self._refresh())
        ttk.Label(bar, text="中文 / 日文 / 英文 / 编号 均可", style="CardFaint.TLabel"
                  ).pack(side="left", padx=(8, 0))
        ent.focus_set()

        self.tree = ttk.Treeview(self, columns=("idx", "zh", "jp", "iid"), show="headings")
        for c, t, w in (("idx", "编号", 70), ("zh", "中文名", 230), ("jp", "日文名", 230), ("iid", "ID", 100)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center" if c in ("idx", "iid") else "w")
        self.tree.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.tree.bind("<Double-1>", lambda e: self._pick())
        self.tree.bind("<Return>", lambda e: self._pick())

        foot = ttk.Frame(self, style="Card.TFrame", padding=(12, 8))
        foot.pack(side="bottom", fill="x")
        ttk.Button(foot, text="放入选中格子", style="Accent.TButton",
                   command=self._pick).pack(side="right")

        app.place_dialog(self, 700, 560)
        self.transient(app)
        self._refresh()

    def _refresh(self):
        q = self.var_q.get().strip().lower()
        if q:
            self.filtered = [r for r in self.rows
                             if q in r.zh.lower() or q in r.jp.lower()
                             or q in r.en.lower() or q in r.iid.lower() or q == str(r.idx)]
        else:
            self.filtered = self.rows[:600]
        self.tree.delete(*self.tree.get_children())
        for r in self.filtered[:800]:
            self.tree.insert("", "end", values=(r.idx, r.zh, r.jp, r.iid))
        self.title(f"选择物品（内存模式） · {len(self.filtered)} 条匹配")

    def _pick(self):
        sel = self.tree.selection()
        if not sel:
            self.title("选择物品（内存模式） · 请先在列表里选一行")
            return
        idx = int(self.tree.item(sel[0], "values")[0])
        row = next((r for r in self.filtered if r.idx == idx), None)
        if row:
            self.on_pick(row)
            self.destroy()


# --------------------------------------------------------------- 武器熟练度对话框
class _MasteryDialog(tk.Toplevel):
    """13 个武器槽的熟练度编辑框。

    数据来源是记录头 +0x2E 起的 13 个 u16（与存档 masteries 一一同构，82/82 实测全对）。
    槽号 ↔ 武器名用 mem_editor.MASTERY_NAMES（来自 fefw_save.SKILL_NAMES），
    并已和实测对上：穆 槽 4 = 拳（2560 显示拳 C+）、迪托利希 槽 0 = 剑（2560 显示剑 S）。
    等级用 mem_editor.MASTERY_THRESHOLDS（S+ = 2560，上游注释写明已在游戏内确认）。
    """

    def __init__(self, app, unit_name: str, values: list[int], on_apply):
        super().__init__(app)
        self.app = app
        self.on_apply = on_apply
        self.vars: list[tk.StringVar] = []
        self.lbl_grade: list[ttk.Label] = []
        self.title(f"武器熟练度 · {unit_name}")

        tip = ttk.Frame(self, style="Card.TFrame", padding=(12, 10))
        tip.pack(side="top", fill="x")
        ttk.Label(tip, text=f"{unit_name}    记录 +0x2E 起的 13 个槽", style="CardH3.TLabel"
                  ).pack(anchor="w")
        ttk.Label(tip, text="等级阶梯 E→S+：0 / 20 / 35 / 80 / 220 / 440 / 690 / 910 / "
                            "1215 / 1510 / 1960 / 2560；改完立即生效、无需读档",
                  style="CardFaint.TLabel").pack(anchor="w", pady=(4, 0))

        grid = ttk.Frame(self, style="Card.TFrame", padding=(12, 4))
        grid.pack(fill="both", expand=True)
        for c, t in ((0, "槽"), (1, "项目"), (2, "当前值"), (3, "等级"), (4, "新值")):
            ttk.Label(grid, text=t, style="CardMuted.TLabel").grid(
                row=0, column=c, sticky="w", padx=(0, 12), pady=(0, 6))
        for idx, val in enumerate(values):
            name = M.MASTERY_NAMES[idx] if idx < len(M.MASTERY_NAMES) else f"槽{idx}"
            ttk.Label(grid, text=str(idx), style="CardMono.TLabel").grid(
                row=idx + 1, column=0, sticky="w", padx=(0, 12), pady=1)
            ttk.Label(grid, text=name, style="CardMono.TLabel").grid(
                row=idx + 1, column=1, sticky="w", padx=(0, 12), pady=1)
            ttk.Label(grid, text=str(val), style="CardMono.TLabel").grid(
                row=idx + 1, column=2, sticky="w", padx=(0, 12), pady=1)
            g = ttk.Label(grid, text=M.mastery_grade(val), style="CardMono.TLabel")
            g.grid(row=idx + 1, column=3, sticky="w", padx=(0, 12), pady=1)
            self.lbl_grade.append(g)
            v = tk.StringVar(value=str(val))
            self.vars.append(v)
            ttk.Entry(grid, textvariable=v, width=8).grid(
                row=idx + 1, column=4, sticky="w", pady=1)
            # 边输边算等级，方便对着游戏界面调
            v.trace_add("write", lambda *_, i=idx, var=v: self._preview(i, var))

        foot = ttk.Frame(self, style="Card.TFrame", padding=(12, 8))
        foot.pack(side="bottom", fill="x")
        ttk.Button(foot, text="应用改动", style="Accent.TButton",
                   command=self._apply).pack(side="right")
        ttk.Button(foot, text="全部 +100", style="Small.TButton",
                   command=lambda: self._bump_add(100)).pack(side="right", padx=(0, 8))
        ttk.Button(foot, text="全部满级(2560)", style="Small.TButton",
                   command=lambda: self._bump_set(2560)).pack(side="right", padx=(0, 8))
        self.lbl_msg = ttk.Label(foot, text="", style="CardFaint.TLabel")
        self.lbl_msg.pack(side="left")

        app.place_dialog(self, 520, 560)
        self.transient(app)

    def _preview(self, idx: int, var):
        """输入框变化时把该行的等级字母跟着刷新（不写内存，只是预览）。"""
        s = var.get().strip()
        text = M.mastery_grade(int(s)) if s.isdigit() else "?"
        self.lbl_grade[idx].configure(text=text)

    def _bump_add(self, delta: int):
        """快捷填充：在当前值上每槽加 delta（"全部 +100"）。"""
        for v in self.vars:
            s = v.get().strip()
            base = int(s) if s.isdigit() else 0
            v.set(str(min(base + delta, M.MASTERY_MAX)))
        self.lbl_msg.configure(
            text=f"已按「每槽 +{delta}」填入，点「应用改动」才写入内存")

    def _bump_set(self, value: int):
        """快捷填充：13 个槽统一设成 value（满级 = 2560，实测 S 值）。"""
        for v in self.vars:
            v.set(str(value))
        self.lbl_msg.configure(
            text=f"已全部填成 {value}，点「应用改动」才写入内存")

    def _apply(self):
        changes = []
        bad = []
        for idx, v in enumerate(self.vars):
            s = v.get().strip()
            if not s.isdigit():
                bad.append(idx)
                continue
            n = int(s)
            if not (0 <= n <= M.MASTERY_MAX):
                bad.append(idx)
                continue
            changes.append((idx, n))
        if bad:
            self.lbl_msg.configure(text=f"槽 {', '.join(map(str, bad))} 不是合法数字")
            return
        done = self.on_apply(changes)
        self.lbl_msg.configure(text=f"已写入 {done}/{len(changes)} 个槽")
        if done == len(changes):
            self.destroy()
