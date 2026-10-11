/*
 * <shopmate-chat>: the ShopMate assistant for a storefront page. Plain JavaScript, no dependency, Shadow DOM.
 *
 *   <script src="http://127.0.0.1:8766/widget/shopmate-chat.js" defer></script>
 *   <shopmate-chat api-base="http://127.0.0.1:8766"></shopmate-chat>
 *
 * or, with no markup at all (what docs/storefront-integration.patch uses), let the script mount it:
 *
 *   <script src=".../shopmate-chat.js" data-api-base="http://127.0.0.1:8766" defer></script>
 *
 * Attributes: api-base (the chat API; default: the origin the script was loaded from; "" means the page's own origin),
 *             title (the name in the header, default "Trợ lý ShopMate"), open (start with the panel open).
 * Talks to POST {api-base}/api/chat and POST {api-base}/api/chat/confirm (app/chat_api.py).
 *
 * Script API, on window.ShopMateChat (the demo page uses the same thumbnail function as the widget, so a product looks the same everywhere):
 *   ShopMateChat.thumbnail({kind, color, material, name}, {decorative, tile}) -> an <svg> "fabric swatch": a silhouette by kind, the product's own colour,
 *   a weave by material. shapeOf(kind), colorOf(name), patternOf(material) are the three rules it is made of.
 *   On the element: open(), close(), ask(text).
 *
 * Fonts: the widget asks for "Be Vietnam Pro" and falls back to the system font. It loads nothing itself; a page that wants the font loads it (the demo page does).
 * Everything the server sends is put on the page with textContent / created nodes, never as HTML. The session id lives in sessionStorage (try/catch; a
 * variable in memory if the storage is blocked). Enter sends, Shift+Enter is a new line, Esc closes the panel, new messages are announced through aria-live.
 */
(function () {
  'use strict';
  if (typeof window === 'undefined') return;

  var SVG_NS = 'http://www.w3.org/2000/svg';

  /* ---------------------------------------------------------------------------------------------------------------------------------------------------
   * The fabric swatch: one function draws the thumbnail of a product, for the widget cards and for the demo page.
   * ------------------------------------------------------------------------------------------------------------------------------------------------- */

  var INK = '#1A1D24', TINT = '#E8EAF4', PAPER = '#F7F8FA';

  function fold(text) {
    return String(text == null ? '' : text).normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/đ/g, 'd').replace(/Đ/g, 'D').toLowerCase().trim();
  }

  // the real colour names of the catalogue (data/seed/catalog.vi.json), folded; anything else is a neutral grey
  var COLORS = {
    'den': '#20232A', 'trang': '#F2F2EE', 'xanh navy': '#22335E', 'nau': '#7A5238', 'do': '#C23A3A', 'xam': '#8A8F98', 'hong': '#E8A0B4', 'be': '#D9C5A3',
    'xanh reu': '#6B7A45', 'xanh duong': '#3A78C2', 'xanh dam': '#1F4580', 'do do': '#7B1E2B', 'kem': '#EFE4C8', 'xanh nhat': '#A9CBEA', 'vang': '#E6BF3A',
    'bac': '#C3C7CE', 'tim': '#7A5AA6', 'cam': '#E58A3A'
  };
  var NEUTRAL = '#C9CDD6';

  function colorOf(name) { return COLORS[fold(name)] || NEUTRAL; }

  // eight silhouettes; every kind of the catalogue (35 of them) maps to one
  function shapeOf(kind) {
    var k = fold(kind);
    if (/khoac/.test(k)) return 'jacket';
    if (/so mi|polo/.test(k)) return 'shirt';
    if (/^ao\b/.test(k)) return 'tee';
    if (/vay/.test(k)) return 'dress';
    if (/quan/.test(k)) return 'pants';
    if (/giay|sandal|dep/.test(k)) return 'shoe';
    if (/^(tui|balo|vi)\b/.test(k)) return 'bag';
    return 'accessory';
  }

  // the weave: ripstop for dù, twill for denim, coarse horizontal thread for linen, a fine grain for leather, nothing for cotton and the rest
  function patternOf(material) {
    var m = fold(material);
    if (m === 'du') return 'ripstop';
    if (/denim|jean/.test(m)) return 'twill';
    if (/\blinen\b|vai lanh/.test(m)) return 'linen';  // not just "lanh": "thun lạnh" (cooling jersey) is a plain knit
    if (/^da\b/.test(m)) return 'grain';
    return 'plain';
  }

  function rgb(hex) { var n = parseInt(hex.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
  function toHex(c) { return '#' + c.map(function (v) { return (Math.max(0, Math.min(255, Math.round(v))) + 256).toString(16).slice(1); }).join('').toUpperCase(); }
  function mix(a, b, t) { var x = rgb(a), y = rgb(b); return toHex([x[0] + (y[0] - x[0]) * t, x[1] + (y[1] - x[1]) * t, x[2] + (y[2] - x[2]) * t]); }
  function luminance(hex) {
    var c = rgb(hex).map(function (v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }

  // body: filled with the product's colour (and the weave); trim: filled with a neutral; lines: seams and details; dots: buttons; handle: a stroke of its own
  var SHAPES = {
    jacket: {
      body: ['M37 15 L24 21 L8 67 L20 73 L29 47 L29 88 L71 88 L71 47 L80 73 L92 67 L76 21 L63 15 L56 18 L50 33 L44 18 Z'],
      lines: ['M50 33 L50 88', 'M29 79 L71 79', 'M35 63 L35 74', 'M65 63 L65 74', 'M44 18 L50 26 L56 18']
    },
    shirt: {
      body: ['M38 14 L24 20 L10 71 L22 76 L30 46 L30 88 L70 88 L70 46 L78 76 L90 71 L76 20 L62 14 L56 19 L50 32 L44 19 Z'],
      lines: ['M44 19 L38 28 L50 32 L62 28 L56 19', 'M50 32 L50 88', 'M30 80 L70 80'],
      dots: [[50, 42], [50, 54], [50, 66]]
    },
    tee: {
      body: ['M38 15 L24 21 L8 43 L20 51 L29 43 L29 86 L71 86 L71 43 L80 51 L92 43 L76 21 L62 15 Q50 29 38 15 Z'],
      lines: ['M38 15 Q50 29 62 15', 'M29 79 L71 79']
    },
    dress: {
      body: ['M38 12 L44 12 L50 20 L56 12 L62 12 L64 30 L61 46 L80 90 L20 90 L39 46 L36 30 Z'],
      lines: ['M39 46 L61 46', 'M45 48 L36 90', 'M55 48 L64 90', 'M23 82 L77 82']
    },
    pants: {
      body: ['M27 12 L73 12 L79 90 L56 90 L50 40 L44 90 L21 90 Z'],
      lines: ['M27 21 L73 21', 'M50 21 L50 40', 'M32 21 L34 33', 'M68 21 L66 33']
    },
    shoe: {
      body: ['M14 38 L34 38 L40 51 Q58 57 78 61 Q93 64 93 74 L93 78 L14 78 Z'],
      trim: ['M12 78 L95 78 L95 85 Q95 89 90 89 L17 89 Q12 89 12 84 Z'],
      lines: ['M43 52 L49 45', 'M50 55 L56 48', 'M57 58 L63 51', 'M79 62 Q84 70 83 78', 'M14 66 L36 66']
    },
    bag: {
      body: ['M20 38 L80 38 L87 89 L13 89 Z'],
      handle: 'M36 38 Q36 13 50 13 Q64 13 64 38',
      lines: ['M20 54 L80 54'],
      dots: [[50, 54]]
    },
    accessory: {
      body: ['M20 20 L64 20 L82 38 L82 82 L20 82 Z'],
      trim: ['M64 20 L64 38 L82 38 Z'],
      lines: ['M27 30 L56 30', 'M27 74 L75 74', 'M27 30 L27 74', 'M75 46 L75 74']
    }
  };

  function svgEl(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    if (attrs) Object.keys(attrs).forEach(function (k) { if (attrs[k] != null) node.setAttribute(k, String(attrs[k])); });
    for (var i = 2; i < arguments.length; i++) if (arguments[i]) node.appendChild(arguments[i]);
    return node;
  }

  function weave(kind, id, tone) {
    var w = tone ? { stroke: tone, fill: 'none' } : {};
    if (kind === 'ripstop') {  // a fine grid, a stronger thread every third line
      return svgEl('pattern', { id: id, width: 18, height: 18, patternUnits: 'userSpaceOnUse' },
        svgEl('path', { d: 'M0 6H18M0 12H18M6 0V18M12 0V18', stroke: w.stroke, 'stroke-width': 0.35, fill: 'none' }),
        svgEl('path', { d: 'M0 0H18M0 0V18', stroke: w.stroke, 'stroke-width': 0.9, fill: 'none' }));
    }
    if (kind === 'twill') {  // diagonal ribs
      return svgEl('pattern', { id: id, width: 4, height: 4, patternUnits: 'userSpaceOnUse' },
        svgEl('path', { d: 'M-1 5L5 -1M-1 1L1 -1M3 5L5 3', stroke: w.stroke, 'stroke-width': 0.7, fill: 'none' }));
    }
    if (kind === 'linen') {  // coarse horizontal threads, with a slub here and there
      return svgEl('pattern', { id: id, width: 14, height: 7, patternUnits: 'userSpaceOnUse' },
        svgEl('path', { d: 'M0 1.2H9M10.5 1.2H14M0 4.6H4M5.5 4.6H14', stroke: w.stroke, 'stroke-width': 0.8, fill: 'none' }),
        svgEl('path', { d: 'M6 2.6H8.4', stroke: w.stroke, 'stroke-width': 1.5, fill: 'none' }));
    }
    return svgEl('pattern', { id: id, width: 7, height: 7, patternUnits: 'userSpaceOnUse' },  // grain: small pores
      svgEl('circle', { cx: 1.2, cy: 1.5, r: 0.42, fill: w.stroke }), svgEl('circle', { cx: 4.6, cy: 3, r: 0.42, fill: w.stroke }),
      svgEl('circle', { cx: 2.6, cy: 5.6, r: 0.42, fill: w.stroke }), svgEl('circle', { cx: 6, cy: 6.2, r: 0.42, fill: w.stroke }));
  }

  var swatches = 0;

  function thumbnail(product, options) {
    product = product || {}; options = options || {};
    var shape = shapeOf(product.kind), pattern = patternOf(product.material), fill = colorOf(product.color);
    var spec = SHAPES[shape], id = 'sm' + (++swatches);
    var edge = mix(fill, INK, 0.38);
    var alpha = { ripstop: 0.26, twill: 0.15, linen: 0.22, grain: 0.22 }[pattern] || 0.2;
    var tone = luminance(fill) > 0.5 ? 'rgba(26,29,36,' + alpha + ')' : 'rgba(255,255,255,' + alpha + ')';
    var label = options.decorative ? null : String(product.name || [product.kind, product.color].filter(Boolean).join(' ') || 'Sản phẩm');
    var svg = svgEl('svg', { xmlns: SVG_NS, viewBox: '0 0 100 100', role: options.decorative ? null : 'img', 'aria-label': label, 'aria-hidden': options.decorative ? 'true' : null,
      focusable: 'false', 'data-shape': shape, 'data-pattern': pattern, 'data-color': fill });
    if (options.tile !== false) svg.appendChild(svgEl('rect', { width: 100, height: 100, fill: TINT }));
    var bodies = function () { return spec.body.map(function (d) { return svgEl('path', { d: d }); }); };
    var defs = svgEl('defs');
    if (pattern !== 'plain') {
      defs.appendChild(weave(pattern, id + 'w', tone));
      var clip = svgEl('clipPath', { id: id + 'c' });
      bodies().forEach(function (p) { clip.appendChild(p); });
      defs.appendChild(clip);
      svg.appendChild(defs);
    }
    spec.body.forEach(function (d) { svg.appendChild(svgEl('path', { d: d, fill: fill, stroke: edge, 'stroke-width': 1.6, 'stroke-linejoin': 'round' })); });
    if (spec.trim) spec.trim.forEach(function (d) { svg.appendChild(svgEl('path', { d: d, fill: PAPER, stroke: edge, 'stroke-width': 1.4, 'stroke-linejoin': 'round' })); });
    if (pattern !== 'plain') svg.appendChild(svgEl('g', { 'clip-path': 'url(#' + id + 'c)' }, svgEl('rect', { width: 100, height: 100, fill: 'url(#' + id + 'w)' })));
    if (spec.handle) svg.appendChild(svgEl('path', { d: spec.handle, fill: 'none', stroke: edge, 'stroke-width': 3, 'stroke-linecap': 'round' }));
    var detail = luminance(fill) > 0.5 ? mix(fill, INK, 0.42) : mix(fill, '#FFFFFF', 0.42);
    spec.lines.forEach(function (d) { svg.appendChild(svgEl('path', { d: d, fill: 'none', stroke: detail, 'stroke-width': 1.1, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' })); });
    (spec.dots || []).forEach(function (c) { svg.appendChild(svgEl('circle', { cx: c[0], cy: c[1], r: 1.6, fill: detail })); });
    return svg;
  }

  window.ShopMateChat = window.ShopMateChat || {};
  window.ShopMateChat.thumbnail = thumbnail;
  window.ShopMateChat.shapeOf = shapeOf;
  window.ShopMateChat.colorOf = colorOf;
  window.ShopMateChat.patternOf = patternOf;

  if (!window.customElements || window.customElements.get('shopmate-chat')) return;

  /* ---------------------------------------------------------------------------------------------------------------------------------------------------
   * The widget
   * ------------------------------------------------------------------------------------------------------------------------------------------------- */

  var SCRIPT = document.currentScript;
  var DEFAULT_BASE = '';
  try { DEFAULT_BASE = new URL(SCRIPT.src).origin; } catch (e) { /* inline or unknown: the page's own origin */ }
  var SESSION_KEY = 'shopmate-chat-session';
  var MAX_LENGTH = 1000;
  var TIMEOUT_MS = 90000;
  var MAX_ROWS = 4;
  var memorySession = null;

  var GREETING = 'Chào bạn! Mình là trợ lý của ShopMate Fashion. Bạn cần tìm sản phẩm, hỏi về size, chất liệu, đổi trả hay tra đơn hàng?';
  var CHIPS = ['Áo khoác nam dưới 500k', 'Đổi hàng trong bao nhiêu ngày?', 'Tra đơn #84', 'Giao hàng mất mấy ngày?'];
  var T = {
    open: 'Mở trò chuyện với trợ lý ShopMate', minimize: 'Thu nhỏ cửa sổ trò chuyện', name: 'Trợ lý ShopMate', status: 'Thường trả lời trong vài giây',
    dialog: 'Trò chuyện với trợ lý ShopMate', log: 'Cuộc trò chuyện', suggestions: 'Gợi ý câu hỏi', placeholder: 'Nhập tin nhắn…', label: 'Tin nhắn của bạn',
    send: 'Gửi tin nhắn', typing: 'Trợ lý ShopMate đang soạn câu trả lời…', ticketYes: 'Có, tạo yêu cầu', offerYes: 'Có, cho mình xem', no: 'Không cần',
    inStock: 'Còn hàng', outOfStock: 'Hết hàng', view: 'Xem sản phẩm', cards: 'Sản phẩm gợi ý', retry: 'Thử lại', trace: 'Mã tra cứu',
    disclaimer: 'Trợ lý AI có thể nhầm — vui lòng kiểm tra lại trước khi đặt hàng.',
    network: 'Không kết nối được tới trợ lý. Bạn kiểm tra mạng rồi bấm “Thử lại” nhé.',
    timeout: 'Trợ lý trả lời quá lâu. Bạn bấm “Thử lại”, hoặc gửi lại sau ít phút nhé.',
    generic: 'Trợ lý đang gặp trục trặc kỹ thuật. Bạn bấm “Thử lại”; nếu vẫn lỗi, vui lòng thử lại sau ít phút.',
    invalid: 'Tin nhắn chưa hợp lệ: cần từ 1 đến 1000 ký tự. Bạn sửa lại rồi gửi nhé.',
    wait: 'Bạn nhắn hơi nhanh. Vui lòng đợi khoảng {n} giây rồi bấm “Thử lại”.'
  };

  var CSS = [
    ':host{all:initial;position:fixed;right:20px;bottom:20px;z-index:2147483000;--indigo:#1F2A5C;--paper:#F7F8FA;--ink:#1A1D24;--tint:#E8EAF4;--green:#2E7D5B;--red:#B3261E;--muted:#566075;--line:#D5D9EA;--field:#7F89AD;' +
      'font-family:"Be Vietnam Pro",system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;font-size:14.5px;line-height:1.5;color:var(--ink);color-scheme:light}',
    '*{box-sizing:border-box;margin:0}',
    'button,textarea{font:inherit;color:inherit}',
    'button{cursor:pointer}',
    ':focus-visible{outline:2px solid var(--indigo);outline-offset:2px}',
    '.fab{width:56px;height:56px;border-radius:50%;border:0;background:var(--indigo);color:#fff;display:grid;place-items:center}',
    '.fab:hover{background:#2B3A7C}',
    '.fab[hidden]{display:none}',
    '.fab svg,.send svg,.min svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}',
    '.panel{position:absolute;right:0;bottom:0;width:380px;height:min(620px,calc(100vh - 40px));background:var(--paper);border:1px solid var(--line);border-radius:16px;box-shadow:0 12px 40px rgba(26,29,36,.22);display:flex;flex-direction:column;overflow:hidden}',
    '.panel[hidden]{display:none}',
    '.head{display:flex;align-items:center;gap:12px;background:var(--indigo);color:#fff;padding:14px 12px 14px 16px;flex:0 0 auto}',
    '.head :focus-visible{outline-color:#fff}',
    '.avatar{width:40px;height:40px;border-radius:50%;background:var(--tint);color:var(--indigo);display:grid;place-items:center;font-weight:700;font-size:18px;flex:0 0 auto}',
    '.who{flex:1;min-width:0}.who strong{display:block;font-size:15.5px;font-weight:600;line-height:1.3}.who span{display:block;font-size:12.5px;color:rgba(255,255,255,.84)}',
    '.min{width:38px;height:38px;border:0;border-radius:10px;background:transparent;color:#fff;display:grid;place-items:center;flex:0 0 auto}',
    '.min:hover{background:rgba(255,255,255,.14)}',
    '.log{position:relative;flex:1;min-height:0;overflow-y:auto;padding:16px 14px 8px;display:flex;flex-direction:column;gap:12px}',
    '.log>*{flex-shrink:0}',
    '.msg{display:flex;flex-direction:column;gap:8px;max-width:88%}',
    '.msg.user{align-self:flex-end;align-items:flex-end}.msg.bot{align-self:flex-start;align-items:flex-start}',
    '.bubble{padding:10px 13px;border-radius:12px;overflow-wrap:anywhere}',
    '.user .bubble{background:var(--indigo);color:#fff;border-bottom-right-radius:4px}',
    '.bot .bubble{background:#fff;border:1px solid var(--tint);border-bottom-left-radius:4px}',
    '.bot.error .bubble{border-color:var(--red)}',
    '.bubble p+p{margin-top:.45em}.bubble ul{margin:.45em 0 0;padding-left:1.15em}.bubble li{margin:.2em 0}.bubble li::marker{color:var(--indigo)}',
    '.trace{font-size:11.5px;color:var(--muted)}',
    '.chips{display:flex;flex-direction:column;align-items:flex-start;gap:8px}',
    '.chip{background:#fff;border:1px solid var(--indigo);color:var(--indigo);border-radius:999px;padding:7px 13px;font-size:13px;font-weight:500;text-align:left;max-width:100%}',
    '.chip:hover{background:var(--tint)}',
    '.chip:disabled{opacity:.55;cursor:default}',
    '.rail{display:flex;gap:10px;overflow-x:auto;scroll-snap-type:x mandatory;scroll-padding:0 14px;margin:0 -14px;padding:2px 14px 10px;align-self:stretch;scrollbar-width:thin;scrollbar-color:var(--field) transparent}',
    '.card{flex:0 0 164px;scroll-snap-align:start;background:#fff;border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}',
    '.thumb{background:var(--tint);height:108px;display:block}.thumb svg{width:100%;height:100%;display:block}',
    '.info{padding:10px 11px 11px;display:flex;flex-direction:column;gap:6px;flex:1}',
    '.name{font-weight:600;font-size:13.5px;line-height:1.35;min-height:2.7em;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}',
    '.price{font-weight:700;font-size:15px;color:var(--indigo)}',
    '.stock{align-self:flex-start;display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;padding:2px 9px;border-radius:999px}',
    '.stock::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}',
    '.stock.in{color:var(--green);background:rgba(46,125,91,.12)}.stock.out{color:var(--red);background:rgba(179,38,30,.10)}',
    '.view{margin-top:auto;display:block;text-align:center;text-decoration:none;font-weight:600;font-size:13px;padding:7px 10px;border-radius:10px;border:1px solid var(--indigo);color:var(--indigo);background:#fff}',
    '.view:hover{background:var(--indigo);color:#fff}',
    '.actions{display:flex;gap:8px;flex-wrap:wrap}',
    '.btn{border-radius:10px;padding:9px 15px;border:1px solid var(--indigo);background:#fff;color:var(--indigo);font-weight:600;font-size:13.5px}',
    '.btn.primary{background:var(--indigo);color:#fff}',
    '.btn:hover{background:var(--tint)}.btn.primary:hover{background:#2B3A7C}',
    '.btn:disabled{opacity:.55;cursor:default}',
    '.typing{display:inline-flex;align-items:center;gap:5px;padding:14px 15px}',
    '.typing i{width:7px;height:7px;border-radius:50%;background:var(--indigo);opacity:.4}',
    '.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}',
    '.composer{display:flex;gap:8px;align-items:flex-end;padding:10px 12px 6px;background:#fff;border-top:1px solid var(--tint);flex:0 0 auto}',
    '.composer textarea{flex:1;min-width:0;resize:none;border:1px solid var(--field);border-radius:10px;padding:9px 12px;line-height:21px;max-height:' + (MAX_ROWS * 21 + 20) + 'px;background:#fff;overflow-y:auto}',
    '.composer textarea::placeholder{color:var(--muted)}',
    '.composer textarea:focus-visible{outline:2px solid var(--indigo);outline-offset:0;border-color:var(--indigo)}',
    '.send{flex:0 0 40px;width:40px;height:40px;border:0;border-radius:10px;background:var(--indigo);color:#fff;display:grid;place-items:center}',
    '.send:hover:not(:disabled){background:#2B3A7C}',
    '.send:disabled{background:#C5CADF;cursor:not-allowed}',
    '.send svg{width:20px;height:20px}',
    '.foot{font-size:11.5px;line-height:1.4;color:var(--muted);text-align:center;text-wrap:balance;padding:2px 16px 10px;background:#fff;flex:0 0 auto}',
    '@media (max-width:480px){:host{right:16px;bottom:16px}.panel{position:fixed;inset:0;width:100%;height:100%;height:100dvh;border:0;border-radius:0}.composer textarea{font-size:16px}.foot{padding-bottom:max(10px,env(safe-area-inset-bottom))}.card{flex-basis:152px}}',
    '@media (prefers-reduced-motion:no-preference){.typing i{animation:bounce 1.2s infinite ease-in-out}.typing i:nth-child(2){animation-delay:.15s}.typing i:nth-child(3){animation-delay:.3s}' +
      '@keyframes bounce{0%,60%,100%{transform:translateY(0);opacity:.4}30%{transform:translateY(-4px);opacity:1}}}'
  ].join('\n');

  function el(tag, props) {
    var node = document.createElement(tag);
    if (props) Object.keys(props).forEach(function (k) {
      var v = props[k];
      if (v == null || v === false) return;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k.indexOf('on') === 0) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? '' : String(v));
    });
    for (var i = 2; i < arguments.length; i++) if (arguments[i]) node.appendChild(arguments[i]);
    return node;
  }

  function uuid() {
    var c = window.crypto;
    if (c && c.randomUUID) return c.randomUUID();
    var b = new Uint8Array(16);
    if (c && c.getRandomValues) c.getRandomValues(b); else for (var i = 0; i < 16; i++) b[i] = Math.floor(Math.random() * 256);
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    var h = Array.prototype.map.call(b, function (x) { return (x + 256).toString(16).slice(1); }).join('');
    return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16) + '-' + h.slice(16, 20) + '-' + h.slice(20);
  }

  function sessionId() {
    try {
      var id = window.sessionStorage.getItem(SESSION_KEY);
      if (!id) { id = uuid(); window.sessionStorage.setItem(SESSION_KEY, id); }
      return id;
    } catch (e) {
      if (!memorySession) memorySession = uuid();
      return memorySession;
    }
  }

  function formatPrice(vnd) {
    var n = Number(vnd);
    if (!isFinite(n)) return '';
    try { return new Intl.NumberFormat('vi-VN').format(Math.round(n)) + '₫'; } catch (e) { return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, '.') + '₫'; }
  }

  function safeUrl(value) {
    try { var u = new URL(String(value)); return (u.protocol === 'http:' || u.protocol === 'https:') ? u.href : null; } catch (e) { return null; }
  }

  function icon(d) {
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false');
    var path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', d);
    svg.appendChild(path);
    return svg;
  }
  var ICON_CHAT = 'M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-3.6-.8L3 20.5l1.5-4.6A8.4 8.4 0 1 1 21 11.5z';
  var ICON_SEND = 'M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z';
  var ICON_DOWN = 'M6 9l6 6 6-6';

  /* the text of a bubble as nodes: a line that starts with "- " is a list item, any other line is a paragraph; nothing is parsed as HTML */
  function fillText(container, text) {
    var list = null;
    String(text).replace(/\r\n?/g, '\n').split('\n').forEach(function (line) {
      var item = /^\s*-\s+(.*)$/.exec(line);
      if (item) {
        if (!list) { list = el('ul'); container.appendChild(list); }
        list.appendChild(el('li', { text: item[1] }));
      } else if (line.trim()) {
        list = null;
        container.appendChild(el('p', { text: line }));
      }
    });
    if (!container.firstChild) container.appendChild(el('p', { text: String(text) }));
  }

  function productCard(p) {
    var href = p.url ? safeUrl(p.url) : null;
    var thumb = el('div', { class: 'thumb' });
    thumb.appendChild(thumbnail(p, { decorative: true }));
    var info = el('div', { class: 'info' }, el('span', { class: 'name', text: String(p.name || '') }), el('span', { class: 'price', text: formatPrice(p.price_vnd) }),
      el('span', { class: 'stock ' + (p.in_stock ? 'in' : 'out'), text: p.in_stock ? T.inStock : T.outOfStock }));
    if (href) info.appendChild(el('a', { class: 'view', href: href, target: '_blank', rel: 'noopener noreferrer', 'aria-label': T.view + ': ' + String(p.name || ''), text: T.view }));
    return el('article', { class: 'card' }, thumb, info);
  }

  function ApiError(status, data) { this.status = status; this.data = data; }

  class ShopMateChat extends HTMLElement {
    constructor() {
      super();
      this._pending = false; this._open = false; this._greeted = false; this._abort = null; this._actions = null; this._chips = null; this._typing = null; this._last = null;
    }

    static get observedAttributes() { return ['title']; }

    attributeChangedCallback(name) { if (name === 'title' && this._nameEl) this._nameEl.textContent = this.getAttribute('title') || T.name; }

    get apiBase() {
      var base = this.hasAttribute('api-base') ? this.getAttribute('api-base') : DEFAULT_BASE;
      return String(base || '').replace(/\/+$/, '');
    }

    connectedCallback() {
      if (this._built) return;
      this._built = true;
      var root = this.attachShadow({ mode: 'open' });
      if (window.CSSStyleSheet && 'adoptedStyleSheets' in document && CSSStyleSheet.prototype.replaceSync) {
        try { var sheet = new CSSStyleSheet(); sheet.replaceSync(CSS); root.adoptedStyleSheets = [sheet]; } catch (e) { root.appendChild(el('style', { text: CSS })); }
      } else root.appendChild(el('style', { text: CSS }));

      var self = this;
      this._nameEl = el('strong', { text: this.getAttribute('title') || T.name });
      var minimize = el('button', { class: 'min', type: 'button', 'aria-label': T.minimize, onclick: function () { self.close(); } }, icon(ICON_DOWN));
      var head = el('header', { class: 'head' }, el('div', { class: 'avatar', 'aria-hidden': 'true', text: 'S' }), el('div', { class: 'who' }, this._nameEl, el('span', { text: T.status })), minimize);
      this._log = el('div', { class: 'log', role: 'log', 'aria-live': 'polite', 'aria-relevant': 'additions text', 'aria-label': T.log, tabindex: '0' });
      this._input = el('textarea', { rows: '1', maxlength: String(MAX_LENGTH), placeholder: T.placeholder, 'aria-label': T.label, autocomplete: 'off' });
      this._send = el('button', { class: 'send', type: 'submit', 'aria-label': T.send, disabled: true }, icon(ICON_SEND));
      var form = el('form', { class: 'composer', novalidate: true }, this._input, this._send);
      this._panel = el('section', { class: 'panel', role: 'dialog', 'aria-label': T.dialog, hidden: true }, head, this._log, form, el('p', { class: 'foot', text: T.disclaimer }));
      this._fab = el('button', { class: 'fab', type: 'button', 'aria-label': T.open, 'aria-expanded': 'false', 'aria-haspopup': 'dialog', onclick: function () { self.open(); } }, icon(ICON_CHAT));
      root.appendChild(this._panel); root.appendChild(this._fab);

      form.addEventListener('submit', function (e) { e.preventDefault(); self._submit(); });
      this._input.addEventListener('input', function () { self._grow(); self._refresh(); });
      this._input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); self._submit(); }  // 229: an IME is composing (Vietnamese input)
      });
      this._panel.addEventListener('keydown', function (e) { if (e.key === 'Escape') { e.stopPropagation(); self.close(); } });
      if (this.hasAttribute('open')) this.open();
    }

    disconnectedCallback() { if (this._abort) this._abort.abort(); }

    open() { this._toggle(true); }
    close() { this._toggle(false); }
    toggle(force) { this._toggle(typeof force === 'boolean' ? force : !this._open); }

    /* programmatic use (the demo page, a "Hỏi trợ lý" button): open the panel and send this text as the customer */
    ask(text) { this.open(); if (!this._pending && String(text || '').trim()) this._dispatch(String(text).trim(), null); }

    _toggle(open) {
      if (!this._built) return;
      this._open = open;
      this._panel.hidden = !open;
      this._fab.hidden = open;  // the launcher never sits on top of the panel
      this._fab.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open) {
        if (!this._greeted) { this._greeted = true; this._greet(); }
        this._input.focus();
      } else this._fab.focus();
    }

    _greet() {
      var self = this;
      this._add('bot', GREETING);
      this._chips = el('div', { class: 'chips', role: 'group', 'aria-label': T.suggestions });
      CHIPS.forEach(function (text) { self._chips.appendChild(el('button', { class: 'chip', type: 'button', text: text, onclick: function () { self._chip(text); } })); });
      this._log.appendChild(this._chips);
    }

    _chip(text) { if (!this._pending) this._dispatch(text, null); }

    _grow() { this._input.style.height = 'auto'; this._input.style.height = Math.min(this._input.scrollHeight, MAX_ROWS * 21 + 20) + 'px'; }

    _refresh() { this._send.disabled = this._pending || !this._input.value.trim(); this._input.setAttribute('aria-busy', this._pending ? 'true' : 'false'); }

    /* after a new message: show its end (always, when it carries product cards), or its start when it is a long text taller than the log */
    _scroll(first, hasCards) {
      var log = this._log, top = first ? first.offsetTop - 8 : log.scrollHeight;
      log.scrollTop = first && !hasCards && log.scrollHeight - top > log.clientHeight ? top : log.scrollHeight;
    }

    _showTyping(on) {
      if (on && !this._typing) {
        this._typing = el('div', { class: 'msg bot' }, el('div', { class: 'bubble typing' }, el('i'), el('i'), el('i'), el('span', { class: 'sr', text: T.typing })));
        this._log.appendChild(this._typing); this._scroll(null);
      } else if (!on && this._typing) { this._typing.remove(); this._typing = null; }
    }

    _add(who, text, extra) {
      extra = extra || {};
      var self = this;
      var body = el('div', { class: 'bubble' });
      fillText(body, text);
      var msg = el('div', { class: 'msg ' + who + (extra.error ? ' error' : '') }, body);
      if (extra.retry) msg.appendChild(el('div', { class: 'actions' }, el('button', { class: 'btn', type: 'button', text: T.retry, onclick: function () { self._retry(); } })));
      if (extra.trace) msg.appendChild(el('span', { class: 'trace', text: T.trace + ': ' + String(extra.trace).slice(0, 12) }));
      this._log.appendChild(msg);
      var cards = Array.isArray(extra.products) ? extra.products.filter(function (p) { return p && typeof p === 'object'; }).slice(0, 5) : [];
      if (cards.length) {
        var rail = el('div', { class: 'rail', role: 'group', 'aria-label': T.cards });
        cards.forEach(function (p) { rail.appendChild(productCard(p)); });
        this._log.appendChild(rail);
      }
      if (extra.offer) this._buttons(T.offerYes, function () { self._dispatch('có', T.offerYes); }, function () { self._dispatch('không cần', T.no); });
      if (extra.ticket) this._buttons(T.ticketYes, function () { self._confirm(true); }, function () { self._confirm(false); });
      this._scroll(who === 'bot' ? msg : null, cards.length > 0);
      return msg;
    }

    /* two clear buttons under the question: yes (filled) and no (outlined); they go away as soon as one is used or the customer types something else */
    _buttons(yesText, onYes, onNo) {
      var yes = el('button', { class: 'btn primary', type: 'button', text: yesText, onclick: onYes });
      var no = el('button', { class: 'btn', type: 'button', text: T.no, onclick: onNo });
      this._actions = el('div', { class: 'actions' }, yes, no);
      this._log.appendChild(this._actions);
    }

    _clearActions() {
      if (this._actions) { this._actions.remove(); this._actions = null; }
      if (this._chips) { this._chips.remove(); this._chips = null; }
    }

    async _post(path, body) {
      if (!window.fetch) throw new ApiError(0, null);
      this._abort = window.AbortController ? new AbortController() : null;
      var timer = this._abort ? setTimeout(this._abort.abort.bind(this._abort), TIMEOUT_MS) : null;
      try {
        var res = await fetch(this.apiBase + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: this._abort ? this._abort.signal : undefined });
        var data = null;
        try { data = await res.json(); } catch (e) { data = null; }
        if (!res.ok) throw new ApiError(res.status, data);
        return data;
      } finally { if (timer) clearTimeout(timer); this._abort = null; }
    }

    /* what to tell the customer, and whether trying again can help */
    _failure(e) {
      if (e instanceof ApiError) {
        if (e.status === 429) {
          var n = e.data && Number(e.data.retry_after);
          return { text: T.wait.replace('{n}', isFinite(n) && n > 0 ? String(Math.round(n)) : '10'), retry: true };
        }
        if (e.status === 422) return { text: T.invalid, retry: false };
        var m = e.data && typeof e.data.message === 'string' ? e.data.message : null;
        return { text: m || T.generic, retry: e.status >= 500 };
      }
      if (e && e.name === 'AbortError') return { text: T.timeout, retry: true };
      return { text: T.network, retry: true };
    }

    _show(data) {
      var products = Array.isArray(data.products) ? data.products : [];
      this._add('bot', typeof data.reply === 'string' ? data.reply : T.generic,
        { products: products, ticket: data.awaiting_confirmation === true, offer: data.awaiting_offer === true, trace: data.reply_source === 'error_template' ? data.trace_id : null });
    }

    async _run(path, body) {
      this._last = { path: path, body: body };
      this._pending = true; this._refresh(); this._showTyping(true);
      try { var data = await this._post(path, body); this._showTyping(false); this._show(data); }
      catch (e) { this._showTyping(false); var f = this._failure(e); this._add('bot', f.text, { error: true, retry: f.retry }); }
      finally { this._pending = false; this._showTyping(false); this._refresh(); this._input.focus(); }
    }

    _retry() {
      if (this._pending || !this._last) return;
      var last = this._last;
      Array.prototype.forEach.call(this._log.querySelectorAll('.msg.error .actions'), function (a) { a.remove(); });
      this._run(last.path, last.body);
    }

    /* send `message` as the customer; `label` is what the bubble says when the text sent is not what the customer pressed ("có" for "Có, cho mình xem") */
    _dispatch(message, label) {
      if (this._pending) return;
      this._clearActions();
      this._add('user', label || message);
      this._run('/api/chat', { session_id: sessionId(), message: message.slice(0, MAX_LENGTH) });
    }

    _submit() {
      var text = this._input.value.trim();
      if (this._pending || !text) return;
      this._input.value = ''; this._grow();
      this._dispatch(text, null);
    }

    _confirm(yes) {
      if (this._pending) return;
      this._clearActions();
      this._add('user', yes ? T.ticketYes : T.no);
      this._run('/api/chat/confirm', { session_id: sessionId(), confirm: yes });
    }
  }

  window.customElements.define('shopmate-chat', ShopMateChat);

  function mount() {
    if (document.querySelector('shopmate-chat')) return;
    var node = document.createElement('shopmate-chat');
    if (SCRIPT && SCRIPT.hasAttribute('data-api-base')) node.setAttribute('api-base', SCRIPT.getAttribute('data-api-base'));
    document.body.appendChild(node);
  }
  if (SCRIPT && (SCRIPT.hasAttribute('data-api-base') || SCRIPT.hasAttribute('data-auto-mount'))) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
  }
})();
