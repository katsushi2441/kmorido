#!/usr/bin/env python3
"""国土数値情報 大規模盛土造成地（A54）を SQLite に取り込む。

  /usr/bin/python3 scripts/load_a54.py 23        # 愛知県
  /usr/bin/python3 scripts/load_a54.py           # 配布されている都道府県を全部

**A56（規制区域）とは別のもの**。README に書いてあるとおり、
  A56 = そこで盛土・切土をするには許可・届出が要る「法的な線」
  A54 = そこに**すでに盛土がある**という「土地の成り立ち」
の違いで、同じ住所について両方を返せるようにするために足す。

**配布が GML（XML）しかない。** Shapefile も GeoJSON も無く、GDAL の GML ドライバでも
レイヤとして開けない（ksj 独自スキーマ + xlink 参照のため）。なので自前で読む:
  <gml:Curve gml:id="cvN"><gml:posList>緯度 経度 緯度 経度 …</gml:posList>
  <gml:Surface gml:id="sfN"><gml:exterior>… xlink:href="#cvN" …
  <ksj:LargeScaleFillSlope><ksj:bounds xlink:href="#sfN"/> …属性…
座標は「緯度 経度」の順なので、GeoJSON 用に [経度, 緯度] へ入れ替える。

盛土区分のコードは国の対応表（codelist/fillSlopeClassification.xlsx）どおり:
  1=谷埋め型 / 2=腹付け型 / 9=区分をしていない。**当社で推測した名前は付けない。**

利用条件: CC BY 4.0（出典表示のみ）。ただし「概ねの範囲」であり法的図書ではない。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://nlftp.mlit.go.jp/ksj/gml/data/A54/A54-23"
RAW = Path(os.environ.get("KMORIDO_RAW_DIR", "/mnt/data/kmorido/raw"))
DB = ROOT / "data" / "kmorido.sqlite"
VINTAGE = "2023年度（令和5年度）版・データ基準年月日 2023年3月31日"
ATTRIBUTION = "出典: 国土数値情報（大規模盛土造成地）国土交通省 を加工して作成（CC BY 4.0）"
UA = {"User-Agent": "kmorido/1.0 (kurage.exbridge.jp)"}

# 国土数値情報の対応表（codelist/fillSlopeClassification.xlsx）をそのまま使う
FILL_CLASS = {"1": "谷埋め型", "2": "腹付け型", "9": "区分をしていない"}

PREFS = [f"{i:02d}" for i in range(1, 48)]


def fetch(pref: str) -> Path | None:
    RAW.mkdir(parents=True, exist_ok=True)
    name = f"A54-23_{pref}_GML.zip"
    path = RAW / name
    if path.exists() and path.stat().st_size:
        return path
    try:
        req = urllib.request.Request(f"{BASE}/{name}", headers=UA)
        with urllib.request.urlopen(req, timeout=600) as r, open(str(path) + ".part", "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
        os.replace(str(path) + ".part", path)
        return path
    except Exception:
        # 大規模盛土造成地マップを公表していない県は配布が無い（404）。異常ではない。
        Path(str(path) + ".part").unlink(missing_ok=True)
        return None


def parse_gml(text: str) -> list[dict]:
    """ksj の GML から {属性 + ring} を取り出す。ring は [経度, 緯度] の並び。"""
    curves: dict[str, list] = {}
    for m in re.finditer(r'<gml:Curve gml:id="([^"]+)".*?<gml:posList>(.*?)</gml:posList>', text, re.S):
        nums = m.group(2).split()
        curves[m.group(1)] = [[float(nums[i + 1]), float(nums[i])] for i in range(0, len(nums) - 1, 2)]
    surfaces: dict[str, list] = {}
    for m in re.finditer(r'<gml:Surface gml:id="([^"]+)".*?<gml:exterior>(.*?)</gml:exterior>', text, re.S):
        ring: list = []
        for ref in re.findall(r'xlink:href="#([^"]+)"', m.group(2)):
            ring.extend(curves.get(ref, []))
        if len(ring) >= 4:
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            surfaces[m.group(1)] = ring
    out = []
    for m in re.finditer(r"<ksj:LargeScaleFillSlope[^>]*>(.*?)</ksj:LargeScaleFillSlope>", text, re.S):
        body = m.group(1)
        href = re.search(r'<ksj:bounds xlink:href="#([^"]+)"', body)
        ring = surfaces.get(href.group(1)) if href else None
        if not ring:
            continue

        def g(tag: str) -> str:
            mm = re.search(rf"<ksj:{tag}>(.*?)</ksj:{tag}>", body)
            return mm.group(1).strip() if mm else ""

        out.append(dict(cls=g("fillSlopeClassification"), pref_code=g("prefectureCode"),
                        pref=g("prefectureName"), admin_code=g("administrativeCode"),
                        city=g("localGovernmentName"), number=g("fillSlopeNumber"), ring=ring))
    return out


def schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS fill_slopes (
          id INTEGER PRIMARY KEY,
          pref_code TEXT NOT NULL, pref TEXT,
          admin_code TEXT, city TEXT,
          class_code TEXT, class_name TEXT, number TEXT,
          minx REAL, miny REAL, maxx REAL, maxy REAL,
          geometry TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS fill_bbox ON fill_slopes(minx, maxx, miny, maxy);
        CREATE INDEX IF NOT EXISTS fill_admin ON fill_slopes(admin_code);
        CREATE TABLE IF NOT EXISTS fill_datasets (
          pref_code TEXT PRIMARY KEY, pref TEXT, count INTEGER,
          data_vintage TEXT, attribution TEXT, loaded_at TEXT);
        """
    )


def load_pref(conn: sqlite3.Connection, pref: str) -> int:
    path = fetch(pref)
    if not path:
        return -1
    with zipfile.ZipFile(path) as z:
        name = next((n for n in z.namelist() if n.endswith(".xml") and "KS-META" not in n), None)
        if not name:
            return -1
        text = z.read(name).decode("utf-8", "replace")
    feats = parse_gml(text)
    conn.execute("DELETE FROM fill_slopes WHERE pref_code=?", (pref,))
    kept, unknown = 0, set()
    pref_name = ""
    for f in feats:
        ring = f["ring"]
        xs = [c[0] for c in ring]
        ys = [c[1] for c in ring]
        code = f["cls"]
        if code and code not in FILL_CLASS:
            unknown.add(code)
        pref_name = pref_name or f["pref"]
        conn.execute(
            """INSERT INTO fill_slopes
               (pref_code,pref,admin_code,city,class_code,class_name,number,minx,miny,maxx,maxy,geometry)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pref, f["pref"], f["admin_code"], f["city"], code, FILL_CLASS.get(code, ""),
             f["number"], min(xs), min(ys), max(xs), max(ys),
             json.dumps({"type": "Polygon", "coordinates": [ring]}, ensure_ascii=False)))
        kept += 1
    conn.execute("""INSERT OR REPLACE INTO fill_datasets
                    (pref_code,pref,count,data_vintage,attribution,loaded_at) VALUES (?,?,?,?,?,?)""",
                 (pref, pref_name, kept, VINTAGE, ATTRIBUTION, time.strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    if unknown:
        print(f"  ！ 対応表に無い盛土区分コード: {sorted(unknown)}（名前は付けずに保存した）", file=sys.stderr)
    return kept


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prefs", nargs="*", help="都道府県コード（省略で全国）")
    a = ap.parse_args()
    conn = sqlite3.connect(DB)
    schema(conn)
    total, loaded = 0, 0
    for p in (a.prefs or PREFS):
        n = load_pref(conn, p)
        if n < 0:
            continue
        loaded += 1
        total += n
        print(f"  {p}: {n:,}件")
    conn.close()
    print(f"大規模盛土造成地: {total:,}件 / {loaded}都道府県 → {DB}")


if __name__ == "__main__":
    main()
