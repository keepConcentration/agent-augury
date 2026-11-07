/** Safe Markdown for model text (WBS 3.2, N-004, 09 §6).
 *
 * marked tokens become React elements directly — there is no HTML string and no
 * ``innerHTML``, so raw HTML in model output can only ever show up as text.
 * Links work for http(s) only; images are never fetched or drawn (a picture of
 * an approval card would be the easiest fake card).
 */

import {Lexer, type Token, type Tokens} from "marked";
import {useMemo, type ReactNode} from "react";

const SAFE_HREF = /^https?:\/\//i;

/** marked leaves entities in ``text``; React escapes on its own, so decode once. */
function decode(text: string): string {
  return text
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&");
}

function hostOf(href: string): string | null {
  try {
    return new URL(href).hostname;
  } catch {
    return null;
  }
}

function inline(tokens: Token[] | undefined): ReactNode[] {
  return (tokens ?? []).map((token, i) => {
    switch (token.type) {
      case "text": {
        const t = token as Tokens.Text;
        return t.tokens?.length ? <span key={i}>{inline(t.tokens)}</span> : decode(t.text);
      }
      case "escape":
        return decode((token as Tokens.Escape).text);
      case "strong":
        return <strong key={i}>{inline((token as Tokens.Strong).tokens)}</strong>;
      case "em":
        return <em key={i}>{inline((token as Tokens.Em).tokens)}</em>;
      case "del":
        return <del key={i}>{inline((token as Tokens.Del).tokens)}</del>;
      case "codespan":
        return <code key={i}>{decode((token as Tokens.Codespan).text)}</code>;
      case "br":
        return <br key={i} />;
      case "link": {
        const link = token as Tokens.Link;
        const label = link.tokens?.length ? inline(link.tokens) : link.text;
        const host = hostOf(link.href);
        if (!SAFE_HREF.test(link.href) || host === null) {
          return <span key={i}>{label}</span>; // javascript:, data:, relative … → plain text
        }
        // Always say where it really goes: "[https://bank.com](https://evil.com)" must not pass.
        return (
          <span key={i}>
            <a href={link.href} target="_blank" rel="noopener noreferrer">
              {label}
            </a>
            <span className="md-host">{` (${host})`}</span>
          </span>
        );
      }
      case "image": {
        const image = token as Tokens.Image;
        return <span key={i} className="md-image">{`[이미지: ${decode(image.text) || "?"}]`}</span>;
      }
      default:
        // html and anything unknown: show what was written, never interpret it.
        return token.raw;
    }
  });
}

function block(token: Token, i: number): ReactNode {
  switch (token.type) {
    case "space":
      return null;
    case "paragraph":
      return <p key={i}>{inline((token as Tokens.Paragraph).tokens)}</p>;
    case "text": {
      const t = token as Tokens.Text;
      return <p key={i}>{t.tokens?.length ? inline(t.tokens) : decode(t.text)}</p>;
    }
    case "heading": {
      const h = token as Tokens.Heading;
      return <p key={i} className={`md-h md-h${h.depth}`}>{inline(h.tokens)}</p>;
    }
    case "code":
      return (
        <pre key={i}>
          <code>{(token as Tokens.Code).text}</code>
        </pre>
      );
    case "blockquote":
      return <blockquote key={i}>{(token as Tokens.Blockquote).tokens.map(block)}</blockquote>;
    case "list": {
      const list = token as Tokens.List;
      const items = list.items.map((item, j) => (
        <li key={j}>
          {item.task ? (item.checked ? "☑ " : "☐ ") : null}
          {item.tokens.map(block)}
        </li>
      ));
      return list.ordered ? <ol key={i}>{items}</ol> : <ul key={i}>{items}</ul>;
    }
    case "hr":
      return <hr key={i} />;
    case "table": {
      const table = token as Tokens.Table;
      return (
        <table key={i}>
          <thead>
            <tr>{table.header.map((cell, j) => <th key={j}>{inline(cell.tokens)}</th>)}</tr>
          </thead>
          <tbody>
            {table.rows.map((row, r) => (
              <tr key={r}>{row.map((cell, j) => <td key={j}>{inline(cell.tokens)}</td>)}</tr>
            ))}
          </tbody>
        </table>
      );
    }
    default:
      // html blocks and anything unknown: literal text.
      return <p key={i}>{token.raw}</p>;
  }
}

export function Markdown({text}: {text: string}) {
  // Lex once per text, not on every re-render of the row around it.
  const tokens = useMemo(() => new Lexer().lex(text), [text]);
  return <div className="md">{tokens.map(block)}</div>;
}
