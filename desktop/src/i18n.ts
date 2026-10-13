/** App-owned native dialogs follow the renderer language. OS controls follow the OS locale. */
const zh = {
  "A connection or shutdown operation is already in progress.": "连接或关闭操作正在进行中。",
  "Could not read session results; they may still be held by the backend.": "无法读取会话结果；结果可能仍保存在后端中。",
  "Invalid session list.": "会话列表无效。",
  "Invalid export request.": "导出请求无效。",
  "Connection changed before exporting.": "导出前连接已更改。",
  "Export failed (HTTP {status}).": "导出失败（HTTP {status}）。",
  "Connection changed during export.": "导出过程中连接已更改。",
  "Backend was forcibly terminated.": "后端已被强制终止。",
  "The local backend is unavailable; its stop state could not be confirmed.": "本地后端不可用，无法确认其停止状态。",
  "Backend remains stopped; reconnect before sending commands.": "后端仍处于停止状态，请重新连接后再发送命令。",
  "Could not return to the stopped backend.": "无法返回已停止的后端。",
  "Shutdown failed.": "关闭失败。",
  "Choose a connection mode.": "请选择连接方式。",
  "Bundled Python target does not match this app.": "内置 Python 运行环境与此应用不匹配。",
  "Choose files on the backend machine in remote mode.": "远程模式下，请选择后端计算机上的文件。",
  "Connection changed while choosing a file.": "选择文件时连接已更改。",

  "The local backend is still stopping.": "本地后端仍在停止中。",
  "Continue waiting, or force this app's processes to exit. Forced exit can lose results and does not confirm that physical devices have stopped.": "请继续等待，或强制退出本应用的进程。强制退出可能导致结果丢失，也无法确认物理设备是否已停止。",
  "Continue waiting": "继续等待",
  "Force exit": "强制退出",
  "Discard unsaved configuration changes?": "放弃未保存的配置更改？",
  "Return to the editor to save changes. Nothing will be saved or applied automatically.": "请返回编辑器保存更改。系统不会自动保存或应用任何更改。",
  "Return to editor": "返回编辑器",
  "Discard changes": "放弃更改",
  "Disconnect from the remote backend?": "断开与远程后端的连接？",
  "The remote experiment and service will continue. Results remain in its memory and are lost if that service restarts.": "远程实验和服务将继续运行。结果保存在远程服务的内存中，服务重启后将丢失。",
  "Cancel": "取消",
  "Disconnect": "断开连接",
  "Stop the local backend?": "停止本地后端？",
  "Acquisition, stimulation and outputs will stop. You can export the final results before disconnecting.": "采集、刺激和输出将停止。断开连接前，您可以导出最终结果。",
  "Stop and continue": "停止并继续",
  "Normal shutdown could not be confirmed.": "无法确认是否已正常关闭。",
  "Unknown shutdown result.": "关闭结果未知。",
  "Keep window open": "保持窗口打开",
  "{count} session(s) have unexported results.": "有 {count} 个会话的结果尚未导出。",
  "The local backend stores results in memory. Export them before disconnecting.": "本地后端将结果保存在内存中，请在断开连接前导出。",
  "Export JSON": "导出 JSON",
  "Discard results": "放弃结果",
  "Cancel disconnect": "取消断开",
  "CNT recording": "CNT 录制文件",
  "Save results": "保存结果",
  "Save": "保存",
  "Choose recording": "选择录制文件",
  "Open": "打开",
  "Desktop operation failed.": "桌面操作失败。"
} as const;

export function desktopText(language: "en" | "zh", message: keyof typeof zh, params?: Record<string, string | number>): string {
  const template = language === "zh" ? zh[message] : message;
  return template.replace(/\{(\w+)\}/g, (match, key: string) => params?.[key] == null ? match : String(params[key]));
}
