import assert from "node:assert/strict";
import { test } from "node:test";

import {
  normalizePhoneInput,
  placeVerificationIssue,
  schoolSourceUrl,
} from "../admin-utils.js";

test("대표전화는 지역번호 포함 형식만 허용한다", () => {
  assert.equal(normalizePhoneInput(" 055-249-2027 "), "055-249-2027");
  assert.equal(normalizePhoneInput(""), "");
  assert.throws(() => normalizePhoneInput("249-2027"), /055-249-1234/);
});

test("검수 요건이 부족한 장소는 사유와 함께 건너뛴다", () => {
  const valid = {
    kind: "unit",
    source_url: "https://www.kyungnam.ac.kr/ko/4251/subview.do",
    snapshot_at: "2026-09-29",
    phone: "055-249-2027",
  };
  assert.equal(placeVerificationIssue(valid), null);
  assert.equal(placeVerificationIssue({ ...valid, source_url: "http://example.com" }), "학교 HTTPS 원문 URL 없음");
  assert.equal(placeVerificationIssue({ ...valid, snapshot_at: "" }), "확인일 없음");
  assert.equal(placeVerificationIssue({ ...valid, phone: "249-2027" }), "대표전화 형식 오류");
  assert.equal(placeVerificationIssue({ ...valid, kind: "building", phone: "" }), "위치 또는 부서 대표전화 없음");
});

test("학교 하위 도메인의 HTTPS 원문만 허용한다", () => {
  assert.equal(schoolSourceUrl("https://ifes.kyungnam.ac.kr/about"), true);
  assert.equal(schoolSourceUrl("https://evil.example/"), false);
  assert.equal(schoolSourceUrl("http://www.kyungnam.ac.kr/"), false);
});
