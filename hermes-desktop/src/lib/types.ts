export type HealthState = 'healthy' | 'degraded' | 'offline' | 'updating';

export interface RuntimeComponent {
  id: 'cloud' | 'connector' | 'plugin' | 'core';
  name: string;
  detail: string;
  state: HealthState;
  latencyMs?: number;
}

export interface ProviderStatus {
  name: string;
  model: string;
  state: 'connected' | 'attention' | 'not-configured';
  note: string;
}

export interface RuntimeEvent {
  id: string;
  at: string;
  title: string;
  detail: string;
  tone: 'success' | 'neutral' | 'attention';
}

export interface RuntimeSnapshot {
  deviceName: string;
  profileName: string;
  platform: string;
  architecture: string;
  desktopVersion: string;
  runtimeVersion: string;
  runtimeGeneration: string;
  state: HealthState;
  workspaceAuthenticated: boolean;
  workspaceEndpoint?: string;
  workspaceUser?: string;
  devicePaired: boolean;
  devicePairingState: string;
  deviceCredentialFingerprint?: string;
  cloudConnected: boolean;
  agentReady: boolean;
  activeSessions: number;
  runningTasks: number;
  updateAvailable: boolean;
  updateVersion?: string;
  lastChecked: string;
  components: RuntimeComponent[];
  providers: ProviderStatus[];
  events: RuntimeEvent[];
}

export type NavigationKey =
  | 'overview'
  | 'agents'
  | 'sessions'
  | 'providers'
  | 'updates'
  | 'diagnostics';
