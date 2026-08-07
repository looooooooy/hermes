<script lang="ts">
  import {
    ArrowRight,
    Cable,
    Check,
    CircleDot,
    Cloud,
    Cpu,
    KeyRound,
    Laptop,
    RefreshCw,
    ShieldCheck,
    Sparkles,
  } from 'lucide-svelte';
  import type { RuntimeSnapshot } from './types';

  export let snapshot: RuntimeSnapshot;
  export let refreshing = false;
  export let onRefresh: () => void;

  type StepKey = 'foundation' | 'enterprise' | 'device' | 'runtime' | 'provider' | 'ready';
  type StepState = 'complete' | 'current' | 'pending';

  const steps: Array<{ key: StepKey; eyebrow: string; title: string }> = [
    { key: 'foundation', eyebrow: '01', title: 'Desktop foundation' },
    { key: 'enterprise', eyebrow: '02', title: 'Enterprise Cloud' },
    { key: 'device', eyebrow: '03', title: 'Device pairing' },
    { key: 'runtime', eyebrow: '04', title: 'Managed Runtime' },
    { key: 'provider', eyebrow: '05', title: 'Model service' },
    { key: 'ready', eyebrow: '06', title: 'Ready' },
  ];

  function componentHealthy(id: string) {
    return snapshot.components.some((component) => component.id === id && component.state === 'healthy');
  }

  function managerConnected() {
    return snapshot.runtimeGeneration !== 'manager-not-connected' && snapshot.runtimeVersion !== 'not-connected';
  }

  function devicePaired() {
    return componentHealthy('connector');
  }

  function runtimeInstalled() {
    return !['not-connected', 'not-installed'].includes(snapshot.runtimeVersion);
  }

  function localAuthorityReady() {
    return runtimeInstalled() && componentHealthy('core') && componentHealthy('plugin') && snapshot.agentReady;
  }

  function providerConfigured() {
    return snapshot.providers.some((provider) => provider.state === 'connected');
  }

  function completelyReady() {
    return managerConnected()
      && snapshot.cloudConnected
      && devicePaired()
      && localAuthorityReady()
      && providerConfigured();
  }

  function currentStep(): StepKey {
    if (!managerConnected()) return 'foundation';
    if (!snapshot.cloudConnected) return 'enterprise';
    if (!devicePaired()) return 'device';
    if (!localAuthorityReady()) return 'runtime';
    if (!providerConfigured()) return 'provider';
    return 'ready';
  }

  function stepComplete(key: StepKey) {
    switch (key) {
      case 'foundation': return managerConnected();
      case 'enterprise': return snapshot.cloudConnected;
      case 'device': return devicePaired();
      case 'runtime': return localAuthorityReady();
      case 'provider': return providerConfigured();
      case 'ready': return completelyReady();
    }
  }

  function stepState(key: StepKey): StepState {
    if (stepComplete(key)) return 'complete';
    return currentStep() === key ? 'current' : 'pending';
  }

  function activeTitle() {
    switch (currentStep()) {
      case 'foundation': return 'Prepare this computer for Hermes.';
      case 'enterprise': return 'Connect your enterprise workspace.';
      case 'device': return 'Approve this device as an Agent host.';
      case 'runtime': return 'Verify the local Managed Runtime.';
      case 'provider': return 'Connect a model service locally.';
      case 'ready': return 'Hermes is ready to work.';
    }
  }

  function activeDescription() {
    switch (currentStep()) {
      case 'foundation':
        return 'Hermes Desktop is open, but the local Runtime Manager has not provided a verified authority snapshot yet.';
      case 'enterprise':
        return 'The local manager is reachable. The next evidence gate is an authenticated Cloud connection for this enterprise account.';
      case 'device':
        return 'Cloud is available, but the Connector has not yet proven that this computer is paired as the current user’s device.';
      case 'runtime':
        return 'The device is connected. Hermes now requires the content-addressed private runtime, Core and Agent Plugin to become healthy.';
      case 'provider':
        return 'Local execution is healthy. Add at least one model provider; its credential must remain in the platform secret store on this host.';
      case 'ready':
        return 'Every onboarding gate has evidence: local authority, Cloud transport, device identity, Managed Runtime and model service.';
    }
  }

  function blockerTitle() {
    switch (currentStep()) {
      case 'foundation': return 'Runtime Manager evidence missing';
      case 'enterprise': return 'Cloud authentication not established';
      case 'device': return 'Connector pairing not established';
      case 'runtime': return runtimeInstalled() ? 'Core / Plugin authority not ready' : 'Managed Runtime not installed';
      case 'provider': return 'No model provider is connected';
      case 'ready': return 'All evidence gates passed';
    }
  }

  function blockerDetail() {
    switch (currentStep()) {
      case 'foundation':
        return 'The Desktop shell will not invent a healthy state. Start or install the Runtime Manager, then refresh evidence.';
      case 'enterprise':
        return 'The future login action will hand off through the enterprise Cloud flow. Until a Cloud-backed identity receipt exists, this step remains blocked.';
      case 'device':
        return 'Pairing must produce a device-bound Connector identity. A UI click alone is not accepted as proof.';
      case 'runtime':
        return `Current runtime: ${snapshot.runtimeVersion}. Core and Agent Plugin must both report healthy before onboarding can continue.`;
      case 'provider':
        return 'Provider configuration is intentionally local-first. No API key is considered configured until the native secret-store path confirms it.';
      case 'ready':
        return 'This machine can enter the Runtime Cockpit without hiding an incomplete dependency.';
    }
  }

  function activeIcon() {
    return currentStep();
  }
</script>

<div class="onboarding-shell">
  <header class="onboarding-top" data-tauri-drag-region>
    <div class="onboarding-brand">
      <div class="onboarding-mark" aria-hidden="true"><span></span></div>
      <div>
        <strong>Hermes</strong>
        <span>Managed Runtime</span>
      </div>
    </div>
    <div class="onboarding-security"><ShieldCheck size={14} /> Local execution authority</div>
  </header>

  <main class="onboarding-main">
    <aside class="onboarding-rail">
      <div class="rail-intro">
        <span>First run</span>
        <h2>Set up Hermes</h2>
        <p>Each step advances only when the native runtime provides evidence.</p>
      </div>

      <div class="onboarding-steps">
        {#each steps as step}
          <div class:current={stepState(step.key) === 'current'} class:complete={stepState(step.key) === 'complete'} class="onboarding-step">
            <div class="step-glyph">
              {#if stepState(step.key) === 'complete'}
                <Check size={13} strokeWidth={2.2} />
              {:else if stepState(step.key) === 'current'}
                <CircleDot size={13} strokeWidth={2} />
              {:else}
                <span></span>
              {/if}
            </div>
            <div class="step-copy">
              <span>{step.eyebrow}</span>
              <strong>{step.title}</strong>
            </div>
          </div>
        {/each}
      </div>

      <div class="rail-device">
        <Laptop size={15} />
        <div><strong>{snapshot.deviceName}</strong><span>{snapshot.platform} · {snapshot.architecture}</span></div>
      </div>
    </aside>

    <section class="onboarding-stage">
      <div class="stage-halo" aria-hidden="true"></div>
      <div class="stage-content">
        <div class="stage-eyebrow">
          <span class:ready={completelyReady()}></span>
          {completelyReady() ? 'All gates verified' : `Step ${steps.findIndex((step) => step.key === currentStep()) + 1} of ${steps.length}`}
        </div>

        <div class="stage-icon">
          {#if activeIcon() === 'foundation'}<Cpu size={26} strokeWidth={1.55} />{/if}
          {#if activeIcon() === 'enterprise'}<Cloud size={26} strokeWidth={1.55} />{/if}
          {#if activeIcon() === 'device'}<Cable size={26} strokeWidth={1.55} />{/if}
          {#if activeIcon() === 'runtime'}<ShieldCheck size={26} strokeWidth={1.55} />{/if}
          {#if activeIcon() === 'provider'}<Sparkles size={26} strokeWidth={1.55} />{/if}
          {#if activeIcon() === 'ready'}<Check size={26} strokeWidth={1.7} />{/if}
        </div>

        <h1>{activeTitle()}</h1>
        <p class="stage-description">{activeDescription()}</p>

        <div class="evidence-card">
          <div class="evidence-heading">
            <div>
              <span>Current evidence gate</span>
              <strong>{blockerTitle()}</strong>
            </div>
            <div class:verified={completelyReady()} class="evidence-status">
              {#if completelyReady()}<Check size={13} /> Verified{:else}<CircleDot size={13} /> Waiting{/if}
            </div>
          </div>
          <p>{blockerDetail()}</p>

          <div class="evidence-grid">
            <div><span>Runtime Manager</span><strong class:good={managerConnected()}>{managerConnected() ? 'Connected' : 'Missing'}</strong></div>
            <div><span>Cloud</span><strong class:good={snapshot.cloudConnected}>{snapshot.cloudConnected ? 'Connected' : 'Offline'}</strong></div>
            <div><span>Device</span><strong class:good={devicePaired()}>{devicePaired() ? 'Paired' : 'Pending'}</strong></div>
            <div><span>Runtime</span><strong class:good={localAuthorityReady()}>{localAuthorityReady() ? 'Healthy' : snapshot.runtimeVersion}</strong></div>
            <div><span>Provider</span><strong class:good={providerConfigured()}>{providerConfigured() ? 'Connected' : 'Not configured'}</strong></div>
          </div>
        </div>

        <div class="stage-actions">
          <button class="refresh-evidence" on:click={onRefresh} disabled={refreshing}>
            <span class:spin={refreshing}><RefreshCw size={15} /></span>
            {refreshing ? 'Checking…' : 'Refresh evidence'}
          </button>
          <button class="next-action" disabled={!completelyReady()}>
            Open Runtime Cockpit <ArrowRight size={15} />
          </button>
        </div>

        <div class="privacy-line"><KeyRound size={13} /> Provider credentials stay in this computer’s native secret store.</div>
      </div>
    </section>
  </main>
</div>

<style>
  .onboarding-shell {
    width: 100%; height: 100%; min-height: 680px; display: flex; flex-direction: column;
    background: radial-gradient(circle at 74% 12%, rgba(68, 167, 135, .10), transparent 27%), linear-gradient(180deg, #081210, #07100f 62%, #06100e);
    color: var(--text); overflow: hidden;
  }
  .onboarding-top { height: 56px; flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between; padding: 0 25px; border-bottom: 1px solid var(--line); }
  .onboarding-brand { display: flex; align-items: center; gap: 10px; }
  .onboarding-brand strong { display: block; font-size: 13px; font-weight: 720; }
  .onboarding-brand > div:last-child span { display: block; margin-top: 2px; color: var(--muted-2); font-size: 8.5px; letter-spacing: .075em; text-transform: uppercase; }
  .onboarding-mark { position: relative; width: 26px; height: 26px; border: 1px solid rgba(111,227,189,.18); border-radius: 50%; transform: rotate(-22deg) scaleY(.72); }
  .onboarding-mark::after { content: ''; position: absolute; inset: 4px; border: 1px solid rgba(117,168,255,.18); border-radius: 50%; transform: rotate(68deg) scaleY(.68); }
  .onboarding-mark span { position: absolute; z-index: 2; left: 8px; top: 8px; width: 8px; height: 8px; border-radius: 50%; background: var(--accent-strong); box-shadow: 0 0 18px rgba(111,227,189,.24); transform: scaleY(1.38); }
  .onboarding-security { display: flex; align-items: center; gap: 6px; color: #6e9186; font-size: 9.5px; }

  .onboarding-main { min-height: 0; flex: 1; display: grid; grid-template-columns: 282px minmax(0, 1fr); }
  .onboarding-rail { display: flex; flex-direction: column; padding: 46px 26px 24px; border-right: 1px solid var(--line); background: rgba(8, 19, 17, .6); }
  .rail-intro > span { color: #648079; font-size: 8.5px; font-weight: 700; letter-spacing: .13em; text-transform: uppercase; }
  .rail-intro h2 { margin: 7px 0 0; font-size: 21px; letter-spacing: -.035em; font-weight: 630; }
  .rail-intro p { margin: 8px 0 0; color: #60756f; font-size: 9.5px; line-height: 1.65; }
  .onboarding-steps { display: grid; gap: 2px; margin-top: 34px; }
  .onboarding-step { position: relative; display: flex; align-items: center; gap: 11px; min-height: 51px; padding: 7px 8px; border-radius: 10px; color: #536a64; }
  .onboarding-step::before { content: ''; position: absolute; left: 20px; top: -6px; width: 1px; height: 12px; background: rgba(212,238,231,.07); }
  .onboarding-step:first-child::before { display: none; }
  .onboarding-step.current { color: #b9cdc7; background: rgba(111,227,189,.045); box-shadow: inset 0 0 0 1px rgba(111,227,189,.06); }
  .onboarding-step.complete { color: #789b90; }
  .step-glyph { z-index: 1; display: grid; place-items: center; width: 25px; height: 25px; flex: 0 0 auto; border: 1px solid rgba(212,238,231,.08); border-radius: 8px; background: #0b1715; color: #526b64; }
  .onboarding-step.current .step-glyph { color: var(--accent); border-color: rgba(111,227,189,.2); box-shadow: 0 0 20px rgba(111,227,189,.04); }
  .onboarding-step.complete .step-glyph { color: #6eb89f; }
  .step-glyph > span { width: 4px; height: 4px; border-radius: 50%; background: #3b504a; }
  .step-copy span { display: block; margin-bottom: 3px; color: #405650; font-size: 7.5px; font-weight: 700; letter-spacing: .08em; }
  .step-copy strong { display: block; font-size: 10.3px; font-weight: 620; }
  .onboarding-step.current .step-copy span { color: #6a9387; }
  .rail-device { display: flex; gap: 9px; align-items: center; margin-top: auto; padding: 12px; border: 1px solid var(--line); border-radius: 11px; color: #6f8b83; background: rgba(255,255,255,.012); }
  .rail-device > div { min-width: 0; }
  .rail-device strong { display: block; overflow: hidden; color: #a8bdb7; font-size: 9.5px; white-space: nowrap; text-overflow: ellipsis; }
  .rail-device span { display: block; margin-top: 3px; color: #526962; font-size: 8.3px; }

  .onboarding-stage { position: relative; min-width: 0; overflow: auto; display: grid; place-items: center; padding: 52px 7vw 64px; }
  .stage-halo { position: absolute; width: 600px; height: 600px; border-radius: 50%; background: radial-gradient(circle, rgba(74,179,143,.055), transparent 64%); filter: blur(2px); pointer-events: none; }
  .stage-content { position: relative; z-index: 1; width: min(720px, 100%); }
  .stage-eyebrow { display: flex; align-items: center; gap: 8px; color: #6c857e; font-size: 9px; font-weight: 690; letter-spacing: .09em; text-transform: uppercase; }
  .stage-eyebrow > span { width: 6px; height: 6px; border-radius: 50%; background: var(--warning); box-shadow: 0 0 12px rgba(242,199,119,.18); }
  .stage-eyebrow > span.ready { background: var(--accent); box-shadow: 0 0 12px rgba(111,227,189,.3); }
  .stage-icon { display: grid; place-items: center; width: 52px; height: 52px; margin: 22px 0 20px; border: 1px solid rgba(111,227,189,.13); border-radius: 15px; color: #80cdb3; background: linear-gradient(180deg, rgba(111,227,189,.065), rgba(111,227,189,.025)); box-shadow: 0 18px 48px rgba(0,0,0,.12); }
  .stage-content h1 { max-width: 660px; font-size: clamp(31px, 4vw, 48px); font-weight: 610; letter-spacing: -.052em; }
  .stage-description { max-width: 640px; margin: 13px 0 0; color: #718780; font-size: 11.5px; line-height: 1.75; }

  .evidence-card { margin-top: 30px; padding: 18px; border: 1px solid var(--line); border-radius: 14px; background: linear-gradient(180deg, rgba(15,29,26,.76), rgba(10,21,19,.76)); box-shadow: 0 22px 70px rgba(0,0,0,.12); }
  .evidence-heading { display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; }
  .evidence-heading > div:first-child span { display: block; margin-bottom: 5px; color: #526a63; font-size: 8px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
  .evidence-heading strong { color: #dce9e5; font-size: 11.5px; font-weight: 650; }
  .evidence-status { display: inline-flex; align-items: center; gap: 5px; padding: 5px 8px; border: 1px solid rgba(242,199,119,.12); border-radius: 999px; color: #b89b61; background: rgba(242,199,119,.035); font-size: 8.5px; font-weight: 620; }
  .evidence-status.verified { color: #72bda4; border-color: rgba(111,227,189,.13); background: rgba(111,227,189,.04); }
  .evidence-card > p { margin: 10px 0 16px; max-width: 620px; color: #657a74; font-size: 9.7px; line-height: 1.65; }
  .evidence-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); border: 1px solid rgba(212,238,231,.065); border-radius: 10px; overflow: hidden; }
  .evidence-grid > div { min-width: 0; padding: 10px 11px; border-right: 1px solid rgba(212,238,231,.06); background: rgba(255,255,255,.01); }
  .evidence-grid > div:last-child { border-right: 0; }
  .evidence-grid span { display: block; margin-bottom: 5px; color: #4f645e; font-size: 7.5px; font-weight: 660; text-transform: uppercase; letter-spacing: .055em; }
  .evidence-grid strong { display: block; overflow: hidden; color: #a08359; font-size: 8.7px; font-weight: 620; white-space: nowrap; text-overflow: ellipsis; }
  .evidence-grid strong.good { color: #75bca4; }

  .stage-actions { display: flex; gap: 9px; margin-top: 17px; }
  .stage-actions button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; height: 36px; padding: 0 14px; border-radius: 9px; font-size: 9.7px; font-weight: 660; cursor: pointer; }
  .refresh-evidence { border: 1px solid var(--line); color: #9db2ac; background: rgba(255,255,255,.022); }
  .refresh-evidence:hover:not(:disabled) { border-color: var(--line-strong); color: var(--text); }
  .next-action { border: 1px solid rgba(111,227,189,.3); color: #07110e; background: linear-gradient(180deg, #82e6c4, #6cd3af); }
  .next-action:disabled { cursor: not-allowed; opacity: .22; filter: saturate(.3); }
  button:disabled { cursor: default; }
  .privacy-line { display: flex; align-items: center; gap: 6px; margin-top: 16px; color: #536b64; font-size: 8.5px; }
  .spin { display: inline-grid; animation: onboarding-spin .75s linear infinite; }
  @keyframes onboarding-spin { to { transform: rotate(360deg); } }

  @media (max-width: 1080px) {
    .onboarding-main { grid-template-columns: 250px minmax(0, 1fr); }
    .onboarding-stage { padding-left: 5vw; padding-right: 5vw; }
    .evidence-grid { grid-template-columns: repeat(3, 1fr); }
    .evidence-grid > div:nth-child(3) { border-right: 0; }
    .evidence-grid > div:nth-child(n+4) { border-top: 1px solid rgba(212,238,231,.06); }
  }
</style>
