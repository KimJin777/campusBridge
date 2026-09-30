// 실행: node --test frontend/tests/*.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import { hash36, isNoise } from "../telemetry.js";

const O = "https://campusbridge.example";

test("우리 스크립트 오류만 수집하고 잡음은 버린다", () => {
  assert.equal(isNoise("TypeError: x", `${O}/app.js`, O), false);
  assert.equal(isNoise("ResizeObserver loop limit exceeded", `${O}/app.js`, O), true);
  assert.equal(isNoise("Script error.", "", O), true);
  assert.equal(isNoise("boom", "chrome-extension://abc/content.js", O), true);
  assert.equal(isNoise("boom", "https://dapi.kakao.com/sdk.js", O), true);
});

test("지문은 짧고 결정적이다", () => {
  assert.equal(hash36("/app.js:10:5"), hash36("/app.js:10:5"));
  assert.match(hash36("/app.js:10:5"), /^[0-9a-z]{1,8}$/);
});

test("새로고침해도 탭 세션당 3건 상한이 유지된다(GPT5 #782)", async () => {
  const store = new Map([["campusbridge.cerr.sent", "3"]]);
  globalThis.sessionStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  let calls = 0;
  globalThis.fetch = () => {
    calls += 1;
    return Promise.resolve({});
  };
  const fresh = await import(`../telemetry.js?reload=${Date.now()}`);
  fresh.reportError("js_error");
  assert.equal(calls, 0);
  store.set("campusbridge.cerr.sent", "1");
  const again = await import(`../telemetry.js?reload=${Date.now() + 1}`);
  again.reportError("js_error");
  again.reportError("js_error");
  again.reportError("js_error");
  assert.equal(calls, 2);
  assert.equal(store.get("campusbridge.cerr.sent"), "3");
});
