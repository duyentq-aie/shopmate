/*
 * The product grid and the "Hỏi trợ lý" button of the demo page (widget/demo.html). No inline script: the page's CSP forbids it.
 * The products are the static list widget/demo-products.json (built from the real catalogue by scripts/build_demo_products.py); the page only reads it.
 * The thumbnails are drawn by the same function the widget uses for its product cards (window.ShopMateChat.thumbnail, from shopmate-chat.js).
 * Everything from the file goes on the page as text or created nodes.
 */
(function () {
  'use strict';

  var CATEGORIES = { nam: ['thoi-trang-nam'], nu: ['thoi-trang-nu'], giay: ['giay-dep'], 'phu-kien': ['phu-kien', 'tui-vi'] };
  var LABELS = { nam: 'Nam', nu: 'Nữ', giay: 'Giày dép', 'phu-kien': 'Phụ kiện' };
  var products = [];
  var grid = document.getElementById('grid');
  var count = document.getElementById('count');

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function price(vnd) {
    try { return new Intl.NumberFormat('vi-VN').format(Math.round(Number(vnd))) + '₫'; } catch (e) { return String(vnd) + '₫'; }
  }

  function card(p) {
    var thumb = el('div', 'thumb');
    var draw = window.ShopMateChat && window.ShopMateChat.thumbnail;
    if (draw) thumb.appendChild(draw(p, { decorative: true }));
    var body = el('div', 'body');
    body.appendChild(el('h3', null, p.name));
    body.appendChild(el('span', 'meta', 'Chất liệu: ' + p.material + ' · ' + p.color));
    body.appendChild(el('span', 'price', price(p.price_vnd)));
    body.appendChild(el('span', 'stock ' + (p.in_stock ? 'in' : 'out'), p.in_stock ? 'Còn hàng' : 'Hết hàng'));
    var item = el('article', 'item');
    item.appendChild(thumb);
    item.appendChild(body);
    return item;
  }

  function show(category) {
    var wanted = category ? CATEGORIES[category] : null;
    var list = wanted ? products.filter(function (p) { return wanted.indexOf(p.category) >= 0; }) : products;
    grid.replaceChildren.apply(grid, list.length ? list.map(card) : [el('p', 'empty', 'Chưa có sản phẩm nào trong danh mục này.')]);
    count.textContent = (category ? LABELS[category] + ' · ' : '') + list.length + ' sản phẩm';
    Array.prototype.forEach.call(document.querySelectorAll('nav a[data-cat]'), function (a) {
      if (a.getAttribute('data-cat') === category) a.setAttribute('aria-current', 'true'); else a.removeAttribute('aria-current');
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll('nav a[data-cat]'), function (a) {
    a.addEventListener('click', function (e) {
      e.preventDefault();
      var cat = a.getAttribute('data-cat');
      show(a.getAttribute('aria-current') === 'true' ? null : cat);  // a second click on the same category shows everything again
      document.getElementById('san-pham').scrollIntoView({ behavior: 'auto', block: 'start' });
    });
  });
  document.getElementById('home').addEventListener('click', function () { show(null); });
  document.getElementById('ask').addEventListener('click', function () {
    var chat = document.querySelector('shopmate-chat');
    if (chat && chat.open) chat.open();
  });

  fetch('/widget/demo-products.json', { cache: 'no-store' })
    .then(function (res) { if (!res.ok) throw new Error('HTTP ' + res.status); return res.json(); })
    .then(function (data) { products = Array.isArray(data.products) ? data.products : []; show(null); })
    .catch(function () { grid.replaceChildren(el('p', 'empty', 'Không đọc được danh sách sản phẩm của trang demo.')); count.textContent = ''; });
})();
