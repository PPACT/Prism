/** ANSI 剥离与限量尾部文本：旧后端仍回放原始 backlog 时，前端用它生成可回滚的纯文本预览。 */

const OSC_RE = /\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g;
const CSI_RE = /\x1b\[[0-?]*[ -/]*[@-~]/g;
const ESC_RE = /\x1b[@-_]/g;
const CTRL_RE = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g;

export function stripAnsi(text: string): string {
  return text
    .replace(OSC_RE, "")
    .replace(CSI_RE, "")
    .replace(ESC_RE, "")
    .replace(CTRL_RE, "")
    .replace(/\r/g, "");
}

/** 先按行保留最近 maxLines 行，再按字符数截断；避免把 100KB 历史帧塞进 xterm。 */
export function tailText(
  text: string,
  maxLines = 200,
  maxChars = 20 * 1024,
): string {
  const plain = stripAnsi(text);
  const lines = plain.split("\n");
  const tail = lines.slice(-maxLines).join("\n");
  return tail.length > maxChars ? tail.slice(-maxChars) : tail;
}

export function tailFromChunks(
  chunks: Uint8Array[],
  maxLines = 200,
  maxChars = 20 * 1024,
): string {
  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.length;
  }
  return tailText(new TextDecoder().decode(merged), maxLines, maxChars);
}
