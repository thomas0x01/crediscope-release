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

## 首次配置

启动后进入 **左下角 ⚙ 设置 → API 密钥**，需填写 3 项密钥（缺任一项无法开始分析）：

### 1. AI API Key

1. 打开 [platform.deepseek.com](https://platform.deepseek.com) 注册
2. 右上角头像 → API Keys → 创建后粘贴

也可使用 OpenAI 兼容接口：在「Base URL」和「Model」填入对应值即可（默认值为 DeepSeek 官方接口）。

### 2. QCC API Key

1. 打开 [agent.qcc.com](https://agent.qcc.com) 注册
2. 控制台 → API 管理 → 获取 Key

> 提示：若系统挂着代理/VPN，请将 `agent.qcc.com` 加入直连规则，否则企业信息查询可能因证书校验失败而取不到数据（表现为 CBR 行业对标为空）。

### 3. MinerU Token

1. 打开 [mineru.net](https://mineru.net) 注册
2. 控制台 → 获取 Token

三项填好后状态栏显示「已配置 · 环境就绪」即可开始新建分析。

## 说明

本仓库仅用于发布安装包与 CI 构建，不包含源代码。
