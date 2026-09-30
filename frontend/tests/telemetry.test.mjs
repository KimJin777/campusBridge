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
