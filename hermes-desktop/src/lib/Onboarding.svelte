<script lang="ts">
  import {
    ArrowRight,
    Cable,
    Check,
    CircleDot,
    Cloud,
    Cpu,
    ExternalLink,
    KeyRound,
    Laptop,
    RefreshCw,
    ShieldCheck,
    Sparkles,
  } from 'lucide-svelte';
  import type { RuntimeSnapshot } from './types';

  export let snapshot: RuntimeSnapshot;
  export let refreshing = false;
  export let workspaceConnecting = false;
  export let workspaceError = '';
  export let onRefresh: () => void;
  export let onConnectWorkspace: (endpoint: string) => void;

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

  let workspaceEndpoint = snapshot.workspaceEndpoint ?? '';
  $: if (!workspaceEndpoint && snapshot.workspaceEndpoint) workspaceEndpoint = snapshot.workspaceEndpoint;

  function componentHealthy(id: string) {
    return snapshot.components.some((component) => component.id === id && component.state === 'healthy');
  }

  function managerConnected() {
    return snapshot.runtimeGeneration !== 'manager-not-connected' && snapshot.runtimeVersion !== 'not-connected';
  }

  function workspaceConnected() {
    return snapshot.workspaceAuthenticated;
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
      && workspaceConnected()
      && snapshot.cloudConnected
      && devicePaired()
      && localAuthorityReady()
      && providerConfigured();
  }

  function currentStep(): StepKey {
    if (!managerConnected()) return 'foundation';
    if (!workspaceConnected()) return 'enterprise';
    if (!devicePaired()) return 'device';
    if (!localAuthorityReady()) return 'runtime';
    if (!providerConfigured()) return 'provider';
    return 'ready';
  }

  function stepComplete(key: StepKey) {
    switch (key) {
      case 'foundation': return managerConnected();
      case 'enterprise': return workspaceConnected();
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
      case 'runtime': return 'Prepare the local Managed Runtime.';
      case 'provider': return 'Connect a model service locally.';
      case 'ready': return 'Hermes is ready to work.';
    }
  }

  function activeDescription() {
    switch (currentStep()) {
      case 'foundation':
        return 'Hermes Desktop is open, but the local Runtime Manager has not provided a verified authority snapshot yet.';
      case 'enterprise':
        return 'Sign in through your browser. Hermes Desktop uses PKCE and a loopback callback; workspace credentials are stored only in the native Keychain.';
      case 'device':
        return 'Your enterprise identity is verified. The next gate is a device-bound pairing identity for this Mac.';
      case 'runtime':
        return 'The device is paired. Hermes now requires the content-addressed private runtime, Core and Agent Plugin to become healthy.';
      case 'provider':
        return 'Local execution is healthy. Add at least one model provider; its credential must remain in the platform secret store on this host.';
      case 'ready':
        return 'Every onboarding gate has evidence: workspace identity, local authority, Cloud transport, device identity, Managed Runtime and model service.';
    }
  }

  function blockerTitle() {
    switch (currentStep()) {
      case 'foundation': return 'Runtime Manager evidence missing';
      case 'enterprise': return 'Workspace authentication required';
      case 'device': return 'Device pairing not established';
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
        return 'Enter the HTTPS address of your Hermes Cloud and complete the secure browser sign-in. No access token is copied into the Desktop UI.';
      case 'device':
        return 'Pairing must produce a device-bound identity. A UI click alone is not accepted as proof; this is the next product gate we will close.';
      case 'runtime':
        return `Current runtime: ${snapshot.runtimeVersion}. Core and Agent Plugin must both report healthy before onboarding can continue.`;
      case 'provider':
        return 'Provider configuration is local-first. No API key is considered configured until the native secret-store path confirms it.';
      case 'ready':
        return 'This machine can enter the Runtime Cockpit without hiding an incomplete dependency.';
    }
  }
</script>

<div class="onboarding-shell">
  <header class="top" data-tauri-drag-region>
    <div class="brand">
      <div class="mark" aria-hidden="true"><span></span></div>
      <div><strong>Hermes</strong><span>Managed Runtime</span></div>
    </div>
    <div class="security"><ShieldCheck size={14} /> Local execution authority</div>
  </header>

  <main class="main">
    <aside class="rail">
      <div class="rail-intro">
        <span>First run</span>
        <h2>Set up Hermes</h2>
        <p>Each step advances only when native evidence exists.</p>
      </div>

      <div class="steps">
        {#each steps as step}
          <div class="step" class:current={stepState(step.key) === 'current'} class:complete={stepState(step.key) === 'complete'}>
            <div class="step-glyph">
              {#if stepState(step.key) === 'complete'}
                <Check size={13} strokeWidth={2.2} />
              {:else if stepState(step.key) === 'current'}
                <CircleDot size={13} strokeWidth={2} />
              {:else}<span></span>{/if}
            </div>
            <div class="step-copy"><span>{step.eyebrow}</span><strong>{step.title}</strong></div>
          </div>
        {/each}
      </div>

      <div class="device">
        <Laptop size={15} />
        <div><strong>{snapshot.deviceName}</strong><span>{snapshot.platform} · {snapshot.architecture}</span></div>
      </div>
    </aside>

    <section class="stage">
      <div class="halo" aria-hidden="true"></div>
      <div class="content">
        <div class="eyebrow"><span class:ready={completelyReady()}></span>{completelyReady() ? 'All gates verified' : `Step ${steps.findIndex((step) => step.key === currentStep()) + 1} of ${steps.length}`}</div>

        <div class="stage-icon">
          {#if currentStep() === 'foundation'}<Cpu size={26} strokeWidth={1.55} />{/if}
          {#if currentStep() === 'enterprise'}<Cloud size={26} strokeWidth={1.55} />{/if}
          {#if currentStep() === 'device'}<Cable size={26} strokeWidth={1.55} />{/if}
          {#if currentStep() === 'runtime'}<ShieldCheck size={26} strokeWidth={1.55} />{/if}
          {#if currentStep() === 'provider'}<Sparkles size={26} strokeWidth={1.55} />{/if}
          {#if currentStep() === 'ready'}<Check size={26} strokeWidth={1.7} />{/if}
        </div>

        <h1>{activeTitle()}</h1>
        <p class="description">{activeDescription()}</p>

        <div class="evidence-card">
          <div class="evidence-heading">
            <div><span>Current evidence gate</span><strong>{blockerTitle()}</strong></div>
            <div class="evidence-status" class:verified={stepComplete(currentStep())}>
              {#if stepComplete(currentStep())}<Check size={13} /> Verified{:else}<CircleDot size={13} /> Waiting{/if}
            </div>
          </div>
          <p>{blockerDetail()}</p>

          {#if currentStep() === 'enterprise'}
            <div class="workspace-form">
              <label for="workspace-endpoint">Hermes Cloud address</label>
              <div class="workspace-row">
                <input id="workspace-endpoint" bind:value={workspaceEndpoint} placeholder="https://hermes.example.com/" autocomplete="url" spellcheck="false" />
                <button class="connect" on:click={() => onConnectWorkspace(workspaceEndpoint)} disabled={workspaceConnecting || !workspaceEndpoint.trim()}>
                  {#if workspaceConnecting}<span class="spinner"></span> Waiting for browser…{:else}<ExternalLink size={14} /> Sign in with browser{/if}
                </button>
              </div>
              {#if workspaceError}<div class="workspace-error">{workspaceError}</div>{/if}
              <div class="workspace-note"><KeyRound size={12} /> PKCE sign-in · loopback callback · credentials stored in macOS Keychain</div>
            </div>
          {/if}

          <div class="grid">
            <div><span>Runtime Manager</span><strong class:good={managerConnected()}>{managerConnected() ? 'Connected' : 'Missing'}</strong></div>
            <div><span>Workspace</span><strong class:good={workspaceConnected()}>{workspaceConnected() ? (snapshot.workspaceUser ?? 'Authenticated') : 'Sign in'}</strong></div>
            <div><span>Device</span><strong class:good={devicePaired()}>{devicePaired() ? 'Paired' : 'Pending'}</strong></div>
            <div><span>Runtime</span><strong class:good={localAuthorityReady()}>{localAuthorityReady() ? 'Healthy' : snapshot.runtimeVersion}</strong></div>
            <div><span>Provider</span><strong class:good={providerConfigured()}>{providerConfigured() ? 'Connected' : 'Not configured'}</strong></div>
          </div>
        </div>

        <div class="actions">
          <button class="refresh" on:click={onRefresh} disabled={refreshing}>
            <span class:spin={refreshing}><RefreshCw size={15} /></span>{refreshing ? 'Checking…' : 'Refresh evidence'}
          </button>
          <button class="cockpit" disabled={!completelyReady()}>Open Runtime Cockpit <ArrowRight size={15} /></button>
        </div>
        <div class="privacy"><KeyRound size={13} /> Secrets stay in this computer’s native secret store.</div>
      </div>
    </section>
  </main>
</div>

<style>
  .onboarding-shell { width:100%; height:100%; min-height:680px; display:flex; flex-direction:column; color:var(--text); background:radial-gradient(circle at 74% 12%,rgba(68,167,135,.10),transparent 27%),linear-gradient(180deg,#081210,#07100f 62%,#06100e); overflow:hidden; }
  .top { height:56px; flex:0 0 auto; display:flex; align-items:center; justify-content:space-between; padding:0 25px; border-bottom:1px solid var(--line); }
  .brand { display:flex; align-items:center; gap:10px; } .brand strong{display:block;font-size:13px;font-weight:720}.brand>div:last-child span{display:block;margin-top:2px;color:var(--muted-2);font-size:8.5px;letter-spacing:.075em;text-transform:uppercase}
  .mark{position:relative;width:26px;height:26px;border:1px solid rgba(111,227,189,.18);border-radius:50%;transform:rotate(-22deg) scaleY(.72)}.mark::after{content:'';position:absolute;inset:4px;border:1px solid rgba(117,168,255,.18);border-radius:50%;transform:rotate(68deg) scaleY(.68)}.mark span{position:absolute;z-index:2;left:8px;top:8px;width:8px;height:8px;border-radius:50%;background:var(--accent-strong);box-shadow:0 0 18px rgba(111,227,189,.24);transform:scaleY(1.38)}
  .security{display:flex;align-items:center;gap:6px;color:#6e9186;font-size:9.5px}.main{min-height:0;flex:1;display:grid;grid-template-columns:282px minmax(0,1fr)}
  .rail{display:flex;flex-direction:column;padding:46px 26px 24px;border-right:1px solid var(--line);background:rgba(8,19,17,.6)}.rail-intro>span{color:#648079;font-size:8.5px;font-weight:700;letter-spacing:.13em;text-transform:uppercase}.rail-intro h2{margin:7px 0 0;font-size:21px;letter-spacing:-.035em;font-weight:630}.rail-intro p{margin:8px 0 0;color:#60756f;font-size:9.5px;line-height:1.65}
  .steps{display:grid;gap:2px;margin-top:34px}.step{position:relative;display:flex;align-items:center;gap:11px;min-height:51px;padding:7px 8px;border-radius:10px;color:#536a64}.step::before{content:'';position:absolute;left:20px;top:-6px;width:1px;height:12px;background:rgba(212,238,231,.07)}.step:first-child::before{display:none}.step.current{color:#b9cdc7;background:rgba(111,227,189,.045);box-shadow:inset 0 0 0 1px rgba(111,227,189,.06)}.step.complete{color:#789b90}
  .step-glyph{z-index:1;display:grid;place-items:center;width:25px;height:25px;flex:0 0 auto;border:1px solid rgba(212,238,231,.08);border-radius:8px;background:#0b1715;color:#526b64}.step.current .step-glyph{color:var(--accent);border-color:rgba(111,227,189,.2)}.step.complete .step-glyph{color:#6eb89f}.step-glyph>span{width:4px;height:4px;border-radius:50%;background:#3b504a}.step-copy span{display:block;margin-bottom:3px;color:#405650;font-size:7.5px;font-weight:700;letter-spacing:.08em}.step-copy strong{display:block;font-size:10.3px;font-weight:620}
  .device{margin-top:auto;display:flex;align-items:center;gap:10px;padding:13px;border:1px solid rgba(212,238,231,.08);border-radius:10px;color:#708c84}.device strong{display:block;font-size:9.5px}.device span{display:block;margin-top:2px;font-size:8px;color:#516961}
  .stage{position:relative;display:grid;place-items:center;overflow:auto}.halo{position:absolute;width:720px;height:520px;border-radius:50%;background:radial-gradient(circle,rgba(40,126,104,.08),transparent 66%);filter:blur(12px);pointer-events:none}.content{position:relative;z-index:1;width:min(760px,calc(100% - 80px));padding:54px 0 48px}.eyebrow{display:flex;align-items:center;gap:8px;color:#69827b;font-size:8.5px;font-weight:700;letter-spacing:.14em;text-transform:uppercase}.eyebrow>span{width:7px;height:7px;border-radius:50%;background:#e4ad55;box-shadow:0 0 12px rgba(228,173,85,.22)}.eyebrow>span.ready{background:#6fe3bd}
  .stage-icon{display:grid;place-items:center;width:48px;height:48px;margin-top:27px;border:1px solid rgba(111,227,189,.16);border-radius:14px;background:rgba(9,30,25,.72);color:#72cbb0}.content h1{max-width:720px;margin:24px 0 0;color:#e8f1ee;font-size:42px;line-height:1.04;letter-spacing:-.055em;font-weight:620}.description{max-width:720px;margin:15px 0 0;color:#718981;font-size:11.5px;line-height:1.75}
  .evidence-card{margin-top:31px;padding:18px;border:1px solid rgba(212,238,231,.10);border-radius:14px;background:rgba(8,23,19,.72);box-shadow:0 18px 55px rgba(0,0,0,.12)}.evidence-heading{display:flex;align-items:center;justify-content:space-between}.evidence-heading>div:first-child span{display:block;color:#526f66;font-size:7.5px;font-weight:700;letter-spacing:.12em;text-transform:uppercase}.evidence-heading strong{display:block;margin-top:6px;font-size:11px;color:#d4dfdc}.evidence-status{display:flex;align-items:center;gap:5px;padding:6px 9px;border:1px solid rgba(210,165,75,.18);border-radius:999px;color:#bc9550;font-size:8px;font-weight:700}.evidence-status.verified{color:#67b89d;border-color:rgba(103,184,157,.2)}.evidence-card>p{margin:11px 0 0;color:#5f766f;font-size:9.5px;line-height:1.6}
  .workspace-form{margin-top:16px;padding:13px;border:1px solid rgba(111,227,189,.09);border-radius:11px;background:rgba(6,18,15,.52)}.workspace-form label{display:block;margin-bottom:7px;color:#6d8a81;font-size:8px;font-weight:700;letter-spacing:.06em}.workspace-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px}.workspace-row input{min-width:0;height:36px;padding:0 11px;border:1px solid rgba(212,238,231,.11);border-radius:8px;outline:none;background:#071310;color:#cbd8d4;font-size:10px}.workspace-row input:focus{border-color:rgba(111,227,189,.35);box-shadow:0 0 0 3px rgba(111,227,189,.04)}.connect{display:flex;align-items:center;gap:7px;height:36px;padding:0 14px;border:1px solid rgba(111,227,189,.18);border-radius:8px;background:#15362d;color:#bfe5d8;font-size:9px;font-weight:650;cursor:pointer}.connect:disabled{opacity:.45;cursor:not-allowed}.spinner{width:11px;height:11px;border:1px solid currentColor;border-right-color:transparent;border-radius:50%;animation:spin .8s linear infinite}.workspace-error{margin-top:8px;color:#d49278;font-size:8.8px;line-height:1.45}.workspace-note{display:flex;align-items:center;gap:6px;margin-top:9px;color:#506b63;font-size:8px}
  .grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));margin-top:15px;border:1px solid rgba(212,238,231,.07);border-radius:9px;overflow:hidden}.grid>div{min-width:0;padding:9px 11px;border-right:1px solid rgba(212,238,231,.06)}.grid>div:last-child{border-right:0}.grid span{display:block;color:#476158;font-size:6.7px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}.grid strong{display:block;margin-top:5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#a98749;font-size:8px}.grid strong.good{color:#68b99e}
  .actions{display:flex;gap:8px;margin-top:13px}.actions button{display:flex;align-items:center;gap:7px;height:36px;padding:0 13px;border-radius:8px;font-size:9px;font-weight:630}.refresh{border:1px solid rgba(212,238,231,.10);background:#091714;color:#87a19a;cursor:pointer}.cockpit{border:0;background:#91aaa2;color:#12201c}.cockpit:disabled{opacity:.42}.privacy{display:flex;align-items:center;gap:6px;margin-top:11px;color:#4d665e;font-size:8px}.spin{display:inline-flex;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
  @media(max-width:900px){.main{grid-template-columns:220px minmax(0,1fr)}.rail{padding-left:18px;padding-right:18px}.content{width:calc(100% - 42px)}.content h1{font-size:34px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.workspace-row{grid-template-columns:1fr}.connect{justify-content:center}}
  @media(prefers-reduced-motion:reduce){.spin,.spinner{animation:none}}
</style>
