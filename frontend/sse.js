// SSE 청크 버퍼링 파서(상세설계 05 §1-2). 네트워크 청크가 이벤트 중간에서 잘려도
// 빈 줄 단위로 완성된 이벤트만 돌려주고, 남은 조각은 다음 청크와 합친다.

export function createSSEParser() {
  let buffer = "";
  return {
    /** @param {string} chunk @returns {{event: string, data: any}[]} */
    push(chunk) {
      buffer += chunk.replace(/\r\n/g, "\n");
      const out = [];
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const ev = parseBlock(block);
        if (ev) out.push(ev);
      }
      return out;
    },
    rest() {
      return buffer;
    },
  };
}

function parseBlock(block) {
  let event = "message";
  const data = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue; // 주석(ping)
    const i = line.indexOf(":");
    const field = i < 0 ? line : line.slice(0, i);
    let value = i < 0 ? "" : line.slice(i + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  if (!data.length) return null;
  const raw = data.join("\n");
  try {
    return { event, data: JSON.parse(raw) };
  } catch {
    return { event, data: raw };
  }
}
