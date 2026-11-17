"""build_zh_names.py - official Simplified Chinese names for the editor's tables.

The game ships its own Chinese text: romfs/Msg/CNzh/*.msbt.zst carries the same
labels as Msg/JPja and Msg/EUen, so a Japanese name and its Chinese name sit
under one label.  This adds a "zh" field next to the existing "jp"/"en" of

    item_ids.json   items          (Item.msbt)
    job_ids.json    classes        (Job.msbt)
    skill_ids.json  arts / skills  (Skill.msbt)
    unit_ids.json   people / units (Person.msbt + ids)

Nothing else changes: "jp"/"en" stay exactly as they are, because they are the
search keys and the data keys the editor matches saves against.  The editor
shows "zh" when it exists and falls back to "en" when it doesn't.

    python build_zh_names.py [romfs folder]     (or set FEFW_ROM)

Matching mirrors build_item_names.py / build_unit_names.py, so a name the
English build could match also gets a Chinese name here.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import msbt                                       # noqa: E402

ROM = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("FEFW_ROM", "")
LANG = "CNzh"                                     # Simplified Chinese folder


# ---------------------------------------------------------------------------
# message files -> lookup tables
# ---------------------------------------------------------------------------
def _pairs(rom: str, lang: str):
    """({jp text: zh text}, {en text: zh text}, {label: zh text}) from the dump.

    Files whose names start with Item / Person / Job are read first so their
    wording wins when two files spell a word differently (that is what
    build_unit_names.py does with Person/Job, and build_item_names.py with Item).
    Help texts (_H_ labels) are skipped: they are paragraphs, not names.
    """
    jp_dir = os.path.join(rom, "Msg", "JPja")
    zh_dir = os.path.join(rom, "Msg", LANG)
    if not os.path.isdir(zh_dir):
        raise SystemExit("no %s folder under %s\\Msg" % (LANG, rom))
    first = ("item", "person", "job")
    files = sorted(glob.glob(os.path.join(jp_dir, "*.msbt*")),
                   key=lambda p: next((i for i, f in enumerate(first)
                                      if os.path.basename(p).lower().startswith(f)),
                                     len(first)))
    jp2zh: dict[str, str] = {}
    en2zh: dict[str, str] = {}
    by_label: dict[str, str] = {}
    for jp_path in files:
        zh_path = os.path.join(zh_dir, os.path.basename(jp_path))
        if not os.path.isfile(zh_path) or not os.path.getsize(jp_path):
            continue
        try:
            jp = msbt.read(jp_path)
            zh = msbt.read(zh_path)
        except Exception as exc:                  # noqa: BLE001 - skip unreadable file
            print("  skipped %s: %s" % (os.path.basename(jp_path), exc))
            continue
        en_path = os.path.join(msbt.english_dir(os.path.join(rom, "Msg")),
                               os.path.basename(jp_path))
        en = msbt.read(en_path) if os.path.isfile(en_path) else {}
        for label, jtext in jp.items():
            if "_H_" in label:
                continue
            ztext = zh.get(label) or ""
            if not jtext or not ztext:
                continue
            by_label.setdefault(label, ztext)
            jp2zh.setdefault(jtext, ztext)
            etext = (en.get(label) or "").strip()
            if etext:
                en2zh.setdefault(etext, ztext)
    return jp2zh, en2zh, by_label


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------
# ids the game's text never names (cut or unused content).  build_item_names.py
# fills these with hand English names; for Chinese we only reuse a Chinese name
# when the English one is itself a game name, so nothing gets invented here.
def _item_candidates(jp: str) -> list[str]:
    """Every spelling under which the game may know this item's id."""
    jp = unicodedata.normalize("NFKC", jp or "").strip()
    m = re.search(r"\+(\d+)$", jp)
    base = jp[:m.start()] if m else jp
    stem = re.sub(r"_SC\d+_\d+$", "", base)
    out = [jp, base, stem]
    if stem.startswith("デモ_"):
        out.append(stem[len("デモ_"):])
    if stem.endswith("_戦技なし"):
        out.append(stem[:-len("_戦技なし")])
    # per-chapter copies: 木製の剣_TUTO_01 / …_チュートリアル
    out.append(re.sub(r"_(?:SC\d+_\d+|TUTO_\d+|チュートリアル)$", "", stem))
    # older working names kept in the ids (see build_item_names.OLD_NAMES)
    if stem in ("燧結晶", "FlintCrystal"):
        out.append("熱素鉱")
    # skill books are 戦術書 / 秘蔵の戦術書 in the ids but 戦術指南書 in game
    if stem.endswith("書") and not stem.endswith("指南書"):
        s2 = stem[:-1]
        if s2.startswith("秘蔵の"):
            out.append("上級" + s2[len("秘蔵の"):] + "指南書")
        out.append(s2 + "指南書")
    return out


def fill_items(items: dict, jp2zh: dict, en2zh: dict) -> tuple[int, int]:
    hit = 0
    for meta in items.values():
        raw = meta.get("jp") or ""
        jp = unicodedata.normalize("NFKC", raw).strip()
        m = re.search(r"\+(\d+)$", jp)
        zh = ""
        for cand in _item_candidates(raw):
            zh = jp2zh.get(cand, "")
            if zh:
                break
        if not zh:
            # forged copies / cutscene props: the English name is already the
            # game's own, so the Chinese text under the same label is right too
            en = unicodedata.normalize("NFKC", meta.get("en") or "").strip()
            base_en = re.sub(r"\s*\+\d+$", "", en)
            zh = en2zh.get(base_en, "")
            if not zh:
                for cand in _item_candidates(raw):
                    zh = en2zh.get(unicodedata.normalize("NFKC", cand).strip(), "")
                    if zh:
                        break
        if zh:
            if m and not re.search(r"\+", zh):
                zh = "%s+%s" % (zh, m.group(1))
            meta["zh"] = zh
            hit += 1
        else:
            meta.pop("zh", None)
    return hit, len(items)


# ---------------------------------------------------------------------------
# classes / skills
# ---------------------------------------------------------------------------
def fill_jobs(jobs: dict, jp2zh: dict, en2zh: dict) -> tuple[int, int]:
    hit = 0
    for meta in jobs.values():
        jp = unicodedata.normalize("NFKC", meta.get("jp") or "").strip()
        zh = jp2zh.get(jp) or jp2zh.get(re.sub(r"^JID_", "", jp)) or ""
        if not zh:
            zh = en2zh.get((meta.get("en") or "").strip(), "")
        # 下馬 / 上馬: the dismounted / mounted variant of a class.  The game has
        # no text of its own for these ids, so the class's own Chinese name plus
        # a parenthesised marker is the only honest option (marked 派生 below).
        if not zh:
            for suffix, tag in (("下馬", "（下马）"), ("上馬", "（上马）")):
                if jp.endswith(suffix) and len(jp) > len(suffix):
                    base = jp[:-len(suffix)]
                    z = (jp2zh.get(base) or jp2zh.get(re.sub(r"^JID_", "", base)) or "")
                    if z:
                        zh = z + tag
                        meta["zh_src"] = "derived"
                        break
        if not zh:
            zh = CN_SPECIAL.get(re.sub(r"^JID_", "", jp), "")
            if zh:
                meta["zh_src"] = "derived"
        if zh:
            meta["zh"] = zh
            hit += 1
        else:
            meta.pop("zh", None)
            meta.pop("zh_src", None)
    return hit, len(jobs)


def fill_skills(skills: dict, jp2zh: dict, en2zh: dict) -> tuple[int, int]:
    hit = 0
    for meta in skills.values():
        sid = meta.get("sid") or ""
        if sid.startswith("SID_>"):          # category header, not an art
            meta.pop("zh", None)
            continue
        jp = unicodedata.normalize("NFKC", meta.get("jp") or "").strip()
        zh = jp2zh.get(jp) or ""
        if not zh:
            zh = en2zh.get((meta.get("en") or "").strip(), "")
        if zh:
            meta["zh"] = zh
            hit += 1
        else:
            meta.pop("zh", None)
    return hit, len(skills)


# ---------------------------------------------------------------------------
# people / units
# ---------------------------------------------------------------------------
CJK = re.compile(r"[぀-ヿ㐀-鿿]")

# ids whose name the game never spells out; these are the editor's own words
CN_SPECIAL = {"無し": "无", "無職": "无职业"}

# the same template word lists build_unit_names.py uses, in Chinese
CN_GLOSSARY = {
    "標準": "", "汎用": "",            # 不是名字的一部分，英文版同样留空
    "冥界": "冥界", "冥": "冥界", "強": "强", "弱": "弱", "中": "中", "最強": "最强",
    "ボス": "首领", "右": "右", "左": "左", "攻撃": "攻击", "防御": "防御",
    "魔法": "魔法", "物理": "物理",
    "力強化": "力量+", "技強化": "技巧+", "速さ強化": "速度+", "守備強化": "防守+",
    "魔力強化": "魔力+", "魔防強化": "魔防+", "幸運強化": "幸运+", "HP強化": "生命+",
    "守備劣化": "防守-", "速さ劣化": "速度-", "魔防劣化": "魔防-", "力劣化": "力量-",
    "必殺無効": "免疫必杀", "回避": "回避高", "計略": "计策型", "アサシン": "刺客",
    "アサシン型": "刺客型", "外套": "披风", "外套あり": "有披风", "霊体": "灵体",
    "護衛": "护卫", "0部": "序章", "1部": "第一部", "2部": "第二部", "3部": "第三部",
    "第1部": "第一部", "第2部": "第二部", "第3部": "第三部",
    "腰の武器なし": "无腰间武器", "OP": "开场", "大型": "大型", "小型": "小型",
    "召喚": "召唤", "受付": "接待", "市場受付": "市场接待",
    "武器屋受付": "武器店接待", "道具屋受付": "道具店接待",
    "訓練所受付": "训练所接待", "モブ男性": "男路人", "モブ女性": "女路人",
    "モブ少年": "少年路人", "モブ少女": "少女路人",
    "村の男性": "村民（男）", "村の女性": "村民（女）", "街の男性": "镇民（男）",
    "街の女性": "镇民（女）", "裕福な男性": "富商（男）", "裕福な女性": "富商（女）",
    "男性神官": "神官（男）", "女性神官": "神官（女）", "試練兵": "试炼兵",
    "近衛兵": "近卫兵", "ツワモノ": "老练兵", "歩行兵士": "步行兵",
    "興行師": "艺人", "歩兵": "步兵", "騎馬": "骑兵", "駝鳥": "驼鸟骑士",
    "ノブタ": "野猪", "ドラゴンナイト": "龙骑士", "巨鳥ルフ": "巨鸟鲁弗",
}
CN_WEAPON = {"手斧": "手斧", "手槍": "标枪", "素手": "徒手", "籠手": "护手",
             "剣": "剑", "槍": "枪", "斧": "斧", "弓": "弓", "炎": "火", "氷": "冰",
             "雷": "雷", "風": "风", "毒": "毒", "スケルトン": "骷髅"}
CN_SUFFIX = {"替": "替身", "代理": "替身", "小": "小", "大": "大",
             "弱": "弱", "金": "金", "銀": "银"}
CN_GROUPS = {"刃鳴り": "鸣刃团", "モブ": "路人", "第1部": "第一部",
             "第2部": "第二部", "第3部": "第三部", "0部": "序章",
             "1部": "第一部", "2部": "第二部", "3部": "第三部"}
CN_PREFIX = re.compile(r"^(PID_|DN_|MN_)?([A-Z]{2}_(\d+_)?|SC[0-9X]+_[0-9X]+_?|BD\d+_\d+_)?")


def cn_piece(tok: str, jp2zh: dict, depth: int = 0):
    """One word of a template id -> (Chinese word, [modifier tags]) or None."""
    if depth > 4 or not tok:
        return None
    zh = jp2zh.get(tok)
    if zh:
        return zh, []
    if tok in CN_GLOSSARY and tok not in ("男", "女"):
        return CN_GLOSSARY[tok], []
    m = re.match(r"^モブ(\d*)$", tok)
    if m:
        return "路人" + (m.group(1) or ""), []
    m = re.match(r"^(.+?)(\[(.+)\]|[A-Z]|\d+)$", tok)
    if m:                                     # 盗賊_ローグ_A -> A 只是变体标号
        got = cn_piece(m.group(1), jp2zh, depth + 1)
        if got:
            tag = CN_GLOSSARY.get(m.group(3) or m.group(2), "")
            return got[0], got[1] + ([tag] if tag else [])
    for suf, tag in CN_SUFFIX.items():
        if tok.endswith(suf) and len(tok) > len(suf):
            got = cn_piece(tok[:-len(suf)], jp2zh, depth + 1)
            if got:
                return got[0], got[1] + [tag]
    if tok.startswith("女") and len(tok) > 1:
        got = cn_piece(tok[1:], jp2zh, depth + 1)
        if got:
            return got[0], got[1] + ["女"]
    for pre, word in CN_WEAPON.items():
        if tok.startswith(pre) and len(tok) > len(pre):
            got = cn_piece(tok[len(pre):], jp2zh, depth + 1)
            if got:
                return word + got[0], got[1]
    for i in range(2, len(tok) - 1):          # クラプカ近衛兵 = クラプカ + 近衛兵
        if tok[:i] in jp2zh:
            got = cn_piece(tok[i:], jp2zh, depth + 1)
            if got:
                return jp2zh[tok[:i]] + got[0], got[1]
    return None


def cn_unit_name(name: str, jp2zh: dict) -> str:
    """Chinese name for an internal unit template, or "" when unreadable.

    PID_SCXX_02_アーチャー        -> 弓兵
    DN_アトラスベア_力強化_冥界    -> 阿特拉斯熊（力量+、冥界）
    """
    s = CN_PREFIX.sub("", name or "").replace("\ufffd", "")
    test = s.startswith("テスト")
    if test:
        s = re.sub(r"^テスト(DN_|クイックバトル_)?", "", s)
    s = re.sub(r"^(動物|拠点|兵種)_", "", s)
    parts = [p for p in s.split("_") if p]
    lead = ["测试"] if test else []
    while len(parts) > 1 and parts[0] in CN_GROUPS:
        lead.append(CN_GROUPS[parts.pop(0)])
    if parts and parts[0].startswith("冥界") and len(parts[0]) > 2:
        parts[0] = parts[0][2:]
        lead.append("冥界")
    for n in range(len(parts), 0, -1):
        got = cn_piece("_".join(parts[:n]), jp2zh)
        if not got:
            continue
        main, tags = got
        tags = list(tags)
        for p in parts[n:]:
            t = CN_GLOSSARY.get(p)
            if t is None:
                sub = cn_piece(p, jp2zh)
                t = sub[0] if sub else (p if not CJK.search(p) else "")
            if t:
                tags.append(t)
        tags += [x for x in lead if x != "测试"]
        m = re.match(r"^(.*)（(.*)）$", main)   # 村民（男）+ 其它标记
        if m and tags:
            main, tags = m.group(1), [m.group(2)] + tags
        if test:
            tags.insert(0, "测试")
        return main + ("（%s）" % "、".join(tags) if tags else "")
    return ""


def fill_units(units: dict, jp2zh: dict, en2zh: dict, by_label: dict) -> tuple[int, int]:
    hit = 0
    for meta in units.values():
        pid = meta.get("pid") or ""
        zh = ""
        # story characters: the person's own entry in Person.msbt, matched by
        # the id (PID_Fabio -> MPID_Fabio), which is exact
        for label in (("MPID_" + pid[4:], pid, "PID_" + pid[4:])
                      if pid.startswith("PID_") else (pid,)):
            zh = by_label.get(label, "")
            if zh:
                break
        if not zh:
            name = unicodedata.normalize("NFKC", meta.get("name") or "").strip()
            zh = en2zh.get(name, "")
        if not zh:
            jp = unicodedata.normalize("NFKC", meta.get("jp") or "").strip()
            zh = jp2zh.get(jp, "")
        if not zh:
            # internal unit templates (PID_SCXX_02_アーチャー, DN_…_力強化):
            # assembled from the game's own Chinese words, marked 派生
            zh = cn_unit_name(meta.get("jp") or pid, jp2zh)
            if zh:
                meta["zh_src"] = "derived"
        if zh:
            meta["zh"] = zh
            hit += 1
        else:
            meta.pop("zh", None)
            meta.pop("zh_src", None)
    return hit, len(units)


# ---------------------------------------------------------------------------
# support chart (supports.json: "pairs" with jp a/b and English a_en/b_en)
# ---------------------------------------------------------------------------
def fill_supports(path: str, jp2zh: dict) -> tuple[int, int]:
    """a_zh / b_zh for every support pair; the page shows them with the chart."""
    data = json.load(open(path, encoding="utf8"))
    prs = data.get("pairs") or []
    hit = 0
    for pr in prs:
        for side in ("a", "b"):
            zh = jp2zh.get(pr.get(side) or "", "")
            if zh:
                pr[side + "_zh"] = zh
            else:
                pr.pop(side + "_zh", None)
        if pr.get("a_zh") or pr.get("b_zh"):
            hit += 1
    with open(path, "w", encoding="utf8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    return hit, len(prs)


# ---------------------------------------------------------------------------
def main() -> int:
    if not os.path.isdir(os.path.join(ROM, "Msg")):
        print("usage: python build_zh_names.py <path to your dumped romfs folder>\n"
              "       (or set FEFW_ROM to it)")
        return 2
    jp2zh, en2zh, by_label = _pairs(ROM, LANG)
    print("中文词条：jp->zh %d 条，en->zh %d 条，按 label %d 条"
          % (len(jp2zh), len(en2zh), len(by_label)))

    for name, fill in (("item_ids.json", fill_items), ("job_ids.json", fill_jobs),
                       ("skill_ids.json", fill_skills),
                       ("unit_ids.json", lambda d, a, b: fill_units(d, a, b, by_label))):
        path = os.path.join(HERE, name)
        data = json.load(open(path, encoding="utf8"))
        hit, total = fill(data, jp2zh, en2zh)
        with open(path, "w", encoding="utf8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        print("%-15s %5d/%5d 条有官方中文 (%.0f%%) -> %s"
              % (name, hit, total, 100.0 * hit / max(total, 1), path))
    sup = os.path.join(HERE, "supports.json")
    if os.path.isfile(sup):
        hit, total = fill_supports(sup, jp2zh)
        print("%-15s %5d/%5d 组支援对有人名中文 (%.0f%%) -> %s"
              % ("supports.json", hit, total, 100.0 * hit / max(total, 1), sup))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
