/* ニュースサイト共通のUI。6サイトで同じものを使っている。
 *
 *  1. 下にスクロールすると上のメニュー（見出し＋絞り込み）を隠し、一番上に戻ると出す。
 *  2. 記事カードの右上の☆を押すとお気に入りに入り、「★ お気に入り」タブでまとめて読み返せる。
 *  3. お気に入りをほかの端末へ渡す。ふだんはQRコード（または受け渡しリンク）1回で済む。
 *     毎回自動で揃えたい人向けに、GitHubの非公開Gistを使う同期も残してある。
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
 *
 * 端末をまたぐ同期は GitHub の非公開Gist 1個（6サイトぶんを別ファイルで持つ）。
 * トークンは6サイト共通で1回入れれば足りる（どうせ同じドメインなので分けても意味がない）。
 */
(() => {
  "use strict";

  // currentScript は実行中しか読めないので、いちばん先に取っておく。
  const script = document.currentScript;
  const SITE = (script && script.dataset.site) ||
    location.pathname.replace(/[^/]*$/, "") || "local";
  const KEY = "newsfav:" + SITE;
  const TOKEN_KEY = "newsfav:token";    // 6サイト共通
  const GIST_KEY = "newsfav:gistid";    // 6サイト共通
  const USER_KEY = "newsfav:login";
  const GIST_MARK = "newsfav-sync";     // Gistを探すときの目印
  const GIST_FILE = `favorites-${SITE}.json`;
  const TOKEN_URL = "https://github.com/settings/tokens/new?scopes=gist&description=news-favorites";
  const MAX_FAVS = 300;
  // 受け渡しリンクをQRにするときの上限。これより大きい型番は画面で読み取るのが現実的でない
  // （型番20で97マス角。これ以上は1マスが細かくなりすぎてカメラが拾えない）。
  const MAX_QR_VERSION = 20;
  // 消した記録を残しておく期間。これが無いと、別の端末が古い★を持ってきて復活する。
  const TOMB_DAYS = 180;
  // 写して持っておく項目。お気に入りの一覧を描くのに要るものだけ。
  const FIELDS = [
    "id", "url", "title_ja", "title_original", "summary_ja", "one_line",
    "source", "image", "published", "category", "lang", "region", "media",
    "event_when", "has_detail",
  ];

  /* --------------------------------------------------------------- QRコード */

  /* QRコード（バイトモード・誤り訂正レベルL・型番1〜40）を作る。
     外部の部品を持ち込みたくないので自前で持つ。返すのは true/false の二次元配列。 */
  const QR = (() => {
    "use strict";

    // 型番ごとの [1ブロックあたりの誤り訂正語数, ブロック数, データ語数, （2群目）…]
    const EC_L = [[7,1,19],[10,1,34],[15,1,55],[20,1,80],[26,1,108],[18,2,68],[20,2,78],[24,2,97],[30,2,116],[18,2,68,2,69],[20,4,81],[24,2,92,2,93],[26,4,107],[30,3,115,1,116],[22,5,87,1,88],[24,5,98,1,99],[28,1,107,5,108],[30,5,120,1,121],[28,3,113,4,114],[28,3,107,5,108],[28,4,116,4,117],[28,2,111,7,112],[30,4,121,5,122],[30,6,117,4,118],[26,8,106,4,107],[28,10,114,2,115],[30,8,122,4,123],[30,3,117,10,118],[30,7,116,7,117],[30,5,115,10,116],[30,13,115,3,116],[30,17,115],[30,17,115,1,116],[30,13,115,6,116],[30,12,121,7,122],[30,6,121,14,122],[30,17,122,4,123],[30,4,122,18,123],[30,20,117,4,118],[30,19,118,6,119]];

    // 型番2以降の位置合わせパターンの中心座標
    const ALIGN = [[6,18],[6,22],[6,26],[6,30],[6,34],[6,22,38],[6,24,42],[6,26,46],[6,28,50],[6,30,54],[6,32,58],[6,34,62],[6,26,46,66],[6,26,48,70],[6,26,50,74],[6,30,54,78],[6,30,56,82],[6,30,58,86],[6,34,62,90],[6,28,50,72,94],[6,26,50,74,98],[6,30,54,78,102],[6,28,54,80,106],[6,32,58,84,110],[6,30,58,86,114],[6,34,62,90,118],[6,26,50,74,98,122],[6,30,54,78,102,126],[6,26,52,78,104,130],[6,30,56,82,108,134],[6,34,60,86,112,138],[6,30,58,86,114,142],[6,34,62,90,118,146],[6,30,54,78,102,126,150],[6,24,50,76,102,128,154],[6,28,54,80,106,132,158],[6,32,58,84,110,136,162],[6,26,54,82,110,138,166],[6,30,58,86,114,142,170]];

    // 型番7以降に書き込む型番情報（18ビット）
    const VERSION_INFO = [31892,34236,39577,42195,48118,51042,55367,58893,63784,68472,70749,76311,79154,84390,87683,92361,96236,102084,102881,110507,110734,117786,119615,126325,127568,133589,136944,141498,145311,150283,152622,158308,161089,167017];

    // 形式情報（レベルL・マスク0〜7）
    const FORMAT_L = [30660,29427,32170,30877,26159,25368,27713,26998];

    // --- GF(256) ---
    const EXP = new Uint8Array(512), LOG = new Uint8Array(256);
    for (let i = 0, x = 1; i < 255; i++) {
      EXP[i] = x; LOG[x] = i;
      x <<= 1; if (x & 0x100) x ^= 0x11d;
    }
    for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
    const mul = (a, b) => (a === 0 || b === 0) ? 0 : EXP[LOG[a] + LOG[b]];

    // 誤り訂正語を作るための生成多項式
    function generator(degree) {
      let poly = [1];
      for (let i = 0; i < degree; i++) {
        const next = new Array(poly.length + 1).fill(0);
        for (let j = 0; j < poly.length; j++) {
          next[j] ^= poly[j];
          next[j + 1] ^= mul(poly[j], EXP[i]);
        }
        poly = next;
      }
      return poly;
    }

    function ecCodewords(data, count) {
      const gen = generator(count);
      const out = new Array(count).fill(0);
      for (const byte of data) {
        const factor = byte ^ out[0];
        out.shift(); out.push(0);
        for (let i = 0; i < count; i++) out[i] ^= mul(gen[i + 1], factor);
      }
      return out;
    }

    const blocksOf = version => {
      const row = EC_L[version - 1];
      const groups = [];
      for (let i = 1; i < row.length; i += 2) groups.push([row[i], row[i + 1]]);
      return { ecCount: row[0], groups };
    };

    const dataCapacity = version => {
      const { groups } = blocksOf(version);
      return groups.reduce((sum, [n, d]) => sum + n * d, 0);
    };

    const countBits = version => (version < 10 ? 8 : 16);

    function pickVersion(byteLength) {
      for (let v = 1; v <= 40; v++) {
        const bits = 4 + countBits(v) + byteLength * 8;
        if (bits <= dataCapacity(v) * 8) return v;
      }
      return 0;   // 入りきらない
    }

    function makeCodewords(bytes, version) {
      const bits = [];
      const push = (value, length) => {
        for (let i = length - 1; i >= 0; i--) bits.push((value >> i) & 1);
      };
      push(0b0100, 4);                       // バイトモード
      push(bytes.length, countBits(version));
      for (const b of bytes) push(b, 8);

      const capacity = dataCapacity(version) * 8;
      for (let i = 0; i < 4 && bits.length < capacity; i++) bits.push(0);   // 終端
      while (bits.length % 8) bits.push(0);
      const data = [];
      for (let i = 0; i < bits.length; i += 8) {
        data.push(bits.slice(i, i + 8).reduce((v, b) => (v << 1) | b, 0));
      }
      const pad = [0xec, 0x11];
      for (let i = 0; data.length < dataCapacity(version); i++) data.push(pad[i % 2]);

      // ブロックに分けて誤り訂正語を付け、交互に並べ直す
      const { ecCount, groups } = blocksOf(version);
      const dataBlocks = [], ecBlocks = [];
      let at = 0;
      for (const [count, size] of groups) {
        for (let i = 0; i < count; i++) {
          const block = data.slice(at, at + size);
          at += size;
          dataBlocks.push(block);
          ecBlocks.push(ecCodewords(block, ecCount));
        }
      }
      const out = [];
      const longest = Math.max(...dataBlocks.map(b => b.length));
      for (let i = 0; i < longest; i++) {
        for (const block of dataBlocks) if (i < block.length) out.push(block[i]);
      }
      for (let i = 0; i < ecCount; i++) {
        for (const block of ecBlocks) out.push(block[i]);
      }
      return out;
    }

    function buildMatrix(version) {
      const size = version * 4 + 17;
      const modules = Array.from({ length: size }, () => new Array(size).fill(null));
      const fixed = Array.from({ length: size }, () => new Array(size).fill(false));
      const set = (r, c, value) => {
        if (r < 0 || c < 0 || r >= size || c >= size) return;
        modules[r][c] = value; fixed[r][c] = true;
      };

      // 位置検出パターンと分離帯
      for (const [row, col] of [[0, 0], [0, size - 7], [size - 7, 0]]) {
        for (let r = -1; r <= 7; r++) {
          for (let c = -1; c <= 7; c++) {
            const on = (r >= 0 && r <= 6 && (c === 0 || c === 6)) ||
                       (c >= 0 && c <= 6 && (r === 0 || r === 6)) ||
                       (r >= 2 && r <= 4 && c >= 2 && c <= 4);
            set(row + r, col + c, on);
          }
        }
      }
      // タイミングパターン
      for (let i = 8; i < size - 8; i++) {
        set(6, i, i % 2 === 0);
        set(i, 6, i % 2 === 0);
      }
      // 位置合わせパターン
      if (version > 1) {
        const centers = ALIGN[version - 2];
        for (const r of centers) {
          for (const c of centers) {
            if ((r <= 8 && c <= 8) || (r <= 8 && c >= size - 9) || (r >= size - 9 && c <= 8)) continue;
            for (let dr = -2; dr <= 2; dr++) {
              for (let dc = -2; dc <= 2; dc++) {
                set(r + dr, c + dc, Math.max(Math.abs(dr), Math.abs(dc)) !== 1);
              }
            }
          }
        }
      }
      // 常に黒いマス
      set(size - 8, 8, true);
      // 形式情報の場所を先に押さえる（中身はマスクを決めてから書く）
      for (let i = 0; i < 9; i++) {
        if (i === 6) continue;            // ここはタイミングパターン。形式情報ではない
        set(8, i, false); set(i, 8, false);
      }
      for (let i = 0; i < 8; i++) { set(8, size - 1 - i, false); set(size - 1 - i, 8, false); }
      // 型番情報
      if (version >= 7) {
        const info = VERSION_INFO[version - 7];
        for (let i = 0; i < 18; i++) {
          const bit = ((info >> i) & 1) === 1;
          set(Math.floor(i / 3), size - 11 + (i % 3), bit);
          set(size - 11 + (i % 3), Math.floor(i / 3), bit);
        }
      }
      return { size, modules, fixed };
    }

    function placeData(state, codewords) {
      const { size, modules, fixed } = state;
      let bit = 0;
      const total = codewords.length * 8;
      let upward = true;
      for (let right = size - 1; right >= 1; right -= 2) {
        if (right === 6) right = 5;              // タイミング列は飛ばす
        for (let step = 0; step < size; step++) {
          const row = upward ? size - 1 - step : step;
          for (const col of [right, right - 1]) {
            if (fixed[row][col]) continue;
            let value = false;
            if (bit < total) {
              value = ((codewords[bit >> 3] >> (7 - (bit & 7))) & 1) === 1;
              bit++;
            }
            modules[row][col] = value;
          }
        }
        upward = !upward;
      }
    }

    const MASKS = [
      (r, c) => (r + c) % 2 === 0,
      (r, c) => r % 2 === 0,
      (r, c) => c % 3 === 0,
      (r, c) => (r + c) % 3 === 0,
      (r, c) => (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0,
      (r, c) => ((r * c) % 2) + ((r * c) % 3) === 0,
      (r, c) => (((r * c) % 2) + ((r * c) % 3)) % 2 === 0,
      (r, c) => (((r + c) % 2) + ((r * c) % 3)) % 2 === 0,
    ];

    function penalty(modules, size) {
      let score = 0;
      // 規則1: 同じ色が5つ以上続く
      for (let i = 0; i < size; i++) {
        for (const line of [modules[i], modules.map(row => row[i])]) {
          let run = 1;
          for (let j = 1; j < size; j++) {
            if (line[j] === line[j - 1]) {
              run++;
              if (run === 5) score += 3; else if (run > 5) score += 1;
            } else run = 1;
          }
        }
      }
      // 規則2: 2×2の同色
      for (let r = 0; r < size - 1; r++) {
        for (let c = 0; c < size - 1; c++) {
          const v = modules[r][c];
          if (v === modules[r][c + 1] && v === modules[r + 1][c] && v === modules[r + 1][c + 1]) score += 3;
        }
      }
      // 規則3: 位置検出パターンに似た並び
      const a = [true, false, true, true, true, false, true, false, false, false, false];
      const b = [false, false, false, false, true, false, true, true, true, false, true];
      const has = (line, pattern) => {
        let count = 0;
        for (let i = 0; i + 11 <= size; i++) {
          let ok = true;
          for (let j = 0; j < 11; j++) if (line[i + j] !== pattern[j]) { ok = false; break; }
          if (ok) count++;
        }
        return count;
      };
      for (let i = 0; i < size; i++) {
        const row = modules[i], col = modules.map(r => r[i]);
        score += 40 * (has(row, a) + has(row, b) + has(col, a) + has(col, b));
      }
      // 規則4: 白黒の偏り
      let dark = 0;
      for (let r = 0; r < size; r++) for (let c = 0; c < size; c++) if (modules[r][c]) dark++;
      const ratio = (dark * 100) / (size * size);
      score += 10 * Math.floor(Math.abs(ratio - 50) / 5);
      return score;
    }

    function writeFormat(modules, size, mask) {
      const info = FORMAT_L[mask];
      for (let i = 0; i < 15; i++) {
        const bit = ((info >> i) & 1) === 1;
        // 左上（縦の並び→折れて横の並び）
        if (i < 6) modules[i][8] = bit;
        else if (i === 6) modules[7][8] = bit;
        else if (i === 7) modules[8][8] = bit;
        else if (i === 8) modules[8][7] = bit;
        else modules[8][14 - i] = bit;
        // 右上と左下
        if (i < 8) modules[8][size - 1 - i] = bit;
        else modules[size - 15 + i][8] = bit;
      }
      modules[size - 8][8] = true;
    }

    /* 文字列（UTF-8）からQRを作る。入りきらないときは null を返す。 */
    function encode(text) {
      const bytes = Array.from(new TextEncoder().encode(text));
      const version = pickVersion(bytes.length);
      if (!version) return null;

      const codewords = makeCodewords(bytes, version);
      const base = buildMatrix(version);
      placeData(base, codewords);

      let best = null;
      for (let mask = 0; mask < 8; mask++) {
        const modules = base.modules.map(row => row.slice());
        for (let r = 0; r < base.size; r++) {
          for (let c = 0; c < base.size; c++) {
            if (!base.fixed[r][c] && MASKS[mask](r, c)) modules[r][c] = !modules[r][c];
          }
        }
        writeFormat(modules, base.size, mask);
        const score = penalty(modules, base.size);
        if (!best || score < best.score) best = { score, modules, mask };
      }
      return { version, size: base.size, mask: best.mask, modules: best.modules };
    }

    return { encode };
  })();

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

  .ui-syncbar {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    margin: 0 0 12px; padding: 9px 13px;
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    font-size: 12.5px; color: var(--muted);
  }
  .ui-syncbar .ui-syncstate { flex: 1 1 auto; min-width: 0; }
  .ui-syncbar button {
    flex: 0 0 auto; font: inherit; font-size: 12.5px; cursor: pointer;
    background: transparent; color: var(--accent); font-weight: 600;
    border: 1px solid var(--line); border-radius: 999px; padding: 3px 12px;
  }
  .ui-dot { color: #34a853; }
  .ui-dot.off { color: var(--muted); }
  .ui-dot.bad { color: #d93025; }

  .ui-modal {
    position: fixed; inset: 0; z-index: 100;
    background: rgba(0,0,0,.45); display: flex; align-items: flex-end; justify-content: center;
    padding: 16px; overflow-y: auto;
  }
  .ui-modal[hidden] { display: none; }
  @media (min-width: 620px) { .ui-modal { align-items: center; } }
  .ui-sheet {
    background: var(--card); color: var(--text);
    border: 1px solid var(--line); border-radius: 14px;
    width: 100%; max-width: 480px; padding: 20px 20px 16px;
    box-shadow: 0 12px 40px rgba(0,0,0,.3); margin: auto 0;
  }
  .ui-sheet h3 { margin: 0 0 10px; font-size: 17px; }
  .ui-sheet p, .ui-sheet li { font-size: 13px; line-height: 1.85; margin: 0 0 10px; }
  .ui-sheet ol { margin: 0 0 12px; padding-left: 20px; }
  .ui-sheet a { color: var(--accent); font-weight: 600; }
  .ui-sheet input[type="password"], .ui-sheet textarea {
    width: 100%; font: inherit; font-size: 15px; padding: 10px 12px;
    background: var(--bg); color: var(--text);
    border: 1px solid var(--line); border-radius: 9px; margin-bottom: 10px;
  }
  .ui-sheet textarea { font-size: 12px; height: 110px; resize: vertical; font-family: ui-monospace, monospace; }
  .ui-sheet details { margin: 14px 0 6px; }
  .ui-sheet summary { font-size: 13px; cursor: pointer; color: var(--muted); margin-bottom: 8px; }
  .ui-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
  .ui-btn {
    font: inherit; font-size: 14px; cursor: pointer; padding: 9px 15px;
    border-radius: 9px; border: 1px solid var(--line);
    background: var(--bg); color: var(--text); font-weight: 600;
  }
  .ui-btn.primary { background: var(--accent); color: #fff; border-color: transparent; }
  .ui-btn.plain { font-weight: 400; color: var(--muted); }
  .ui-qr { text-align: center; margin: 4px 0 12px; }
  .ui-qr svg { width: min(340px, 78vw); height: auto; background: #fff; padding: 12px; border-radius: 10px; box-shadow: 0 1px 6px rgba(0,0,0,.18); }
  .ui-qr p { font-size: 12px; color: var(--muted); margin: 8px 0 0; }
  .ui-step { font-size: 13px; line-height: 1.9; margin: 0 0 12px; }
  .ui-msg { min-height: 1.2em; font-size: 12.5px; }
  .ui-msg.bad { color: #d93025; }
  .ui-msg.good { color: #2a7a3a; }
  .ui-fine { font-size: 11.5px; color: var(--muted); line-height: 1.8; border-top: 1px solid var(--line); padding-top: 12px; margin: 12px 0 0; }
  `;
  (document.head || document.documentElement).appendChild(style);

  /* ---------------------------------------------------------- 手元の保存と併合 */

  const nowIso = () => new Date().toISOString();

  function emptyStore() {
    return { v: 2, items: [], removed: {} };
  }

  function readStore() {
    let data;
    try {
      data = JSON.parse(localStorage.getItem(KEY) || "");
    } catch (err) {
      return emptyStore();
    }
    if (!data || typeof data !== "object") return emptyStore();
    return {
      v: 2,
      items: Array.isArray(data.items) ? data.items.filter(f => f && f.id) : [],
      removed: (data.removed && typeof data.removed === "object") ? data.removed : {},
    };
  }

  function writeStore() {
    try {
      localStorage.setItem(KEY, JSON.stringify(store));
    } catch (err) {
      /* 容量いっぱい・プライベートブラウズなど。保存できなくても画面は動かす。 */
    }
  }

  // 消した記録は増え続けるので、古いものは落とす。
  function pruneTombs(removed) {
    const limit = new Date(Date.now() - TOMB_DAYS * 86400000).toISOString();
    const kept = {};
    Object.keys(removed || {}).forEach(id => {
      if (removed[id] > limit) kept[id] = removed[id];
    });
    return kept;
  }

  /* 2つの保存内容を1つにまとめる。
     同じ記事があれば「入れた時刻」が新しいほうを採る。
     「消した時刻」のほうが新しければ、その記事は消えたものとして扱う。
     これが無いと、片方の端末で外した★が、もう片方の端末の古い控えで復活してしまう。 */
  function mergeStores(a, b) {
    const removed = pruneTombs({ ...(a.removed || {}) });
    Object.entries(b.removed || {}).forEach(([id, at]) => {
      if (!removed[id] || removed[id] < at) removed[id] = at;
    });

    const byId = new Map();
    [...(a.items || []), ...(b.items || [])].forEach(item => {
      if (!item || !item.id) return;
      const current = byId.get(item.id);
      if (!current || (item.saved_at || "") > (current.saved_at || "")) byId.set(item.id, item);
    });

    const items = [...byId.values()]
      .filter(item => !(removed[item.id] && removed[item.id] > (item.saved_at || "")))
      .sort((x, y) => String(y.saved_at || "").localeCompare(String(x.saved_at || "")))
      .slice(0, MAX_FAVS);

    return { v: 2, items, removed: pruneTombs(removed) };
  }

  const sameStore = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  let store = readStore();
  let detailItem = null;   // 詳細ページで開いている記事
  let noteBox = null;      // 受け渡しリンクで取り込んだときの知らせ（描き直しの影響を受けない場所）
  let favBox = null;
  let chip = null;
  let row = null;

  const isFav = id => store.items.some(f => f.id === id);

  function liveById(id) {
    const list = window.NEWS_ITEMS || [];
    return list.find(i => i.id === id)
      || (detailItem && detailItem.id === id ? detailItem : null)
      || store.items.find(f => f.id === id)
      || null;
  }

  function snapshot(item) {
    const copy = {};
    FIELDS.forEach(key => { if (item[key] !== undefined) copy[key] = item[key]; });
    copy.saved_at = nowIso();
    return copy;
  }

  function toggle(id) {
    const at = nowIso();
    if (isFav(id)) {
      store.items = store.items.filter(f => f.id !== id);
      store.removed[id] = at;              // 消したことを覚えておく
    } else {
      const item = liveById(id);
      if (!item) return;
      delete store.removed[id];
      store.items.unshift(snapshot(item));
      store.items = store.items.slice(0, MAX_FAVS);
    }
    writeStore();
    refreshUi();
    schedulePush();
  }

  function refreshUi() {
    syncStars();
    syncChip();
    if (document.body.classList.contains("ui-favmode")) renderFavs();
  }

  /* --------------------------------------------------- ほかの端末への受け渡し */

  const b64encode = bytes => {
    let text = "";
    for (const byte of bytes) text += String.fromCharCode(byte);
    return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  };
  const b64decode = text => {
    const padded = text.replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(padded + "=".repeat((4 - padded.length % 4) % 4));
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return bytes;
  };

  async function gzip(bytes) {
    if (typeof CompressionStream !== "function") return null;
    const stream = new Blob([bytes]).stream().pipeThrough(new CompressionStream("gzip"));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }
  async function gunzip(bytes) {
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }

  /* 受け渡し用の荷物を作る。
     いま一覧に載っている記事は「IDと入れた時刻」だけ渡し、中身は受け取り側が
     自分の articles.json から引く。これで荷物がうんと小さくなり、QRに収まる。
     一覧から落ちた記事だけは引きようがないので、写しをそのまま入れる。 */
  async function packStore() {
    const live = window.NEWS_ITEMS || [];
    const known = [], carried = [];
    store.items.forEach(item => {
      if (live.some(i => i.id === item.id)) known.push([item.id, item.saved_at || ""]);
      else carried.push(item);
    });
    const payload = { v: 3, k: known, f: carried, r: store.removed || {} };
    const bytes = new TextEncoder().encode(JSON.stringify(payload));
    const packed = await gzip(bytes);
    return packed ? "1" + b64encode(packed) : "0" + b64encode(bytes);
  }

  async function unpackStore(text) {
    const bytes = b64decode(text.slice(1));
    const raw = text[0] === "1" ? await gunzip(bytes) : bytes;
    const payload = JSON.parse(new TextDecoder().decode(raw));
    const live = window.NEWS_ITEMS || [];
    const items = [];
    (payload.k || []).forEach(([id, savedAt]) => {
      const found = live.find(i => i.id === id);
      // 相手の一覧にあってこちらに無い記事は、中身を引けないので諦める
      // （同じサイトの同じ articles.json を見ているので、ふつうは見つかる）。
      if (found) items.push({ ...snapshot(found), saved_at: savedAt || nowIso() });
    });
    (payload.f || []).forEach(item => { if (item && item.id) items.push(item); });
    return { v: 2, items, removed: payload.r || {} };
  }

  async function handoffLink() {
    const base = location.origin + location.pathname.replace(/[^/]*$/, "");
    return base + "#fav=" + await packStore();
  }

  /* URLの # に付いてきたお気に入りを取り込む。
     # の中身はサーバーに送られないので、荷物がどこかに残ることはない。 */
  async function importFromHash() {
    const match = /[#&]fav=([A-Za-z0-9\-_]+)/.exec(location.hash || "");
    if (!match) return null;
    history.replaceState(null, "", location.pathname + location.search);
    try {
      const before = store.items.length;
      store = mergeStores(store, await unpackStore(match[1]));
      writeStore();
      refreshUi();
      schedulePush();
      return { before, after: store.items.length };
    } catch (err) {
      return { error: true };
    }
  }

  let importing = false;

  /* 受け渡しリンクを踏んだときの取り込み。
     IDだけ渡ってくるので、記事一覧（articles.json）が読めるまで待ってから動かす。 */
  function tryImport() {
    if (importing || !/[#&]fav=/.test(location.hash || "")) return;
    importing = true;
    let waited = 0;
    const timer = setInterval(async () => {
      waited += 120;
      if (!(window.NEWS_ITEMS || []).length && waited < 12000) return;
      clearInterval(timer);
      const result = await importFromHash();
      importing = false;
      if (!result) return;
      setFavMode(true);
      showNote(result.error
        ? "⚠️ 受け渡しリンクを読めませんでした。もう一度作り直してください。"
        : `✅ ほかの端末から取り込みました（${result.before}件 → ${result.after}件）`);
    }, 120);
  }

  /* ------------------------------------------------------- GitHub Gist との同期 */

  const getToken = () => { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (err) { return ""; } };
  const getGistId = () => { try { return localStorage.getItem(GIST_KEY) || ""; } catch (err) { return ""; } };
  const getLogin = () => { try { return localStorage.getItem(USER_KEY) || ""; } catch (err) { return ""; } };

  let syncState = { state: getToken() ? "idle" : "off", at: null, error: "" };
  let syncing = false;
  let pushTimer = null;
  let lastAuto = 0;

  function setSyncState(state, error) {
    syncState = { state, at: state === "done" ? new Date() : syncState.at, error: error || "" };
    paintSyncBar();
  }

  async function api(path, options) {
    const token = getToken();
    if (!token) throw new Error("トークンがありません");
    let response;
    try {
      response = await fetch("https://api.github.com" + path, {
        ...options,
        headers: {
          Authorization: "Bearer " + token,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          ...(options && options.body ? { "Content-Type": "application/json" } : {}),
          ...((options && options.headers) || {}),
        },
      });
    } catch (err) {
      // 圏外・機内モードなど。素の "Failed to fetch" のままだと何が起きたか分からない。
      throw new Error("ネットにつながらないため同期できませんでした。");
    }
    if (response.status === 401) throw new Error("トークンが無効です。作り直して入れ直してください。");
    if (response.status === 403) throw new Error("GitHubに断られました（権限か回数制限）。トークンの gist 権限を確認してください。");
    if (response.status === 404) throw new Error("保存先が見つかりませんでした。");
    if (!response.ok) throw new Error(`GitHubから ${response.status} が返りました。`);
    return response.status === 204 ? null : response.json();
  }

  // 6サイトぶんを1つのGistに入れる。新しい端末でもトークンだけで見つけられるように、
  // Gistのidではなく説明文の目印で探す（idを持ち歩かなくて済む）。
  async function findOrCreateGist() {
    const known = getGistId();
    if (known) return known;
    const list = await api("/gists?per_page=100");
    const found = (list || []).find(g => (g.description || "").startsWith(GIST_MARK));
    if (found) {
      localStorage.setItem(GIST_KEY, found.id);
      return found.id;
    }
    const created = await api("/gists", {
      method: "POST",
      body: JSON.stringify({
        description: `${GIST_MARK} — ニュースサイトのお気に入り（自動生成・消さないでください）`,
        public: false,
        files: { [GIST_FILE]: { content: JSON.stringify(emptyStore(), null, 1) } },
      }),
    });
    localStorage.setItem(GIST_KEY, created.id);
    return created.id;
  }

  async function readRemote(gistId) {
    const gist = await api("/gists/" + gistId);
    const file = (gist.files || {})[GIST_FILE];
    if (!file) return emptyStore();
    // 大きいファイルは content が切り詰められるので、その場合は raw を取りに行く。
    let text = file.content;
    if (file.truncated && file.raw_url) {
      text = await (await fetch(file.raw_url)).text();
    }
    try {
      const data = JSON.parse(text);
      return {
        v: 2,
        items: Array.isArray(data.items) ? data.items.filter(f => f && f.id) : [],
        removed: (data.removed && typeof data.removed === "object") ? data.removed : {},
      };
    } catch (err) {
      return emptyStore();
    }
  }

  async function writeRemote(gistId, data) {
    await api("/gists/" + gistId, {
      method: "PATCH",
      body: JSON.stringify({
        files: { [GIST_FILE]: { content: JSON.stringify(data, null, 1) } },
      }),
    });
  }

  /* 取ってきて混ぜて、必要なら書き戻す。
     書く前に必ず読んでから混ぜるので、2台で同時に触っても片方が消えることはない。 */
  async function sync(reason) {
    if (!getToken() || syncing) return;
    syncing = true;
    setSyncState("busy");
    try {
      const gistId = await findOrCreateGist();
      const remote = await readRemote(gistId);
      const merged = mergeStores(store, remote);
      if (!sameStore(merged, store)) {
        store = merged;
        writeStore();
        refreshUi();
      }
      if (!sameStore(merged, remote)) await writeRemote(gistId, merged);
      setSyncState("done");
    } catch (err) {
      setSyncState("bad", err.message || String(err));
      if (reason !== "auto") console.warn("同期に失敗:", err);
    } finally {
      syncing = false;
    }
  }

  function schedulePush() {
    if (!getToken()) return;
    clearTimeout(pushTimer);
    pushTimer = setTimeout(() => sync("push"), 1200);
  }

  function autoSync() {
    if (!getToken()) return;
    if (Date.now() - lastAuto < 20000) return;   // 開き直すたびに叩きに行かない
    lastAuto = Date.now();
    sync("auto");
  }

  // 同じ端末の別のタブで★を触ったら、こちらにも反映する。
  addEventListener("storage", event => {
    if (event.key !== KEY) return;
    store = readStore();
    refreshUi();
  });

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
    const count = store.items.length;
    chip.innerHTML = count
      ? `★ お気に入り <span class="ui-count">${count}</span>`
      : "★ お気に入り";
  }

  // 絞り込みの段は横スクロールするので、末尾に足すと画面の外に出て気づかれない。
  // 先頭の「すべて」のすぐ隣に置く。
  function placeChip() {
    row.insertBefore(chip, row.children[1] || null);
  }

  function showNote(text) {
    if (!noteBox) return;
    noteBox.innerHTML = `<div class="ui-syncbar"><span class="ui-syncstate">${escape(text)}</span></div>`;
    noteBox.hidden = false;
    clearTimeout(showNote.timer);
    showNote.timer = setTimeout(() => { noteBox.hidden = true; noteBox.innerHTML = ""; }, 12000);
  }

  function setFavMode(on) {
    document.body.classList.toggle("ui-favmode", on);
    if (chip) chip.setAttribute("aria-pressed", String(on));
    if (on) {
      renderFavs();
      autoSync();
      window.scrollTo({ top: 0 });
    }
  }

  const escape = value => String(value === undefined || value === null ? "" : value)
    .replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const dateFmt = new Intl.DateTimeFormat("ja-JP", {
    year: "numeric", month: "numeric", day: "numeric", timeZone: "Asia/Tokyo",
  });
  const timeFmt = new Intl.DateTimeFormat("ja-JP", {
    hour: "2-digit", minute: "2-digit", timeZone: "Asia/Tokyo",
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

  function syncLabel() {
    if (!getToken()) {
      return '<span class="ui-dot off">●</span> この端末だけに保存しています';
    }
    if (syncState.state === "busy") return '<span class="ui-dot">●</span> 同期中…';
    if (syncState.state === "bad") {
      return `<span class="ui-dot bad">●</span> 同期できません：${escape(syncState.error)}`;
    }
    const who = getLogin() ? escape(getLogin()) : "GitHub";
    const at = syncState.at ? `・${timeFmt.format(syncState.at)} に同期` : "";
    return `<span class="ui-dot">●</span> ${who} と同期${at}`;
  }

  function paintSyncBar() {
    const bar = favBox && favBox.querySelector(".ui-syncstate");
    if (bar) bar.innerHTML = syncLabel();
  }

  function renderFavs() {
    if (!favBox) return;
    const bar = `
      <div class="ui-syncbar">
        <span class="ui-syncstate">${syncLabel()}</span>
        <button type="button" class="ui-syncbtn">${getToken() ? "同期の設定" : "ほかの端末へ渡す"}</button>
      </div>`;

    if (!store.items.length) {
      favBox.innerHTML = bar +
        '<p class="ui-note">お気に入りはまだありません。<br>記事カードの右上の ☆ を押すと、ここに集まります。</p>';
      return;
    }
    // 一覧に残っている記事は最新の内容を使い、入れ替わりで消えた記事は写しから出す。
    const live = window.NEWS_ITEMS || [];
    const query = ((document.getElementById("search") || {}).value || "").trim().toLowerCase();
    const rows = store.items
      .map(fav => {
        const current = live.find(i => i.id === fav.id);
        return current ? { ...fav, ...current, _gone: false } : { ...fav, _gone: true };
      })
      .filter(item => {
        if (!query) return true;
        const hay = `${item.title_ja} ${item.summary_ja || item.one_line || ""} ${item.title_original || ""} ${item.source}`;
        return hay.toLowerCase().includes(query);
      });
    favBox.innerHTML = bar + (rows.length
      ? rows.map(card).join("")
      : '<p class="ui-note">該当するお気に入りがありません</p>');
    decorate(favBox);
  }

  /* ------------------------------------------------------------- 同期の設定画面 */

  let modal = null;

  function openSettings() {
    if (!modal) modal = buildModal();
    modal.hidden = false;
    const token = modal.querySelector(".ui-token");
    token.value = "";
    token.placeholder = getToken() ? "（この端末には登録済み）" : "ghp_…";
    modal.querySelector(".ui-clear").hidden = !getToken();
    modal.querySelector(".ui-gist").open = !!getToken();
    message("", "");
    drawHandoff();
  }

  function message(text, kind) {
    const box = modal && modal.querySelector(".ui-msg");
    if (box) { box.textContent = text; box.className = "ui-msg " + (kind || ""); }
  }

  function buildModal() {
    const box = document.createElement("div");
    box.className = "ui-modal";
    box.hidden = true;
    box.innerHTML = `
      <div class="ui-sheet" role="dialog" aria-modal="true" aria-label="お気に入りをほかの端末で見る">
        <h3>お気に入りをほかの端末で見る</h3>
        <p class="ui-step">別の端末のカメラでこのQRコードを読むと、そのままお気に入りが引き継がれます。手前の★と<strong>足し合わせる</strong>ので、どちら向きに渡しても消えません。</p>
        <div class="ui-qr"></div>
        <div class="ui-row">
          <button type="button" class="ui-btn primary ui-copylink">リンクをコピー</button>
          <button type="button" class="ui-btn ui-refresh">作り直す</button>
        </div>
        <p class="ui-msg"></p>
        <p class="ui-fine">お気に入りはリンクの <code>#</code> より後ろに入っています。この部分はサーバーへ送られないので、どこかに保存されることはありません。AirDrop・メッセージ・ユニバーサルクリップボードなど、好きな方法で送れます。</p>

        <details class="ui-gist">
          <summary>毎回自動でそろえたいとき（GitHubのトークンを使う）</summary>
          <p>GitHub の非公開Gistに預けて、開くたびに自動で同期します。端末ごとに1回だけトークンを貼れば、6サイトぶんまとめて効きます。<strong>手間をかけたくなければ、上のQRコードだけで足ります。</strong></p>
          <ol>
            <li><a href="${TOKEN_URL}" target="_blank" rel="noopener noreferrer">GitHub でトークンを作る</a>（gist だけにチェックが入った状態で開きます）</li>
            <li>できた <code>ghp_…</code> を下に貼り付けて「保存して同期」</li>
          </ol>
          <input type="password" class="ui-token" placeholder="ghp_…" autocomplete="off" spellcheck="false">
          <div class="ui-row">
            <button type="button" class="ui-btn primary ui-save">保存して同期</button>
            <button type="button" class="ui-btn ui-now">いま同期する</button>
            <button type="button" class="ui-btn plain ui-clear">この端末から消す</button>
          </div>
          <p class="ui-fine">トークンはこの端末のブラウザの中だけに保存され、送り先は GitHub だけです。ただし <code>mifune39428.github.io</code> に置いたページからはどれでも読める場所に入るので、<strong>gist 権限だけ・期限付き</strong>のトークンを使ってください。要らなくなったら GitHub 側で失効させれば、この端末の登録も無効になります。</p>
        </details>

        <div class="ui-row"><button type="button" class="ui-btn ui-close">閉じる</button></div>
      </div>`;
    document.body.appendChild(box);

    box.addEventListener("click", event => {
      if (event.target === box) box.hidden = true;
    });
    box.querySelector(".ui-close").addEventListener("click", () => { box.hidden = true; });
    box.querySelector(".ui-refresh").addEventListener("click", () => drawHandoff());

    box.querySelector(".ui-copylink").addEventListener("click", async () => {
      try {
        const link = await handoffLink();
        await navigator.clipboard.writeText(link);
        message(`リンクをコピーしました（お気に入り ${store.items.length}件）。別の端末で開いてください。`, "good");
      } catch (err) {
        message("コピーできませんでした。QRコードのほうを使ってください。", "bad");
      }
    });

    box.querySelector(".ui-save").addEventListener("click", async () => {
      const value = box.querySelector(".ui-token").value.trim();
      if (!value) { message("トークンを貼り付けてください。", "bad"); return; }
      localStorage.setItem(TOKEN_KEY, value);
      message("確認しています…", "");
      try {
        const me = await api("/user");
        localStorage.setItem(USER_KEY, me.login || "");
        await sync("manual");
        if (syncState.state === "bad") {
          message("同期に失敗しました：" + syncState.error, "bad");
        } else {
          message(`${me.login} として同期しました（お気に入り ${store.items.length}件）。`, "good");
          box.querySelector(".ui-clear").hidden = false;
          box.querySelector(".ui-token").value = "";
          box.querySelector(".ui-token").placeholder = "（この端末には登録済み）";
        }
      } catch (err) {
        localStorage.removeItem(TOKEN_KEY);
        localStorage.removeItem(USER_KEY);
        message(err.message || String(err), "bad");
      }
      renderFavs();
    });

    box.querySelector(".ui-now").addEventListener("click", async () => {
      if (!getToken()) { message("先にトークンを登録してください。", "bad"); return; }
      message("同期しています…", "");
      await sync("manual");
      message(syncState.state === "bad"
        ? "同期に失敗しました：" + syncState.error
        : `同期しました（お気に入り ${store.items.length}件）。`,
        syncState.state === "bad" ? "bad" : "good");
      renderFavs();
    });

    box.querySelector(".ui-clear").addEventListener("click", () => {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
      localStorage.removeItem(GIST_KEY);
      setSyncState("off");
      message("この端末からトークンを消しました。お気に入り自体は残っています。", "good");
      box.querySelector(".ui-clear").hidden = true;
      box.querySelector(".ui-token").placeholder = "ghp_…";
      renderFavs();
    });

    return box;
  }

  // QRコードを描く。件数が多くて読み取れない大きさになるときは、リンクだけ案内する。
  async function drawHandoff() {
    const area = modal.querySelector(".ui-qr");
    if (!store.items.length) {
      area.innerHTML = '<p>お気に入りがまだありません。★を付けてから渡してください。</p>';
      return;
    }
    area.innerHTML = '<p>用意しています…</p>';
    let link;
    try {
      link = await handoffLink();
    } catch (err) {
      area.innerHTML = '<p>受け渡しの用意に失敗しました。</p>';
      return;
    }
    const code = QR.encode(link);
    if (!code || code.version > MAX_QR_VERSION) {
      area.innerHTML = `<p>お気に入りが${store.items.length}件あり、QRコードにすると細かすぎて読み取れません。<br>下の「リンクをコピー」で送ってください。</p>`;
      return;
    }
    const quiet = 2, span = code.size + quiet * 2;
    let path = "";
    for (let r = 0; r < code.size; r++) {
      let c = 0;
      while (c < code.size) {
        if (!code.modules[r][c]) { c++; continue; }
        let run = 1;
        while (c + run < code.size && code.modules[r][c + run]) run++;
        path += `M${c + quiet} ${r + quiet}h${run}v1h-${run}z`;
        c += run;
      }
    }
    area.innerHTML =
      `<svg viewBox="0 0 ${span} ${span}" shape-rendering="crispEdges" role="img" aria-label="お気に入りの受け渡し用QRコード">` +
      `<rect width="${span}" height="${span}" fill="#fff"/><path d="${path}" fill="#000"/></svg>` +
      `<p>お気に入り ${store.items.length}件</p>`;
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

    // 受け渡しリンクで開かれたなら、記事一覧が読めてから取り込む。
    // すでにサイトを開いている状態でリンクを踏むと、ページは読み込み直されず
    // # だけが変わるので、hashchange でも拾う。
    tryImport();
    addEventListener("hashchange", tryImport);

    // 開いたとき・戻ってきたときに、ほかの端末で付けた★を取りに行く。
    autoSync();
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") autoSync();
    });

    /* --- ☆とお気に入りタブ（一覧ページだけ） --- */
    const list = document.getElementById("list");
    if (!list) return;

    decorate(list);
    // 「もっと見る」や絞り込みで差し替わったカードにも☆を付ける。
    new MutationObserver(() => decorate(list)).observe(list, { childList: true, subtree: true });

    noteBox = document.createElement("div");
    noteBox.id = "ui-note";
    noteBox.hidden = true;
    favBox = document.createElement("div");
    favBox.id = "ui-favs";
    if (list.parentNode) {
      list.parentNode.insertBefore(noteBox, list);
      list.parentNode.insertBefore(favBox, list.nextSibling);
    }
    favBox.addEventListener("click", event => {
      if (event.target.closest(".ui-syncbtn")) openSettings();
    });

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

  // 動作確認用（画面には使わない）。
  window.__uiFav = { mergeStores, readStore, get store() { return store; } };
})();
