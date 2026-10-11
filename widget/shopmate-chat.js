/*
 * <shopmate-chat>: the ShopMate chat widget for a storefront page. Plain JavaScript, no dependency, Shadow DOM.
 *
 *   <script src="http://127.0.0.1:8766/widget/shopmate-chat.js" defer></script>
 *   <shopmate-chat api-base="http://127.0.0.1:8766"></shopmate-chat>
 *
 * or, with no markup at all (what docs/storefront-integration.patch uses), let the script mount it:
 *
 *   <script src=".../shopmate-chat.js" data-api-base="http://127.0.0.1:8766" defer></script>
 *
 * Attributes: api-base (the chat API; default: the origin the script was loaded from; "" means the page's own origin),
 *             title (the header, default "ShopMate").
 * Talks to POST {api-base}/api/chat and POST {api-base}/api/chat/confirm (app/chat_api.py).
 *
 * Everything the server sends is put on the page with textContent / created nodes, never as HTML. The session id lives in
 * sessionStorage (try/catch; a variable in memory if the storage is blocked). Enter sends, Shift+Enter is a new line, the send
 * button is locked while an answer is awaited, new messages are announced through aria-live="polite".
 */
(function () {
  'use strict';
  if (typeof window === 'undefined' || !window.customElements) return;
  if (window.customElements.get('shopmate-chat')) return;

  var SCRIPT = document.currentScript;
  var DEFAULT_BASE = '';
  try { DEFAULT_BASE = new URL(SCRIPT.src).origin; } catch (e) { /* inline or unknown: the page's own origin */ }
  var SESSION_KEY = 'shopmate-chat-session';
  var MAX_LENGTH = 1000;
  var TIMEOUT_MS = 90000;
  var memorySession = null;

  var GREETING = 'Chào bạn! Mình là trợ lý ShopMate. Bạn muốn tìm sản phẩm, hỏi về chính sách đổi trả, vận chuyển, bảo hành hay kiểm tra đơn hàng?';
  var T = {
    open: 'Mở trò chuyện với ShopMate', close: 'Đóng cửa sổ trò chuyện', title: 'ShopMate', subtitle: 'Trợ lý mua sắm',
    placeholder: 'Nhập tin nhắn…', label: 'Tin nhắn của bạn', send: 'Gửi', waiting: 'ShopMate đang trả lời…',
    yes: 'Có, tạo yêu cầu', no: 'Không', inStock: 'Còn hàng', outOfStock: 'Hết hàng', view: 'Xem sản phẩm',
    network: 'Không kết nối được tới ShopMate. Bạn kiểm tra mạng hoặc thử lại sau nhé.',
    timeout: 'ShopMate trả lời quá lâu. Bạn thử gửi lại nhé.', generic: 'Xin lỗi, mình đang gặp trục trặc kỹ thuật. Bạn thử lại giúp mình nhé.',
    invalid: 'Tin nhắn chưa hợp lệ: cần từ 1 đến 1000 ký tự.', youSaid: 'Bạn', shopSaid: 'ShopMate', trace: 'Mã tra cứu'
  };

  var CSS = [
    ':host{all:initial;position:fixed;right:16px;bottom:16px;z-index:2147483000;font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;font-size:15px;line-height:1.45;color:#14232b;color-scheme:light}',
    '*{box-sizing:border-box}',
    '.fab{width:56px;height:56px;border-radius:50%;border:0;background:#0e7c86;color:#fff;cursor:pointer;display:grid;place-items:center;box-shadow:0 6px 20px rgba(8,40,48,.35)}',
    '.fab:hover{background:#0b6a73}',
    '.fab svg{width:26px;height:26px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}',
    'button,textarea{font:inherit;color:inherit}',
    ':focus-visible{outline:3px solid #ffbf47;outline-offset:2px}',
    '.panel{position:absolute;right:0;bottom:68px;width:min(380px,calc(100vw - 32px));height:min(560px,calc(100vh - 110px));background:#fff;border:1px solid #c9d6dc;border-radius:14px;box-shadow:0 14px 44px rgba(8,40,48,.3);display:flex;flex-direction:column;overflow:hidden}',
    '.panel[hidden]{display:none}',
    '.head{display:flex;align-items:center;justify-content:space-between;gap:8px;background:#0e7c86;color:#fff;padding:10px 12px 10px 16px}',
    '.head strong{display:block;font-size:16px}.head span{display:block;font-size:12.5px;opacity:.92}',
    '.close{background:transparent;border:0;color:#fff;width:36px;height:36px;border-radius:8px;cursor:pointer;font-size:24px;line-height:1}',
    '.close:hover{background:rgba(255,255,255,.18)}',
    '.log{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px;background:#f6f9fa}',
    '.msg{display:flex;flex-direction:column;gap:6px;max-width:92%}',
    '.msg.user{align-self:flex-end;align-items:flex-end}.msg.bot{align-self:flex-start;align-items:flex-start}',
    '.bubble{padding:9px 12px;border-radius:14px;white-space:pre-wrap;overflow-wrap:anywhere}',
    '.user .bubble{background:#0e7c86;color:#fff;border-bottom-right-radius:4px}',
    '.bot .bubble{background:#fff;border:1px solid #d3dee3;border-bottom-left-radius:4px}',
    '.bot.error .bubble{background:#fdecee;border-color:#e8a1a9;color:#6b0f1a}',
    '.trace{font-size:11.5px;color:#5b707a}',
    '.cards{display:flex;flex-direction:column;gap:6px;width:100%}',
    '.card{display:block;background:#fff;border:1px solid #c9d6dc;border-radius:10px;padding:8px 10px;color:inherit;text-decoration:none}',
    'a.card:hover{border-color:#0e7c86;background:#f0fafb}',
    '.card .n{font-weight:600;display:block;overflow-wrap:anywhere}',
    '.card .m{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:2px;font-size:13.5px}',
    '.card .p{font-weight:700;color:#0b5e66}',
    '.card .s{font-size:12.5px;padding:1px 8px;border-radius:999px;border:1px solid currentColor}',
    '.card .s.in{color:#0f6a3a}.card .s.out{color:#8a4b00}',
    '.card .v{font-size:12.5px;color:#0b5e66;text-decoration:underline}',
    '.actions{display:flex;gap:8px;flex-wrap:wrap}',
    '.actions button{border-radius:999px;padding:7px 14px;border:1px solid #0e7c86;background:#fff;color:#0b5e66;cursor:pointer;font-weight:600}',
    '.actions button.yes{background:#0e7c86;color:#fff}',
    '.actions button:disabled{opacity:.5;cursor:default}',
    '.status{min-height:20px;padding:0 14px 4px;font-size:13px;color:#42565f;background:#f6f9fa}',
    '.dots::after{content:"";display:inline-block;width:1.2em;text-align:left}',
    '.composer{display:flex;gap:8px;align-items:flex-end;padding:10px;border-top:1px solid #d3dee3;background:#fff}',
    '.composer textarea{flex:1;resize:none;border:1px solid #9fb3bb;border-radius:10px;padding:8px 10px;max-height:110px;background:#fff}',
    '.composer .send{border:0;border-radius:10px;background:#0e7c86;color:#fff;padding:9px 14px;font-weight:600;cursor:pointer}',
    '.composer .send:disabled{background:#9fb3bb;cursor:not-allowed}',
    '@media (max-width:480px){:host{right:12px;bottom:12px}.panel{position:fixed;right:0;bottom:0;left:0;top:0;width:100%;height:100%;border-radius:0;border:0}.fab[aria-expanded=true]{display:none}}',
    '@media (prefers-reduced-motion:no-preference){.dots::after{animation:dots 1.2s steps(4,end) infinite}@keyframes dots{0%{content:""}25%{content:"."}50%{content:".."}75%{content:"..."}}}'
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

  function icon() {
    var ns = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false');
    var path = document.createElementNS(ns, 'path');
    path.setAttribute('d', 'M21 11.5a8.4 8.4 0 0 1-9 8.4 9 9 0 0 1-3.6-.8L3 20.5l1.5-4.6A8.4 8.4 0 1 1 21 11.5z');
    svg.appendChild(path);
    return svg;
  }

  function ApiError(status, data) { this.status = status; this.data = data; }

  class ShopMateChat extends HTMLElement {
    constructor() {
      super();
      this._pending = false; this._open = false; this._greeted = false; this._abort = null; this._actions = null;
    }

    static get observedAttributes() { return ['title']; }

    attributeChangedCallback(name) { if (name === 'title' && this._titleEl) this._titleEl.textContent = this.getAttribute('title') || T.title; }

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
      this._titleEl = el('strong', { text: this.getAttribute('title') || T.title });
      var close = el('button', { class: 'close', type: 'button', 'aria-label': T.close, text: '×', onclick: function () { self.toggle(false); } });
      var head = el('div', { class: 'head' }, el('div', null, this._titleEl, el('span', { text: T.subtitle })), close);
      this._log = el('div', { class: 'log', role: 'log', 'aria-live': 'polite', 'aria-relevant': 'additions text', 'aria-label': 'Cuộc trò chuyện', tabindex: '0' });
      this._status = el('div', { class: 'status', 'aria-live': 'polite' });
      this._input = el('textarea', { rows: '1', maxlength: String(MAX_LENGTH), placeholder: T.placeholder, 'aria-label': T.label, autocomplete: 'off' });
      this._send = el('button', { class: 'send', type: 'submit', text: T.send, disabled: true });
      var form = el('form', { class: 'composer', novalidate: true }, this._input, this._send);
      this._panel = el('section', { class: 'panel', role: 'dialog', 'aria-label': 'Trò chuyện với ShopMate', hidden: true }, head, this._log, this._status, form);
      this._fab = el('button', { class: 'fab', type: 'button', 'aria-label': T.open, 'aria-expanded': 'false', 'aria-haspopup': 'dialog', onclick: function () { self.toggle(); } }, icon());
      root.appendChild(this._panel); root.appendChild(this._fab);

      form.addEventListener('submit', function (e) { e.preventDefault(); self._submit(); });
      this._input.addEventListener('input', function () { self._grow(); self._refresh(); });
      this._input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); self._submit(); }  // 229: an IME is composing (Vietnamese input)
      });
      this._panel.addEventListener('keydown', function (e) { if (e.key === 'Escape') { e.stopPropagation(); self.toggle(false); } });
    }

    disconnectedCallback() { if (this._abort) this._abort.abort(); }

    toggle(force) {
      var open = typeof force === 'boolean' ? force : !this._open;
      this._open = open;
      this._panel.hidden = !open;
      this._fab.setAttribute('aria-expanded', open ? 'true' : 'false');
      this._fab.setAttribute('aria-label', open ? T.close : T.open);
      if (open) {
        if (!this._greeted) { this._greeted = true; this._add('bot', GREETING); }
        this._input.focus();
      } else this._fab.focus();
    }

    _grow() { this._input.style.height = 'auto'; this._input.style.height = Math.min(this._input.scrollHeight, 110) + 'px'; }

    _refresh() {
      this._send.disabled = this._pending || !this._input.value.trim();
      this._input.setAttribute('aria-busy', this._pending ? 'true' : 'false');
      this._status.replaceChildren();
      if (this._pending) this._status.appendChild(el('span', { class: 'dots', text: T.waiting }));
    }

    _scroll() { this._log.scrollTop = this._log.scrollHeight; }

    _add(who, text, extra) {
      extra = extra || {};
      var msg = el('div', { class: 'msg ' + who + (extra.error ? ' error' : '') }, el('div', { class: 'bubble', text: text }));
      var cards = Array.isArray(extra.products) ? extra.products.filter(function (p) { return p && typeof p === 'object'; }) : [];
      if (cards.length) {
        var list = el('div', { class: 'cards' });
        cards.slice(0, 5).forEach(function (p) {
          var href = p.url ? safeUrl(p.url) : null;
          var inner = [el('span', { class: 'n', text: String(p.name || '') }),
            el('span', { class: 'm' }, el('span', { class: 'p', text: formatPrice(p.price_vnd) }),
              el('span', { class: 's ' + (p.in_stock ? 'in' : 'out'), text: p.in_stock ? T.inStock : T.outOfStock }))];
          if (href) inner.push(el('span', { class: 'v', text: T.view }));
          var card = el(href ? 'a' : 'div', href ? { class: 'card', href: href, target: '_blank', rel: 'noopener noreferrer' } : { class: 'card' });
          inner.forEach(function (n) { card.appendChild(n); });
          list.appendChild(card);
        });
        msg.appendChild(list);
      }
      if (extra.awaiting) {
        var self = this;
        var yes = el('button', { class: 'yes', type: 'button', text: T.yes, onclick: function () { self._confirm(true); } });
        var no = el('button', { class: 'no', type: 'button', text: T.no, onclick: function () { self._confirm(false); } });
        this._actions = el('div', { class: 'actions' }, yes, no);
        msg.appendChild(this._actions);
      }
      if (extra.trace) msg.appendChild(el('span', { class: 'trace', text: T.trace + ': ' + String(extra.trace).slice(0, 12) }));
      this._log.appendChild(msg);
      this._scroll();
      return msg;
    }

    _lockActions() {
      if (this._actions) { Array.prototype.forEach.call(this._actions.querySelectorAll('button'), function (b) { b.disabled = true; }); this._actions = null; }
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

    _failure(e) {
      if (e instanceof ApiError) {
        var m = e.data && typeof e.data.message === 'string' ? e.data.message : null;
        if (m) return m;
        if (e.status === 422) return T.invalid;
        return T.generic;
      }
      if (e && e.name === 'AbortError') return T.timeout;
      return T.network;
    }

    _show(data) {
      var products = Array.isArray(data.products) ? data.products : [];
      this._add('bot', typeof data.reply === 'string' ? data.reply : T.generic,
        { products: products, awaiting: data.awaiting_confirmation === true, trace: data.reply_source === 'error_template' ? data.trace_id : null });
    }

    async _run(path, body) {
      this._pending = true; this._refresh();
      try { this._show(await this._post(path, body)); }
      catch (e) { this._add('bot', this._failure(e), { error: true }); }
      finally { this._pending = false; this._refresh(); this._input.focus(); }
    }

    _submit() {
      var text = this._input.value.trim();
      if (this._pending || !text) return;
      this._lockActions();
      this._add('user', text);
      this._input.value = ''; this._grow();
      this._run('/api/chat', { session_id: sessionId(), message: text.slice(0, MAX_LENGTH) });
    }

    _confirm(yes) {
      if (this._pending) return;
      this._lockActions();
      this._add('user', yes ? T.yes : T.no);
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
