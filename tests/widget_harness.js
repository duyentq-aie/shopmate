// Runs the thumbnail part of widget/shopmate-chat.js under Node with a tiny fake DOM and prints JSON.
//   node tests/widget_harness.js widget/shopmate-chat.js "<expression using `api`>"
// With no customElements on the fake window the script stops after it has published window.ShopMateChat (thumbnail, shapeOf, colorOf, patternOf).
// `xml(node)` serialises a fake node, so a test can parse the drawing with a real XML parser.
const vm = require('vm'), fs = require('fs');

class Node {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.text = ''; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  appendChild(c) { this.children.push(c); return c; }
}
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
function xml(n) {
  const a = Object.keys(n.attrs).map(k => ' ' + k + '="' + esc(n.attrs[k]) + '"').join('');
  return '<' + n.tag + a + '>' + esc(n.text) + n.children.map(xml).join('') + '</' + n.tag + '>';
}
const sandbox = {window: {}, document: {createElementNS: (ns, tag) => new Node(tag)}, console};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);
sandbox.api = sandbox.window.ShopMateChat;
sandbox.xml = xml;
if (!sandbox.api || !sandbox.api.thumbnail) { console.error('the script did not publish window.ShopMateChat'); process.exit(3); }
console.log(JSON.stringify(vm.runInContext(process.argv[3], sandbox)));
