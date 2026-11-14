import type { PermissionLevel, ProviderCredentialInput, SaveUserSettingsRequest } from "./type";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// 浏览器可选项。刻意不含 ""：空串在写侧表示「保留库里原值」（后端 COALESCE），
// 那是给「不知道档位」的调用方用的，而设置表单永远知道用户选了什么。
const PERMISSION_LEVELS: readonly PermissionLevel[] = ["read_only", "approval", "full"];

// 返回 undefined = 本次不修改；null = 非法值（整份拒绝）。
// 非法值不静默降级：静默降到更严格的一档，用户会以为改动生效了。
function parsePermissionLevel(value: unknown): PermissionLevel | null | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== "string") return null;
  const level = value.trim() as PermissionLevel;
  return PERMISSION_LEVELS.includes(level) ? level : null;
}

// parseSaveUserSettingsRequest is the browser-facing settings boundary before
// The BFF route adds userId.
export function parseSaveUserSettingsRequest(value: unknown): SaveUserSettingsRequest | null {
  if (!isRecord(value) || typeof value.personality !== "string" || typeof value.defaultModelId !== "string" || !value.defaultModelId.trim() || !Array.isArray(value.providers)) {
    return null;
  }

  const permissionLevel = parsePermissionLevel(value.permissionLevel);
  if (permissionLevel === null) {
    return null;
  }

  const providers: ProviderCredentialInput[] = [];
  for (const provider of value.providers) {
    if (!isRecord(provider) || typeof provider.providerId !== "string" || !provider.providerId.trim()) {
      return null;
    }
    if ("apiKey" in provider && typeof provider.apiKey !== "string") {
      return null;
    }
    providers.push({
      providerId: provider.providerId.trim(),
      ...(typeof provider.apiKey === "string" ? { apiKey: provider.apiKey } : {}),
    });
  }
  return {
    personality: value.personality,
    defaultModelId: value.defaultModelId.trim(),
    ...(typeof value.timezone === "string" ? { timezone: value.timezone.trim() } : {}),
    ...(permissionLevel !== undefined ? { permissionLevel } : {}),
    providers,
  };
}

