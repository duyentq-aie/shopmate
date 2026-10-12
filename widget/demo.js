/*
 * The product grid and the "Hỏi trợ lý" button of the demo page (widget/demo.html). No inline script: the page's CSP forbids it.
 * The products are the static list widget/demo-products.json (built from the real catalogue by scripts/build_demo_products.py); the page only reads it.
 * The pictures come with it (the chat API serves that one file, not a folder of images, so they are data: URIs in it): a photo per product and one hero picture.
 * The card picture is the same function the widget uses for its own product cards (window.ShopMateChat.picture, from shopmate-chat.js): the product's photo, or, for a
 * product without one, the fabric swatch (window.ShopMateChat.thumbnail). The page also gives the widget the photos by product slug, so the carousel of the
 * assistant shows the same pictures as the grid.
 * Everything from the file goes on the page as text, created nodes or an image `src` the widget has checked.
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
    var frame = el('div', 'frame');
    var draw = window.ShopMateChat && (window.ShopMateChat.picture || window.ShopMateChat.thumbnail);
    if (draw) frame.appendChild(draw(p, { decorative: true }));
    var thumb = el('div', 'thumb');
    thumb.appendChild(frame);
    var body = el('div', 'body');
    body.appendChild(el('h3', null, p.name));
    body.appendChild(el('span', 'meta', p.material + ' · ' + p.color));
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

  function showHero(hero) {
    var src = hero && typeof hero.image === 'string' ? hero.image : '';
    if (!/^data:image\/(?:webp|png|jpeg);base64,[A-Za-z0-9+\/=]+$/.test(src)) return;  // no picture, no empty frame
    var img = document.getElementById('hero-img');
    img.setAttribute('src', src);
    img.setAttribute('alt', typeof hero.alt === 'string' ? hero.alt : '');
    document.getElementById('hero-credit').textContent = typeof hero.credit === 'string' ? hero.credit : '';
    document.getElementById('hero-pic').hidden = false;
    document.getElementById('hero').classList.add('has-pic');
  }

  function givePhotosToTheWidget() {
    var photos = {};
    products.forEach(function (p) { if (p && typeof p.slug === 'string' && typeof p.image === 'string') photos[p.slug] = p.image; });
    if (window.ShopMateChat && window.ShopMateChat.setProductImages) window.ShopMateChat.setProductImages(photos);
  }

  fetch('/widget/demo-products.json', { cache: 'no-store' })
    .then(function (res) { if (!res.ok) throw new Error('HTTP ' + res.status); return res.json(); })
    .then(function (data) { products = Array.isArray(data.products) ? data.products : []; givePhotosToTheWidget(); showHero(data.hero); show(null); })
    .catch(function () { grid.replaceChildren(el('p', 'empty', 'Không đọc được danh sách sản phẩm của trang demo.')); count.textContent = ''; });
})();
