/**
 * 设置提交边界的回归（`lib/userconfig/request.ts`）。
 *
 *   node --test tests/userconfig-request.test.ts
 *
 * 权限档位在这里有个容易做错的地方：非法值**不能静默降级**。悄悄换成
 * read_only 看起来「更安全」，但用户以为切到了自己选的档位，实际是另一档——
 * 这类静默偏差在权限系统里比报错危险得多，所以整份拒绝（返回 null → 400）。
 */
import test from "node:test";
import assert from "node:assert/strict";

import { parseSaveUserSettingsRequest } from "../lib/userconfig/request.ts";

function body(extra: Record<string, unknown> = {}) {
  return {
    personality: "简洁一点",
    defaultModelId: "deepseek-v4.1-flash",
    timezone: "Asia/Shanghai",
    providers: [{ providerId: "dashscope" }],
    ...extra,
  };
}

test("合法档位原样带出", () => {
  const parsed = parseSaveUserSettingsRequest(body({ permissionLevel: "read_only" }));
  assert.equal(parsed?.permissionLevel, "read_only");
});

test("档位带空白时归一", () => {
  const parsed = parseSaveUserSettingsRequest(body({ permissionLevel: " approval " }));
  assert.equal(parsed?.permissionLevel, "approval");
});

test("不传档位 = 本次不修改（字段不出现在请求里）", () => {
  const parsed = parseSaveUserSettingsRequest(body());
  assert.ok(parsed);
  assert.equal("permissionLevel" in parsed, false);
});

test("空串不是合法档位：浏览器不该用「保留原值」这个写侧约定", () => {
  assert.equal(parseSaveUserSettingsRequest(body({ permissionLevel: "" })), null);
});

test("非法档位整份拒绝，不静默降级", () => {
  assert.equal(parseSaveUserSettingsRequest(body({ permissionLevel: "admin" })), null);
  assert.equal(parseSaveUserSettingsRequest(body({ permissionLevel: 1 })), null);
});

test("其余字段仍然照旧解析（档位是新加的，不能挤掉老的）", () => {
  const parsed = parseSaveUserSettingsRequest(
    body({ permissionLevel: "full", providers: [{ providerId: " dashscope ", apiKey: "sk-1" }] }),
  );
  assert.ok(parsed);
  assert.equal(parsed.personality, "简洁一点");
  assert.equal(parsed.defaultModelId, "deepseek-v4.1-flash");
  assert.equal(parsed.timezone, "Asia/Shanghai");
  assert.equal(parsed.permissionLevel, "full");
  assert.deepEqual(parsed.providers, [{ providerId: "dashscope", apiKey: "sk-1" }]);
});
