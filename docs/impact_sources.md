# ⑪ 株価インパクトニュース — ソースの採否（実測）

採否は説明文ではなく、実際にリクエストした結果だけで決めた。
基準: **HTTP 403/404・0件・最新記事が48時間以上前 → 不採用**。

- 測定日時: 2026-11-10 16:20〜16:46 JST（デスクトップPC・自宅回線から。タイムアウト10秒）
- 再測定: `.venv\Scripts\python.exe src\impact.py --probe`（採用中のソースだけを測る）
- 「経過」は測定時刻から最新記事までの時間

## 採用

| 層 | ソース | 取得方法 | HTTP | 件数 | 最新記事(JST) | 経過 |
|---|---|---|---:|---:|---|---:|
| A | TDnet | 既存 ⑥-A（やのしんWEB-API・監視銘柄コード指定）の結果を再利用 | 200 | 50 ※ | 2026-11-10 16:10 | 0.2h |
| A | 日本銀行 | `https://www.boj.or.jp/rss/whatsnew.xml` | 200 | 71 | 2026-11-10 08:50 | 7.9h |
| A | FRB | `https://www.federalreserve.gov/feeds/press_all.xml` | 200 | 20 | 2026-11-10 06:00 | 10.8h |
| A | 財務省 | `https://www.mof.go.jp/news.rss` | 200 | 100 | 2026-11-10 15:15 | 1.5h |
| A | 金融庁 | `https://www.fsa.go.jp/fsaNewsListAll_rss2.xml` | 200 | 15 | 2026-11-10 10:00 | 6.8h |
| B | NHK | `https://www.nhk.or.jp/rss/news/cat5.xml`（経済） | 200 | 137 | 2026-11-10 16:40 | 0.1h |
| B | 日本経済新聞 | 既存 ⑥-C（wor.jp 経由 economy/news/business）を再利用 ＋ `assets.wor.jp/rss/rdf/nikkei/markets.rdf` | 200 | 30 | 2026-11-10 16:03 | 0.7h |
| B | 産経・読売 | 既存 ⑥-C（wor.jp 経由 economy）を再利用 | 200 | 27 / 24 | 11-10 14:53 / 13:07 | 1.5h / 3.2h |
| B | ロイター日本語 | Googleニュース検索 `site:jp.reuters.com 株` | 200 | 100 | 2026-11-10 08:57 | 7.8h |
| B | ブルームバーグ日本語 | Googleニュース検索 `site:bloomberg.com/jp 株` | 200 | 100 | 2026-11-10 12:00 | 4.8h |
| C | フィスコ「強弱材料」 | Googleニュース検索 `強弱材料 フィスコ`（財経新聞・Investing.com日本版などの配信先を含む） | 200 | 45 | 2026-11-10 10:25 | 6.4h |
| C | フィスコ「寄り付き概況」 | Googleニュース検索 `寄り付き概況 フィスコ` | 200 | 32 | 2026-11-10 12:58 | 3.8h |
| C | Investing.com日本版 | `https://jp.investing.com/rss/news.rss` | 200 | 10 | 2026-11-10 16:39 | 0.1h |
| C | 株探 | Googleニュース検索 `site:kabutan.jp 株価` | 200 | 100 | 2026-11-10 16:35 | 0.2h |
| 媒体別 | 市況まとめ（日本） | Googleニュース検索 `日経平均 大引け`。層は記事ごとの媒体名で決める | 200 | 69 | 2026-11-10 16:00 | 0.8h |
| 媒体別 | 市況まとめ（米国） | Googleニュース検索 `米国株 ナスダック`。同上 | 200 | 65 | 2026-11-10 13:53 | 2.9h |

※ TDnet の件数は `list/recent.json?limit=50`（全社）での到達確認。本番は既存 ⑥-A の銘柄指定取得をそのまま使う。

Googleニュース経由の記事は、検索語ではなく**記事側の媒体名**（`<source>`）で層を決める
（`impact.PUBLISHER_LAYERS`）。Yahoo!等のポータル経由の転載は、見出しのクレジット
「（ロイター）」「(フィスコ)」等があれば配信元で数える（`impact.CREDIT_LAYERS`）。
同じ記事の転載を「別媒体で確認」と数えないため。

## 不採用

| 層 | 候補 | HTTP | 件数 | 最新記事 | 理由 |
|---|---|---:|---:|---|---|
| A | EDINET API v2（キー無し） | 200 | 0 | — | 本文が `{"StatusCode": 401, "message": "Access denied due to invalid subscription key..."}`。HTTPは200だが中身は認証エラー。APIキーが必要 |
| A | 日本銀行 `rss/news.xml` | 404 | 0 | — | 存在しない（`whatsnew.xml` を採用） |
| A | FRB `press_monetary.xml` | 200 | 15 | 2026-10-29 03:00 | 最新が301時間前（FOMC間は更新されない）。金融政策の発表は `press_all.xml` に含まれるのでそちらで拾う |
| A | FRB `speeches.xml` | 200 | 15 | 2026-10-15 11:55 | 最新が628時間前 |
| A | 財務省 `rss/index.rss` | 404 | 0 | — | 存在しない（`news.rss` を採用） |
| B | ロイター wor.jp `reuters/top.rdf` | 403 | 0 | — | 拒否 |
| B | ブルームバーグ wor.jp `bloomberg/top.rdf` | 403 | 0 | — | 拒否 |
| B | ブルームバーグ Googleニュース `site:bloomberg.co.jp` | 200 | 4〜6 | 2016年 | 旧ドメイン。最新が約1.5年以上前（現行は bloomberg.com/jp） |
| B | NHK Googleニュース `site:www3.nhk.or.jp` | 200 | 0 | — | 0件（NHKは自前RSSを採用） |
| C | 財経新聞 `rss/sections/stock.xml` | 404 | 0 | — | 存在しない（Googleニュース経由のフィスコ配信で代替） |
| C | 株探 `kabutan.jp/rss/news` | 404 | 0 | — | 存在しない（Googleニュース `site:kabutan.jp` で代替） |
| — | SNS単体・まとめサイト・出典不明ブログ | — | — | — | 仕様で不使用。加えて `config.yaml impact.blocked_publishers` で、Googleニュースに混ざるまとめ・AI要約サイト（BigGo等）・プレスリリース配信（PR TIMES、アットプレス等）・量産の市場調査記事（IndexBox）を除外 |

## 過去日の再現（テスト1）で再現できないソース

日銀/FRB/財務省/金融庁/NHK/日経(wor.jp)/Investing.com の RSS は「現在の最新分」しか返さないため、
過去日（2026-07-17, 2026-11-06）の分は取得できない。再現テストでは Googleニュース検索
（`after:` / `before:`）で取れるソースだけを使い、取れないものは `tests/fixtures/*/index.json` の
`not_reproducible` に列挙してある。TDnet は銘柄別に300件遡って保存した。

## 既知の注意点

- 金融庁RSSの日時は `Tue, 10 Nov 2026 10:00:00 JST` 表記で、feedparser は `JST` を解釈できず
  UTC として扱う（実測で9時間未来にずれた）。`naive_tz: "JST"` で生文字列から読み直している。
- Googleニュース検索の日時は過去日になるほど日付単位（`07:00:00 GMT` 固定）になる。
  再現テストの時刻判定はこの粒度でしか行えない。
