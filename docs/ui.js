/* ニュースサイト共通のUI。6サイトで同じものを使っている。
 *
 *  1. 下にスクロールすると上のメニュー（見出し＋絞り込み）を隠し、一番上に戻ると出す。
 *  2. 記事カードの右上の☆を押すとお気に入りに入り、「★ お気に入り」タブでまとめて読み返せる。
 *
 * ページ側に足すのは次の3つだけ。
 *   ・記事カードを <article data-id="…"> にする
 *   ・読み込んだ記事一覧を window.NEWS_ITEMS に入れる
 *   ・記事カードのリンク先を返す関数を window.NEWS_LINK に入れる（省略可）
 * 詳細ページ（a.html）は、記事を描いたあとに uiAttachDetail(記事) を呼ぶ。
 *
 * 読み込みは <head> の中で defer を付けずに置く:
 *   <script src="ui.js" data-site="greenday-news"></script>
 * defer にすると、詳細ページの fetch のほうが先に終わったときに
 * uiAttachDetail がまだ無く、お気に入りボタンが付かないことがある。
 * 画面をいじる処理は DOMContentLoaded まで待つので、head に置いても困らない。
 *
 * お気に入りは localStorage に「記事そのもの」を写して持つ。
 * 一覧（articles.json）は30〜45日で入れ替わるので、IDだけ覚えても後から中身が引けない。
 * 保存先のキーは data-site で分ける。6サイトとも mifune39428.github.io の同じドメインに
 * あり、localStorage はドメイン単位で共有されるので、これが無いと互いのお気に入りが混ざる。
 */
(() => {
  "use strict";

  // currentScript は実行中しか読めないので、いちばん先に取っておく。
  const script = document.currentScript;
  const KEY = "newsfav:" +
    ((script && script.dataset.site) || location.pathname.replace(/[^/]*$/, "") || "local");
  const MAX_FAVS = 300;
  // 写して持っておく項目。お気に入りの一覧を描くのに要るものだけ。
  const FIELDS = [
    "id", "url", "title_ja", "title_original", "summary_ja", "one_line",
    "source", "image", "published", "category", "lang", "region", "media",
    "event_when", "has_detail",
  ];

  /* ------------------------------------------------------------------ 見た目 */

  const style = document.createElement("style");
  style.textContent = `
  header { transition: transform .24s ease; will-change: transform; }
  header.ui-hidden { transform: translateY(-100%); }

  #list article, #ui-favs article { position: relative; }
  /* 見出しや時刻が☆の下に潜らないように、カードの右上に場所を空ける。 */
  #list article > .meta, #ui-favs article > .meta { padding-right: 40px; }

  .ui-star {
    position: absolute; top: 7px; right: 7px; z-index: 3;
    width: 34px; height: 34px; padding: 0; border-radius: 50%;
    border: 1px solid var(--line); background: var(--card); color: var(--muted);
    font: inherit; font-size: 17px; line-height: 1; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 1px 5px rgba(0,0,0,.14);
    -webkit-tap-highlight-color: transparent;
    transition: transform .12s ease, color .12s ease, border-color .12s ease;
  }
  .ui-star[aria-pressed="true"] { color: #eda020; border-color: #eda020; }
  .ui-star:active { transform: scale(.9); }
  /* 詳細ページでは丸ボタンではなく、日付などと並ぶ小さな文字のボタンにする。 */
  .ui-star.ui-inline {
    position: static; width: auto; height: auto; border-radius: 999px;
    padding: 2px 11px; font-size: 12px; font-weight: 600; box-shadow: none;
    white-space: nowrap;
  }

  body.ui-favmode #list, body.ui-favmode #more { display: none !important; }
  body:not(.ui-favmode) #ui-favs { display: none; }
  .ui-sum { margin: 0 0 10px; font-size: 14px; line-height: 1.65; color: var(--text); opacity: .85; }
  .ui-note { text-align: center; color: var(--muted); padding: 44px 16px; font-size: 14px; line-height: 2; }
  .ui-gone { border: 1px solid var(--line); border-radius: 4px; padding: 1px 6px; }
  .ui-count { opacity: .6; font-variant-numeric: tabular-nums; }
  `;
  (document.head || document.documentElement).appendChild(style);

  /* ------------------------------------------------------------ お気に入りの箱 */

  function read() {
    try {
      const data = JSON.parse(localStorage.getItem(KEY) || "");
      return Array.isArray(data && data.items) ? data.items.filter(f => f && f.id) : [];
    } catch (err) {
      return [];
    }
  }
  function write() {
    try {
      localStorage.setItem(KEY, JSON.stringify({ v: 1, items: favs.slice(0, MAX_FAVS) }));
    } catch (err) {
      /* 容量いっぱい・プライベートブラウズなど。保存できなくても画面は動かす。 */
    }
  }

  let favs = read();
  let detailItem = null;   // 詳細ページで開いている記事
  let favBox = null;
  let chip = null;
  let row = null;

  const isFav = id => favs.some(f => f.id === id);

  function liveById(id) {
    const list = window.NEWS_ITEMS || [];
    return list.find(i => i.id === id)
      || (detailItem && detailItem.id === id ? detailItem : null)
      || favs.find(f => f.id === id)
      || null;
  }

  function snapshot(item) {
    const copy = {};
    FIELDS.forEach(key => { if (item[key] !== undefined) copy[key] = item[key]; });
    copy.saved_at = new Date().toISOString();
    return copy;
  }

  function toggle(id) {
    if (isFav(id)) {
      favs = favs.filter(f => f.id !== id);
    } else {
      const item = liveById(id);
      if (!item) return;
      favs.unshift(snapshot(item));
      favs = favs.slice(0, MAX_FAVS);
    }
    write();
    syncStars();
    syncChip();
    if (document.body.classList.contains("ui-favmode")) renderFavs();
  }

  /* --------------------------------------------------------- ☆ボタンの取り付け */

  function decorate(root) {
    if (!root) return;
    root.querySelectorAll("article[data-id]").forEach(card => {
      if (card.querySelector(".ui-star")) return;
      card.appendChild(makeStar(card.dataset.id, false));
    });
  }

  function makeStar(id, inline) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = inline ? "ui-star ui-inline" : "ui-star";
    button.dataset.id = id;
    paintStar(button, isFav(id));
    return button;
  }

  function paintStar(button, on) {
    button.setAttribute("aria-pressed", String(on));
    const label = on ? "お気に入りから外す" : "お気に入りに入れる";
    button.setAttribute("aria-label", label);
    button.title = label;
    const inline = button.classList.contains("ui-inline");
    button.textContent = (on ? "★" : "☆") + (inline ? " お気に入り" : "");
  }

  function syncStars() {
    document.querySelectorAll(".ui-star").forEach(button => {
      paintStar(button, isFav(button.dataset.id));
    });
  }

  // カードは丸ごと <a> で包まれているので、捕捉フェーズで受けて記事に飛ばさない。
  document.addEventListener("click", event => {
    const button = event.target.closest && event.target.closest(".ui-star");
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    toggle(button.dataset.id);
  }, true);

  /* ------------------------------------------------------- お気に入りタブと一覧 */

  function syncChip() {
    if (!chip) return;
    chip.innerHTML = favs.length
      ? `★ お気に入り <span class="ui-count">${favs.length}</span>`
      : "★ お気に入り";
  }

  // 絞り込みの段は横スクロールするので、末尾に足すと画面の外に出て気づかれない。
  // 先頭の「すべて」のすぐ隣に置く。
  function placeChip() {
    row.insertBefore(chip, row.children[1] || null);
  }

  function setFavMode(on) {
    document.body.classList.toggle("ui-favmode", on);
    if (chip) chip.setAttribute("aria-pressed", String(on));
    if (on) {
      renderFavs();
      window.scrollTo({ top: 0 });
    }
  }

  const escape = value => String(value === undefined || value === null ? "" : value)
    .replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const dateFmt = new Intl.DateTimeFormat("ja-JP", {
    year: "numeric", month: "numeric", day: "numeric", timeZone: "Asia/Tokyo",
  });

  function linkFor(item) {
    // ページ側がリンクの決め方を持っていればそれを使う（媒体ごとの翻訳リンクなど）。
    if (typeof window.NEWS_LINK === "function") {
      try {
        const target = window.NEWS_LINK(item);
        // 一覧から落ちた記事は解説ファイルも消えているので、詳細ページには送らない。
        if (target && target.href && !(item._gone && /^a\.html/.test(target.href))) return target;
      } catch (err) { /* 落ちてもお気に入りは出す */ }
    }
    if (!item._gone && item.has_detail) {
      return { href: `a.html?id=${encodeURIComponent(item.id)}`, blank: false, label: "詳しく読む →" };
    }
    return {
      href: item.lang === "en"
        ? "https://translate.google.com/translate?sl=auto&tl=ja&u=" + encodeURIComponent(item.url)
        : item.url,
      blank: true,
      label: item.lang === "en" ? "日本語で原文を読む →" : "元の記事を読む →",
    };
  }

  function card(item) {
    const summary = item.summary_ja || item.one_line || "";
    const thumb = item.image
      ? `<img class="thumb" src="${escape(item.image)}" alt="" loading="lazy" decoding="async"
             referrerpolicy="no-referrer" onerror="this.remove()">`
      : "";
    const target = linkFor(item);
    let when = "";
    try { when = dateFmt.format(new Date(item.published)); } catch (err) { when = ""; }
    return `
    <article data-id="${escape(item.id)}">
      <div class="meta">
        ${item.category ? `<span class="tag">${escape(item.category)}</span>` : ""}
        <span>${escape(item.source)}</span>
        ${when ? `<span>・${escape(when)}</span>` : ""}
        ${item._gone ? '<span class="ui-gone">掲載終了</span>' : ""}
      </div>
      <div class="row"><div class="body"><h2>${escape(item.title_ja)}</h2></div>${thumb}</div>
      ${summary ? `<p class="ui-sum">${escape(summary)}</p>` : ""}
      ${item.event_when ? `<p class="ui-sum">📅 ${escape(item.event_when)}</p>` : ""}
      <a class="src" href="${escape(target.href)}"${target.blank ? ' target="_blank" rel="noopener noreferrer"' : ""}>${escape(target.label)}</a>
    </article>`;
  }

  function renderFavs() {
    if (!favBox) return;
    if (!favs.length) {
      favBox.innerHTML = '<p class="ui-note">お気に入りはまだありません。<br>記事カードの右上の ☆ を押すと、ここに集まります。</p>';
      return;
    }
    // 一覧に残っている記事は最新の内容を使い、入れ替わりで消えた記事は写しから出す。
    const live = window.NEWS_ITEMS || [];
    const query = ((document.getElementById("search") || {}).value || "").trim().toLowerCase();
    const rows = favs
      .map(fav => {
        const current = live.find(i => i.id === fav.id);
        return current ? { ...fav, ...current, _gone: false } : { ...fav, _gone: true };
      })
      .filter(item => {
        if (!query) return true;
        const hay = `${item.title_ja} ${item.summary_ja || item.one_line || ""} ${item.title_original || ""} ${item.source}`;
        return hay.toLowerCase().includes(query);
      });
    favBox.innerHTML = rows.length
      ? rows.map(card).join("")
      : '<p class="ui-note">該当するお気に入りがありません</p>';
    decorate(favBox);
  }

  /* ------------------------------------------------------------------ 詳細ページ */

  function attachDetailStar() {
    if (!detailItem) return;
    const meta = document.querySelector("main .meta");
    if (!meta || meta.querySelector(".ui-star")) return;
    meta.appendChild(makeStar(detailItem.id, true));
  }

  // a.html から、記事を描いたあとに呼ぶ。日付などが並ぶ行に「☆ お気に入り」を足す。
  // 画面の用意より先に呼ばれても、あとで init() が付け直すので順番を気にしなくてよい。
  window.uiAttachDetail = function (item) {
    if (!item || !item.id) return;
    detailItem = item;
    attachDetailStar();
  };

  /* -------------------------------------------------------------- 画面の組み立て */

  function init() {
    attachDetailStar();

    /* --- 下にスクロールしたら上のメニューを隠す --- */
    const header = document.querySelector("header");
    if (header) {
      let hidden = false;
      // 高さは先に測っておく。スクロールのたびに offsetHeight を読むと、
      // そのつどレイアウトの計算をやり直すことになる。
      let headHeight = 60;
      const measure = () => { headHeight = header.offsetHeight || 60; };
      measure();
      addEventListener("resize", measure, { passive: true });
      // 絞り込みのチップが増えると段が増えて高さが変わる。
      new MutationObserver(measure).observe(header, { childList: true, subtree: true });

      addEventListener("scroll", () => {
        const y = window.scrollY || document.documentElement.scrollTop || 0;
        // 隠すのは見出しの高さを過ぎてから、戻すのは一番上に着いたとき。
        // しきい値を2つに分けておかないと、境目で出たり消えたりする。
        if (!hidden && y > headHeight) {
          hidden = true;
          header.classList.add("ui-hidden");
        } else if (hidden && y <= 8) {
          hidden = false;
          header.classList.remove("ui-hidden");
        }
      }, { passive: true });
    }

    /* --- ☆とお気に入りタブ（一覧ページだけ） --- */
    const list = document.getElementById("list");
    if (!list) return;

    decorate(list);
    // 「もっと見る」や絞り込みで差し替わったカードにも☆を付ける。
    new MutationObserver(() => decorate(list)).observe(list, { childList: true, subtree: true });

    favBox = document.createElement("div");
    favBox.id = "ui-favs";
    if (list.parentNode) list.parentNode.insertBefore(favBox, list.nextSibling);

    row = document.querySelector(".filters");
    if (!row) return;

    chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.id = "ui-favChip";
    chip.setAttribute("aria-pressed", "false");
    placeChip();
    syncChip();

    // 絞り込みのチップは記事を読み込むたびに作り直されるので、消えたら付け直す。
    new MutationObserver(() => {
      if (!row.contains(chip)) placeChip();
    }).observe(row, { childList: true });

    chip.addEventListener("click", event => {
      event.stopPropagation();     // ページ側のチップの処理を動かさない
      setFavMode(!document.body.classList.contains("ui-favmode"));
    }, true);

    // ほかの絞り込みを押したら、お気に入りタブから抜けて普通の一覧に戻す。
    document.addEventListener("click", event => {
      const other = event.target.closest && event.target.closest(".chip");
      if (other && other !== chip) setFavMode(false);
    }, true);

    const search = document.getElementById("search");
    if (search) {
      search.addEventListener("input", () => {
        if (document.body.classList.contains("ui-favmode")) renderFavs();
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
