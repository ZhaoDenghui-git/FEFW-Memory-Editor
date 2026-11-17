"""给"考试票证"两族条目补中文名（只填空着的，不动已有译名）。

依据：日文名 `下級/中級/上級/最上級/戦象兵/踊り子 + 資格手形` 与
`Junior/Inter/High/Highest/Elephant/Dancer + License` 是同一套票证的两种写法；
表里已有的 `ElephantLicense = 战象兵考试票证` 说明游戏中文叫"考试票证"，
所以按同级对应翻译，重复条目族加"（重复条目）"以便和主条目区分。
"""
import csv
import shutil

P = "物品代码对照表.csv"
shutil.copyfile(P, P + ".bak")

MAP = {
    # 資格手形 族（日文名本来就是汉字）
    "df87da2a": "初级考试票证",
    "d4e73468": "中级考试票证",
    "d5b0de79": "上级考试票证",
    "6f937d07": "最上级考试票证",
    "6e42a917": "战象兵考试票证",
    "3e06380f": "舞娘考试票证",
    # License 族（英文标识，只在内"内部重复条目"里出现）
    "61469ef8": "初级考试票证（重复条目）",
    "693bb0c7": "中级考试票证（重复条目）",
    "236212bd": "上级考试票证（重复条目）",
    "eaf8b1b5": "最上级考试票证（重复条目）",
    "e2cb9f94": "战象兵考试票证（重复条目）",
    "e47c6492": "舞娘考试票证（重复条目）",
}

rows = list(csv.DictReader(open(P, encoding="utf-8-sig")))
field = list(rows[0].keys())
done = 0
for r in rows:
    iid = r["物品ID"].lower()
    if iid in MAP and not r["中文名"].strip():
        r["中文名"] = MAP[iid]
        done += 1
print(f"补了 {done} 条（应为 12 条；已在表里写过中文的会跳过）")

with open(P, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=field)
    w.writeheader()
    w.writerows(rows)

print("\n回读校验：")
for r in csv.DictReader(open(P, encoding="utf-8-sig")):
    if r["物品ID"].lower() in MAP:
        print(f"  {r['内部编号b0b1']:>5} | {r['物品ID']} | {r['中文名']:<24} | 日={r['日文名']}")
