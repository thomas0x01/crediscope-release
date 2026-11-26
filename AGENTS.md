# Agent 操作守则 — crediscope-release

> 本仓是**公开构建/发布枢纽**（public，Actions 免费）。不含产品源码；
> Release 即对外下载站。三仓拓扑见主仓 crediscope-llm 的 AGENTS.md
> 「仓库拓扑」节；本文件写本仓的工作流与红线。

## 本仓角色

- 主仓 crediscope-llm 是 private 且 Actions 额度耗尽——CI 与发布全部
  由本仓承载：构建时用只读 fine-grained PAT（secret `BUILD_PAT`）临时
  checkout 私有仓源码到 runner，构建完 runner 即销毁，源码不进本仓
- wiki 知识同理：按主仓 `wiki_ref` 锚定的 ref checkout 内容仓
  crediscope-wiki（private，BUILD_PAT 需对其有读权限），摆回
  `src/crediscope-llm/wiki/` 后再打包/测试

## Workflows

- **`ci.yml`（Source CI）**：主仓 CI。`check`（ubuntu：py_compile +
  import 验证 + 全量 pytest；被测 ref 含 crediscope-llm/package.json 时
  先 setup-node + npm ci，供 jsdom 前端行为测试；含 tests/e2e_browser/
  时再加 playwright chromium 浏览器冒烟——连通性巡检 + Origin 守卫负向
  + renderMd XSS）+ `packaging-smoke`
  （windows：冻结 sidecar 构建+冒烟+mock 迷你分析）+ `pytest-windows`
  （windows：平台专项 pytest 子集——路径/编码/子进程终止/冻结模拟等，
  无需 poppler/node/LLM key）+ `packaging-smoke-macos`（macos：冻结
  sidecar 构建+冒烟+mock 迷你分析，镜像 build-macos 的 sidecar 部分）
  + `status`（红绿回写
  主仓 commit，context=`source-ci`）。内置 **wiki 新鲜度检查**：
  wiki_ref 落后内容仓 main 时发 ::warning（不阻断）。
  触发：`gh workflow run ci.yml -R thomas0x01/crediscope-release -f source_ref=main`；
  每日 02:37 UTC 兜底跑 main
- **`build.yml`（Desktop Release）** 固定测试过的应用源码 SHA 和构建号。原生 runner 从 NSIS、DMG 和更新压缩包回读程序、后端和架构。`publish-manifest` 核对同批收据、平台观测时间、五件文件摘要和双平台 updater 签名。R2 `releases/<tag>/candidate-publication.json` 固定已接纳的候选和内测选择，并按运行号和重试号拒绝旧批。GitHub 更换附件时先挂 `candidate-publication-pending.json`，再撤下旧清单。新清单和五件制品核对完成后解除暂停。同批续发保留已校验的附件。正式候选先发布为 prerelease，带后缀的候选可以更新下载页内测区。
- **`promote.yml`** 从应用仓主线 `docs/testing/golden-runs/` 读取两份候选回执，从候选 SHA 读取验签器、公钥、渲染器和页面资源。转正在共享 `release-publication` 并发组内预检，拒绝暂停中的候选。发行日期取候选清单附件时间的 UTC 日期。逐站读回 `latest.json`、GitHub Latest、首页、内测指针及仓库镜像后才报告成功。
- **`read-feedback.yml`（查询产品反馈）**：在本仓执行 `bash scripts/read_recent_feedback.sh 2`，按最后更新时间跨状态返回最近两单的摘要和完整事件。参数范围 1–10。维护者用已登录的 `gh` 发起查询，一次性公钥供 runner 加密，密文制品保留一天；脚本在本机解密并将 JSON 写到标准输出，结束时删除临时密钥。只读查询仅允许仓库所有者从 main 手动触发。
- **`feedback-followup.yml`（原单追问）**：仓库所有者用 `python3 scripts/send_feedback_followup.py fb_<完整ID>` 从标准输入传入已经确认的追问正文。脚本以临时 Actions secret 传递请求，结束时删除。工作流在 main 核对原单、追加 `needs_info` 后逐字回读。同单已有相同追问时跳过发送。用上条只读入口查看后续补充。
- **`deploy-worker.yml`**：反馈 Worker 测试 → CF Worker/KV/路由部署 → 维护读取验证。
  main 上 `desktop/scripts/cf_worker/track.js`、对应测试或本 workflow 变化时自动执行，
  也可手动 `gh workflow run deploy-worker.yml -R thomas0x01/crediscope-release -f source_ref=main`；
  安装版验收可另传 `-f feedback_id=fb_xxx` 按 ID 取回完整证据（缺省只列未处理反馈）。
  仅允许 main 部署。secrets：`CF_API_TOKEN`（Worker Scripts/KV/Routes Edit）、
  `STATS_KEY`（统计读取）、`FEEDBACK_MAINTAINER_KEY`（反馈正本读取和维护事件）
- **`deploy-demo.yml`**：demo 演示站部署（demo-site/ → R2 demo/），
  仅手动触发，与发版链路无关，不加自动触发
- **`winget.yml`（winget 跟版）**：正式版发出后手动触发
  `gh workflow run winget.yml -f release_tag=vX.Y.Z`——
  `scripts/winget_update.py` 读取已转正 Release 的 `candidate-manifest.json`，复核 Release 文件及摘要后据确切 Windows setup 渲染三件 manifest（InstallerUrl 使用 dl.thomas0x00.uk 的候选路径，境内
  可达；**不用**第三方 winget-releaser，它会把 URL 改写成 GitHub 直链）
  同版本串行提交。续跑核对 fork 分支差异、文件内容和已有 PR，仅增加缺失的 YAML，同一候选的开放或已合并 PR 可以复用。带后缀 tag 一律拒绝
  （prerelease 不进 winget）。secret：`WINGET_TOKEN`（classic PAT，
  public_repo）。ARP 元数据 2026-10-13 VM 注册表实测：HKCU
  `Crediscope LLM` 键，Publisher `crediscope`，每用户安装。

## Secrets 清单（变更只在本仓重设）

- `BUILD_PAT`：fine-grained 只读 PAT，仓库选择必须包含 crediscope-llm
  **和 crediscope-wiki**（2026-08-03 起），否则 checkout 401
- `RESOURCE_KEY_PASSPHRASE`：资源加密口令，每次构建从明文资源重新加密
- `TAURI_SIGNING_PRIVATE_KEY`(+`_PASSWORD`)：updater 签名
- R2/Cloudflare 分发链路 secrets：见主仓 docs/境内分发架构.md

## 红线

- **发布卫生**：带后缀 tag（test/beta/rc）一律 prerelease（build.yml
  已内置规则）；干净版本号才标 Latest；测试 release 定期清理
  （`gh release delete <tag> --cleanup-tag`，留最新 1-2 个），正式版永不清
- 主仓/内容仓的**写权限凭证不进本仓 secrets**（BUILD_PAT 只读是刻意的：
  本仓 public，泄露面必须最小）
- `latest.json` / `page_state.json` 是正式转正后的仓库镜像。正式版本取 GitHub Latest，内测候选取 R2 当前首页，`release-beta.json` 随首页修复。候选制品存 `releases/<tag>/<candidate_id>/`，保留旧路径供已发布链接使用。显式 `pre_tag` 在构建时固定内测候选 ID。
- 应用仓先合入原生制品身份、生成器与候选回执，再切换枢纽工作流。切换前让旧发布运行全部退出，再核对 GitHub Latest、R2 和 `updates`。历史正式版延用既有首页和更新链接。新转正和 winget 需要第二版候选清单，旧候选须重建并重新取证。历史正式版补提 winget 留给独立审计，既有 winget 清单可以继续使用。
- Windows J9 首次转正前核对实测起点版本与当前有效的 GitHub Latest。Latest 已指向同一候选 ID 时，按起点 tag 读取历史正式发布，核对返回 tag，并要求非草稿、非预发布。起点有 v2 候选清单时，`from_running_executables` 必须与已验证清单的 Windows `desktop`、`sidecar` 两项 SHA-256 精确相等。若有 R2 publication 记录，须为 ready 且与 GitHub 清单一致。
- Windows J9 存量起点在 GitHub 无候选清单、无暂停标记且 R2 无 publication 记录时，沿用正式 tag 和已上传的历史安装件核对。该口径仅证明版本，无旧执行件身份凭据。存在任一新式发布凭据或暂停标记时，按新式规则核验，缺失或损坏的清单阻断转正与续跑。上述核对均先于发布写入。macOS 原生更新仍要求从较旧版本升级。
- 若转正中断，先核对 R2 `latest.json`、`updates` 和 GitHub Latest，再使用同一候选及回执重试。原生构建需要重跑时重跑全部构建任务，单独重试发布任务可沿用同批产物。共享并发组只保证串行，GitHub Actions 可替换排队任务，取消的运行需重新触发。
- 本仓 public：**任何文件提交前检查不含密钥/客户数据/私有路径**
