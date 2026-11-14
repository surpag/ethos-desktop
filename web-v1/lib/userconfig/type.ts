export type ProviderCredentialState = { providerId: string; hasApiKey: boolean };

/** 执行档位：read_only 只读、approval 每次写/执行需批准、full 不设限（含 Shell） */
export type PermissionLevel = "read_only" | "approval" | "full";

export type UserSettingsResponse = { personality: string; defaultModelId: string; timezone: string; gitAuthorName: string; gitAuthorEmail: string; permissionLevel: PermissionLevel; providers: ProviderCredentialState[] };
export type ProviderCredentialInput = { providerId: string; apiKey?: string };
export type SaveUserSettingsRequest = { personality: string; defaultModelId: string; timezone?: string; gitAuthorName?: string; gitAuthorEmail?: string; permissionLevel?: PermissionLevel; providers: ProviderCredentialInput[] };
