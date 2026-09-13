/** Lightweight Markdown → Ink elements (pt TUI rich.Markdown parity). */

import React from "react";
import {Box, Text, Newline} from "ink";
import {Lexer, type Token, type Tokens} from "marked";

type Props = {
  children: string;
};

function Inline({tokens}: {tokens?: Token[]}) {
  if (!tokens?.length) {
    return null;
  }
  return (
    <>
      {tokens.map((token, i) => {
        const key = `${token.type}-${i}`;
        switch (token.type) {
          case "text":
            return <Text key={key}>{decodeEntities((token as Tokens.Text).text)}</Text>;
          case "strong":
            return (
              <Text key={key} bold>
                <Inline tokens={(token as Tokens.Strong).tokens} />
              </Text>
            );
          case "em":
            return (
              <Text key={key} italic>
                <Inline tokens={(token as Tokens.Em).tokens} />
              </Text>
            );
          case "codespan":
            return (
              <Text key={key} color="cyan">
                {(token as Tokens.Codespan).text}
              </Text>
            );
          case "link": {
            const link = token as Tokens.Link;
            const label = link.tokens?.length ? (
              <Inline tokens={link.tokens} />
            ) : (
              <Text>{link.text}</Text>
            );
            return (
              <Text key={key} color="blue" underline>
                {label}
                <Text dimColor>{` (${link.href})`}</Text>
              </Text>
            );
          }
          case "escape":
            return <Text key={key}>{(token as Tokens.Escape).text}</Text>;
          case "br":
            return <Newline key={key} />;
          case "del":
            return (
              <Text key={key} strikethrough>
                <Inline tokens={(token as Tokens.Del).tokens} />
              </Text>
            );
          default:
            if ("text" in token && typeof (token as {text?: string}).text === "string") {
              return <Text key={key}>{(token as {text: string}).text}</Text>;
            }
            return null;
        }
      })}
    </>
  );
}

function Block({token}: {token: Token}) {
  switch (token.type) {
    case "space":
      return <Box height={1} />;
    case "heading": {
      const h = token as Tokens.Heading;
      return (
        <Text bold color={h.depth <= 2 ? "magenta" : "white"}>
          <Inline tokens={h.tokens} />
        </Text>
      );
    }
    case "paragraph":
      return (
        <Text>
          <Inline tokens={(token as Tokens.Paragraph).tokens} />
        </Text>
      );
    case "code": {
      const code = token as Tokens.Code;
      const lines = code.text.replace(/\n$/, "").split("\n");
      return (
        <Box flexDirection="column" marginY={0} borderStyle="single" borderColor="gray" paddingX={1}>
          {code.lang ? (
            <Text dimColor>
              {code.lang}
            </Text>
          ) : null}
          {lines.map((line, i) => (
            <Text key={i} color="green">
              {line || " "}
            </Text>
          ))}
        </Box>
      );
    }
    case "blockquote":
      return (
        <Box flexDirection="column" marginLeft={2}>
          <Blocks tokens={(token as Tokens.Blockquote).tokens} />
        </Box>
      );
    case "list": {
      const list = token as Tokens.List;
      return (
        <Box flexDirection="column">
          {list.items.map((item, i) => (
            <Box key={i} flexDirection="row" gap={1}>
              <Text dimColor>{list.ordered ? `${i + 1}.` : "•"}</Text>
              <Box flexDirection="column">
                <Blocks tokens={item.tokens} />
              </Box>
            </Box>
          ))}
        </Box>
      );
    }
    case "hr":
      return <Text dimColor>{"─".repeat(40)}</Text>;
    case "text":
      return (
        <Text>
          <Inline tokens={(token as Tokens.Text).tokens ?? undefined} />
          {!(token as Tokens.Text).tokens ? (token as Tokens.Text).text : null}
        </Text>
      );
    case "html":
      return <Text dimColor>{(token as Tokens.HTML).text}</Text>;
    default:
      if ("raw" in token && typeof (token as {raw?: string}).raw === "string") {
        return <Text>{(token as {raw: string}).raw}</Text>;
      }
      return null;
  }
}

function Blocks({tokens}: {tokens: Token[]}) {
  return (
    <>
      {tokens.map((token, i) => (
        <Box key={`${token.type}-${i}`} flexDirection="column">
          <Block token={token} />
        </Box>
      ))}
    </>
  );
}

function decodeEntities(text: string): string {
  return text
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'");
}

/** Render Markdown text as Ink elements. Falls back to plain Text on parse errors. */
export default function Markdown({children}: Props) {
  const src = children ?? "";
  if (!src.trim()) {
    return null;
  }
  try {
    const tokens = Lexer.lex(src);
    return (
      <Box flexDirection="column">
        <Blocks tokens={tokens} />
      </Box>
    );
  } catch {
    return <Text>{src}</Text>;
  }
}
