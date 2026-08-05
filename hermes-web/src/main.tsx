import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { CloudHermesRuntime } from "./platform/web/realtime/CloudHermesRuntime";
import { HttpTicketProvider } from "./platform/web/realtime/HttpTicketProvider";
import { BrowserPasswordAuthClient } from "./platform/web/auth/BrowserPasswordAuthClient";
import { getOrCreateBrowserClientInstanceId } from "./platform/web/auth/BrowserClientInstanceId";
import { BrowserSessionCatalogClient } from "./platform/web/catalog/BrowserSessionCatalogClient";
import { ProductionApp } from "./production/ProductionApp";
import "./styles.css";

async function start(): Promise<void> {
  let application: React.ReactNode;
  if (import.meta.env.DEV) {
    const [{ createPreviewFixture }, { PreviewRuntimeAdapter }] = await Promise.all([
      import("./dev/fixtures"),
      import("./dev/PreviewRuntimeAdapter"),
    ]);
    application = <App initialState={createPreviewFixture()} runtime={new PreviewRuntimeAdapter()} />;
  } else {
    const params = new URLSearchParams(window.location.search);
    const clientInstanceId = getOrCreateBrowserClientInstanceId();
    const authClient = new BrowserPasswordAuthClient({
      loginEndpoint: new URL("/auth/password-login", window.location.origin).toString(),
      logoutEndpoint: new URL("/auth/logout", window.location.origin).toString(),
    });
    const catalogClient = new BrowserSessionCatalogClient({
      agentsEndpoint: new URL("/api/v1/agents", window.location.origin).toString(),
      sessionsEndpoint: new URL("/api/v1/agents", window.location.origin).toString(),
    });
    application = (
      <ProductionApp
        authClient={authClient}
        catalogClient={catalogClient}
        initialSessionKey={params.get("session") ?? ""}
        runtimeFactory={({ agentId, sessionId, sessionKey, profile }) => {
          const ticketProvider = new HttpTicketProvider({
            endpoint: new URL("/api/auth/ws-ticket", window.location.origin).toString(),
            clientInstanceId,
          });
          const websocketUrl = new URL("/api/ws", window.location.origin);
          websocketUrl.protocol = websocketUrl.protocol === "https:" ? "wss:" : "ws:";
          return new CloudHermesRuntime({
            websocketUrl: websocketUrl.toString(),
            agentId,
            sessionId,
            sessionKey,
            profile,
            ticketProvider,
            clientInstanceId,
          });
        }}
      />
    );
  }
  const root = document.getElementById("root");
  if (root === null) throw new Error("Hermes Web root element is missing");
  createRoot(root).render(
    <React.StrictMode>
      {application}
    </React.StrictMode>,
  );
}

void start();
