# Codex SDK + Reviewer Skill Worker 重构执行规格

状态：Candidate Implementation Standard / Governance-gated / `PROPOSED_INERT` / 未授权生产切换

版本：2026-08-05-r3（cross-repository execution standard hardening）

范围：`pullwise-worker`、`pullwise-server`、`pullwise-web`、`pullwise-admin`

| 元数据 | 固定值 |
|---|---|
| 规范标识 | `pullwise-reviewer-refactor/v1` |
| 候选 normative unit | `reviewer-refactor-program` |
| 当前成熟度 | `CANDIDATE_NOT_ACTIVE` |
| 当前可执行边界 | 现有只读检查与 inert 规范自检；`SPEC-READY-04-BOOTSTRAP` PASS 前不得声称 GOV-0A collector 已可执行 |
| 机器入口 | `docs/reviewer-refactor/spec-manifest.json`、`readiness.json`、`execution-cards.json` |

本规格与下列配套文件构成同一个 content-addressed 候选规范单元：

- `docs/reviewer-refactor/authority-and-readiness.md`
- `docs/reviewer-refactor/evidence-and-determinism.md`
- `docs/reviewer-refactor/runtime-contract-and-security.md`
- `docs/reviewer-refactor/skill-context-and-evaluation.md`
- `docs/reviewer-refactor/operations-and-execution.md`

配套文件只细化本规格；发现同版本冲突时整体为 `INDETERMINATE`，不得以文件顺序静默覆盖。文件名中的 `proposal` 仅为链接稳定性，不表示本文落库、合并或被引用即获得 authority。

## 0. 文档地位与授权边界

本文把原架构 Proposal 和 2026-08-05 的补充评审意见合并为可执行的跨仓重构规格，规定目标架构、契约、工作包、测试、阶段门、切换、回滚和删除标准。

- `contracts/agent-first/spec-decision-register.json` 仍是当前决策权威。冲突的生产实现必须先由 append-only 新决策逐项 supersede。
- 当前 D41 仍禁止 D24 激活、部署、生产流量、真实 benchmark、canary、cutover 和 legacy 删除。
- `SPEC-READY-04-BOOTSTRAP` PASS 前可立即执行的只有现有只读命令与 inert 规范自检；正式 GOV-0A collector 尚不存在，不得把人工目录或手写 JSON 冒充标准 evidence generation。collector 交付后，Stage 0A 仍只允许只读取证、不可覆盖 evidence 和 exact-bound inert docs/decision packet。即使某个 tracked baseline/test/script 修复不改变既有语义，也必须先证明当前 resolution/`AGENTS.md` exact 授权该 write set；本文不提供该授权。Stage 0B 若替换 gate 或改变 completion 语义，必须先有 append-only 决策。Stage A 需架构所有者参与；Stage B 需新决策明确授权离线 candidate；Stage C–F 在前序门和显式授权前均为 `NO-GO`。
- 生产始终只有一个 current contract；不得增加 shadow authority、fallback、双写/双读、协议协商、downgrade 或 compatibility mode。

| 阶段 | 当前可执行 | 额外授权 |
|---|---|---|
| Stage 0A 证据/provenance | 部分：现有只读审计可做；formal collector 在 `SPEC-READY-04` 前不可做 | collector exact bytes/self-test + RR-GOV bootstrap binding；tracked docs apply 必须 byte-match proposal；baseline/test/script 写入另需现行 exact write-set 授权 |
| Stage 0B gate replacement | 否 | RR-GOV draft/freeze 记录先冻结 history/live catalog、三态、目标 bytes/digests 和替换义务 |
| Stage A 决策/契约冻结 | 否 | Stage 0B verified PASS + signed advance；RR-SCOPE/TRUST/TRUTH/EVAL/CUT 逐项 draft→freeze |
| Stage B 离线 candidate | 否 | 新决策取代 D41 停止边界 |
| Stage C 最小生产壳候选 | 否 | Stage B2 signed PASS + C advance；仍不接生产 |
| Stage D 跨仓切换准备 | 否 | Stage C signed PASS + exact-one generation resolution + D advance |
| Stage E clean cutover/canary | 否 | 离线 attestation + strict absence + D24 + 发布授权 |
| Stage F 全量/收尾 | 否 | canary PASS |

### 0.1 当前执行状态与状态词

决策注册表的 `ready` 只表示“当前注册表没有未解决的 active decision”，不表示本文 RR-* packets（早期讨论简称 D42–D47）已获授权，也不表示可以开始 candidate、benchmark 或生产实现。本文和后续 issue 只使用以下状态词：

| 状态 | 含义 |
|---|---|
| `NOT_AUTHORIZED` | 前置 append-only 决策或阶段授权缺失；禁止写入该阶段的实现代码或运行该阶段的真实操作 |
| `READY` | 前置授权与输入证据齐全，可以开始，但尚无完成声明 |
| `IN_PROGRESS` | 已开始且保留 red/green/命令/产物证据 |
| `PASS` | 所有适用验收项有当前、直接、可复算证据 |
| `FAIL` | 已证明至少一个适用门不满足 |
| `INDETERMINATE` | 缺证、证据损坏、样本不足、不可比或 gate 自身无法给出确定结论 |

截至本文版本，只有现有只读审计与本候选规范的 inert self-check 可进入 `READY`；formal `GOV-0A` package 因 collector 未交付而仍是 `NOT_IMPLEMENTED/NOT_AUTHORIZED`。任何 tracked baseline/test/script 修复仍为 `NOT_AUTHORIZED`，除非另有适用 resolution exact 列出该 write set。`GOV-0B` 及后续工作包全部是 `NOT_AUTHORIZED`。`FAIL` 与 `INDETERMINATE` 都不能被当作软通过；二者也不能通过修改 expected、排除项或删除历史证据转成 `PASS`。

Decision resolution 只设置允许到达的最大边界，不自动推进阶段。从 Stage A 起，每次进入下一阶段还必须有第 0.4 节的 signed stage-advance record，引用前一阶段 exact PASS evidence、目标 release generation digest、允许的 work packages/write sets 和禁止项；前置 evidence stale、输入 root 或 release generation digest 改变时授权自动失效。Stage 0A→0B 只使用第 0.3 节的一次性 bootstrap 规则和 RR-GOV freeze resolution，不能把该例外扩展到后续阶段。

### 0.2 规范性产物与证据包

本文是实施总规格，不取代决策注册表、仓库指令和生成契约。实施时按下列优先级解析冲突：

1. append-only decision register 中适用且未被 supersede 的 resolution；
2. 每个目标文件适用的 repository-root/path-scoped `AGENTS.md` current instructions；
3. Server-owned canonical schemas/registries 及其 signed manifest；
4. 由 canonical source 原子生成并 exact-pin 的 Worker/Web/Admin artifacts；
5. 本文、阶段 runbook、issue 与测试计划；
6. 非规范性说明和历史文档。

前两级冲突时不靠上述排序静默选择：目标 resolution 必须逐条列出受影响的 `AGENTS.md` 规则，并在获授权的同一治理切片把它更新为 current rule 或明确标成不可执行历史。Stage A 入场前先生成 `instruction-conflict-plan/v1`，枚举四仓适用 `AGENTS.md` 的 current path/digest、冲突句、目标 decision digest、proposed exact target bytes/digest、处置和验证命令；只有被 DRAFT resolution 与 Stage A advance exact 列出的 instruction-remediation write set 可以暂时携带 `unresolved`，且只能应用 plan 中的 bytes。应用后生成 `instruction-conflict-report/v1`；该报告仍有 `unresolved` 时 Stage A 不得 PASS，任何 B–F package 均为 `NOT_AUTHORIZED`。决策不自动改写仓库指令，旧 prose 也不能覆盖有效 resolution。

每个阶段使用同一个 `release_id`，并以递增、不可复用的 `evidence_generation` 输出不可覆盖的 `reviewer-refactor-evidence/<release_id>/<stage>/<work-package>/<generation>/` 证据包。证据包至少包含：`inputs.json`、`commands.jsonl`、`tests.json`、`artifacts.json`、`environment.json`、`decision-bindings.json`、`result.json` 和 `manifest.json`；需要授权的 generation 另有 detached `manifest.sig`。`result.json` 只能是上述六态之一，并逐项引用直接证据；重跑创建新 generation，禁止原地改写旧失败。CI 保存 exact manifest bytes，release attestation 只引用已签名 generation 的 out-of-band manifest digest。

`manifest.json` 使用 `reviewer-refactor-evidence-manifest/v1`，按 canonical relative path 的 UTF-8 bytes 排序，列出除自身和 detached signature 外每个文件的 size/SHA-256/media type，并计算 domain-separated `content_root`。manifest 不列自己的 hash，任何被引用处直接保存 exact manifest bytes 的 SHA-256；`manifest.sig` 只签 exact manifest bytes 及固定 signing purpose，因此不存在 manifest/signature 自引用。未知文件、缺文件、重复/case-colliding path、symlink/reparse/non-regular file、size/digest 漂移均为 `INDETERMINATE`。exit `0` 只对应 `PASS`，exit `1` 只对应 `FAIL`，`NOT_AUTHORIZED/READY/IN_PROGRESS/INDETERMINATE` 均为 exit `2`。

evidence CLI 必须显式接收 canonical absolute `--evidence-root`；该 root 位于四个 repository worktree、model-visible filesystem、source snapshot 和 validation copy 之外，拒绝 symlink/reparse、宽松权限和已存在的目标 generation。标识符 grammar 固定为：`release_id=[a-z0-9][a-z0-9._-]{0,63}`，stage 仅 `0A|0B|A|B|B2|C|D|E|F`，work-package id 仅 `[A-Z][A-Z0-9-]{1,31}`，generation 为无前导零的十进制 `1..2147483647`；验证后才拼接路径，禁止 caller 提供相对 path 或覆盖旧目录。表格中的 `WEB-1/ADM-1` 只是并列简写，实际始终是两个 package ids/generations。CI artifact/object-store URI 与本地 root 的映射进入 `environment.json`，但 manifest 只使用相对 path，因而换机器可复算。命令环境只记录 allowlist；secret-bearing raw env/token 不进入证据包。

所有进入 digest/signature 的 JSON body 使用仓库现行 Pullwise JCS Profile 1：UTF-8、无 BOM、NFC、ASCII object keys、safe integers、拒绝 float/duplicate key，并由 canonical serializer 产生 exact bytes；verifier 必须 parse、按同 profile 重编码并 byte-compare，不能接受语义相同但 bytes 不同的 JSON。bootstrap 若目标 canonicalizer 尚未实现，仍以 packet 中保存的 exact bytes/SHA-256 为权威，EVD-0 必须在 back-validation 时完成 canonical byte check；失败保持 `INDETERMINATE`。

tracked inert decision bundles 固定在 `pullwise-worker/docs/reviewer-refactor-decision-drafts/<packet>/<generation>/`，使用 `reviewer-refactor-decision-draft-manifest/v1` 并遵守相同无自引用规则。该目录只允许 packet、target mapping、proposed bytes/fixtures 和说明；任何 package/import/generator/runtime/release-check consumer 都使 draft validation FAIL。FREEZE 后 canonical artifact 进入其 owner repository，旧 draft 仍作为 immutable provenance，不转成运行时 source。

所有尚不存在的目标命令、schema、registry 和 evidence writer 都是相应切片的交付物，不是当前已经可用的事实。文档中的“应运行”不能替代真实命令输出。

### 0.3 一次性 evidence bootstrap

当前没有本文目标的 evidence writer/aggregator，不能要求它先验证自己的创建授权。唯一 bootstrap 如下：

1. `GOV-0A` 把现有只读治理命令的原始 stdout/stderr/exit、命令参数、四仓 HEAD/dirty 状态、环境版本和所有输入/输出 SHA-256 放入上述目录形状；此时 `result.json.status` 只能是 `READY/IN_PROGRESS`，另以非授权字段 `provisional_result=PASS|FAIL|INDETERMINATE` 记录直接命令的暂定归纳，不得称为 signed stage PASS。EVD-0 back-validation 后生成新 generation，才可把六态 `status` 设为 verified final result。
2. `GOV-0A` 同时在仓外 evidence generation 的 `proposed-inert/` 中产生 inert `RR-GOV` decision draft bundle 的 exact proposed bytes。它不在采集期间写入任一 worktree，也不被 runtime、generator、schema registry 或 CI release gate 消费；bundle 必须包含拟议目标 path、exact proposed bytes/digests、failing/pass fixtures、旧义务映射、write set 和禁止项。bootstrap 发布后，只有在当前 authority 明确允许 inert docs write set 时，才可把这些 bytes 作为独立 tracked-doc action 写到第 0.6 节路径；复制前后必须 byte-compare，且合并后用新 generation 重新建立 clean snapshot。
3. architecture/governance owner 可依据 exact bootstrap manifest digest 签发 RR-GOV draft/freeze resolution；这是唯一可以在目标 aggregator 不存在时推进的决策。它只授权 `GOV-0B` 的 `EVD-0` 和 replacement gate write sets，继续禁止 candidate、Generate、benchmark、部署、流量和删除。
4. `EVD-0` 是 `GOV-0B` 的第一个实现包：先以 tamper/missing/replay/self-check failing fixtures 实现最小 evidence schema/writer/verifier、detached-signature verifier 和 work-package ledger；它不得依赖尚未创建的 v2 runtime/contract。
5. `EVD-0` 必须重新导入并验证 GOV-0A bootstrap generation、自己的 red/green/direct CI evidence 和 GOV-0B 其余包；对同一 immutable generation 连续两次只读验证必须给出 byte-identical verification verdict/exit。fresh capture 必须新建 generation，只比较 `stable-projection/v1`，不得要求含 generation/timestamp/raw timing 的 manifest/result byte-identical。详细 volatile allowlist 与 fixtures 见 `docs/reviewer-refactor/evidence-and-determinism.md`。back-validation 将 GOV-0A 的 provisional result 变成 `verified PASS/FAIL/INDETERMINATE`，不得强改为 PASS。GOV-0A 非 PASS 时，只有 RR-GOV-FREEZE-A 已逐项接受该 exact bootstrap digest、解释为何治理 replacement 可关闭不确定性且 EVD-0 证明所有 replacement obligations PASS，GOV-0B 才可在这一次 bootstrap 例外下 PASS。

若 bootstrap bytes 缺失、被覆盖、无法复算或 RR-GOV 没有 exact freeze binding，状态保持 `INDETERMINATE/NOT_AUTHORIZED`。此例外不允许手工把任何后续 work package 改成 PASS，也不允许 EVD-0 以“验证器由自己生成”为由省略独立 fixtures、代码审查或 CI evidence。

### 0.4 Signed stage-advance contract

`EVD-0` 必须交付并验证 `reviewer-refactor-stage-advance/v1` 与 `reviewer-refactor-stage-advance-policy/v1`。record 至少包含：

- `record_id/release_id/from_stage/to_stage/evidence_generation`；
- 前一阶段 exact PASS manifest URI/digest/content root、适用 decision resolution digests，以及 Stage A 入场用 `instruction-conflict-plan` digest 或后续入场用 unresolved=0 的 `instruction-conflict-report` digest；
- 本阶段 immutable input root、目标 release generation digest、允许的 work-package ids、repository/write sets、命令/环境边界和明确禁止项；
- `issued_at/expires_at`、signing purpose、所需 signer roles/key ids 和 stage-advance policy version。

`stage-advance.json` 只含上述 canonical unsigned body，不含自身 digest 或 signatures；引用者保存 exact body SHA-256。一个或多个 `stage-advance.<role>.sig` detached documents 只列 body digest、role/key id 和对 exact body bytes + fixed signing purpose 的 signature，不含自身 digest。任何把 digest/signature 填回被 hash 的 body 的实现都因自引用而无效。

policy 冻结每条边所需角色与职责分离：Stage A/B/C 至少 architecture/governance owner；B2 另需 benchmark owner；D 另需 release operator；E/F 另需 release 与 deployment operator。它还必须冻结签名算法/版本、public-key format、key id derivation、domain separator、threshold/order、trust-root digest、revocation/rotation/expiry 和 clock-skew policy。实际身份与 key 来自受控 trust registry，不能写进 Agent input 或由自由文本推断。现有 Server production release-trust registry 尚不接受该 schema/purpose；EVD-0 必须在 Worker governance/evidence contract 下交付独立、offline-only 的 trust registry/purpose/schema/role verifier 及 valid/invalid/revoked/expired fixtures，不得借此修改生产 release trust 或 runtime。若 Stage D 需要 Server 消费同一 record，必须在 CON/SRV 的 decision/write set 中显式集成并证明 parity；在 offline verifier 通过前不存在有效 stage advance。

验证顺序固定为 schema/canonical body → detached signature/purpose/role/revocation/expiry → decision 与适用 instruction conflict plan/report binding → previous PASS manifest → input/release generation digest → requested work package/write set。Stage A advance 只能授权 plan 中的 draft/remediation paths；B–F advance 必须引用 unresolved=0 的 report。exact record replay 返回同一 verdict；同 `record_id` 不同 bytes、stale/revoked/expired evidence、越界 write set 或任一 digest 改变均为 `NOT_AUTHORIZED`/exit `2`，不得降为 warning。stage advance 不赋予 decision resolution 未允许的权限，也不能授权未列出的副作用。

### 0.5 GOV-0A bootstrap 执行契约

本节把“可立即执行的只读取证”收敛为一个闭合、可交接的 provisional bootstrap；它不创建实现授权，不修改 tracked source/test/script，不替代 EVD-0，也不把当前 gate 的成功或失败改写成新的语义。bootstrap 采集进程只可在四仓之外的显式 `--evidence-root` 创建新 generation；第 0.6 节 packet 先作为该 generation 内的 proposed bytes 生成，不能在同一次输入快照中写回 Worker。把 exact proposed bytes 应用到已获准的 inert docs 路径是采集完成后的独立 tracked-doc action；应用前后 digest 不同、混入其他 worktree 改动或未在合并后重新取得 clean generation 均为 `INDETERMINATE`。

#### 0.5.1 标识、目录与原子性

执行前由 operator 选择符合第 0.2 节 grammar 的 `release_id` 和未使用的正整数 `generation`；stage 固定为 `0A`，work package 固定为 `GOV-0A`。`--evidence-root` 表示名为 `reviewer-refactor-evidence` 的证据根本身而不是其父目录，canonical target 为：

```text
<evidence-root>/<release_id>/0A/GOV-0A/<generation>/
  inputs.json
  commands.jsonl
  tests.json
  artifacts.json
  environment.json
  decision-bindings.json
  result.json
  raw/
    <ordinal>-<command-id>.stdout.bin
    <ordinal>-<command-id>.stderr.bin
    <ordinal>-<command-id>.exit.json
  derived/
    slice0-provenance.json
    packaging-pin-provenance.json
    deletion-inventory.json
  proposed-inert/
    reviewer-refactor-decision-drafts/<packet>/<packet-generation>/
      packet.json
      targets.json
      validation-commands.json
      fixtures/
        manifest.json
        <fixture-id>/<fixture-relative-path>
      proposed/<repo-id>/<canonical-target-relative-path>
      manifest.json
  manifest.json
```

- `<evidence-root>` 必须是 canonical absolute path，位于四仓 worktree、Git object store、source/validation/model-visible roots 之外；目标 generation 及其任一父级 symlink/reparse 都拒绝。
- generation 以 owner-only（POSIX mode `0700`；Windows 为当前 service identity 独占且关闭继承的 ACL）私有 sibling staging directory 创建；验证所有 bytes 后用 no-clobber rename 发布。POSIX 使用可证明 no-replace 的 rename + file/directory fsync；Windows 使用不带 replace 的同卷 move，并在 move 前 flush 每个 file handle、在可用时请求 write-through。目标已经存在、跨卷、平台原子/持久性语义不可证明、权限过宽或目标类型不确定时不覆盖，结果为 `INDETERMINATE`。
- raw stdout/stderr 保留进程返回的原始 bytes，不做换行、encoding 或 redaction 后再 hash；若原始输出含 secret，整个 generation 不得发布，先删除未发布 staging、修正命令环境并使用新 generation。不得把脱敏后 bytes 冒充原始输出。
- 所有 path 在 JSON 中使用相对 generation root 的 canonical POSIX path。真实 repository/evidence absolute path 只进入 `environment.json.repository_roots`/`evidence_root`，不得进入 manifest path。
- generation 的允许文件集只包含上图固定文件、每个 catalog command 的三个 raw files，以及由 `artifacts.json` 明列且受第 0.6 节 bundle manifest 闭合的 `proposed-inert/` files。其他文件、alternate data stream、hardlink、device 或 socket 均为 `INDETERMINATE`。
- Stage 0A 不产生 `manifest.sig`，`result.json.status` 最终固定为 `IN_PROGRESS`；只有 EVD-0 back-validation 生成的新 generation 才能给出 verified final status。bootstrap helper 即使完整写出 provisional package，也返回 exit `2`，不得以 exit `0` 暗示 stage PASS。

#### 0.5.2 Bootstrap canonical bytes

Stage 0A 允许复用当前只读的 `pullwise_worker.agent_kernel_canonical.canonical_bytes` 产生 Pullwise JCS Profile 1 bytes，但必须把该模块的 path、Git blob id、file SHA-256 和 Python runtime 记入 `inputs.json`。它只是被 exact-pin 的 bootstrap serializer，不成为 Reviewer v2 runtime authority；EVD-0 必须用自己的实现 parse、重编码并 byte-compare。

- `inputs.json` exact keys：`schema_id/release_id/stage/work_package/generation/workspace_snapshot/command_catalog/input_files/bootstrap_serializer`。`workspace_snapshot` exact keys 为 `captured_before/captured_after/repositories`；每个 repository exact keys 为 `repo_id/head_before/head_after/status_before_sha256/status_after_sha256/clean_before/clean_after`。`command_catalog` 每项 exact keys 为 `ordinal/command_id/cwd_repo/argv/timeout_seconds/max_stdout_bytes/max_stderr_bytes`。`input_files` 每项 exact keys 为 `repo_id/path/role/size_before/sha256_before/size_after/sha256_after`。`bootstrap_serializer` exact keys 为 `repo_id/path/git_blob_id/file_sha256/python_executable/python_version`。
- `commands.jsonl` 每行是一个以 LF 终止的 canonical JSON object，exact keys：`schema_id/ordinal/command_id/cwd_repo/argv/started_at/finished_at/exit_code/stdout_path/stdout_sha256/stderr_path/stderr_sha256/exit_record_path/exit_record_sha256`。`ordinal` 从 1 连续递增；`argv` 是无 shell 解释的 string array；`exit_code` 仅在 launch failure 时为 `null`，其他情况是 signed 32-bit integer；环境只来自第 0.5.3 节 allowlist。
- 每个 `raw/*.exit.json` exact keys 为 `schema_id/ordinal/command_id/exit_code/termination_kind`；`termination_kind` 仅 `exited/timeout/signaled/launch_failed`，且必须与 command entry 一致。即使 launch failure，没有 stdout/stderr bytes 也要创建零长度 raw files 并保存其 SHA-256。
- `tests.json` exact keys：`schema_id/checks`。每个 check exact keys：`check_id/source_command_ids/observed_exits/observed_status/classification/reason_codes/report_sha256s`；每个 `observed_exits` item exact keys 为 `command_id/exit_code`，derived-only check 使用空 array；`observed_status` 为底层 exact status string 或 `null`；`report_sha256s` 是零个或多个 raw structured report digests。`classification` 仅 `PASS/FAIL/INDETERMINATE`，保留底层 gate 的原始 status，不用预期值覆盖。`check_id` 固定为 `REPOSITORY-CLEAN/INPUT-STABILITY/DECISION/SLICE0/V1-CONTRACT/ABSENCE-RATCHET/ABSENCE-STRICT/PACKAGING-PIN/DELETION-INVENTORY/AUTHORITY`。
- `artifacts.json` exact keys：`schema_id/artifacts`。每个 artifact exact keys：`artifact_id/path/media_type/size_bytes/sha256/role`；包括 raw outputs、provenance、四仓 deletion inventory 和 inert RR-GOV bundle manifest，不含 manifest 自身。
- `derived/slice0-provenance.json` exact keys 为 `schema_id/decision_refs/producer/expected/actual/first_observed_commit/classification/reason_codes`。`producer` exact keys 为 `path/file_sha256/version/generation`；`expected`/`actual` 各为 `path/line_count/sha256`；`first_observed_commit` 为 40 位 lowercase Git object id 或 `null`。`classification` 仅 `MECHANICAL_SYNC_CANDIDATE/REPLACEMENT_OBLIGATION/INDETERMINATE`，不得由 collector 自动把 candidate 当作 write authorization。
- `derived/packaging-pin-provenance.json` exact keys 为 `schema_id/producers/installed_distribution/wheels/classification/reason_codes`。每个 producer exact keys 为 `producer_id/source_path/source_sha256/build_argv/declared_requirement/supported`；installed distribution 为 `null` 或 exact keys `python_executable/distribution_name/version/metadata_sha256`；每个 wheel exact keys 为 `path/size_bytes/sha256/requires_dist`。`classification` 仅 `CONSISTENT/PACKAGING_PIN_DRIFT/INDETERMINATE`。
- `derived/deletion-inventory.json` exact keys 为 `schema_id/repositories/signatures`。每个 repository exact keys 为 `repo_id/head`；每个 signature exact keys 为 `repo_id/catalog_id/path/state/size_bytes/sha256/consumer_ids`，`state` 仅 `present/absent`，absent 时 size/digest 为 `null`。它只记录现有 frozen catalog 的 observed population，不增加排除项或改写 catalog。
- `environment.json` exact keys：`schema_id/captured_at/evidence_root/repository_roots/host_os/shell/python/git/allowlisted_environment/ci_mapping`。`repository_roots` 每项 exact keys 为 `repo_id/absolute_path`；`host_os` exact keys 为 `platform/release/architecture`；`shell/python/git` 各为 `executable/version/file_sha256`，无法安全读取 executable bytes 时 `file_sha256=null`；`allowlisted_environment` 每项 exact keys 为 `name/value`。`ci_mapping` 为 `null` 或 exact keys `provider/repository/run_id/job_id/artifact_uri`，不能伪造 CI run。
- `decision-bindings.json` exact keys：`schema_id/register_path/register_sha256/register_status/d41_resolution_sha256/agents/documents/authority_status`。`agents`/`documents` 每项 exact keys 为 `repo_id/path/size_bytes/sha256`，按 repo id + canonical path 排序；`authority_status` 必须明确 `read_only_and_inert_docs_only`。
- `result.json` exact keys：`schema_id/status/provisional_result/reason_codes/check_refs/artifact_refs/started_at/finished_at`。`status` 只可为 `READY/IN_PROGRESS`；已发布 generation 使用 `IN_PROGRESS`。`check_refs`/`artifact_refs` 分别是去重排序的 check/artifact id arrays。先判 evidence integrity：缺输出、input/HEAD/status 漂移、pre-existing dirty worktree、无法分类或命令不可运行为 `INDETERMINATE`；完整证据中任一 check 为 `INDETERMINATE` 时 provisional result 也是 `INDETERMINATE`，否则任一 check 为 `FAIL` 时为 `FAIL`，全部为 `PASS` 才为 `PASS`。已证明覆盖旧 evidence、越界写入或伪造输入直接为 `FAIL`。provisional `PASS` 仍不等于 verified stage PASS。
- `manifest.json` exact keys：`schema_id/release_id/stage/work_package/generation/profile/files/content_root`；`profile` 固定 `bootstrap`。`files` 排除 `manifest.json`/detached signatures，按 path UTF-8 bytes 排序，每项 exact keys 为 `path/size_bytes/sha256/media_type`。`content_root = SHA-256(pullwise:reviewer-refactor-evidence-content-root/v1\0 || JCS(files))`，以 64 位 lowercase hex 保存；manifest 本身也使用 JCS exact bytes，外部引用保存其普通 SHA-256。

`schema_id` 只接受下列精确映射：`inputs.json=reviewer-refactor-bootstrap-inputs/v1`、`commands.jsonl` entry=`reviewer-refactor-bootstrap-command/v1`、`raw/*.exit.json=reviewer-refactor-bootstrap-exit/v1`、`tests.json=reviewer-refactor-bootstrap-tests/v1`、`artifacts.json=reviewer-refactor-bootstrap-artifacts/v1`、`environment.json=reviewer-refactor-bootstrap-environment/v1`、`decision-bindings.json=reviewer-refactor-bootstrap-decision-bindings/v1`、`result.json=reviewer-refactor-bootstrap-result/v1`、`derived/slice0-provenance.json=reviewer-refactor-slice0-provenance/v1`、`derived/packaging-pin-provenance.json=reviewer-refactor-packaging-pin-provenance/v1`、`derived/deletion-inventory.json=reviewer-refactor-deletion-inventory/v1`、`manifest.json=reviewer-refactor-evidence-manifest/v1`。所有 arrays 除 `argv`/`build_argv` 外按其主 id 的 UTF-8 bytes 去重排序；这两个 argv fields 保持执行顺序。未知 key、重复 key、重复/case-colliding path、非安全整数、float、非 NFC string、非 ASCII object key 或语义相同但 bytes 非 canonical 均使 bootstrap `INDETERMINATE`。

除各字段更窄约束外，所有 id 使用 `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`，digest 是 64 位 lowercase hex，size 是 `0..9007199254740991`，时间是带 `Z` 的 UTC RFC 3339 seconds，relative path 是无 `.`/`..`/空 segment、反斜杠或 percent-encoded separator 的 NFC POSIX path。`provisional_result` 仅 `PASS/FAIL/INDETERMINATE`；artifact `role` 仅 `raw_stdout/raw_stderr/raw_exit/structured_report/provenance/deletion_inventory/decision_bundle`。任何 required string 为空、nullable 规则之外的 `null`、未知 enum 或 path escaping 都是 schema invalid。

#### 0.5.3 固定只读命令目录

GOV-0A 只执行下列 logical command ids；实现必须以 argv array 直接启动，不接受 caller shell fragment、额外 path、测试 node、timeout 或排除项。每个 repository command 的 `cwd_repo` 使用 `worker/server/web/admin` id，由 `environment.json.repository_roots` 映射到 absolute root。

| Command id | cwd | logical argv/义务 | timeout / 每流上限 |
|---|---|---|---|
| `ENV-PYTHON` | worker | `python -B --version`；同时记录 executable/version/digest（可得时） | 30s / 1 MiB |
| `ENV-GIT` | worker | `git --version` | 30s / 1 MiB |
| `REPO-<id>-HEAD` | 各仓 | `git rev-parse HEAD` | 30s / 1 MiB |
| `REPO-<id>-STATUS` | 各仓 | `git status --porcelain=v2 --branch` | 60s / 8 MiB |
| `GOV-DECISION` | worker | `python -B scripts/agent_first_decision_register.py check --repo-root .` | 120s / 16 MiB |
| `GOV-SLICE0` | worker | `python -B scripts/agent_first_slice0_baseline.py check --repo-root .` | 300s / 32 MiB |
| `GOV-V1-CONTRACT` | worker | `python -B scripts/verify_agent_first_contract_baseline.py check --workspace-root ..` | 600s / 64 MiB |
| `GOV-ABSENCE-RATCHET` | worker | `python -B scripts/verify_agent_first_legacy_absence.py --workspace-root ..` | 600s / 64 MiB |
| `GOV-ABSENCE-STRICT` | worker | 同上并加 `--require-absent`；只记录当前三态，不要求通过 | 600s / 64 MiB |

`REPO-<id>-*` 中的 id 精确展开为 `WORKER/SERVER/WEB/ADMIN`，`cwd_repo` 仍使用对应小写值。ordinal 固定为：两个 ENV commands → 按 `WORKER/SERVER/WEB/ADMIN` 顺序各自 HEAD 后 STATUS → 五个 GOV commands 按表中顺序；ordinal 的文件名部分使用无前导零十进制。任何省略、重排或额外 command 均为 catalog mismatch。

`logical argv` 的 `python`/`git` 在初始 snapshot 前解析一次；实际 `commands.jsonl.argv[0]` 必须是该 executable 的 canonical absolute path，后续解析结果变化为 input drift。子进程从空环境开始，只允许继承 `PATH/PATHEXT/SystemRoot/WINDIR/ComSpec/LANG/LC_ALL/TZ` 中存在且通过 secret scan 的值，并固定加入 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`、`PYTHONDONTWRITEBYTECODE=1`、`GIT_TERMINAL_PROMPT=0`、`GCM_INTERACTIVE=Never`；Windows 环境名按这里的 canonical casing 去重，任何其他 inherited variable 都拒绝。`TEMP/TMP/TMPDIR` 指向 generation staging directory 的 sibling private temp，命令结束必须为空并删除，不能进入 published generation。

命令前后分别读取并 byte-hash decision register、四仓适用 `AGENTS.md`、Slice-0/legacy/absence manifests、两个 packaging metadata 文件、第 0.6 节 packet inputs 和本规格；命令结束后 exact re-read。任一 input 变化、四仓 status 变化或 HEAD 变化都使 generation `INDETERMINATE`。canonical bootstrap 要求四仓在初始 snapshot 前 clean；dirty 状态仍可保存 raw discovery，但不得发布 `provisional_result=PASS`。命令的 timeout、spawn failure、zero-test、signal/exception 和 output truncation 使用 closed reason 记录，不能删除该 command entry；达到 byte cap 时先保存已读 raw bytes，再标记 `output_truncated`，不得把截断 digest当作完整输出。

bootstrap 自身的 `reason_codes` 只允许：`bootstrap.authority_violation/bootstrap.command_exception/bootstrap.command_launch_failed/bootstrap.command_signaled/bootstrap.command_timeout/bootstrap.dirty_worktree/bootstrap.input_drift/bootstrap.nonatomic_publish/bootstrap.output_truncated/bootstrap.path_unsafe/bootstrap.schema_invalid/bootstrap.secret_detected/bootstrap.serializer_mismatch/bootstrap.unclassified_exit/bootstrap.unexpected_file/bootstrap.zero_test/packaging.pin_drift`。底层 gate reason 不改写，编码为 `gate:<check-id>:<percent-encoded-original-code>`，且 original code 必须能在该 check 绑定的 raw structured report 中逐字找到；不得由 collector 发明新 gate reason。

执行顺序固定为：验证 identifiers/root → 初始 input/HEAD/status snapshot → environment commands → decision/Slice-0/contract/default absence → strict absence → packaging/provenance/inventory 只读派生 → 第二次 input/HEAD/status snapshot → result → manifest → no-clobber publish。任何重跑使用新 generation；不在失败 generation 内续写。

### 0.6 Current decision-register v1 兼容桥

RR-GOV 必须先使用现有 `pullwise-agent-first-spec-decision-register/v1` 完成 DRAFT/FREEZE 授权，不能为了记录替换 gate 的决策而预先替换 gate。Stage 0A 不增加 register top-level/decision/resolution keys，不修改 `DECISION_KEYS`、`SLICES` 或 authority catalog；本节把逻辑 RR record 定义为“一个现有 v1 decision entry + 一个 exact-bound inert packet bundle”。第 11 节所称 `record_phase/write set/target digests` 等扩展字段全部位于 packet，不能硬塞进 v1 decision object。

#### 0.6.1 Inert packet bundle

每个 bundle 的 canonical tracked target 固定在 `docs/reviewer-refactor-decision-drafts/<packet>/<generation>/`；`<packet>` grammar 为 `[a-z][a-z0-9-]{1,31}`，本次 bootstrap 只允许 `rr-gov`，generation 使用第 0.2 节相同整数 grammar。GOV-0A 先在 evidence 的 `proposed-inert/reviewer-refactor-decision-drafts/...` 产生以下 exact bundle-relative bytes；tracked action 只做 no-transform byte copy，因此 bundle manifest 不因根路径变化而改变：

```text
packet.json
targets.json
validation-commands.json
fixtures/manifest.json
fixtures/<fixture-id>/<fixture-relative-path>
proposed/<repo-id>/<canonical-target-relative-path>
manifest.json
```

`packet.json` 的 `schema_id` 固定为 `reviewer-refactor-decision-packet/v1`，exact keys 为 `schema_id/packet_id/generation/record_phase/option_id/exact_confirmation_text/decider_roles/provenance/supersession_intent/authorized_repositories/authorized_write_set/forbidden_actions/required_inputs/required_fixtures/next_confirmation`；`record_phase` 仅 `DRAFT_A/FREEZE_A`。`provenance` exact keys 为 `bootstrap_ref/spec_ref/created_at/created_by_role`；每个 `supersession_intent` exact keys 为 `decision_id/action/reason`，`action` 仅 `retain/supersede`；`next_confirmation` 为 `null` 或 exact keys `record_phase/option_id/exact_confirmation_text`。其余 plural fields 都是 UTF-8 byte-sorted unique strings；repository 固定为 `worker/server/web/admin`，write-set string 固定为 `<repo-id>:<canonical-posix-relative-path>`。

`decider_roles` 只可包含 `user/architecture_owner` 且至少一个；`created_by_role` 只可为现有 v1 catalog 的 `user/architecture_owner/operator`，recorder/operator 不能替代 decider。`forbidden_actions` 只使用 `candidate_write/gate_semantics_change/benchmark_execution/production_activation/deployment/production_traffic/legacy_deletion/runtime_consumer/release_consumer`。DRAFT-A 必须包含全部九项；RR-GOV FREEZE-A 只有在 exact target/write set 明确授权时才可移除 `gate_semantics_change`，其余禁止项继续存在。

`targets.json` 的 `schema_id` 固定为 `reviewer-refactor-decision-targets/v1`，exact keys 为 `schema_id/targets`；每个 target exact keys 为 `repository/path/expected_state/expected_sha256/proposed_path/proposed_size_bytes/proposed_sha256/consumer_state/validation_command_ids`。`expected_state` 仅 `present/absent`，absent 时 `expected_sha256=null`，present 时必须是 exact old bytes digest；`proposed_path` 固定在 bundle 的 `proposed/<repo-id>/...`；`consumer_state` 固定 `inert_only`；validation commands 是第 0.5.3 节 command ids 或 RR-GOV packet 内明列的 proposed command ids。

`validation-commands.json` 的 `schema_id` 固定为 `reviewer-refactor-decision-validation-commands/v1`，exact keys 为 `schema_id/commands`；每项 exact keys 为 `command_id/cwd_repo/argv/timeout_seconds/expected_exit/fixture_ids`。`argv` 必须是无 shell 解释的 string array，`cwd_repo` 是四仓 id，`fixture_ids` 只能引用同 bundle fixtures；命令在应用 proposed targets 的 disposable copy 中运行，不得写原 worktree。target 引用不存在的 command、command 引用不存在的 fixture 或命令没有界限均使 packet FAIL。

`fixtures/manifest.json` 的 `schema_id` 固定为 `reviewer-refactor-decision-fixtures/v1`，exact keys 为 `schema_id/fixtures`；每项 exact keys 为 `fixture_id/path/expected_verdict/size_bytes/sha256`，verdict 仅 `PASS/FAIL/INDETERMINATE/NOT_AUTHORIZED`。bundle `manifest.json` 的 `schema_id` 固定为 `reviewer-refactor-decision-draft-manifest/v1`，其余 exact keys、排序、content root 与无自引用规则同第 0.5.2 节，但 domain separator 固定为 `pullwise:reviewer-refactor-decision-draft-content-root/v1\0`，`profile` 固定 `inert-decision-draft`。manifest 必须列出 packet/targets/validation commands/fixtures/proposed 的全部 regular files；未知 file/consumer、tracked path 与 proposed bytes 不一致，或任何 runtime/build/package/release import 均使 bundle FAIL。

v1 refs 使用 closed ASCII form：`repo:<repo-id>:<percent-encoded-path>#<percent-encoded-section>`、`bootstrap:<release-id>:0A:GOV-0A:<generation>:sha256:<64-lowerhex>`、`packet:<packet-id>:<generation>:sha256:<64-lowerhex>`、`confirmation:<system>:<percent-encoded-conversation-id>:<percent-encoded-message-id>:sha256:<64-lowerhex>`。percent encoding 使用 RFC 3986 uppercase hex，禁止未编码 colon、`@`、`#` 和 slash；locator 到 local/CI artifact URI 的映射只放在 packet provenance 与 evidence environment，不能用可变 URI 代替 digest identity。

#### 0.6.2 v1 decision entry 映射

- DRAFT-A 与 FREEZE-A 始终是两个按实际顺序追加的新 decision ids；不得预留 D42–D47。DRAFT key 使用 `reviewer-refactor-<packet>-draft-a`，FREEZE key 使用 `reviewer-refactor-<packet>-freeze-a`。
- 两项继续使用 v1 的 exact `DECISION_KEYS`。`resolution.decision_text` 必须 byte-for-byte 等于 packet 的 `exact_confirmation_text`；`source_refs` 至少包含本规格的 `repo:...` ref 和 exact bootstrap ref；`resolution.evidence_refs` 至少包含 exact confirmation ref 与 packet ref。ref 中的 digest 必须按所指 manifest/message exact bytes 独立复算。
- `required_by_slice` 在 v1 bridge 中固定为 `S8`，表示这些后续治理决策必须阻断当前最深 release slice，而不是把 RR 工作误称为旧 S8 实现。`affected_units` 必须列出被 supersession intent 影响的现有 normative units；RR-GOV 至少包含 `mvp-executable-gates` 与 `post-closure`。新增 normative unit 或新 stage 枚举只能由 RR-GOV-FREEZE-A 授权的 replacement schema 实现。
- DRAFT resolution 不 supersede 旧生产 decision，只授权 packet 中 inert draft paths。FREEZE 的 v1 `depends_on` 必须包含 exact DRAFT id；DRAFT 的 `canonical_resolution_sha256` 由 FREEZE packet 的 `required_inputs` 和 evidence ref 绑定，因为 v1 `depends_on` 本身不存 digest。FREEZE 按 packet 逐项 supersede 与新架构冲突的旧 decisions；不冲突的 D41 禁止项继续有效。`question_order` 追加实际 ids，`normative_units[].decision_ids` 与 `affected_units` 双向同步。
- v1 entry 不复制 packet 的 write set、fixtures 或 target bytes；只保存 content-addressed refs。packet bytes 改变必须新 generation 和新 amend/freeze decision，不能改写已 resolved entry。

#### 0.6.3 记录步骤与 fail-closed 规则

1. GOV-0A 先生成 inert packet、proposed register/document bytes 和 manifest；当前 live register 不变。
2. recorder 展示 exact `exact_confirmation_text`、packet manifest SHA-256、只授权 write set 和禁止项；“按文档做”“继续”等模糊文本不追加 decision。
3. user/architecture owner 给出 exact option-anchored confirmation 后，才把对应 v1 decision entry append 到 live register，并用现有 `canonical_resolution_sha256` 计算 digest。该确认只授权 decision register 与 generated human view 的记录写入，以及 packet 明列的下一阶段最大边界；不自动授权目标实现。
4. 运行 `agent_first_decision_register.py check`，再运行 `sync-document` 并复查 check；保存前后 exact bytes、命令和 CI。失败时该变更不得合并，也不能靠手改 expected/definition digest 放行。
5. FREEZE 使用新的独立 confirmation 重复该流程。DRAFT confirmation、PR merge、旧 issue 或一般性“同意重构”都不能代替 FREEZE。

当前 CLI 只有 check/render/sync，没有 record 子命令；在 EVD-0 replacement recorder 交付前，上述步骤由受控 recorder 人工组装 entry，但必须 exact compare confirmation、使用现有 canonical digest helper 并通过 current structural/history checks。若 current v1 无法在不改变 schema/tool 的情况下表达某项授权，状态保持 `NOT_AUTHORIZED`，把 schema/tool exact target bytes 加入 RR-GOV-FREEZE-A；不得先改 validator 再让新 validator 授权自己。

## 1. 执行结论

采用：

> **Thin deterministic Worker shell + pinned Codex Python SDK + explicitly bound versioned Reviewer Skill + mechanical validator + durable terminal candidate outbox**

- Skill 维护审查方法、证据标准、严重性、置信度、误报抑制和输出纪律。
- Worker 维护单槽、snapshot、受控指令面、SDK/runtime pin、deadline、cancel、sandbox、coverage、redaction 和 outbox。
- Server 是队列、租约、取消、预算和全局终态 CAS 的唯一权威。
- 离线 benchmark 证明语义质量；每次运行的 validator 证明 wire/source/location/coverage/发布事实。
- 删除无当前产品消费者的通用 Agent OS 抽象：Task Owner、独立 Quality Verifier、Requirement/Observation/Attestation 图、通用 capability/effect、语义 checkpoint、固定 fanout/voting。

这不是纯 prompt Worker；安全、权限、时间、资源、事实和发布仍由代码强制执行。

## 2. 当前证据快照

2026-08-05 当前四仓复核：

- 默认 builder 仍返回 `ReviewWorkerV1`；Agent Kernel 只是可选 shadow projection。
- `review_worker_v1.py` 仍注册 30 个生产 phase，SDK 调用仍在该路径。
- Worker 的 `pyproject.toml` 固定 `openai-codex==0.1.0b3`，当前安装环境已有 `SkillInput`、`Thread.run(..., output_schema=...)`、thread/turn/interrupt；但 `setup.py` 仍声明未固定的 `openai-codex`，因此“所有受支持打包路径都 exact-pin”尚未被证明。Stage 0A 必须按 5.3.1 分类该 drift；未获 exact write-set 授权前不直接修改。该版本公开 Sandbox 封装只表达预设模式，不能单独证明受限读取根；Stage B 必须先对 exact SDK/CLI/runtime tuple 做 capability probe。探针通过时可不升级；否则只能使用经验证的外部 sandbox、经显式决策的兼容性升级，或 `NO-GO`。
- Server/Web 已接收动态 `progressSteps`；旧依赖主要在 billing/quota phase、artifact kind、测试、Admin 配置和文案。
- Server quota 仍识别 `repo_map`、`risk_routing`、`reviewer_fanout`、`clustering_and_voting`、`validator_disproof`、`final_report_json`。
- Admin 仍编辑 `reviewerConcurrency`，并消费 `maxBundles/maxReviewerAssignments`。
- 决策注册表为 `ready`：40 resolved、D2 inactive、无 active decision。
- Slice-0 当前失败：当前 generated wrapper 为 8,762 行、SHA-256 `9404c18b39afdb0ee6bd9d15fdbb3b24d9b85f1972a597a5919a868afe480697`，与 D41 记录一致；Slice-0 baseline 仍固定 D39 的 8,062 行、SHA-256 `bd099dd825c2b2340061b67500bc02f1bb4fee0a1ce7ff44138b36b8821a59fd` 及旧 producer。Stage 0A 必须证明二者的完整 provenance，再决定是既有生成链机械漏同步还是需要 RR-GOV replacement；本文不预判结论。
- `tests/test_agent_first_decision_register_gate.py` 当前 405 行且未入 Slice-0 baseline。它不能以“新增 grandfathered 超限文件”的方式直接纳入；若现有语义确实要求收录，先按单一职责拆分/缩减到不超过 400 行并证明测试语义不变，否则记录为 Stage 0B replacement obligation。
- legacy contract baseline 为 `compatible`，14 组固定 probe 全部通过。
- 默认 absence ratchet 为 `ratchet_clean=true`、`legacy_absent=false`。
- 当前 `--require-absent` 既发现 live legacy，又因 `worker.004-frozen-contract-baseline` 自引用而 `indeterminate`；当前报告有 108 个 failure，failure 列表首项为 `server.001-agents`，唯一 indeterminate reason 为 `strict_catalog_self_reference`，不能证明 clean break。
- 四仓取证时工作树均干净；远端 CI 未纳入本地证据，实施阶段必须复查。

因此必须从 Stage 0A 开始；未完成 Stage 0B 所需决策时，不能替换 gate、写 candidate 或删旧代码。

## 3. 范围、目标与非目标

唯一产品任务是 `repo_review.full_scan`：输入为 Server 授权的不可变仓库快照与政策；source 只读；受控验证只写 disposable copy；输出 findings/coverage/limitations/usage/terminal candidate；中断后从头重算，只恢复冻结 outbox。

目标：

- 语义来源收敛为一个 Worker-owned Reviewer Skill。
- 正常路径一个 root thread、一个 main turn；最多一次纯格式修复 turn。
- 公开进度为 `preparing/reviewing/validating/publishing/terminal`。
- 使用三层最小契约，不向浏览器泄露 Worker 私有字段。
- 以 D22 专业化后的 paired benchmark 证明质量不劣。
- cutover 后删除 30-phase、非目标 Agent Kernel、shadow/fallback/compatibility 和旧消费者。

非目标：

- 写回原仓库、自动修复/PR、外部 tool/provider effect、人工 approval、通用 Agent task。
- reviewer fanout、投票、per-run Verifier、子 Agent、跨 lease semantic resume。
- 证明模型“理解每一行”；产品只承诺 inventory complete + coverage accounted。
- 兼容旧任务/wire/table/DTO/数据。历史留存需单独的只读、不可执行、隔离合规决策。

## 4. 系统不变量

1. 每 Worker identity 最多一个 active job、一个 SDK/App Server、一个 auth store。
2. Server 拥有 lease/cancel/budget/global terminal authority；Agent 不创造 authority。
3. source snapshot 只读；写入只在 disposable validation copy。
4. Skill 及其全部 runtime-reachable bytes、SDK、CLI、model、effort、schema、source、instruction manifest 均 exact-pin。
5. Agent tool 的可读根、可写根、命令、环境和网络均为显式 allowlist；它无 provider/control credential、无 approval。provider OpenAI 出站是独立通道。
6. 缺失、漂移、超限、未知 schema/reason、stale attempt、source mutation 均 fail closed。
7. 无 finding 可以成功；无 coverage closure 不能成功。
8. Worker outbox 是不可变 attempt candidate；Server CAS 才是全局终态。
9. 不存在第二 runner/authority/current contract/fallback。
10. 新手写文件遵守 400/600 行门，不向现有超大文件增加新职责。

## 5. 目标架构与信任边界

```text
Pullwise Server
  ├─ queue / acceptance / lease / cancel / budget / deadline
  ├─ private Worker control + terminal CAS
  ├─ normalized review result + issue dedupe
  └─ public Web/Admin DTO
          │ authenticated control/clone transport
          ▼
Thin Review Worker
  ├─ one slot + supervisor
  ├─ immutable source + instruction manifest（模型文件系统外）
  ├─ source/instruction/validation gateway + append-only receipt ledger
  ├─ scratch-only model filesystem + sanitized tool environment
  ├─ controlled CODEX_HOME/CWD + pinned Skill/SDK/CLI
  ├─ one thread/turn + optional format repair
  ├─ source/location/coverage/result validator
  └─ immutable terminal candidate outbox
          │ Worker-scoped provider channel
          ▼
Codex SDK / App Server
  ├─ explicit Reviewer Skill
  ├─ typed source/instruction/search tools
  ├─ bounded validation tool
  └─ optional generic shell（看不到 source/validation mount）
```

三条通道：

| 通道 | 允许 | 凭据 | Agent 可见 |
|---|---|---|---|
| SDK/provider | 仅 OpenAI/Codex 出站 | Worker 专属 auth | 否；工具读不到 auth/env |
| Worker↔Server/clone | lease/heartbeat/cancel/artifact/result/clone | Worker token/短期 token | 否 |
| Agent tools | instrumented source/instruction read/search、bounded validation profile | 无；使用 Worker 预建立的本地 capability channel | 是；只能看到工具返回值和 scratch，network disabled/deny-all |

“无网络、无生产凭据”专指 Agent tools，不是让 SDK 或 Worker control transport 失效。

### 5.1 模型可见文件系统与进程边界

仅设置 Worker-owned `CODEX_HOME`、turn `cwd` 或 `readOnly/workspaceWrite` preset 不能证明读取隔离。当前 [Codex App Server](https://developers.openai.com/codex/app-server) wire 支持显式 `readOnly.access` / `workspaceWrite.readOnlyAccess`，但固定的 `openai-codex==0.1.0b3` 公开 Python Sandbox 封装只提供 preset，不能表达 restricted readable roots。因此 exact tuple 必须通过运行时 capability probe，不能从 App Server 文档、版本号或 prompt 推断 Python SDK 路径已经安全。

每个 turn 的 model-visible filesystem 只能包含：

- Worker-owned model-turn scratch：读写、容量有界、attempt 后销毁，不含任何 source/instruction/control bytes。
- staged Reviewer Skill runtime assets：只读、manifest exact-bound；eval fixtures 和 Worker-only schema/control assets 不挂载。
- 受审计的 tool bridge/最小 binary/library/runtime：只读。bridge 本身没有 source 路径或生产凭据，只能使用 Worker 预建立、attempt-scoped 的本地 capability channel。

immutable source snapshot、target `AGENTS.md` bytes/instruction manifest、disposable validation copy、Worker source/control/runtime state、terminal outbox、logs、clone/control token、host/user home、其他 worker root、未列入 manifest 的 temp/cache，以及 `CODEX_HOME` auth/config/session **一律不进入模型可见文件系统**。Agent 只能通过 5.4 的工具结果看到获准 source/instruction bytes；validation copy 始终由 Worker-owned validation service 持有，模型和通用 shell 都不能直接遍历或修改它。

App Server/provider supervisor 可以使用 Worker-scoped auth，但 tool child process 只收到固定的 sanitized environment；不得继承 API key、token、proxy credential、`HOME`/`CODEX_HOME` credential path 或 Server transport credential。若运行时不能关闭通用 shell，shell 也只能看到 scratch 和最小 runtime，不能成为 source/read-receipt/validation 的旁路。

实现顺序：

1. Stage A FREEZE 前先运行 `CAP-0` artifact/introspection/loopback probe，证明 exact SDK/CLI/App Server 的受支持公开 surface 能够表达 SkillInput、strict output schema、精确工具配置、隐式 surface 关闭、sandbox wire、event/usage、interrupt/close 和 context/compaction accounting；不能表达的机制不得写入冻结架构。
2. 优先使用能通过全部探针的受支持 Python SDK/API 路径。若 `0.1.0b3` 不能表达所需能力，只有在 RR-TRUST-FREEZE-A 明确选择并 exact-pin 的兼容升级后才能采用新 SDK；不能把当前 App Server wire 的能力当作当前 Python SDK 已暴露的能力。
3. 允许保留 `0.1.0b3` 的唯一例外，是经验证的外部 Linux sandbox/mount namespace 已使 model/tool process 只能看到 scratch/tool bridge，并由 Worker 外部服务持有 source/validation。该隔离仍需 exact-pin image/kernel/policy evidence。
4. 两条受支持路径都不能满足时，Stage B `NO-GO`。不得手写 App Server JSON-RPC、猴子补丁 generated SDK types、依赖未公开字段，也不得把普通 `read-only` preset、文件 ACL 愿望或 prompt 当作证据。

`CAP-0` 只证明 architecture feasibility；Stage B 的 `CAP-1` 必须用真实 isolated turn/sentinels 证明实际 enforce。两者的命令、输入和停止边界见 `docs/reviewer-refactor/skill-context-and-evaluation.md`。任一 tuple 改变都同时使 CAP-0/CAP-1 和 downstream context budget stale。

Stage B 使用模型已知名称的 sentinel files/env/endpoints 覆盖 host home、其他 worker root、auth store、outbox、source、instruction、validation copy、source write、scratch write、网络和 approval。只有允许操作成功、所有拒绝路径稳定 fail closed，才可签发 capability evidence。

### 5.2 责任边界

| 组件 | 必须负责 | 不得负责 |
|---|---|---|
| Server | acceptance/lease/cancel/deadline/budget/CAS/quota/public projection | prompt/Skill/语义判断 |
| Worker | 读写根与 tool env 隔离、snapshot/instruction/SDK/deadline/validation/outbox | 全局终态/issue 生命周期/公共 DTO |
| SDK | thread/turn/tool/event/usage/interrupt | lease/terminal truth |
| Skill | 审查方法和输出纪律 | 权限/预算/终态/网络/凭据 |
| Validator | schema/binding/location/hash/coverage/redaction | 自然语言复审 |
| Web/Admin | 公共 DTO 和当前配置 | lease/attempt/Skill/source 私有事实 |

### 5.3 Runtime tuple 与 capability contract

每次 attempt 绑定一个 `evaluation_runtime_digest`。其 canonical document 至少包含 Worker wheel digest、Python 版本、`openai-codex` distribution/version/wheel digest、CLI/App Server executable digest、initialize metadata/protocol version、model/provider/effort/service tier、thread/turn config digest、sandbox/image/kernel/policy digest、Reviewer Skill manifest digest、Agent-output schema digest、tool manifest digest 和 redaction policy digest。字段缺失或实际值与预期不一致时，不启动 main turn。

`runtime-capability-report/v1` 必须由真实进程探针生成，不接受静态代码推断，并逐项给出 `PASS/FAIL/INDETERMINATE`：

- 显式 Skill 只加载 manifest 中的 exact bytes；
- strict output schema 被真实 turn 接受并拒绝附加/未知字段；
- 实际 tool/MCP/plugin/hook/approval surface 与 tool manifest 完全一致；
- source/instruction/validation 只能经 5.4 gateway，generic shell 无旁路；
- tool child 无 provider/control credential、无任意网络，scratch 是唯一可写根；
- provider 和 Worker control 通道在 tool sandbox 外正常工作；
- interrupt、deadline、event/usage、archive/close 有界且错误可分类；
- capability report 自身绑定 executable/config/image/kernel 和 probe fixture digests。

任何 SDK/CLI/App Server/model/sandbox mechanism 变化都会产生新的 runtime digest、使旧 capability report 失效，并触发 RR-EVAL-FREEZE-A 的运行时可比性规则。

#### 5.3.1 SDK packaging pin authority

当前和目标的 Python dependency authority 是已发布 Worker wheel 的 canonical `METADATA`，不是某一个 source 声明单独成立。只要 `pyproject.toml`、`setup.py`、lock/audit input、CI/release build command 中仍有两个可执行的 packaging path，它们必须对 `openai-codex` 产生同一个 exact `Requires-Dist`；“常用命令恰好走 pyproject”不能把另一个 runnable path 的宽松依赖降为 warning。

- 在 RR-TRUST 另行 freeze 升级前，目标 exact requirement 固定为 `openai-codex==0.1.0b3`。版本范围、无版本 requirement、环境中恰好已装该版本或 SDK 自带 CLI pin 都不能替代 Worker wheel dependency pin。
- GOV-0A 保存 `pyproject.toml`、`setup.py`、`MANIFEST.in`、release/CI build workflow 和现有 wheel（如有）的 exact bytes/digests，并记录当前支持的 PEP 517 与 Ubuntu fallback build entrypoints。当前 `pyproject.toml` exact pin 与 `setup.py` unpinned 的组合分类为 `PACKAGING_PIN_DRIFT`；它是 RR-GOV replacement obligation，不授权 Stage 0A 直接改文件。
- RR-GOV-FREEZE-A 必须二选一并绑定 exact bytes：让所有仍可运行的 metadata producer 输出同一 exact requirement，或删除/封闭旧 producer 的 dependency-authority 能力并以 failing fixture 证明它不能再构建可安装发行物。不得保留“主路径 pin、fallback latest”的双义运行时。
- Stage B wheel probe 必须在隔离环境构建实际 release wheel，解析 wheel `METADATA`，安装后读取 `importlib.metadata` 的 distribution/version/file digests，并核对 SDK-bundled CLI 或明确配置的 standalone CLI executable digest。若支持 fallback build，再以该 exact command 构建第二个 wheel并 byte/metadata compare；任一路径下载了未 exact-pin SDK、使用外部全局 package 或无法复算 transitive manifest，`RUN-2` 为 `FAIL/INDETERMINATE`。
- `evaluation_runtime_digest` 绑定的是实际安装 wheel、SDK distribution、CLI executable 与 sandbox tuple。source declaration 一致但安装 bytes 不一致仍拒绝启动；source declaration 不一致也不能靠 runtime 恰好一致放行。

### 5.4 不可绕过的 read/validation gateway

Gateway 是 Worker-owned、attempt-scoped 的本地服务，运行在 model/tool sandbox 外。Codex 侧只配置受支持的 typed local tool bridge；bridge 通过预建立的 OS capability/IPC handle 调用 gateway，不能接受模型提供的 token、socket path 或 host absolute path。它不是第二 runner，也不是手写 App Server transport。

冻结的 `reviewer-tool-protocol/v1` 只提供以下工具：

| 工具 | 主要输入 | 主要输出 | 权威事实 |
|---|---|---|---|
| `source_inventory` | `inventory_id/cursor/limit` | canonical entry page、next cursor、page digest、terminal completion seal | inventory 枚举与分页，不代表已审查 |
| `instruction_read` | inventory id、scope id、expected instruction-set digest、cursor、`max_bytes` | ordered file/chunk identity、actual bytes/range、next cursor、terminal completion seal | 对应 scope 的 paged complete instruction-set receipts |
| `source_read` | inventory id、canonical path、expected file digest、byte/line range | 实际返回范围、encoding、bytes、content digest | full/bounded read receipt |
| `source_search` | inventory id、literal/受限 regex、path filter、limit | 有界 match/span 列表、truncation | 只证明返回 match，不能生成 inspected |
| `validation_run` | validation profile id、inventory/source digest、schema-validated args、timeout | exit、bounded stdout/stderr、mutation summary、output digests | 在一次性 copy 中执行的机械验证事实 |

所有模型提供的 source path/filter 只能引用 inventory 中的 canonical relative path；拒绝 absolute path、`..`、alternate separator、case ambiguity、symlink/reparse/submodule escape、unknown id 和 stale digest。instruction paths 由 gateway 从 frozen manifest/scope 解析，模型不能自选、重排或省略适用文件。一个 scope 只有在全部 instruction chunks 按 manifest 顺序写入 ledger 且 gateway 发出 terminal completion-seal receipt 后才是 `instruction_complete`；零 instruction 的 scope 也必须有显式空集 seal。完成前 gateway 自身拒绝该 scope 的首个 `source_read/source_search/validation_run`，不能只靠事后 validator 检查顺序。`validation_run` 只能选择 manifest 中冻结的 command/profile 和参数 schema；service 创建并销毁 disposable copy，执行时网络 deny-all、env allowlist、CPU/RSS/output/deadline 有界，绝不接受任意 shell 字符串。

每个响应包含 `protocol_version/attempt_id/turn_id/tool_call_id/sequence/status/receipt_id/receipt_digest`。Gateway 在模型不可写的 append-only ledger 中按单调 sequence 记录 request digest、resolved object identity、实际 byte/line range、returned-bytes digest、instruction-set digest、limit/truncation、started/finished time、exit/error code，并形成 previous-digest hash chain。Worker freeze result 前验证链、tool-call correlation 和 ledger fsync 边界；模型伪造同形 JSON 不会产生 ledger receipt。

工具错误使用 closed registry：`INVALID_REQUEST`、`STALE_BINDING`、`PATH_REJECTED`、`INSTRUCTION_REQUIRED`、`CURSOR_CONFLICT`、`LIMIT_EXCEEDED`、`UNSUPPORTED_ENCODING`、`VALIDATION_DENIED`、`TIMEOUT`、`SERVICE_UNAVAILABLE`、`INTERNAL_INDETERMINATE`。未知错误、bridge/gateway crash、sequence gap、hash-chain mismatch 或 output truncation 未被结果声明时 fail closed。具体分页、单次/累计 bytes、search matches、validation profiles、CPU/RSS/output 限额在 Stage A 冻结并进入 benchmark binding；`instruction_read.max_bytes` 的硬上限为 64 KiB，因而 1 MiB instruction-set 不能伪装成单响应完整返回。

#### 5.4.1 Source inventory policy 与 complete population

Stage A 冻结 `source-inventory-policy/v1`，每个 attempt 在读取 source 前生成 `source-inventory-manifest/v1`。policy 必须绑定 source descriptor/snapshot kind、root identity、固定 control-root exclusions、entry-kind classifier、text/binary/language detector version、generated/vendor rules、instruction scope resolver、所有数量/字节上限和 policy digest。实现不得从 path keyword 或 Agent 输出临时扩大排除项。

inventory population 定义为 immutable snapshot 中除固定 `.git/**` 元数据和 Worker-owned `.codex-review/**` control tree 外的每个非目录 entry。regular text/source、docs/config、generated、vendor、binary、symlink/reparse、gitlink/submodule 和 special entry 都必须有一项；后五类可以按 closed policy disposition 标为 skipped/unsupported，但不能从 inventory 消失。symlink/reparse 不跟随，gitlink 只绑定 exact object identity；case-fold collision、重复 path、目录/文件 prefix collision、escape、不可读 regular file 或扫描前后 source-state 漂移使 inventory `INDETERMINATE`。

生产 Git snapshot 只接受 exact commit/tree 中的 tracked entries；出现未声明 untracked/ignored bytes 即 `SOURCE_STATE_MISMATCH`。非 Git snapshot 必须由 Server-owned content manifest 逐项 exact-bind，不能以当前工作目录遍历替代。manifest entry 至少含 dense `entry_id`、canonical POSIX path、kind、size、regular-file SHA-256、language/encoding detector result、review disposition/closed reason、instruction scope id 和 object identity；整体按 path 的 UTF-8 bytes 排序并绑定 policy/source/root digest。

最多 2,000 的限制适用于上述全部 inventory entries，不是只对模型最终选择的“included files”计数。`N > 2,000`、总 source bytes/单文件超出冻结 preflight 限额或 manifest 无法完整形成时，在任何 main turn 前拒绝；不得抽样、截断、隐藏 vendor/generated 后再宣称全仓。目录统计可以另存，但不进入 dense coverage index。

`N <= 2,000` 仍不代表单 turn 可行。Worker 必须按 exact tokenizer、真实 tool-response serialization 和冻结的最大 tool/output/reserve 计算 `context-budget-report/v1`，满足 `F + I + S + V + O + R <= C` 才可启动；hidden compaction 不允许。详细整数公式和失败语义见 `docs/reviewer-refactor/skill-context-and-evaluation.md`。不满足时在 turn 前以 `CONTEXT_BUDGET_EXCEEDED` fail closed，不抽样或隐式拆 turn。

`source_inventory` cursor 是绑定 attempt/inventory/page-size/last-entry-id 的 opaque capability。每页最多返回 Stage A 冻结且不大于 256 的 entries；最后一页必须返回 entry count、inventory digest 和 completion seal。相同 tool-call/request exact replay 返回原 receipt；复用 cursor 改 limit/filter/inventory 或跳页返回 `CURSOR_CONFLICT/STALE_BINDING`。没有 terminal seal 的枚举不能作为 coverage input。

#### 5.4.2 Instruction pagination 与顺序 seal

`instruction-manifest/v1` 从 sealed inventory 机械派生。每个 scope 的 instruction set 是按 precedence、canonical path、文件 byte offset 固定排序的 chunk stream；chunk 不能跨文件，单响应最多 64 KiB，返回 file digest、`byte_start/byte_end`、chunk digest、next cursor 和 truncation=false。cursor 同时绑定 inventory、scope、instruction-set digest、前一 chunk digest 和 next offset。

Gateway 只接受期望 cursor 的下一 chunk；同 tool call exact replay 返回原 receipt，同 cursor 不同参数或越序返回 `CURSOR_CONFLICT`。最后一个 chunk 后另写 completion seal，seal 绑定 ordered chunk-receipt digests、总文件/字节数和 instruction-set digest。任何 missing/duplicate/overlap/gap/truncation、文件漂移、bridge restart 丢链或超过 128 files/单文件 256 KiB/总计 1 MiB 都不得 seal，并按 closed reason 使 attempt `PARTIAL/FAILED/INDETERMINATE`。模型声称“已读”或一次大响应不能代替 seal。

## 6. Reviewer Skill 与受控指令面

### 6.1 唯一语义资产

- `pullwise_worker/reviewer_skill/SKILL.md`
- `pullwise_worker/reviewer_skill/manifest.json`
- `pullwise_worker/reviewer_skill/review-agent-output-v1.schema.json`
- `pullwise_worker/reviewer_skill/review-result-v1.schema.json`
- `pullwise_worker/reviewer_skill/eval-fixtures/**`

`manifest.json` 固定 Skill name/version、Skill/schema digests、工具 allowlist、最大 turn 数、最小 SDK/runtime tuple，并把文件分为 model-visible runtime assets 与 Worker-only control assets。`review-agent-output-v1.schema.json` 由 Worker 作为 `outputSchema` 传入，`review-result-v1.schema.json` 只供 Worker validator 使用；二者都不需要作为 tool-readable 文件挂载。wheel check 必须证明安装 bytes 一致。旧 prompt 的有效知识经有来源的迁移表进入 Skill；迁移后生产不再读取旧 prompt。

`manifest.json` 还必须列出按 canonical relative path 排序的全部 runtime-reachable Skill files 及其 size/SHA-256。若 `SKILL.md` 引用 `references/`、`scripts/`、`assets/` 或其他文件，它们全部进入同一 manifest；未列入、重复、越界或 digest 漂移的文件拒绝 staging。Stage B 推荐 runtime Skill 只含 `SKILL.md` 和明确必要的只读引用。生产语义只来自这组 runtime assets；`eval-fixtures/**` 仅是离线评测证据，永不 staging、挂载或暴露给模型。

Skill 的 normative sections、finding admission、severity、integer `confidence_bps`、validation status、counterexample pass 和不报告项按 `docs/reviewer-refactor/skill-context-and-evaluation.md` 固定。Stage A 另需 `reviewer-skill-migration/v1` 对全部 production-reachable 旧 prompt semantic units 逐项 retain/rewrite/delete/runtime-policy 迁移并双向绑定 eval fixtures；未映射或仍有旧 consumer 时 `SKILL-1` 不能 PASS。

### 6.2 显式绑定与防 TOCTOU

每个 attempt：

1. 从已安装 package 按 manifest 将 exact Skill runtime assets 复制到 model-visible read-only staging，将 schemas/control assets 复制到独立 Worker-only validation staging；两者都不在 source 内且都不可由模型写入。
2. 拒绝 symlink/reparse/non-regular/multi-link/越界路径，生产 POSIX 使用私有权限。
3. 对每个 runtime file 记录 path/device/inode/size/SHA-256；`Thread.run` 同时传 `$pullwise-reviewer` 文本和指向 staged `SKILL.md` 的显式 `SkillInput`。
4. turn 后重检全部 staged 对象；任何增删、替换或 bytes 漂移使 attempt `FAILED`。
5. result 绑定 staged Skill digest 和 package manifest digest。

Stage B 必须故障注入 swap、symlink、truncate、turn 中修改。

### 6.3 关闭隐式 surface

- 使用 Worker-owned 空白/生成式 `CODEX_HOME`，不继承 user/admin/system skills、插件、MCP、hooks 或全局会话。它只控制 Codex discovery/state，不替代 5.1 的文件读取隔离。
- turn `cwd` 位于 Worker-owned model-turn 目录，不在 target repo/Worker source，避免父级 `AGENTS.md` 自动发现。
- 只装 allowlist Skill；启动后枚举实际 skill/tool/MCP/plugin/hook surface，与 manifest exact compare。
- 无法证明非允许 surface 被关闭时，Stage B `NO-GO`；prompt 声明不是隔离证据。

### 6.4 target `AGENTS.md`

不依赖 Codex 默认 32 KiB merge，也不把“记录 digest”当成模型已看到内容。target `AGENTS.md` 是可能由攻击者控制的 repository policy input，只能提供 boundary 内的 domain/review/presentation guidance；它不能扩大工具、文件、网络、凭据、budget、coverage、validation 或 terminal 权限。越界内容记录为 `instruction_effect_denied`，不得执行。完整 precedence/注入规则见 `docs/reviewer-refactor/skill-context-and-evaluation.md`。Worker 在 snapshot 上生成 `instruction-manifest/v1`：

- 机械发现 root/nested exact `AGENTS.md`，只接受 regular file。
- canonical POSIX path；浅到深优先级，nested 只作用其子树。
- 记录 path/scope/depth/size/SHA-256/precedence。
- 禁止截断。初始上限：128 files、单文件 256 KiB、总计 1 MiB；越限 `PARTIAL/FAILED`。
- boot input 只内联有界 policy 与 opaque instruction-manifest id/digest，不暴露 host path；Skill 在审查 scope 前通过 `instruction_read` 读取适用 bytes。
- source 与 instruction bytes 只能通过 Worker-owned instrumented read/search gateway 形成 coverage authority。SDK tool events 只用于 call correlation/diagnostics，不是文件读取证明。
- gateway 在 model 不可写 ledger 中记录 receipt：attempt/turn/tool-call、单调序号、inventory id、canonical path、byte/line range、returned-bytes SHA-256、完整/部分读取类型、适用 instruction-set digest。search receipt 只能证明返回的匹配范围，不能冒充整文件读取。
- instruction receipt 必须在对应 scope 的首个 source-read receipt 之前；Worker 按 ledger 时序验证。coverage entry 绑定适用 instruction-set digest；缺失、晚到、digest 不符或只有 search receipt 时不得标 `inspected`。
- gateway 或不可伪造 ledger 无法实现时，Stage B `NO-GO`；不得从 shell command text、stdout、模型自报或普通 SDK event 合成 receipt。

限额必须在 Stage A 冻结，benchmark 解盲后不得修改。

### 6.5 Agent 拓扑

- 每 attempt 一个新 root thread，一个 main turn。
- 仅最终消息 schema 无效且预算充足时，同 thread 允许一次 format repair；不得重扫或新增 finding。
- 禁止 fanout、独立 verifier、子 Agent、并行 turn、第二 App Server。
- archive/close 有界；失败标记 runtime unhealthy，下一 attempt 使用新 runtime。
- main turn 前 context preflight 必须证明全部 mandatory source/instruction/tool envelopes 和 output/repair reserve 可容纳；否则不启动。运行中出现未预算 context overhead 时只可诚实 `PARTIAL/FAILED`，不得靠 hidden compaction 或丢 coverage 维持 `COMPLETED`。

## 7. 三层契约

### 7.1 Server↔Worker 私有 wire

Server-owned canonical source 只有 `pullwise-server/contracts/reviewer-refactor/v2/**` 一个 layered atomic root；private subtree 至少含：

- `worker-registration/v2`
- `worker-session/v2`
- `review-task-claim/v2`
- `review-worker-heartbeat/v2`
- `review-run-event/v2`
- `review-source-grant-request/v2`
- `review-source-grant/v2`
- `review-source-acquired/v2`
- `review-artifact-upload-request/v2`
- `review-artifact-upload-grant/v2`
- `review-artifact-upload-complete/v2`
- `review-artifact-descriptor/v2`
- `review-cancel-command/v2`
- `review-terminal-candidate/v2`
- `review-terminal-receipt/v2`
- `review-attempt-disposition/v2`
- `review-error/v2`

它包含 tenant/job/run/lease/attempt/epoch、session principal、deadline/budget、authoritative cancel generation/digest、runtime/Skill/schema/source/instruction binding、source/artifact capability identity、outbox generation/digest/idempotency。source fetch 与 artifact upload 使用 one-use、短期、method/object/tenant/attempt/size/digest-bound capability，credential 只经 Worker private handle 传递，不进入 schema/log/evidence/Agent。完整协议见 `docs/reviewer-refactor/runtime-contract-and-security.md`。Server 生成，Worker exact-pin Python wrapper；生成遵守一次性原子跨仓 parity，禁止手改。

### 7.2 Server 内部 normalized result

`normalized-review-result/v1` 不是 wire/浏览器 DTO；它把 accepted candidate 转为 findings/coverage/limitations/usage/artifacts/public state，生成 finding instance id/issue key，并保存私有 audit facts。

建议模块：

- `pullwise_server/review_worker_contract.py`
- `pullwise_server/review_terminal_cas.py`
- `pullwise_server/review_result_normalization.py`
- `pullwise_server/review_public_projection.py`

现有 `_app_part_XX`/`db.py` 只留 composition seam 和冻结行数，不承接新职责。

### 7.3 Server→Web/Admin 公共 DTO

`review-run/v2` 仅含 public run/scan id、公开 status、五级 progress/message/counters/ETA、summary/findings/coverage/limitations、artifact metadata/安全 URL、公开错误与时间。

禁止输出 Worker token、lease、attempt epoch、Skill/instruction 私有 binding、provider auth、thread id、raw env/source、CAS internals。Web 继续动态渲染；Admin 只消费 health/quota 和仍存在的配置。

### 7.4 Canonical package、字段矩阵与生成规则

Server canonical source 固定为一个 root，而不是互不闭合的 private/public 两个 manifest：

```text
pullwise-server/contracts/reviewer-refactor/v2/
  manifest.json
  registry.json
  generator.lock.json
  shared/schemas/*.schema.json
  private/
    schemas/*.schema.json
    fixtures/{valid,invalid}/*.json
  public/
    schemas/*.schema.json
    fixtures/{valid,invalid}/*.json
  cross-layer/
    projections/*.json
    fixtures/{valid,invalid}/*.json
```

所有 object schema 使用 `additionalProperties: false`；所有 integer 有范围；时间是 UTC RFC 3339；digest 为 lowercase SHA-256；ID 有长度/字符集限制；path 只允许 canonical POSIX relative path。每个 private message 有共同 envelope：`schema_id`、`schema_version`、`contract_digest`、`message_id`、`sent_at`。业务判断不得依赖 `sent_at`，而依赖 Server version/epoch/sequence。

最小字段矩阵如下；Stage A 可以增加有明确消费者和测试的字段，但不能删除、改义或把 trusted 字段交给 Agent：

| Schema | 必需字段（除共同 envelope） | 关键约束 |
|---|---|---|
| `worker-registration/v2` | worker/session id、build/runtime/capability/tool-manifest digests、slot capacity | capacity 必须为 1；Server 只接受 allowlist exact tuple |
| `worker-session/v2` | worker/session principal、issuer/audience、build/runtime digest、issued/expiry/revocation generation | short-lived、可轮换但不换 identity；Agent/tool 不可见 |
| `review-task-claim/v2` | job/run/attempt id、lease id/epoch/version/expiry、source descriptor/digest、instruction/inventory digest、deadline、budget、cancel generation/digest | 全部由 Server/Worker preflight 注入；Agent 不可见 |
| `review-source-grant-request/v2` / `review-source-grant/v2` | tenant/run/attempt/source object、expected size/digest、capability id/purpose/expiry/secret digest | one-use、≤5 分钟且不晚于 lease；transport secret 只经 private handle |
| `review-source-acquired/v2` | grant/object/source/snapshot/inventory digests、closed acquisition status | exact ACK 前不启动 main turn；失败不得降级到任意 clone/local path |
| `review-worker-heartbeat/v2` | claim identity、worker state、event sequence、observed lease/cancel generation、remaining deadline | sequence 单调；不能延长 lease/deadline |
| `review-run-event/v2` | claim identity、event id/sequence、event type、event payload digest | closed event type；`semantic_review_started` 仅一次且幂等 |
| `review-artifact-upload-request/v2` / `review-artifact-upload-grant/v2` | tenant/run/attempt、artifact id/kind/size/digest/redaction、capability/purpose/expiry | one-use single object；uncommitted bytes 不可被 reader/terminal 引用 |
| `review-artifact-upload-complete/v2` | capability/object/actual size/digest/storage generation/encryption metadata digest | Server/store read-back 并幂等 commit 后才可返回 durable descriptor |
| `review-artifact-descriptor/v2` | artifact id/kind/media type/size/digest、storage generation、redaction class | descriptor 不含本地绝对路径或 raw source |
| `review-cancel-command/v2` | job/run/attempt、cancel generation/digest/reason/issued version | generation 单调；由 Server 签发 |
| `review-terminal-candidate/v2` | claim identity、expected nonterminal version、classification、result/artifact-set/outbox digests、idempotency key、execution/source bindings、可选 exact cancel binding | freeze 后 bytes 不变；`CANCELLED` 必须有 cancel binding |
| `review-terminal-receipt/v2` | idempotency key/candidate digest、accepted/rejected、global classification/version、accepted result ref、closed receipt/retry code | 同 key/digest exact replay 返回同一 receipt；rejected 不自动释放 Worker slot |
| `review-attempt-disposition/v2` | job/run/attempt、candidate/receipt digest、Server run version、`RELEASE_SLOT_NEW_ATTEMPT/KEEP_BLOCKED`、closed reason | 只能处置已拒 candidate 的本地 slot；不能改写 candidate 或生成全局终态 |
| `review-error/v2` | scope、closed code、retry disposition、sanitized public code、evidence ref | message 仅诊断，不控制状态 |
| `normalized-review-result/v1` | run/execution/source binding、findings、coverage、limitations、usage、artifacts、candidate classification、normalization digest | 只由 Worker validator + Server normalization 产生 |
| `review-run/v2` | public id/status/progress、summary、findings、coverage、limitations、artifact metadata/safe URL、public error、created/updated/terminal times、etag/version | 私有字段 schema-level absent；public status 只来自 terminal/projection mapping |

root `manifest.json` 对 registry、全部 shared/private/public/cross-layer schema/fixture/projection 和 generator version 计算 path/size/SHA-256；subtree 不拥有独立 authority/version/digest。与第 0.2 节相同，root manifest 不列自己或 detached signature，也不包含被计算的 `contract_digest` 字段。`contract_digest` 由 exact canonical manifest body bytes + fixed domain separator 计算并由外部 envelope/pin 保存，另存普通 manifest SHA-256；二者都不写回被 hash 的 body。`registry.json` 是所有 enum/reason/event/status 的唯一来源。Server generator 先写临时目录、校验 valid/invalid/cross-layer fixtures、生成语言 artifacts，再进入下述受控发布事务。

逻辑 package identity 固定为 `pullwise-reviewer-contract-v2`。Server 生成的 Python/npm wrappers 都携带 path/size/digest 完全一致的 canonical root bundle 与 logical digest；语言 loader/types 只是派生层。Worker 只 import private/shared exports；Web/Admin 只 import public exports，并以 browser reference-graph gate 证明 private bytes/fields 未进入交付资产。每个仓的 `check` 在临时目录重生成并 byte-compare；任何手改 generated file、root bytes 不一致、manifest mismatch、private public leak、unknown consumer 或跨仓 logical digest 不一致均失败。

四仓没有一个共同 filesystem/Git atomic-commit primitive，因此“原子生成”明确指 `generation-transaction/v1` 的可恢复逻辑事务，不声称四个 worktree 的 rename 在同一瞬间完成。事务只能运行在 orchestrator 持有单写 lease 的专用、clean release checkouts，不能运行在用户或其他 Agent 正在编辑的共享 worktree：

1. 事务绑定 canonical input/registry/generator/runtime digests、四仓 HEAD/dirty 状态、全部 target paths、expected-old digest（或 absent）和 staged-new digest；target 集合之外禁止写入。
2. 在 worktree 外的私有 staging 生成全部 bytes，完成 schema fixtures、language checks、byte parity 和 target collision 检查；随后将 `PREPARED` journal、staged bytes 和 parent directory 持久化。
3. 按固定 repository/path 顺序取得 generation locks，重新校验 HEAD/dirty、expected-old bytes 和 target 类型。任一并发漂移在首个 publish 前使事务 `ABORTED`，不得覆盖用户修改。
4. 每个 target 以同目录临时文件、fsync、no-clobber/atomic replace、directory fsync 发布，并在每步后推进 durable journal。所有 target 精确等于 staged-new、四仓 regenerate/byte-compare 再次通过后才写 `COMMITTED`。
5. crash recovery 必须在任何 build/check/commit 前先读取 journal：若尚无 target 发布则安全 abort；若已发布且每个 target 仍精确等于 expected-old 或 staged-new，则只按 journal forward-complete 到全 new；出现第三种 bytes、缺 staging、锁/HEAD 漂移或无法持久化时为 `INDETERMINATE`，阻止新事务和 release，由 operator 处理。`PREPARED`/部分发布状态绝不能被 consumer 当作 package。

事务 journal/staging 是 Worker/CI-owned build evidence，不进入模型可见文件系统，也不成为第二 contract authority。恢复只处理事务声明的 generated targets，不能 restore/reset 仓库其他路径。failure/crash/concurrent-edit/disk-full fixtures 必须证明：最终是未发布的 all-old、已提交的 all-new，或显式 blocked；不存在被 release 消费的 mixed set。

四仓各自只在 `COMMITTED` 后提交 exact generated bytes。一次 release change set 的 `release-manifest.json` 随后在第 0.2 节的外部 evidence generation 中创建，exact-pin 四仓 commit、generation-transaction manifest、package/build digest 和 private/public contract digest；它不得位于其所 pin 的任一 commit 中。它同样排除自身/detached signature 的自引用，引用者保存 exact manifest SHA-256。任一 pinned commit/build 变化都要求新 release generation、重新签名和下游 stage advance。

### 7.5 Closed registries 与信任来源

`registry.json` 至少冻结：

- Worker state：`IDLE/CLAIMED/PREPARING/REVIEWING/VALIDATING/PUBLISHING/PUBLISH_BLOCKED/LOCAL_TERMINAL`；
- terminal classification：`COMPLETED/PARTIAL/FAILED/CANCELLED`；
- public status/progress：第 9.4 节固定映射和五级 progress；
- run events、artifact kinds、coverage status/reason、validation status；
- private error/receipt/cancel/deadline/sandbox/source/Skill/SDK/outbox/CAS reason；
- public error allowlist 和 private→public sanitization mapping；
- retry disposition：`NEVER/NEW_ATTEMPT/OPERATOR`，不得由 Agent 或自由文本决定。

初始 registry 必须覆盖本文出现的全部 code；未知值在所有边界 fail closed。free-text `message/detail` 必须有长度、字符和 redaction 上限，只作人类诊断，永不驱动 retry、billing、terminal、coverage 或 public projection。registry 变更等同 contract 变更，必须新 version/digest、fixtures、consumer parity 和适用 benchmark/cutover。

### 7.6 新存储模型与事务边界

Clean break 使用全新的 v2 tables/keys，不复用旧 row shape、trigger 或 reader：

| Table | 核心 key/内容 |
|---|---|
| `review_runs_v2` | PK run id；job/repository、current contract/source、state/version、current attempt、deadline/budget、cancel head、terminal head、projection state |
| `review_attempts_v2` | PK attempt id；run/worker/lease epoch、runtime/Skill/schema/source/instruction bindings、state/event sequence、started/frozen/finished time |
| `review_run_events_v2` | PK event id；unique(attempt, sequence)；event type/payload digest；billing marker |
| `review_cancel_commands_v2` | unique(run, generation)；command digest/reason/issued version |
| `review_terminal_receipts_v2` | unique(idempotency key)；candidate digest、CAS input/output version、global classification、accepted evidence refs |
| `review_results_v2` | immutable result/normalization digest、private payload ref、public projection input ref |
| `review_artifacts_v2` | immutable descriptor/storage generation/digest/redaction class |
| `review_projection_outbox_v2` | unique(run, terminal version, projection kind)；payload digest/delivery state |

四个事务是规范边界：

1. **claim**：只在 run nonterminal、无 active attempt、lease policy 满足时创建 attempt 并推进 run version；
2. **cancel**：递增 generation、写 command、推进 run version；不直接接受 Worker terminal；
3. **event/billing**：按 event id + attempt sequence 幂等落库；只有首次有效 `semantic_review_started` 消费 reservation；
4. **terminal CAS**：按 8.3 的固定顺序，在一个事务写 terminal receipt、run terminal head、accepted evidence/result、quota transition 和 projection outbox。

外键、unique/check constraints 和真实 SQLite 并发测试必须证明没有双 active attempt、sequence 回退、同 key 异 digest、stale side effect 或 terminal 改写。projection worker 只消费 outbox，可重复执行但不能更新 terminal truth。

Stage D 必须选择并记录“全新 v2 schema + pre-cutover task 清空/隔离”的 exact migration；旧表可因合规被保留为不可执行只读 archive，但新进程不得拥有 reader/import/trigger/view，也不得建立 old→v2 转换。没有已演练的 destructive-data 处置授权时只 stop-intake/fence，不擅自删除历史数据。

## 8. 状态机与终态线性化

### 8.1 Worker 本地状态

| 状态 | 含义 | 允许下一状态 |
|---|---|---|
| `IDLE` | 无 active slot | `CLAIMED` |
| `CLAIMED` | job/run/lease/attempt 已持久化 | `PREPARING`、terminal candidate |
| `PREPARING` | checkout/snapshot/Skill/runtime/instruction/budget 校验 | `REVIEWING`、terminal candidate |
| `REVIEWING` | main turn 或一次 format repair | `VALIDATING`、terminal candidate |
| `VALIDATING` | 非 Agent validator | `PUBLISHING`、terminal candidate |
| `PUBLISHING` | outbox 已 freeze，等待 Server CAS/ACK | `PUBLISHING` exact replay、`PUBLISH_BLOCKED`、`LOCAL_TERMINAL` |
| `PUBLISH_BLOCKED` | permanent rejected receipt 已持久化，slot/attempt 继续被 fence | `PUBLISH_BLOCKED`、收到 bound attempt disposition 后 `LOCAL_TERMINAL` |
| `LOCAL_TERMINAL` | accepted receipt，或 rejected receipt + `RELEASE_SLOT_NEW_ATTEMPT` disposition 已持久化 | `IDLE` |

每个状态必须对应可观察边界和故障测试；不得为假想能力建状态。

无 receipt 的 timeout/transport/HTTP 408/429/5xx 只让 Worker 留在 `PUBLISHING`，由外层 control loop 重放完全相同的 outbox bytes；不得占着单槽 sleep，也不得生成新 candidate/idempotency key。accepted receipt 持久化后进入 `LOCAL_TERMINAL`。任何 permanent rejected receipt 先进入 `PUBLISH_BLOCKED`，原 candidate 永久 fence；只有 Server 随后签发且 exact-bind candidate/receipt 的 `RELEASE_SLOT_NEW_ATTEMPT` disposition 才允许释放本地 slot，`KEEP_BLOCKED`、缺失或 stale disposition 都保持占槽供 operator 诊断。该 disposition 只结束本地 attempt，不创建/改变 Server terminal truth 或 projection。

### 8.2 Worker terminal candidate

Worker 在同一锁内：

1. 读取最新 lease/deadline，并验证可见的 Server cancel command generation/digest。
2. 验证 result、artifact snapshot 和 binding。
3. canonicalize terminal candidate。
4. 临时写入、`fsync`、atomic no-clobber publish、目录 `fsync`。
5. 记录 candidate digest/idempotency key。
6. 进入 `PUBLISHING`，之后 payload 永不可变。

只有 exact-bound Server cancel command 才能让 Worker freeze `CANCELLED` candidate。local operator stop、deadline、interrupt 或模型自报均不是 cancel authority，必须按 closed reason registry 进入 `FAILED/PARTIAL`。freeze 前 authoritative cancel 获胜时 candidate 为 `CANCELLED`；freeze 后不得改写、补 finding 或重跑。

### 8.3 Server 唯一终态 CAS

Server 以 `(job_id, run_id, attempt_id, lease_epoch, expected_nonterminal_version)` 在一个事务中线性化。校验顺序也是契约：

1. 先按 idempotency key 查询既有 terminal receipt。同 key + 同 digest 直接返回原 receipt，即使当前 lease/version 已推进；同 key + 不同 digest 为 `IDEMPOTENCY_CONFLICT`。
2. 无既有 receipt 时验证 job/run/attempt/lease/version、candidate digest/schema/binding。stale attempt/lease 拒绝且无任何终态/projection side effect。
3. candidate 自报 `CANCELLED` 时必须携带与 Server 当前 authoritative cancel record 完全一致的 generation/digest；无 cancel、缺 binding 或 binding stale 时以 `CANCEL_BINDING_INVALID` 拒绝，不得接受 Worker 自创取消。
4. CAS 前无 authoritative cancel 时，只接受 Worker 计算的 `COMPLETED/PARTIAL/FAILED` classification。
5. CAS 前 authoritative cancel 已提交时，全局终态为 `CANCELLED`；已冻结的非取消 candidate 只作 attempt evidence，不得投影为成功/部分/失败。
6. terminal CAS 已先提交时，后到的不同 result/cancel 不改变终态；只有第 1 步的 exact replay 可返回原 receipt。

事务同时保存 terminal state、accepted/attempt evidence pointer、适用的 normalized result pointer、quota transition、receipt、projection-pending marker。公共 projection 可幂等收敛，不能改写终态。

| 场景 | 结果 |
|---|---|
| turn 前 lease 丢失 | 不启动 turn；旧 attempt 永久禁发 |
| turn 中 cancel/deadline | bounded interrupt，放弃未冻结输出 |
| crash，未 freeze | 新 attempt 从头重跑 |
| crash，已 freeze | 只重放 exact outbox |
| ACK 丢失 | 相同 key/digest 查询/重放，返回原 receipt |
| bound cancel 在 freeze 前 | Worker candidate cancelled，携带 exact cancel binding |
| cancel 在 freeze 后、CAS 前 | Server CAS 选择 cancelled |
| cancel 在 CAS 后 | 终态不变 |
| Worker 自报 cancel、Server 无匹配记录 | `CANCEL_BINDING_INVALID`，无终态/projection side effect |
| stale/permanent rejected submit | 持久化 rejected receipt，Worker `PUBLISH_BLOCKED`，无 projection；仅 bound disposition 可释放 slot |

## 9. Agent output、`review-result/v1` 与 coverage

### 9.1 不可信 Agent payload 与可信 Worker result

`Thread.run(..., output_schema=...)` 的目标是 `review-agent-output/v1`，它是不可信模型 payload，只含：

- findings 的语义字段与可校验 location/evidence components，不含稳定 ID。
- limitations。
- 非权威 coverage claims：inventory id/scope、模型声明的 disposition/reason；它不能制造 read receipt。

Agent payload 禁止包含或覆盖 job/run/attempt/lease/cancel、Worker/SDK/CLI/runtime、model/effort/Skill/schema/source/instruction digest、usage、artifact identity、candidate classification、terminal/public status。即使模型输出同名字段，strict schema 也必须拒绝。

Worker 在模型 payload 通过 schema/semantic/source/location/coverage 验证后生成 `review-result/v1`：

- `run_binding`：Worker 从 active attempt 注入 job/run/attempt/lease epoch。
- `execution_binding`：Worker 从实际 runtime 注入 Worker/SDK/CLI/runtime/model/effort/Skill/output schema digest。
- `source_binding`：Worker 从 immutable snapshot/inventory/instruction ledger 注入 repository/commit/tree/inventory/instruction digest。
- `findings`：验证并由 Worker 计算 instance identity 后的 findings。
- `coverage`：Worker 对 Agent claims 与 instrumented receipts 做交集后机械生成。
- `limitations`：模型声明与 Worker 检测的限制合并，Worker 检测项不可被模型删除。
- `usage`、`artifacts`：只来自 SDK events 和 Worker artifact snapshot。
- `candidate_classification`：只由 Worker closed classifier 计算。

`outputSchema` 只约束当前 turn 的 `review-agent-output/v1` 最终消息 shape；它不证明 trusted binding，也不允许 Agent 生成 terminal candidate。Worker validator/runner 才能组装 `review-result/v1` 和 frozen candidate。

### 9.2 finding identity 与证据

模型不得决定稳定 id，只输出可校验 identity components。

- Worker 计算 `finding_instance_id`：domain separator + inventory digest + concern/rule ref + canonical path + span + evidence hash。
- Server 计算 `issue_key`：repository lineage + concern/rule ref + path + primary symbol（如有）+ normalized failure signature。
- 组件不足时不猜测跨扫描同一性；创建新 issue。
- 同扫描按 instance id 去重；跨扫描只按 exact issue key。路径移动默认新 issue，除非未来有独立 alias 决策。

finding 必须含 title、severity、integer `confidence_bps` `0..10000`、failure scenario、impact、recommendation、false-positive risk、rule refs、validation status、location。`10000` 表示 100%，任何进入 canonical JSON 的 confidence 不得使用 float；显示层转换不参与 identity/gate。location 含 inventory path、有效行范围、`evidence_sha256` 和有界 span；文件/行/hash 不匹配不得进入 main findings。

### 9.3 紧凑 coverage（最多 2,000 个 inventory entries）

Inventory 按 canonical path UTF-8 byte order 排序并编号 `0..N-1`。coverage 用有序、互斥、无空洞的 inclusive ranges：

```json
{"start": 0, "end": 12, "status": "inspected", "reason": null}
```

- status 仅 `inspected/partially_inspected/skipped/unsupported`。
- `inspected` 必须同时有 Agent coverage claim、在适用 instruction completion seal 后产生的一个或多个 source-read receipts、相同 instruction-set digest，且这些 receipts 的 byte-range union 对该 regular file 精确覆盖 `[0,size)`；零字节文件也需要显式 zero-length full-read receipt。bounded reads 只有合并后完整覆盖才可成为 `inspected`。这只证明全部 bytes 经受控工具呈现并被 Agent 声明处理，不证明模型理解每个 byte。
- `partially_inspected` 必须有 Agent claim、至少一个有效 source-read receipt、实际互斥 range set 和 closed reason，但 range union 未覆盖全文件；它至少使 candidate 为 `PARTIAL`。单字节/任意小 bounded read 只能得到该状态，不能得到 `inspected`。
- `skipped/unsupported` 必须有由 Worker 从 inventory/policy/tool facts 推导的 closed reason code；Agent 不能选 terminal impact。`coverage-policy/v1` 对每个 disposition/reason 冻结 `NONE/PARTIAL/FAILED` impact。只有预先标为 non-mandatory 且 impact=`NONE` 的 generated/vendor/binary 等项可以在 `COMPLETED` 中 skipped；所有 reviewable regular text entry 必须为 `inspected` 才能 `COMPLETED`。
- search receipt 永远不能产生 `inspected/partially_inspected`；search-only、instruction seal 晚到/缺失、receipt 缺失或 digest 不符由 validator 映射为 closed skipped/unsupported reason，并按 policy 进入 `PARTIAL/FAILED/INDETERMINATE`。
- `coverage/v1` 另含按 `entry_id` 排序的 `partial_details`；每个 `partially_inspected` entry 恰有一项，保存 file size、互斥有序 byte ranges、对应 receipt digests、uncovered-range digest 和 closed reason。非 partial entry 不得伪造该项。document 还绑定完整 receipt-ledger root，validator 从 ledger 重算 full/partial status，不能只信 compact ranges 或 Agent claims。
- ranges 必须精确分区 inventory，不能 gap/overlap/OOB/unknown。
- document 绑定 inventory digest、entry count、encoding version、ranges digest。
- 超过 2,000 个第 5.4.1 节 population entries 时 preflight 拒绝，不抽样或通过排除项伪装全仓。

“全仓”只表示 inventory complete + coverage accounted，不表示每行语义理解。

### 9.4 Worker classification 与 debug

classification 由 Worker 代码基于 validated result、receipt ledger、deadline/runtime facts 和 bound cancel command 计算，Agent 无输入权：

- `COMPLETED`：结构/source/coverage 全闭合，无未声明 mandatory gap。
- `PARTIAL`：有可发布结果，但 context/permission/dependency/deadline/instruction/coverage 限制已声明。
- `FAILED`：无有效可发布结果，或 SDK/auth/sandbox/source/Skill/binding 失败。
- `CANCELLED`：Worker candidate 必须绑定 authoritative cancel generation/digest；Server CAS 仍会以自己的 cancel record 决定全局终态。

Server 只投影 CAS 选定的全局终态；`COMPLETED/PARTIAL` 可引用 accepted normalized result，`FAILED/CANCELLED` 只使用 sanitized terminal facts。映射固定且不可由 Worker/Agent 覆盖：

| Server terminal classification | public status | public progress |
|---|---|---|
| `COMPLETED` | `completed` | `terminal` |
| `PARTIAL` | `partial` | `terminal` |
| `FAILED` | `failed` | `terminal` |
| `CANCELLED` | `cancelled` | `terminal` |

非终态只由 Server 按已接受的 lifecycle event 投影为 `preparing/reviewing/validating/publishing`；未知私有状态或 reason 不透传，projection fail closed 并记录 sanitized internal error。

debug bundle 与 audit bundle 分离；不含 source、`run/bundles/**`、auth/token/raw env/其他用户数据。只含 sanitized SDK events/progress/runtime/Skill/source/instruction digests、validator、outbox/receipt metadata 和 Server scoped evidence。所有日志/event/stdout/stderr 有 redaction 与大小上限。

## 10. 30 phase、计费、Admin 与 Web

| 当前职责 | 目标归宿 |
|---|---|
| checkout/workspace/SDK init/auth/inventory/budget | Worker `PREPARING` |
| repo map/risk routing/bundle planning | Skill 方法，不作生产中间协议 |
| packing/fanout/clustering/voting | 删除；数据证明单 turn 不足前不重建 |
| JSON/location/QA | Worker `VALIDATING` |
| intent/disproof | Skill 可选方法；执行仍受 Worker policy/sandbox |
| validation workspace/test running | disposable validation boundary |
| final report JSON | main turn 结构化输出 |
| Markdown render | Server/Web 或小型确定性 renderer |
| hash/upload/submit | Worker `PUBLISHING` |
| cleanup | terminal finally，不是公开 phase |

计费删除旧 phase 列表，改为 durable `semantic_review_started`：

- main turn 获得有效 thread/turn id 并开始语义工作后才发送。
- Server 验证 run/attempt/lease 并幂等持久化；首次事件消费 reservation，此前失败/取消释放。
- 后续 progress id 不参与计费；迟到/恢复从 durable event store 收敛。

Admin 在协调切换时删除 Plan `reviewerConcurrency`、system `reviewWorker.maxBundles/maxReviewerAssignments` 及默认值/schema/API/UI/tests/copy。保留 model、effort、turn timeout、scan deadline、repository limits、output language；不得换名保留 fanout。

Web 已支持动态 steps：切到五级 steps 与 `review-run/v2`，保留 partial/ETA/debug URL 安全规则，删除旧 artifact/phase fallback，不显示私有 binding，并保持移动端/390px gate。

## 11. Append-only 决策计划

以下只是待确认 decision packets，不是 resolution。“D42–D47”只可作为历史讨论简称，不预留实际 ID；每次 append 的 ID 以 register 当时顺序为准。在 option-anchored confirmation、provenance 和 exact digest 进入 register 前，状态一律为 `PENDING_EXPLICIT_RESOLUTION/NOT_AUTHORIZED`。

每个选项 A 必须经过两个不可合并的 append-only records，解决“先有 artifact digest 还是先有授权”的循环：

1. `RR-<name>-DRAFT-A` 选择架构方向，绑定 inert decision-draft bundle manifest 和完整 supersession intent，只授权 bundle 中列出的 Stage A draft paths/tests。draft artifact 不得被 runtime、generator、package、production CI release gate 或 consumer 读取；不得 Generate/激活/跑真实 benchmark。
2. draft work 完成后，`RR-<name>-FREEZE-A` 绑定 canonical target path、exact bytes/SHA-256、schema/registry/fixture/generator/test digests、instruction-conflict plan/proposed `AGENTS.md` bytes 和 replacement obligations。只有 FREEZE record 设置后续最大授权边界；任一 byte/path/义务改变必须再 append amend/freeze record，绝不修改既有 resolution。应用 exact instruction bytes 后的 unresolved=0 report 是 Stage A PASS/后续 advance 条件，不反向要求 FREEZE 前已经更新 current instructions。

RR-GOV 是唯一 bootstrap 变体：GOV-0A 先把 inert draft bundle exact bytes 写入仓外 evidence；随后只有 byte-identical 的独立 tracked-doc apply 可使用当前 inert-doc 边界。`RR-GOV-DRAFT-A` 只允许把 replacement contract/fixtures 完整化，`RR-GOV-FREEZE-A` 才授权 GOV-0B 的 EVD-0 与 gate replacement。后续 packet 不得复用该 bootstrap 例外。

| Packet | 目标选项 A | 停止选项 B | DRAFT-A 只授权 | FREEZE-A 最大授权边界 |
|---|---|---|---|---|
| RR-GOV | 分离 immutable history 与 live forbidden catalog；用非自引用 Slice-0/absence v2 replacement，固定 exit 0/1/2 三态并机械迁移旧义务 | 保留当前 gate；本重构停在 GOV-0A | inert replacement contract/fixtures/EVD-0 draft | GOV-0B；继续禁止 candidate/Generate/benchmark/生产 |
| RR-SCOPE | 唯一任务 `repo_review.full_scan`；单 root thread/main turn、最多一次 format repair、无 fanout/verifier/sub-agent、失败从头重跑 | 不接受该专用边界；停止 candidate | Stage A scope/Skill/result draft specs | 与 TRUST/TRUTH freeze 共同授权 Stage B 离线包 |
| RR-TRUST | exact Skill/runtime；source/instruction/validation 仅经 5.4 gateway；scratch-only model FS；supported SDK 路径或经验证 external sandbox；能力不足 NO-GO | 不接受该 trust boundary；停止 candidate | Stage A inventory/instruction/tool/runtime policy 与 fixtures | Stage B 离线 trust/runtime slices |
| RR-TRUTH | Agent payload 不可信；Worker 生成 immutable candidate；Server terminal CAS 是唯一全局权威；采用 7–9 节三层契约 | 保留现有 terminal authority；停止 v2 contract | Stage A schemas/registries/DDL/CAS fixtures | Stage B offline schemas/fixtures；不自动授权 C |
| RR-EVAL | 采用第 13 节 power-gated、task-clustered paired benchmark、runtime bridge、PASS/FAIL/INDETERMINATE 和签发职责 | 不接受该 release proof；禁止真实 benchmark/发布 | Stage A power/statistics/evaluator policy 与 synthetic fixtures | B2 真实离线 benchmark；不授权生产 |
| RR-CUT | 采用第 12/16 节 stop-intake、无 mixed-version 的 clean cutover、pre-canary deletion、same-contract rollback | 业务不接受维护窗/隔离；禁止 cutover | Stage A migration/deletion/runbook/release policy drafts | Stage D release preparation；Stage E 仍需 D24 与独立发布授权 |

在第 0.6 节的 current register v1 bridge 期间，下文“record 包含”指 v1 decision entry 与其 exact-bound inert packet 的逻辑并集；register entry 自身仍严格使用现有 `DECISION_KEYS`，扩展字段一律位于 packet。每个 DRAFT logical record 至少包含：

1. `packet_id/record_phase/option_id/exact_confirmation_text`、decider、时间、provenance；
2. inert draft bundle manifest URI/digest/content root，以及 proposed target-path mapping；
3. 被 supersede 的 decision id + digest + normative unit，逐项写“保留/替换/撤销”；
4. 只授权的 draft repository/write set、tests 和明确禁止项；
5. FREEZE 前必须补齐的 artifact/fixture/replacement/instruction-conflict-plan obligations。

每个 FREEZE logical record 还必须包含 exact canonical artifact paths/digests、所有 required failing/pass fixtures、generator/version inputs、instruction-conflict-plan digest 与 proposed target `AGENTS.md` digests、前置 DRAFT resolution digest、最大 stage/work-package 边界、expiry/rollback（如有）及下一个独立确认。目标 artifact 可以在 draft write set 中存在，但在 FREEZE 前保持 non-consumed；验证器必须证明没有 import/build/CI/runtime consumer。post-apply `instruction-conflict-report` digest 进入 Stage A result 和 B advance，而不是回写 FREEZE record。

有效确认必须明确 phase 与 option，例如：`确认 RR-GOV-DRAFT-A，仅授权完成 inert gate/EVD-0 draft，继续禁止实现、Generate、benchmark、部署和流量。` 以及随后独立的 `确认 RR-GOV-FREEZE-A，绑定 manifest <digest>，仅授权 GOV-0B。` “按文档做”“同意重构”“继续”或 issue/PR 合并都不是 option-anchored resolution。EVD-0 recorder 必须机械拒绝模糊确认、跳过 FREEZE、越级授权、缺 supersession/artifact digest 和把多个独立选项静默合并；在它存在前，第 0.6.3 节的受控记录步骤按 exact text/digest fail closed，current CLI 的 structural PASS 不能替代该人工核验。

实际 ID 由 register 顺序生成。既有决策处理：

| 处理 | 决策 |
|---|---|
| 保留核心 | D1、D23、D24 barrier、D26、D27、D28、D31 |
| 专业化 supersede | D22：保留样本/seed/owner/三态/可比性/canary，替换通用 Agent 指标 |
| 明确 supersede | D6、D8、D10-D12、D14-D20、D25、D29-D30、D32-D33、D41 的冲突部分 |
| 保留安全不变量、替换实现 | D3 的 R0/R1/无 effect；D7 deadline 不延长；D13 cancel precedence；D21 单 current contract |
| 明确替换 | D5 通用 Task version → Server terminal CAS version；D9 Worker TaskResult authority → Server global terminal authority |

每个 supersession 列出旧 digest、保留不变量、撤销义务、normative units、新测试证据；本文不得静默覆盖。

## 12. 分阶段实施计划

### Stage 0A：只读治理证据与 provenance

1. `S0A.0` 按第 0.2/0.3/0.5 节创建不可覆盖 bootstrap generation，记录四仓 HEAD/clean status、当前命令/环境/input digests；同一 generation 不执行或夹带任何 tracked 修复。所有原始 stdout/stderr/exit 直接保存，`result.json` 只写 provisional 状态；另获授权的 tracked action 完成并合并后必须用新 generation 复核。
2. `S0A.1` 用当前 register/Slice-0 commands 复现 wrapper 8,762/8,062 和 digest 漂移，输出 `slice0-provenance.json`：D39/D41 record digest、producer version、Generate generation、expected/actual path/line/digest、首次出现 commit。只有既能证明是既有权威生成链/冻结语义内的机械同步遗漏，又有当前 resolution exact 授权目标 write set 时才可修复；截至本文快照不假定该授权存在。否则只产出 evidence packet，留给 RR-GOV。
3. `S0A.2` 只读取证 405 行 decision-register gate test 未入 baseline。若现有 baseline 定义确已要求收录，也必须先取得 exact write-set 授权，才可 split/reduce 到每个新手写文件 ≤400 行、运行原有 focused/full tests 并机械同步；否则只记录 replacement obligation，不扩大 current baseline。
4. `S0A.3` 按 5.3.1 取证 `pyproject.toml` exact pin、`setup.py` unpinned requirement、release/CI build commands、当前安装 distribution 和可得 wheel metadata，输出 `packaging-pin-provenance.json`。在未证明所有 runnable producer 一致前分类为 `PACKAGING_PIN_DRIFT`，只写入 RR-GOV replacement obligation，不直接修改 packaging files。
5. `S0A.4` 建含 Admin 的四仓 deletion inventory，记录 entrypoint/config/table/artifact/test/docs，明确它不是兼容承诺。
6. `S0A.5` 保持默认 absence ratchet 语义不变，补齐当前 self-reference/legacy-present/108 failures/indeterminate 的可重复证据和 CI 状态。
7. `S0A.6` 对每个另获 exact 授权的修改在前后重跑 decision register、Slice-0、contract baseline、default absence，并保存 exit/stdout/stderr/digest；没有修改时也保存一次完整 current run。strict absence 只记录当前 `INDETERMINATE`，不宣称通过。
8. `S0A.7` 按第 0.6 节在 `proposed-inert/` 生成 inert RR-GOV draft bundle：replacement schema/三态/fixtures、history/live 分离、EVD-0 bootstrap contract、目标 paths/digests、旧义务映射、write set 与禁止项；证明它无 runtime/generator/release consumer，并生成 current register v1 entry 的 proposed exact bytes。bootstrap 发布后可把 bundle byte-identical 地独立应用到准许的 tracked docs path，但无 exact confirmation 时不写 live register。

退出：每个 drift 被分类为“可在现有语义内机械修复”或“需新决策”；允许项的当前命令有直接证据；四仓 inventory 可重复；gate/生产语义未改；bootstrap manifest 和 RR-GOV draft bundle 可复算。此时只可声明 `provisional PASS/FAIL/INDETERMINATE`，不得声明 signed stage PASS。provenance 不足时仍可提交 RR-GOV-DRAFT/FREEZE packet，但 FREEZE 必须显式接受该 exact 不确定性和 replacement obligation。

### Stage 0B：有决策的治理 gate replacement

1. 先取得 RR-GOV-DRAFT-A 与独立 RR-GOV-FREEZE-A resolution；FREEZE 必须绑定 Stage 0A exact bootstrap/draft manifest、Slice-0 保留/退休边界、immutable history storage、live forbidden catalog、absence v2 三态、self-reference 消除、replacement tests 和仅限 GOV-0B 的 write set。
2. 第一个实现包是 `EVD-0`：用 failing fixtures 建最小 evidence writer/verifier、detached signature/stage-advance verifier、trust purpose/role policy 和 work-package-ledger skeleton；先 self-check，再 back-validate Stage 0A。
3. 以 failing fixtures 证明当前 strict gate 对真正 absent/self-reference 无法给出正确确定性结果，再实现非自引用 verifier。
4. replacement 必须让 live legacy present=exit `1`/`FAIL`、真正 absent=exit `0`/status `absent`、缺证或历史损坏=exit `2`/`INDETERMINATE`，且 immutable history 不作为 live forbidden input。
5. CI 默认 ratchet 继续阻止新增 legacy；exact release artifact 在 Stage D 使用 strict gate。不得通过放宽 exclusion、删除历史或只改 expected 获得 PASS。
6. EVD-0 聚合 GOV-0A/GOV-0B 的直接命令、fixtures、review、line-count、CI 和 decision bindings；生成不可覆盖 signed generation。对同一 generation 二次只读验证必须产生 byte-identical verdict；对两个新 generation 只比较 schema-governed stable projection，不删除真实漂移。

退出：RR-GOV DRAFT/FREEZE resolution/provenance PASS；EVD-0 schema/signature/stage-advance/ledger tamper fixtures PASS；GOV-0A 已得到 verified final result，且所有被 FREEZE 接受的不确定性有 replacement closure；Slice-0 或有决策的 replacement PASS；absence v2 fixtures/三态 PASS；`GOV-0B/result.json=PASS` 且生产行为未改。

### Stage A：决策与契约冻结

1. 在 GOV-0B signed PASS 后，先在 docs/evidence 中准备 RR-SCOPE/TRUST/TRUTH/EVAL/CUT inert packet bundles 与 `instruction-conflict-plan/v1`，并取得各自 `DRAFT-A` resolution；随后签发仅含 Stage A draft/remediation work packages/write sets 的 stage-advance。缺任一适用 DRAFT、plan 或 advance 时 Stage A 不开始。
2. 在 DRAFT 明确允许的 non-consumed target paths 定义 7.4 的 Server canonical private/public source、manifest、valid/invalid fixtures 与 generator contract；保持 consumer/import/build/release generation disabled，未获 FREEZE/后续 Generate 授权前不 Generate/激活。generator contract 必须区分“worktree 外临时 dry-run/byte-compare”与“向任一 repo/package 发布 generated consumer”：只有前者可由 CON-0 draft write set 授权，后者一律计入 Stage D exact-one Generate transaction。
3. 冻结第 5.4/9.3 节 inventory population、instruction pagination/seal、coverage result、identity、status/error/reason registry 和 private→public sanitization mapping。
4. 冻结 terminal CAS/cancel binding/ACK/rejection disposition/stale 和 untrusted payload/trusted envelope 边界。
5. 冻结 Skill/tool protocol、scratch-only model FS、tool env、gateway/receipt、validation profiles、限额和 exact-load evidence。
6. 冻结 7.6 DDL/constraints/四事务、migration/历史数据处置和 pre/post-activation rollback，以及 7.4 的 generation-transaction/recovery contract。
7. 冻结 RR-EVAL benchmark documents，包括 power calculation、统计单位、runtime comparison cells、paired estimator/CI 和 missing-run 规则。
8. 为每个 packet 运行 exact schema/fixture/test/consumer-absence checks，生成 canonical path/bytes/digests；逐项取得独立 `FREEZE-A` resolution。任何 freeze 后 change 都回到新 amend/freeze record，不原地更新 resolution。
9. 只应用 FREEZE exact-bound 的四仓 `AGENTS.md` bytes，明确 superseded rules；生成 post-apply `instruction-conflict-report/v1` 并使 unresolved count=0。实际 bytes 与 plan/FREEZE 不同则停止并 append amend/freeze，不能就地更新 digest。
10. 将 EVD-0 扩展为 EVD-1，验证完整 dependency/stage-advance/release-generation/direct-CI 规则，并发布新 ledger generation；EVD-1 必须继续验证全部 EVD-0 fixtures 和旧 generation。

退出：register immutable history/provenance PASS；DRAFT/FREEZE 对及 normative units 引用 exact digest；schemas/registries/DDL/tool/generation/benchmark policy 均有 valid+invalid fixtures；instruction conflicts=0；EVD-1/ledger PASS；Stage A signed PASS。各 FREEZE 只设置表中最大边界，实际进入 Stage B/B2 仍需新的 signed stage-advance。

### Stage B：离线 candidate

Stage B 只有在 Stage A signed PASS、`RR-SCOPE-FREEZE-A`、`RR-TRUST-FREEZE-A`、`RR-TRUTH-FREEZE-A` 均有效，且新的 stage-advance exact-list `SKILL-1/RUN-1/RUN-2/RES-1/PUB-1` 及其 write sets 后才为 `READY`。advance 不得包含 production lease/result/table/builder/route/deployment，也不得继承 D41 已消费的 Generate。

```text
pullwise_worker/reviewer_runtime/
  types.py
  source_snapshot.py
  instruction_bundle.py
  model_fs_policy.py
  gateway_service.py
  tool_bridge.py
  validation_service.py
  receipt_ledger.py
  runtime_policy.py
  sdk_session.py
  runner.py
  coverage_codec.py
  result_validator.py
  terminal_candidate.py
pullwise_worker/reviewer_skill/
  SKILL.md
  manifest.json
  review-agent-output-v1.schema.json
  review-result-v1.schema.json
scripts/run_reviewer_candidate.py
```

先写 failing tests：`test_reviewer_skill_binding.py`、`test_reviewer_instruction_surface.py`、`test_reviewer_model_fs_policy.py`、`test_reviewer_gateway_service.py`、`test_reviewer_validation_service.py`、`test_reviewer_receipt_ledger.py`、`test_reviewer_candidate_runner.py`、`test_reviewer_result_validator.py`、`test_reviewer_coverage_codec.py`、`test_reviewer_runtime_policy.py`。

candidate 只读 fixture/snapshot 并写本地结果；不得调用生产 lease/result、写 production table、切 builder、shadow traffic 或部署。必须证明 exact-load、surface control、scratch-only model FS、sanitized tool env、instrumented AGENTS/source receipts、gateway 外 source 不可见、validation copy 仅 service 可写、tool 无网络/凭据、单 turn、有界 cancel/timeout/close、untrusted payload/trusted result、result/coverage/redaction、2,000-file bounded encoding。无法控制任一 surface、SDK supported path 或 receipt authority 即 `NO-GO`。

### Stage B2：paired benchmark

Stage B2 需 Stage B signed PASS、RR-EVAL-FREEZE-A 和由 architecture + benchmark owners 签发的 B→B2 advance。先通过第 13 节 sample-size/power preflight。若 candidate runtime digest 与 stable 相同，则同 source/model/effort/SDK/CLI/machine/budget 交错运行 stable/candidate。若不同，必须按 13.2 建立 candidate-runtime comparison cell，不能把 stable-native 与 candidate 直接称为 runtime-controlled pair。顺序预注册；每 task 3 seeds；独立 oracle 解盲；保存 raw samples/exclusions/bindings/可复算 report。只有 RR-EVAL-FREEZE-A 的全部适用门 PASS 才进入 C；缺证/不可比/超时/样本不足均按预注册 missing-run 规则计失败或 INDETERMINATE，不得静默排除。

### Stage C：最小生产壳候选，不激活

Stage C 需 B2 signed PASS 和只允许本地/CI production-shell candidate 的 advance；RR-TRUTH-FREEZE-A 不自动赋予生产接入、D24、Generate、部署或流量权限。

- 从 legacy 按行为测试提取 slot/supervisor/checkout/source/SDK/deadline/cancel/usage/redaction/outbox，不复制 30-phase。
- Worker 消费 private package，持久化 active marker/exact outbox。
- Server 小模块实现 claim/heartbeat/event/CAS/normalization；新表无生产入口。
- 注入 crash-before/after-freeze、cancel-before/after、ACK loss、replay conflict、stale、source drift、hung close。
- 本地 E2E：Server fixture → claim → candidate → CAS → public projection。测试只能从 frozen CON-0 input 在隔离 temp 中 dry-run 生成并安装 ephemeral artifacts；不得复制/提交/publish 到四仓 consumer path 或 package registry。若 RR-TRUTH-FREEZE-A 未明确把该 exact temp-only 行为列为 dry-run，按 Generate 处理并保持 `NOT_AUTHORIZED`。

退出：单一终态、真实 SQLite 并发/recovery PASS、public/debug 无 source/secret、生产 builder/routes 未切。进入 Stage D 仍需独立 signed stage-advance record，Stage C PASS 本身不授权生成 release change set。

### Stage D：跨仓切换准备

Stage D 需 Stage C signed PASS、RR-CUT-FREEZE-A、release operator 共同签发的 stage-advance，以及一个新的 append-only exact-one `generation-transaction` resolution；D41 和任何既有已消费 Generate 不能复用。该 resolution 必须绑定 canonical input/generator/target manifest digest 和只允许的一次 transaction id，不授权激活、部署或流量。

- Server 按 7.4 的 recoverable generation transaction 生成 contract，接入但 intake disabled；改计费事件；完成 projection/debug/D24 barrier/v1 rejection 和 7.6 v2 schema migration。migration rehearsal 从生产形状的备份副本开始，禁止 old→v2 task conversion。
- 形成一个不可拆分的四仓 exact release change set：Worker builder 只指向新 runner，并删除 `ReviewWorkerV1`、非目标 Agent Kernel 和旧 outbox/result；Server 删除旧 phase billing/artifact/route/storage consumer；Web 删除旧 DTO/phase/artifact fallback；Admin 删除 reviewer/bundle/assignment 配置。不得用 flag/fallback 暂存第二路径。
- Worker exact-pin private package；Web/Admin 只 pin public DTO；doctor 校验 exact runtime tuple、tool surface 和 scratch-only filesystem capability evidence。
- 生成 production runbook，逐命令列出 stop-intake、drain/fence、停旧 Worker/Server、v2 migration、部署顺序、D24 activation、smoke、capacity gate、pre-activation abort、post-activation rollback 和 evidence capture。
- 对将部署的 exact commits/build artifacts 运行 strict absence v2、引用图、wheel/install、contract parity、DDL/CAS、四仓 local/CI。不可把“部署后再删除”当作 Stage D PASS。

退出：exact release build 内只有一个 current contract/runner/Skill，strict absence exit=`0` 且 status=`absent`；四仓 pins/fixtures/CI PASS；operator 在 production-like 环境完成完整部署、pre-activation abort、same-contract rollback 或 fence/reject 演练；release build 尚未部署/接流量；deletion manifest 全部关闭或有明确 immutable-history 处置。

### Stage E：clean cutover 与 capacity-only canary

Stage E 需 Stage D signed PASS、D24/current-generation 与发布授权、exact release attestation，以及 release + deployment operators 对 D→E record 的有效签名。Stage E 使用维护窗或无共享 authority 的 blue/green；禁止让 v1/v2 Server 或 Worker 同时连接同一生产 queue/current tables，禁止 mixed-version rolling deploy。

1. 核验 signed release/evidence digests，stop intake 并冻结 operator generation。
2. 等待 pre-cutover tasks 到权威终态；不能完成的 task tombstone/delete 或撤权隔离，不得迁移。导出 task/lease/outbox 清单并证明 active=0。
3. 停止全部旧 Worker，撤销旧 worker session/token；验证没有旧 heartbeat/claim。
4. 停止并从负载均衡/queue 移除全部旧 Server/consumer；取得 DB migration lock。此时仍未激活 v2，失败可按 16 节 pre-activation abort。
5. 应用已演练的 v2 schema/data-isolation migration；部署 v2 Server，保持 intake/claim disabled，并证明所有 v1 route/wire/event/result/replay fail closed。
6. 部署 v2 Worker，允许 registration/health 但 claim disabled；再部署只消费 public v2 DTO 的 Web/Admin。核验 exact pins、contract/runtime/capability digests。
7. 在隔离的 release-smoke namespace 运行 synthetic E2E，验证 claim/CAS/projection/debug/redaction；该任务不进入生产业务队列或 benchmark 分母。
8. 在一个 acceptance 事务中激活 D24/current contract generation，同时启用 v2 intake/claim；旧 generation 永久拒绝。
9. 新 current contract 开 5% capacity，其余容量保持关闭，而不是导向旧路径。
10. 写入 signed `5-percent-capacity-start` record 后开始 5% 窗口；同一 exact build 在 5% capacity 累计至少 24 个 eligible wall-clock hours，且该窗口内至少 200 个 accepted current tasks 达到可评估终态后，才可签发 25% promotion。
11. 25% promotion 事务写入独立 signed `25-percent-capacity-start` record 并把观察时钟清零；同一 exact build 在 25% capacity 再累计至少 72 个 eligible wall-clock hours，且该窗口内另有至少 1,000 个 accepted current tasks 达到可评估终态后，才可考虑 full。5% 窗口的时间和 task 不计入 25% 最小值，因此 canary 最短 eligible 时间为 96 小时。

`eligible wall-clock` 只在 exact contract/schema/runtime/build digest 未变、目标 capacity 已生效、生产 intake 开放且 gate 所需 telemetry 完整可读时累计。stop-intake、capacity 未达到、观测缺口或未决安全事件期间暂停计时且不得补算；runtime/schema/contract/build 改变会使两个窗口证据 stale，必须回 Stage D/E。两个窗口的 accepted population 按 Server acceptance record 归属，release-smoke、拒绝于 acceptance 前的请求、旧 generation 和前一窗口 task 均不进入后一个窗口分母。每次 promotion record 必须绑定 window start/end、eligible seconds、accepted task inventory digest、gate report/attestation digest 和 exact release digest。

capacity 使用 signed eligible-slot snapshot 和整数向上取整：`K = (C * p + 99) // 100`，每 slot capacity=1；5% 要求 `C >= 20`，25% 要求 `C >= 4`。只启用 snapshot 中 signed sorted list 选出的 K 个 v2 slots，其余 capacity 关闭且不导向 v1。slot/denominator/release 变化暂停或重启窗口。5%/25% calendar 最长分别 14/30 天；低流量未达到 task 门只能 `INDETERMINATE`，不能只靠时间 promotion。窗口结束的未决任务只在不大于两倍最大 scan deadline 的冻结 settlement horizon 内等待，之后按 policy 计失败或不确定。

promotion 不只看时间/数量。`canary-policy/v1` 必须按 `docs/reviewer-refactor/operations-and-execution.md` 固定 telemetry completeness、zero-tolerance authority/security、internal failure/partial/terminalization confidence bounds、p95 wall/cost 和 deterministic output audit sample；任一 telemetry gap 暂停 eligible clock，任一 zero-tolerance event 立即 auto-stop。

门失败即停止扩容；只可 rollback 到同 contract/schema/storage 的 signed stable，否则 stop-intake/fence/reject。

### Stage F：全量与证据收尾

Stage F 需 Stage E signed PASS 和 release + deployment operators 对 E→F record 的有效签名。Stage F 不再修改已 canary 的 runtime/schema/contract，也不在 canary 后才删除 legacy；否则新 build 没有被 canary 覆盖，必须退回 Stage D 并重跑 Stage E：

- 核验 5%/25% 的时窗、样本、质量、安全、成本和 operator stop evidence 后，按签发计划提升到 full capacity。
- 对 exact canary/full build 重跑 strict absence、引用图、四仓 pins 和 CI，确认无 flag/fallback/第二 runner/旧 consumer。
- 归档 release attestation、deletion manifest、operator evidence 和被 live catalog 隔离的 immutable decision history；关闭临时 issue/checklist，但不得删除审计要求保留的不可执行历史。

退出：full capacity 使用与 canary 相同的 exact contract/schema/runtime build；strict absence 仍为 exit=`0`/status=`absent`；引用图无未解释 consumer；四仓 local/CI PASS。

## 13. Benchmark 与发布门（D22 专业化）

### 13.1 corpus 与运行纪律

- `120` 只是继承 D22 的绝对下限，不是所有置信门的充分样本量。解盲和排程前，evaluator 必须根据每个门的冻结统计单位、置信方向、阈值和允许失败数计算 `n_required`；最终独立 task-cluster 数是 `max(120, 每个适用总体门 n_required, 每个适用 per-family 门 n_required)`。
- 对 Bernoulli 门，policy 保存公式、z 值/单双侧、严格不等号、最大允许 failures 和机器复算 fixture。例如零失败时要求 95% Wilson upper **严格小于** 2%：若冻结为单侧 95%，至少 133 个独立 clusters；若使用常见双侧 95% interval 的 upper endpoint，至少 189 个。RR-EVAL-FREEZE-A 未明确单双侧前按 189 做容量规划，不能把 120 宣称为可发布样本。
- 至少 3 个 sealed unknown repository families，每 family 至少 15 tasks。
- 至少 50 个 oracle-positive in-scope findings。
- 覆盖 security、correctness、API/schema、state/concurrency/resource、test-gap。
- 每个适用核心簇对 real defect、bad/incomplete fix、clean counterexample、environment/capability failure、adversarial/prompt injection 各至少 3 tasks。
- 覆盖小/大仓、monorepo、generated/vendor/binary/submodule、nested `AGENTS.md`、依赖缺失、测试不可运行、context/token/deadline 限制。
- 每 task 3 个预注册 seed；所有计划 run 都必须保留。seed 在 task 内等权，但不是三个独立统计样本；不得为追 PASS 追加或替换运行。
- 只允许 policy 预列 infrastructure reason 排除，逐样本报告；解盲后不得改分母、权重、seed、baseline、阈值或 evaluator。
- `15 tasks/family` 只足以作为最小覆盖/报告门，不能支持 98% Wilson-style family claim。RR-EVAL-FREEZE-A 必须逐指标标注 `overall/per-stratum/per-family` 适用范围；若 98% 或 <2% bound 适用于单个 family，该 family 也必须扩到其 `n_required`，否则该门 `INDETERMINATE`。
- task-cluster bootstrap 指标的样本量用冻结的 simulation/power procedure 预估；pilot 只能使用不含 sealed benchmark label 的历史/合成数据。procedure、effect/margin、相关结构、RNG 和目标 power 进入 signed policy。

#### 13.1.1 首发 stable baseline

candidate 实现或 sealed corpus 解盲前必须签发 `stable-baseline/v1`，绑定当前实际 production `ReviewWorkerV1` 的 signed release commit/wheel/source、prompt/Skill/contract/config、model/effort/SDK/CLI/runtime/machine/budget digests，以及 corpus schedule commitment和旧输出到 evaluator model 的 loss-accounted mapping。工作树、latest branch、candidate早期版本或事后最好的一组 run 不能充当 stable。

若 exact stable artifact/semantic assets 无法取得或无法在 candidate runtime 中不改业务语义地运行，对应 relative gate 为 `INDETERMINATE`；不得改成只看 candidate absolute score。runtime不同时继续使用 13.2 的三 cell bridge，且旧/新 output mapping、不可比字段和信息损失必须在解盲前 freeze。完整 first-release规则见 `docs/reviewer-refactor/skill-context-and-evaluation.md`。

### 13.2 统计单位、缺失运行与置信区间

RR-EVAL-FREEZE-A 必须在解盲前冻结可执行统计契约：

- `task_id` 是质量比较的 primary cluster，repository family/known-vs-unknown 是预注册 strata。legacy/candidate 在同一 `(task_id, seed)` 内配对并按预注册顺序交错运行。
- 三个 seed 是 task 内重复测量。task-level 指标先在 task 内聚合，再按 task 等权进入总体；finding、seed、tool event 和 coverage entry 不得被当作相互独立样本扩大有效样本量。
- recall/FDR 等 finding-level 指标保留 finding 权重，但 bootstrap/variance 必须以 task 为 cluster，将一个 task 的全部 seeds/findings 一起重采样。unknown family 同时逐 family 出具结果。
- RR-EVAL-FREEZE-A 为每个指标冻结 numerator、denominator、oracle mapping、severity/concern weight、tie/rounding、undefined case 和通过方向。`task success`、location accuracy、actionability、false verified 等术语没有这些定义时不能运行 benchmark。
- 真正以 task/attempt Bernoulli 为冻结单位的绝对门复用 D22/D41 的 exact Wilson 计算：success 等使用预注册的下置信界，error 等使用上置信界；不能只比较 point estimate。location/FDR/recall 等 observations 嵌套于 task 的指标使用 task-cluster bound，禁止对 findings/seeds 直接套独立样本 Wilson。零容忍门还要求 observed count=`0` 和最小样本满足，但不表述为总体风险等于零。
- paired non-inferiority 使用确定性的、按 strata 分层的 task-cluster bootstrap，计算 `candidate - stable` 的单侧 95% 下置信界；重采样次数、RNG/seed derivation、quantile、small-sample 和 p95 wall-time/cost 算法全部进入 signed policy。若另选统计方法，必须在 RR-EVAL-FREEZE-A 中命名、证明 paired/cluster handling 并重新冻结 evaluator，不能在结果后切换。
- 系统被测自身产生的 timeout、SDK/auth/sandbox/result failure 是评分结果，不是 infrastructure exclusion。只有 policy 预列、同时影响可比双方且有外部证据的基础设施故障可排除；单侧缺样、无法配对、超出排除上限或样本不足均使适用相对门 `INDETERMINATE`，不得补跑替代。
- 所有适用 absolute/relative/per-family 门取交集，必须全部 PASS；不得用总体平均覆盖失败 strata，也不得解盲后选择 primary metric。

运行时比较单元同样在 RR-EVAL-FREEZE-A 冻结：

| Cell | 语义/build 输入 | Runtime | 用途 |
|---|---|---|---|
| `S-native` | exact signed stable release | stable runtime digest | 漂移锚点，不在 runtime 改变时直接作为 candidate pair |
| `S-candidate-runtime` | exact stable source/contract/Skill/prompt semantics；只替换被 RR-EVAL-FREEZE-A 允许的 runtime tuple | candidate evaluation runtime digest | runtime-controlled baseline |
| `C-candidate-runtime` | candidate | 同一 candidate evaluation runtime digest | 与上一 cell 做 primary pair |

candidate runtime 与 stable 相同时只需 `S-native ↔ C`。不同时，三 cell 在同一 72h window、同 machine class/budget/source/task/seed 下按预注册顺序交错；primary comparison 只能是 `S-candidate-runtime ↔ C-candidate-runtime`，并把 `S-native ↔ S-candidate-runtime` 作为 runtime drift 报告。这里“stable”指 exact stable source/semantic assets，不能把替换了 SDK/CLI/model 的 cell 虚称为 byte-identical stable build。若 stable 语义不能在 candidate runtime 运行、需改业务代码/contract、或 runtime drift 超出 RR-EVAL-FREEZE-A 上限，则结果 `INDETERMINATE`。

报告必须包含 raw scheduled runs、排除证据、task-level aggregates、cluster/strata membership、bootstrap inputs/seed、每个 confidence bound 和可复算 evaluator output。

### 13.3 质量、安全和相对门

零容忍：

- source mutation = 0。
- unauthorized network/credential/tool/approval = 0。
- stale/double/conflicting terminal publish = 0。
- critical/adversarial false verified = 0。
- `COMPLETED` 中未声明 mandatory coverage gap = 0。

绝对门：

- schema/location/evidence/control/source/Skill binding valid rate = 100%。
- known task success ≥ 70%，unknown ≥ 50%。
- environment/capability classification accuracy ≥ 95%。
- `false_verified_rate` point estimate < 1%，且适用的 95% Wilson upper < 2%；numerator/denominator 和 critical/adversarial 子门按 D22/RR-EVAL-FREEZE-A 冻结。
- false discovery rate ≤ 20%。
- location accuracy 的预注册 point/bound 规则不得低于 98%；若采用 confidence lower bound，样本量必须通过 13.1 preflight，不能只以 observed point 代替。
- clean counterexample 不出现系统性规则噪声。

相对 current stable paired baseline：

- high/critical 加权 recall 的 95% non-inferiority 下界不低于预注册 margin，默认 margin = -5 percentage points。
- false discovery 增加 ≤ 2 percentage points。
- location/actionability 下降 ≤ 2 percentage points。
- verified-success p95 wall time/cost 增加均 ≤ 20%。
- unknown family 单独报告，不能被 known 平均值掩盖。

运行诚实性：

- inventory/coverage partition valid rate = 100%。
- indeterminate case 保持 `PARTIAL/FAILED`，不得假成功。
- SDK/auth/quota/timeout/cancel/source mutation/ACK loss 分类与 oracle 一致。
- debug/log 不含 source/secret。
- runtime 不可比时按 13.2 三 cell 交错重跑；不能形成 candidate-runtime stable cell 或仍不可比则 INDETERMINATE。

### 13.4 evaluator 与职责分离

- benchmark owner 冻结 dataset/oracle。
- CI/eval owner 产生 raw samples/可复算 report，无 promote 权。
- release operator 冻结 policy、核验 report、签发 attestation。
- deployment operator 只能执行已签发 canary/rollback plan。

Evaluator 只允许 exit 0 PASS、exit 1 FAIL、exit 2 INDETERMINATE；只有 exact PASS report 可签发 attestation。

## 14. 测试与验证矩阵

| 领域 | 必测场景 |
|---|---|
| Governance/evidence | 0.5 closed-key/bootstrap JCS/JSONL、fixed ordinal/env/timeout/raw-exit、slice0/packaging/deletion derived schemas、existing-generation no-clobber、input/HEAD/dirty drift、raw byte preservation、仓外 proposed-inert→tracked byte-compare、bootstrap import/back-validation、0.6 v1 entry+packet/ref/validation-command binding、模糊确认/DRAFT/FREEZE 跳级、AGENTS conflict、manifest/signature 自引用/篡改、role/revocation/expiry、stale stage advance、越界 write set、exit 0/1/2 |
| Skill | explicit input、transitive runtime manifest、unlisted/eval asset exposure、digest drift、swap/symlink/truncate、隐式 skill/plugin/MCP/hook |
| Inventory | tracked/content-manifest population、generated/vendor/binary/symlink/gitlink/special accounting、untracked/ignored、case/prefix collision、0/1/2,000/2,001、page cursor/completion seal、source drift |
| Instructions | root/nested precedence、64 KiB multi-chunk、超 32 KiB/总量超限、cursor replay/conflict/gap/overlap、空集/final seal、manifest drift、instruction-before-source ordering、伪造 receipt |
| Gateway | typed tool schema、canonical path、instruction-first enforcement、pagination/limit/truncation、bridge crash/restart、sequence/hash-chain/correlation、伪造 response 无 receipt |
| Sandbox | scratch-only model FS、host/other-worker/auth/outbox/source/instruction/validation sentinel、scratch write、network、sanitized credential path/env、approval |
| SDK/packaging | pyproject/setup/release path exact requirement、PEP 517/fallback wheel METADATA parity、isolated install distribution/file digests、exact tuple capability probe、restricted-read/external-sandbox evidence、missing/wrong thread/turn id/status、notification failure、timeout、archive/close hang |
| Result | Agent 注入 trusted fields/classification、Worker binding injection、malformed schema、unknown enum、traversal、line OOB、evidence mismatch、duplicate |
| Coverage | full byte-range union vs one-byte/partial/search-only、claim/receipt intersection、0/1/2,000 entries、gap/overlap/order/OOB、reason terminal impact、instruction seal binding |
| Lifecycle | bound/unbound/stale cancel、deadline/lease loss before/during/after turn、crash before/after freeze |
| Publish | exact replay before stale check、ACK loss、key conflict、stale/permanent rejection、bound/unbound disposition、slot hold/release、cancel vs result CAS concurrency |
| Contract | valid/invalid fixtures、additionalProperties、closed registries、generation transaction prepare/publish/crash/concurrent edit/disk full/recovery、跨仓 byte parity、private/public import 边界、manifest/release-manifest 无自引用 |
| Server | DDL constraints、claim/cancel/event/terminal 四事务、cancel binding、quota event idempotency、normalization、DTO redaction、projection recovery |
| Web/Admin | dynamic five steps、partial/debug/ETA、private-field absence、配置删除、390px |
| Benchmark | task-cluster/strata、3-seed repeated measures、Wilson bounds、paired CI、single-side missing、exclusion cap、per-family failure |
| Deployment | 无 mixed v1/v2、stop/drain/revoke/migration/order、v1 reject、pre-activation abort、same-contract rollback、D24 atomic activation |

每个 feature/bug slice 必须保存：

1. 实现前最小 failing test 命令与 failure。
2. 同一 test 实现后通过。
3. focused suite。
4. 项目全套 check。
5. 文件行数和超大文件 frozen baseline。
6. 对应 CI run/check。

纯文档/schema 无运行行为时，可用 schema fixture/generator/check 作 red/green，但需说明例外。

当前/目标命令：

Worker：

```powershell
python scripts\agent_first_decision_register.py check --repo-root .
python scripts\agent_first_slice0_baseline.py check --repo-root .
python scripts\verify_agent_first_contract_baseline.py check --workspace-root ..
python scripts\verify_agent_first_legacy_absence.py --workspace-root ..
python scripts\check_output_contracts.py
python -m unittest discover -s tests -p "test_*.py"
```

exact release slice 使用 Stage 0B 决策并实现的非自引用 verifier `--require-absent`；Stage 0B 完成前不得把现有 strict command 当通过证据。

Server：

```powershell
python -m pytest
```

Web/Admin：

```powershell
npm run check
```

发布前还需 Worker wheel/install probe、Server generator/parity、跨仓 exact-pin、真实 SQLite concurrency 和对应 CI。未运行/超时必须报告缺证，不能写“通过”。

GOV-0B 的 EVD-0 必须先交付 bootstrap-compatible evidence aggregator，Stage A 的 EVD-1 再向后兼容扩展它：

```powershell
python scripts/check_reviewer_refactor_evidence.py check --workspace-root .. --evidence-root <absolute-path-outside-repos> --release-id <id> --stage <0A|0B|A|B|B2|C|D|E|F> --generation <n>
```

它只验证 canonical manifest/signature、signed stage advance（适用时）、decision/instruction bindings、dependency results、direct evidence、artifact digests、commands/CI 和适用门，不替代底层测试；exit 0=`PASS`、1=`FAIL`、2=`INDETERMINATE/NOT_AUTHORIZED/READY/IN_PROGRESS`。EVD-0 只需理解 0A/0B 与通用 manifest/ledger/stage-advance；EVD-1 必须继续 byte-for-byte 验证 EVD-0 fixtures 和历史 generation，并覆盖 A–F。Stage 0A 在工具存在前只产生第 0.3 节 provisional bootstrap，不能自行签发 PASS。

## 15. 文件所有权与实施切片

| 切片 | 主仓 | 主要目录/职责 | 独立验收 |
|---|---|---|---|
| GOV-0A | Worker | current decision/slice0/absence evidence、四仓 inventory | 不改变语义的 drift classification/current check |
| EVD-0 | Worker | minimal evidence/signature/stage-advance schema、writer/verifier、ledger skeleton | bootstrap import、tamper/missing/replay/role/expiry/exit-code fixtures |
| GOV-0B | Worker | RR-GOV、replacement slice0/absence scripts、contracts、docs | 三态/self-reference/true-absence fixtures + signed stage PASS |
| EVD-1 | Worker | A–F evidence/dependency/release-generation aggregator extension | EVD-0 backward compatibility、stale dependency/write-set/CI fixtures |
| CON-0 | Server | non-consumed canonical schemas/registry/fixtures/generator dry-run | target bytes/consumer absence/valid-invalid fixtures；不发布 generated consumers |
| SKILL-1 | Worker | `reviewer_skill/**` | package bytes/binding/eval fixtures |
| RUN-1 | Worker | source inventory policy/snapshot/instruction bundle/read gateway | complete population、pagination/seal、source/instruction/receipt faults |
| RUN-2 | Worker | scratch-only filesystem/runtime policy/SDK session/runner | read/env/network sandbox、SDK/turn/capability |
| RES-1 | Worker | Agent output/result schemas、coverage codec/result validator | trusted-field injection/schema/location/coverage |
| PUB-1 | Worker | terminal candidate/active marker/outbox/rejected-disposition | crash/replay/permanent reject/slot release |
| BEN-0 | Worker/eval | RR-EVAL power/statistics/evaluator policy + synthetic fixtures | clustered paired calculation/三态复算，无 sealed corpus run |
| BEN-1 | Worker/eval | sealed samples/evaluator/report | power gate、runtime cells、clustered paired statistics/三态复算 |
| CON-1 | Server/四仓 | authorized `generation-transaction/v1` 与 exact pins | recovery、schema/parity/exact-pin、all-old/all-new/blocked |
| SRV-1 | Server | terminal CAS/normalization/projection/DB | SQLite concurrency/recovery |
| SRV-2 | Server | routes/quota/debug | route/integration |
| WEB-1 | Web | normalizer/flow/detail/history | check + browser QA |
| ADM-1 | Admin | plan/settings/worker copy | check + mobile |
| CUT-1 | 四仓 | builder/routes/config/deletion/CI/docs | coordinated checklist |

两个切片不得同时修改同一超大 legacy 文件；需触及时先提取 narrow seam，并记录所有权/合并顺序。

### 15.1 依赖 DAG

```text
GOV-0A bootstrap + inert RR-GOV bundle
  └─ RR-GOV-DRAFT-A → RR-GOV-FREEZE-A
       └─ GOV-0B: EVD-0 first → replacement gates → signed GOV-0B PASS
            └─ inert RR-* bundles → RR-*-DRAFT-A → signed A advance
                 └─ Stage A: CON-0 + BEN-0 + policies/fixtures + EVD-1
                      └─ RR-*-FREEZE-A + instruction conflicts=0 → signed A PASS
                           └─ signed B advance
                                ├─ SKILL-1 ─┐
                                ├─ RUN-1 ───┼─ RUN-2 ─┐
                                └─ RES-1 ─────────────┼─ PUB-1 → signed B PASS
                                                     └─ signed B2 advance → BEN-1 → signed B2 PASS
                                                          └─ signed C advance → SRV-1/local E2E → signed C PASS
                                                               └─ exact-one Generate resolution + signed D advance
                                                                    └─ CON-1 → SRV-2 + WEB-1 + ADM-1 + CUT-1 → signed D PASS
                                                                         └─ signed E advance → E PASS → signed F advance → F
```

除第 0.3 节唯一 bootstrap 外，依赖只接受前置 work package 的 exact signed `PASS` manifest 和适用 stage-advance；工作树上“代码看起来已合并”不算依赖完成。可以并行的切片必须有互斥 write set 和共同 pinned input root；任一共享契约、decision、instruction、runtime 或 release generation 变化会使所有下游证据 stale。图中一条箭头不赋予额外权限，仍以 record 中 exact work-package/write-set 为准。

### 15.2 Work-package ledger 与验收证据

EVD-0 在 GOV-0B 生成 `work-package-ledger/v1` skeleton；它列出所有已知 package，但未授权项初始为 `NOT_AUTHORIZED`。每个后续阶段创建不可覆盖的新 ledger generation，不原地修改旧状态。每项至少含 owner/reviewer、repository/write set、stage-advance digest、dependency evidence digest、decision/instruction/contract/runtime/release inputs、red command+failure、green command、focused/full/CI commands、output artifacts、line-count result、derived state 和 superseded-by。ledger manifest 不列自己的 hash；package result 由 aggregator 从直接 evidence 推导并反向引用 ledger manifest digest，避免自引用。手工编辑 state、只引用 PR/commit 或缺 direct command/CI bytes 不能产生 PASS。

| Work package | Entry gate | PASS 必须直接证明 |
|---|---|---|
| GOV-0A | current read-only/document authority；不扩大 D41 | provenance、四仓 inventory、现有 gate raw outputs、inert RR-GOV bundle；零语义变化；仅 provisional result |
| EVD-0 | RR-GOV-FREEZE-A | bootstrap import、manifest/signature/stage-advance/ledger tamper、role/revocation/expiry、exit 0/1/2、deterministic self-check |
| GOV-0B | EVD-0 + RR-GOV-FREEZE-A | replacement 三态、true absence/live present/history damage/self-reference fixtures、旧义务映射、signed PASS |
| EVD-1 | RR-* DRAFT-A + signed A advance | EVD-0 backward compatibility；missing/stale dependency/unauthorized write→exit 2，真实 fail→exit 1，完整 evidence→exit 0 |
| CON-0 | RR-TRUTH-DRAFT-A + signed A advance | canonical draft schemas/registry/fixtures、generator dry-run、target digests、zero runtime/build/release consumers |
| BEN-0 | RR-EVAL-DRAFT-A + signed A advance | power/sample/unit/estimator/missing-run policy、synthetic golden/invalid fixtures；未读 sealed labels |
| SKILL-1 | RR-SCOPE-FREEZE-A + RR-TRUST-FREEZE-A + signed B advance | wheel/install bytes、transitive manifest、explicit SkillInput、无 eval/隐式 surface |
| RUN-1 | RR-TRUST-FREEZE-A + signed B advance | immutable complete inventory、instruction pagination/precedence/seal、五工具协议、不可伪造 ordered receipt |
| RUN-2 | RUN-1 + SKILL-1 | exact runtime capability、scratch-only FS、env/network deny、single turn、bounded interrupt/close |
| RES-1 | RUN-1 + RR-TRUTH-FREEZE-A + signed B advance | untrusted injection rejection、trusted binding、location/evidence、full/partial closed coverage/classifier |
| PUB-1 | RUN-2 + RES-1 | freeze/no-clobber/fsync/replay/crash/permanent rejection/disposition；无生产 submit |
| BEN-1 | signed B PASS + RR-EVAL-FREEZE-A + signed B2 advance | power preflight、sealed corpus、三 runtime cells（适用时）、全部门三态可复算 |
| SRV-1 | signed B2 PASS + signed C advance | 新 DDL constraints、四事务、SQLite concurrency/recovery、terminal CAS/local E2E；无生产入口 |
| CON-1 | signed C PASS + exact-one Generate resolution + signed D advance | `generation-transaction` crash/concurrent-edit/disk-full recovery、canonical parity、四仓 exact pins |
| SRV-2 | CON-1 + signed D advance | intake disabled integration、billing/projection/debug/v1 rejection |
| WEB-1/ADM-1 | public generator transaction + SRV-2 | 只依赖 public v2、旧 fallback/config absent、check + 390px QA |
| CUT-1 | 所有适用上游 signed PASS + D advance | external exact release manifest、strict absence、完整 rehearsal、CI、signed runbook/attestation |

每个新手写 production source/test/script 默认 ≤400 行；401–600 行必须在 ledger 中有 cohesion rationale 和 review；>600 不得进入新架构。当前超大 legacy 文件只允许删除或添加调用 narrow seam 所需的最小变更，不得承接新职责。

## 16. 回滚与 operator runbook

回滚边界以 D24/current-contract activation transaction 为线：

- **激活前 abort**：只有在 v2 accepted task=0、D24/current generation 未改变、旧 schema/storage 未被破坏且旧 token/replica 可按 signed pre-cutover manifest 恢复时，才可整体恢复 exact v1 release。它是仍处于 v1 current contract 时的部署中止，不是 v2 fallback；任何条件不确定就保持 stop-intake。
- **激活后 rollback**：v2 已成为 current 后，允许回滚仅限已签发 v2 stable build，且同时保持：

  - 相同 private/public current contract identity/version/digest；
  - 相同 DB schema/storage semantics；
  - 相同 D24 barrier 与新 task population；
  - 不重新开放旧 route/task/data/Worker。

无此 stable build 时只能 stop-intake、fence/reject 并保留证据。

禁止回滚：

- 回 `ReviewWorkerV1` 或打开 shadow/fallback/legacy flag。
- 恢复旧 protocol/schema/table reader。
- 将 current task 交给旧 Worker。
- 通过 Admin 选择旧 mode。
- 用旧 baseline/QA 覆盖新 release gate。

operator 必须演练：

- stop/reopen intake。
- barrier 前 task 清点与隔离。
- stale lease/event/result rejection。
- ACK loss/outbox recovery。
- same-contract stable rollback。
- 无 stable 时 fence/reject。
- canary auto-stop/证据导出。
- debug bundle 无 source/secret 审计。

演练使用非生产环境和 exact release artifacts，结果进入 attestation evidence。

## 17. 风险、停止条件与估算

出现任一事实即停止或回 Stage A：

1. 近期产品需要写代码、外部 effect、人工 approval 或通用 Agent task。
2. SDK/runtime 无法关闭非允许 instruction/tool/filesystem/env surface，无法形成可信 read receipt，或无法 pin Skill/sandbox/output。
3. paired benchmark 无法通过关键召回、误报、大仓稳定性或成本 non-inferiority。
4. 单 turn 系统性无法诚实 coverage，且小规模拓扑扩展也无数据支持。
5. terminal CAS/clean cutover 无法协调完成，业务又不允许 stop-intake。
6. 从头重跑 SLO 不可接受，且数据证明需 semantic resume。
7. 合规要求每次运行独立 principal/不可抵赖 attestation，离线 release gate 不足。

| 风险 | 缓解 |
|---|---|
| Skill 混入全局 surface | controlled CWD/CODEX_HOME + surface inventory + fail closed |
| 模型读取 auth/control/host state | restricted readable roots 或 external sandbox + sanitized tool env + sentinel faults |
| 大 AGENTS 静默截断 | 自有 manifest/receipt/限额，不依赖默认 merge |
| coverage/read receipt 自报不实 | instrumented gateway ledger + claim/receipt intersection + partial |
| Server/Worker 双终态 | Worker candidate + Server CAS + concurrency/ACK/stale tests |
| 简化降低召回 | paired benchmark + unknown families + 3 seeds + oracle |
| 漏 Admin/计费 | 四仓 inventory + 专属工作包 |
| evidence/gate 自举或自引用 | Stage 0A provisional bootstrap；RR-GOV-FREEZE-A 后 EVD-0 back-validation；manifest/signature/release manifest 均 detached |
| 超大文件继续增长 | 小模块 + 400/600 门 + frozen baseline |
| Python SDK public surface 不足 | Stage B capability probe；受支持升级或外部隔离；否则 NO-GO |
| contract/registry 跨仓漂移 | Server canonical source + journaled generation transaction/recovery + external release manifest exact parity |
| benchmark 样本量不足/伪独立 | power preflight + task cluster + per-family applicability + 三态 |
| mixed-version 部署破坏 clean break | stop/drain/stop-old/deploy-disabled/atomic D24；禁止 rolling mixed authority |

估算必须按下面的 workload model 重新计算，不能仅按 Worker LOC：

```text
engineering_calendar =
  decision_wait
  + critical_path(GOV-0A, RR-GOV, EVD-0/GOV-0B, Stage A/EVD-1, RUN/RES/PUB, Stage C, generation/CUT)
  + max(contract+server+web+admin lane, corpus+oracle lane)
  + ceil(scheduled_benchmark_runs / safe_parallel_slots) * observed_run_p95
  + CI/review/rework
  + canary_minimum_96h
```

同 runtime 的 benchmark 最少约为 `N_clusters × 3 seeds × 2 cells`；runtime 改变时是 `×3 cells`。仅以 13.1 的保守 `N=189` 举例就分别是 1,134/1,701 planned runs，尚未计 per-family 扩样、无效基础设施重排禁止项和 canary。

在决策及时、SDK capability 不阻塞、corpus 可复用且单人连续投入的假设下，当前 planning range 是 12–25 工程周：

- Stage 0A–0B：1–2.5 周，不含授权等待；
- Stage A/EVD/契约冻结：1.5–3 周；
- Stage B Worker candidate：3–5 周；
- corpus/oracle/evaluator + 实际运行：3–7 周；
- Stage C Server/Worker E2E：2–4 周；
- Stage D–F 四仓 clean cutover：2–4 周。

这是区间而非承诺；每个阶段以 ledger 的 actual throughput/p95 更新 forecast。外部授权等待和 canary 至少 24h + 72h 的墙钟时间单列。corpus/oracle、SDK capability 和跨仓 release rehearsal 都可能成为关键路径。

## 18. Definition of Done

只有每项有当前直接证据时才完成：

- **DOD-01** append-only DRAFT/FREEZE 决策对已授权、绑定 exact artifacts 并逐项 supersede；四仓 `instruction-conflict-report` unresolved=0。
- **DOD-02** EVD-0/EVD-1、detached signature/trust policy、work-package ledger 和每条 stage-advance 可复算；Slice-0/replacement 与非自引用 strict absence gate 可用。
- **DOD-03** 生产只有一个 current private contract、一个 runner、一个 Reviewer Skill。
- **DOD-04** attempt exact-bind SDK/CLI/runtime/model/effort/全部 runtime Skill assets/Agent-output schema/result schema/source inventory policy/complete manifest/instruction seal/tool。
- **DOD-05** CWD/CODEX_HOME/scratch-only model FS/tool env/network/credential/approval 全部通过 sentinel/故障注入。
- **DOD-06** slot/lease/bound cancel/deadline/source/budget/outbox/rejected disposition/Server CAS 通过并发/崩溃测试；Worker 无法自创 `CANCELLED` 或自行释放 blocked slot。
- **DOD-07** Agent payload 与 Worker trusted result 分离；binding/classification/identity/location/evidence/coverage/redaction 均由非 Agent 代码生成或验证；one-byte/search-only 不能成为 `inspected`。
- **DOD-08** RR-EVAL offline benchmark 所有适用门、power/sample gate 和 task-clustered paired statistics PASS，无缺证/INDETERMINATE。
- **DOD-09** billing 不再依赖旧 phase；三层 contract、recoverable generation transaction、外部无自引用 release manifest、跨仓 parity 和 public redaction PASS。
- **DOD-10** Web 使用五级 progress/public DTO；Admin 旧配置已删除。
- **DOD-11** D24 barrier、legacy reject、完整 deployment、pre-activation abort、post-activation rollback/stop-intake 演练通过。
- **DOD-12** 30-phase、非目标 Agent Kernel、shadow/fallback/compatibility、旧配置/DTO/table/docs consumers 已在 exact release build/canary 前删除。
- **DOD-13** exact release build 的 strict absence 确定性 exit=`0`/status=`absent`，且 canary/full 对同一 build 重检仍通过。
- **DOD-14** 四仓 local checks 与对应 CI 全绿；CI 不可用不能完成生产 DoD。
- **DOD-15** 同一 exact build 的 5% 窗口满足 ≥24 eligible hours + ≥200 window-local accepted tasks，随后独立 25% 窗口满足 ≥72 eligible hours + ≥1,000 window-local accepted tasks，且两个窗口全部质量/安全/成本阈值 PASS 后才提升到 full；canary 后若改 runtime/schema/contract/build，必须重走 Stage D/E。
- **DOD-16** 四仓 `AGENTS.md` 记录 durable current rules，不把 superseded rules 留作 current。
- **DOD-17** `SPEC-READY-01..12`、content-addressed spec manifest、readiness、machine execution cards/schema/verifier/self-tests 全 PASS；card DAG/write sets/commands 与实际 stage evidence一致。
- **DOD-18** actual principal/key registry、source/artifact/control auth、tenant isolation、fleet/observability/canary queries与 data/evidence lifecycle policy 均 exact-bound，直接故障/删除/恢复证据 PASS。

### 18.1 Completion audit matrix

| DoD | Owner | 最小直接证据 |
|---|---|---|
| 01 | architecture/governance owner | register immutable-history check、DRAFT/FREEZE digests、supersession provenance、四仓 instruction-conflict report |
| 02 | EVD/GOV-0B owner | bootstrap back-validation、tamper/role/expiry/stale stage-advance fixtures、replacement + strict true-absence/live-present/history-damage reports |
| 03 | CUT-1 owner | exact release reference graph、builder/routes/package inventory |
| 04 | Worker owner | attempt binding fixture、wheel/install/runtime capability、complete inventory/page seal、instruction chunk/seal、tool manifest report |
| 05 | Worker security owner | sentinel matrix raw results、sandbox/image/config digests |
| 06 | Worker + Server owner | crash/ACK/stale/cancel/deadline/rejected-disposition/slot-release/SQLite concurrency reports |
| 07 | RES-1 owner | injection/schema/location/full-vs-partial coverage/classifier/redaction tests |
| 08 | benchmark owner + release operator | signed RR-EVAL policy、raw schedule、power output、evaluator PASS report |
| 09 | CON/SRV owner | generation transaction journal/recovery、byte parity、external release manifest、registry/fixtures、billing idempotency、DTO redaction |
| 10 | Web/Admin owners | generated public digest、checks、browser/mobile QA、absence inventory |
| 11 | deployment operator | production-like runbook transcript、D24 transaction、v1 rejects、abort/rollback evidence |
| 12 | four-repo owners | deletion manifest closed、strict catalog/reference graph no unexplained consumer |
| 13 | release operator | exact artifact strict-absence reports at D/E/F，均绑定同一 release digest |
| 14 | four-repo owners | local command logs + immutable CI run ids/artifacts for exact commits |
| 15 | release operator | 5%/25% signed start records、各自 eligible seconds/window-local accepted inventory、quality/safety/cost gates、exact-build full promotion signature |
| 16 | governance owner | 四仓 AGENTS digest、current-rule renderer/check、superseded text audit |
| 17 | spec/governance owner | spec verifier self-test、manifest/readiness/card DAG/write-set/command/fixture digests、`SPEC-READY-01..12` reports |
| 18 | security/reliability owners | principal/key registry、cross-tenant/auth/storage faults、fleet/telemetry dashboards与alerts、lifecycle deletion/restore/hold reports |

最终 `release-attestation.json` 必须逐个列出 DOD-01..18 的 evidence URI/digest/owner/result；缺任一项时 aggregator 只能返回 `INDETERMINATE`，不能用总括性“全部测试通过”替代。

## 19. 立即下一步

1. 当前只开 `GOV-0A`：先确保四仓 clean，按第 0.5 节在仓外创建 no-clobber provisional bootstrap generation，复现并分类 Slice-0/absence drift，生成 provenance、packaging pin 取证、四仓 deletion inventory 和 `proposed-inert` RR-GOV draft bundle；随后仅把 exact-match bundle 作为独立 inert-doc action 应用。没有另一个 exact write-set resolution 时，不修改 baseline/test/script/packaging metadata，即使判断它是机械遗漏。
2. 将无法在现有语义内修复的项目写入 RR-GOV exact target/fixture/obligation mapping；当前至少包括 Slice-0 generated wrapper drift、405 行未登记 test、strict absence self-reference 和 `setup.py` unpinned requirement。不改 expected、不 Generate、不替换 gate，不把 provisional result 称为 signed PASS。
3. 按第 0.6 节以 current register v1 entry + exact-bound packet 分别取得 `RR-GOV-DRAFT-A` 与 `RR-GOV-FREEZE-A` option-anchored resolution；然后 GOV-0B 先按 TDD 实现 EVD-0/back-validation，再实现 replacement gate。缺 FREEZE、packet digest 或 exact confirmation 任一项即停在 GOV-0A。
4. 取得 GOV-0B signed PASS 后，准备 RR-SCOPE/TRUST/TRUTH/EVAL/CUT inert bundles；先取得 DRAFT-A records 与 Stage A advance，完成 non-consumed artifacts/tests，再逐项取得 FREEZE-A。RR-TRUTH 必须明确 supersede D5/D9 的 terminal authority。
5. Stage A 冻结 canonical private/public contract、inventory population、instruction pagination/seal、Agent-output/result/coverage、registry/DDL、gateway/tool/receipt、scratch-only model FS、generation transaction、`semantic_review_started`、RR-EVAL power/statistics 和 EVD-1 ledger；四仓 instruction conflicts 必须为零。
6. 仅在相应 FREEZE + signed B advance 后按 DAG/TDD 建 Skill 和无生产 authority candidate；SDK capability 不能证明就 `NO-GO`。
7. exact-load/surface/env/gateway-receipt/full-vs-partial coverage/terminal rejection faults 未全绿，不签 B PASS、不开始真实 benchmark。
8. power preflight 与 paired benchmark 未 signed PASS，不开始 Stage C–F；Stage D 另需 exact-one Generate resolution，任何部署、流量、canary 或删除仍需对应 advance/发布授权。

最小正确原则：

> **由 Skill 维护审查智慧，由代码维护系统真相，由 Server CAS 维护全局终态。**
