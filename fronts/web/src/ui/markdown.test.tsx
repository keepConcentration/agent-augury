import assert from "node:assert/strict";
import {test} from "node:test";
import {renderToStaticMarkup} from "react-dom/server";
import {Markdown} from "./markdown.js";

const html = (text: string) => renderToStaticMarkup(<Markdown text={text} />);

test("raw HTML in model text is shown as text, never as markup (N-004)", () => {
  for (const attack of [
    "<script>alert(1)</script>",
    "hello <img src=x onerror=alert(1)> world",
    "<iframe src=https://evil.example></iframe>",
    "<a href=\"javascript:alert(1)\">x</a>",
    "<div style=\"position:fixed;inset:0\">FAKE APPROVAL</div>",
  ]) {
    const out = html(attack);
    assert.doesNotMatch(out, /<(script|img|iframe|div style|a href="javascript)/i, out);
    assert.match(out, /&lt;/, out);
  }
});

test("links: http(s) only, opened safely; other schemes become plain text", () => {
  const ok = html("[docs](https://example.com/a?b=1)");
  assert.match(ok, /<a href="https:\/\/example.com\/a\?b=1" target="_blank" rel="noopener noreferrer">docs<\/a>/);
  // The real destination is always shown: a label cannot pose as another site.
  const posing = html("[https://bank.example](https://evil.example/login)");
  assert.match(posing, />https:\/\/bank.example<\/a><span class="md-host"> \(evil.example\)<\/span>/);
  // A URL the browser cannot parse is not a link (and does not crash rendering).
  assert.doesNotMatch(html("[x](https://[::1)"), /<a /);
  for (const bad of ["[x](javascript:alert(1))", "[x](data:text/html;base64,PHNjcmlwdD4=)", "[x](vbscript:msgbox)", "[x](/api/cmd)"]) {
    const out = html(bad);
    assert.doesNotMatch(out, /<a /, out);
    assert.match(out, />x</, out);
  }
});

test("images are never fetched or drawn — external or data: (09 §6)", () => {
  for (const img of ["![card](https://evil.example/fake-approval.png)", "![card](data:image/png;base64,iVBORw0KGgo=)"]) {
    const out = html(img);
    assert.doesNotMatch(out, /<img/i, out);
    assert.match(out, /\[이미지: card\]/);
  }
});

test("ordinary Markdown still renders: emphasis, code, lists, tables, entities", () => {
  const out = html("**bold** _it_ `a<b`\n\n```\n<raw>\n```\n\n- one\n- two\n\n| h |\n|---|\n| c |\n\nAT&T &lt;tag&gt;");
  assert.match(out, /<strong>bold<\/strong>/);
  assert.match(out, /<em>it<\/em>/);
  assert.match(out, /<code>a&lt;b<\/code>/);
  assert.match(out, /<pre><code>&lt;raw&gt;<\/code><\/pre>/);
  assert.match(out, /<ul><li>.*one.*<\/li><li>.*two.*<\/li><\/ul>/s);
  assert.match(out, /<th>h<\/th>/);
  // CommonMark: "&lt;" is an entity for "<" (decoded once); "&amp;lt;" stays "&lt;" (not decoded twice).
  assert.match(out, /AT&amp;T &lt;tag&gt;/);
  assert.match(html("&amp;lt;x"), /<p>&amp;lt;x<\/p>/);
  assert.match(html("- [ ] todo\n- [x] done"), /☐ .*todo.*☑ .*done/s);
});
