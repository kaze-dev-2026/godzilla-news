#!/usr/bin/env python3
"""ゴジラニュース収集プログラム（トークンなし版）

RSSを取得し、キーワードで4分類し、重複を除いて news.json に追加保存し、
index.html を生成する。AIは使わない。標準ライブラリのみで動く。

使い方: python3 main.py
"""
import html
import json
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (compatible; GodzillaNewsBot/1.0)"}
TIMEOUT = 25
MAX_ITEMS = 300

CATEGORIES = ["映画", "グッズ", "イベント", "ゲーム"]

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "dclid", "yclid", "mc_cid", "mc_eid", "ref", "from",
}


def log(lines, msg):
    lines.append(msg)
    print(msg)


def normalize_url(url):
    """重複判定用にURLを正規化する（追跡パラメータ除去など）。"""
    try:
        p = urllib.parse.urlparse(url.strip())
    except Exception:
        return url.strip()
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
         if k.lower() not in TRACKING_PARAMS]
    path = p.path.rstrip("/") or "/"
    return urllib.parse.urlunparse((
        p.scheme.lower(), p.netloc.lower(), path,
        "", urllib.parse.urlencode(q), "",
    ))


def normalize_title(title):
    """タイトル一致の重複判定用（空白と記号を除いて小文字化）。"""
    t = re.sub(r"[\s　・…!！?？「」『』【】()（）\[\]~〜\-―:：/／|｜]", "", title or "")
    return t.lower()


def strip_html(text):
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", "", text)
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:220]


def clean_description(title, raw, source_name=""):
    """説明文を整える。

    Googleニュース形式（リンク一覧のHTML）の場合は先頭リンクの文だけを使う。
    タイトルの繰り返しにしかならない説明文は「説明なし」として空にする。
    Googleニュースのタイトルには末尾に「 - 配信元」が付くため、それを除いた
    本体タイトルとも比較する。
    """
    if not raw:
        return ""
    text = raw
    m = re.search(r"<a[^>]*>(.*?)</a>", raw, re.S)
    if m:
        text = m.group(1)
    desc = strip_html(text)
    if not desc:
        return ""
    core_title = title
    if source_name and title.endswith(" - " + source_name):
        core_title = title[: -len(" - " + source_name)]
    for t in (title, core_title):
        if desc == t or (desc.startswith(t) and len(desc) - len(t) < 12):
            return ""
    return desc


def parse_date(text):
    """RSSの日付文字列を aware datetime に変換する。失敗したら None。"""
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def localname(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def child_text(elem, names):
    for child in elem:
        if localname(child.tag) in names:
            if localname(child.tag) == "link":
                href = child.get("href")
                if href:
                    return href
            if child.text and child.text.strip():
                return child.text.strip()
    return ""


def parse_feed(xml_text):
    """RSS2.0 / RDF / Atom を共通形式の辞書リストにする。"""
    root = ET.fromstring(xml_text)
    items = []
    for elem in root.iter():
        if localname(elem.tag) not in ("item", "entry"):
            continue
        title = child_text(elem, ["title"])
        link = child_text(elem, ["link"])
        date_text = child_text(elem, ["pubDate", "date", "published", "updated"])
        desc = child_text(elem, ["description", "summary", "content"])
        source_name = child_text(elem, ["source"])
        if title and link:
            clean_title = strip_html(title)
            items.append({
                "title": clean_title,
                "link": link,
                "date": parse_date(date_text),
                "description": clean_description(clean_title, desc, source_name),
                "feed_source_name": source_name,
            })
    return items


def classify(title, description, keywords, default_category):
    text = f"{title} {description}"
    cats = [c for c in CATEGORIES if any(k in text for k in keywords.get(c, []))]
    if not cats and default_category:
        cats = [default_category]
    if not cats:
        cats = ["その他"]
    if default_category and default_category in cats and cats[0] != default_category:
        cats.remove(default_category)
        cats.insert(0, default_category)
    return cats


def is_personal_review(title, source_name=""):
    """個人の感想・評価レビューかどうかを判定する（友達の意見で追加、2026-11-21）。

    対象は映画.com等の利用者レビューで、タイトルに
    「映画レビュー（感想・評価）」や「○○さんの映画レビュー」が入る物。
    編集部の記事や興行収入などのニュースは対象にしない。
    """
    t = title or ""
    if "映画レビュー" in t and "感想" in t:
        return True
    if "感想・評価" in t:
        return True
    if re.search(r"さん+の映画レビュー", t):
        return True
    if (source_name or "") == "映画.com" and "映画レビュー" in t:
        return True
    return False


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


CAT_CLASS = {
    "映画": "movie",
    "グッズ": "goods",
    "イベント": "event",
    "ゲーム": "game",
    "その他": "other",
}


def write_html(items, now):
    rows = []
    for it in items:
        try:
            dt = datetime.fromisoformat(it["published"])
            date_str = dt.strftime("%Y/%m/%d %H:%M")
        except Exception:
            date_str = it["published"]
        badges = " ".join(
            '<button type="button" class="badge badge-' + CAT_CLASS.get(c, "other")
            + '" data-filter="' + CAT_CLASS.get(c, "other") + '">'
            + html.escape(c) + "</button>"
            for c in it["categories"])
        desc = ('<p class="desc">' + html.escape(it["description"]) + "</p>"
                if it["description"] else "")
        main_cat = CAT_CLASS.get(it["categories"][0], "other") if it["categories"] else "other"
        cats_attr = " ".join(CAT_CLASS.get(c, "other") for c in it["categories"])
        rows.append(
            '<article class="card card-' + main_cat + '" data-cats="' + cats_attr + '">'
            + '<div class="meta"><span class="badges">' + badges + "</span>"
            + "<time>" + date_str + "</time></div>"
            + '<h2><a href="' + html.escape(it["url"], quote=True) + '">'
            + html.escape(it["title"]) + "</a></h2>" + desc
            + '<div class="src">出典：' + html.escape(it["source"]) + "</div>"
            + "</article>")
    legend = (
        '<button type="button" class="badge badge-all is-active" data-filter="all">すべて</button> '
        + " ".join(
            '<button type="button" class="badge badge-' + CAT_CLASS[c]
            + '" data-filter="' + CAT_CLASS[c] + '">' + c + "</button>"
            for c in ["映画", "グッズ", "イベント", "ゲーム", "その他"]))
    page = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ゴジラニュース</title>
<style>
:root {
  --bg: #0a0a0d;
  --surface: #16161d;
  --border: #2b2b36;
  --text: #f4f3ef;
  --sub: #c9c8cf;
  --muted: #8f8e99;
  --accent: #ff5a1f;
  --movie: #ff6a2b;
  --goods: #2dd4bf;
  --event: #a78bfa;
  --game: #5ea8ff;
  --other: #9a99a3;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--text);
  background:
    radial-gradient(900px 420px at 50% -120px, rgba(255, 90, 31, 0.16), rgba(255, 90, 31, 0) 70%),
    linear-gradient(180deg, #101016 0, var(--bg) 340px);
  background-color: var(--bg);
  font-family: "Hiragino Kaku Gothic ProN", "Hiragino Sans", "Noto Sans CJK JP", "Yu Gothic UI", "Yu Gothic", sans-serif;
  line-height: 1.7;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 840px; margin: 0 auto; padding: 0 18px 56px; }
.hero {
  margin: 22px 0 0;
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
  position: relative;
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.45);
  line-height: 0;
}
.hero img {
  display: block;
  width: 100%;
  height: clamp(170px, 32vw, 330px);
  object-fit: cover;
  object-position: center 30%;
}
.hero::after {
  content: "";
  position: absolute;
  left: 0; right: 0; bottom: 0;
  height: 45%;
  background: linear-gradient(180deg, rgba(10, 10, 13, 0), rgba(10, 10, 13, 0.55));
  pointer-events: none;
}
header { padding: 30px 0 30px; }
.overline {
  color: var(--accent);
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.42em;
  margin: 0 0 10px;
}
h1 {
  margin: 0;
  font-size: clamp(2.7rem, 10vw, 4.4rem);
  font-weight: 900;
  line-height: 1.08;
  letter-spacing: 0.04em;
}
h1 .accent { color: var(--accent); }
.tagline { margin: 14px 0 0; color: var(--sub); font-size: 0.95rem; }
.rule {
  height: 4px;
  margin-top: 26px;
  background: linear-gradient(90deg, var(--accent), rgba(255, 90, 31, 0.25) 55%, rgba(255, 90, 31, 0));
}
.headmeta {
  display: flex; flex-wrap: wrap; gap: 8px 18px; align-items: center;
  margin-top: 16px; color: var(--muted); font-size: 0.85rem;
}
.headmeta .count { color: var(--text); font-weight: 700; }
.legend { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 14px; }
.badge {
  display: inline-block;
  padding: 1px 9px;
  border-radius: 999px;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  border: 1px solid;
  white-space: nowrap;
}
.badge-movie { color: var(--movie); border-color: var(--movie); background: rgba(255, 106, 43, 0.12); }
.badge-goods { color: var(--goods); border-color: var(--goods); background: rgba(45, 212, 191, 0.10); }
.badge-event { color: var(--event); border-color: var(--event); background: rgba(167, 139, 250, 0.12); }
.badge-game  { color: var(--game);  border-color: var(--game);  background: rgba(94, 168, 255, 0.12); }
.badge-other { color: var(--other); border-color: var(--other); background: rgba(154, 153, 163, 0.10); }
button.badge { cursor: pointer; font-family: inherit; }
button.badge:hover { filter: brightness(1.35); }
button.badge.is-active { background: var(--text); color: #111116; border-color: var(--text); }
.badge-all { color: var(--text); border-color: var(--text); background: rgba(244, 243, 239, 0.10); }
.filter-status { margin: 12px 0 0; color: var(--muted); font-size: 0.85rem; }
.card.is-hidden { display: none; }
.card {
  background: linear-gradient(180deg, #181820, #121218);
  border: 1px solid var(--border);
  border-left: 5px solid var(--other);
  border-radius: 12px;
  padding: 18px 22px 16px;
  margin: 16px 0;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
}
.card-movie { border-left-color: var(--movie); }
.card-goods { border-left-color: var(--goods); }
.card-event { border-left-color: var(--event); }
.card-game  { border-left-color: var(--game); }
.card-other { border-left-color: var(--other); }
.meta { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center; }
.meta .badges { display: inline-flex; flex-wrap: wrap; gap: 6px; }
.meta time { margin-left: auto; color: var(--muted); font-size: 0.8rem; letter-spacing: 0.04em; }
.card h2 { margin: 10px 0 0; font-size: 1.13rem; font-weight: 700; line-height: 1.55; }
.card h2 a { color: var(--text); text-decoration: none; }
.card h2 a:hover { color: var(--accent); text-decoration: underline; }
.desc { margin: 8px 0 0; color: var(--sub); font-size: 0.92rem; }
.src { margin-top: 10px; color: var(--muted); font-size: 0.78rem; }
footer { margin-top: 40px; color: var(--muted); font-size: 0.78rem; text-align: center; }
@media (max-width: 560px) {
  .wrap { padding: 0 12px 40px; }
  .hero { margin-top: 14px; border-radius: 10px; }
  .hero img { height: 150px; }
  header { padding: 22px 0 22px; }
  .card { padding: 14px 16px 12px; border-radius: 10px; }
  .card h2 { font-size: 1.03rem; }
  .meta time { margin-left: 0; }
}
</style>
</head>
<body>
<div class="wrap">
<div class="hero"><img src="header.webp" alt="ゴジラ風の怪獣のイラスト"></div>
<header>
<p class="overline">GODZILLA NEWS</p>
<h1>ゴジラ<span class="accent">ニュース</span></h1>
<p class="tagline">ゴジラに関する新着情報を、映画・グッズ・イベント・ゲームに分けて自動で集めています。</p>
<div class="rule"></div>
<div class="headmeta"><span>最終更新：UPDATED</span><span class="count">全 COUNT 件</span></div>
<div class="legend">LEGEND</div>
<p class="filter-status" id="filterStatus">タグを押すと、その種類だけに絞り込めます。もう一度「すべて」を押すと戻ります。</p>
</header>
<main>
ROWS
</main>
<footer>このページはプログラムが自動で作成しています。</footer>
</div>
<script>
(function () {
  var buttons = document.querySelectorAll("[data-filter]");
  var cards = document.querySelectorAll(".card[data-cats]");
  var status = document.getElementById("filterStatus");
  var total = cards.length;
  function apply(name) {
    var shown = 0;
    cards.forEach(function (card) {
      var hit = name === "all" || (card.getAttribute("data-cats") || "").split(" ").indexOf(name) !== -1;
      card.classList.toggle("is-hidden", !hit);
      if (hit) shown += 1;
    });
    buttons.forEach(function (b) {
      b.classList.toggle("is-active", b.getAttribute("data-filter") === name);
    });
    if (status) {
      status.textContent = name === "all"
        ? "全 " + total + " 件を表示しています。タグを押すと絞り込めます。"
        : shown + " 件を表示しています。「すべて」を押すと戻ります。";
    }
  }
  buttons.forEach(function (b) {
    b.addEventListener("click", function () { apply(b.getAttribute("data-filter")); });
  });
})();
</script>
</body>
</html>
"""
    page = page.replace("UPDATED", now.strftime("%Y/%m/%d %H:%M"))
    page = page.replace("COUNT", str(len(items)))
    page = page.replace("LEGEND", legend)
    page = page.replace("ROWS", "\n".join(rows))
    (BASE / "index.html").write_text(page, encoding="utf-8")


def main():
    started = datetime.now(JST)
    lines = [f"実行開始: {started.isoformat()}"]
    sources = json.loads((BASE / "sources.json").read_text(encoding="utf-8"))
    keywords = json.loads((BASE / "keywords.json").read_text(encoding="utf-8"))

    news_path = BASE / "news.json"
    existing = []
    if news_path.exists():
        try:
            existing = json.loads(news_path.read_text(encoding="utf-8"))
        except Exception as e:
            log(lines, f"警告: 既存 news.json が読めないため空から開始する: {e}")
            existing = []
    before_purge = len(existing)
    existing = [it for it in existing
                if not is_personal_review(it.get("title", ""), it.get("source", ""))]
    purged = before_purge - len(existing)
    if purged:
        log(lines, f"既存データから個人レビューを除去: {purged}件")
    seen_urls = {normalize_url(it["url"]) for it in existing}
    seen_titles = {normalize_title(it["title"]) for it in existing}

    total_fetched = 0
    total_kept = 0
    new_items = []
    dup_count = 0
    failures = []

    for src in sources:
        name = src["name"]
        try:
            xml_text = fetch(src["url"])
            items = parse_feed(xml_text)
        except Exception as e:
            failures.append((name, f"{type(e).__name__}: {e}"))
            log(lines, f"取得失敗: {name}: {type(e).__name__}: {e}")
            continue
        kept = 0
        for it in items:
            if is_personal_review(it["title"], it.get("feed_source_name") or name):
                continue
            req_kw = src.get("require_keyword")
            if req_kw and req_kw not in it["title"] and req_kw not in it["description"]:
                continue
            kept += 1
            nurl = normalize_url(it["link"])
            ntitle = normalize_title(it["title"])
            if nurl in seen_urls or (ntitle and ntitle in seen_titles):
                dup_count += 1
                continue
            seen_urls.add(nurl)
            if ntitle:
                seen_titles.add(ntitle)
            cats = classify(it["title"], it["description"], keywords,
                            src.get("default_category"))
            published = it["date"] or started
            new_items.append({
                "title": it["title"],
                "url": it["link"],
                "published": published.astimezone(JST).isoformat(),
                "source": it["feed_source_name"] or name,
                "description": it["description"],
                "categories": cats,
                "first_seen": started.date().isoformat(),
            })
        total_fetched += len(items)
        total_kept += kept
        log(lines, f"取得: {name}: フィード{len(items)}件 / 対象{kept}件")

    merged = new_items + existing
    merged.sort(key=lambda x: x["published"], reverse=True)
    merged = merged[:MAX_ITEMS]
    tmp = news_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(news_path)

    cat_counts = {}
    for it in merged:
        for c in it["categories"]:
            cat_counts[c] = cat_counts.get(c, 0) + 1

    write_html(merged, started)

    log(lines, f"合計: 取得{total_fetched}件 / 対象{total_kept}件 / 新規{len(new_items)}件 / 重複{dup_count}件 / 保存総数{len(merged)}件")
    log(lines, f"分類別（保存総数ベース）: {cat_counts}")
    if failures:
        log(lines, f"失敗した取得先: {[f[0] for f in failures]}")
    else:
        log(lines, "失敗した取得先: なし")
    log(lines, f"実行終了: {datetime.now(JST).isoformat()}")

    log_dir = BASE / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"run-{started.strftime('%Y%m%d-%H%M%S')}.log"
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"ログ: {log_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
