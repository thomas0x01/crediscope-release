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
  先 setup-node + npm ci，供 jsdom 前端行为测试）+ `packaging-smoke`
  （windows：冻结 sidecar 构建+冒烟+mock 迷你分析）+ `status`（红绿回写
  主仓 commit，context=`source-ci`）。内置 **wiki 新鲜度检查**：
  wiki_ref 落后内容仓 main 时发 ::warning（不阻断）。
  触发：`gh workflow run ci.yml -R thomas0x01/crediscope-release -f source_ref=main`；
  每日 02:37 UTC 兜底跑 main
- **`build.yml`（Desktop Release）**：pytest 门禁 → mac/win 双包
  （smoke 接 mock 迷你分析）→ Release + R2 下载站。触发：
  `gh workflow run build.yml -R thomas0x01/crediscope-release -f source_ref=main -f release_tag=vX.Y.Z[-suffix]`
- **`deploy-demo.yml`**：demo 演示站部署（demo-site/ → R2 demo/），
  仅手动触发，与发版链路无关，不加自动触发

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
- `latest.json` / `page_state.json` 是下载站状态，由发版链路维护，
  手工改动前先搞清 updater 语义（主仓 desktop/ 有说明）
- 本仓 public：**任何文件提交前检查不含密钥/客户数据/私有路径**
