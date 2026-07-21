# Crediscope 信贷智能分析 — 下载

面向客户经理与审查员的 LLM 辅助信贷分析工作台（桌面版）。

## 下载最新版本

前往 [**Releases**](https://github.com/thomas0x01/crediscope-release/releases) 页面下载：

| 平台 | 文件 |
| --- | --- |
| macOS（Apple Silicon） | `Crediscope.LLM_*_aarch64.dmg` |
| Windows 安装包 | `Crediscope.LLM_*_x64-setup.exe` |
| Windows MSI | `Crediscope.LLM_*_x64_en-US.msi` |

## 安装说明

- **macOS**：本应用未做 Apple 公证。如提示"无法验证开发者"，请在「系统设置 → 隐私与安全性」中点"仍要打开"，或在终端执行：
  ```bash
  xattr -dr com.apple.quarantine /Applications/Crediscope\ LLM.app
  ```
- **Windows**：如 SmartScreen 提示，选择"更多信息 → 仍要运行"。

## 说明

本仓库仅用于发布安装包与 CI 构建，不包含源代码。
