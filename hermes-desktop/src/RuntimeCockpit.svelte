<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import {
    Activity,
    Bot,
    Cable,
    Check,
    ChevronRight,
    Cloud,
    Cpu,
    Download,
    Gauge,
    KeyRound,
    MessagesSquare,
    RefreshCw,
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
    { key: 'overview', label: '运行总览' },
    { key: 'agents', label: 'Agent' },
    { key: 'sessions', label: '会话' },
    { key: 'providers', label: '模型服务' },
    { key: 'updates', label: '版本更新' },
    { key: 'diagnostics', label: '诊断' },
  ];

  onMount(refreshSnapshot);

  async function refreshSnapshot() {
    if (refreshing) return;
    refreshing = true;
    try {
      snapshot = await invoke<RuntimeSnapshot>('runtime_snapshot');
      nativeSource = true;
    } catch {
      nativeSource = false;
    } finally {
      refreshing = false;
    }
  }

  function runtimeReady() {
    return snapshot.agentReady && snapshot.cloudConnected;
  }

  function stateText(state: string) {
    switch (state) {
      case 'healthy':
      case 'connected': return '正常';
      case 'degraded': return '降级';
      case 'offline': return '离线';
      case 'updating': return '更新中';
      case 'attention': return '需要处理';
      case 'not-configured': return '未配置';
      default: return state || '未知';
    }
  }

  function componentName(id: string, fallback: string) {
    switch (id) {
      case 'cloud': return 'Hermes Cloud';
      case 'connector': return 'Connector';
      case 'plugin': return 'Agent Plugin';
      case 'core': return 'Hermes Core';
      default: return fallback;
    }
  }

  function heroTitle() {
    if (runtimeReady()) return 'Hermes 已经可以工作';
    if (snapshot.agentReady) return '本地 Agent 已启动，正在等待云连接';
    return '本地运行环境还没有完全就绪';
  }

  function heroDescription() {
    if (runtimeReady()) return '任务在这台电脑上执行，Hermes Cloud 负责连接、控制和状态同步。';
    if (snapshot.agentReady) return '本地执行已经可用，但当前还没有建立完整的 Cloud 控制链路。';
    return 'Hermes 会继续检查 Runtime Manager、Core、Agent Plugin 与 Connector 的真实运行状态。';
  }

  function providerCount() {
    return snapshot.providers.filter((provider) => provider.state === 'connected').length;
  }
</script>

<div class="cockpit-shell">
  <aside class="sidebar">
    <div class="brand-row" data-tauri-drag-region>
      <div class="brand-mark"><span></span></div>
      <div>
        <strong>Hermes</strong>
        <span>本地智能体运行中心</span>
      </div>
    </div>

    <nav aria-label="主要导航">
      {#each navigation as item}
        <button class:active={selected === item.key} on:click={() => (selected = item.key)}>
          {#if item.key === 'overview'}<Gauge size={19} />{/if}
          {#if item.key === 'agents'}<Bot size={19} />{/if}
          {#if item.key === 'sessions'}<MessagesSquare size={19} />{/if}
          {#if item.key === 'providers'}<Sparkles size={19} />{/if}
          {#if item.key === 'updates'}<RefreshCw size={19} />{/if}
          {#if item.key === 'diagnostics'}<Stethoscope size={19} />{/if}
          <span>{item.label}</span>
          {#if item.key === 'updates' && snapshot.updateAvailable}<i></i>{/if}
        </button>
      {/each}
    </nav>

    <div class="sidebar-spacer"></div>

    <div class="device-card">
      <Cpu size={20} />
      <div>
        <strong>{snapshot.deviceName}</strong>
        <span>{snapshot.platform} · {snapshot.architecture}</span>
      </div>
      <Settings2 size={17} />
    </div>

    <div class="sidebar-status">
      <span class:good={snapshot.agentReady}></span>
      {snapshot.agentReady ? '本机执行权限已就绪' : '等待本机执行权限'}
    </div>
  </aside>

  <main class="main-area">
    <header class="topbar" data-tauri-drag-region>
      <div>
        <strong>{navigation.find((item) => item.key === selected)?.label}</strong>
        <span>{nativeSource ? '实时本机状态' : '等待本机状态'}</span>
      </div>
      <button class="refresh-button" on:click={refreshSnapshot} disabled={refreshing}>
        <span class:spin={refreshing}><RefreshCw size={18} /></span>
        {refreshing ? '刷新中…' : '刷新状态'}
      </button>
    </header>

    <div class="content-frame">
      {#if selected === 'overview'}
        <section class="hero">
          <div>
            <div class="hero-status"><span class:good={runtimeReady()}></span>{runtimeReady() ? '运行正常' : '正在准备'}</div>
            <h1>{heroTitle()}</h1>
            <p>{heroDescription()}</p>
          </div>
          <button class="primary-action" disabled={!snapshot.agentReady}><Bot size={19} /> 打开 Agent</button>
        </section>

        <section class="metrics" aria-label="运行状态摘要">
          <div>
            <span>Agent</span>
            <strong class:good={snapshot.agentReady}>{snapshot.agentReady ? '已就绪' : '未就绪'}</strong>
          </div>
          <div>
            <span>Cloud</span>
            <strong class:good={snapshot.cloudConnected}>{snapshot.cloudConnected ? '已连接' : '离线'}</strong>
          </div>
          <div>
            <span>活动会话</span>
            <strong>{snapshot.activeSessions}</strong>
          </div>
          <div>
            <span>运行任务</span>
            <strong>{snapshot.runningTasks}</strong>
          </div>
          <div>
            <span>模型服务</span>
            <strong class:good={providerCount() > 0}>{providerCount()} 个已连接</strong>
          </div>
        </section>

        <div class="overview-layout">
          <section class="panel runtime-panel">
            <div class="panel-title">
              <div>
                <h2>本地运行链路</h2>
                <p>从 Cloud 到本地执行，所有状态都来自真实组件。</p>
              </div>
              <div class="verified"><ShieldCheck size={18} /> {runtimeReady() ? '已验证' : '等待验证'}</div>
            </div>

            <div class="runtime-chain">
              {#each snapshot.components as component}
                <div class="runtime-row">
                  <div class="runtime-icon">
                    {#if component.id === 'cloud'}<Cloud size={22} />{/if}
                    {#if component.id === 'connector'}<Cable size={22} />{/if}
                    {#if component.id === 'plugin'}<Zap size={22} />{/if}
                    {#if component.id === 'core'}<Cpu size={22} />{/if}
                  </div>
                  <div class="runtime-copy">
                    <strong>{componentName(component.id, component.name)}</strong>
                    <span>{component.detail}</span>
                  </div>
                  <div class="runtime-state" class:good={component.state === 'healthy'}>{stateText(component.state)}</div>
                </div>
              {/each}
            </div>

            <div class="authority-bar">
              <KeyRound size={17} />
              <span>运行版本</span>
              <strong>{snapshot.runtimeVersion}</strong>
              <span class="generation">Generation {snapshot.runtimeGeneration}</span>
            </div>
          </section>

          <section class="panel activity-panel">
            <div class="panel-title compact">
              <div>
                <h2>最近活动</h2>
                <p>本机运行与连接事件。</p>
              </div>
            </div>
            <div class="events">
              {#if snapshot.events.length === 0}
                <div class="empty">暂时没有新的运行事件。</div>
              {:else}
                {#each snapshot.events.slice(0, 6) as event}
                  <div class="event-row">
                    <span class:good={event.tone === 'success'} class:warn={event.tone === 'attention'}></span>
                    <div>
                      <strong>{event.title}</strong>
                      <p>{event.detail}</p>
                    </div>
                    <time>{event.at}</time>
                  </div>
                {/each}
              {/if}
            </div>
          </section>
        </div>

        <section class="panel provider-panel">
          <div class="panel-title compact">
            <div>
              <h2>模型服务</h2>
              <p>API Key 只保存在本机系统安全存储中。</p>
            </div>
            <button class="text-action" on:click={() => (selected = 'providers')}>管理模型服务 <ChevronRight size={17} /></button>
          </div>
          <div class="provider-list">
            {#each snapshot.providers as provider}
              <div class="provider-row">
                <div class="provider-logo">{provider.name.slice(0, 1)}</div>
                <div>
                  <strong>{provider.name}</strong>
                  <span>{provider.model}</span>
                </div>
                <div class="provider-note"><KeyRound size={15} /> {provider.note}</div>
                <div class="provider-state" class:good={provider.state === 'connected'}>{stateText(provider.state)}</div>
              </div>
            {/each}
          </div>
        </section>

        <section class="update-strip" class:available={snapshot.updateAvailable}>
          <Download size={22} />
          <div>
            <strong>{snapshot.updateAvailable ? `发现新版本 ${snapshot.updateVersion}` : '当前已经是最新版本'}</strong>
            <span>更新包经过签名验证、健康检查，并支持失败自动回滚。</span>
          </div>
          <button on:click={() => (selected = 'updates')}>查看更新</button>
        </section>

      {:else if selected === 'agents'}
        <section class="page-heading">
          <h1>Agent</h1>
          <p>查看这台电脑上的本地 Agent 执行状态与运行版本。</p>
        </section>
        <section class="panel detail-panel">
          <div class="agent-identity">
            <div class="agent-icon"><Bot size={30} /></div>
            <div>
              <h2>{snapshot.deviceName}</h2>
              <p>{snapshot.profileName} · {snapshot.platform}</p>
            </div>
            <strong class:good={snapshot.agentReady}>{snapshot.agentReady ? '已就绪' : '未就绪'}</strong>
          </div>
          <div class="detail-grid">
            <div><span>运行版本</span><strong>{snapshot.runtimeVersion}</strong></div>
            <div><span>Generation</span><strong>{snapshot.runtimeGeneration}</strong></div>
            <div><span>活动会话</span><strong>{snapshot.activeSessions}</strong></div>
            <div><span>运行任务</span><strong>{snapshot.runningTasks}</strong></div>
          </div>
        </section>

      {:else if selected === 'sessions'}
        <section class="page-heading">
          <h1>会话</h1>
          <p>查看由本地 Agent 持有的实时工作会话。</p>
        </section>
        <section class="panel simple-list">
          {#if snapshot.activeSessions > 0}
            {#each Array(snapshot.activeSessions) as _, index}
              <div class="simple-row"><Activity size={18} /><strong>活动会话 {index + 1}</strong><span>运行中</span></div>
            {/each}
          {:else}
            <div class="empty large">当前没有活动会话。</div>
          {/if}
        </section>

      {:else if selected === 'providers'}
        <section class="page-heading">
          <h1>模型服务</h1>
          <p>管理本机使用的模型提供商。密钥不会同步到 Cloud。</p>
        </section>
        <section class="panel provider-list large-list">
          {#each snapshot.providers as provider}
            <div class="provider-row">
              <div class="provider-logo">{provider.name.slice(0, 1)}</div>
              <div><strong>{provider.name}</strong><span>{provider.model}</span></div>
              <div class="provider-note"><KeyRound size={15} /> 本机安全存储</div>
              <div class="provider-state" class:good={provider.state === 'connected'}>{stateText(provider.state)}</div>
            </div>
          {/each}
        </section>

      {:else if selected === 'updates'}
        <section class="page-heading">
          <h1>版本更新</h1>
          <p>Hermes 使用签名、分阶段安装、健康检查和自动回滚保护更新过程。</p>
        </section>
        <section class="panel update-detail">
          <Download size={30} />
          <div>
            <h2>{snapshot.updateAvailable ? `新版本 ${snapshot.updateVersion} 可用` : '当前已经是最新版本'}</h2>
            <p>当前版本：{snapshot.runtimeVersion}</p>
          </div>
          <button disabled={!snapshot.updateAvailable}>{snapshot.updateAvailable ? '安装更新' : '无需更新'}</button>
        </section>

      {:else if selected === 'diagnostics'}
        <section class="page-heading">
          <h1>诊断</h1>
          <p>快速查看 Hermes 的关键组件是否正常。</p>
        </section>
        <section class="panel diagnostics-list">
          {#each snapshot.components as component}
            <div>
              <span class:good={component.state === 'healthy'}></span>
              <div><strong>{componentName(component.id, component.name)}</strong><p>{component.detail}</p></div>
              <b>{stateText(component.state)}</b>
            </div>
          {/each}
        </section>
      {/if}
    </div>
  </main>
</div>

<style>
  .cockpit-shell {
    width: 100%;
    height: 100%;
    min-height: 720px;
    display: grid;
    grid-template-columns: 260px minmax(0, 1fr);
    color: #e7f0ed;
    background: #07110f;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", "Noto Sans SC", sans-serif;
    -webkit-font-smoothing: antialiased;
  }

  .sidebar {
    display: flex;
    flex-direction: column;
    min-height: 0;
    padding: 26px 18px 20px;
    border-right: 1px solid rgba(212,238,231,.09);
    background: #081512;
  }

  .brand-row { display: flex; align-items: center; gap: 11px; padding: 5px 7px 24px; }
  .brand-row strong { display: block; font-size: 19px; font-weight: 720; }
  .brand-row span { display: block; margin-top: 4px; color: #6c877e; font-size: 12px; }
  .brand-mark { position: relative; width: 30px; height: 30px; border: 1px solid rgba(111,227,189,.22); border-radius: 50%; }
  .brand-mark span { position: absolute; left: 9px; top: 9px; width: 10px; height: 10px; border-radius: 50%; background: #6fe3bd; box-shadow: 0 0 18px rgba(111,227,189,.28); }

  nav { display: grid; gap: 6px; }
  nav button {
    position: relative;
    display: flex;
    align-items: center;
    gap: 11px;
    width: 100%;
    min-height: 48px;
    padding: 0 13px;
    border: 0;
    border-radius: 10px;
    background: transparent;
    color: #708a82;
    font: inherit;
    font-size: 15px;
    font-weight: 600;
    text-align: left;
    cursor: pointer;
  }
  nav button:hover { background: rgba(111,227,189,.045); color: #b9cec7; }
  nav button.active { background: rgba(111,227,189,.085); color: #d4e8e1; box-shadow: inset 0 0 0 1px rgba(111,227,189,.12); }
  nav button i { position: absolute; right: 13px; width: 7px; height: 7px; border-radius: 50%; background: #e4ad55; }

  .sidebar-spacer { flex: 1; }
  .device-card { display: grid; grid-template-columns: auto 1fr auto; gap: 10px; align-items: center; padding: 13px; border: 1px solid rgba(212,238,231,.09); border-radius: 12px; color: #7e9990; }
  .device-card strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; }
  .device-card span { display: block; margin-top: 4px; color: #5f776f; font-size: 12px; }
  .sidebar-status { display: flex; align-items: center; gap: 7px; margin-top: 14px; padding: 0 5px; color: #647b73; font-size: 12px; }
  .sidebar-status > span { width: 8px; height: 8px; border-radius: 50%; background: #d0a158; }
  .sidebar-status > span.good { background: #6fe3bd; }

  .main-area { min-width: 0; min-height: 0; display: flex; flex-direction: column; }
  .topbar { height: 72px; flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between; padding: 0 34px; border-bottom: 1px solid rgba(212,238,231,.09); }
  .topbar strong { display: block; font-size: 18px; }
  .topbar > div > span { display: block; margin-top: 4px; color: #6b837b; font-size: 12px; }
  .refresh-button { display: flex; align-items: center; gap: 8px; height: 40px; padding: 0 13px; border: 1px solid rgba(212,238,231,.12); border-radius: 9px; background: #0a1a16; color: #9bb0a9; font: inherit; font-size: 14px; cursor: pointer; }

  .content-frame { flex: 1; min-height: 0; overflow: auto; padding: 36px 38px 46px; }
  .hero { display: flex; align-items: flex-end; justify-content: space-between; gap: 30px; padding: 6px 0 28px; }
  .hero-status { display: flex; align-items: center; gap: 8px; color: #8aa198; font-size: 13px; font-weight: 650; }
  .hero-status span { width: 8px; height: 8px; border-radius: 50%; background: #d3a35c; }
  .hero-status span.good { background: #6fe3bd; }
  .hero h1, .page-heading h1 { margin: 14px 0 0; color: #eff6f3; font-size: 38px; line-height: 1.2; letter-spacing: -.03em; font-weight: 680; }
  .hero p, .page-heading p { max-width: 760px; margin: 13px 0 0; color: #7c948b; font-size: 16px; line-height: 1.75; }
  .primary-action { display: flex; align-items: center; gap: 8px; height: 46px; padding: 0 17px; border: 0; border-radius: 10px; background: #6fe3bd; color: #07110f; font: inherit; font-size: 15px; font-weight: 700; }
  .primary-action:disabled { opacity: .42; }

  .metrics { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); border-top: 1px solid rgba(212,238,231,.09); border-bottom: 1px solid rgba(212,238,231,.09); }
  .metrics > div { padding: 20px 18px; border-right: 1px solid rgba(212,238,231,.07); }
  .metrics > div:last-child { border-right: 0; }
  .metrics span { display: block; color: #60776f; font-size: 12px; }
  .metrics strong { display: block; margin-top: 8px; color: #d3a75f; font-size: 20px; font-weight: 680; }
  .metrics strong.good { color: #72c6aa; }

  .overview-layout { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(320px, .8fr); gap: 18px; margin-top: 22px; }
  .panel { border: 1px solid rgba(212,238,231,.10); border-radius: 14px; background: rgba(8,23,19,.72); }
  .runtime-panel, .activity-panel, .provider-panel, .detail-panel, .simple-list, .large-list, .update-detail, .diagnostics-list { padding: 22px; }
  .panel-title { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
  .panel-title h2 { margin: 0; font-size: 19px; font-weight: 680; }
  .panel-title p { margin: 7px 0 0; color: #718981; font-size: 13.5px; line-height: 1.55; }
  .verified { display: flex; align-items: center; gap: 7px; color: #7aa898; font-size: 13px; }

  .runtime-chain { margin-top: 20px; }
  .runtime-row { display: grid; grid-template-columns: 42px minmax(0, 1fr) auto; gap: 13px; align-items: center; min-height: 70px; padding: 10px 0; border-top: 1px solid rgba(212,238,231,.07); }
  .runtime-row:first-child { border-top: 0; }
  .runtime-icon { display: grid; place-items: center; width: 40px; height: 40px; border-radius: 11px; background: rgba(111,227,189,.06); color: #79bca7; }
  .runtime-copy strong { display: block; font-size: 15px; }
  .runtime-copy span { display: block; margin-top: 5px; color: #667e76; font-size: 13px; line-height: 1.45; }
  .runtime-state { color: #d0a35b; font-size: 13px; font-weight: 700; }
  .runtime-state.good { color: #72c6aa; }

  .authority-bar { display: flex; align-items: center; gap: 9px; margin-top: 16px; padding: 13px 14px; border-radius: 10px; background: rgba(5,17,14,.46); color: #6e867e; font-size: 13px; }
  .authority-bar strong { color: #c9d7d2; }
  .generation { margin-left: auto; color: #5e766e; }

  .events { margin-top: 16px; }
  .event-row { display: grid; grid-template-columns: 9px minmax(0,1fr) auto; gap: 10px; align-items: start; padding: 14px 0; border-top: 1px solid rgba(212,238,231,.07); }
  .event-row:first-child { border-top: 0; }
  .event-row > span { width: 8px; height: 8px; margin-top: 6px; border-radius: 50%; background: #758780; }
  .event-row > span.good { background: #6fe3bd; }
  .event-row > span.warn { background: #d7a257; }
  .event-row strong { font-size: 14px; }
  .event-row p { margin: 5px 0 0; color: #667d75; font-size: 12.5px; line-height: 1.5; }
  .event-row time { color: #546b63; font-size: 11.5px; }

  .provider-panel { margin-top: 18px; }
  .text-action { display: flex; align-items: center; gap: 4px; border: 0; background: transparent; color: #8fb2a7; font: inherit; font-size: 13.5px; cursor: pointer; }
  .provider-list { margin-top: 14px; }
  .provider-row { display: grid; grid-template-columns: 42px minmax(160px,1fr) minmax(180px,1fr) auto; gap: 12px; align-items: center; min-height: 64px; padding: 9px 0; border-top: 1px solid rgba(212,238,231,.07); }
  .provider-row:first-child { border-top: 0; }
  .provider-logo { display: grid; place-items: center; width: 40px; height: 40px; border-radius: 11px; background: #0d211b; color: #8bc8b5; font-weight: 750; }
  .provider-row strong { display: block; font-size: 14.5px; }
  .provider-row span { display: block; margin-top: 4px; color: #657c74; font-size: 12.5px; }
  .provider-note { display: flex; align-items: center; gap: 6px; color: #667d75; font-size: 12.5px; }
  .provider-state { color: #d2a45d; font-size: 13px; font-weight: 700; }
  .provider-state.good { color: #72c6aa; }

  .update-strip { display: grid; grid-template-columns: auto minmax(0,1fr) auto; gap: 13px; align-items: center; margin-top: 18px; padding: 18px 20px; border: 1px solid rgba(212,238,231,.09); border-radius: 13px; color: #749087; background: #091915; }
  .update-strip strong { display: block; color: #d9e5e1; font-size: 15px; }
  .update-strip span { display: block; margin-top: 5px; color: #657c74; font-size: 12.5px; }
  .update-strip button, .update-detail button { height: 38px; padding: 0 13px; border: 1px solid rgba(212,238,231,.12); border-radius: 8px; background: #10251f; color: #a8c1b9; font: inherit; font-size: 13.5px; }

  .page-heading { padding: 8px 0 28px; }
  .detail-panel { min-height: 260px; }
  .agent-identity { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 14px; }
  .agent-icon { display: grid; place-items: center; width: 54px; height: 54px; border-radius: 14px; background: rgba(111,227,189,.07); color: #79c5ad; }
  .agent-identity h2 { margin: 0; font-size: 20px; }
  .agent-identity p { margin: 5px 0 0; color: #687f77; font-size: 13px; }
  .agent-identity > strong { color: #d1a45b; font-size: 14px; }
  .agent-identity > strong.good { color: #72c6aa; }
  .detail-grid { display: grid; grid-template-columns: repeat(4,1fr); margin-top: 26px; border-top: 1px solid rgba(212,238,231,.08); }
  .detail-grid > div { padding: 18px 12px 0 0; }
  .detail-grid span { display: block; color: #627970; font-size: 12px; }
  .detail-grid strong { display: block; margin-top: 7px; font-size: 17px; }

  .simple-row { display: flex; align-items: center; gap: 10px; min-height: 58px; padding: 0 4px; border-top: 1px solid rgba(212,238,231,.07); }
  .simple-row:first-child { border-top: 0; }
  .simple-row strong { font-size: 14.5px; }
  .simple-row span { margin-left: auto; color: #72c6aa; font-size: 13px; }
  .empty { color: #667e76; font-size: 14px; line-height: 1.6; }
  .empty.large { padding: 38px 4px; font-size: 16px; }

  .update-detail { display: flex; align-items: center; gap: 16px; }
  .update-detail h2 { margin: 0; font-size: 19px; }
  .update-detail p { margin: 6px 0 0; color: #697f77; font-size: 13px; }
  .update-detail button { margin-left: auto; }

  .diagnostics-list > div { display: grid; grid-template-columns: 9px minmax(0,1fr) auto; gap: 11px; align-items: center; min-height: 64px; border-top: 1px solid rgba(212,238,231,.07); }
  .diagnostics-list > div:first-child { border-top: 0; }
  .diagnostics-list > div > span { width: 8px; height: 8px; border-radius: 50%; background: #d1a45b; }
  .diagnostics-list > div > span.good { background: #6fe3bd; }
  .diagnostics-list strong { font-size: 14.5px; }
  .diagnostics-list p { margin: 4px 0 0; color: #657c74; font-size: 12.5px; }
  .diagnostics-list b { color: #8fa69e; font-size: 13px; }

  .spin { display: inline-flex; animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  @media (max-width: 1050px) {
    .cockpit-shell { grid-template-columns: 220px minmax(0,1fr); }
    .overview-layout { grid-template-columns: 1fr; }
    .metrics { grid-template-columns: repeat(2,1fr); }
    .provider-row { grid-template-columns: 42px 1fr auto; }
    .provider-note { display: none; }
  }

  @media (max-width: 820px) {
    .cockpit-shell { grid-template-columns: 1fr; }
    .sidebar { display: none; }
    .content-frame { padding: 26px 22px 36px; }
    .hero { align-items: flex-start; flex-direction: column; }
    .topbar { padding: 0 22px; }
  }

  @media (prefers-reduced-motion: reduce) {
    .spin { animation: none; }
  }
</style>