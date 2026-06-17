/**
 * 预加载脚本：在隔离上下文中向渲染进程（前端）安全暴露最小桥接 API。
 * 前端通过 window.desktop 判定是否运行在桌面客户端，并调用原生文件对话框，
 * 直接拿本地路径提交（避免上传大文件时的拷贝）。
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("desktop", {
  isElectron: true,
  /** 打开原生视频/音频文件对话框，返回所选文件绝对路径（取消返回 null）。 */
  pickVideo: () => ipcRenderer.invoke("pick-video"),
});
