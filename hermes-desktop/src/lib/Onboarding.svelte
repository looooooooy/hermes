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
  export let devicePairing = false;
  export let devicePairingError = '';
  export let onRefresh: () => void;
  export let onConnectWorkspace: (endpoint: string) => void;
  export let onPairDevice: () => void;

  type StepKey = 'foundation' | 'enterprise' | 'device' | 'runtime' | 'provider' | 'ready';
  type StepState = 'complete' | 'current' | 'pending';

  const steps: Array<{ key: StepKey; number: string; title: string }> = [
    { key: 'foundation', number: '01', title: '基础环境' },
    { key: 'enterprise', number: '02', title: '企业云登录' },
    { key: 'device', number: '03', title: '设备绑定' },
    { key: 'runtime', number: '04', title: '本地运行环境' },
    { key: 'provider', number: '05', title: '模型服务' },
    { key: 'ready', number: '06', title: '完成' },
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
    return snapshot.devicePaired;
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
      case 'foundation': return '正在准备 Hermes 本地环境';
      case 'enterprise': return '连接你的企业工作空间';
      case 'device': return '绑定这台 Mac';
      case 'runtime': return '安装并启动本地运行环境';
      case 'provider': return '配置模型服务';
      case 'ready': return 'Hermes 已准备就绪';
    }
  }

  function activeDescription() {
    switch (currentStep()) {
      case 'foundation':
        return 'Hermes 正在确认本机 Runtime Manager 是否已经就绪。只有拿到真实的本地运行证据后，才会进入下一步。';
      case 'enterprise':
        return '通过浏览器完成企业账号登录。Hermes 使用 PKCE 完成安全授权，登录凭据只保存在 macOS 系统钥匙串中。';
      case 'device':
        return 'Hermes 会为这台电脑创建独立的 Ed25519 设备身份，并绑定到你的企业工作空间。整个过程不会把密钥暴露到界面中。';
      case 'runtime':
        return '设备绑定完成后，Hermes 会准备私有运行环境，并确认 Core、Agent Plugin 和 Connector 都处于可用状态。';
      case 'provider':
        return '连接至少一个模型服务。模型密钥只保存在本机安全存储中，不会显示在界面或日志里。';
      case 'ready':
        return '企业身份、设备身份、本地运行环境和模型服务都已经通过验证，可以开始使用 Hermes。';
    }
  }

  function blockerTitle() {
    switch (currentStep()) {
      case 'foundation': return '等待 Runtime Manager 就绪';
      case 'enterprise': return '需要完成企业账号登录';
      case 'device': return '这台电脑还没有绑定';
      case 'runtime': return runtimeInstalled() ? '本地核心组件还未就绪' : '本地运行环境尚未安装';
      case 'provider': return '还没有配置模型服务';
      case 'ready': return '所有准备工作已完成';
    }
  }

  function blockerDetail() {
    switch (currentStep()) {
      case 'foundation':
        return 'Hermes 不会用模拟状态冒充成功。Runtime Manager 提供真实状态后，本步骤会自动完成。';
      case 'enterprise':
        return '确认企业云地址后点击“浏览器登录”。登录成功后 Desktop 会自动接收授权结果并进入设备绑定。';
      case 'device':
        return '点击“绑定这台 Mac”。只有设备凭据写入系统钥匙串，并生成有效的 paired.json 后才算绑定完成。';
      case 'runtime':
        return `当前运行环境：${runtimeStatusText() || snapshot.runtimeVersion}。Hermes 会等待 Core 和 Agent Plugin 同时健康后再继续。`;
      case 'provider':
        return '选择并配置一个模型服务。只有本机安全存储确认凭据存在后，Hermes 才会认为配置完成。';
      case 'ready':
        return '这台电脑已经具备进入 Hermes 运行中心所需的全部证据。';
    }
  }

  function deviceStateText() {
    if (devicePaired()) return '已绑定';
    switch (snapshot.devicePairingState) {
      case 'inactive':
      case 'not-paired': return '未绑定';
      case 'blocked': return '已阻止';
      case 'active': return '已激活';
      default: return snapshot.devicePairingState || '未绑定';
    }
  }

  function runtimeStatusText() {
    if (localAuthorityReady()) return '运行正常';
    switch (snapshot.runtimeVersion) {
      case 'not-installed': return '未安装';
      case 'not-connected': return '未连接';
      default: return snapshot.runtimeVersion;
    }
  }

  function providerStatusText() {
    return providerConfigured() ? '已连接' : '未配置';
  }

  function workspaceStatusText() {
    if (!workspaceConnected()) return '未登录';
    return snapshot.workspaceUser ?? '已登录';
  }

  function progressIndex() {
    return steps.findIndex((step) => step.key === currentStep()) + 1;
  }
</script>

<div class="onboarding-shell">
  <header class="top" data-tauri-drag-region>
    <div class="brand">
      <div class="mark" aria-hidden="true"><span></span></div>
      <div>
        <strong>Hermes</strong>
        <span>本地智能体运行中心</span>
      </div>
    </div>
    <div class="security"><ShieldCheck size={17} /> 本机安全运行</div>
  </header>

  <main class="main">
    <aside class="rail">
      <div class="rail-intro">
        <span>首次设置</span>
        <h2>初始化 Hermes</h2>
        <p>按照左侧步骤完成一次设置。每一步都必须有真实系统状态作为依据。</p>
      </div>

      <div class="steps">
        {#each steps as step}
          <div
            class="step"
            class:current={stepState(step.key) === 'current'}
            class:complete={stepState(step.key) === 'complete'}
          >
            <div class="step-glyph">
              {#if stepState(step.key) === 'complete'}
                <Check size={17} strokeWidth={2.1} />
              {:else if stepState(step.key) === 'current'}
                <CircleDot size={17} strokeWidth={2} />
              {:else}
                <span></span>
              {/if}
            </div>
            <div class="step-copy">
              <span>{step.number}</span>
              <strong>{step.title}</strong>
            </div>
          </div>
        {/each}
      </div>

      <div class="device">
        <Laptop size={19} />
        <div>
          <strong>{snapshot.deviceName}</strong>
          <span>{snapshot.platform} · {snapshot.architecture}</span>
        </div>
      </div>
    </aside>

    <section class="stage">
      <div class="halo" aria-hidden="true"></div>
      <div class="content">
        <div class="progress-line">
          <span class:ready={completelyReady()}></span>
          {#if completelyReady()}
            全部完成
          {:else}
            当前进度 {progressIndex()} / {steps.length}
          {/if}
        </div>

        <div class="stage-icon">
          {#if currentStep() === 'foundation'}<Cpu size={30} strokeWidth={1.6} />{/if}
          {#if currentStep() === 'enterprise'}<Cloud size={30} strokeWidth={1.6} />{/if}
          {#if currentStep() === 'device'}<Cable size={30} strokeWidth={1.6} />{/if}
          {#if currentStep() === 'runtime'}<ShieldCheck size={30} strokeWidth={1.6} />{/if}
          {#if currentStep() === 'provider'}<Sparkles size={30} strokeWidth={1.6} />{/if}
          {#if currentStep() === 'ready'}<Check size={30} strokeWidth={1.8} />{/if}
        </div>

        <h1>{activeTitle()}</h1>
        <p class="description">{activeDescription()}</p>

        <section class="evidence-card" aria-live="polite">
          <div class="evidence-heading">
            <div>
              <span>当前状态</span>
              <strong>{blockerTitle()}</strong>
            </div>
            <div class="evidence-status" class:verified={stepComplete(currentStep())}>
              {#if stepComplete(currentStep())}
                <Check size={16} /> 已验证
              {:else}
                <CircleDot size={16} /> 等待中
              {/if}
            </div>
          </div>
          <p class="evidence-detail">{blockerDetail()}</p>

          {#if currentStep() === 'enterprise'}
            <div class="workspace-form">
              <label for="workspace-endpoint">企业云地址</label>
              <div class="workspace-row">
                <input
                  id="workspace-endpoint"
                  bind:value={workspaceEndpoint}
                  placeholder="https://api.example.com/hermes/"
                  autocomplete="url"
                  spellcheck="false"
                />
                <button
                  class="connect"
                  on:click={() => onConnectWorkspace(workspaceEndpoint)}
                  disabled={workspaceConnecting || !workspaceEndpoint.trim()}
                >
                  {#if workspaceConnecting}
                    <span class="spinner"></span> 等待浏览器登录…
                  {:else}
                    <ExternalLink size={18} /> 浏览器登录
                  {/if}
                </button>
              </div>
              {#if workspaceError}<div class="workspace-error">{workspaceError}</div>{/if}
              <div class="workspace-note">
                <KeyRound size={15} /> PKCE 安全授权 · Desktop 自动完成授权交换 · 凭据仅保存在 macOS 钥匙串
              </div>
            </div>
          {/if}

          {#if currentStep() === 'device'}
            <div class="workspace-form pairing-form">
              <div class="pairing-head">
                <div>
                  <span>当前设备</span>
                  <strong>{snapshot.deviceName}</strong>
                </div>
                <div class="pairing-state">{deviceStateText()}</div>
              </div>
              <button
                class="connect pairing-connect"
                on:click={onPairDevice}
                disabled={devicePairing || workspaceConnecting || !snapshot.workspaceAuthenticated}
              >
                {#if devicePairing}
                  <span class="spinner"></span> 正在创建设备身份…
                {:else}
                  <ShieldCheck size={18} /> 绑定这台 Mac
                {/if}
              </button>
              <button
                class="connect pairing-relogin"
                on:click={() => onConnectWorkspace(workspaceEndpoint)}
                disabled={workspaceConnecting || devicePairing || !workspaceEndpoint.trim()}
              >
                {#if workspaceConnecting}
                  <span class="spinner"></span> 正在重新登录企业账号…
                {:else}
                  <ExternalLink size={18} /> 重新登录企业账号
                {/if}
              </button>
              {#if workspaceError}
                <div class="workspace-error">{workspaceError}</div>
              {:else if devicePairingError}
                <div class="workspace-error">{devicePairingError}</div>
              {/if}
              {#if snapshot.deviceCredentialFingerprint}
                <div class="workspace-note fingerprint"><KeyRound size={15} /> {snapshot.deviceCredentialFingerprint}</div>
              {/if}
              <div class="workspace-note">
                <KeyRound size={15} /> Ed25519 设备密钥 · Connector Pairing v1 · 设备凭据仅保存在系统钥匙串
              </div>
            </div>
          {/if}

          <div class="status-grid">
            <div>
              <span>Runtime Manager</span>
              <strong class:good={managerConnected()}>{managerConnected() ? '已连接' : '未连接'}</strong>
            </div>
            <div>
              <span>企业账号</span>
              <strong class:good={workspaceConnected()}>{workspaceStatusText()}</strong>
            </div>
            <div>
              <span>设备</span>
              <strong class:good={devicePaired()}>{deviceStateText()}</strong>
            </div>
            <div>
              <span>本地运行环境</span>
              <strong class:good={localAuthorityReady()}>{runtimeStatusText()}</strong>
            </div>
            <div>
              <span>模型服务</span>
              <strong class:good={providerConfigured()}>{providerStatusText()}</strong>
            </div>
          </div>
        </section>

        <div class="actions">
          <button class="refresh" on:click={onRefresh} disabled={refreshing || devicePairing}>
            <span class:spin={refreshing}><RefreshCw size={18} /></span>
            {refreshing ? '正在刷新…' : '刷新状态'}
          </button>
          <button class="cockpit" disabled={!completelyReady()}>
            进入运行中心 <ArrowRight size={18} />
          </button>
        </div>
        <div class="privacy"><KeyRound size={15} /> 敏感凭据只保存在这台电脑的系统安全存储中。</div>
      </div>
    </section>
  </main>
</div>

<style>
  .onboarding-shell {
    width: 100%;
    height: 100%;
    min-height: 720px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    color: #e7f0ed;
    background:
      radial-gradient(circle at 72% 10%, rgba(66, 166, 133, .12), transparent 30%),
      linear-gradient(180deg, #071411 0%, #07110f 62%, #06100e 100%);
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", "Noto Sans SC", sans-serif;
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
  }

  .top {
    height: 72px;
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 30px;
    border-bottom: 1px solid rgba(209, 237, 229, .10);
    background: rgba(5, 17, 14, .42);
  }

  .brand { display: flex; align-items: center; gap: 12px; }
  .brand strong { display: block; font-size: 18px; line-height: 1.1; font-weight: 720; }
  .brand > div:last-child span { display: block; margin-top: 4px; color: #738e86; font-size: 12px; line-height: 1.2; }

  .mark {
    position: relative;
    width: 32px;
    height: 32px;
    border: 1px solid rgba(111, 227, 189, .22);
    border-radius: 50%;
    transform: rotate(-22deg) scaleY(.75);
  }
  .mark::after {
    content: '';
    position: absolute;
    inset: 5px;
    border: 1px solid rgba(117, 168, 255, .20);
    border-radius: 50%;
    transform: rotate(68deg) scaleY(.7);
  }
  .mark span {
    position: absolute;
    z-index: 2;
    left: 10px;
    top: 10px;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: #6fe3bd;
    box-shadow: 0 0 20px rgba(111, 227, 189, .30);
    transform: scaleY(1.3);
  }

  .security { display: flex; align-items: center; gap: 8px; color: #7d9c93; font-size: 13px; font-weight: 550; }

  .main { min-height: 0; flex: 1; display: grid; grid-template-columns: 320px minmax(0, 1fr); }

  .rail {
    display: flex;
    flex-direction: column;
    padding: 36px 24px 24px;
    border-right: 1px solid rgba(209, 237, 229, .10);
    background: rgba(7, 20, 17, .66);
  }

  .rail-intro > span { color: #72958a; font-size: 12px; font-weight: 700; letter-spacing: .08em; }
  .rail-intro h2 { margin: 8px 0 0; font-size: 28px; line-height: 1.2; letter-spacing: -.025em; font-weight: 670; }
  .rail-intro p { margin: 12px 0 0; color: #738b84; font-size: 14px; line-height: 1.7; }

  .steps { display: grid; gap: 6px; margin-top: 30px; }
  .step {
    position: relative;
    display: flex;
    align-items: center;
    gap: 14px;
    min-height: 64px;
    padding: 9px 12px;
    border-radius: 12px;
    color: #647c75;
  }
  .step::before {
    content: '';
    position: absolute;
    left: 28px;
    top: -9px;
    width: 1px;
    height: 16px;
    background: rgba(212, 238, 231, .08);
  }
  .step:first-child::before { display: none; }
  .step.current {
    color: #c9dad5;
    background: rgba(111, 227, 189, .065);
    box-shadow: inset 0 0 0 1px rgba(111, 227, 189, .12);
  }
  .step.complete { color: #83a99d; }

  .step-glyph {
    z-index: 1;
    display: grid;
    place-items: center;
    width: 36px;
    height: 36px;
    flex: 0 0 auto;
    border: 1px solid rgba(212, 238, 231, .10);
    border-radius: 10px;
    background: #0b1916;
    color: #607a72;
  }
  .step.current .step-glyph { color: #7bd5b8; border-color: rgba(111, 227, 189, .28); }
  .step.complete .step-glyph { color: #75bea5; }
  .step-glyph > span { width: 5px; height: 5px; border-radius: 50%; background: #425a53; }
  .step-copy span { display: block; margin-bottom: 3px; color: #587168; font-size: 11px; font-weight: 700; letter-spacing: .06em; }
  .step-copy strong { display: block; font-size: 15px; line-height: 1.25; font-weight: 620; }

  .device {
    margin-top: auto;
    display: flex;
    align-items: center;
    gap: 11px;
    padding: 14px;
    border: 1px solid rgba(212, 238, 231, .09);
    border-radius: 12px;
    color: #7d9991;
    background: rgba(5, 16, 14, .42);
  }
  .device strong { display: block; font-size: 14px; line-height: 1.25; }
  .device span { display: block; margin-top: 4px; font-size: 12px; color: #5f766f; }

  .stage { position: relative; display: grid; place-items: center; overflow: auto; }
  .halo {
    position: absolute;
    width: 760px;
    height: 560px;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(40, 126, 104, .09), transparent 68%);
    filter: blur(14px);
    pointer-events: none;
  }

  .content { position: relative; z-index: 1; width: min(900px, calc(100% - 72px)); padding: 46px 0 44px; }
  .progress-line { display: flex; align-items: center; gap: 9px; color: #7c9990; font-size: 13px; font-weight: 650; }
  .progress-line > span { width: 8px; height: 8px; border-radius: 50%; background: #e4ad55; box-shadow: 0 0 12px rgba(228, 173, 85, .22); }
  .progress-line > span.ready { background: #6fe3bd; }

  .stage-icon {
    display: grid;
    place-items: center;
    width: 58px;
    height: 58px;
    margin-top: 24px;
    border: 1px solid rgba(111, 227, 189, .20);
    border-radius: 16px;
    background: rgba(9, 31, 25, .78);
    color: #79d0b4;
  }

  .content h1 {
    max-width: 820px;
    margin: 24px 0 0;
    color: #eef5f2;
    font-size: clamp(38px, 4vw, 46px);
    line-height: 1.18;
    letter-spacing: -.035em;
    font-weight: 660;
  }

  .description {
    max-width: 830px;
    margin: 16px 0 0;
    color: #82978f;
    font-size: 16px;
    line-height: 1.8;
  }

  .evidence-card {
    margin-top: 30px;
    padding: 24px;
    border: 1px solid rgba(212, 238, 231, .12);
    border-radius: 16px;
    background: rgba(8, 24, 20, .76);
    box-shadow: 0 22px 60px rgba(0, 0, 0, .16);
  }

  .evidence-heading { display: flex; align-items: center; justify-content: space-between; gap: 18px; }
  .evidence-heading > div:first-child span { display: block; color: #638177; font-size: 12px; font-weight: 700; }
  .evidence-heading strong { display: block; margin-top: 6px; font-size: 18px; line-height: 1.35; color: #e2ebe8; }
  .evidence-status {
    display: flex;
    align-items: center;
    gap: 6px;
    flex: 0 0 auto;
    padding: 8px 11px;
    border: 1px solid rgba(210, 165, 75, .24);
    border-radius: 999px;
    color: #cda75e;
    font-size: 13px;
    font-weight: 700;
  }
  .evidence-status.verified { color: #75c6aa; border-color: rgba(103, 184, 157, .26); }
  .evidence-detail { margin: 13px 0 0; color: #778d85; font-size: 14.5px; line-height: 1.7; }

  .workspace-form {
    margin-top: 20px;
    padding: 18px;
    border: 1px solid rgba(111, 227, 189, .11);
    border-radius: 13px;
    background: rgba(6, 19, 16, .58);
  }
  .workspace-form label { display: block; margin-bottom: 9px; color: #8ca69d; font-size: 14px; font-weight: 650; }
  .workspace-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; }
  .workspace-row input {
    min-width: 0;
    height: 46px;
    padding: 0 14px;
    border: 1px solid rgba(212, 238, 231, .14);
    border-radius: 10px;
    outline: none;
    background: #071511;
    color: #d8e4e0;
    font: inherit;
    font-size: 15.5px;
  }
  .workspace-row input:focus { border-color: rgba(111, 227, 189, .42); box-shadow: 0 0 0 3px rgba(111, 227, 189, .055); }

  .connect {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    min-width: 150px;
    height: 46px;
    padding: 0 16px;
    border: 1px solid rgba(111, 227, 189, .23);
    border-radius: 10px;
    background: #174338;
    color: #d0efe4;
    font: inherit;
    font-size: 15px;
    font-weight: 680;
    cursor: pointer;
  }
  .connect:hover:not(:disabled) { background: #1b4c3f; }
  .connect:disabled { opacity: .48; cursor: not-allowed; }

  .spinner { width: 15px; height: 15px; border: 1.5px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: spin .8s linear infinite; }
  .workspace-error { margin-top: 10px; color: #e6a08b; font-size: 14px; line-height: 1.55; }
  .workspace-note { display: flex; align-items: flex-start; gap: 7px; margin-top: 11px; color: #617a72; font-size: 12.5px; line-height: 1.55; }

  .pairing-form { display: grid; gap: 12px; }
  .pairing-head { display: flex; align-items: center; justify-content: space-between; gap: 18px; }
  .pairing-head span { display: block; color: #69857c; font-size: 12px; font-weight: 700; }
  .pairing-head strong { display: block; margin-top: 5px; color: #d4e1dd; font-size: 15px; }
  .pairing-state { padding: 7px 10px; border: 1px solid rgba(212, 238, 231, .10); border-radius: 999px; color: #81968f; font-size: 12px; }
  .pairing-connect { width: max-content; }
  .pairing-relogin { width: max-content; background: transparent; color: #8fb3a8; }
  .pairing-relogin:hover:not(:disabled) { background: rgba(111, 227, 189, .07); }
  .fingerprint { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, monospace; word-break: break-all; }

  .status-grid {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    margin-top: 20px;
    border: 1px solid rgba(212, 238, 231, .08);
    border-radius: 11px;
    overflow: hidden;
  }
  .status-grid > div { min-width: 0; padding: 13px 14px; border-right: 1px solid rgba(212, 238, 231, .07); background: rgba(5, 17, 14, .32); }
  .status-grid > div:last-child { border-right: 0; }
  .status-grid span { display: block; color: #58736a; font-size: 11.5px; font-weight: 650; }
  .status-grid strong { display: block; margin-top: 6px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #c09a54; font-size: 14px; line-height: 1.25; }
  .status-grid strong.good { color: #71c3a7; }

  .actions { display: flex; gap: 10px; margin-top: 16px; }
  .actions button {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    height: 44px;
    padding: 0 16px;
    border-radius: 10px;
    font: inherit;
    font-size: 14.5px;
    font-weight: 650;
  }
  .refresh { border: 1px solid rgba(212, 238, 231, .12); background: #091a16; color: #9aafa8; cursor: pointer; }
  .cockpit { border: 0; background: #91aaa2; color: #12201c; }
  .cockpit:disabled { opacity: .4; }
  .privacy { display: flex; align-items: center; gap: 7px; margin-top: 14px; color: #61776f; font-size: 12.5px; }

  .spin { display: inline-flex; animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  @media (max-width: 1100px) {
    .main { grid-template-columns: 270px minmax(0, 1fr); }
    .rail { padding-left: 18px; padding-right: 18px; }
    .content { width: calc(100% - 48px); }
    .content h1 { font-size: 38px; }
    .status-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .status-grid > div { border-bottom: 1px solid rgba(212, 238, 231, .07); }
    .status-grid > div:nth-child(2n) { border-right: 0; }
  }

  @media (max-width: 860px) {
    .main { grid-template-columns: 1fr; }
    .rail { display: none; }
    .workspace-row { grid-template-columns: 1fr; }
    .connect { width: 100%; }
  }

  @media (prefers-reduced-motion: reduce) {
    .spin, .spinner { animation: none; }
  }
</style>
