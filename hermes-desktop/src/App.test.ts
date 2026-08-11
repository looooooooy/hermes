// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, unmount } from 'svelte';
import App from './App.svelte';
import { mockRuntimeSnapshot } from './lib/mock-runtime';
import type { RuntimeSnapshot } from './lib/types';

const { invokeMock } = vi.hoisted(() => ({
  invokeMock: vi.fn(),
}));

vi.mock('@tauri-apps/api/core', () => ({
  invoke: invokeMock,
}));

function unpairedSnapshot(): RuntimeSnapshot {
  return {
    ...mockRuntimeSnapshot,
    workspaceAuthenticated: true,
    devicePaired: false,
    devicePairingState: 'unpaired',
    deviceCredentialFingerprint: undefined,
  };
}

function buttonWithText(label: string): HTMLButtonElement | undefined {
  return Array.from(document.querySelectorAll<HTMLButtonElement>('button'))
    .find((button) => button.textContent?.includes(label));
}

describe('workspace reauthentication from device pairing', () => {
  let component: ReturnType<typeof mount> | undefined;

  beforeEach(() => {
    document.body.innerHTML = '<main id="test-root"></main>';
    invokeMock.mockReset();
  });

  afterEach(async () => {
    if (component) {
      await unmount(component);
      component = undefined;
    }
    document.body.innerHTML = '';
  });

  it('shows the existing browser sign-in action after a reauthentication-required pairing failure', async () => {
    const snapshot = unpairedSnapshot();
    invokeMock.mockImplementation(async (command: string) => {
      if (command === 'runtime_snapshot') return snapshot;
      if (command === 'device_pair') {
        throw {
          code: 'workspace_reauth_required',
          message: 'Hermes workspace sign-in has expired. Sign in again.',
        };
      }
      if (command === 'workspace_connect') {
        return {
          authenticated: true,
          endpoint: snapshot.workspaceEndpoint,
          userId: snapshot.workspaceUser,
        };
      }
      throw new Error(`Unexpected command: ${command}`);
    });

    component = mount(App, {
      target: document.getElementById('test-root')!,
    });

    await vi.waitFor(() => expect(buttonWithText('绑定这台 Mac')).toBeDefined());
    buttonWithText('绑定这台 Mac')!.click();

    await vi.waitFor(() => expect(buttonWithText('浏览器登录')).toBeDefined());
    buttonWithText('浏览器登录')!.click();

    await vi.waitFor(() => {
      expect(invokeMock).toHaveBeenCalledWith('workspace_connect', {
        endpoint: snapshot.workspaceEndpoint,
      });
    });
    await vi.waitFor(() => expect(buttonWithText('绑定这台 Mac')).toBeDefined());
    expect(document.body.textContent).not.toContain('企业工作空间登录已过期，请重新登录。');
    expect(buttonWithText('浏览器登录')).toBeUndefined();
  });

  it('keeps a generic pairing failure on the device step while offering explicit workspace re-login', async () => {
    const snapshot = unpairedSnapshot();
    let completeWorkspaceConnect: ((value: {
      authenticated: boolean;
      endpoint: string;
      userId: string | undefined;
    }) => void) | undefined;
    const workspaceConnect = new Promise<{
      authenticated: boolean;
      endpoint: string;
      userId: string | undefined;
    }>((resolve) => {
      completeWorkspaceConnect = resolve;
    });
    invokeMock.mockImplementation(async (command: string) => {
      if (command === 'runtime_snapshot') return snapshot;
      if (command === 'device_pair') {
        throw { code: 'device_pairing_failed', message: 'Unrelated pairing failure.' };
      }
      if (command === 'workspace_connect') return workspaceConnect;
      throw new Error(`Unexpected command: ${command}`);
    });

    component = mount(App, {
      target: document.getElementById('test-root')!,
    });

    await vi.waitFor(() => expect(buttonWithText('绑定这台 Mac')).toBeDefined());
    buttonWithText('绑定这台 Mac')!.click();

    await vi.waitFor(() => {
      expect(document.body.textContent).toContain('设备绑定没有完成，请重新尝试。');
    });
    expect(buttonWithText('绑定这台 Mac')).toBeDefined();
    expect(buttonWithText('重新登录企业账号')).toBeDefined();
    expect(invokeMock.mock.calls.filter(([command]) => command === 'device_pair')).toHaveLength(1);

    buttonWithText('重新登录企业账号')!.click();

    await vi.waitFor(() => {
      expect(invokeMock).toHaveBeenCalledWith('workspace_connect', {
        endpoint: snapshot.workspaceEndpoint,
      });
      expect(buttonWithText('重新登录企业账号')?.disabled).toBe(true);
    });
    expect(invokeMock.mock.calls.filter(([command]) => command === 'device_pair')).toHaveLength(1);

    completeWorkspaceConnect!({
      authenticated: true,
      endpoint: snapshot.workspaceEndpoint!,
      userId: snapshot.workspaceUser,
    });

    await vi.waitFor(() => {
      expect(buttonWithText('绑定这台 Mac')).toBeDefined();
      expect(buttonWithText('重新登录企业账号')?.disabled).toBe(false);
      expect(document.body.textContent).not.toContain('设备绑定没有完成，请重新尝试。');
    });
    expect(invokeMock.mock.calls.filter(([command]) => command === 'device_pair')).toHaveLength(1);
  });

  it('shows workspace re-login failures on the device step without retrying pairing', async () => {
    const snapshot = unpairedSnapshot();
    invokeMock.mockImplementation(async (command: string) => {
      if (command === 'runtime_snapshot') return snapshot;
      if (command === 'workspace_connect') {
        throw new Error('企业账号重新登录测试失败。');
      }
      if (command === 'device_pair') {
        throw { code: 'device_pairing_failed', message: 'Fresh pairing failure.' };
      }
      throw new Error(`Unexpected command: ${command}`);
    });

    component = mount(App, {
      target: document.getElementById('test-root')!,
    });

    await vi.waitFor(() => expect(buttonWithText('重新登录企业账号')).toBeDefined());
    buttonWithText('重新登录企业账号')!.click();

    await vi.waitFor(() => {
      expect(document.body.textContent).toContain('企业账号重新登录测试失败。');
    });
    expect(buttonWithText('绑定这台 Mac')).toBeDefined();
    expect(invokeMock.mock.calls.filter(([command]) => command === 'device_pair')).toHaveLength(0);

    buttonWithText('绑定这台 Mac')!.click();

    await vi.waitFor(() => {
      expect(document.body.textContent).not.toContain('企业账号重新登录测试失败。');
      expect(document.body.textContent).toContain('设备绑定没有完成，请重新尝试。');
    });
    expect(invokeMock.mock.calls.filter(([command]) => command === 'device_pair')).toHaveLength(1);
  });

  it('keeps Bind visible and disabled while workspace re-login is in progress', async () => {
    const snapshot = unpairedSnapshot();
    const pendingWorkspaceConnect = new Promise(() => {});
    invokeMock.mockImplementation(async (command: string) => {
      if (command === 'runtime_snapshot') return snapshot;
      if (command === 'workspace_connect') return pendingWorkspaceConnect;
      if (command === 'device_pair') return undefined;
      throw new Error(`Unexpected command: ${command}`);
    });

    component = mount(App, {
      target: document.getElementById('test-root')!,
    });

    await vi.waitFor(() => expect(buttonWithText('重新登录企业账号')).toBeDefined());
    buttonWithText('重新登录企业账号')!.click();

    await vi.waitFor(() => {
      expect(invokeMock).toHaveBeenCalledWith('workspace_connect', {
        endpoint: snapshot.workspaceEndpoint,
      });
      expect(buttonWithText('正在重新登录企业账号')).toBeDefined();
    });
    const bindButton = buttonWithText('绑定这台 Mac')!;
    expect.soft(bindButton.disabled).toBe(true);

    bindButton.click();

    expect(invokeMock.mock.calls.filter(([command]) => command === 'device_pair')).toHaveLength(0);
  });

  it('ignores legacy English categories when the stable code is a generic pairing failure', async () => {
    const snapshot = unpairedSnapshot();
    invokeMock.mockImplementation(async (command: string) => {
      if (command === 'runtime_snapshot') return snapshot;
      if (command === 'device_pair') {
        throw {
          code: 'device_pairing_failed',
          message: 'pairing helper failed safely after an unrelated failure',
        };
      }
      throw new Error(`Unexpected command: ${command}`);
    });

    component = mount(App, {
      target: document.getElementById('test-root')!,
    });

    await vi.waitFor(() => expect(buttonWithText('绑定这台 Mac')).toBeDefined());
    buttonWithText('绑定这台 Mac')!.click();

    await vi.waitFor(() => {
      expect(document.body.textContent).toContain('设备绑定没有完成，请重新尝试。');
    });
    expect(document.body.textContent).not.toContain('设备绑定组件没有完成初始化');
    expect(buttonWithText('浏览器登录')).toBeUndefined();
  });
});
