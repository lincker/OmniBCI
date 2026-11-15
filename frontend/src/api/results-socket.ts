import { currentConnection, desktopBridge } from "../desktop";
import { verifyBrowserSession } from "./browser-session";
import type { ResultsEvent } from "../../../desktop/src/contracts";

export interface ResultsSocket { close: () => void }

/** Transport only; reconnect and snapshot recovery remain in useResultsFeed. */
export function openResultsSocket(listener: (event: ResultsEvent) => void): ResultsSocket {
  const desktop = desktopBridge();
  if (desktop) {
    const owner = currentConnection()?.id;
    if (!owner) throw new Error("No server connection selected.");
    let closed = false;
    const unsubscribe = desktop.subscribeResults(owner, (event) => {
      if (!closed && currentConnection()?.id === owner) listener(event);
    });
    return { close: () => {
      if (closed) return;
      closed = true;
      unsubscribe();
      listener({ type: "closed" });
    } };
  }
  let closed = false;
  let socket: WebSocket | undefined;
  void verifyBrowserSession().then((valid) => {
    if (closed) return;
    if (!valid) { listener({ type: "closed" }); return; }
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${protocol}://${location.host}/api/v1/ws/results`);
    socket.onopen = () => { if (!closed) listener({ type: "open" }); };
    socket.onmessage = (event) => { if (!closed) listener({ type: "message", data: String(event.data) }); };
    socket.onclose = () => {
      if (!closed) void verifyBrowserSession().then(() => { if (!closed) listener({ type: "closed" }); });
    };
    socket.onerror = () => { if (!closed) listener({ type: "error" }); };
  });
  return { close: () => {
    if (closed) return;
    closed = true;
    socket?.close();
    listener({ type: "closed" });
  } };
}
