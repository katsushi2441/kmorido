# -*- coding: utf-8 -*-
"""Kurage 盛土規制区域マップ（内部の略称 kmorido）

住所を入れると、その場所が盛土規制法（宅地造成及び特定盛土等規制法）の
「宅地造成等工事規制区域」「特定盛土等規制区域」に入っているかを返す。
入っていれば、区域の種別・指定した自治体・告示番号・施行年月日まで出す。

大規模盛土造成地マップとの違い（いちばん混同される）:
  大規模盛土造成地マップは「そこに盛土がある」を示す図。
  盛土規制法の規制区域は「そこで盛土等の工事をするには許可・届出が要る」という法的な線。
  土地を買う・造成する前に効くのは後者で、住所で引ける横断的な道具が無かった
  （実務では「◯◯県 宅地造成工事規制区域」と検索して県ごとのPDFを探している）。

構成:
  ジオコーディング: 国土地理院 AddressSearch API（無料・キー不要）
  判定            : SQLite + shapely（10万件なので PostGIS は要らない）
  データ          : 国土数値情報 A56（CC BY 4.0）。概略位置であり法的図書ではない
"""
import json
import os
import re
from datetime import date

import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.codes import AREA_MEANING
from app.lookup import Index

PORT = int(os.environ.get("KMORIDO_PORT", "18312"))
SITE = os.environ.get("KMORIDO_SITE_NAME", "Kurage 盛土規制区域マップ")
PUBLIC_BASE = os.environ.get("KMORIDO_PUBLIC_BASE", "https://kurage.exbridge.jp/kmorido.php").rstrip("/")
GSI = "https://msearch.gsi.go.jp/address-search/AddressSearch"
UA = {"User-Agent": "kmorido/1.0 (kurage.exbridge.jp)"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(ROOT, "app", "templates"))
app = FastAPI(title=SITE)
app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "app", "static")), name="static")
INDEX = Index()

LINKS = {
    # 買い切り版の商品ページ。デモから商品へ必ず導線を張る（全製品そろえる）
    "kappstore": "https://kappstore.exbridge.jp/app.php?id=40efd031ba24c9d8&ref=kmorido",
    "kriskarea": "https://kurage.exbridge.jp/kriskarea.php/",
    "kflood": "https://kurage.exbridge.jp/kflood.php/",
    "khazard": "https://kurage.exbridge.jp/khazard.php/",
    "kfault": "https://kurage.exbridge.jp/kfault.php/",
    "mlit": "https://www.mlit.go.jp/toshi/web/morido.html",
    "portal": "https://disaportal.gsi.go.jp/maps/",
}

PREF_OF = {
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


@app.on_event("startup")
def _startup() -> None:
    INDEX.load()


def geocode(q: str):
    """住所→座標。国土地理院の住所検索API。見つからなければ None。"""
    r = requests.get(GSI, params={"q": q}, headers=UA, timeout=10)
    r.raise_for_status()
    items = r.json()
    if not items:
        return None
    top = items[0]
    lon, lat = top["geometry"]["coordinates"]
    return float(lat), float(lon), top["properties"].get("title") or q


def admin_code_of(title: str) -> str:
    """住所文字列 → 行政コード。

    判定に要るのは実は都道府県だけ（covers_pref）なので、まず県名から2桁を取る。
    A56 は県単位で指定されるため、市区町村まで突き合わせる必要がない。
    """
    m = re.match(r"(.+?[都道府県])", title or "")
    if not m:
        return ""
    pref = m.group(1)
    for code, name in PREF_OF.items():
        if name == pref:
            return code + "000"
    return ""


FAQ = [
    ("盛土規制法の規制区域とは何ですか",
     "宅地造成及び特定盛土等規制法（盛土規制法）にもとづき、都道府県知事・指定都市・中核市の長が指定する区域です。"
     "「宅地造成等工事規制区域」と「特定盛土等規制区域」の2種類があり、区域内で一定規模以上の盛土・切土を行うには、"
     "工事の前に許可（または届出）が要ります。2021年の熱海市の土石流を受けて2023年5月に施行されました。"),
    ("大規模盛土造成地マップと何が違うのですか",
     "大規模盛土造成地マップは「そこに盛土がある」という土地の成り立ちを示す図で、工事の可否は決めません。"
     "盛土規制法の規制区域は法律にもとづく規制そのもので、区域内では盛土等の工事に許可・届出が義務づけられます。"
     "土地を買う・造成する前に効くのは規制区域のほうです。"),
    ("不動産取引で説明されますか",
     "宅地造成等工事規制区域は宅地建物取引業法35条の重要事項説明の対象です（法令に基づく制限として説明されます）。"
     "ただし説明されるのは契約の直前です。土地を探している段階で自分で確かめられるように作りました。"),
    ("区域外と表示されれば安全ですか",
     "いいえ。盛土規制法の区域は「工事に許可・届出が要る範囲」を定めるもので、崖崩れや土砂災害の危険そのものを"
     "示すものではありません。土砂災害の想定は土砂災害警戒区域、浸水は洪水・内水のハザードマップで確認してください。"),
    ("このサイトの判定は公的な証明になりますか",
     "なりません。国土数値情報は「概略的な位置を示すものであり法的図書ではない」と明記されており、申請資料には使えません。"
     "住所から求めた代表点による参考情報です。正確な区域は、告示番号をもとに指定した自治体の担当課で確認してください。"),
]


def jsonld_for(path: str) -> str:
    """構造化データ。AI検索・検索エンジンに「何を答えるサイトか」を機械可読で渡す。"""
    graph = [{
        "@type": "WebSite",
        "@id": PUBLIC_BASE + "/#website",
        "name": SITE,
        "url": PUBLIC_BASE + "/",
        "inLanguage": "ja",
        "publisher": {"@type": "Organization", "name": "株式会社エクスブリッジ", "url": "https://exbridge.jp/"},
        "potentialAction": {
            "@type": "SearchAction",
            "target": {"@type": "EntryPoint", "urlTemplate": PUBLIC_BASE + "/?q={search_term_string}"},
            "query-input": "required name=search_term_string",
        },
    }]
    if path in ("/", "/about"):
        graph.append({
            "@type": "FAQPage",
            "mainEntity": [
                {"@type": "Question", "name": q,
                 "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in FAQ
            ],
        })
    if path != "/":
        graph.append({
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": SITE, "item": PUBLIC_BASE + "/"},
                {"@type": "ListItem", "position": 2,
                 "name": "地図で見る" if path.startswith("/map") else "このデータについて",
                 "item": PUBLIC_BASE + path},
            ],
        })
    return json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False)


def root_prefix(path: str) -> str:
    """画面内のリンクに付ける相対プレフィックス。

    公開時は heteml の /kmorido.php/ 配下に置かれるので、リンクを "/about" と
    絶対で書くとサイト直下（404）へ飛ぶ。末尾スラッシュの有無で深さが変わる。
    """
    segs = [s for s in path.split("/") if s]
    depth = len(segs) if path.endswith("/") else max(0, len(segs) - 1)
    return "../" * depth


def page(request: Request, name: str, **kw):
    path = request.url.path
    kw.update(site=SITE, links=LINKS, year=date.today().year,
              count=INDEX.count, city_count=INDEX.city_count, pref_count=INDEX.pref_count,
              vintage=INDEX.vintage, attribution=INDEX.attribution, meaning=AREA_MEANING,
              public_base=PUBLIC_BASE, canonical=PUBLIC_BASE + path,
              terms=TERMS, root=root_prefix(path), jsonld=jsonld_for(path), faq=FAQ)
    return templates.TemplateResponse(request, name, kw)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = ""):
    result, error = None, ""
    if q.strip():
        try:
            found = geocode(q.strip())
            if not found:
                error = "住所が見つかりませんでした。市区町村から入れ直してください。"
            else:
                lat, lon, title = found
                result = INDEX.check(lat, lon, title, admin_code_of(title))
        except requests.RequestException:
            error = "住所検索に接続できませんでした。時間をおいて試してください。"
    return page(request, "index.html", q=q, result=result, error=error)


@app.get("/api/check")
def api_check(q: str = "", lat: float = None, lon: float = None):
    if lat is not None and lon is not None:
        r = INDEX.check(lat, lon, "", "")
    elif q.strip():
        found = geocode(q.strip())
        if not found:
            return JSONResponse({"error": "住所が見つかりません"}, status_code=404)
        lat, lon, title = found
        r = INDEX.check(lat, lon, title, admin_code_of(title))
    else:
        return JSONResponse({"error": "q または lat/lon が要ります"}, status_code=400)
    return {
        "address": r.address, "lat": r.lat, "lon": r.lon, "status": r.status,
        "areas": [a.__dict__ for a in r.areas], "nearest_m": r.nearest_m,
        "notes": r.notes, "data_vintage": r.vintage, "attribution": r.attribution,
    }


@app.get("/api/areas.geojson")
def areas_geojson(bbox: str = "", limit: int = 3000):
    """表示中の範囲にある規制区域を GeoJSON で返す。bbox は minlon,minlat,maxlon,maxlat。"""
    try:
        minx, miny, maxx, maxy = [float(v) for v in bbox.split(",")]
    except ValueError:
        return JSONResponse({"error": "bbox は minlon,minlat,maxlon,maxlat の形で渡してください"}, status_code=400)
    if INDEX._tree is None:
        INDEX.load()
    feats = []
    for row in INDEX._rows:
        if row["maxx"] < minx or row["minx"] > maxx or row["maxy"] < miny or row["miny"] > maxy:
            continue
        feats.append({
            "type": "Feature",
            "geometry": json.loads(row["geometry"]),
            "properties": {
                "id": row["id"], "city": row["city"] or "",
                "area_type": row["area_type"] or "", "area_type_code": row["area_type_code"] or "",
                "subject": row["subject"] or "", "notice": row["notice_no"] or "",
                "effective_date": row["effective_date"] or "",
            },
        })
        if len(feats) >= limit:
            break
    return {"type": "FeatureCollection", "features": feats, "truncated": len(feats) >= limit}


@app.get("/map/", response_class=HTMLResponse)
def map_page(request: Request, lat: float = None, lon: float = None, q: str = ""):
    if q.strip() and lat is None:
        try:
            found = geocode(q.strip())
            if found:
                lat, lon = found[0], found[1]
        except requests.RequestException:
            pass
    return page(request, "map.html", lat=lat, lon=lon, q=q[:100])


@app.get("/healthz")
def healthz():
    return {"ok": True, "areas": INDEX.count, "cities": INDEX.city_count,
            "prefs": INDEX.pref_count, "vintage": INDEX.vintage}


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return page(request, "about.html")



# 検索する人の言い方と、法令・行政の用語はずれている。両方の語で拾えるようにする。
# 実例: 名古屋市は「内水ハザードマップ」を「雨水出水浸水想定区域」へ改称し、URLも変えた
# （旧URLは404。2026-09-14 実測）。
TERMS = [
    ('盛土規制', '宅地造成及び特定盛土等規制法（盛土規制法）'),
    ('宅地造成工事規制区域', '宅地造成等工事規制区域（2023年の法改正で「等」が入りました）'),
    ('盛土の届出が要る区域', '特定盛土等規制区域'),
    ('大規模盛土造成地マップ', '別のもの。あちらは「盛土がある場所」、ここは「工事に許可・届出が要る区域」'),
    ('造成・切土・埋め立て', '盛土等（規模によって許可か届出かが変わります）'),
    ('土捨て場・残土処分', '特定盛土等・土石の堆積'),
]

MUNI = {}
MUNI_BY_PREF = {}


def _load_muni():
    """muni_stats（scripts/build_muni_stats.py が作る）を起動時に読む。1,539件。"""
    import sqlite3
    out = {}
    try:
        c = sqlite3.connect(os.path.join(ROOT, "data", "kmorido.sqlite"))
        c.row_factory = sqlite3.Row
        for r in c.execute("SELECT * FROM muni_stats"):
            d = dict(r)
            d["samples"] = json.loads(d["samples"] or "[]")
            out[d["admin_code"]] = d
        c.close()
    except Exception as e:  # noqa: BLE001
        print("muni_stats を読めません（地域ページは出ません）:", e)
    return out


MUNI = _load_muni()
for _d in sorted(MUNI.values(), key=lambda x: -x["areas"]):
    MUNI_BY_PREF.setdefault(_d["pref_code"], []).append(_d)


@app.get("/area/", response_class=HTMLResponse)
@app.get("/area", response_class=HTMLResponse)
def area_index(request: Request):
    return page(request, "area_index.html",
                prefs=sorted(MUNI_BY_PREF.items(), key=lambda x: x[0]),
                muni_count=len(MUNI), total=sum(d["areas"] for d in MUNI.values()))


@app.get("/area/pref/{pref_code}", response_class=HTMLResponse)
def area_pref(request: Request, pref_code: str):
    """都道府県ごとの一覧。市区町村ページをクロールさせる内部リンクの束ね役。"""
    lst = MUNI_BY_PREF.get(pref_code)
    if not lst:
        return JSONResponse({"error": "その都道府県のページはありません"}, status_code=404)
    return page(request, "area_pref.html", pref=lst[0]["pref"], rows=lst,
                total=sum(d["areas"] for d in lst))


@app.get("/area/{code}", response_class=HTMLResponse)
def area(request: Request, code: str):
    m = MUNI.get(code)
    if not m:
        return JSONResponse({"error": "その市区町村のページはありません"}, status_code=404)
    sib = [x for x in MUNI_BY_PREF.get(m["pref_code"], []) if x["admin_code"] != code][:40]
    return page(request, "area.html", m=m, siblings=sib)


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return f"User-agent: *\nAllow: /\n\nSitemap: {PUBLIC_BASE}/sitemap.xml\n"


@app.get("/sitemap.xml")
def sitemap():
    # 1,539市区町村＋47都道府県。枚数を出さないと検索の入口が増えない（2026-09-13 実測の結論）
    paths = (["/", "/map/", "/about", "/area/"]
             + [f"/area/pref/{pc}" for pc in sorted(MUNI_BY_PREF)]
             + [f"/area/{c}" for c in sorted(MUNI)])
    urls = "".join(
        f"<url><loc>{PUBLIC_BASE}{p}</loc><changefreq>monthly</changefreq></url>"
        for p in paths
    )
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(content=xml, media_type="application/xml")


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms():
    """AI検索（ChatGPT/Claude/Perplexity 等）向けの要約。"""
    prefs = sorted({PREF_OF.get(r["pref_code"], "") for r in INDEX._rows if r["pref_code"]})
    missing = [n for c, n in sorted(PREF_OF.items())
               if n not in prefs]
    return f"""# {SITE}

> 住所を入れると、その場所が盛土規制法（宅地造成及び特定盛土等規制法）の
> 「宅地造成等工事規制区域」または「特定盛土等規制区域」に入っているかを返すサイト。
> 入っていれば、区域の種別・指定した自治体（都道府県／指定都市／中核市）・告示番号・施行年月日を表示する。

## 大規模盛土造成地マップとの違い（よく混同される）
- 大規模盛土造成地マップ: その場所に「盛土がある」という土地の成り立ちを示す。工事の可否は決めない。
- 盛土規制法の規制区域: 法律にもとづく規制。区域内では一定規模以上の盛土・切土に**許可または届出**が要る。
  土地を買う・造成する前に効くのはこちら。宅地造成等工事規制区域は重要事項説明の対象（宅地建物取引業法35条）。

## 2種類の区域
- 宅地造成等工事規制区域: 市街地や集落など、崖崩れ・土砂流出の被害が出るおそれが大きい区域。
- 特定盛土等規制区域: その周辺で、山間部などを含めて広く指定される区域。

## 収録
- 区域数: {INDEX.count:,}
- 市区町村数: {INDEX.city_count:,}
- 収録都道府県: {len(prefs)}（{", ".join(prefs)}）
- 未収録（まだ指定・公開されていない）: {", ".join(missing) if missing else "なし"}
- データ時点: {INDEX.vintage}
- {INDEX.attribution}


## 買い切り版
- 商品ページ: https://kappstore.exbridge.jp/app.php?id=40efd031ba24c9d8
- 税込55,000円。ソースコード（MIT）・データ取り込みスクリプト・設置手順書を同梱。自社サーバーで動かせる。

## 使い方
- 住所で調べる: {PUBLIC_BASE}/?q=<住所>
- 地図で見る: {PUBLIC_BASE}/map/
- データの説明: {PUBLIC_BASE}/about
- API: {PUBLIC_BASE}/api/check?q=<住所> （JSON。status は inside / outside / uncovered の3値）

## 注意
判定は住所から求めた代表点による参考情報で、公的な証明ではない。
国土数値情報は「概略的な位置を示すものであり法的図書ではない」と明記されており、申請資料には使えない。
正確な区域は、告示番号をもとに指定した自治体の担当課で確認すること。
未収録の都道府県は「区域外」ではなく「未指定」と表示する（規制が無いという意味ではない）。

## 関連（同じ運営のツール）
- 災害危険区域マップ（建築基準法39条の建築制限）: {LINKS['kriskarea']}
- 洪水・内水ハザードマップ: {LINKS['kflood']}
- 盛土規制法について（国土交通省）: {LINKS['mlit']}

運営: 株式会社エクスブリッジ https://exbridge.jp/
"""
