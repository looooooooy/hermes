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
      snapshot = { ...snapshot, lastChecked: 'refresh failed' };
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
        throw new Error('Hermes Cloud sign-in completed without an active workspace session.');
      }

      // The native command is the authority for this transition. Apply its
      // authenticated result immediately instead of waiting for a second
      // snapshot round-trip that can race with short-lived credential state.
      snapshot = {
        ...snapshot,
        workspaceAuthenticated: true,
        workspaceEndpoint: result.endpoint ?? endpoint,
        workspaceUser: result.userId ?? snapshot.workspaceUser,
        lastChecked: 'workspace authenticated',
      };
      nativeSource = true;
    } catch (error) {
      workspaceError = typeof error === 'string'
        ? error
        : error instanceof Error
          ? error.message
          : 'Hermes workspace sign-in did not complete.';
    } finally {
      workspaceConnecting = false;
    }
  }

  async function pairDevice() {
    if (devicePairing) return;
    devicePairing = true;
    devicePairingError = '';
    try {
      await invoke('device_pair');
      await refreshEvidence();
    } catch (error) {
      devicePairingError = typeof error === 'string'
        ? error
        : 'Hermes device pairing did not complete.';
      await refreshEvidence();
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
</script>

{#if !nativeChecked}
  <div class="boot-shell" aria-label="Starting Hermes Desktop">
    <div class="boot-mark" aria-hidden="true"><span></span></div>
    <strong>Hermes</strong>
    <span>Verifying local runtime authority…</span>
  </div>
{:else if nativeSource && !onboardingComplete()}
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
    gap: 10px;
    color: var(--text);
    background:
      radial-gradient(circle at 50% 43%, rgba(83, 193, 157, .075), transparent 24%),
      linear-gradient(180deg, #081210, #07100f);
  }
  .boot-shell strong { margin-top: 7px; font-size: 15px; font-weight: 690; letter-spacing: -.02em; }
  .boot-shell > span:last-child { color: #60766f; font-size: 9px; letter-spacing: .035em; }
  .boot-mark { position: relative; width: 42px; height: 42px; border: 1px solid rgba(111,227,189,.16); border-radius: 50%; animation: boot-orbit 2.4s linear infinite; }
  .boot-mark::after { content: ''; position: absolute; inset: 7px; border: 1px solid rgba(117,168,255,.15); border-radius: 50%; transform: rotate(64deg) scaleY(.62); }
  .boot-mark span { position: absolute; left: 15px; top: 15px; width: 10px; height: 10px; border-radius: 50%; background: var(--accent-strong); box-shadow: 0 0 24px rgba(111,227,189,.28); }
  @keyframes boot-orbit { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .boot-mark { animation: none; } }
</style>