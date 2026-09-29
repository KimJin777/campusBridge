import assert from "node:assert/strict";
import { test } from "node:test";

import { googleCalendarUrl, icsText } from "../ics.js";

test("all-day ics uses exclusive end date and escapes text", () => {
  const t = icsText({ title: "중간고사, 기간; 안내", start: "2026-10-20", end: "2026-10-26", url: "https://www.kyungnam.ac.kr/x" });
  assert.match(t, /DTSTART;VALUE=DATE:20261020/);
  assert.match(t, /DTEND;VALUE=DATE:20261027/);
  assert.ok(t.includes(String.raw`SUMMARY:중간고사\, 기간\; 안내`));
  assert.match(t, /TRIGGER:-P1D/);
  assert.ok(t.includes("\r\n"));
});

test("single day and month rollover", () => {
  const t = icsText({ title: "마감", start: "2026-09-30", end: "2026-09-30" });
  assert.match(t, /DTEND;VALUE=DATE:20261001/);
  const g = new URL(googleCalendarUrl({ title: "마감", start: "2026-12-31", end: "2026-12-31" }));
  assert.equal(g.searchParams.get("dates"), "20261231/20270101");
});
