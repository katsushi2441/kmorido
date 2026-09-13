#!/usr/bin/env python3
"""市区町村ごとの盛土規制区域の統計を作る（地域ページの中身）。

A56 は admin_code（全国地方公共団体コード）と city を持っているので住所の解析は要らない。
市区町村ページに載せる数字はすべてここで実測する。

  cd /home/kojima/work/kmorido && /usr/bin/python3 scripts/build_muni_stats.py
"""
import os, json, sqlite3, collections

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "kmorido.sqlite")

DDL = """
CREATE TABLE IF NOT EXISTS muni_stats (
  admin_code TEXT PRIMARY KEY, pref_code TEXT, pref TEXT, city TEXT,
  areas INTEGER, takuchi INTEGER, morido INTEGER, both INTEGER,
  subject TEXT, notices INTEGER, first_eff TEXT, last_eff TEXT, samples TEXT
);
CREATE INDEX IF NOT EXISTS muni_stats_pref ON muni_stats(pref_code);
"""


def main():
    con = sqlite3.connect(DB)
    con.executescript(DDL)
    pref_of = {c: p for c, p in con.execute("SELECT pref_code, pref FROM datasets")}
    agg = {}
    for code, pc, city, atc, at, subj, no, eff in con.execute(
            "SELECT admin_code, pref_code, city, area_type_code, area_type, subject, notice_no, effective_date FROM areas"):
        a = agg.get(code)
        if a is None:
            a = agg[code] = dict(pref_code=pc, pref=pref_of.get(pc, ""), city=city, areas=0,
                                 takuchi=0, morido=0, both=0, subjects=collections.Counter(),
                                 notices=set(), effs=set(), samples=[])
        a["areas"] += 1
        if atc == "1": a["takuchi"] += 1
        elif atc == "2": a["morido"] += 1
        elif atc == "9": a["both"] += 1
        if subj: a["subjects"][subj] += 1
        if no: a["notices"].add(no)
        if eff: a["effs"].add(eff)
        if len(a["samples"]) < 5 and no:
            s = {"area_type": at, "notice_no": no, "effective_date": eff}
            if s not in a["samples"]:
                a["samples"].append(s)
    con.execute("DELETE FROM muni_stats")
    con.executemany(
        "INSERT INTO muni_stats (admin_code,pref_code,pref,city,areas,takuchi,morido,both,"
        "subject,notices,first_eff,last_eff,samples) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(code, a["pref_code"], a["pref"], a["city"], a["areas"], a["takuchi"], a["morido"],
          a["both"], (a["subjects"].most_common(1) or [("", 0)])[0][0], len(a["notices"]),
          min(a["effs"]) if a["effs"] else None, max(a["effs"]) if a["effs"] else None,
          json.dumps(a["samples"], ensure_ascii=False)) for code, a in agg.items()])
    con.commit()
    print(f"市区町村 {len(agg):,} / 区域 {sum(a['areas'] for a in agg.values()):,}")
    for r in con.execute("SELECT pref,city,areas,takuchi,morido,both,subject FROM muni_stats ORDER BY areas DESC LIMIT 5"):
        print(f"   {r[0]}{r[1]}: 区域{r[2]:,} 宅造{r[3]:,} 特定盛土{r[4]:,} 両方{r[5]:,} 指定者={r[6]}")
    con.close()


if __name__ == "__main__":
    main()
