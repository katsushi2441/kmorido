# -*- coding: utf-8 -*-
"""住所・座標 → 盛土規制法の規制区域の判定。

設計の芯（kriskarea / kflood と同じ約束）:
  1. 「区域外」と「未指定（データなし）」を必ず区別する。
     盛土規制法は2023年5月施行で、区域の指定は都道府県ごとに進行中。
     まだ指定していない県で黙って「区域外」と答えると、利用者は
     「規制が無い土地だ」と誤解する。
  2. 判定結果には必ず根拠（告示番号・施行年月日・指定主体）とデータ時点を添える。
  3. 住所から求めた座標は町丁目の代表点なので、近くに区域があるときは言い切らない。
  4. これは「盛土がある場所」ではなく「工事に許可・届出が要る区域」。
     大規模盛土造成地マップ（重ねるハザードマップ）とは別物であることを毎回書く。
  5. 概略位置であり法的図書ではない。申請資料には使えない（配布元の明示条件）。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "kmorido.sqlite"

#: 代表点がこれより近ければ「区域外」と言い切らない（度。約250m）
NEAR_DEG = 0.0025


@dataclass
class Area:
    id: int
    city: str
    admin_code: str
    area_type_code: str
    area_type: str
    subject: str
    notice_no: str
    effective_date: str


@dataclass
class Result:
    lat: float
    lon: float
    address: str = ""
    status: str = "uncovered"      # inside / outside / uncovered
    areas: list[Area] = field(default_factory=list)
    nearest_m: int | None = None
    notes: list[str] = field(default_factory=list)
    vintage: str = ""
    attribution: str = ""


class Index:
    """区域ポリゴンを一度だけ読んで空間索引に載せる。"""

    def __init__(self, db: Path = DB):
        self.db = db
        self._tree: STRtree | None = None
        self._rows: list[sqlite3.Row] = []
        self._geoms: list = []
        self._pref_codes: set[str] = set()
        self._admin_codes: set[str] = set()
        self.vintage = ""
        self.attribution = ""

    def load(self) -> None:
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        self._rows = conn.execute("SELECT * FROM areas").fetchall()
        self._geoms = [shape(json.loads(r["geometry"])) for r in self._rows]
        self._pref_codes = {r["pref_code"] for r in self._rows}
        self._admin_codes = {r["admin_code"] for r in self._rows}
        meta = conn.execute("SELECT data_vintage, attribution FROM datasets LIMIT 1").fetchone()
        if meta:
            self.vintage, self.attribution = meta["data_vintage"], meta["attribution"]
        conn.close()
        self._tree = STRtree(self._geoms) if self._geoms else None

    @property
    def count(self) -> int:
        return len(self._rows)

    @property
    def city_count(self) -> int:
        return len(self._admin_codes)

    @property
    def pref_count(self) -> int:
        return len(self._pref_codes)

    def _area(self, i: int) -> Area:
        r = self._rows[i]
        return Area(r["id"], r["city"] or "", r["admin_code"] or "",
                    r["area_type_code"] or "", r["area_type"] or "",
                    r["subject"] or "", r["notice_no"] or "", r["effective_date"] or "")

    def covers_pref(self, admin_code: str) -> bool:
        """その都道府県に規制区域の指定データがあるか。

        kriskarea と違って市区町村単位ではなく**都道府県単位**で見る。
        盛土規制法の区域は知事が県全体を見て指定するので、
        「県内に指定はあるが、この市には無い」は正常な状態（＝区域外）だからだ。
        指定が1件も無い県だけを「未指定」と呼ぶ。
        """
        return bool(admin_code) and admin_code[:2] in self._pref_codes

    def check(self, lat: float, lon: float, address: str = "", admin_code: str = "") -> Result:
        if self._tree is None:
            self.load()
        out = Result(lat=lat, lon=lon, address=address,
                     vintage=self.vintage, attribution=self.attribution)
        pt = Point(lon, lat)
        hit = [i for i in self._tree.query(pt) if self._geoms[i].covers(pt)]
        if hit:
            out.status = "inside"
            out.areas = [self._area(i) for i in hit]
            out.notes.append(
                "この区域内で一定規模以上の盛土・切土の工事をするときは、"
                "着手前に許可（または届出）が要ります。土地の売買では、"
                "宅地建物取引業法35条の重要事項説明の対象になります。")
            out.notes.append(
                "区域の線は概略です。実際の敷地が区域に入るかどうかは、"
                "告示番号をもとに指定した自治体（都道府県・指定都市・中核市）の担当課で確認してください。")
            return out

        covered = self.covers_pref(admin_code)
        near = pt.buffer(NEAR_DEG)
        neighbours = list(self._tree.query(near))
        if not covered and not neighbours:
            out.status = "uncovered"
            out.notes.append(
                "この住所の都道府県には、盛土規制法の規制区域データがまだありません。"
                "盛土規制法は2023年5月施行で、区域の指定は都道府県ごとに進んでいる途中です。"
                "「規制が無い」という意味ではありません。都道府県の担当課で最新の指定状況を確認してください。")
            return out

        out.status = "outside"
        if neighbours:
            d = min(self._geoms[i].distance(pt) for i in neighbours)
            out.nearest_m = int(d * 111_000)
            out.notes.append(
                f"最も近い規制区域まで約{out.nearest_m}mです。"
                "住所から求めた座標は町丁目のおおよその位置なので、"
                "実際の敷地が区域内である可能性があります。地番で確認してください。")
        out.notes.append(
            "規制区域の外であることは、その土地が安全という意味ではありません。"
            "盛土規制法の区域は「工事に許可・届出が要る範囲」を定めるもので、"
            "崖崩れや土砂災害の危険そのものを示すものではありません"
            "（土砂は土砂災害警戒区域、浸水は洪水・内水のハザードマップで確認してください）。")
        return out
