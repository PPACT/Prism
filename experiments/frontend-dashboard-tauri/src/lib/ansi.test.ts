import { describe, expect, it } from "vitest";

import { stripAnsi, tailFromChunks, tailText } from "./ansi";

describe("stripAnsi", () => {
  it("removes CSI/OSC/control sequences but keeps text and newlines", () => {
    const raw = "\x1b[2J\x1b[H\x1b[31mred\x1b[0m\n\x1b]0;title\x07ok\r\n";
    expect(stripAnsi(raw)).toBe("red\nok\n");
  });
});

describe("tailText", () => {
  it("keeps only the last lines and characters", () => {
    const raw = Array.from({ length: 300 }, (_, index) => `line-${index}`).join("\n");
    const tail = tailText(raw, 200, 20 * 1024);
    expect(tail.split("\n")).toHaveLength(200);
    expect(tail.startsWith("line-100")).toBe(true);
    expect(tailText(`a\n${"x".repeat(50)}`, 200, 20).endsWith("x".repeat(20))).toBe(true);
  });
});

describe("tailFromChunks", () => {
  it("decodes split ANSI chunks and returns plain tail", () => {
    const encoder = new TextEncoder();
    const chunks = [
      encoder.encode("\x1b[32mgreen"),
      encoder.encode("-text\x1b[0m\nlast line"),
    ];
    expect(tailFromChunks(chunks)).toBe("green-text\nlast line");
  });
});
