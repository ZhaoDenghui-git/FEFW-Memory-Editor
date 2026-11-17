# -*- coding: utf-8 -*-
"""
fefw 内存背包读写核心（v0.1）

已实证的内存模型（2026-11-14，第 1 部第 5 章存档 5 人队伍）：
  · 每个单位一条"物品区"：30 格 × 8 字节
      b0b1 = 物品内部编号（小端 u16，0 = 空格）
      b2   = 已消耗耐久（0 = 全新）
      b3   = 已装备标记（0/1）
      b4   = 未定（实测 0/1）
      b5..b7 = 0
  · 同一存档内各单位物品区按 0x8B0 等间距排列，**单位顺序与存档内单位顺序一致**
  · 同一份数据在内存里有**两份副本**（A 主副本、B 镜像），实测改任意一份都会互相同步
  · 物品格起始地址是 **4 字节对齐**（不一定 8 对齐），且整块可能位于 MEM_MAPPED 区域
    → 定位扫描必须按 4 字节步进、并且不跳过映射区（这两个坑都曾让扫描一无所获）

定位策略（不依赖固定地址，每次重启都能重新找）：
  1. 从存档文件读出每个单位实际持有的物品，用 物品代码对照表.csv 换成内部编号
  2. 内存里搜"连续 N 格、每格间隔 8 字节、编号与存档一致"
  3. 命中地址作为锚点，按 0x8B0 反推出该存档的整条地址梯（base0 = 锚点 - 单位序号 × 0x8B0）
  4. 逐块结构校验（b5..b7 必须为 0、编号在范围内、至少一格非空）
  5. 找到多条梯（A/B 副本）时全部记录，写入时全写

只读用法（不修改任何内存）：
    python mem_editor.py --save <存档路径> --list
写入用法：
    python mem_editor.py --save <存档路径> --repair-all             # 全体修满耐久
    python mem_editor.py --save <存档路径> --set 4 1 478             # 杨界 第2格 → 478 沙拉曼达+5 全新
    python mem_editor.py --save <存档路径> --set 4 1 478 --dry-run   # 只打印将要写入的内容
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from ctypes import wintypes as wt

import ctypes
import numpy as np

PROJ = os.path.dirname(os.path.abspath(__file__))     # 物品代码对照表.csv / fefw_*.py 都放在同目录
sys.path.insert(0, PROJ)
import fefw_layer as L          # noqa: E402
import fefw_core as C           # noqa: E402

CELLS = 30
STRIDE = 0x8B0          # 单位之间的间距（实测）
CELL_BYTES = 8
# 背包序列允许的"插入/删除"编辑距离：游戏自己用掉一件物品（那格清空、后面整体后移一格）算 1。
SEQ_TOLERANCE = 3
# 放大档：多人来回交换道具时，每个单位的距离会累加（换走 3 件 + 收到 3 件 ≈ 6）。
# 结构校验（9 个单位 × 30 格、b5..b7 全 0、0x8B0 连梯）仍然保留，只是内容判据放宽。
SEQ_TOLERANCE_MAX = 10
CHUNK = 32 << 20
MIN_MATCH = 3           # 锚点至少连续命中几格

# 热点窗口半宽：定位成功后把命中地址记进缓存，下次先只扫它 ±hot_span 的范围。
# 实测同一个别名跨游戏重启只漂几 MB：
#   0x7806ba133c / 0x7806c5233c / 0x7806f4333c / 0x7806f8333c / 0x7806a2433c（最大差 ≈10 MB）
# 32 MB 足够覆盖；窗口没命中会自动退回全量扫描，所以这个值只影响速度，不影响正确性。
# 实测（15 人主档、MEM_MAPPED、容差 3）：全量 24.8 s → 热点 ±32 MB 0.72 s。
HOT_SPAN = 0x2000000

# 本程序写过的格：{格首地址: 写入的编号}。用于让"改过的内存"仍然通过存档校验，
# 否则改一件物品之后这份内存就永久判失效（实测会卡住后续所有写入）。
_EDITS: dict[int, int] = {}

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
k32.ReadProcessMemory.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
k32.WriteProcessMemory.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t))
k32.VirtualQueryEx.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)
k32.VirtualProtectEx.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD,
                                 ctypes.POINTER(wt.DWORD))

MEM_COMMIT, PAGE_GUARD = 0x1000, 0x100
READABLE = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("PartitionId", wt.WORD), ("_pad", wt.WORD),
                ("RegionSize", ctypes.c_size_t), ("State", wt.DWORD),
                ("Protect", wt.DWORD), ("Type", wt.DWORD)]


# ------------------------------------------------------------------ 名字表
_CODE_BY_IID: dict[str, int] = {}
_NAME_BY_INDEX: dict[int, str] = {}


def load_code_table() -> None:
    """物品代码对照表.csv → iid(hex) 反查内部编号，编号反查名字。"""
    with open(PROJ + r"\物品代码对照表.csv", encoding="utf-8-sig") as fp:
        for row in csv.DictReader(fp):
            v = row["内部编号b0b1"].strip()
            if not v.isdigit():
                continue
            idx = int(v)
            _CODE_BY_IID[row["物品ID"].strip().lower()] = idx
            _NAME_BY_INDEX.setdefault(idx, (row["中文名"] or row["日文名"]).strip())


def name_of(idx: int) -> str:
    return _NAME_BY_INDEX.get(idx, "(未知)")


def unit_name(person: int) -> str:
    v = L.unit_ids().get(person) or {}
    return v.get("zh") or v.get("pid") or f"person={person}"


# ------------------------------------------------------------------ 进程与内存
def find_pid(exe: str = "fefw.exe") -> int | None:
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = (wt.DWORD, wt.DWORD)
    # 必须声明参数类型：否则 64 位 HANDLE 会被截断成 32 位，Process32FirstW 静默失败
    k32.Process32FirstW.argtypes = (wt.HANDLE, ctypes.c_void_p)
    k32.Process32FirstW.restype = wt.BOOL
    k32.Process32NextW.argtypes = (wt.HANDLE, ctypes.c_void_p)
    k32.Process32NextW.restype = wt.BOOL

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", wt.WCHAR * 260)]        # 必须是 WCHAR[260]，用 char[260] 会导致结构长度不符（错误 24）

    snap = k32.CreateToolhelp32Snapshot(2, 0)
    pe = PE32()
    pe.dwSize = ctypes.sizeof(PE32)
    out = None
    if k32.Process32FirstW(snap, ctypes.byref(pe)):
        while True:
            if pe.szExeFile.lower() == exe.lower() and pe.th32ProcessID:
                out = pe.th32ProcessID
            if not k32.Process32NextW(snap, ctypes.byref(pe)):
                break
    k32.CloseHandle(snap)
    return out


def regions(h, only_type: int | None = None) -> list[tuple[int, int]]:
    """已提交且可读的区域（含 MEM_MAPPED：物品数组就落在映射区）。"""
    addr, mbi, out = 0, MBI(), []
    while k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
        base, size = mbi.BaseAddress or 0, mbi.RegionSize
        if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE
                and not (mbi.Protect & PAGE_GUARD) and size
                and (only_type is None or mbi.Type == only_type)
                and mbi.Type != 0x1000000):        # 映像区是游戏代码，绝不可能放背包数据，直接跳过
            out.append((base, size))
        nxt = base + size
        if nxt <= addr:
            break
        addr = nxt
    return out


def clip_regions(regs: list[tuple[int, int]], lo: int, hi: int) -> list[tuple[int, int]]:
    """把区域列表裁到 [lo, hi) 窗口内（与窗口没有交集的原样丢掉）。

    "热点窗口扫描"就靠它：不用重新枚举内存，只在 VirtualQueryEx 结果里留下与
    上次命中地址相近的那一小段，剩下的 13 GB 一次都不读。
    """
    out = []
    for b, s in regs:
        nb, ne = max(b, lo), min(b + s, hi)
        if ne > nb:
            out.append((nb, ne - nb))
    return out


def hot_windows(regs: list[tuple[int, int]], spots: list[int], span: int) -> list[tuple[int, int]]:
    """把若干"热点地址"各自 ±span 的窗口求并集，再裁到区域列表里。

    为什么要多个：同一份单位数组在内存里有 A/B 两个别名（实测 0x7806xxxxxx 与
    一个 0x11xxxxxxxx 的高地址都指向同一份内容）。两个别名里，0x7806 那一个
    跨进程重启只漂几 MB（0x7806ba133c / 0x7806c5233c / 0x7806a2433c …），
    高地址那一个会跳几十 GB。所以先试一组地址，哪个在窗口里就命中哪个。
    """
    wins = sorted((s - span, s + span) for s in spots if s)
    merged: list[list[int]] = []
    for lo, hi in wins:                 # 窗口重叠就合并，避免同一段内存被重复读
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    out: list[tuple[int, int]] = []
    for lo, hi in merged:
        out.extend(clip_regions(regs, lo, hi))
    return out


def read_bytes(h, addr: int, size: int) -> bytes | None:
    buf = (ctypes.c_ubyte * size)()
    got = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)):
        return None
    return bytes(buf[:got.value])


def read_np(h, addr: int, size: int) -> "np.ndarray | None":
    """把一段内存直接读进 numpy 缓冲（扫描专用热路径）。

    为什么单独开一条：`(c_ubyte*n)()` 会先把整块缓冲**清零**，`bytes(buf)` 再**拷一遍**，
    60 MB 的块光这两步就把吞吐压到 54 MB/s；换成 np.empty（不清零）+ 直接给指针，
    实测 3300 MB/s（同一块内存、同一进程，实测数据见说明文档）。
    """
    if size <= 0:
        return None
    buf = np.empty(size, dtype=np.uint8)
    got = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr),
                                 buf.ctypes.data_as(ctypes.c_void_p), size, ctypes.byref(got)):
        return None
    return buf if got.value >= size else buf[:got.value]


# ------------------------------------------------------------------ 存档侧：单位物品编号序列
@dataclass
class SaveUnit:
    person: int
    name: str
    seq: list[int]          # 内存里应出现的编号序列（按格序）
    index: int = 0          # 在存档里的单位序号（0 起）
    stats: tuple = ()       # 记录头属性组指纹（= dw 存档 stats，9 个 u16，跟背包无关）


def load_save_units_stable(path: str, require_all_known: bool = True,
                           tries: int = 3) -> list[SaveUnit]:
    """读存档的"稳定版"：读的过程中文件若被游戏改写（自动存档随时可能发生），就重读。

    为什么需要：自动存档会在战斗结束后直接覆盖文件。如果正好读到"写了一半/刚换内容"的
    存档，锚点和工作台里真实阵容对不上，扫描会 0 命中，看起来像"程序坏了"。
    实测就撞到过一次：同一路径两次读，一次 6 个单位、一次 10 个单位。
    """
    import os
    import time as _time
    for _ in range(max(1, tries)):
        try:
            m0 = os.stat(path).st_mtime_ns
        except OSError:
            return load_save_units(path, require_all_known)
        units = load_save_units(path, require_all_known)
        try:
            m1 = os.stat(path).st_mtime_ns
        except OSError:
            return units
        if m0 == m1:
            return units
        _time.sleep(0.15)          # 让游戏把文件写完再读
    return load_save_units(path, require_all_known)


def load_save_units(path: str, require_all_known: bool = True) -> list[SaveUnit]:
    f = L.SaveFile(path)
    sf = C.SaveFile(bytes(f.payload))
    out: list[SaveUnit] = []
    for u in sf.units():
        seq, unknown = [], 0
        for it in C.unit_items(sf, u):
            k = f"{it.id:08x}"
            if k in _CODE_BY_IID:
                seq.append(_CODE_BY_IID[k])
            else:
                unknown += 1
        if seq and (unknown == 0 or not require_all_known):
            # stats：存档里每人固定的属性组，内存记录头同样有一份（见 REC_SHIFT 注释），
            # 拿它做"不依赖背包"的身份指纹。
            out.append(SaveUnit(u.person, unit_name(u.person), seq, len(out),
                                tuple(int(v) for v in (u.stats or ()))))
    return out


# ------------------------------------------------------------------ 定位
@dataclass
class Ladder:
    """一条地址梯：base0 + 每个单位自己的槽位 × 0x8B0。

    **单位在内存里的槽位顺序不等于存档里的队伍顺序**（实战：6 人队伍时替补占 0..8，
    招到第 10 个人时他被放在第 55 槽，另一个角色乌尔坦德在第 23 槽）。
    所以不能再用"存档第 i 个 → base0 + i*0x8B0"，改成按内容给每个单位认槽（`slots`）。
    """
    base0: int
    count: int
    addrs: list[int] = field(default_factory=list)
    codes: list[list[int]] | None = None
    slots: list[int] | None = None       # 每个单位实际落在第几槽（-1 = 还没认出来）

    def slot(self, unit_index: int) -> int:
        """该单位的内存槽号：认出来了就用它，没认出来就退回"存档顺序=槽位顺序"。"""
        if self.slots and 0 <= unit_index < len(self.slots) and self.slots[unit_index] >= 0:
            return self.slots[unit_index]
        return unit_index

    def addr(self, unit_index: int) -> int:
        return self.base0 + self.slot(unit_index) * STRIDE


__all__ = []          # 占位：模块内工具函数都在下面按需引用

# 内存单位表的槽位上限：表是"按角色固定序号"的大数组，不是队伍列表。
# 实测：哥莱亚斯在第 55 槽、乌尔坦德在第 23 槽；招募到第 12 个人（马克西姆）时
# 落到了**第 136 槽** —— 原来取 96 就会把他判成"没有块"。512 槽 = 512*0x8B0 ≈ 1.1 MB
# 的窗口，逐槽读一条 240 B 只有 123 KB，毫秒级，放宽不影响速度。
MAX_SLOTS = 512

# ---- 单位记录的真实结构（背包之外的部分，本轮实测结论） -------------------
# 一条单位记录其实不是从"背包首格"开始的，而是从 **背包地址 - 360** 开始的
# 0x8B0 字节，30 格背包落在记录内 +360 处。实测依据：
#   把 12 个角色的存档 stats 当作签名去全槽窗口里反查，每个人恰好命中 1 次，
#   而且全部落在"自己背包槽的前一块的 1864 偏移"上（= 自己背包地址 - 360）。
# 记录头字段（用迪托利希的游戏内面板核对过）：
#   record + 0x00  9 × u16  固定属性组，与存档 SaveUnit.stats 完全同构
#                           例：迪托利希 [16,17,9,21,21,20,11,13,20]
#   record + 0x18  u16      **当前 HP**（不是上限！2026-11-16 转职实测：19:34 由 14 → 48，
#                           与存档里同源 hp 字段逐个对得上；受伤的人两边差 1~2 = 还在掉）
#   record + 0x1E  u16      等级（迪托利希 36，与截图 LV36 一致）
# 到目前为止 360 字节记录头里**没有找到最大 HP**：它要么由 属性组 + 兵种 现算，
# 要么存在别的结构里（165 号字段 +0x1a 曾短暂出现过与当前 HP 相同的值，转职后回落 0）。
#   record + 0x1E  u16      等级（迪托利希 36，与截图 LV36 一致）
# 关键价值：属性组**逐人唯一、且与背包内容无关** —— 玩家用道具、换道具、清空背包，
# 这一段都不变。所以它能当"不靠背包认人"的身份指纹，也是背包被游戏改掉时
# 地址梯不该判失效的依据。
REC_SHIFT = -360          # 记录起点相对背包地址的偏移
REC_FP_COUNT = 9          # 记录头 9 个 u16 = 属性组
REC_HP_OFF = 24           # 记录 +0x18 = **当前 HP**（战斗/治疗随时改它，绝不能当身份依据）
REC_LV_OFF = 30           # 记录 +0x1E = 等级
# 记录 +0x16 是**自身槽号 + 1**（实测 2026-11-15：槽 0→1、槽 23→24、槽 55→56、
# 槽 136→137、槽 167→168；临时单位池整体从 168 搬到 207 之后，207 号块里也同步变成 208）。
# 这是整套结构里唯一一个"块自己承认自己在第几格"的字段，等于第二道白送的自校验：
#   · 认槽时用来打破属性组打平的僵局；
#   · 也能用来察觉"整张表搬家"（全部块的自身槽号一起偏移）。
REC_SELF_OFF = 22         # 记录 +0x16 = 自身槽号 + 1
FP_LIMIT = 255            # 属性组取值都远小于它；超出说明读到了别的结构（越界垃圾）

# 宽泛锁定参数：属性组不是"逐位必须相等"，而是允许整体小幅漂移。
# 实测（2026-11-15 一晚真实游戏）：这 9 个数是**个人基础属性**（不含职业/装备加成），
# 每升 1 级，成长到的几项各 +1 —— 迪托利希 36→37 级时差异合计 +4；
# 艾丝梅拉尔达 31→32 级时 +5。用道具、换装、清背包都不会动它。
# 所以容差要能盖住"存档还没刷新前的那几级"，并且必须靠**相对唯一性**担保：
# 自己的槽是全部槽里最像的那个，且第二名要明显更差。绝对阈值只是防垃圾读数的上限。
FP_SUM_TOL = 24           # 9 个字段合计允许的漂移（实测每级约 4~5，够盖 5 级）
# 注意：这 12 个人里最相像的一对（努蒂奴 / 马克西姆）属性组差异只有 9，
# 伊欧 / 诺克裘拉 只有 10 —— 所以"唯一最佳"的余量必须小，否则升级漂移一多就误判失效。
# 误判失效的代价很小：等级变化不改背包，判据会退回 `ladder_codes_match` 照样成立。
FP_MARGIN = 2             # 第二名至少要比自己槽差这么多，才敢用属性组判据
FP_FIELD_HINT = 100       # 单字段合理上限（基础属性不会超过这个量级）

# ---- 武器熟练度（2026-11-16 解出，15 人 × 全部武器 82/82 严格对上）----------
# 记录 +0x2E 起是一串 u16，每 2 字节一个"武器槽"，与存档 SaveUnit.masteries 的
# [(武器idx, 点数)] 一一同构：熟练度(idx) = 记录头 + 0x2E + 2 * idx。
# 定址依据：枚举基址 0x20..0x60 做严格比对，只有 +0x2E 全对（82 对 / 0 错），
# 相邻候选 +0x2A / +0x32 各只对 3 个、错 79 个。
# 训练格斗场那次实测到涨的正是这里的 w2/w3/w4（迪托利希 283/289/155 逐位相等）。
REC_MASTERY_OFF = 0x2E      # 熟练度数组在记录内的起点
REC_MASTERY_COUNT = 13      # 存档里出现过的最大槽号是 12 → 共 13 个槽
MASTERY_MAX = 9999          # 存档实测最大 2560（S 级），留足余量再当越界判据

# 13 个槽到底是什么——这张表来自 fefw_save.SKILL_NAMES（上游逆向的结论，
# 并且已被实测对上：穆 槽4=2560 界面显示"拳 C+"，槽4 正是 Fists；
# 迪托利希 槽0=2560 显示"剑 S"，槽0 正是 Swords）。
MASTERY_NAMES = ["剑", "枪", "斧", "弓", "拳", "黑魔法", "白魔法", "特殊",
                 "步兵", "骑马", "重装", "飞行", "指挥"]
# 等级阶梯来自 fefw_save.SKILL_THRESHOLDS（注释写明 S+ = 2560 已在游戏内确认）
MASTERY_THRESHOLDS = [0, 20, 35, 80, 220, 440, 690, 910, 1215, 1510, 1960, 2560]
MASTERY_GRADES = ["E", "E+", "D", "D+", "C", "C+", "B", "B+", "A", "A+", "S", "S+"]


def mastery_grade(value: int) -> str:
    """点数 → 等级字母（E … S+）。"""
    idx = 0
    for i, t in enumerate(MASTERY_THRESHOLDS):
        if value >= t:
            idx = i
    return MASTERY_GRADES[idx]


# ---------------------------------------------------------------- 合理性区间
# 用途：扫描/认槽时的"宽口径常识过滤"，剔除不可能属于真人的块。
# 背景（2026-11-16 实测）：未收编 NPC 会随主角团等级一起上涨，
# 所以属性组本身会漂移、不能再当"绝不变化"的绝对指纹；
# 但**取值范围的常识**始终成立——等级不会是 0 或 65535，HP 不会是负数，
# 单项属性不会大到几千。垃圾块（函数指针、文本、别的结构）正是靠这些越界值露馅。
# 口径故意放得很宽：已见最高等级 45、最高 HP 53、单项属性 24，
# 这里分别留到 99 / 999 / 99，宁可漏过噪声，不能误杀真人。
PLAUSIBLE_LV_MAX = 99     # 等级上限（满级未知，但一定远小于此）
PLAUSIBLE_HP_MAX = 999    # 当前 HP 上限（0 也合法：阵亡/被击倒的块照样得认得出是人）
PLAUSIBLE_STAT_MAX = 99   # 单项基础属性上限（0 是合法的，例如哥莱亚斯有一项为 0）


def fingerprint_score(fp, want) -> int | None:
    """两组属性组的"差异度"（9 个字段绝对差之和）；结构不符返回 None。

    用差异度而不是逐位相等，是为了容忍转职/职业成长修正带来的整体小幅变化；
    差异度越小越像，配合"唯一最佳"约束就能既宽泛又不认错人。
    """
    if not fp or not want or len(fp) != len(want):
        return None
    if any(v < 0 or v > FP_LIMIT for v in fp) or any(v < 0 or v > FP_LIMIT for v in want):
        return None
    return sum(abs(int(a) - int(b)) for a, b in zip(fp, want))


def read_fingerprint(h, bag_addr: int) -> tuple[int, ...] | None:
    """读一条单位记录的"属性组指纹"（9 个 u16）；结构不像就返回 None。

    用 FP_LIMIT 过滤：真实单位的属性组都是小整数，而槽号窗口越界后会读到文本/指针
    这类大数值（实测出现 65535、15820、42184 之类），必须挡掉，否则会认错人。
    """
    size = REC_FP_COUNT * 2
    raw = read_bytes(h, bag_addr + REC_SHIFT, size)
    if not raw or len(raw) < size:
        return None
    fp = struct.unpack_from("<%dH" % REC_FP_COUNT, raw, 0)
    if not any(fp) or any(v > FP_LIMIT for v in fp):
        return None
    return tuple(int(v) for v in fp)


def read_record(h, bag_addr: int, strict_lv: bool = True) -> dict:
    """读单位记录头三件套：属性组 + 当前 HP + 等级（给快照/诊断用）。

    注意：**当前 HP 一战斗就变，等级随升级变**，两者都不能当身份；
    属性组（+0x00 的 9 个 u16）在一场游戏里基本不变——2026-11-16 转职实测
    （迪托利希 19:30）九项一个都没动，所以认人只用属性组。

    strict_lv=False 的用途（2026-11-17 阵亡抓包实测）：
      参加战斗的单位，其 +0x1E 会被游戏写成 294 / 552 / 295 这类越界值
      （同一块在战前是正常的 38 / 40 / 39）。严格口径会把这类块整条当垃圾丢掉，
      于是"死了的人"在监控里表现为"槽突然不存在"，阵亡漏采就是这么来的。
      监控/抓包这类只读探针必须传 False，认人改由 属性组 + self 负责。
      注意返回的 "lv" 此时可能不可信，调用方要按 lv_reliable 判断能不能显示。
    """
    size = REC_LV_OFF + 2
    raw = read_bytes(h, bag_addr + REC_SHIFT, size)
    if not raw or len(raw) < size:
        return {}
    fp = read_fingerprint(h, bag_addr)
    if fp is None:
        return {}
    rec = {"stats": list(fp),
           "hp": struct.unpack_from("<H", raw, REC_HP_OFF)[0],
           "lv": struct.unpack_from("<H", raw, REC_LV_OFF)[0],
           "self": struct.unpack_from("<H", raw, REC_SELF_OFF)[0],
           "lv_reliable": 1 <= struct.unpack_from("<H", raw, REC_LV_OFF)[0] <= PLAUSIBLE_LV_MAX}
    # 常识过滤：等级/HP/单属性越界的块一律当成"不是人"，直接不返回。
    # 这一条挡的是"结构看着像背包、但记录头是垃圾"的块（表外区域常见）。
    if strict_lv and not rec["lv_reliable"]:
        return {}
    # HP 允许 0：被击倒的单位就是 0，若把它当"不是人"，锚点会在有人倒下时莫名失效。
    if not (0 <= rec["hp"] <= PLAUSIBLE_HP_MAX):
        return {}
    if any(v > PLAUSIBLE_STAT_MAX for v in rec["stats"]):
        return {}
    return rec


def read_masteries(h, bag_addr: int) -> list[int] | None:
    """读该单位的武器熟练度表（13 个 u16）。

    只读、不依赖背包内容：熟练度与物品无关，换装/清空背包都不会动它，
    所以它既能显示，也能当"这一块确实是个真单位"的旁证。
    读到的值超出 MASTERY_MAX 就直接放弃（说明这块不是单位记录）。
    """
    size = REC_MASTERY_COUNT * 2
    raw = read_bytes(h, bag_addr + REC_SHIFT + REC_MASTERY_OFF, size)
    if not raw or len(raw) < size:
        return None
    vals = [struct.unpack_from("<H", raw, i * 2)[0] for i in range(REC_MASTERY_COUNT)]
    if any(v > MASTERY_MAX for v in vals):
        return None
    return vals


def write_mastery(h, bag_addr: int, idx: int, value: int) -> bool:
    """把第 idx 个武器槽的熟练度写成 value（两个字节）。

    写完**立刻回读校验**：这类写入没有返回值，只有回读才能证明真的生效；
    调用方据此在日志里给出"改前 → 改后"，出错时有据可查。
    """
    if not (0 <= idx < REC_MASTERY_COUNT) or not (0 <= value <= 0xFFFF):
        return False
    addr = bag_addr + REC_SHIFT + REC_MASTERY_OFF + idx * 2
    if not write_bytes(h, addr, struct.pack("<H", int(value))):
        return False
    back = read_bytes(h, addr, 2)
    return bool(back) and struct.unpack_from("<H", back, 0)[0] == int(value)


def record_plausible(h, bag_addr: int) -> bool:
    """该块是否"像个人"（等级/HP/9 项属性都在常识范围内）。

    给扫描候选做二次筛查用：能通过背包结构校验的垃圾块（例如别处的一段数字表）
    往往在等级或 HP 上露馅，这一步几乎零成本。
    """
    return bool(read_record(h, bag_addr))


def read_self_index(h, bag_addr: int) -> int | None:
    """读记录里的"自身槽号 + 1"（+0x16）；不在合理范围就返回 None。

    真实单位的这个值恒等于"所在槽号 + 1"，所以它能当零成本的自校验：
    拿它和槽号对一下就知这块是不是真的坐在它该坐的位置上。
    """
    raw = read_bytes(h, bag_addr + REC_SHIFT + REC_SELF_OFF, 2)
    if not raw or len(raw) < 2:
        return None
    v = struct.unpack_from("<H", raw, 0)[0]
    return v if 1 <= v <= MAX_SLOTS else None


def slot_self_ok(h, base0: int, slot: int) -> bool | None:
    """该槽的"自身槽号"是否与槽号对得上；读不到返回 None（不参与判断）。"""
    v = read_self_index(h, base0 + int(slot) * STRIDE)
    if v is None:
        return None
    return v == int(slot) + 1


def slot_fingerprints(h, base0: int, slots) -> dict[int, tuple[int, ...]]:
    """只读指定槽位的属性组（比铺满 512 槽便宜，L0 校验走这条）。"""
    out: dict[int, tuple[int, ...]] = {}
    for k in slots or []:
        if k is None or int(k) < 0:
            continue
        fp = read_fingerprint(h, base0 + int(k) * STRIDE)
        if fp is not None:
            out[int(k)] = fp
    return out


def all_fingerprints(h, base0: int, max_slots: int = MAX_SLOTS) -> dict[int, tuple[int, ...]]:
    """整张表的属性组（认新角色时用）。"""
    return slot_fingerprints(h, base0, range(max_slots))


def enumerate_slots(h, base0: int, max_slots: int = MAX_SLOTS) -> dict[int, list[int]]:
    """从 base0 起按 0x8B0 逐个槽读 30 格，返回 {槽号: 编号列表}（只收结构合法的非空块）。"""
    out: dict[int, list[int]] = {}
    size = CELLS * CELL_BYTES
    for k in range(max_slots):
        raw = read_bytes(h, base0 + k * STRIDE, size)
        if not raw or len(raw) < size:
            continue
        if any(raw[i * 8 + 5] or raw[i * 8 + 6] or raw[i * 8 + 7] for i in range(CELLS)):
            continue                       # b5..b7 恒 0，说明这不是物品格
        ids = [raw[i * 8] | (raw[i * 8 + 1] << 8) for i in range(CELLS)]
        if not any(ids):
            continue                       # 空格：单位不在队里 / 没有物品
        out[k] = ids
    return out


def resolve_slots(h, base0: int, units: list[SaveUnit],
                  tolerance: int = SEQ_TOLERANCE,
                  slots_map: dict[int, list[int]] | None = None,
                  fps: dict[int, tuple[int, ...]] | None = None) -> list[int]:
    """给每个存档单位认一个内存槽位；认不出来给 -1。

    做法：把"所有 (单位, 槽, 距离)"里距离够小的配对按距离从小到大贪心分配，
    一个槽只给一个单位。这样"努蒂奴（224,641）"和"哥莱亚斯（224,235,641）"这种
    编号很像的两个角色也不会互相抢错（距离 0 的配对被优先锁死）。

    fps：记录头属性组（槽号 → 9 个 u16）。给了就优先按它认人 ——
    属性组与背包无关，玩家换过道具之后依然能认，也避免"两人背包几乎一样"时抢错。
    """
    if slots_map is None:
        slots_map = enumerate_slots(h, base0)
    if not slots_map:
        return [-1] * len(units)
    if fps is None:
        fps = slot_fingerprints(h, base0, slots_map.keys())
    pairs = []
    for i, u in enumerate(units):
        want = _seq_padded(u.seq)
        want_fp = tuple(getattr(u, "stats", None) or ())
        # 先给这个单位找"属性组最像的槽"。要求**唯一最佳**：第二名并列就不用指纹，
        # 免得两个人属性组相近时抢错（后期 4 队 40+ 人时这条尤其重要）。
        fp_pick = None
        if want_fp:
            scored = sorted((fingerprint_score(fp, want_fp), k) for k, fp in fps.items()
                            if fingerprint_score(fp, want_fp) is not None)
            # 唯一最佳：最优 ≤ 容差，且第二名要比它差至少 FP_MARGIN。
            # 升级会让属性组整体 +几，所以这里不能再用"逐位相等"，靠相对余量担保。
            if scored and scored[0][0] <= FP_SUM_TOL and scored[0][0] >= 0:
                top = scored[0][0]
                # 并列最优（两个块属性组一模一样，后期 40 人时可能出现）时，
                # 用记录 +0x16 的"自身槽号"打破僵局：真正坐在该位置的那块才是本人。
                same = [k for sc, k in scored if sc == top]
                if len(same) > 1:
                    ok = [k for k in same if slot_self_ok(h, base0, k)]
                    if len(ok) == 1:
                        fp_pick = (ok[0], top)
                if fp_pick is None and (len(scored) == 1 or scored[1][0] > top + FP_MARGIN):
                    fp_pick = (scored[0][1], top)
        for k, ids in slots_map.items():
            if fp_pick and k == fp_pick[0]:
                # 属性组唯一最佳 → 排在最前（rank 0），差异度越小越优先
                pairs.append((0, fp_pick[1], i, k))
                continue
            d = min(_seq_distance(want, (_seq_padded(ids))),
                    _bag_distance(want, _seq_padded(ids)))
            if d <= tolerance:
                pairs.append((1, d, i, k))
    pairs.sort()
    res = [-1] * len(units)
    used_u, used_s = set(), set()
    for _rank, _d, i, k in pairs:
        if i in used_u or k in used_s:
            continue
        res[i] = k
        used_u.add(i)
        used_s.add(k)
    return res


def build_roster(paths: list[str]) -> dict[int, dict]:
    """把一批存档文件拼成"角色名册"：{person: {"name": 名字, "seqs": [多份历史背包]}}。

    为什么要多份快照：一个角色在不同存档里背包不同（用了道具、换了装备）。
    只留一份（比如第一次见到的那份）就会像实测那样 —— 杨界、伊欧当前的背包和那份快照
    差几件，整条槽贴不上名字。多留几份，历史任何一个时刻对得上就能认。
    """
    roster: dict[int, dict] = {}
    for p in paths:
        try:
            units = load_save_units_stable(p)
        except Exception:
            continue
        for u in units:
            item = roster.setdefault(u.person, {"name": u.name, "seqs": []})
            seq = list(u.seq)
            if seq not in item["seqs"]:
                item["seqs"].append(seq)
    return roster


def identify_slots(h, base0: int, roster: dict[int, dict],
                   tolerance: int = 2, max_slots: int = MAX_SLOTS) -> dict[int, dict]:
    """枚举整张单位表，再拿名册给每个非空槽贴名字。

    返回 {槽号: {"ids":..., "person":.., "name":.., "dist":..}}；贴不上名字的 name 为 None。
    **不需要把某个角色招募到队里**：只要历史存档里出现过他，就能靠背包内容认出来。
    """
    out: dict[int, dict] = {}
    for slot, ids in enumerate_slots(h, base0, max_slots).items():
        want = _seq_padded(ids)
        best = None
        for person, item in roster.items():
            for seq in item["seqs"]:
                d = min(_seq_distance(want, _seq_padded(seq)),
                        _bag_distance(want, _seq_padded(seq)))
                if d <= tolerance and (best is None or d < best[0]):
                    best = (d, person, item["name"])
        out[slot] = {"ids": ids, "person": best[1] if best else None,
                     "name": best[2] if best else None, "dist": best[0] if best else None}
    return out


def _cells_of(raw: bytes) -> list[tuple[int, int, int, int]]:
    """(编号, b2, b3, b4) 列表。"""
    out = []
    for k in range(min(CELLS, len(raw) // CELL_BYTES)):
        c = raw[k * 8: k * 8 + 8]
        out.append((c[0] + c[1] * 256, c[2], c[3], c[4]))
    return out


def block_ok(h, addr: int) -> bool:
    """结构校验：30 格每格 b5..b7=0、编号在范围、至少一格非空。"""
    raw = read_bytes(h, addr, CELLS * CELL_BYTES)
    if not raw or len(raw) < CELLS * CELL_BYTES:
        return False
    nz = 0
    for k in range(CELLS):
        c = raw[k * 8: k * 8 + 8]
        if c[5] or c[6] or c[7]:
            return False
        idx = c[0] + c[1] * 256
        if idx > 4000:
            return False
        if idx:
            nz += 1
    return nz > 0


def _scan_one_region(h, base: int, size: int, plans, hits: dict[int, list[tuple[int, int]]],
                     min_match: int) -> None:
    """扫描单个区域，命中写进 hits（线程安全靠 GIL + 每线程独立 list 后合并）。"""
    off = 0
    while off < size:
        take = min(CHUNK, size - off)
        read_len = min(take + 8, size - off)
        buf = read_np(h, base + off, read_len)          # 免拷贝读，见 read_np 注释
        if buf is None:
            off += take
            continue
        arr = buf
        for align in (0, 4):
            n = (arr.size - align) // 8
            if n < min_match:
                continue
            a = arr[align: align + n * 8].reshape(n, 8)
            idx = a[:, 0].astype(np.uint16) + a[:, 1].astype(np.uint16) * 256
            limit = n - min_match + 1
            for ui, pat in plans:
                p = np.nonzero(idx[:limit] == pat[0])[0]        # 只比较首格
                for q in p:
                    if all(idx[q + i] == pat[i] for i in range(1, len(pat))):
                        hits.setdefault(ui, []).append((base + off + align + int(q) * 8, min_match))
        off += take


def scan_anchors(h, units: list[SaveUnit], min_match: int = MIN_MATCH,
                 only_type: int | None = None, threads: int = 6) -> dict[int, list[tuple[int, int]]]:
    """
    多线程分区扫描锚点。性能要点：
      · 每块 chunk 只算一次"8 字节格表"（b0b1 组成编号），之后各单位只做 1 次数组比较
      · 先把首格命中筛成很小的候选集合，再逐个验证第 2/3 格
      · 4 字节双对齐（物品区起始不保证 8 对齐），chunk 之间重叠 8 字节防止跨边界漏检
      · ReadProcessMemory 是内核调用，ctypes 调用期间释放 GIL，故多线程能真正并行读
    only_type: 只扫某种 Region Type（0x40000=MEM_MAPPED / 0x20000=MEM_PRIVATE），None=全部
    """
    plans = [(u.index, u.seq[:min_match]) for u in units]
    regs = regions(h, only_type)
    if not regs:
        return {}

    buckets: list[list[tuple[int, int]]] = [[] for _ in range(max(1, threads))]
    total = sum(s for _, s in regs) or 1
    acc = 0
    for b, s in regs:                     # 按容量做均衡切分
        buckets[min(int(acc / total * threads), threads - 1)].append((b, s))
        acc += s

    local: list[dict[int, list[tuple[int, int]]]] = [{} for _ in range(len(buckets))]
    import threading

    def work(i: int):
        for b, s in buckets[i]:
            _scan_one_region(h, b, s, plans, local[i], min_match)

    ths = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(len(buckets))]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    merged: dict[int, list[tuple[int, int]]] = {}
    for d in local:
        for k, v in d.items():
            merged.setdefault(k, []).extend(v)
    return merged


def _pick_seed(unit: SaveUnit) -> tuple[int, int]:
    """挑一个"最不可能撞车"的格子当种子。

    选编号最大的那一格：背包编号里 3000 以上的稀少，641（伤药）这种到处都是，
    拿它当种子会召回一堆候选。编号相同就取靠后的格（靠后更容易避开别的表的干扰）。
    """
    seq = list(unit.seq or [])
    cand = [k for k in range(min(CELLS, len(seq))) if seq[k]]
    if not cand:
        return -1, 0
    return max(cand, key=lambda k: (seq[k], k)), seq[max(cand, key=lambda k: (seq[k], k))]


def scan_unit_anchor(h, unit: SaveUnit, only_type: int | None = 0x40000, threads: int = 6,
                     progress=None, chunk_size: int = 64 << 20,
                     size_limit: int | None = None, seeds: int = 3,
                     tolerance: int = SEQ_TOLERANCE,
                     hotspot: "int | list[int] | None" = None,
                     hot_span: int = HOT_SPAN) -> list[int]:
    """只用一个单位的 30 格锚点串扫全内存，返回"该单位块起始地址"候选。

    为什么快：老实现把 N 个单位都当锚点，每块 chunk 做 N 次数组比较；
    这里每块只比较"种子格编号"这一个值，命中种子（通常个位数）以后才逐格核对整串。
    所以 numpy 侧的复杂度从 O(N·内存) 降到 O(内存)。

    为什么要"多个种子 + 容差"：
    游戏自己会改背包（用掉一本技能书，那格清空、后面的物品整体后移一格），这时锚点串里
    那本书已经不存在、每个物品的格号也整体偏了一位。做法是：
      · 取 3 个"编号最大"的格当种子（编号越大越稀有），避免种子正好是被用掉的那件；
      · 种子命中后，在 ±3 格范围内试 7 个窗口，用 `_seq_distance ≤ tolerance` 判真伪。
    既不依赖"30 格逐一相等"，也不会把随便一段内存当背包。

    进度：progress(已扫字节, 总字节) 会被频繁调用，界面据此画进度条。

    热点窗口（hotspot）：实测每次定位命中的地址几乎不搬家（同一进程内一直落在
    0x7806xxxxxx 那个映射块，两次重启之间也只差几 MB）。所以调用方可以把上次的
    命中地址传进来，本函数先只扫 hotspot ± hot_span，命中就立刻返回；
    窗口内无命中才扫全部区域。判据、容差、校验完全一致，只是读的内存少了一大截。
    """
    seq = [int(x) for x in (unit.seq or [])[:CELLS]]
    seq += [0] * (CELLS - len(seq))
    cand = [k for k in range(CELLS) if seq[k]]
    if not cand:
        return []
    # 种子：编号最大的若干个格（编号大的物品稀少，召回少）
    seed_list = sorted(cand, key=lambda k: (-seq[k], -k))[:max(1, seeds)]
    hits: list[int] = []
    lock = threading.Lock()

    def verify(start: int) -> bool:
        if start < 0:
            return False
        raw = read_bytes(h, start, CELLS * CELL_BYTES)
        if not raw or len(raw) < CELLS * CELL_BYTES:
            return False
        got = [raw[i * 8] | (raw[i * 8 + 1] << 8) for i in range(CELLS)]
        for i in range(CELLS):
            if raw[i * 8 + 5] or raw[i * 8 + 6] or raw[i * 8 + 7]:
                return False
        if sum(1 for v in got if v) == 0:
            return False
        if got == seq:
            return record_plausible(h, start)
        # 顺序口径（用掉一件→后移）与背包口径（游戏重排格子）取最小
        return (min(_seq_distance(seq, got), _bag_distance(seq, got)) <= tolerance
                and record_plausible(h, start))

    regs = regions(h, only_type)
    if size_limit:                       # 先只扫小区域（背包数组实测落在 33 MB 的映射块）
        regs = [(b, s) for b, s in regs if s <= size_limit]
    if not regs:
        return []

    def scan_regs(rs: list[tuple[int, int]]) -> list[int]:
        """在给定区域集合上跑完整的一轮"多 seed 扫描"，返回块起始地址候选。

        抽成内部函数是为了让"热点窗口"和"全量扫描"共用同一段热路径代码：
        两者的唯一区别只是 rs 里装了多少区域。
        """
        if not rs:
            return []
        total_r = sum(s for _, s in rs) or 1
        done = [0]
        buckets_r: list[list[tuple[int, int]]] = [[] for _ in range(max(1, threads))]
        acc = 0
        for b, s in rs:                  # 按容量做均衡切分
            buckets_r[min(int(acc / total_r * threads), threads - 1)].append((b, s))
            acc += s

        found: list[int] = []
        for seed_k, seed_val in [(k, seq[k]) for k in seed_list]:
            def work(bucket, seed_k=seed_k, seed_val=seed_val):
                local: list[int] = []
                pad = CELL_BYTES * (CELLS + tolerance + 4)
                for b, s in bucket:
                    off = 0
                    while off < s:
                        take = min(chunk_size, s - off)
                        read_len = min(take + pad, s - off)
                        raw = read_np(h, b + off, read_len)     # 免拷贝读，见 read_np 注释
                        if raw is None:
                            off += take
                            continue
                        arr = raw
                        for align in (0, 4):
                            n = (arr.size - align) // 8
                            if n <= 0:
                                continue
                            a = arr[align: align + n * 8].reshape(n, 8)
                            idx = a[:, 0].astype(np.uint16) + a[:, 1].astype(np.uint16) * 256
                            for p in np.nonzero(idx == seed_val)[0]:
                                p = int(p)
                                for d in range(-tolerance - 1, tolerance + 2):
                                    start = b + off + align + (p - seed_k - d) * 8
                                    if start < 0:
                                        continue
                                    if verify(start):
                                        local.append(start)
                        off += take
                        with lock:
                            done[0] += take
                        if progress:
                            try:
                                progress(min(done[0] / len(seed_list), total_r), total_r)
                            except Exception:
                                pass
                return local

            results: list[list[int]] = []
            ths = []
            for bucket in buckets_r:
                t = threading.Thread(target=lambda bk=bucket: results.append(work(bk)), daemon=True)
                ths.append(t)
                t.start()
            for t in ths:
                t.join()
            for r in results:
                found.extend(r)
        return sorted(set(found))

    # 热点窗口：上次命中的地址就在这附近（0x7806 那个别名跨重启只漂几 MB，
    # 实测 0x7806ba133c / 0x7806c5233c / 0x7806a2433c 三代的活梯都在同一个 0x7806 块里）。
    # 先只扫这些窗口，命中就直接返回，13~15 GiB 里剩下的一个字节都不读。
    spots = ([hotspot] if isinstance(hotspot, int) else list(hotspot or []))
    if spots:
        near = hot_windows(regs, spots, hot_span)
        if near:
            fast = scan_regs(near)
            if fast:
                return fast
    return scan_regs(regs)


def _pattern_source(units: list[SaveUnit], use_cache: bool) -> tuple[list[list[int]], list[list[int]] | None]:
    """扫描用哪套编号当锚。

    优先用缓存里的编号快照：它是"上一次亲眼读到的内存内容"，包含我们自己改出来的物品；
    存档文件则可能早就跟内存对不上了（改过东西之后必然对不上）。没有缓存快照才退回存档。
    返回 (每个单位的锚点序列, 快照序列或 None)。
    """
    save_seqs = [_seq_padded(u.seq) for u in units]
    if not use_cache:
        return save_seqs, None
    entries = (load_cache().get("ladders") or [])
    for e in entries:
        codes = e.get("codes")
        if codes and len(codes) == len(units):
            return [_seq_padded(c) for c in codes], codes
    return save_seqs, None


def locate_by_anchor(h, units: list[SaveUnit], anchor_index: int | None = None,
                     only_type: int | None = 0x40000, threads: int = 6,
                     progress=None, size_limit: int | None = None,
                     use_cache: bool = True,
                     tolerance: int = SEQ_TOLERANCE,
                     hotspot: "int | list[int] | None" = None,
                     hot_span: int = HOT_SPAN) -> tuple[list[Ladder], int]:
    """用一个单位的锚点定位整条梯，再用"每单位逐格编号"复核。

    返回 (有效梯列表, 候选数)。候选数用于日志：候选才几条就说明锚点足够唯一。
    重启游戏后区域尺寸/偏移都会变，缓存指纹（L1/L1b）常常失效，这时就走这里。

    tolerance：允许多件物品被游戏自己搬动过（换人、交给别人、卖掉）。
    交换一件物品在两个单位之间 = 各自距离 2，容差 3 只够 1~1.5 次交换；
    多人来回倒手时距离会累加，调用方可先用小容差、找不到再放大（见 mem_panel）。

    hotspot：上次定位成功时记下的地址。给了就先只扫它附近 ±hot_span 的窗口；
    窗口里认不出梯（例如游戏重启、地址整体搬家了）就自动退回全量扫描 ——
    所以它只加快速度，不会让定位变"找得到/找不到"取决于缓存。
    """
    if not units:
        return [], 0
    seqs, ref_codes = _pattern_source(units, use_cache)
    if anchor_index is None:                     # 默认用"物品最多"的单位当锚
        anchor_index = max(range(len(seqs)),
                           key=lambda i: sum(1 for v in seqs[i] if v))
    anchor_unit = SaveUnit(person=units[anchor_index].person, name=units[anchor_index].name,
                           seq=seqs[anchor_index], index=units[anchor_index].index)

    def derive(cands: list[int]) -> list[Ladder]:
        """把"锚点单位块起始地址"候选翻译成整条梯（逐单位认槽 + 结构/内容校验）。"""
        ladders: list[Ladder] = []
        seen: set[int] = set()
        for start in cands:
            # 候选命中点 = 锚点单位块的起始地址。锚点单位可能不在第 anchor_index 槽
            # （招募之后槽号是按角色固定分配的，例如新人在第 55 槽），所以要把
            # 0..MAX_SLOTS-1 每个可能的槽号都反推一次 base0，再用"逐单位认槽 + 内容校验"定案。
            #
            # 但不必真的从 0 试到 511：单位记录 +0x16 就写着"自己的槽号+1"（REC_SELF_OFF），
            # 读一下就能直接算出 base0。实测这一步原本要 1.1 s（512 次枚举槽），
            # 用自槽号后第一次尝试就中。读不出/值不合理时才退回全量枚举。
            ks: list[int] = []
            rec = read_record(h, start)
            own = (rec or {}).get("self")
            if isinstance(own, int) and 1 <= own <= MAX_SLOTS:
                ks.append(own - 1)
            ks.extend(k for k in range(MAX_SLOTS) if k not in ks)
            for k in ks:
                base0 = start - k * STRIDE
                if base0 < 0x10000 or base0 in seen:
                    continue
                slots_map = enumerate_slots(h, base0)
                slots = resolve_slots(h, base0, units, tolerance, slots_map)
                if any(s < 0 for s in slots):
                    continue                # 有单位认不出来 → 这个 base0 不是那份数组
                cand_ladder = Ladder(base0, len(units), [base0 + s * STRIDE for s in slots],
                                     ref_codes, slots)
                if not all(block_ok(h, a) and record_plausible(h, a) for a in cand_ladder.addrs):
                    continue
                # 定案用 ladder_alive：属性组指纹优先（玩家改过背包也能认对），背包内容兜底
                if not ladder_alive(h, cand_ladder, units, tolerance):
                    continue
                seen.add(base0)
                ladders.append(cand_ladder)
                break                       # 同一个命中点只收一个 base0
        return ladders

    def scan(spot: int | None) -> tuple[list[Ladder], int]:
        cands = scan_unit_anchor(h, anchor_unit, only_type=only_type,
                                 threads=threads, progress=progress, size_limit=size_limit,
                                 tolerance=tolerance, hotspot=spot, hot_span=hot_span)
        return derive(cands), len(cands)

    ladders, cand_n = scan(hotspot)
    if ladders or not hotspot:
        return ladders, cand_n
    # 热点窗口里有候选但一张梯都认不出来（地址漂出去了 / 陈旧的重复块）→ 老实全量再扫一遍
    ladders, cand_n = scan(None)
    return ladders, cand_n


def build_ladders(h, units: list[SaveUnit], anchors: dict[int, list[tuple[int, int]]]) -> list[Ladder]:
    """
    由锚点反推整条地址梯，并用结构校验确认。

    说明：内存里单位顺序与存档顺序一致（本存档实测：迪托利希→…→杨界 依次递增 0x8B0），
    因此 unit_index 直接用存档序号。
    """
    cands: dict[int, dict[int, int]] = {}         # base0 -> {unit: match_len}
    for ui, hits in anchors.items():
        for addr, k in hits:
            base0 = addr - ui * STRIDE
            if base0 < 0x10000:
                continue
            d = cands.setdefault(base0, {})
            d[ui] = max(d.get(ui, 0), k)

    ladders = []
    for base0, d in cands.items():
        if len(d) < 2:                      # 至少两个单位同时命中才认为是一条梯
            continue
        ok = [base0 + i * STRIDE for i in range(len(units))]
        if not all(block_ok(h, a) and record_plausible(h, a) for a in ok):
            continue
        # 关键：每个单位都要逐格编号一致，不能只凭"结构像个物品区"就收
        if not all(unit_codes_match(h, ok[i], units[i]) for i in range(len(units))):
            continue
        ladders.append(Ladder(base0, len(units), ok))
    # 命中格数多的排前面
    ladders.sort(key=lambda l: -sum(d.get(i, 0) for i in range(len(units)) for d in [cands[l.base0]]))
    return ladders


# ------------------------------------------------------------------ 读写单元
def read_unit(h, addr: int) -> list[tuple[int, int, int, int]] | None:
    raw = read_bytes(h, addr, CELLS * CELL_BYTES)
    return _cells_of(raw) if raw else None


def write_cell(h, addr: int, slot: int, idx: int, b2: int = 0, b3: int = 0, b4: int = 1) -> bool:
    """把某一格写成指定编号（默认全新、未装备）。"""
    if not (0 <= slot < CELLS):
        raise ValueError("槽位越界")
    target = addr + slot * CELL_BYTES
    # 8 字节 = 编号(2B) + b2..b7(6B)。格式串必须正好 8 字节、7 个字段：
    # 之前写成 "<HBBBB"（5 字段）却塞 7 个值，struct 直接抛异常 →
    # 「替换物品」每次都在写入前就炸了，界面看着像"没反应"。
    payload = struct.pack("<HBBBBBB", idx & 0xFFFF, b2 & 0xFF, b3 & 0xFF,
                          b4 & 0xFF, 0, 0, 0)
    ok = write_bytes(h, target, payload)
    if ok:
        _EDITS[target] = idx & 0xFFFF      # 登记，让后续校验认识这份"我们改过"的内存
    return ok


def unit_pattern(u, tight: bool = True) -> str:
    """把一个单位的 30 格转成 CE「Array of byte」可搜索的十六进制串。

    每格 8 字节 = 编号(2B，小端) + 已消耗耐久(1B) + 装备标记(1B) + 未知(1B) + 000000(3B)。
    存档里只知道编号；b2/b3/b4 随游戏状态变化，所以留 ?? 通配。b5..b7 实测恒 0，
    tight=True 时把它们固定成 00，串更"硬"，命中率更高。
    """
    seq = list(getattr(u, "seq", []) or [])
    out = []
    for i in range(CELLS):
        if i < len(seq) and seq[i]:
            code = seq[i] & 0xFFFF
            tail = ["00", "00", "00"] if tight else ["??", "??", "??"]
            out += [f"{code & 0xFF:02X}", f"{code >> 8:02X}", "??", "??", "??"] + tail
        else:
            # 空格理论上 8 个 0；不确定时（松版）留通配，免得整串因为一格非 0 就搜不到
            out += (["00"] * 8) if tight else (["??"] * 8)
    return " ".join(out)


def anchors_text(units, save_path: str) -> str:
    """生成给人看/给 CE 用的锚点串清单（一个单位两行：紧版 + 全通配版）。"""
    lines = [
        f"# 存档槽：{os.path.basename(save_path)}（{len(units)} 个单位）",
        f"# 存档路径：{save_path}",
        f"# 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "# 用法：CE → Memory Scan → Value Type = Array of byte → 勾 Hex →",
        "#       粘贴下面任一行（?? 为通配）→ First Scan；命中多条时拿下一个单位再比。",
        "# 每格 8 字节：编号(2B 小端) ?? ?? ?? 00 00 00（b2=已消耗耐久 b3=装备 b4=未知）",
        "",
    ]
    for k, u in enumerate(units):
        seq = list(getattr(u, "seq", []) or [])
        lines.append(f"[{k}] {u.name}  {len(seq)} 件  编号: {seq}")
        lines.append(unit_pattern(u, tight=True))
        lines.append(unit_pattern(u, tight=False))
        lines.append("")
    return "\n".join(lines)


def write_bytes(h, addr: int, payload: bytes) -> bool:
    """写内存：先直接写；只有真的没权限（错误码 5）才临时改页保护，写完立刻还原。

    之前是"每次都改成 PAGE_EXECUTE_READWRITE"，对一组运行中的游戏页反复翻保护没必要，
    也更容易踩到运行时自己的页状态（写崩游戏的怀疑点之一）。
    """
    wrote = ctypes.c_size_t(0)
    buf = ctypes.create_string_buffer(payload, len(payload))
    if k32.WriteProcessMemory(h, ctypes.c_void_p(addr), buf, len(payload), ctypes.byref(wrote)):
        return True
    if ctypes.get_last_error() != 5:               # 5 = ERROR_ACCESS_DENIED
        return False
    old = wt.DWORD(0)
    if not k32.VirtualProtectEx(h, ctypes.c_void_p(addr), len(payload), 0x04, ctypes.byref(old)):
        return False
    ok = k32.WriteProcessMemory(h, ctypes.c_void_p(addr), buf, len(payload), ctypes.byref(wrote))
    k32.VirtualProtectEx(h, ctypes.c_void_p(addr), len(payload), old.value, ctypes.byref(old))
    return bool(ok)


def repair_unit(h, addr: int, slots: list[int] | None = None,
                expect_stats=None) -> list[int]:
    """把指定格的 b2 清 0（修满耐久），只改 b2 字节，不动编号与装备标记。

    两道新防线（2026-11-16 加）：
      1. **写前复核**：记录头不像真人（等级/HP/属性越界，或读不到）就一格都不写。
         未收编 NPC 的属性会随主角团等级漂移，理论上存在"临时块碰巧像某个队友"的可能，
         这一条保证即便认错，也不会往一块垃圾/临时块上乱写。
      2. **写后回读**：每格写完立刻读回，确认 b0b1（物品编号）没变、b2 已归零，
         确认过才计入 done。写失败或写歪了不会静默算成功。
    expect_stats：调用方已知该块的身份时传入（9 个 u16），属性组不符就整块不写。
    """
    rec = read_record(h, addr)
    if not rec:
        return []
    if expect_stats is not None and list(rec["stats"]) != [int(v) for v in expect_stats]:
        return []
    done = []
    for slot in (range(CELLS) if slots is None else slots):
        cell = read_bytes(h, addr + slot * CELL_BYTES, CELL_BYTES)
        if not cell or len(cell) < CELL_BYTES:
            continue
        idx_before = cell[0] | (cell[1] << 8)
        if not cell[2]:                       # b2 已经是 0，不用动
            continue
        if write_bytes(h, addr + slot * CELL_BYTES + 2, b"\x00"):
            after = read_bytes(h, addr + slot * CELL_BYTES, CELL_BYTES)
            if after and (after[0] | (after[1] << 8)) == idx_before and after[2] == 0:
                done.append(slot)
    return done


# ------------------------------------------------------------------ 地址缓存（L0 快速定位）
CACHE_PATH = __file__.replace("mem_editor.py", "mem_cache.json")


def load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return {}


def load_hotspot() -> list[int] | None:
    """取上次定位成功时记下的一组地址（"热点"）。

    只用来加快下一次定位：给 locate_by_anchor 当扫描窗口的中心。
    拿不到（老缓存没有这个字段 / 文件坏了 / 从没定位成功过）就返回 None，
    调用方照常全量扫描，行为与以前完全一致。
    """
    try:
        c = load_cache()
        spots = c.get("hotspots")
        if isinstance(spots, list) and spots:
            return [int(v) for v in spots if v]
        v = c.get("hotspot")
        return [int(v)] if v else None
    except Exception:
        return None


def save_cache(ladders: list[Ladder], h, units: list[SaveUnit] | None = None, only_type=None) -> None:
    """
    缓存每条梯的"区域指纹"：base0 + 所在区域基址/尺寸/类型。

    另外把"此刻内存里的编号快照"(codes)一起存下来：重启游戏读档后，
    内存内容可能早就被我们改过、和存档文件不一致了，这时靠 codes 仍然能认出同一份数组。

    注意：**空列表不写缓存**。定位失败时把缓存清空等于删掉唯一一份"上次确认过的地址"，
    下次连 L0 都没得试，只能全量扫。

    另外 `slot_map` / `code_map` 是**合并**写入的（不是覆盖）：一次会话只看得见当前存档里
    的那几个单位，但如果覆盖，就白白丢掉"别的角色在第几槽"的认知 —— 用户换一份存档
    （Auto 只写出战的 6 人、Manual1 写全员 11 人）回来，L0 立刻又要降级。
    """
    if not ladders:
        return
    old = load_cache().get("ladders") or []
    old_slots, old_codes, old_fps = {}, {}, {}
    for e in old:                      # 旧缓存里的认知先全部捞出来（键统一成字符串）
        old_slots.update(e.get("slot_map") or {})
        old_codes.update(e.get("code_map") or {})
        old_fps.update(e.get("fp_map") or {})
    entries = []
    for l in ladders:
        base, size, rtype = region_of(h, l.base0) or (0, 0, 0)
        slot_map = dict(old_slots)
        code_map = dict(old_codes)
        fp_map = dict(old_fps)
        if units:
            l.codes = [codes_of(h, l.addr(u.index)) or _seq_padded(u.seq) for u in units]
            for u in units:
                slot_map[str(u.person)] = l.slot(u.index)
                code_map[str(u.person)] = codes_of(h, l.addr(u.index))
                want = tuple(getattr(u, "stats", None) or ())
                if want:
                    fp_map[str(u.person)] = list(want)
        entries.append({"base0": l.base0, "region_base": base, "region_size": size,
                        "region_type": rtype, "offset": l.base0 - base if base else 0,
                        "codes": l.codes, "slots": l.slots,
                        "slot_map": slot_map, "code_map": code_map, "fp_map": fp_map})
    try:
        old = load_cache()
        spots = [int(l.base0) for l in ladders]
        with open(CACHE_PATH, "w", encoding="utf-8") as fp:
            json.dump({"ladders": entries,
                       "hotspots": spots,                       # 新版：一组热点（A/B 双副本）
                       "hotspot": spots[0] if spots else old.get("hotspot")},
                      fp, indent=2, ensure_ascii=False)
    except Exception:
        pass


def region_of(h, addr: int) -> tuple[int, int, int] | None:
    addr0, mbi = 0, MBI()
    while k32.VirtualQueryEx(h, ctypes.c_void_p(addr0), ctypes.byref(mbi), ctypes.sizeof(mbi)):
        b, s = mbi.BaseAddress or 0, mbi.RegionSize
        if b <= addr < b + s:
            return b, s, mbi.Type
        nxt = b + s
        if nxt <= addr0:
            return None
        addr0 = nxt
    return None


def _seq_padded(seq) -> list[int]:
    s = [int(v) for v in (seq or [])[:CELLS]]
    return s + [0] * (CELLS - len(s))


def codes_of(h, addr: int) -> list[int] | None:
    """读出一个单位 30 格的编号（忽略 b2..b4 这些会变的值）。"""
    raw = read_bytes(h, addr, CELLS * CELL_BYTES)
    if not raw or len(raw) < CELLS * CELL_BYTES:
        return None
    return [raw[k * 8] | (raw[k * 8 + 1] << 8) for k in range(CELLS)]


def codes_equal(h, addr: int, codes: list[int], tolerance: int = SEQ_TOLERANCE) -> bool:
    """内存里的编号序列是否等于给定快照（同时要求每格 b5..b7 为 0、空位确实全 0）。

    同样允许"少一件/多一件"（游戏自己用掉物品会整体后移一格），否则快照一旦过时，
    这份地址就永久作废。
    """
    raw = read_bytes(h, addr, CELLS * CELL_BYTES)
    if not raw or len(raw) < CELLS * CELL_BYTES:
        return False
    codes = _seq_padded(codes)
    got = []
    for k in range(CELLS):
        c = raw[k * 8:k * 8 + 8]
        if c[5] or c[6] or c[7]:
            return False
        got.append(c[0] | (c[1] << 8))
    if got == codes:
        return True
    return min(_seq_distance(codes, got), _bag_distance(codes, got)) <= tolerance


def _unit_score(cells, seq: list[int]) -> int:
    """该块与存档串的匹配度：只看"编号出现在块里"，容忍装备位换格。"""
    have = {c[0] for c in cells if c[0]}
    return sum(1 for s in seq if s in have)


def _seq_distance(a: list[int], b: list[int]) -> int:
    """两条背包序列的"插入/删除"编辑距离（30 格全是整数编号，替换按 2 算）。

    为什么需要它：游戏自己也会改背包 —— 用掉一本耐久 1 的技能书，那格变空，
    后面的物品**整体后移一格**（实测杨界：第 6 格 484→0、第 7 格 486→484、第 8 格 503→486…）。
    这不是"地址错了"，是内容合法地变了。若判据要求 30 格逐一相等，这种变化会让
    整个地址永久判失效、扫描也搜不到（锚点串里那本书已经不存在了）。
    用编辑距离容忍"少了一件/多了一件"，既放行游戏的合法变化，又不至于把随便一段
    内存当成背包（随机序列要凑到距离≤3 几乎不可能）。
    """
    n, m = len(a), len(b)
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        for j in range(1, m + 1):
            cur[j] = min(prev[j] + 1,
                         cur[j - 1] + 1,
                         prev[j - 1] + (0 if a[i - 1] == b[j - 1] else 2))
        prev = cur
    return prev[m]


def _bag_distance(a: list[int], b: list[int]) -> int:
    """按"背包语义"比差异：只看每件物品有几个，**完全不看格子顺序**。

    为什么必须有这一条：游戏自己会搬格子。实测法毕欧的 474 从第 6 格挪到第 1 格
    （其余整串顺移），按顺序比对算出来是 4 处差异，判失效；但按背包语义差异是 0 ——
    道具还是那 13 件，一件没多一件没少。

    代价：顺序信息被忽略。所以它只是"另一种合法解释"，不是放宽阈值；
    真认错块的几率仍然极低（要在一段 240 字节里凑出同一批稀有编号）。
    """
    from collections import Counter
    ca, cb = Counter(a), Counter(b)
    return sum(abs(ca.get(k, 0) - cb.get(k, 0)) for k in set(ca) | set(cb))


def unit_codes_match(h, addr: int, unit: SaveUnit, tolerance: int = SEQ_TOLERANCE) -> bool:
    """逐格、按顺序比对编号（空位必须全 0，且每格 b5..b7 必须为 0）。

    这是「写之前」唯一可信的判据：
    以前用 _unit_score（只要求"编号在块里出现过"，且 2 个单位命中就收）会把
    别人的 240 字节恰好长得像的区域当成背包，写进去就是往任意内存乱写 —— 实测会把游戏写崩。

    例外一：**本程序自己写进去的格**允许与存档不同（否则改完一件物品，这份内存就再也
    "校验不过"，界面立刻把地址判失效）。判据是 `_EDITS`：写入时登记"这个地址被我写成了什么"，
    只要内存里现在正是那个值，就认为是我们改的，放行。
    例外二：**游戏自己造成的变化**——用掉消耗品（那格清空、后面整体后移一格）用
    `_seq_distance` 计；游戏自己重排格子（某件挪到最前、其余顺移）用 `_bag_distance` 计。
    两者**取最小**：只要能解释成"少了几件"或"挪了位置"，就算同一份背包。
    """
    raw = read_bytes(h, addr, CELLS * CELL_BYTES)
    if not raw:
        return False
    seq = list(getattr(unit, "seq", []) or [])
    seq = (seq + [0] * CELLS)[:CELLS]
    got = [raw[i * 8] | (raw[i * 8 + 1] << 8) for i in range(CELLS)]
    for i in range(CELLS):
        if raw[i * 8 + 5] or raw[i * 8 + 6] or raw[i * 8 + 7]:
            return False                   # b5..b7 恒 0，非 0 说明不是物品格
    for i in range(CELLS):
        if got[i] != seq[i]:
            cell_addr = addr + i * CELL_BYTES
            if _EDITS.get(cell_addr) == got[i]:
                continue                   # 我们自己写的那格
    if got == seq:
        return True
    return min(_seq_distance(seq, got), _bag_distance(seq, got)) <= tolerance


def ladder_codes_match(h, ladder: Ladder, units: list[SaveUnit],
                       tolerance: int = SEQ_TOLERANCE_MAX) -> bool:
    """整条梯逐单位比对；任何一个单位不像就整条作废。

    一个单位算"像"的条件：编号与存档一致（或与本次写过的格一致），**或者**与这条梯
    缓存下来的编号快照一致（后者用于"内存已经被我改过、不再等于存档"的情况）；
    两者都允许"多件道具被搬动"的编辑距离（写前闸门用放大档，多人换道具也不拦）。
    """
    for i, u in enumerate(units):
        a = ladder.addr(u.index)
        if unit_codes_match(h, a, u, tolerance):
            continue
        if ladder.codes and i < len(ladder.codes) and codes_equal(h, a, ladder.codes[i], tolerance):
            continue
        return False
    return True


def ladder_alive(h, ladder: Ladder, units: list[SaveUnit],
                 tolerance: int = SEQ_TOLERANCE_MAX,
                 fp_tol: int = FP_SUM_TOL) -> bool:
    """地址梯是否仍然可信：**属性组指纹优先，背包内容兜底**。

    这是"游戏自己改了背包"不再判失效的关键：玩家用道具、交换、清空格子，
    背包内容会变，但记录头那 9 个数（个人基础属性）不跟着背包变。

    判据分两层：
    1. 绝对上限：自己的槽和自己的属性组差异 ≤ fp_tol（升级会把属性组整体 +几，
       实测每级约 4~5，所以给 24 的余量）；
    2. 相对唯一：自己去比别的槽更像"自己"——自己的差异必须比别的槽都小，而且
       第二名要明显更差（≥ FP_MARGIN）。这一条才是真正的身份担保，光靠绝对阈值
       在 40+ 人时可能两个人同时落在容差内。
    """
    fps = slot_fingerprints(h, ladder.base0,
                            [ladder.slot(u.index) for u in units])
    if fps:
        for u in units:
            want = tuple(getattr(u, "stats", None) or ())
            if not _fp_unique(fps, ladder.slot(u.index), want, fp_tol):
                break
        else:
            return True                    # 全员属性组都在容差内且各自最像 → 成立
    return ladder_codes_match(h, ladder, units, tolerance)


def ladder_report(h, ladder: Ladder, units: list[SaveUnit]) -> list[str]:
    """给界面用：一条梯逐单位的核对结果，第一个不一致处写清楚（供 CE 对照）。"""
    lines = []
    for u in units:
        a = ladder.addr(u.index)
        raw = read_bytes(h, a, CELLS * CELL_BYTES)
        if not raw:
            lines.append(f"{u.name} @{a:#010x}  读不到（页可能已释放）")
            continue
        seq = list(getattr(u, "seq", []) or [])
        bad = None
        for k in range(CELLS):
            c = raw[k * 8:k * 8 + 8]
            got = c[0] | (c[1] << 8)
            want = seq[k] if k < len(seq) else 0
            if got != want or c[5] or c[6] or c[7]:
                bad = (k, want, got, c[5] | c[6] | c[7])
                break
        if bad is None:
            lines.append(f"{u.name} @{a:#010x}  30 格全部吻合")
        else:
            k, want, got, tz = bad
            extra = "（b5..b7 非 0）" if tz else ""
            lines.append(f"{u.name} @{a:#010x}  第 {k + 1} 格不符：存档 {want} / 内存 {got}{extra}")
    return lines


def _fp_unique(fps: dict[int, tuple[int, ...]], slot: int,
               want, fp_tol: int = FP_SUM_TOL, margin: int = FP_MARGIN) -> bool:
    """这个单位的属性组是否"只属于自己这个槽"。

    两道闸：①自己槽的差异 ≤ fp_tol（升级会让属性组整体 +几，所以给余量）；
    ②别的槽都不能比自己的槽更接近（且第二名要明显更差 ≥ margin）。
    只满足①不满足②时宁可判"不算"，退回背包判据 —— 这是 40+ 人时的防错关键。
    """
    mine = fingerprint_score(fps.get(slot), want)
    if mine is None or mine > fp_tol:
        return False
    rivals = [fingerprint_score(v, want) for k, v in fps.items() if k != slot]
    rivals = [r for r in rivals if r is not None]
    return (not rivals) or min(rivals) >= mine + margin


def _validate_ladder(h, base0: int, units: list[SaveUnit], codes=None,
                     tolerance: int = SEQ_TOLERANCE,
                     slots: list[int] | None = None,
                     fps: dict[int, tuple[int, ...]] | None = None,
                     fp_tol: int = FP_SUM_TOL) -> bool:
    """结构 + 内容双重校验，全部通过才允许使用这个地址。

    codes：缓存里的编号快照。存档与内存不一致时（我们自己改过物品），必须靠它认地址，
    否则会出现"改完一件东西就再也定位不上"的死结。
    slots：每个单位的内存槽号（招募后槽位不按队伍顺序，见 Ladder 注释）。
    tolerance：允许"道具被搬动过"的编辑距离；调用方先试小容差、再放大。
    fps：记录头属性组（槽号 → 9 个 u16）。只要每个单位的属性组都和存档逐一相等，
    这条梯就直接算有效 —— **完全不依赖背包内容**。这是"游戏自己改背包"时地址梯
    不该失效的根本依据（属性组不随用道具/换道具变化）。
    """
    ladder = Ladder(base0, len(units), [], codes, slots)
    if slots and any(s < 0 for s in slots):
        return False                    # 有单位没认出槽位，宁可不认这个地址
    if not all(block_ok(h, ladder.addr(u.index)) for u in units):
        return False
    if fps:
        ok = True
        for u in units:
            want = tuple(getattr(u, "stats", None) or ())
            if not _fp_unique(fps, ladder.slot(u.index), want, fp_tol):
                ok = False
                break
        if ok:
            return True
    return ladder_codes_match(h, ladder, units, tolerance)


def try_cache(h, units: list[SaveUnit]) -> tuple[list[Ladder] | None, str]:
    """
    L0：缓存地址直接读（同进程内，几毫秒）。
    L1：区域指纹重定位（游戏重启后区域基址变了，但区域尺寸+偏移往往不变）。
    校验不过就返回 None，由调用方降级到扫描。

    校验口径：与存档一致 **或** 与缓存里的编号快照一致。后者很关键 ——
    用户改过背包以后，内存本来就不再等于存档文件，只认存档会让地址永久失效。
    """
    c = load_cache()
    entries = c.get("ladders") or []
    if not entries:
        return None, "无缓存"

    def mk(e, base0: int) -> Ladder:
        """按角色 id 还原"这个单位在第几槽 + 它的物品快照"。"""
        smap = e.get("slot_map") or {}
        cmap = e.get("code_map") or {}
        old_slots = e.get("slots") or []
        slots, codes = [], []
        for i, u in enumerate(units):
            s = smap.get(str(u.person))
            if s is None and i < len(old_slots) and old_slots[i] is not None:
                s = old_slots[i]           # 老缓存没有 slot_map，退回按序数组
            slots.append(int(s) if s is not None else -1)
            codes.append(cmap.get(str(u.person)))
        return Ladder(base0, len(units), [], codes, slots)

    ladders = [mk(e, e["base0"]) for e in entries if e.get("base0")]
    # 有单位认不出槽位（这份存档里出现了缓存没见过的新角色）：现场按内容认一次，
    # 96 槽 × 240 B 只是几 KB 读取，比降级到全量扫描便宜得多。
    for l in ladders:
        if any(s < 0 for s in l.slots):
            res = resolve_slots(h, l.base0, units, SEQ_TOLERANCE_MAX)
            l.slots = [l.slots[i] if l.slots[i] >= 0 else res[i] for i in range(len(units))]
            l.grew_slots = True            # 认到新角色的槽位，值得写回缓存
        l.addrs = [l.addr(u.index) for u in units]
        # 记录头属性组：拿它做"不依赖背包"的校验（见 _validate_ladder 注释）
        l.fps = slot_fingerprints(h, l.base0, l.slots)
    # 两档容差：先按"只少/多一件"的小容差校验，没过再放大（多人换道具时距离会累加）
    good, tol = [], SEQ_TOLERANCE
    for tol in (SEQ_TOLERANCE, SEQ_TOLERANCE_MAX):
        good = [l for l in ladders
                if _validate_ladder(h, l.base0, units, l.codes, tol, l.slots,
                                    getattr(l, "fps", None))]
        if good:
            break
    if good:
        # 缓存里可能混进过"恰好像"的假梯（弱校验时代的遗留），这里逐条剔掉，
        # 并只把通过校验的地址写回缓存，避免下次再拿假地址去写；
        # 另外"这次才认出来的新角色槽位"也要写回，免得下次又现场认一遍。
        if len(good) != len(ladders) or any(getattr(l, "grew_slots", False) for l in good):
            save_cache(good, h, units)
        return good, (f"L0 缓存直接命中：{len(good)} 条地址梯"
                      f"（容差 {tol}）"
                      + (f"（丢弃 {len(ladders) - len(good)} 条校验不过的）"
                         if len(good) != len(ladders) else ""))

    # L1：按"同尺寸同类型区域 + 同偏移"重新定位
    found = []
    for e in entries:
        size, rtype, off = e.get("region_size", 0), e.get("region_type", 0), e.get("offset", 0)
        if not size:
            continue
        for b, s in regions(h, rtype):
            if s != size:
                continue
            cand = b + off
            cand_ladder = mk(e, cand)
            cand_fps = slot_fingerprints(h, cand, cand_ladder.slots)
            if _validate_ladder(h, cand, units, e.get("codes"), SEQ_TOLERANCE,
                                cand_ladder.slots, cand_fps):
                found.append(cand_ladder)
                break
    if len(found) >= 1:
        return found, f"L1 区域指纹重定位（{len(found)} 条梯，未做全量扫描）"

    # L1b：重启后区域尺寸也可能变，但区域内偏移往往不变 → 同偏移跨任意尺寸探测。
    # 代价约 0.1 秒（每个区域只读一次校验），比全量扫描便宜得多。
    for e in entries:
        rtype, off = e.get("region_type", 0), e.get("offset", 0)
        if not off:
            continue
        for b, _s in regions(h, rtype):
            cand = b + off
            cand_ladder = mk(e, cand)
            cand_fps = slot_fingerprints(h, cand, cand_ladder.slots)
            if _validate_ladder(h, cand, units, e.get("codes"), SEQ_TOLERANCE,
                                cand_ladder.slots, cand_fps):
                found.append(cand_ladder)
                break
    if len(found) >= 1:
        return found, f"L1b 同偏移重定位（{len(found)} 条梯，区域尺寸已变）"
    return None, "缓存失效"


# ------------------------------------------------------------------ 快照 / 撤销
class Snapshot:
    """改前快照：记录被写入的每个地址原值，可一键还原。

    2026-11-16 20:24 事故后的加固：原来的 undo() 是**盲写** —— 把 saved 里的地址
    逐个 Try 回原值，不管那些地址现在还是不是"当初那条记录"。只要游戏在两次操作之间
    搬过数组/释放过页面，就会把原值写进别人的结构里，轻则锚点失效，重则游戏闪退。
    现在 undo 必须给出一份"当前仍合法的记录范围"，范围外的地址一律跳过并计数。
    """

    def __init__(self, h):
        self.h = h
        self.saved: dict[int, bytes] = {}
        self.written: dict[int, bytes] = {}   # 我们写下去的"新值"，撤销时用来认自己
        self.skipped = 0          # 上一次 undo 里被各种校验挡掉的条数

    def remember(self, addr: int, size: int = CELL_BYTES) -> None:
        if addr in self.saved:
            return
        raw = read_bytes(self.h, addr, size)
        if raw:
            self.saved[addr] = raw

    def note_written(self, addr: int, after: bytes) -> None:
        """记下"这个地址被我们写成了什么"。

        撤销时要靠它认自己：只有当该地址现在**仍然是**我们写下的那几字节，
        才允许回写原值。若中间被游戏或别人改过，说明这块已经不由我们说了算，
        直接跳过 —— 否则就是拿一个过期的原值去覆盖一段新的内容。
        """
        self.written[addr] = after

    def undo(self, ranges: list[tuple[int, int]] | None = None,
             verify: bool = True, on_write=None) -> int:
        """把记录过的原值写回去，返回真正写成功的条数。

        ranges：允许写入的 `(起始, 结束)` 区间列表（都是当前校验过的单位记录范围）。
                传 None 等于放弃保护 —— 只在"地址刚刚校验过、绝无搬动可能"的场合用。
        verify：写完立刻回读比对，回读不一致的计入失败（不静默算了）。
        on_write：每写一条回调 `on_write(addr, raw, ok)`，给面板落盘审计日志用；
                  出事后能凭日志分辨"到底往哪写了什么"。
        """
        self.skipped = 0
        n = 0
        for addr, raw in self.saved.items():
            if ranges is not None and not any(lo <= addr and addr + len(raw) <= hi
                                              for lo, hi in ranges):
                self.skipped += 1
                if on_write:
                    on_write(addr, raw, False)
                continue
            # 只回写"仍是我们写下的那份内容"的地址：中途被别人改过就说明这块不归我们管了
            exp = self.written.get(addr)
            if exp and verify:
                cur = read_bytes(self.h, addr, len(exp))
                if not cur or cur != exp:
                    self.skipped += 1
                    if on_write:
                        on_write(addr, raw, False)
                    continue
            old = wt.DWORD(0)
            k32.VirtualProtectEx(self.h, ctypes.c_void_p(addr), len(raw), 0x40,
                                 ctypes.byref(old))
            buf = ctypes.create_string_buffer(raw, len(raw))
            wrote = ctypes.c_size_t(0)
            ok = k32.WriteProcessMemory(self.h, ctypes.c_void_p(addr), buf, len(raw),
                                        ctypes.byref(wrote))
            k32.VirtualProtectEx(self.h, ctypes.c_void_p(addr), len(raw), old.value,
                                 ctypes.byref(old))
            if ok and verify:
                back = read_bytes(self.h, addr, len(raw))
                ok = bool(back) and back == raw
            n += 1 if ok else 0
            if on_write:
                on_write(addr, raw, bool(ok))
        return n


# ------------------------------------------------------------------ 命令行
def cmd_list(h, units: list[SaveUnit], ladders: list[Ladder]) -> None:
    print(f"定位到 {len(ladders)} 条地址梯：")
    for i, l in enumerate(ladders):
        print(f"  梯{i}: base0={l.base0:#x}  单位数={l.count}")
    if not ladders:
        return
    for ui in range(ladders[0].count):
        print(f"\n=== 单位{ui}  {units[ui].name}   (person={units[ui].person})")
        for li, l in enumerate(ladders):
            cells = read_unit(h, l.addr(ui))
            if not cells:
                print(f"  梯{li} {l.addr(ui):#x}  不可读")
                continue
            items = []
            for k, (idx, b2, b3, b4) in enumerate(cells):
                if not idx:
                    continue
                tag = "★" if b3 else ""
                wear = f" 已消耗{b2}" if b2 else ""
                items.append(f"格{k}:{idx} {name_of(idx)}{tag}{wear}")
            print(f"  梯{li} {l.addr(ui):#x}  " + " | ".join(items))


def cmd_repair(h, ladders: list[Ladder], unit: int | None, dry: bool) -> None:
    snap = Snapshot(h)
    total = 0
    for l in ladders:
        targets = range(l.count) if unit is None else [unit]
        for ui in targets:
            a = l.addr(ui)
            cells = read_unit(h, a)
            if not cells:
                continue
            worn = [k for k, c in enumerate(cells) if c[0] and c[1]]
            if not worn:
                continue
            for k in worn:
                snap.remember(a + k * CELL_BYTES + 2, 1)
            if dry:
                print(f"[dry] 梯@{l.base0:#x} 单位{ui} 将修满格 {worn}")
            else:
                done = repair_unit(h, a, worn)
                total += len(done)
                print(f"梯@{l.base0:#x} 单位{ui} 已修满格 {done}")
    print(f"合计修改 {total} 格" + ("（dry-run 未写入）" if dry else ""))


def cmd_set(h, ladders: list[Ladder], unit: int, slot: int, idx: int, dry: bool) -> None:
    snap = Snapshot(h)
    for l in ladders:
        a = l.addr(unit)
        snap.remember(a + slot * CELL_BYTES)
        if dry:
            print(f"[dry] 梯@{l.base0:#x} 单位{unit} 格{slot} → {idx} {name_of(idx)}")
        else:
            ok = write_cell(h, a, slot, idx)
            print(f"梯@{l.base0:#x} 单位{unit} 格{slot} → {idx} {name_of(idx)}  {'成功' if ok else '失败'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", required=True, help="当前游戏已加载的存档文件路径")
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--repair-all", action="store_true")
    ap.add_argument("--repair-unit", type=int, default=None)
    ap.add_argument("--set", nargs=3, metavar=("UNIT", "SLOT", "IDX"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rescan", action="store_true", help="忽略地址缓存，强制重新扫描")
    ap.add_argument("--anchors", action="store_true",
                    help="只导出手动搜索用的锚点串（不需要游戏在跑）")
    args = ap.parse_args()

    load_code_table()
    units = load_save_units(args.save)
    print("存档单位：")
    for i, u in enumerate(units):
        print(f"  单位{i} {u.name}  {len(u.seq)} 件  {u.seq}")
    if args.anchors:
        txt = anchors_text(units, args.save)
        out = os.path.join(PROJ, "mem_anchors.txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write(txt)
        print(txt)
        print(f"\n已写入 {out}")
        return

    pid = args.pid or find_pid()
    if not pid:
        print("找不到 fefw.exe")
        return
    h = k32.OpenProcess(0x0400 | 0x0010 | 0x0020 | 0x0008, False, pid)
    if not h:
        print("OpenProcess 失败", ctypes.get_last_error())
        return

    print(f"\nPID={pid} 正在定位内存数组 ...", flush=True)
    t0 = time.perf_counter()
    ladders, how = (None, "跳过缓存") if args.rescan else try_cache(h, units)
    if ladders:
        print(f"{how}：{len(ladders)} 条梯，耗时 {(time.perf_counter()-t0)*1000:.2f} ms")
    else:
        # L1：先只扫 MEM_MAPPED（物品区实测都落在这里，13.7 GB）
        print("L0 未命中 → L1 扫描 MEM_MAPPED ...", flush=True)
        anchors = scan_anchors(h, units, only_type=0x40000)
        ladders = build_ladders(h, units, anchors)
        print(f"L1 结果：{len(ladders)} 条梯，耗时 {(time.perf_counter()-t0):.1f} s", flush=True)
        if not ladders:
            # L2：兜底扫私有堆（数组万一落在堆上）
            print("L1 未命中 → L2 扫描 MEM_PRIVATE ...", flush=True)
            anchors = scan_anchors(h, units, only_type=0x20000)
            ladders = build_ladders(h, units, anchors)
            print(f"L2 结果：{len(ladders)} 条梯，总耗时 {(time.perf_counter()-t0):.1f} s", flush=True)
        if ladders:
            save_cache(ladders, h)

    if args.list or not (args.repair_all or args.repair_unit is not None or args.set):
        cmd_list(h, units, ladders)
    if args.repair_all:
        cmd_repair(h, ladders, None, args.dry_run)
    if args.repair_unit is not None:
        cmd_repair(h, ladders, args.repair_unit, args.dry_run)
    if args.set:
        u, s, i = (int(x) for x in args.set)
        cmd_set(h, ladders, u, s, i, args.dry_run)


if __name__ == "__main__":
    main()
