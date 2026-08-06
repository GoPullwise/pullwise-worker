# Reviewer Refactor Authority and Readiness

状态：`PROPOSED_INERT`

规范标识：`pullwise-reviewer-refactor/v1`

本文件是 `codex-sdk-reviewer-skill-worker-refactor-proposal.md` 的规范性配套文件，只定义权威接入、冲突消解和可执行就绪条件。它不修改当前 decision register，不授权 runtime、schema、Generate、benchmark、deployment、traffic、cutover 或 deletion。

## 1. 固定身份与解析规则

规范单元固定为 `reviewer-refactor-program`。文件名中保留 `proposal` 只为避免已有链接失效；标题、PR 合并、issue 状态、口头“继续”或本文落库都不产生决策权威。

一个可被执行器接受的规范快照必须同时绑定：

- 本规范标识与版本；
- `docs/reviewer-refactor/agent-entry.json` 的唯一入口、current card generation/profile 和 allowed read-only actions；
- `docs/reviewer-refactor/spec-manifest.json` 的 exact bytes SHA-256；
- 主规格和全部配套文件的 exact path/size/SHA-256；
- 当前 decision-register manifest/resolution digests；
- 四仓适用 `AGENTS.md` 的 exact path/SHA-256；
- `readiness.json` 中全部适用门的状态和直接证据；
- 目标 release、evidence generation 与 stage-advance identity。

这些输入中任一变化都会形成新规范快照；旧 advance 不得复用。配套文件只细化主规格，不覆盖主规格。发现同版本文件互相冲突时，verdict 固定为 `INDETERMINATE`，不得靠“更具体”“更新日期”或文件顺序静默选边。

## 2. 激活状态机

| 状态 | 可做 | 不可做 |
|---|---|---|
| `PROPOSED_INERT` | 只读审计、规范/fixture/verifier 自检、准备 inert decision packet | 被 runtime/build/release consumer 引用；修改 current authority |
| `GOVERNANCE_DRAFTED` | 仅执行 DRAFT exact write set 中的 non-consumed artifacts/tests | Generate、candidate、真实 benchmark、生产操作 |
| `GOVERNANCE_FROZEN` | 执行 RR-GOV FREEZE 明列的 replacement gate 范围 | 自动进入 Stage A 或扩大 write set |
| `ARCHITECTURE_FROZEN` | 在 signed advance 内执行 Stage A/B 的 exact packages | 未列 package、生产接入、隐式兼容路径 |
| `IMPLEMENTATION_READY` | 执行具备依赖 PASS 与 write-set lease 的离线/候选 package | 发布或生产流量 |
| `RELEASE_READY` | 仅按 exact release attestation/runbook 执行获授权阶段 | 任何 digest 漂移、mixed authority、越级 promotion |

状态只可由适用 append-only resolution、直接证据和 signed stage advance 推导，不得手工填写。当前固定为 `PROPOSED_INERT`。

当前 `agent-entry.json` 只允许 `verify-spec` 与 `inspect-current-gates`，并把 `next_card_id` 固定为 `COL-0D`。这不是 COL-0D 的执行授权：generation 1 的 24 张 card 全部是 `blocked/NOT_AUTHORIZED`。collector packet、collector install 和 formal GOV-0A 分别由 `COL-0D → COL-0F → GOV-0A` 表达，agent 不得省略任一步或把一般性“继续”当作 DRAFT/FREEZE。

第一份 stage-bound successor 至少为 generation 2，并以 `from_generation/from_manifest_sha256/authority_record_refs` 做 CAS。cards、entry、readiness 与 manifest 必须在同一 tracked transaction 更新；只改其中一项、给 blocked card 填命令、让未授权 card 离开 `NOT_AUTHORIZED` 或引用 synthetic path，均不产生有效 readiness。

## 3. 当前权威与 supersession 规则

在 RR 系列 FREEZE 生效前，以下仍是 current authority：

1. `contracts/agent-first/spec-decision-register.json` 中未被 supersede 的 resolution；
2. 四仓 path-scoped `AGENTS.md`；
3. `agent-first-worker-mvp-implementation-design.md` 与 `agent-first-worker-post-mvp-implementation-design.md` 在当前 normative unit 中的有效义务；
4. 当前 `implement-agent-first-worker` skill 的路由与停止边界；
5. 当前生成包、gate、release barrier 和生产实现。

RR packet 必须逐 decision、逐义务处理，禁止用“与新方案冲突的全部旧决定”这类开放集合。候选处理矩阵如下；它只是 packet 编制标准，不是已发生的 supersession：

| 类别 | 现有 decisions | RR packet 必须记录 |
|---|---|---|
| 原样保留 | D1、D23、D24 barrier、D26、D27、D28、D31 | old digest、保留的 exact obligation、新测试和新 artifact 中的落点 |
| 专业化替换 | D22 | 保留 sealed corpus/seed/owner/三态/可比性/canary；替换通用 Agent 指标并绑定 RR-EVAL |
| 保留安全不变量、替换实现 | D3、D7、D13、D21 | 分别保留 R0/R1/无 effect、deadline 不延长、cancel precedence、单 current contract；逐条撤销旧机制 |
| 明确替换 authority | D5、D9 | Task/Worker result authority 分别替换为 Server terminal CAS version/global terminal authority |
| 撤销旧专用义务 | D6、D8、D10-D12、D14-D20、D25、D29-D30、D32-D33 | old digest、被撤销字段/状态/consumer/test、替代物和 absence evidence；D29 的旧 family 被撤销，但 layered atomic-root 不变量由 D28 和新 root contract继续承担 |
| 部分处理 | D41 | 逐句标注 retain/supersede；未明确 supersede 的禁止项继续有效 |

同一旧义务不得在两个类别中重复出现。若一个 decision 同时有保留和撤销内容，packet 必须按稳定 obligation id 拆分，不能只给 decision-level `supersede`。

## 4. 四仓 instruction conflict inventory

Stage A 前必须生成 `instruction-conflict-plan/v1`。至少覆盖以下已知冲突：

| 仓库 | 当前规则 | 目标候选 | 处置要求 |
|---|---|---|---|
| Worker | standalone Codex CLI 默认 `latest` | exact SDK/CLI/App Server/runtime tuple | RR-TRUST 绑定 exact bytes 后更新 current rule |
| Worker | root coordinator、30 phases、bounded fanout | one root thread/main turn、最多一次 format repair | RR-SCOPE 逐条 supersede，不能保留双路径 |
| Worker | v1/Agent Kernel 当前实现与旧设计路由 | Reviewer v2 thin shell | RR-SCOPE/TRUTH/CUT 指明每条旧 consumer 的删除阶段 |
| Server | 当前 terminal/lease/route/table authority | v2 CAS、new tables、private/public v2 contract | RR-TRUTH/CUT 绑定 DDL、route 和 migration exact targets |
| Server | 旧 phase billing/artifact consumers | `semantic_review_started` 与 v2 artifacts | RR-TRUTH/CUT 绑定删除与幂等 fixture |
| Web | v1 DTO/phase/artifact fallback | public `review-run/v2` only | RR-CUT 绑定 generated pin 与 absence check |
| Admin | concurrency/bundle/assignment settings | single-slot Worker，无 fanout config | RR-SCOPE/CUT 绑定 schema/API/UI/test 删除 |
| Worker skill routing | 仅旧 MVP/Post-MVP 设计 | 本规范 manifest-bound route | 只能在 RR FREEZE 与 instruction remediation 同一治理切片更新 |

plan 每项必须含 current bytes/digest、冲突句稳定 id、目标 bytes/digest、授权 decision digest、write set、验证命令和 rollback。应用后生成 `instruction-conflict-report/v1`；`unresolved != 0` 时 Stage A 不能 PASS。

## 5. `SPEC-READY` 门

以下十二门是把本文当作实施依据的必要条件，不是生产发布门：

| Gate | PASS 条件 |
|---|---|
| `SPEC-READY-01-AUTHORITY` | collector 专用 DRAFT/FREEZE 只授权 collector/GOV-0A，随后普通 RR-GOV DRAFT/FREEZE exact-bind GOV-0A 与本规范 manifest，并创建 `reviewer-refactor-program` normative unit；无模糊确认或权限合并 |
| `SPEC-READY-02-INSTRUCTIONS` | 四仓 conflict plan 完整；获授权 bytes 应用后 report `unresolved=0` |
| `SPEC-READY-03-MANIFEST` | agent entry、spec manifest、24-card schema/index、fixtures、schema/lifecycle/tamper self-tests 和每个 verifier/test source ≤400 行全部 PASS |
| `SPEC-READY-04-BOOTSTRAP` | GOV-0A collector 有 canonical path/bytes/digest/command、clean-room test 和 no-clobber evidence；在此之前不得声称 GOV-0A 可自动执行 |
| `SPEC-READY-05-EVIDENCE` | evidence DAG、volatile-field policy、same-generation deterministic verifier 和跨-generation stable projection均有 fixtures |
| `SPEC-READY-06-CONTRACT` | 一个 layered atomic root 闭合 private/public/shared schemas、registries、fixtures 和 wrapper generation |
| `SPEC-READY-07-SECURITY` | source/artifact/control wire、principal、tenant、credential、sandbox 和 redaction contract 闭合 |
| `SPEC-READY-08-SKILL` | Skill rubric、finding admission、旧 prompt migration ledger、target-instruction trust policy 和 eval mapping 闭合 |
| `SPEC-READY-09-CONTEXT` | exact tokenizer/transcript budget preflight 可证明全部 mandatory bytes 与输出 reserve 可容纳；否则 NO-GO |
| `SPEC-READY-10-CAPABILITY` | exact SDK/CLI/App Server path 的 early surface probe 有结论；不支持的机制没有进入冻结架构 |
| `SPEC-READY-11-RELEASE` | 首发 stable baseline、canary capacity/telemetry/formulas、signer/key、rollback 与 retention policy 闭合 |
| `SPEC-READY-12-EXECUTION` | 24 个 package 从 COL-0D 到 PROM-1 全覆盖；当前 generation 1 可保持 blocked，但真正获授权的 successor 必须为每张 bound card 提供真实 write set、artifact dependency、red/green/focused/full/CI commands、产物和 principal binding |

任一门 `FAIL/INDETERMINATE/NOT_AUTHORIZED` 时，implementation readiness 为 false。不得用整体文档长度、人工评审通过或另一门 PASS 抵消。

## 6. 当前非规范性快照

截至 2026-08-06 r4 审计开始的只读快照：Worker HEAD `6a45784`，current decision-register check 报告 40 resolved、0 pending、`ready=true`；contract baseline 为 `compatible`，default absence 为 `ratchet_clean=true/legacy_absent=false`，strict absence 因 108 个 live failure 与 `strict_catalog_self_reference` 为 `INDETERMINATE`。共享环境在文档修改期间自动推进了 Worker `main/origin/main`，因此这里不把后续 HEAD 当作冻结证据。该 `ready` 只描述现有 register，不包含任何 RR decision；HEAD、register、指令或 spec bytes 变化即使本段过期。本段不得被 stage advance 引用，执行证据必须来自新的不可覆盖 generation。

## 7. 激活后的必要接线

只有 RR-GOV/RR-SCOPE/RR-TRUST/RR-TRUTH/RR-EVAL/RR-CUT 逐项 FREEZE 后，获授权治理切片才可：

1. 在四仓 `AGENTS.md` 写入 manifest-bound current rule，并明确旧段落已 superseded；
2. 更新 implementation skill，使其先校验本规范 manifest/readiness/stage advance，再路由到相应 work package；
3. 给旧 MVP/Post-MVP 设计加 content-addressed supersession overlay，不改写历史证据；
4. 把 `reviewer-refactor-program` 加入 decision register 的 normative units，并双向绑定实际 decision ids；
5. 在 CI 中启用唯一 spec verifier/ledger aggregator，禁止并存宽松 gate。

上述接线本身必须位于 exact write set，并产出 unresolved=0 报告；本文存在不等于已经完成接线。
