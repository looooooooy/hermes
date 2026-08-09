#!/usr/bin/env node

import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import net from 'node:net';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const desktopRoot = resolve(scriptDir, '..');
const repoRoot = resolve(desktopRoot, '..');

function sleep(ms) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, ms));
}

function unixSocketReady(socketPath) {
  return new Promise((resolvePromise) => {
    const socket = net.createConnection(socketPath);
    let settled = false;
    const finish = (ready) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolvePromise(ready);
    };
    socket.setTimeout(300, () => finish(false));
    socket.once('connect', () => finish(true));
    socket.once('error', () => finish(false));
  });
}

async function ensureMacRuntimeManager() {
  if (process.platform !== 'darwin') return;

  const socketPath = join(
    homedir(),
    'Library',
    'Application Support',
    'Hermes',
    'state',
    'rm.sock',
  );

  if (await unixSocketReady(socketPath)) {
    console.log('[Hermes dev] Runtime Manager already ready.');
    return;
  }

  const runtimeRoot = join(repoRoot, 'hermes-runtime-manager');
  const runtimeBinary = join(runtimeRoot, 'target', 'debug', 'hermes-runtime-manager');
  let child;

  if (existsSync(runtimeBinary)) {
    child = spawn(runtimeBinary, ['serve-read-only'], {
      cwd: repoRoot,
      detached: true,
      stdio: 'ignore',
      env: process.env,
    });
  } else {
    child = spawn(
      'cargo',
      [
        'run',
        '--quiet',
        '--manifest-path',
        join(runtimeRoot, 'Cargo.toml'),
        '--',
        'serve-read-only',
      ],
      {
        cwd: repoRoot,
        detached: true,
        stdio: 'ignore',
        env: process.env,
      },
    );
  }
  child.unref();

  for (let attempt = 0; attempt < 150; attempt += 1) {
    if (await unixSocketReady(socketPath)) {
      console.log('[Hermes dev] Runtime Manager auto-started.');
      return;
    }
    await sleep(100);
  }

  throw new Error('Runtime Manager did not become ready within 15 seconds.');
}

async function main() {
  await ensureMacRuntimeManager();

  const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm';
  const vite = spawn(npm, ['run', 'dev'], {
    cwd: desktopRoot,
    stdio: 'inherit',
    env: process.env,
  });

  const forward = (signal) => {
    if (!vite.killed) vite.kill(signal);
  };
  process.on('SIGINT', () => forward('SIGINT'));
  process.on('SIGTERM', () => forward('SIGTERM'));

  vite.on('exit', (code, signal) => {
    if (signal) process.kill(process.pid, signal);
    process.exit(code ?? 0);
  });
}

main().catch((error) => {
  console.error(`[Hermes dev] ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
});
