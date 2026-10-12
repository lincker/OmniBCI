import { currentConnection, desktopBridge } from "../desktop";
import type { ResultsEvent } from "../../../desktop/src/contracts";

export interface ResultsSocket { close: () => void }

/** Transport only; reconnect and snapshot recovery remain in useResultsFeed. */
export function openResultsSocket(listener: (event: ResultsEvent) => void): ResultsSocket {
  const desktop = desktopBridge();
  if (desktop) {
    const owner = currentConnection()?.id;
    if (!owner) throw new Error("No backend connection selected.");
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
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/api/v1/ws/results`);
  socket.onopen = () => listener({ type: "open" });
  socket.onmessage = (event) => listener({ type: "message", data: String(event.data) });
  socket.onclose = () => listener({ type: "closed" });
  socket.onerror = () => listener({ type: "error" });
  return { close: () => socket.close() };
}
