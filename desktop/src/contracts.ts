/** The complete renderer capability surface; credentials are never returned. */
export interface ConnectionInfo {
  id: string;
  kind: "owned" | "existing" | null;
  status: "disconnected" | "connecting" | "ready" | "stopping" | "failed";
  url?: string;
  error?: string;
  canChooseRecordingFile?: boolean;
}

export type ConnectionTarget = { kind: "owned" } | { kind: "existing"; url: string; token: string };
export type ResultsEvent = { type: "open" | "message" | "closed" | "error"; data?: string };
export interface DesktopApi {
  getConnection(): Promise<ConnectionInfo>;
  connect(target: ConnectionTarget): Promise<ConnectionInfo>;
  disconnect(): Promise<void>;
  onConnectionChanged(listener: (state: ConnectionInfo) => void): () => void;
  subscribeResults(connectionId: string, listener: (event: ResultsEvent) => void): () => void;
  chooseRecordingFile(): Promise<string | null>;
  setEditorState(state: { dirty: boolean }): void;
  setLanguage(language: "en" | "zh"): void;
  openLogs(): Promise<void>;
}
