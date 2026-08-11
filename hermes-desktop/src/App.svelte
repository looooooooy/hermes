<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import Onboarding from './lib/Onboarding.svelte';
  import RuntimeCockpit from './RuntimeCockpit.svelte';
  import { mockRuntimeSnapshot } from './lib/mock-runtime';
  import type { RuntimeSnapshot } from './lib/types';

  type WorkspaceAuthStatus = {
    authenticated: boolean;
    endpoint?: string | null;
    userId?: string | null;
    provider?: string | null;
    expiresAtEpochSeconds?: number | null;
  };

  let snapshot: RuntimeSnapshot = mockRuntimeSnapshot;
  let nativeChecked = false;
  let nativeSource = false;
  let refreshing = false;
  let workspaceConnecting = false;
  let workspaceError = '';
  let devicePairing = false;
  let devicePairingError = '';

  onMount(loadNativeSnapshot);

  async function loadNativeSnapshot() {
    try {
      snapshot = await invoke<RuntimeSnapshot>('runtime_snapshot');
      nativeSource = true;
    } catch {
      nativeSource = false;
    } finally {
      nativeChecked = true;
    }
  }

  async function refreshEvidence() {
    if (refreshing) return;
    refreshing = true;
    try {
      snapshot = await invoke<RuntimeSnapshot>('runtime_snapshot');
      nativeSource = true;
    } catch {
      nativeSource = false;
      snapshot = { ...snapshot, lastChecked: '状态刷新失败' };
    } finally {
      refreshing = false;
    }
  }

  async function connectWorkspace(endpoint: string) {
    if (workspaceConnecting) return;
    workspaceConnecting = true;
    workspaceError = '';
    try {
      const result = await invoke<WorkspaceAuthStatus>('workspace_connect', { endpoint });
      if (!result.authenticated) {
        throw new Error('登录已完成，但没有拿到有效的企业工作空间会话。');
      }

      devicePairingError = '';
      snapshot = {
        ...snapshot,
        workspaceAuthenticated: true,
        workspaceEndpoint: result.endpoint ?? endpoint,
        workspaceUser: result.userId ?? snapshot.workspaceUser,
        lastChecked: '企业账号已登录',
      };
      nativeSource = true;
    } catch (error) {
      workspaceError = typeof error === 'string'
        ? error
        : error instanceof Error
          ? error.message
          : '企业工作空间登录没有完成，请重新尝试。';
    } finally {
      workspaceConnecting = false;
    }
  }

  function devicePairingCommandError(error: unknown) {
    if (!error || typeof error !== 'object') return undefined;
    const candidate = error as { code?: unknown; message?: unknown };
    if (typeof candidate.code !== 'string' || typeof candidate.message !== 'string') {
      return undefined;
    }
    return { code: candidate.code, message: candidate.message };
  }

  async function pairDevice() {
    if (devicePairing || workspaceConnecting) return;
    devicePairing = true;
    devicePairingError = '';
    workspaceError = '';
    try {
      await invoke('device_pair');
      await refreshEvidence();
    } catch (error) {
      if (devicePairingCommandError(error)?.code === 'workspace_reauth_required') {
        devicePairingError = '';
        workspaceError = '企业工作空间登录已过期，请重新登录。';
        snapshot = {
          ...snapshot,
          workspaceAuthenticated: false,
          lastChecked: '企业账号需要重新登录',
        };
      } else {
        devicePairingError = '设备绑定没有完成，请重新尝试。';
        await refreshEvidence();
      }
    } finally {
      devicePairing = false;
    }
  }

  function componentHealthy(id: string) {
    return snapshot.components.some((component) => component.id === id && component.state === 'healthy');
  }

  function onboardingComplete() {
    const managerConnected = snapshot.runtimeGeneration !== 'manager-not-connected'
      && !['not-connected', 'not-installed'].includes(snapshot.runtimeVersion);
    const localAuthorityReady = snapshot.agentReady && componentHealthy('core') && componentHealthy('plugin');
    const providerConfigured = snapshot.providers.some((provider) => provider.state === 'connected');
    return managerConnected
      && snapshot.workspaceAuthenticated
      && snapshot.cloudConnected
      && snapshot.devicePaired
      && localAuthorityReady
      && providerConfigured;
  }

  function onboardingStateKey() {
    return [
      snapshot.workspaceAuthenticated ? 'workspace-on' : 'workspace-off',
      snapshot.devicePaired ? 'device-on' : 'device-off',
      snapshot.runtimeVersion,
      snapshot.agentReady ? 'agent-on' : 'agent-off',
    ].join(':');
  }
</script>

{#if !nativeChecked}
  <div class="boot-shell" aria-label="正在启动 Hermes Desktop">
    <div class="boot-mark" aria-hidden="true"><span></span></div>
    <strong>Hermes</strong>
    <span>正在验证本机运行环境…</span>
  </div>
{:else if nativeSource && !onboardingComplete()}
  {#key onboardingStateKey()}
    <Onboarding
      {snapshot}
      {refreshing}
      {workspaceConnecting}
      {workspaceError}
      {devicePairing}
      {devicePairingError}
      onRefresh={refreshEvidence}
      onConnectWorkspace={connectWorkspace}
      onPairDevice={pairDevice}
    />
  {/key}
{:else}
  <RuntimeCockpit />
{/if}

<style>
  .boot-shell {
    display: grid;
    place-items: center;
    align-content: center;
    width: 100%;
    height: 100%;
    gap: 14px;
    color: var(--text);
    background:
      radial-gradient(circle at 50% 43%, rgba(83, 193, 157, .09), transparent 25%),
      linear-gradient(180deg, #081210, #07100f);
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", sans-serif;
  }
  .boot-shell strong { margin-top: 10px; font-size: 22px; font-weight: 700; letter-spacing: -.02em; }
  .boot-shell > span:last-child { color: #789087; font-size: 15px; line-height: 1.5; }
  .boot-mark { position: relative; width: 52px; height: 52px; border: 1px solid rgba(111,227,189,.19); border-radius: 50%; animation: boot-orbit 2.4s linear infinite; }
  .boot-mark::after { content: ''; position: absolute; inset: 9px; border: 1px solid rgba(117,168,255,.17); border-radius: 50%; transform: rotate(64deg) scaleY(.62); }
  .boot-mark span { position: absolute; left: 18px; top: 18px; width: 14px; height: 14px; border-radius: 50%; background: var(--accent-strong); box-shadow: 0 0 26px rgba(111,227,189,.32); }
  @keyframes boot-orbit { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .boot-mark { animation: none; } }
</style>
