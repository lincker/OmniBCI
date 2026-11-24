/** App-owned native dialogs follow the renderer language. OS controls follow the OS locale. */
const zh = {
  "A connection or shutdown operation is already in progress.": "连接或关闭操作正在进行中。",
  "Server was forcibly terminated.": "服务端已被强制终止。",
  "The local server is unavailable; its stop state could not be confirmed.": "本地服务端不可用，无法确认其停止状态。",
  "Server remains stopped; reconnect before sending commands.": "服务端仍处于停止状态，请重新连接后再发送命令。",
  "Could not return to the stopped server.": "无法返回已停止的服务端。",
  "Shutdown failed.": "关闭失败。",
  "Choose a connection mode.": "请选择连接方式。",
  "Bundled Python target does not match this app.": "内置 Python 运行环境与此应用不匹配。",
  "Choose a path on the server machine.": "请选择服务端计算机上的路径。",
  "Connection changed while choosing a file.": "选择文件时连接已更改。",

  "The local server is still stopping.": "本地服务端仍在停止中。",
  "Continue waiting, or force this app's processes to exit. Forced exit can lose results and does not confirm that physical devices have stopped.": "请继续等待，或强制退出本应用的进程。强制退出可能导致结果丢失，也无法确认物理设备是否已停止。",
  "Continue waiting": "继续等待",
  "Force exit": "强制退出",
  "Discard unsaved configuration changes?": "放弃未保存的配置更改？",
  "Return to the editor to save changes. Nothing will be saved or applied automatically.": "请返回编辑器保存更改。系统不会自动保存或应用任何更改。",
  "Return to editor": "返回编辑器",
  "Discard changes": "放弃更改",
  "Cancel": "取消",
  "Stop the local server?": "停止本地服务端？",
  "Acquisition, stimulation and outputs will stop. Live results will be cleared.": "采集、刺激和输出将停止，实时结果将清空。",
  "Stop and continue": "停止并继续",
  "Normal shutdown could not be confirmed.": "无法确认是否已正常关闭。",
  "Unknown shutdown result.": "关闭结果未知。",
  "Keep window open": "保持窗口打开",
  "CNT recording": "CNT 录制文件",
  "Choose recording": "选择录制文件",
  "Open": "打开",
  "Desktop operation failed.": "桌面操作失败。"
} as const;

export function desktopText(language: "en" | "zh", message: keyof typeof zh): string {
  return language === "zh" ? zh[message] : message;
}
