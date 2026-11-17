# -*- coding: utf-8 -*-
"""
FEFW 内存背包编辑器（GUI v0.2）

设计原则
  · 所有地址都由 mem_editor 自动定位（L0 缓存 1.7 ms / L1 区域指纹 ~0.5 s / L2 全量扫描兜底），
    界面上不出现任何硬编码地址。
  · 每次写内存前先做 Snapshot，界面上提供"撤销上一批修改"。
  · 写入只做两件事：改 8 字节的物品格（编号+耐久+装备标记）、或只清 b2 字节（修满耐久）。
  · 定位、扫描、写入全部在后台线程执行，主线程只负责界面，窗口不会假死。

用法
  python mem_editor_gui.py
"""
from __future__ import annotations

import csv
import os
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mem_editor as M          # 定位 / 读写核心

PROJ = M.PROJ
CSV_PATH = os.path.join(PROJ, "物品代码对照表.csv")
SAVE_DIR = r"D:\fefw-recomp-0.1.6\save\account\0100000000000000706d635257464546\000"

BG = "#1e1e1e"          # 窗口底色
PANEL = "#252526"       # 面板底色
CARD = "#2d2d30"        # 输入控件底色
FG = "#e8e8e8"
DIM = "#9a9a9a"
ACCENT = "#3b82f6"
OK = "#4ade80"
WARN = "#fbbf24"


# --------------------------------------------------------------------------- 物品目录
@dataclass
class ItemRow:
    idx: int
    iid: str
    jp: str
    en: str
    zh: str
    basis: str


def load_item_catalog() -> list[ItemRow]:
    rows: list[ItemRow] = []
    with open(CSV_PATH, encoding="utf-8-sig") as fp:
        for r in csv.DictReader(fp):
            v = r["内部编号b0b1"].strip()
            if not v.isdigit():
                continue
            idx = int(v)
            zh = (r["中文名"] or "").strip()
            jp = (r["日文名"] or "").strip()
            if not zh and not jp:
                continue
            rows.append(ItemRow(idx, r["物品ID"].strip(), jp, (r["英文名"] or "").strip(),
                                zh or jp, r["编号依据"].strip()))
    rows.sort(key=lambda x: x.idx)
    return rows


# --------------------------------------------------------------------------- 主界面
class MemEditorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FEFW 内存编辑器 · 背包")
        self.geometry("1080x680")
        self.minsize(900, 560)
        self.configure(bg=BG)

        # 运行状态
        self.pid: int | None = None
        self.h = None                      # 进程句柄
        self.save_path: str = ""
        self.units: list[M.SaveUnit] = []
        self.ladders: list[M.Ladder] = []
        self.cells: list[list[tuple[int, int, int, int]]] = []   # 每个单位的 30 格
        self.snap: M.Snapshot | None = None
        self.catalog: list[ItemRow] = []
        self.log_q: queue.Queue[tuple[str, str]] = queue.Queue()

        self.poll_stop = threading.Event()
        self.poll_thread: threading.Thread | None = None

        self._build_style()
        self._build_ui()
        self._load_catalog()
        self._poll_log()
        self.after(200, self._auto_pick_save)

    # ---------------- 样式
    def _build_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=CARD)
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Panel.TLabel", background=PANEL, foreground=FG)
        st.configure("Dim.TLabel", background=PANEL, foreground=DIM)
        st.configure("TButton", background=CARD, foreground=FG, borderwidth=0, padding=(10, 6))
        st.map("TButton", background=[("active", "#3a3a3d")])
        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff")
        st.map("Accent.TButton", background=[("active", "#2f6fd0")])
        st.configure("TCombobox", fieldbackground=CARD, background=CARD, foreground=FG,
                     arrowcolor=FG, padding=4)
        st.map("TCombobox",
               fieldbackground=[("readonly", CARD)],
               foreground=[("readonly", FG)],
               selectbackground=[("readonly", CARD)],
               selectforeground=[("readonly", FG)],
               background=[("active", "#3a3a3d")])
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                     rowheight=22, borderwidth=0)
        st.configure("Treeview.Heading", background=CARD, foreground=DIM, borderwidth=0)
        st.map("Treeview", background=[("selected", "#2f4f7f")], foreground=[("selected", "#ffffff")])
        st.configure("TSeparator", background="#3a3a3d")
        st.configure("TCheckbutton", background=PANEL, foreground=FG)
        st.map("TCheckbutton", background=[("active", PANEL)])
        st.configure("TSpinbox", fieldbackground=CARD, background=CARD, foreground=FG,
                     arrowcolor=FG, padding=3)

    # ---------------- 布局
    def _build_ui(self):
        # 顶部工具条
        top = ttk.Frame(self, style="Panel.TFrame", padding=(12, 10))
        top.pack(side="top", fill="x")

        ttk.Label(top, text="存档", style="Panel.TLabel").pack(side="left")
        self.var_save = tk.StringVar()
        self.cmb_save = ttk.Combobox(top, textvariable=self.var_save, width=18, state="readonly")
        self.cmb_save.pack(side="left", padx=(8, 4))
        self.cmb_save.bind("<<ComboboxSelected>>", lambda e: self._connect())

        ttk.Button(top, text="浏览…", command=self._browse_save).pack(side="left")
        ttk.Button(top, text="连接游戏", style="Accent.TButton",
                   command=self._connect).pack(side="left", padx=(10, 0))
        ttk.Button(top, text="重新定位", command=self._relocate).pack(side="left", padx=(6, 0))

        self.lbl_state = ttk.Label(top, text="未连接", style="Dim.TLabel")
        self.lbl_state.pack(side="left", padx=(14, 0))

        # 中部：左单位列表 + 右物品格
        body = ttk.Frame(self, padding=(12, 10))
        body.pack(side="top", fill="both", expand=True)

        left = ttk.Frame(body, style="Panel.TFrame", padding=(8, 8))
        left.pack(side="left", fill="y")
        ttk.Label(left, text="角色", style="Panel.TLabel").pack(anchor="w", padx=4, pady=(0, 6))
        self.lst_units = tk.Listbox(left, width=22, height=16, bg=PANEL, fg=FG,
                                    selectbackground="#2f4f7f", selectforeground="#ffffff",
                                    borderwidth=0, highlightthickness=0, activestyle="none",
                                    font=("Microsoft YaHei UI", 10))
        self.lst_units.pack(side="left", fill="y")
        self.lst_units.bind("<<ListboxSelect>>", lambda e: self._on_unit_select())
        sb1 = ttk.Scrollbar(left, orient="vertical", command=self.lst_units.yview)
        sb1.pack(side="left", fill="y")
        self.lst_units.configure(yscrollcommand=sb1.set)

        right = ttk.Frame(body, style="Panel.TFrame", padding=(10, 8))
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        head = ttk.Frame(right, style="Panel.TFrame")
        head.pack(side="top", fill="x")
        self.lbl_unit = ttk.Label(head, text="请先连接游戏", style="Panel.TLabel",
                                  font=("Microsoft YaHei UI", 11, "bold"))
        self.lbl_unit.pack(side="left")
        self.lbl_addr = ttk.Label(head, text="", style="Dim.TLabel")
        self.lbl_addr.pack(side="right")

        # 30 格表格
        cols = ("slot", "idx", "name", "wear", "eq")
        self.tree = ttk.Treeview(right, columns=cols, show="headings", height=15)
        for c, t, w, anchor in (("slot", "格", 46, "center"), ("idx", "编号", 70, "center"),
                                ("name", "物品", 300, "w"), ("wear", "耐久", 90, "center"),
                                ("eq", "状态", 80, "center")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=anchor, stretch=(c == "name"))
        self.tree.pack(side="top", fill="both", expand=True, pady=(8, 0))
        self.tree.bind("<Double-1>", lambda e: self._replace_selected())

        # 操作按钮
        btns = ttk.Frame(right, style="Panel.TFrame")
        btns.pack(side="top", fill="x", pady=(10, 0))
        ttk.Button(btns, text="修满耐久（本角色）", command=self._repair_unit).pack(side="left")
        ttk.Button(btns, text="修满耐久（全员）", command=self._repair_all).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="替换物品…", command=self._replace_selected).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="撤销上一批修改", command=self._undo).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="仅刷新显示", command=self._reload_cells).pack(side="left", padx=(8, 0))

        # 底部日志 + 自动修满
        bottom = ttk.Frame(self, style="Panel.TFrame", padding=(12, 8))
        bottom.pack(side="bottom", fill="x")
        row = ttk.Frame(bottom, style="Panel.TFrame")
        row.pack(side="top", fill="x")
        self.var_auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="战斗中自动修满耐久", variable=self.var_auto,
                        command=self._toggle_poll).pack(side="left")
        ttk.Label(row, text="间隔(ms)", style="Panel.TLabel").pack(side="left", padx=(10, 4))
        self.var_iv = tk.IntVar(value=50)
        ttk.Spinbox(row, from_=20, to=1000, increment=10, width=6,
                    textvariable=self.var_iv).pack(side="left")
        ttk.Label(row, text="（只清 b2 字节，不动编号与装备标记）", style="Dim.TLabel").pack(side="left", padx=(10, 0))

        self.txt_log = tk.Text(bottom, height=5, bg=PANEL, fg=DIM, borderwidth=0,
                               highlightthickness=0, font=("Consolas", 9))
        self.txt_log.pack(side="top", fill="x", pady=(6, 0))
        self.txt_log.configure(state="disabled")

    # ---------------- 日志
    def log(self, msg: str, level: str = "info"):
        self.log_q.put((level, msg))

    def _poll_log(self):
        try:
            while True:
                level, msg = self.log_q.get_nowait()
                tag = {"info": "", "ok": " ✔", "warn": " ⚠", "err": " ✘"}.get(level, "")
                line = time.strftime("%H:%M:%S") + tag + "  " + msg + "\n"
                self.txt_log.configure(state="normal")
                self.txt_log.insert("end", line)
                self.txt_log.see("end")
                self.txt_log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(120, self._poll_log)

    def set_state(self, text: str, color: str = DIM):
        self.lbl_state.configure(text=text, foreground=color)

    # ---------------- 目录 / 存档
    def _load_catalog(self):
        try:
            M.load_code_table()            # 关键：填充 mem_editor 的"物品ID ↔ 内部编号 ↔ 名字"反查表
            self.catalog = load_item_catalog()
            self.log(f"物品目录载入 {len(self.catalog)} 条")
        except Exception as e:
            self.log(f"物品目录载入失败：{e}", "err")

    def _auto_pick_save(self):
        try:
            names = []
            for n in sorted(os.listdir(SAVE_DIR)):
                p = os.path.join(SAVE_DIR, n)
                if not os.path.isfile(p):
                    continue
                # 只要真正的存档槽：Auto / Manual1..N（过滤掉 Chapter_*、备份与其他文件）
                if n.startswith(("Auto", "Manual")) and "before" not in n and "." not in n:
                    names.append(n)
            self.cmb_save["values"] = names
            if names:
                for pref in ("Manual2", "Manual1", "Auto"):     # 优先手动槽（mod 多存档口）
                    if pref in names:
                        self.var_save.set(pref)
                        break
                else:
                    self.var_save.set(names[0])
                self.log(f"存档槽：{', '.join(names)}；当前选 {self.var_save.get()}")
                self.after(50, self._connect)          # 启动即自动连接，省一步点击
        except Exception as e:
            self.log(f"存档目录读取失败：{e}", "warn")

    def _browse_save(self):
        p = filedialog.askopenfilename(initialdir=SAVE_DIR, title="选择已加载的存档文件")
        if p:
            self.var_save.set(p)
            self._connect()

    def _save_path_full(self) -> str:
        v = self.var_save.get().strip()
        if not v:
            return ""
        return v if os.path.isabs(v) else os.path.join(SAVE_DIR, v)

    # ---------------- 连接 / 定位
    def _connect(self):
        if self.poll_thread:
            self._toggle_poll()               # 先停轮询，避免换档时写到旧地址
        path = self._save_path_full()
        if not path or not os.path.isfile(path):
            self.set_state("存档文件不存在", WARN)
            return
        self.save_path = path
        self.set_state("正在连接…", WARN)
        self.ladders = []
        self.cells = []
        self.tree.delete(*self.tree.get_children())
        self.lst_units.delete(0, "end")

        def work():
            try:
                self.units = M.load_save_units(path)
                if not self.units:
                    # 允许个别物品编号不在对照表内，退一步用"已知部分"当锚点
                    self.units = M.load_save_units(path, require_all_known=False)
                    if self.units:
                        self.log("警告：存档里有物品编号不在对照表内，锚点只用已知编号", "warn")
                if not self.units:
                    self.log("存档里没有可用单位（可能物品编号不在对照表内）", "err")
                    self.set_state("无可用单位", WARN)
                    return
                self.log(f"存档单位 {len(self.units)} 个：" +
                         "、".join(f"{i} {u.name}" for i, u in enumerate(self.units)))
                pid = M.find_pid()
                if not pid:
                    self.log("找不到 fefw.exe 进程，请先启动游戏", "err")
                    self.set_state("未找到游戏进程", WARN)
                    return
                self.pid = pid
                self.h = M.k32.OpenProcess(0x0400 | 0x0010 | 0x0020 | 0x0008, False, pid)
                if not self.h:
                    self.log(f"OpenProcess 失败：{M.ctypes.get_last_error()}", "err")
                    return
                self.log(f"已连接 PID {pid}")

                t0 = time.perf_counter()
                ladders, how = M.try_cache(self.h, self.units)
                if ladders:
                    self.ladders = ladders
                    self.log(f"{how}，{len(ladders)} 条地址梯，{(time.perf_counter()-t0)*1000:.0f} ms", "ok")
                    self.set_state(f"已定位（{how}）", OK)
                else:
                    self.set_state("缓存失效，开始全量扫描（约 1–5 分钟）…", WARN)
                    self.log(f"{how} → L1 扫描 MEM_MAPPED，请等待…", "warn")
                    anchors = M.scan_anchors(self.h, self.units, only_type=0x40000)
                    ladders = M.build_ladders(self.h, self.units, anchors)
                    if not ladders:
                        self.log("L1 未命中 → L2 扫描私有堆…", "warn")
                        anchors = M.scan_anchors(self.h, self.units, only_type=0x20000)
                        ladders = M.build_ladders(self.h, self.units, anchors)
                    self.ladders = ladders
                    if ladders:
                        M.save_cache(ladders, self.h)
                        self.log(f"定位成功：{len(ladders)} 条梯，总耗时 {time.perf_counter()-t0:.0f} s", "ok")
                        self.set_state("已定位（全量扫描）", OK)
                    else:
                        self.log("定位失败：内存里找不到与存档一致的物品串", "err")
                        self.set_state("定位失败", WARN)
                        return
                self._reload_cells()
            except Exception as e:
                self.log(f"连接失败：{e}", "err")
                self.set_state("连接失败", WARN)

        threading.Thread(target=work, daemon=True).start()

    def _relocate(self):
        """清掉地址缓存后重新定位（换了存档槽或游戏重启后地址对不上时用）。"""
        try:
            if os.path.exists(M.CACHE_PATH):
                os.remove(M.CACHE_PATH)
        except Exception:
            pass
        self.log("已清空地址缓存，重新定位")
        self._connect()

    # ---------------- 显示
    def _reload_cells(self):
        if not self.ladders:
            return
        self.cells = []
        for u in self.units:
            c = M.read_unit(self.h, self.ladders[0].addr(u.index))
            self.cells.append(c or [])
        self._fill_unit_list()
        if self.units:
            self.lst_units.selection_clear(0, "end")
            cur = self._current_unit()
            idx = cur if cur is not None else 0
            self.lst_units.selection_set(idx)
            self._show_unit(idx)

    def _fill_unit_list(self):
        cur = self._current_unit()
        self.lst_units.delete(0, "end")
        for i, u in enumerate(self.units):
            n = sum(1 for c in self.cells[i] if c[0]) if i < len(self.cells) else 0
            self.lst_units.insert("end", f" {i}  {u.name}   ({n} 件)")
        if cur is not None and cur < self.lst_units.size():
            self.lst_units.selection_set(cur)

    def _current_unit(self) -> int | None:
        sel = self.lst_units.curselection()
        return int(sel[0]) if sel else None

    def _on_unit_select(self):
        i = self._current_unit()
        if i is not None:
            self._show_unit(i)

    def _show_unit(self, i: int):
        if not self.ladders:            # 还没定位成功时不要碰地址
            return
        self.tree.delete(*self.tree.get_children())
        cells = self.cells[i] if i < len(self.cells) else []
        worn = 0
        used = 0
        for k in range(30):
            idx, b2, b3, b4 = cells[k] if k < len(cells) else (0, 0, 0, 0)
            if idx:
                wear = f"已消耗 {b2}" if b2 else "全新"
                eq = "★ 装备中" if b3 else "未装备"
                self.tree.insert("", "end", iid=str(k),
                                 values=(k + 1, idx, M.name_of(idx), wear, eq))
                used += 1
                worn += 1 if b2 else 0
            else:
                self.tree.insert("", "end", iid=str(k), values=(k + 1, "—", "（空格）", "—", "—"))
        u = self.units[i]
        self.lbl_unit.configure(text=f"{u.name}    共 {used} 件    有耐久消耗 {worn} 件")
        self.lbl_addr.configure(text=f"梯0 {self.ladders[0].addr(u.index):#x}   梯1 {self.ladders[1].addr(u.index):#x}"
                                     if len(self.ladders) > 1 else f"{self.ladders[0].addr(u.index):#x}")

    # ---------------- 操作：修满耐久
    def _repair_unit(self, unit: int | None = None):
        if not self.ladders or self.h is None:
            return
        i = unit if unit is not None else self._current_unit()
        if i is None:
            self.set_state("请先选择一个角色", WARN)
            return
        self.snap = M.Snapshot(self.h)
        total = 0
        for l in self.ladders:
            a = l.addr(i)
            cells = M.read_unit(self.h, a) or []
            worn = [k for k, c in enumerate(cells) if c[0] and c[1]]
            for k in worn:
                self.snap.remember(a + k * M.CELL_BYTES + 2, 1)
            total += len(M.repair_unit(self.h, a, worn))
        self.log(f"{self.units[i].name}：修满 {total} 格耐久", "ok")
        self._reload_cells()

    def _repair_all(self):
        if not self.ladders or self.h is None:
            return
        self.snap = M.Snapshot(self.h)
        total = 0
        for i in range(len(self.units)):
            for l in self.ladders:
                a = l.addr(i)
                cells = M.read_unit(self.h, a) or []
                worn = [k for k, c in enumerate(cells) if c[0] and c[1]]
                for k in worn:
                    self.snap.remember(a + k * M.CELL_BYTES + 2, 1)
                total += len(M.repair_unit(self.h, a, worn))
        self.log(f"全员修满：共 {total} 格", "ok")
        self._reload_cells()

    # ---------------- 操作：替换物品
    def _replace_selected(self):
        if not self.ladders or self.h is None:
            return
        i = self._current_unit()
        if i is None:
            self.set_state("请先选择一个角色", WARN)
            return
        sel = self.tree.selection()
        if not sel:
            self.set_state("请先在右侧选中一个格子", WARN)
            return
        slot = int(sel[0])

        def apply(item: ItemRow):
            self.snap = M.Snapshot(self.h)
            ok = 0
            for l in self.ladders:
                a = l.addr(i)
                self.snap.remember(a + slot * M.CELL_BYTES)
                if M.write_cell(self.h, a, slot, item.idx):
                    ok += 1
            self.log(f"{self.units[i].name} 格{slot+1} → {item.idx} {item.zh}（全新未装备），写入 {ok}/{len(self.ladders)} 份",
                     "ok" if ok else "err")
            self._reload_cells()

        ItemPickerDialog(self, self.catalog, apply)

    # ---------------- 撤销
    def _undo(self):
        if not self.snap or not self.snap.saved:
            self.set_state("没有可撤销的修改", WARN)
            return
        n = self.snap.undo()
        self.snap = None
        self.log(f"已撤销，还原 {n} 处", "ok")
        self._reload_cells()

    # ---------------- 自动修满
    def _toggle_poll(self):
        if self.var_auto.get():
            if not self.ladders or self.h is None:
                self.var_auto.set(False)
                self.set_state("请先连接游戏", WARN)
                return
            self.poll_stop.clear()
            self.poll_thread = threading.Thread(target=self._poll_worker, daemon=True)
            self.poll_thread.start()
            self.log("自动修满已开启")
        else:
            self.poll_stop.set()
            self.poll_thread = None
            self.log("自动修满已关闭")

    def _poll_worker(self):
        """只清 b2 字节；读整段后仅写有变化的格子，不整段回写（避免覆盖游戏对 b0b1 的写入）。"""
        while not self.poll_stop.is_set():
            try:
                iv = max(20, int(self.var_iv.get())) / 1000.0
            except Exception:
                iv = 0.05
            try:
                for i in range(len(self.units)):
                    for l in self.ladders:
                        a = l.addr(i)
                        raw = M.read_bytes(self.h, a, 30 * M.CELL_BYTES)
                        if not raw:
                            continue
                        worn = [k for k in range(30)
                                if raw[k * 8] | raw[k * 8 + 1] and raw[k * 8 + 2]]
                        if worn:
                            M.repair_unit(self.h, a, worn)
            except Exception:
                pass
            self.poll_stop.wait(iv)


class ItemPickerDialog(tk.Toplevel):
    """物品目录：搜索 + 选中放入。"""

    def __init__(self, master: MemEditorApp, rows: list[ItemRow], on_pick):
        super().__init__(master)
        self.title("选择物品")
        self.geometry("620x520")
        self.configure(bg=BG)
        self.on_pick = on_pick
        self.rows = rows
        self.filtered: list[ItemRow] = []

        bar = ttk.Frame(self, style="Panel.TFrame", padding=(10, 8))
        bar.pack(side="top", fill="x")
        ttk.Label(bar, text="搜索", style="Panel.TLabel").pack(side="left")
        self.var_q = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.var_q, width=26)
        ent.pack(side="left", padx=(8, 0))
        ent.bind("<KeyRelease>", lambda e: self._refresh())
        ttk.Label(bar, text="（中文/日文/英文/编号 均可）", style="Dim.TLabel").pack(side="left", padx=(8, 0))
        ent.focus_set()

        cols = ("idx", "zh", "jp", "iid")
        self.tree = ttk.Treeview(self, columns=cols, show="headings")
        for c, t, w in (("idx", "编号", 80), ("zh", "中文名", 210), ("jp", "日文名", 210), ("iid", "ID", 100)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c != "idx" else "center")
        self.tree.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 8))
        self.tree.bind("<Double-1>", lambda e: self._pick())

        foot = ttk.Frame(self, style="Panel.TFrame", padding=(10, 8))
        foot.pack(side="bottom", fill="x")
        ttk.Button(foot, text="放入选中格子", style="Accent.TButton",
                   command=self._pick).pack(side="right")

        self._refresh()

    def _refresh(self):
        q = self.var_q.get().strip().lower()
        if q:
            self.filtered = [r for r in self.rows
                             if q in r.zh.lower() or q in r.jp.lower()
                             or q in r.en.lower() or q in r.iid.lower() or q == str(r.idx)]
        else:
            self.filtered = self.rows[:400]
        self.tree.delete(*self.tree.get_children())
        for r in self.filtered[:600]:
            self.tree.insert("", "end", values=(r.idx, r.zh, r.jp, r.iid))
        self.title(f"选择物品 · {len(self.filtered)} 条匹配")

    def _pick(self):
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], "values")
        idx = int(vals[0])
        row = next((r for r in self.filtered if r.idx == idx), None)
        if row:
            self.on_pick(row)
            self.destroy()


if __name__ == "__main__":
    app = MemEditorApp()
    # 开发自检用：python mem_editor_gui.py picker   会直接弹出物品目录，方便截图核对
    if "--picker-demo" in sys.argv:
        app.after(1200, lambda: ItemPickerDialog(app, app.catalog, lambda r: app.log(f"测试选中 {r.idx} {r.zh}")))
    app.mainloop()
