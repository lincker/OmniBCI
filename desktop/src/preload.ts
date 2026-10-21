import { contextBridge, ipcRenderer } from "electron";
import type { ConnectionInfo, ConnectionTarget, DesktopApi, ResultsEvent } from "./contracts.js";

let nextSubscription = 0;
const api: DesktopApi = {
  getConnection: () => ipcRenderer.invoke("connection:get"),
  connect: (target: ConnectionTarget) => ipcRenderer.invoke("connection:connect", target),
  disconnect: () => ipcRenderer.invoke("connection:disconnect"),
  onConnectionChanged(listener: (state: ConnectionInfo) => void) {
    const receive = (_event: unknown, state: ConnectionInfo) => listener(state);
    ipcRenderer.on("connection:changed", receive);
    return () => ipcRenderer.removeListener("connection:changed", receive);
  },
  subscribeResults(connectionId: string, listener: (event: ResultsEvent) => void) {
    const subscription = String(++nextSubscription);
    const receive = (_event: unknown, value: { subscription: string; event: ResultsEvent }) => {
      if (value.subscription === subscription) listener(value.event);
    };
    ipcRenderer.on("results:event", receive);
    ipcRenderer.send("results:subscribe", { subscription, connectionId });
    return () => {
      ipcRenderer.removeListener("results:event", receive);
      ipcRenderer.send("results:unsubscribe", subscription);
    };
  },
  chooseRecordingFile: () => ipcRenderer.invoke("recording:choose-file"),
  setEditorState: state => ipcRenderer.send("editor:state", state),
  setLanguage: language => ipcRenderer.send("app:language", language),
  openLogs: () => ipcRenderer.invoke("logs:open"),
  requestQuit: () => ipcRenderer.invoke("app:quit"),
};
contextBridge.exposeInMainWorld("omnibciDesktop", api);
