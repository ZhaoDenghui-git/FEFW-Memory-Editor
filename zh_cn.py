"""zh_cn.py —— FEFW Save Editor 的简体中文界面层。

只改"显示什么"，不改"写什么"：本模块在 Tk 的显示出口处把英文文本换成中文，
存档读写、指针重建、校验等逻辑一行都不动，所以汉化不会影响任何功能。

三张表：
  TR      精确词表（控件 text、表头、按钮、提示……）
  P       句式表（带数字/名字的动态消息，用正则整句匹配，可递归翻译捕获组）
  WHITELIST 见 audit 脚本，不属于本模块

调用 install() 之后生效；可以重复调用。想临时看英文：设置环境变量 FEFW_LANG=en。
"""
from __future__ import annotations

import os
import re
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_EN = "Fortune's Weave Save Editor"
APP_ZH = "万缕千丝 存档修改器"
GAME_EN = "Fortune's Weave"
GAME_ZH = "火焰纹章 万缕千丝"

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------
TR: dict[str, str] = {
    # ---- 品牌 / 顶栏 ----
    "Fortune's Weave": "火焰纹章 万缕千丝",
    "Save": "保存",
    "⟳ Reload": "⟳ 重新读取",
    "Snapshot": "快照",
    "Restore…": "恢复…",
    "Slot": "存档槽",
    "Open folder…": "打开文件夹…",
    "Also edit this route's other saves": "同时写入本路线的其他存档",
    "Ready": "就绪",
    "Save editor": "存档修改器",

    # ---- 主页签 ----
    "Units": "单位",
    "Convoy": "运输队",
    "Gold": "金币",
    "Supports": "支援",
    "Player": "玩家",
    "Classes": "兵种",
    "Flags": "标记",
    "Mounts": "部队与坐骑",

    # ---- 通用列名 / 标签 ----
    "Name": "名称",
    "Lv": "等级",
    "Level": "等级",
    "LEVEL": "等级",
    "EXP": "经验",
    "Exp": "经验",
    "HP": "生命",
    "STR": "力量",
    "MAG": "魔力",
    "DEX": "技巧",
    "SPD": "速度",
    "LCK": "幸运",
    "DEF": "防御",
    "RES": "魔防",
    "CHA": "魅力",
    "Hit points": "生命值",
    "Strength": "力量",
    "Magic": "魔力",
    "Dexterity": "技巧",
    "Speed": "速度",
    "Luck": "幸运",
    "Defense": "防御",
    "Resistance": "魔防",
    "Charm": "魅力",
    "Item": "物品",
    "Type": "类型",
    "Japanese": "日文名",
    "Id": "ID",
    "Qty": "数量",
    "Quantity": "数量",
    "Points": "点数",
    "Max": "上限",
    "Top": "最高",
    "Character": "角色",
    "Convs": "对话数",
    "Support partner": "支援对象",
    "Conversations seen": "已读对话",
    "In party": "在队",
    "Value": "数值",
    "List": "列表",
    "Reads as": "显示为",
    "Kind": "类别",
    "Entry": "条目",
    "Record id": "记录 ID",
    "Internal name": "内部名",
    "Internal id": "内部 ID",
    "Rank": "等级",
    "Weapon": "武器",
    "Class": "兵种",
    "Class exp": "兵种经验",
    "Flags": "标记",
    "Next rank at": "下一级所需",
    "max": "满级",
    "enemy": "敌方",
    "npc": "NPC",
    "char": "角色",
    "Difficulty": "难度",
    "Mode": "模式",
    "Gold": "金币",
    "Renown exp": "名声经验",
    "Karma Shards": "因果碎片",
    "Run settings": "周目设置",
    "Current class": "当前兵种",
    "Battalion": "部队",
    "Mount": "坐骑",
    "Spell": "法术",

    # ---- 物品类型（只用于显示；逻辑里仍用英文原值） ----
    "Sword": "剑",
    "Lance": "枪",
    "Axe": "斧",
    "Bow": "弓",
    "Tome": "魔导书",
    "Staff": "杖",
    "Fist": "拳",
    "Shield": "盾",
    "Weapon": "武器",
    "Accessory": "饰品",
    "Consumable": "消耗品",
    "All types": "全部类型",
    "weapons": "武器",
    "accessories": "饰品",
    "consumables": "消耗品",

    # ---- 武器精通（13 项） ----
    "Swords": "剑",
    "Lances": "枪",
    "Axes": "斧",
    "Bows": "弓",
    "Fists": "拳",
    "Black Magic": "黑魔法",
    "White Magic": "白魔法",
    "Special": "特殊",
    "Infantry": "步兵",
    "Riding": "骑兵",
    "Heavy Armor": "重装",
    "Tactics": "指挥",
    "Foot": "步兵",
    "Cavalry": "骑兵",
    "Heavy": "重装",
    "Fly": "飞行",

    # ---- 难度 / 模式：源码里 DIFFICULTIES/MODES 已直接写成中文，
    #      英文原名不再出现在界面上，故此处不留英文键，避免留下永不命中的死词条。

    # ---- 单位页 ----
    "Search units or classes…  (Ctrl+F)": "搜索单位或兵种…（Ctrl+F）",
    "Show enemies & NPCs": "显示敌人与 NPC",
    "Copy unit in…": "复制单位…",
    "Remove unit": "移除单位",
    "No unit selected": "未选择单位",
    "No save folder open": "未打开存档文件夹",
    "Open a save folder to begin.": "打开存档文件夹开始。",
    "Use “Open folder…” (Ctrl+O) and pick any slot file (Auto, Chapter_*, Route_*) inside your save folder.":
        "用“打开文件夹…”（Ctrl+O）选择存档文件夹里的任意槽文件（Auto、Chapter_*、Route_*）。",
    "Make copies match": "同步各份记录",
    "Stats": "属性",
    "Points gained from level-ups": "升级获得的成长点",
    "These are what the unit has gained from levelling, not the totals shown in game: the game adds them to the unit's class and personal bases, and stat-up items aren't included. Raising one raises the in-game stat by the same amount.":
        "这里显示的是升级成长累计值，不是游戏里看到的总值——游戏会把它们加在兵种基础值和个人基础值上，且不含属性提升道具。把某项加一，游戏内该项就高一点。",
    "Changing HP gained moves max HP with it. Edits apply on Enter / Tab; Save writes them.":
        "改动生命成长值时，最大生命值会跟着变。修改在回车或 Tab 时生效，按保存才写入存档。",
    "Inventory": "物品栏",
    "Carried items": "携带物品",
    "Select a slot, then choose its replacement.": "先选中一个格子，再挑选用来替换的物品。",
    "Allow any item type (can break the game)": "允许任意物品类型（可能弄坏存档）",
    "Add item…": "添加物品…",
    "Remove item": "移除物品",
    "Search classes…": "搜索兵种…",
    "Include monster / internal classes": "包含魔物 / 内部兵种",
    "Change class": "更改兵种",
    "Only the class id is written. Movement, usable weapons, class skills and max HP come from the class and are not recalculated — check the unit screen in game after loading.":
        "只会写入兵种 ID。移动力、可用武器、兵种技能和最大生命值由兵种决定，不会重新计算——读档后请到游戏里的单位界面确认。",
    "Arts & skills": "战技与技能",

    # ---- 运输队 ----
    "Everything in storage, materials included, by slot.": "按槽位列出仓库里的全部物品，含素材。",
    "Filter the convoy…": "筛选运输队…",
    "Repair convoy": "修复运输队",
    "Slot · Item": "槽位 · 物品",
    "Set qty": "设置数量",
    "Remove": "移除",
    "All materials…": "全部素材…",
    "Every item has its own convoy slot. Add item… puts a stack in it and Remove empties a slot; the list keeps its slot count and everything after it is moved safely. All materials… stocks every ore, meat, fish and plant at one quantity.":
        "每件物品都有固定的运输队槽位：添加物品… 会把一摞放进它自己的槽，移除 会清空该槽；列表槽位数不变，后续内容会安全地重排。全部素材… 会把所有矿石、肉、鱼、植物一次性补成同一个数量。",

    # ---- 物品目录 ----
    "Item catalogue": "物品目录",
    "Search by name, Japanese name or id…": "按英文名、日文名或 ID 搜索…",
    "Internal duplicates": "内部重复条目",
    "Double-click or Enter also works": "双击或按回车也可以",
    "Put in selected slot": "放入选中格子",
    "Put in selected row": "放入选中行",
    "Pick an item from the catalogue first.": "请先从目录里选择一个物品。",

    # ---- 金币 ----
    "GOLD": "金币",
    "New amount": "新数值",
    "Set gold": "设置金币",
    "Gold lives at a fixed place in every slot of this game (GameUserData + 6), so there is nothing to locate: the amount below is what the game will read. Chapter and battle snapshots keep their own amount.":
        "本作每个存档的金币都在固定位置（GameUserData + 6），无需定位：下面显示的就是游戏会读到的数值。章节与战斗快照各自保留自己的数值。",
    "This slot has no GameUserData record.": "这个存档槽没有 GameUserData 记录。",
    "Read from this slot's own record.": "读自这个存档槽自己的记录。",
    "All changes saved": "全部改动已保存",
    "No convoy list found in this slot": "这个存档槽里没有找到运输队列表",
    "list runs past the end of the save": "列表超出了存档末尾",
    "Save slots": "存档槽文件",
    "All files": "所有文件",
    "header checksum mismatch": "头部校验和不匹配",
    "payload checksum mismatch": "数据校验和不匹配",
    "no zstd frame": "不是 zstd 压缩帧",
    "Flying": "飞行",

    # ---- 支援 ----
    "Characters": "角色",
    "Every character's supports, with the points this save holds for each pair.":
        "所有角色的支援，以及本存档为每一对记录的点数。",
    "Only characters in this save": "只显示本存档中的角色",
    "Search characters…": "搜索角色…",
    "No character selected": "未选择角色",
    "Open a save to see what it records.": "打开存档查看其中记录的内容。",
    "Nothing to show": "没有可显示的内容",
    "Open a save, or untick “Only characters in this save”.": "请打开存档，或取消勾选“只显示本存档中的角色”。",
    "Record conversation as seen:": "把对话标记为已读：",
    "Forget…": "忘记…",
    "Support points:": "支援点数：",
    "Apply": "应用",
    "Points are what the game turns into a support level: 3200 is the maximum (level 5). The save keeps a slot for every pair, so a pair whose support hasn't opened yet still has a value here — the game may not show it until it opens the relationship itself. “Seen” is the save's 支援会話 flag for a conversation; Max is the highest rank the game data gives the pair.":
        "点数是游戏换算支援等级的依据：3200 为上限（5 级）。存档为每一对角色都留了位置，所以还没开启支援的一对在这里也有数值——游戏可能要先自行建立关系才显示。“已读”是该段对话对应的存档 支援会話 标记；上限 是游戏数据里这对角色能到的最高等级。",

    # ---- 玩家页 ----
    "Army-wide values, stored in GameUserData at the start of the save.": "全军通用的数值，存放在存档开头的 GameUserData 中。",
    "Written into every copy the save keeps, and into every slot in this folder (the run's difficulty is one setting).":
        "会写进存档保留的每一份副本，也会写进本文件夹的每个槽（难度属于整个周目的一项设置）。",
    "Renown exp is the army's total; the level itself follows the game's own Reward.bin table, so set a value between two levels to land on a level.":
        "名声经验是全军累计值；等级本身按游戏自带的 Reward.bin 表换算，两个等级之间取值会落在同一等级上。",
    "This file's GameUserData isn't the known layout.": "这个文件的 GameUserData 不是已知结构。",

    # ---- 兵种页 ----
    "Set current": "设为当前",
    "Change…": "更改…",
    "Classes this unit has": "该单位拥有的兵种",
    "Weapon masteries": "武器精通",
    "Set rank…": "设置等级…",
    "Set exp…": "设置经验…",
    "Add…": "添加…",
    "Swap…": "替换…",
    "Classes and masteries are separate lists inside the unit's record; adding one grows the record and every pointer after it is rewritten (that is what the old editor could not do). Rank thresholds are the game's: E 0 · E+ 20 · D 35 · D+ 80 · C 220 · C+ 440 · B 690 · B+ 910 · A 1215 · A+ 1510 · S 1960 · S+ 2560.":
        "兵种和精通是单位记录里各自独立的列表；增加一项会让记录变长，其后所有指针都会重写（旧版编辑器做不到这点）。等级门槛按游戏原值：E 0 · E+ 20 · D 35 · D+ 80 · C 220 · C+ 440 · B 690 · B+ 910 · A 1215 · A+ 1510 · S 1960 · S+ 2560。",

    # ---- 标记页 ----
    "Saved flags": "存档标记",
    "Add flag…": "添加标记…",
    "Set value…": "设置数值…",
    "All": "全部",
    "Recruited": "已加入",
    "Met": "已会面",
    "Achievements": "成就",
    "Expansion classes": "解锁兵种",
    "Equipped arts": "装备战技",
    "Equipped skills": "装备技能",
    "List 3": "列表 3",
    "Arts known": "已知战技",
    "List 5": "列表 5",
    "Learned (all)": "已学全部",
    "Equipped arts and skills are what the unit takes into battle; “Learned (all)” is everything it has learned. Adding or removing an entry rewrites the list safely.":
        "已装备的战技与技能才是上场时生效的；“已学全部”是该单位学会的全部内容。增删条目会安全地重写整个列表。",
    "Unit": "单位",
    "Flags are the game's named variables: 支援会話_<A>_<B>_<rank> marks a support conversation as seen, 加入_ a recruited character, 面識_ a met one, 実績_ an achievement counter. A name the data files don't contain shows as its hash — that is a real flag, just one the editor can't name.":
        "标记就是游戏里的具名变量：支援会話_<A>_<B>_<等级> 表示某段支援对话已读，加入_ 表示已招募的角色，面識_ 表示已会面，実績_ 是成就计数。数据文件中没有名字的标记会显示成哈希值——它确实是真实标记，只是编辑器叫不出名字。",

    # ---- 部队 / 坐骑页 ----
    "Battalions & mounts": "部队与坐骑",
    "Each unit carries one battalion (and, for mounted classes, one mount) at the end of its record. Pick from the pool this save holds.":
        "每个单位在其记录末尾带有一个部队（骑乘兵种还带一个坐骑）。从本存档持有的池中选择。",
    "This save's pool": "本存档的池",
    "Assign to unit": "分配给单位",
    "Clear unit's": "清除该单位的",
    "Remove from pool": "从池中移除",

    # ---- 弹窗按钮 / 标题 ----
    "Use this": "使用此项",
    "Cancel": "取消",
    "OK": "确定",
    "Pick a snapshot to restore": "选择要恢复的快照",
    "Open any slot in your save folder": "选择存档文件夹中的任意槽文件",
    "Copy a unit from which save?": "从哪个存档复制单位？",
    "Add to the convoy": "添加到运输队",
    "How many?": "数量是多少？",
    "All materials": "全部素材",
    "Pick the unit's current class": "选择单位当前兵种",
    "Add a class": "添加兵种",
    "Add a weapon mastery": "添加武器精通",
    "Add a flag": "添加标记",
    "Forget which conversation?": "忘记哪一段对话？",
    "Class exp": "兵种经验",
    "Mastery exp": "精通经验",
    "Flag value": "标记数值",
    "Change class": "更改兵种",
    "Repair convoy": "修复运输队",
    "Quantity": "数量",
    "New value (0 – 2147483647):": "新数值（0 – 2147483647）：",
    "Value to store (0 – 254, or anything wider):": "要写入的数值（0 – 254，或任意更宽的值）：",
    "Class exp for the selected classes (0 – 255):": "所选兵种的经验（0 – 255）：",
    "Class exp saved with this class (0 – 255):": "这个兵种记录的兵种经验（0 – 255）：",
    "That is the unit's current class. Change the current class first, or use Swap.":
        "这是该单位当前的兵种。请先改当前兵种，或者用“替换…”。",

    # ---- 状态栏消息（静态） ----
    "Re-read every slot from disk.": "已从磁盘重新读取全部存档槽。",
    "Nothing to save — no changes yet.": "没有需要保存的内容——还没有任何修改。",
    "Not saved — the game's newer save was kept. Press Reload, then redo your edits.":
        "未保存——保留了游戏刚写的新存档。请按“重新读取”，然后重做修改。",
    "No Eden save folder found automatically — open one to begin.":
        "没有自动找到 Eden 存档文件夹——请手动打开一个。",
    "Edits now also go into this route's other saves (never chapter checkpoints).":
        "现在修改也会写进本路线的其他存档（不含章节检查点）。",
    "Edits now go into the selected slot only.": "现在修改只写进选中的存档槽。",
    "The game saved a slot you have unsaved edits in. Saving now would overwrite the game's progress. Reload to start again from the game's save.":
        "游戏写入了一个你还有未保存修改的存档槽。现在保存会覆盖游戏的进度。请按“重新读取”，从游戏的新存档重新开始。",
    "Enemy and NPC records are read-only — their layout isn't fully known, so an edit could land on the wrong bytes.":
        "敌人和 NPC 的记录只读——其结构尚未完全弄清，改动可能写到错误的字节上。",
    "🔒 Enemy / NPC — read-only": "🔒 敌人 / NPC — 只读",
    "One of the fields isn't a valid number.": "有字段不是有效数字。",
    "Nothing changed — that unit has no such item slot here.": "没有任何改动——该单位在这个存档里没有这个物品格。",
    "That slot already holds this item.": "该格子已经是这个物品了。",
    "That row already holds this item.": "该行已经是这个物品了。",
    "Pick a unit first.": "请先选择一个单位。",
    "Pick an inventory slot first.": "请先在物品栏里选中一个格子。",
    "Pick a class from the list first.": "请先从列表里选择一个兵种。",
    "Pick a class row first.": "请先选中一行兵种。",
    "Pick a convoy row first.": "请先选中一行运输队。",
    "Pick a battalion in the pool first.": "请先在池中选中一个部队。",
    "Pick a unit on the Mounts page first.": "请先在部队与坐骑页选中一个单位。",
    "Pick a support partner on the Supports page first.": "请先在支援页选中一个支援对象。",
    "Select a convoy row first.": "请先选中一行运输队。",
    "Select one convoy row to replace (any spare item you put there in game).":
        "请选中一行运输队来替换（就是你在游戏里塞了闲置物品的那一行）。",
    "This slot's convoy doesn't need repairing.": "这个存档槽的运输队不需要修复。",
    "Gold is read straight from the save — nothing to search for.":
        "金币直接从存档读取——无需搜索。",
    "Type the new gold amount as a number.": "请把新的金币数量写成数字。",
    "Gold must be between 0 and 4,294,967,295.": "金币必须在 0 到 4,294,967,295 之间。",
    "Nothing to undo.": "没有可撤销的操作。",
    "Nothing to redo.": "没有可重做的操作。",
    "Undone — Ctrl+Y puts it back.": "已撤销——按 Ctrl+Y 可还原。",
    "Redone.": "已重做。",
    "No snapshots yet — press Snapshot first.": "还没有快照——请先按“快照”。",
    "No skill names available (skill_ids.json is missing).": "没有可用的技能名（缺少 skill_ids.json）。",
    "The unit already has every weapon mastery.": "该单位已经拥有全部武器精通。",
    "This save has no flag list to add to.": "这个存档没有可添加的标记列表。",
    "This pair has no conversations in the game data.": "游戏数据里这对角色没有对话。",
    "No materials found in the item list.": "物品表里没有找到素材。",
    "Only battalions can be assigned here.": "这里只能分配部队。",
    "This unit's record has no battalion slot (the game gives it one when the unit actually joins a battle) — nothing to write.":
        "该单位的记录里没有部队字段（要等单位真正参战游戏才会给）——没有可写入的内容。",
    "Pick an item from the catalogue first.": "请先从目录里选择一个物品。",

    # ---- 库存占位 ----
    "  Couldn't find this unit's item list": "找不到该单位的物品列表",
    "  Carrying nothing": "没有携带任何物品",
    "  SPELLS  (read-only)": "法术（只读）",
    "  (empty)": "（空）",
}

# ---------------------------------------------------------------------------
# 句式表：整句匹配，捕获组里若有英文会再走一次 tr()
# ---------------------------------------------------------------------------
P: list[tuple[re.Pattern, object]] = []


def pat(rx: str, func=None, repl: str | None = None):
    P.append((re.compile(rx, re.S), func if func else repl))


def _d(s: str) -> str:
    """捕获组的翻译（允许 None）。"""
    return tr(s) if isinstance(s, str) else s


_MONTHS = {"Jan": "1月", "Feb": "2月", "Mar": "3月", "Apr": "4月", "May": "5月",
           "Jun": "6月", "Jul": "7月", "Aug": "8月", "Sep": "9月", "Oct": "10月",
           "Nov": "11月", "Dec": "12月"}


def _scope(s: str) -> str:
    s = (s or "").strip()
    m = re.match(r"^(\d+) saves of this route$", s)
    if m:
        return f"本路线的 {m.group(1)} 个存档"
    if s == "this slot":
        return "这个存档槽"
    return _d(s)


def _what(s: str) -> str:
    s = (s or "").strip()
    if s == "open another folder":
        return "打开另一个文件夹"
    if s == "quit":
        return "退出"
    return _d(s)


# 品牌 / 版本 / 时间 / 槽位
pat(r"^SAVE EDITOR\s+·\s+v(.+)$", lambda m: f"存档修改器  ·  v{m.group(1)}")
pat(r"^today (\d\d:\d\d)$", lambda m: f"今天 {m.group(1)}")
pat(r"^([A-Z][a-z]{2}) (\d{1,2}) (\d\d:\d\d)$",
    lambda m: f"{_MONTHS.get(m.group(1), m.group(1))}{int(m.group(2))}日 {m.group(3)}")
pat(r"^(.+?) P(\d+) Ch(\d+)$", lambda m: f"{m.group(1)} 第{m.group(2)}部 第{m.group(3)}章")
pat(r"^route (\d+)$", lambda m: f"路线 {m.group(1)}")
pat(r"^Also edit this route's other saves \((.+)\)$",
    lambda m: "同时写入本路线的其他存档"
              + ("（在章节检查点中不可用）" if m.group(1) == "off in checkpoints"
                 else "（此处没有）" if m.group(1) == "none here"
                 else f"（另加 {m.group(1)}）"))

# 单位页 / 顶部卡片
pat(r"^(.+?)\s+·\s+Level\s+(\d+)\s+·\s+(.+)$",
    lambda m: f"{_d(m.group(1))}  ·  等级 {m.group(2)}  ·  {m.group(3)}")
pat(r"^live record · (.+)$", lambda m: f"当前记录 · {_d(m.group(1))}")
pat(r"^(\d+) cop(y|ies)$", lambda m: f"{m.group(1)} 份记录")
pat(r"^(\d+) copies? in (\d+) slots$",
    lambda m: f"{m.group(1)} 份记录（{m.group(2)} 个存档槽）")
pat(r"^(.+): level → (\d+)  \((.*)\)$",
    lambda m: f"{_d(m.group(1))}：等级 → {m.group(2)}（{_d(m.group(3))}）")
pat(r"^(.+): EXP → (\d+)  \((.*)\)$",
    lambda m: f"{_d(m.group(1))}：经验 → {m.group(2)}（{_d(m.group(3))}）")
pat(r"^(.+): (.+) → (\d+)  \((.*)\)$",
    lambda m: f"{_d(m.group(1))}：{_d(m.group(2))} → {m.group(3)}（{_d(m.group(4))}）")
pat(r"^(.+): every copy now matches the values shown \((.*)\)$",
    lambda m: f"{_d(m.group(1))}：各份记录都已与当前显示的数值一致（{_d(m.group(2))}）")
pat(r"^(.+): slot (\d+) → (.+)$",
    lambda m: f"{_d(m.group(1))}：格子 {m.group(2)} → {_d(m.group(3))}")
pat(r"^(.+): added (.+)$", lambda m: f"{_d(m.group(1))}：已添加 {_d(m.group(2))}")
pat(r"^(.+): removed the item in slot (\d+)$",
    lambda m: f"{_d(m.group(1))}：已移除格子 {m.group(2)} 里的物品")
pat(r"^(.+): class → (.+)  \((\d+) ids in (\d+) slot\(s\)\)$",
    lambda m: f"{_d(m.group(1))}：兵种 → {_d(m.group(2))}（{m.group(3)} 处 ID，{m.group(4)} 个存档槽）")
pat(r"^(.+) is already a (.+)\.$", lambda m: f"{_d(m.group(1))} 已经是 {_d(m.group(2))}。")
pat(r"^(.+) must be a number from (\d+) to (\d+)\.$",
    lambda m: f"{_d(m.group(1))} 必须是 {m.group(2)} 到 {m.group(3)} 之间的数字。")
pat(r"^One of the fields isn't a valid number\.$", lambda m: "有字段不是有效数字。")

# 打开 / 保存 / 重新读取 / 关闭
pat(r"^Opened (.+) — (\d+) units\.$",
    lambda m: f"已打开 {m.group(1)} — 共 {m.group(2)} 名单位。")
pat(r"^(\d+) slot\(s\) have unsaved changes\.\s*Save them before you (.+)\?$",
    lambda m: f"{m.group(1)} 个存档槽有未保存的修改。\n\n要在{_what(m.group(2))}之前保存吗？")
pat(r"^Saved (.+)\. Originals kept as \.bak next to each slot\.$",
    lambda m: f"已保存 {m.group(1)}。原始文件以 .bak 形式留在各自槽位旁边。")
pat(r"^Reload from disk and throw away the unsaved changes in (\d+) slot\(s\)\?$",
    lambda m: f"从磁盘重新读取，并丢弃 {m.group(1)} 个存档槽里未保存的修改？")
pat(r"^The game has saved (.+?) since the editor read it\..*Overwrite anyway\? \(No = keep the game's save; then press Reload and redo your edits\.\)$",
    lambda m: f"游戏在编辑器读取之后写入过 {m.group(1)}。\n\n现在保存会用编辑器里的旧存档加你的修改替换这份更新的存档，"
              "你在游戏里之后做的任何事都会丢失。\n\n仍然覆盖吗？（选“否”会保留游戏的存档；接着按“重新读取”并重做修改。）")
pat(r"^Some slots could not be written:\s*(.*?)\s*Is the game still running\?$",
    lambda m: f"以下存档槽写入失败：\n\n{m.group(1)}\n\n游戏是否仍在运行？")
pat(r"^Picked up a newer save from the game: (.+)$",
    lambda m: f"已接收游戏写入的较新存档：{m.group(1)}")
pat(r"^Can't read (.+): (.+)$", lambda m: f"无法读取 {m.group(1)}：{_d(m.group(2))}")
pat(r"^● (\d+) slots? with unsaved changes  ·  Ctrl\+S to save$",
    lambda m: f"● {m.group(1)} 个存档槽有未保存的修改  ·  Ctrl+S 保存")
pat(r"^(\d+) slots? with unsaved changes  ·  Ctrl\+S to save$",
    lambda m: f"● {m.group(1)} 个存档槽有未保存的修改  ·  Ctrl+S 保存")
pat(r"^Save \((\d+)\)$", lambda m: f"保存（{m.group(1)}）")
pat(r"^no Auto/Chapter_\* slot in (.+)$", lambda m: f"在 {m.group(1)} 里没有 Auto/Chapter_* 存档槽")

# 支援页
pat(r"^(\d+) characters?( in this save)?$",
    lambda m: f"{m.group(1)} 名角色" + ("（本存档内）" if m.group(2) else ""))
pat(r"^(\d+) partners · top ([\d,]+) points · (\d+) conversations? recorded in this save$",
    lambda m: f"{m.group(1)} 名对象 · 最高 {m.group(2)} 点 · 本存档已记录 {m.group(3)} 段对话")
pat(r"^(\d+) partners selected: Apply / Max / 0 set all (\d+) of them$",
    lambda m: f"已选 {m.group(1)} 名对象：应用 / 上限 / 0 会同时作用于其中 {m.group(2)} 对")
pat(r"^(.+): ([\d,]+) of ([\d,]+) points$",
    lambda m: f"{_d(m.group(1))}：{m.group(2)} / {m.group(3)} 点")
pat(r"^(.+): no entry in this save$", lambda m: f"{_d(m.group(1))}：本存档没有这一项")
pat(r"^Support (.+) - (.+) \(([A-Za-z+]+)\)$",
    lambda m: f"支援 {_d(m.group(1))} – {_d(m.group(2))}（{m.group(3)}）")
pat(r"^Support (.+) – (.+) \((.+)\) seen$",
    lambda m: f"支援 {_d(m.group(1))} – {_d(m.group(2))}（{m.group(3)}）已标记为已读")
pat(r"^Support (.+) – (.+) \((.+)\) cleared$",
    lambda m: f"支援 {_d(m.group(1))} – {_d(m.group(2))}（{m.group(3)}）已清除")
pat(r"^Support points (.+) – (.+) → ([\d,]+)$",
    lambda m: f"支援点数 {_d(m.group(1))} – {_d(m.group(2))} → {m.group(3)}")
pat(r"^(.+) – (.+) already at ([\d,]+) points\.$",
    lambda m: f"{_d(m.group(1))} – {_d(m.group(2))} 已经是 {m.group(3)} 点。")
pat(r"^Type a support point value between 0 and ([\d,]+)\.$",
    lambda m: f"请输入 0 到 {m.group(1)} 之间的支援点数。")
pat(r"^Already recorded \((.+)\)\.$", lambda m: f"已记录（{m.group(1)}）。")
pat(r"^This pair has a (\w+) conversation in the game data\.$",
    lambda m: f"游戏数据里这对角色有 {m.group(1)} 段对话。")
pat(r"^None of these pairs have a (\w+) conversation in the game data\.$",
    lambda m: f"游戏数据里这些角色都没有 {m.group(1)} 段对话。")
pat(r"^Forget the (.+) conversation between (.+) and (.+)\?$",
    lambda m: f"要忘掉 {_d(m.group(2))} 与 {_d(m.group(3))} 之间的 {m.group(1)} 段对话吗？")

# 兵种 / 精通页
pat(r"^(\d+) units?$", lambda m: f"{m.group(1)} 名单位")
pat(r"^(\d+) partners$", lambda m: f"{m.group(1)} 名对象")
pat(r"^Lv (\d+) · (\d+) class(?:es)? · (\d+) masteries$",
    lambda m: f"等级 {m.group(1)} · {m.group(2)} 个兵种 · {m.group(3)} 项精通")
pat(r"^Lv (\d+) · (.+)$", lambda m: f"等级 {m.group(1)} · {_d(m.group(2))}")
pat(r"^(\d+) exp$", lambda m: f"{m.group(1)} 经验")
pat(r"^Swap (.+) for…$", lambda m: f"把 {_d(m.group(1))} 换成…")
pat(r"^Rank for (.+)$", lambda m: f"{_d(m.group(1))} 的等级")
pat(r"^Current class → (.+)$", lambda m: f"当前兵种 → {_d(m.group(1))}")
pat(r"^Added class (.+)$", lambda m: f"已添加兵种 {_d(m.group(1))}")
pat(r"^Removed class (.+)$", lambda m: f"已移除兵种 {_d(m.group(1))}")
pat(r"^Removed (\d+) classes$", lambda m: f"已移除 {m.group(1)} 个兵种")
pat(r"^Class (.+) → (.+)$", lambda m: f"兵种 {_d(m.group(1))} → {_d(m.group(2))}")
pat(r"^Added mastery (.+)$", lambda m: f"已添加精通 {_d(m.group(1))}")
pat(r"^Removed mastery (.+)$", lambda m: f"已移除精通 {_d(m.group(1))}")
pat(r"^(\d+) masteries' exp → (\d+)$",
    lambda m: f"{m.group(1)} 项精通的经验 → {m.group(2)}")
pat(r"^(.+) class exp → (\d+)$", lambda m: f"{_d(m.group(1))} 的兵种经验 → {m.group(2)}")
pat(r"^(\d+) classes' exp → (\d+)$",
    lambda m: f"{m.group(1)} 个兵种的兵种经验 → {m.group(2)}")
pat(r"^(.+) exp → (\d+)$", lambda m: f"{_d(m.group(1))} 经验 → {m.group(2)}")
pat(r"^(.+) → ([EDCBA]\+?|S\+?)$", lambda m: f"{_d(m.group(1))} → {m.group(2)}")
pat(r"^Exp for (\d+) masteries \((.*)\) \(0 – 65535\):$",
    lambda m: f"{m.group(1)} 项精通（{m.group(2)}）的经验（0 – 65535）：")
pat(r"^Exp for (.+) \(0 – 65535\):$", lambda m: f"{_d(m.group(1))} 的经验（0 – 65535）：")
pat(r"^Remove the (.+) mastery from (.+)\?$",
    lambda m: f"要从 {_d(m.group(2))} 身上移除 {_d(m.group(1))} 精通吗？")

# 标记页
pat(r"^(.{1,40}) = (\d+)$", lambda m: f"{_d(m.group(1))} = {m.group(2)}")
pat(r"^Remove the flag (.+)\?$", lambda m: f"要移除标记 {_d(m.group(1))} 吗？")
pat(r"^([\d,]+) of ([\d,]+) in this file(?: · (.+)\*)?$",
    lambda m: f"{m.group(1)} / {m.group(2)}（本文件）"
              + (f" · {m.group(3)}*" if m.group(3) else ""))
pat(r"^unknown (0x[0-9A-Fa-f]{8})$", lambda m: f"未知 {m.group(1)}")
pat(r"^unknown item (0x[0-9a-f]{8})$", lambda m: f"未知物品 {m.group(1)}")
pat(r"^unknown class ([0-9a-f]{8})$", lambda m: f"未知兵种 {m.group(1)}")
pat(r"^class ([0-9a-f]{8})$", lambda m: f"兵种 {m.group(1)}")
pat(r"^item ([0-9a-f]{8})$", lambda m: f"物品 {m.group(1)}")
pat(r"^\((.+)\)$", lambda m: f"（{_d(m.group(1))}）")
pat(r"^unknown 0x([0-9A-Fa-f]{8})$", lambda m: f"未知 0x{m.group(1)}")
pat(r"^(.+): removed entry (\d+)$",
    lambda m: f"{_d(m.group(1))}：已移除第 {m.group(2)} 项")

# 部队 / 坐骑页
pat(r"^Battalion row (\d+)$", lambda m: f"部队第 {m.group(1)} 行")
pat(r"^Mount #(\d+)$", lambda m: f"坐骑 #{m.group(1)}")
pat(r"^Battalion → row (\d+)$", lambda m: f"部队 → 第 {m.group(1)} 行")
pat(r"^Battalion cleared$", lambda m: "已清除部队")
pat(r"^Removed battalion from the pool$", lambda m: "已从池中移除部队")
pat(r"^Copied (.+) in$", lambda m: f"已复制进 {_d(m.group(1))}")
pat(r"^Removed (.+)$", lambda m: f"已移除 {_d(m.group(1))}")

# 运输队
pat(r"^([\d,]+) items · (\d+) slots$",
    lambda m: f"{m.group(1)} 件物品 · {m.group(2)} 个槽位")
pat(r"^([\d,]+) of ([\d,]+) items$", lambda m: f"{m.group(1)} / {m.group(2)} 件物品")
pat(r"^This slot's convoy list is damaged — see above$",
    lambda m: "该存档槽的运输队列表已损坏——见上方说明")
pat(r"^No convoy list found in this slot$", lambda m: "该存档槽里没有找到运输队列表")
pat(r"^⚠ This convoy list is damaged:.*?clean (\d+)-slot list keeping all (\d+) items it can still find\..*$",
    lambda m: "⚠ 这个运输队列表已损坏：旧版本的“添加”把物品写到了空槽位上，把后面的列表挤错位了，"
              f"游戏读不出来。修复会把它重建为干净的 {m.group(1)} 槽列表，保留还能找到的全部 {m.group(2)} 件物品。"
              "撤销有效，原始文件留在该槽的 .bak 里。")
pat(r"^Rebuild this slot's convoy as a clean (\d+)-slot list with the (\d+) items found in it\?.*$",
    lambda m: f"要把该存档槽的运输队重建为干净的 {m.group(1)} 槽列表，保留其中找到的 {m.group(2)} 件物品吗？"
              "\n\n按下保存之前不会写入任何内容，撤销可以撤回，原始文件留在该槽的 .bak 里。")
pat(r"^Convoy repaired: (\d+) items, (\d+) slots — Ctrl\+S to write it\.$",
    lambda m: f"运输队已修复：{m.group(1)} 件物品、{m.group(2)} 个槽位——按 Ctrl+S 写入。")
pat(r"^Quantity must be 1 – (\d+)\.$", lambda m: f"数量必须是 1 – {m.group(1)}。")
pat(r"^Convoy slot (\d+): (.+) → (.+?)(?: in (\d+) saves)?$",
    lambda m: f"运输队槽 {m.group(1)}：{_d(m.group(2))} → {_d(m.group(3))}"
              + (f"，共 {m.group(4)} 个存档" if m.group(4) else ""))
pat(r"^Quantity → (\d+) on (\d+) row\(s\)(?: in (\d+) saves)?$",
    lambda m: f"数量 → {m.group(1)}，共 {m.group(2)} 行"
              + (f"，{m.group(3)} 个存档" if m.group(3) else ""))
pat(r"^Convoy: added (.+) ×(\d+)$", lambda m: f"运输队：已添加 {_d(m.group(1))} ×{m.group(2)}")
pat(r"^Convoy: removed slot (\d+)$", lambda m: f"运输队：已移除槽 {m.group(1)}")
pat(r"^Convoy: all materials ×(\d+)$", lambda m: f"运输队：全部素材 ×{m.group(1)}")
pat(r"^Materials ×(\d+): (\d+) added, (\d+) set(?:, ([\d,]+) moved into their own slots)?(?: — (\d+) skipped \(their slot holds another item\))?$",
    lambda m: f"素材 ×{m.group(1)}：新增 {m.group(2)}，设为 {m.group(3)}"
              + (f"，{m.group(4)} 件移回了自己的槽" if m.group(4) else "")
              + (f"；{m.group(5)} 件跳过（其槽位被别的物品占用）" if m.group(5) else ""))
pat(r"^(.+) has no convoy slot of its own\.$", lambda m: f"{_d(m.group(1))} 没有自己的运输队槽位。")
pat(r"^Slot (\d+), (.+)'s own slot, holds (.+) — remove that first\.$",
    lambda m: f"槽 {m.group(1)}（{_d(m.group(2))} 自己的槽）里现在是 {_d(m.group(3))}——请先移除它。")
pat(r"^([\d,]+) match(?:es)?(?: · first (\d+))?$",
    lambda m: f"{m.group(1)} 项匹配" + (f" · 前 {m.group(2)} 项" if m.group(2) else ""))
pat(r"^Showing (.+) only, to match the selected slot\.$",
    lambda m: f"为匹配所选格子，只显示{_d(m.group(1))}。")

# 金币 / 玩家
_GOLD_KEY = {"Gold": "金币", "Renown": "名声", "Karma": "因果碎片"}
pat(r"^(Gold|Renown|Karma) → ([\d,]+)$",
    lambda m: f"{_GOLD_KEY[m.group(1)]} → {m.group(2)}")
pat(r"^Gold → ([\d,]+)(?: in (\d+) slots)?$",
    lambda m: f"金币 → {m.group(1)}" + (f"，共 {m.group(2)} 个存档槽" if m.group(2) else ""))
pat(r"^(.+) / (.+) in this slot$",
    lambda m: f"{_d(m.group(1))} / {_d(m.group(2))}（这个存档槽）")
pat(r"^Playthrough (\d+)$", lambda m: f"第 {m.group(1)} 周目")
pat(r"^· every slot: (.+) / (.+)$",
    lambda m: f"· 所有存档槽：{_d(m.group(1))} / {_d(m.group(2))}")
pat(r"^· slots disagree — (.+)$", lambda m: f"· 各存档不一致 — {m.group(1)}")
pat(r"^0 – ([\d,]+)$", lambda m: f"0 – {m.group(1)}")

# 快照 / 单位复制
pat(r"^Snapshot of (\d+) slots saved to (.+)$",
    lambda m: f"已快照 {m.group(1)} 个存档槽，保存到 {m.group(2)}")
pat(r"^Restored (\d+) slots from (.+)\.$",
    lambda m: f"已从 {m.group(2)} 恢复 {m.group(1)} 个存档槽。")
pat(r"^Copy a unit from (.+)$", lambda m: f"从 {m.group(1)} 复制一个单位")
pat(r"^Copy the (\d+) slot files from\s*(.*?)\s*over the save folder\?\s*Everything in the editor is reloaded from those files\.$",
    lambda m: f"把 {m.group(1)} 个槽文件从\n{m.group(2)}\n覆盖到存档文件夹吗？\n\n编辑器里的内容会全部从这些文件重新读取。")
pat(r"^Remove (.+) from this save\?\s*The game may still refer to them.*$",
    lambda m: f"要从这个存档里移除 {_d(m.group(1))} 吗？\n\n游戏可能还会引用它（加入标记、部队、支援），可能会让游戏困惑。"
              "撤销（Ctrl+Z）可以恢复；按下保存之前不会写入任何内容。")

# _core_edit / flash 的通用壳
pat(r"^Couldn't apply “(.+)”(\.|: .*)?$",
    lambda m: f"无法应用“{_d(m.group(1))}”{(m.group(2) or '')}")
pat(r"^Couldn't apply difficulty/mode(\.|: .*)?$",
    lambda m: f"无法应用难度/模式{(m.group(1) or '')}")
pat(r"^(.+?)( in (\d+) slots)? — Ctrl\+S to write it\.$",
    lambda m: f"{_d(m.group(1))}"
              + (f"，共 {m.group(3)} 个存档槽" if m.group(3) else "")
              + " — 按 Ctrl+S 写入。")
pat(r"^(.+) — Ctrl\+S to write it\.$", lambda m: f"{_d(m.group(1))} — 按 Ctrl+S 写入。")


# ---------------------------------------------------------------------------
# 翻译入口
# ---------------------------------------------------------------------------
def _lookup(s: str) -> str | None:
    """精确词表 -> 句式表；命中返回中文，否则 None。"""
    if GAME_EN in s:
        s = s.replace(APP_EN, APP_ZH).replace(GAME_EN, GAME_ZH)
        if s not in TR:
            return s            # 品牌名已替换，其余保持（槽文件名等）
    hit = TR.get(s)
    if hit is not None:
        return hit
    for rx, rep in P:
        m = rx.match(s)
        if m:
            if callable(rep):
                return rep(m)
            return m.expand(rep)
    return None


def tr(text):
    """把一处要显示给用户的英文换成中文；不是字符串则原样返回。"""
    if not isinstance(text, str) or not text:
        return text
    core = text.strip()
    if not core:
        return text
    out = _lookup(core)
    if out is None:
        return text
    lead = text[:len(text) - len(text.lstrip())]
    return lead + out


def tr_map(options):
    """把选项字典里的 text= 换掉；返回新字典（不改调用方传进来的对象）。"""
    if not isinstance(options, dict):
        return options or {}
    if "text" in options:
        options = dict(options)
        options["text"] = tr(options["text"])
    return options


def kind_zh(kind: str) -> str:
    """物品类型显示名（英文原值 -> 中文显示）。"""
    return TR.get(kind, kind)


def kind_en(label: str) -> str:
    """把下拉框里的中文类型换回逻辑用的英文原值。"""
    if label in ("全部类型", "All types"):
        return "All types"
    for en, zh in TR.items():
        if zh == label and en in ("Sword", "Lance", "Axe", "Bow", "Tome", "Staff",
                                 "Fist", "Shield", "Weapon", "Accessory", "Consumable", "Item"):
            return en
    return label


# ---------------------------------------------------------------------------
# 装到 Tk 的显示出口
# ---------------------------------------------------------------------------
_INSTALLED = False


def install() -> None:
    global _INSTALLED
    if _INSTALLED or os.environ.get("FEFW_LANG", "").lower() == "en":
        return
    _INSTALLED = True

    # 1) 控件构造时的 text=（ttk/tk 的 Widget.__init__ 把 cnf/kw 直接交给 Tcl，
    #    不经过 configure；只在构造函数这一处拦，能覆盖全部 Label / Button /
    #    Checkbutton / LabelFrame / Message 等的初始文案）
    orig_widget_init = tk.Widget.__init__

    def widget_init(self, master, widgetName, cnf=None, kw=None, extra=()):
        return orig_widget_init(self, master, widgetName, tr_map(cnf), tr_map(kw), extra)

    tk.Widget.__init__ = widget_init

    # 2) 之后所有控件的 text= 更新（Label.configure、flash() 的状态栏、
    #    Canvas 之外的 config 调用……）
    orig_configure = tk.Misc.configure

    def configure(self, cnf=None, **kw):
        if "text" in kw:
            kw["text"] = tr(kw["text"])
        if isinstance(cnf, dict) and cnf.get("text") is not None:
            cnf = dict(cnf)
            cnf["text"] = tr(cnf["text"])
        return orig_configure(self, cnf, **kw)

    tk.Misc.configure = configure
    tk.Misc.config = configure

    # 2) 窗口标题
    orig_title = tk.Wm.wm_title

    def wm_title(self, string=None):
        if string is None:
            return self.tk.call("wm", "title", self._w)
        return orig_title(self, tr(string))

    tk.Wm.wm_title = wm_title
    tk.Wm.title = wm_title

    # 3) 标签页
    orig_add = ttk.Notebook.add

    def nb_add(self, child, **kw):
        if kw.get("text") is not None:
            kw["text"] = tr(kw["text"])
        return orig_add(self, child, **kw)

    ttk.Notebook.add = nb_add
    orig_tab = ttk.Notebook.tab

    def nb_tab(self, tab_id, option=None, **kw):
        if kw.get("text") is not None:
            kw["text"] = tr(kw["text"])
        return orig_tab(self, tab_id, option, **kw)

    ttk.Notebook.tab = nb_tab

    # 4) 表格：表头 + 行文本 + 行内的显示值
    orig_heading = ttk.Treeview.heading

    def heading(self, column, option=None, **kw):
        if kw.get("text") is not None:
            kw["text"] = tr(kw["text"])
        return orig_heading(self, column, option, **kw)

    ttk.Treeview.heading = heading

    orig_insert = ttk.Treeview.insert

    def insert(self, parent, index, iid=None, **kw):
        if kw.get("text") is not None:
            kw["text"] = tr(kw["text"])
        if "values" in kw:
            v = kw["values"]
            if isinstance(v, (list, tuple)):
                kw["values"] = [tr(x) if isinstance(x, str) else x for x in v]
            elif isinstance(v, str):
                kw["values"] = tr(v)
        return orig_insert(self, parent, index, iid, **kw)

    ttk.Treeview.insert = insert

    orig_item = ttk.Treeview.item

    def item(self, item, option=None, **kw):
        if kw.get("text") is not None:
            kw["text"] = tr(kw["text"])
        return orig_item(self, item, option, **kw)

    ttk.Treeview.item = item

    # 5) 消息框 / 文件对话框
    for name in ("showinfo", "showwarning", "showerror",
                 "askquestion", "askokcancel", "askyesno", "askyesnocancel",
                 "askretrycancel"):
        fn = getattr(messagebox, name, None)
        if fn is None:
            continue

        def wrap(fn):
            def call(title=None, message=None, **options):
                if isinstance(title, str):
                    title = tr(title)
                if isinstance(message, str):
                    message = tr(message)
                return fn(title, message, **options)
            return call

        setattr(messagebox, name, wrap(fn))

    def wrap_dialog(fn):
        def call(**options):
            if isinstance(options.get("title"), str):
                options["title"] = tr(options["title"])
            ft = options.get("filetypes")
            if ft:
                options["filetypes"] = [(tr(d) if isinstance(d, str) else d, p) for d, p in ft]
            options.setdefault("parent", tk._default_root)
            return fn(**options)
        return call

    for name in ("askopenfilename", "asksaveasfilename", "askdirectory", "askopenfilenames"):
        fn = getattr(filedialog, name, None)
        if fn is not None:
            setattr(filedialog, name, wrap_dialog(fn))
