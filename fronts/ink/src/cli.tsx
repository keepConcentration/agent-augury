#!/usr/bin/env node
/**
 * Ink Surface entry.
 *
 * Clears the TTY (viewport + scrollback when supported) before render so the
 * wizard / npm / shell history do not sit above the UI — same idea as Cursor /
 * Claude Code starting on a clean frame.
 */
import React from "react";
import {render} from "ink";
import App from "./App.js";

function clearTerminal(): void {
  if (!process.stdout.isTTY) {
    return;
  }
  // ESC[3J = scrollback (xterm/Windows Terminal); ESC[2J = viewport; ESC[H = home
  process.stdout.write("\x1b[3J\x1b[2J\x1b[H");
}

clearTerminal();
render(<App />);
