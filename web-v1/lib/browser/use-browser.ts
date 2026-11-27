"use client";

import { useCallback, useEffect, useState } from "react";

import {
  EMPTY_BROWSER_STATE,
  browserCommand,
  getBrowserBridge,
  type BrowserState,
} from "./bridge";

/**
 * 订阅浏览器状态 + 发送命令。
 *
 * 状态只有一份来源：主进程通过 `browser:state` 推送。这里不做乐观更新——
 * 点「后退」后地址栏要等页面真的变了才更新，否则会出现「地址栏已经变了、
 * 页面还在旧页」的错觉，而浏览器最忌讳的就是地址栏骗人。
 */
export function useBrowser() {
  const [state, setState] = useState<BrowserState>(EMPTY_BROWSER_STATE);
  const [available, setAvailable] = useState(false);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const bridge = getBrowserBridge();
    if (!bridge) {
      setReady(true);
      return;
    }
    setAvailable(true);
    const unsubscribe = bridge.onState((next) => setState(next));
    void browserCommand<BrowserState>("state")
      .then(setState)
      .catch(() => undefined)
      .finally(() => setReady(true));
    return unsubscribe;
  }, []);

  const call = useCallback(async (method: string, params?: Record<string, unknown>) => {
    try {
      const result = await browserCommand(method, params);
      setError("");
      return result;
    } catch (err) {
      setError(err instanceof Error ? err.message : "浏览器操作失败");
      return null;
    }
  }, []);

  return { state, available, ready, error, call, clearError: () => setError("") };
}
