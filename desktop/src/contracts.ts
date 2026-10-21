/** The complete renderer capability surface; credentials are never returned. */
export interface ConnectionInfo {
  id: string;
  kind: "local" | "remote" | null;
  status: "disconnected" | "connecting" | "ready" | "stopping" | "failed";
  url?: string;
  error?: string;
}

export type ConnectionTarget = { kind: "local" } | { kind: "remote"; url: string; token: string };
export type ResultsEvent = { type: "open" | "message" | "closed" | "error"; data?: string };
export interface DesktopApi {
  getConnection(): Promise<ConnectionInfo>;
  connect(target: ConnectionTarget): Promise<ConnectionInfo>;
  disconnect(): Promise<void>;
  onConnectionChanged(listener: (state: ConnectionInfo) => void): () => void;
  subscribeResults(connectionId: string, listener: (event: ResultsEvent) => void): () => void;
  chooseRecordingFile(): Promise<string | null>;
  setEditorState(state: { dirty: boolean; busy: boolean }): void;
  setLanguage(language: "en" | "zh"): void;
  openLogs(): Promise<void>;
  requestQuit(): Promise<void>;
}
