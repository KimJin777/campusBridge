// 실행: node --test frontend/tests
import assert from "node:assert/strict";
import { test } from "node:test";

import { createSSEParser } from "../sse.js";

test("잘린 청크를 합쳐 완성된 이벤트만 반환", () => {
  const p = createSSEParser();
  assert.deepEqual(p.push('event: meta\ndata: {"turn_'), []);
  assert.deepEqual(p.push('id":"t_r"}\n'), []);
  assert.deepEqual(p.push("\nevent: done\ndata: {}\n\n"), [
    { event: "meta", data: { turn_id: "t_r" } },
    { event: "done", data: {} },
  ]);
  assert.equal(p.rest(), "");
});

test("ping 주석은 무시하고 CRLF도 처리", () => {
  const p = createSSEParser();
  assert.deepEqual(p.push(': ping\r\n\r\nevent: status\r\ndata: {"step":"act"}\r\n\r\n'), [
    { event: "status", data: { step: "act" } },
  ]);
});

test("한글 멀티바이트가 잘려도 디코더 뒤에서는 문자열 단위로 합쳐진다", () => {
  const p = createSSEParser();
  const full = 'event: answer\ndata: {"t":"휴학은 통산 3년"}\n\n';
  const bytes = new TextEncoder().encode(full);
  const dec = new TextDecoder();
  const out = [];
  for (let i = 0; i < bytes.length; i += 5) {
    out.push(...p.push(dec.decode(bytes.slice(i, i + 5), { stream: true })));
  }
  assert.deepEqual(out, [{ event: "answer", data: { t: "휴학은 통산 3년" } }]);
});
