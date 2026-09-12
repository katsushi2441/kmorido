#!/usr/bin/env python3
"""国土数値情報 宅地造成等工事規制区域・特定盛土等規制区域（A56）を SQLite に取り込む。

  /usr/bin/python3 scripts/load_a56.py            # 全国（未指定の県は自動で飛ばす）
  /usr/bin/python3 scripts/load_a56.py 23 22      # 都道府県コードを指定

なぜ PostGIS ではなく SQLite か:
  区域は全国で数万件。kflood の洪水（2,800万面）と違って shapely の STRtree で足りる。
  買い切りキットとして配る以上、docker も postgres も要らない方が導入が速い（kriskarea と同じ判断）。

利用条件:
  A56 は CC BY 4.0。出典表示だけで商用利用できる（kriskarea の A48 と違い、
  自治体ごとの利用条件の差が無いので除外リストは持たない）。
  ただし「概略的な位置であり法的図書ではない」と明記されているので、
  画面でも必ずそう書く。申請資料には使えない。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.codes import AREA_TYPE, SUBJECT  # noqa: E402

BASE = "https://nlftp.mlit.go.jp/ksj/gml/data/A56/A56-25"
RAW = Path(os.environ.get("KMORIDO_RAW_DIR", "/mnt/data/kmorido/raw"))
DB = ROOT / "data" / "kmorido.sqlite"
VINTAGE = "2025年度（令和7年度）版・2025年7月18日時点"
ATTRIBUTION = "出典: 国土数値情報（宅地造成等工事規制区域・特定盛土等規制区域）国土交通省 を加工して作成（CC BY 4.0）"
UA = {"User-Agent": "kmorido/1.0 (kurage.exbridge.jp)"}


def fetch(pref: str) -> Path | None:
    RAW.mkdir(parents=True, exist_ok=True)
    name = f"A56-25_{pref}_GML.zip"
    path = RAW / name
    if path.exists() and path.stat().st_size:
        return path
    try:
        req = urllib.request.Request(f"{BASE}/{name}", headers=UA)
        with urllib.request.urlopen(req, timeout=300) as r, open(str(path) + ".part", "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
        os.replace(str(path) + ".part", path)
        return path
    except Exception:
        # まだ区域を指定していない都道府県は配布されていない（404）。異常ではない。
        Path(str(path) + ".part").unlink(missing_ok=True)
        return None


def schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS areas (
          id INTEGER PRIMARY KEY,
          pref_code TEXT NOT NULL,
          admin_code TEXT, city TEXT,
          area_type_code TEXT, area_type TEXT,
          subject_code TEXT, subject TEXT,
          notice_no TEXT, effective_date TEXT,
          minx REAL, miny REAL, maxx REAL, maxy REAL,
          geometry TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS areas_bbox ON areas(minx, maxx, miny, maxy);
        CREATE INDEX IF NOT EXISTS areas_admin ON areas(admin_code);
        CREATE TABLE IF NOT EXISTS datasets (
          pref_code TEXT PRIMARY KEY, pref TEXT, count INTEGER,
          data_vintage TEXT, attribution TEXT, loaded_at TEXT);
        """
    )


PREF_NAMES = {
    "01": "北海道", "02": "青森県", "03": "岩手県", "04": "宮城県", "05": "秋田県",
    "06": "山形県", "07": "福島県", "08": "茨城県", "09": "栃木県", "10": "群馬県",
    "11": "埼玉県", "12": "千葉県", "13": "東京都", "14": "神奈川県", "15": "新潟県",
    "16": "富山県", "17": "石川県", "18": "福井県", "19": "山梨県", "20": "長野県",
    "21": "岐阜県", "22": "静岡県", "23": "愛知県", "24": "三重県", "25": "滋賀県",
    "26": "京都府", "27": "大阪府", "28": "兵庫県", "29": "奈良県", "30": "和歌山県",
    "31": "鳥取県", "32": "島根県", "33": "岡山県", "34": "広島県", "35": "山口県",
    "36": "徳島県", "37": "香川県", "38": "愛媛県", "39": "高知県", "40": "福岡県",
    "41": "佐賀県", "42": "長崎県", "43": "熊本県", "44": "大分県", "45": "宮崎県",
    "46": "鹿児島県", "47": "沖縄県",
}


def bbox(geom: dict) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []

    def walk(c):
        if isinstance(c, (int, float)):
            return
        if c and isinstance(c[0], (int, float)):
            xs.append(c[0]); ys.append(c[1]); return
        for x in c:
            walk(x)

    walk(geom["coordinates"])
    return min(xs), min(ys), max(xs), max(ys)


def load_pref(conn: sqlite3.Connection, pref: str) -> int:
    path = fetch(pref)
    if not path:
        return -1
    with zipfile.ZipFile(path) as z:
        target = next((n for n in z.namelist() if n.endswith(".geojson")), None)
        if not target:
            return -1
        data = json.loads(z.read(target).decode("utf-8"))
    conn.execute("DELETE FROM areas WHERE pref_code=?", (pref,))
    kept = 0
    unknown: set[str] = set()
    for f in data.get("features", []):
        p = f.get("properties") or {}
        g = f.get("geometry")
        if not g or not g.get("coordinates"):
            continue
        t = str(p.get("A56_004") or "")
        s = str(p.get("A56_005") or "")
        if t and t not in AREA_TYPE:
            unknown.add(f"区域区分={t}")
        if s and s not in SUBJECT:
            unknown.add(f"指定主体={s}")
        minx, miny, maxx, maxy = bbox(g)
        conn.execute(
            """INSERT INTO areas
               (pref_code,admin_code,city,area_type_code,area_type,subject_code,subject,
                notice_no,effective_date,minx,miny,maxx,maxy,geometry)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pref, str(p.get("A56_002") or ""), p.get("A56_003"),
             t, AREA_TYPE.get(t, ""), s, SUBJECT.get(s, ""),
             p.get("A56_006"), p.get("A56_007"),
             minx, miny, maxx, maxy, json.dumps(g, ensure_ascii=False)),
        )
        kept += 1
    conn.execute(
        """INSERT OR REPLACE INTO datasets(pref_code,pref,count,data_vintage,attribution,loaded_at)
           VALUES(?,?,?,?,?,?)""",
        (pref, PREF_NAMES.get(pref, ""), kept, VINTAGE, ATTRIBUTION,
         time.strftime("%Y-%m-%d %H:%M:%S")),
    )
    note = f"（未知のコード: {', '.join(sorted(unknown))}）" if unknown else ""
    print(f"  {pref} {PREF_NAMES.get(pref,''):　<5}: {kept:,}件 {note}")
    return kept


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prefs", nargs="*", help="都道府県コード(2桁)。省略で全国")
    a = ap.parse_args()
    prefs = a.prefs or [f"{i:02d}" for i in range(1, 48)]
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    schema(conn)
    total, missing = 0, []
    for pref in prefs:
        n = load_pref(conn, pref)
        if n < 0:
            missing.append(f"{pref} {PREF_NAMES.get(pref,'')}")
        else:
            total += n
    conn.commit()
    cities = conn.execute("SELECT COUNT(DISTINCT admin_code) FROM areas").fetchone()[0]
    by_type = conn.execute(
        "SELECT area_type, COUNT(*) FROM areas GROUP BY area_type ORDER BY 2 DESC").fetchall()
    conn.close()
    print(f"\n合計 {total:,}件 / {cities}市区町村")
    for name, n in by_type:
        print(f"  {name}: {n:,}件")
    if missing:
        print(f"\n配布なし（まだ規制区域を指定・公開していない都道府県 {len(missing)}）:")
        print("  " + " / ".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
