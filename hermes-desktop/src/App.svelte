<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import {
    Activity,
    ArrowUpRight,
    Bot,
    Cable,
    Check,
    ChevronRight,
    CircleDot,
    Cloud,
    Cpu,
    Download,
    Gauge,
    KeyRound,
    MessagesSquare,
    RefreshCw,
    Search,
    Settings2,
    ShieldCheck,
    Sparkles,
    Stethoscope,
    Zap,
  } from 'lucide-svelte';
  import { mockRuntimeSnapshot } from './lib/mock-runtime';
  import type { NavigationKey, RuntimeSnapshot } from './lib/types';

  let snapshot: RuntimeSnapshot = mockRuntimeSnapshot;
  let selected: NavigationKey = 'overview';
  let nativeSource = false;
  let refreshing = false;

  const navigation: Array<{ key: NavigationKey; label: string }> = [
    { key: 'overview', label: 'Overview' },
    { key: 'agents', label: 'Agent' },
    { key: 'sessions', label: 'Sessions' },
    { key: 'providers', label: 'Models' },
    { key: 'updates', label: 'Updates' },
    { key: 'diagnostics', label: 'Diagnostics' },
  ];

  onMount(async () => {
    try {
      snapshot = await invoke<RuntimeSnapshot>('runtime_snapshot');
      nativeSource = true;
    } catch {
      nativeSource = false;
    }
  });

  async function refreshSnapshot() {
    if (refreshing) return;
    refreshing = true;
    try {
      snapshot = await invoke<RuntimeSnapshot>('runtime_snapshot');
      nativeSource = true;
    } catch {
      snapshot = { ...snapshot, lastChecked: 'just now' };
    } finally {
      window.setTimeout(() => (refreshing = false), 420);
    }
  }

  function iconFor(key: NavigationKey) {
    return key;
  }

  const stateLabel = (state: string) => {
    if (state === 'healthy' || state === 'connected') return 'Healthy';
    if (state === 'not-configured') return 'Not configured';
    return state.charAt(0).toUpperCase() + state.slice(1);
  };
</script>

<div class="app-shell">
  <aside class="sidebar">
    <div class="window-grab" data-tauri-drag-region></div>

    <div class="brand-row">
      <div class="brand-mark" aria-hidden="true">
        <span class="brand-core"></span>
        <span class="brand-orbit orbit-one"></span>
        <span class="brand-orbit orbit-two"></span>
      </div>
      <div>
        <div class="brand-name">Hermes</div>
        <div class="brand-caption">Managed Runtime</div>
      </div>
    </div>

    <nav class="primary-nav" aria-label="Primary navigation">
      {#each navigation as item}
        <button
          class:active={selected === item.key}
          class="nav-item"
          on:click={() => (selected = item.key)}
          aria-current={selected === item.key ? 'page' : undefined}
        >
          <span class="nav-icon">
            {#if iconFor(item.key) === 'overview'}<Gauge size={17} strokeWidth={1.8} />{/if}
            {#if iconFor(item.key) === 'agents'}<Bot size={17} strokeWidth={1.8} />{/if}
            {#if iconFor(item.key) === 'sessions'}<MessagesSquare size={17} strokeWidth={1.8} />{/if}
            {#if iconFor(item.key) === 'providers'}<Sparkles size={17} strokeWidth={1.8} />{/if}
            {#if iconFor(item.key) === 'updates'}<RefreshCw size={17} strokeWidth={1.8} />{/if}
            {#if iconFor(item.key) === 'diagnostics'}<Stethoscope size={17} strokeWidth={1.8} />{/if}
          </span>
          <span>{item.label}</span>
          {#if item.key === 'updates' && snapshot.updateAvailable}<span class="nav-dot"></span>{/if}
        </button>
      {/each}
    </nav>

    <div class="sidebar-spacer"></div>

    <div class="device-card">
      <div class="device-avatar"><Cpu size={16} strokeWidth={1.8} /></div>
      <div class="device-copy">
        <strong>{snapshot.deviceName}</strong>
        <span>{snapshot.profileName} · {snapshot.architecture}</span>
      </div>
      <button class="icon-button mini" aria-label="Device settings"><Settings2 size={15} /></button>
    </div>

    <div class="sidebar-footer">
      <span class="secure-dot"></span>
      <span>Local authority protected</span>
    </div>
  </aside>

  <main class="main-area">
    <header class="topbar" data-tauri-drag-region>
      <div class="topbar-spacer"></div>
      <button class="search-control" aria-label="Search Hermes">
        <Search size={15} strokeWidth={1.9} />
        <span>Search</span>
        <kbd>⌘ K</kbd>
      </button>
      <div class="source-pill" title="Runtime data source">
        <span class:native={nativeSource} class="source-dot"></span>
        {nativeSource ? 'Live runtime' : 'Design preview'}
      </div>
      <button class="icon-button" on:click={refreshSnapshot} aria-label="Refresh runtime status">
        <RefreshCw size={16} class:spin={refreshing} />
      </button>
    </header>

    <div class="content-frame">
      {#if selected === 'overview'}
        <section class="hero-row">
          <div>
            <div class="hero-state">
              <span class="hero-state-dot"></span>
              Runtime ready
            </div>
            <h1>Your Agent is ready to work.</h1>
            <p>Execution stays on this computer. Cloud access is connected through the managed Connector.</p>
          </div>
          <div class="hero-actions">
            <button class="button secondary"><Activity size={16} /> View activity</button>
            <button class="button primary">Open Agent <ArrowUpRight size={16} /></button>
          </div>
        </section>

        <section class="signal-strip" aria-label="Runtime summary">
          <div class="signal-cell">
            <span class="signal-label">Agent</span>
            <div class="signal-value healthy"><span></span>{snapshot.agentReady ? 'Ready' : 'Unavailable'}</div>
          </div>
          <div class="signal-cell">
            <span class="signal-label">Cloud</span>
            <div class="signal-value healthy"><span></span>{snapshot.cloudConnected ? 'Secure' : 'Offline'}</div>
          </div>
          <div class="signal-cell">
            <span class="signal-label">Active sessions</span>
            <div class="signal-number">{snapshot.activeSessions}</div>
          </div>
          <div class="signal-cell">
            <span class="signal-label">Running tasks</span>
            <div class="signal-number">{snapshot.runningTasks}</div>
          </div>
          <div class="signal-cell wide">
            <span class="signal-label">Managed Runtime</span>
            <div class="runtime-version">{snapshot.runtimeVersion}<span>· {snapshot.lastChecked}</span></div>
          </div>
        </section>

        <div class="overview-grid">
          <section class="runtime-surface panel">
            <div class="panel-heading">
              <div>
                <h2>Runtime path</h2>
                <p>One verified authority chain from Cloud to local execution.</p>
              </div>
              <div class="verified-mark"><ShieldCheck size={16} /> Verified</div>
            </div>

            <div class="topology">
              {#each snapshot.components as component, index}
                <div class="topology-node">
                  <div class="node-icon">
                    {#if component.id === 'cloud'}<Cloud size={20} strokeWidth={1.7} />{/if}
                    {#if component.id === 'connector'}<Cable size={20} strokeWidth={1.7} />{/if}
                    {#if component.id === 'plugin'}<Zap size={20} strokeWidth={1.7} />{/if}
                    {#if component.id === 'core'}<Cpu size={20} strokeWidth={1.7} />{/if}
                  </div>
                  <div class="node-copy">
                    <div class="node-title-row">
                      <strong>{component.name}</strong>
                      <span class="node-status"><span></span>{stateLabel(component.state)}</span>
                    </div>
                    <p>{component.detail}</p>
                    {#if component.latencyMs !== undefined}<small>{component.latencyMs} ms</small>{/if}
                  </div>
                </div>
                {#if index < snapshot.components.length - 1}
                  <div class="topology-link"><span></span></div>
                {/if}
              {/each}
            </div>

            <div class="authority-bar">
              <div><KeyRound size={15} /><span>Runtime generation</span></div>
              <code>{snapshot.runtimeGeneration}</code>
              <div class="authority-note">Credentials remain on this host</div>
            </div>
          </section>

          <aside class="activity-rail panel">
            <div class="panel-heading compact">
              <div>
                <h2>Recent activity</h2>
                <p>Runtime and transport events.</p>
              </div>
              <button class="text-action">All events</button>
            </div>
            <div class="event-list">
              {#each snapshot.events as event}
                <div class="event-row">
                  <div class:success={event.tone === 'success'} class:attention={event.tone === 'attention'} class="event-marker"></div>
                  <div class="event-copy">
                    <div class="event-title"><strong>{event.title}</strong><time>{event.at}</time></div>
                    <p>{event.detail}</p>
                  </div>
                </div>
              {/each}
            </div>
          </aside>
        </div>

        <div class="lower-grid">
          <section class="provider-section panel">
            <div class="panel-heading compact">
              <div>
                <h2>Model services</h2>
                <p>Provider keys are stored locally and never copied to Cloud.</p>
              </div>
              <button class="text-action" on:click={() => (selected = 'providers')}>Manage</button>
            </div>
            <div class="provider-list">
              {#each snapshot.providers.slice(0, 2) as provider}
                <div class="provider-row">
                  <div class="provider-monogram">{provider.name.slice(0, 1)}</div>
                  <div class="provider-copy"><strong>{provider.name}</strong><span>{provider.model}</span></div>
                  <div class="provider-security"><KeyRound size={13} /> {provider.note}</div>
                  <div class="connected-text"><span></span>Connected</div>
                </div>
              {/each}
            </div>
          </section>

          <section class="update-panel panel" class:available={snapshot.updateAvailable}>
            <div class="update-icon"><Download size={20} /></div>
            <div class="update-copy">
              <span class="signal-label">Managed update</span>
              <h3>{snapshot.updateAvailable ? `${snapshot.updateVersion} is ready` : 'You are up to date'}</h3>
              <p>Signed, staged, health-gated and automatically reversible.</p>
            </div>
            <button class="button quiet" on:click={() => (selected = 'updates')}>Review <ChevronRight size={15} /></button>
          </section>
        </div>
      {:else if selected === 'agents'}
        <section class="page-heading"><div><span>Execution authority</span><h1>Agent</h1><p>The local Hermes Core remains the only authority allowed to execute work.</p></div><button class="button primary">Open Agent <ArrowUpRight size={16} /></button></section>
        <section class="detail-panel panel">
          <div class="agent-identity"><div class="agent-glyph"><Bot size={28} /></div><div><h2>{snapshot.deviceName}</h2><p>{snapshot.profileName} profile · {snapshot.platform}</p></div><div class="large-health"><span></span>Ready</div></div>
          <div class="detail-metrics"><div><span>Runtime</span><strong>{snapshot.runtimeVersion}</strong></div><div><span>Generation</span><strong>{snapshot.runtimeGeneration}</strong></div><div><span>Sessions</span><strong>{snapshot.activeSessions}</strong></div><div><span>Tasks</span><strong>{snapshot.runningTasks}</strong></div></div>
        </section>
      {:else if selected === 'sessions'}
        <section class="page-heading"><div><span>Live work</span><h1>Sessions</h1><p>Sessions remain authoritative on the local Agent and are safely projected to Cloud.</p></div></section>
        <section class="list-panel panel">
          {#each ['Operations planning', 'PIM product strategy', 'Supplier analysis'] as name, index}
            <button class="session-row"><div class="session-dot"></div><div class="session-main"><strong>{name}</strong><span>Live · generation {snapshot.runtimeGeneration}</span></div><div class="session-meta">{index === 0 ? 'Controlling' : 'Observing'}</div><ChevronRight size={16} /></button>
          {/each}
        </section>
      {:else if selected === 'providers'}
        <section class="page-heading"><div><span>Local credentials</span><h1>Model services</h1><p>Configure providers without moving API keys off this execution host.</p></div><button class="button secondary"><KeyRound size={16} /> Add provider</button></section>
        <section class="provider-grid">
          {#each snapshot.providers as provider}
            <div class="provider-card panel"><div class="provider-card-top"><div class="provider-monogram large">{provider.name.slice(0, 1)}</div><span class:muted={provider.state !== 'connected'} class="connected-text"><span></span>{stateLabel(provider.state)}</span></div><h2>{provider.name}</h2><p>{provider.model}</p><div class="provider-card-foot"><KeyRound size={14} /><span>{provider.note}</span><button><Settings2 size={15} /></button></div></div>
          {/each}
        </section>
      {:else if selected === 'updates'}
        <section class="page-heading"><div><span>Release control</span><h1>Updates</h1><p>Desktop and Managed Runtime releases are verified separately and activated atomically.</p></div></section>
        <div class="update-layout"><section class="release-card panel"><div class="release-status"><div class="release-orb"><RefreshCw size={24} /></div><div><span>Candidate release</span><h2>{snapshot.updateVersion ?? snapshot.runtimeVersion}</h2></div><div class="verified-mark"><ShieldCheck size={16} /> Signed</div></div><div class="release-flow"><div><span>Current</span><strong>{snapshot.runtimeVersion}</strong></div><ChevronRight size={18} /><div><span>Candidate</span><strong>{snapshot.updateVersion}</strong></div><ChevronRight size={18} /><div><span>Rollback</span><strong>Automatic</strong></div></div><button class="button primary full">Stage update</button></section><section class="panel guardrail-panel"><ShieldCheck size={22} /><h2>Health-gated activation</h2><p>Hermes will not promote a candidate until Host, Plugin, Connector, Cloud transport and runtime authority all pass verification.</p><ul><li><Check size={14} /> Immutable release digest</li><li><Check size={14} /> Private toolchain validation</li><li><Check size={14} /> Previous release retained</li></ul></section></div>
      {:else if selected === 'diagnostics'}
        <section class="page-heading"><div><span>Local verification</span><h1>Diagnostics</h1><p>Evidence-first checks for the complete local execution and Cloud path.</p></div><button class="button secondary" on:click={refreshSnapshot}><RefreshCw size={16} /> Run checks</button></section>
        <section class="diagnostic-panel panel">
          {#each [
            ['Runtime authority', 'Hermes Core process identity matches the active release.'],
            ['Plugin local gateway', 'Observer and control endpoints share the active generation.'],
            ['Connector transport', 'Device-bound WSS is authenticated and sequence-safe.'],
            ['Credential boundary', 'Provider credentials remain in the platform secret store.'],
            ['Release integrity', 'Manifest, toolchain and executable paths are inside Hermes roots.'],
          ] as check}
            <div class="diagnostic-row"><div class="diagnostic-check"><Check size={14} /></div><div><strong>{check[0]}</strong><p>{check[1]}</p></div><span>Passed</span></div>
          {/each}
        </section>
      {/if}
    </div>
  </main>
</div>
