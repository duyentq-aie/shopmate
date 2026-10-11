// Runs the pure part of dashboard/index.html under Node and prints JSON.
//   node tests/dashboard_harness.js dashboard/index.html "<expression using `api`>"
// The page script returns before touching the DOM when window.__SHOPMATE_TEST__ is set.
const vm = require('vm'), fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const match = /<script>([\s\S]*)<\/script>/.exec(html);
if (!match) { console.error('no <script> in the page'); process.exit(2); }
const sandbox = {window: {__SHOPMATE_TEST__: true}, console};
vm.createContext(sandbox);
vm.runInContext(match[1], sandbox);
sandbox.api = sandbox.window.__shopmate;
if (!sandbox.api) { console.error('the page did not expose __shopmate'); process.exit(3); }
const value = vm.runInContext(process.argv[3], sandbox);
console.log(JSON.stringify(value));
