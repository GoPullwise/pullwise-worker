# Reviewer Refactor Evidence and Determinism Contract

状态：`PROPOSED_INERT`

本文件细化主规格第 0、12、15、18 节。它定义可实现的 bootstrap 身份、证据依赖图、确定性边界和 JCS 类型规则；不宣称尚不存在的 collector/verifier 已经交付。

## 1. Bootstrap collector 身份

首个正式 collector 的逻辑身份固定为 `reviewer-refactor-bootstrap-collector/v1`，canonical target 固定为：

```text
pullwise-worker/scripts/reviewer_refactor_evidence.py
```

唯一 GOV-0A 子命令形状固定为：

```text
<python-absolute-path> -I -B scripts/reviewer_refactor_evidence.py collect-gov-0a \
  --workspace-root <canonical-absolute-parent-of-four-repos> \
  --evidence-root <canonical-absolute-reviewer-refactor-evidence-root> \
  --release-id <release_id> \
  --generation <positive-integer> \
  --spec-manifest-sha256 <64-lowerhex>
```

它只接受上述 flags，不接受 shell fragment、命令覆盖、repository path 覆盖、exclude、timeout 覆盖、`--force`、`--resume` 或已有 generation。进程必须使用 argv array 启动。`cwd` 固定为 clean Worker root；四仓路径从 canonical workspace root + closed repo-id mapping 推导并验证 Git top-level，不能由四个独立 caller strings 注入。

当前该 target 不存在，因此正式 `GOV-0A` collector 状态是 `NOT_IMPLEMENTED`，`SPEC-READY-04-BOOTSTRAP` 不能 PASS。当前可以运行主规格 0.5.3 的既有只读命令做人工审计，但输出不得冒充 `reviewer-refactor-evidence-manifest/v1` 或 signed/provisional GOV-0A generation。

RR-GOV packet 必须绑定 collector 的 proposed exact source bytes、source SHA-256、installed-script SHA-256、Python executable/version/digest、CLI descriptor digest、valid/invalid fixtures和唯一写根。collector 首次落库与启用属于 RR-GOV exact write set；本文不让一个未来脚本追认自己的创建权限。

## 2. Collector 安全与发布原子性

collector 必须满足以下机械条件：

1. 启动前对 workspace/evidence root 做 handle-based canonicalization；拒绝 overlap、symlink/reparse、Git worktree/object store、model-visible root 和宽松 ACL。
2. 在 evidence root 内以 owner-only 随机 sibling staging 创建 bytes；目标 generation 在整个运行期间必须 absent。
3. 所有 catalog commands 用 direct argv、固定 timeout/output cap、空环境 + allowlist 运行；原始 stdout/stderr bytes 原样保存。
4. 初始与结束分别 hash 全部 bound inputs、四仓 HEAD/status；任一漂移使结果 `INDETERMINATE`。
5. 先 fsync/flush 每个文件和目录，再用 same-volume no-clobber publish；目标存在或原子/持久性不可证明时不覆盖。
6. collector 自身不得 append decision、改 worktree、修 baseline、生成 package、运行真实 benchmark、触达生产或删除历史。
7. 无论 provisional verdict 为何，formal bootstrap 进程 exit 固定为 `2`，因为 EVD-0 back-validation 前不存在 verified stage PASS。

collector 的 `--self-test` 是独立离线模式，只在新的临时目录运行 bundled fixtures，必须覆盖 path escape、case collision、symlink/reparse、dirty/drift、timeout、truncation、secret、existing target、partial publish 和 serializer mismatch。self-test PASS 不等于 GOV-0A PASS。

## 3. Evidence dependency DAG

每个引用使用 exact manifest SHA-256 + content root，不使用可变 branch、artifact label 或“latest”。允许的依赖方向固定为：

```text
spec snapshot + current authority snapshot
  -> GOV-0A provisional generation
      -> RR-GOV DRAFT/FREEZE packet
          -> EVD-0 implementation evidence
              -> GOV-0A back-validation generation
              -> GOV-0B replacement-gate generation
                  -> Stage A advance + EVD-1
                      -> package generations
                          -> stage PASS generation
                              -> next stage advance
                                  -> release generation
                                      -> canary windows
                                          -> final attestation
```

约束：

- edge 只能从新 artifact 指向已发布且不可覆盖的旧 artifact；禁止 forward ref 和环。
- decision packet 可以引用 provisional bootstrap；bootstrap 不得引用后来才产生的 decision resolution。
- manifest 不 hash 自身或 detached signature；signature 不回填 unsigned body。
- package result 可以引用其输入 ledger generation；同一个 ledger generation 不得反向引用该 result。aggregator 在下一 ledger generation 收录结果，避免双向自引用。
- URI 只作 locator；verifier 以 digest 为 identity，并重新计算 size/content root/schema。
- 依赖 artifact 缺失、stale、被撤销、签名 purpose 不符或形成环时 verdict 为 `INDETERMINATE/NOT_AUTHORIZED`，不得当作普通 test failure。

## 4. 三种不同的“确定性”

不得再用一个 `byte-identical` 同时描述写入重跑和只读验证。

### 4.1 Immutable-generation integrity

同一个已发布 generation 的每个 regular file、manifest bytes、外部 manifest SHA-256 和 content root 必须永久不变。任何变更是 tamper，旧 generation 永不修复或续写。

### 4.2 Same-generation verifier determinism

只读 verifier 对同一个 immutable generation、同一个 trust/policy/input snapshot 连续运行两次，必须产生：

- 相同 exit code；
- byte-identical `verification-verdict.json`；
- 相同 reason/evidence refs 顺序。

`verification-verdict.json` 不含 invocation id、host path、started/finished/verified time、duration、nonce 或随机值。运行时诊断另存为不参与 verdict identity 的 invocation envelope；该 envelope不得被 stage PASS 引用。

### 4.3 Fresh-generation reproducibility

重新采集必须使用新 generation，因此 `manifest.json`、`result.json`、raw command bytes 和时间通常不同，禁止要求整包 byte-identical。两个 fresh generations 只比较 `stable-projection/v1`：

- 删除 schema 明列的 volatile fields；
- 把 release/generation/attempt-specific identity 替换为固定 typed sentinel；
- 保留 input digests、command argv/executable digest、exit、structured gate result、artifact digests、reason codes、decision bindings和排序；
- projection 由 verifier从原始 bytes机械产生，不修改原证据。

volatile allowlist 固定为：`captured_at/started_at/finished_at/duration_ms/release_id/generation/raw timing/CI run locator/local absolute root`。不得把 exit、status、reason、HEAD、input/artifact digest、command bytes、tool version、policy或signature classification 标为 volatile。fresh projection 不一致意味着真实漂移，结果至少为 `INDETERMINATE`；不能更新 expected 掩盖。

## 5. Evidence schema 分层

| 层 | 允许内容 | 权威用途 |
|---|---|---|
| raw capture | 原始 stdout/stderr/exit、input snapshots | 审计与重新解析；不直接签 stage PASS |
| structured facts | parser/version-bound gate facts、artifact inventory | verifier 输入 |
| result | 六态 verdict、closed reasons、直接 evidence refs | work-package/阶段结论 |
| manifest | 文件闭包、size/digest/content root | generation identity |
| detached signature | purpose/role/key/body digest/signature | 授权和不可抵赖性 |
| stable projection | schema-governed跨 generation 比较 | reproducibility；不是原始证据替代品 |

parser 必须保留原始 bytes，且 structured fact 引用 raw path/digest/parser digest。未知 parser output、zero-test、truncated stream、非 closed exit/reason 或原始/结构化不一致为 `INDETERMINATE`。

## 6. JCS Profile 1 与数值类型

所有进入 digest/signature 的 JSON 继续使用 Pullwise JCS Profile 1：ASCII object keys、UTF-8/NFC、safe integer、无 float、无 duplicate key、canonical key order。业务比例不得以 JSON number float 表达。

统一换算：

- finding 置信度：`confidence_bps`，integer `0..10000`；显示值仅由 consumer 机械计算，`10000 = 100%`。
- 比率/阈值：`rate_ppm` 或以明确 numerator/denominator 表达，integer `0..1000000`。
- 金额：`cost_microunits` + closed currency/model billing unit。
- duration：integer milliseconds；timestamp 为 UTC RFC 3339 seconds string。
- 统计临界值若不能安全整数化，以 canonical decimal string + scale/rounding enum 表达；evaluator 禁止解析为平台默认 binary float 后签名。

schema、fixtures、Skill 和 public projection 不得再出现 numeric `[0,1]` confidence。显示层可转换为 decimal，但显示值不进入 identity、gate 或签名。

## 7. Verifier 顺序与 exit contract

验证顺序固定为：

1. root/path/file-kind/size safety；
2. JSON parse、schema、canonical byte compare；
3. manifest closed set、file digests、content root；
4. detached signature purpose/role/key/revocation/expiry；
5. authority/instruction/spec snapshot binding；
6. DAG dependency existence、PASS、freshness、acyclicity；
7. package-specific semantic checks；
8. result derivation与 stable projection。

exit `0` 仅 `PASS`，exit `1` 仅已证明的 `FAIL`，exit `2` 对应 `NOT_AUTHORIZED/READY/IN_PROGRESS/INDETERMINATE`。验证器 crash、未知 enum、证据缺失或依赖不可达永远不是 PASS。

## 8. 必需 fixture catalog

EVD-0/EVD-1 至少包含：

- valid minimal、valid multi-file、empty stdout、zero-byte artifact；
- manifest tamper、missing/extra file、digest/size drift、自引用、case collision、path escape、symlink/reparse/hardlink；
- duplicate key、non-NFC、BOM、float、unsafe integer、noncanonical bytes；
- signature wrong purpose/role/key、revoked/expired、threshold 缺失、body digest mismatch；
- dependency missing/stale/cycle/forward ref、same id different bytes；
- same-generation verifier invocation twice byte-identical；
- two fresh generations with only allowed volatile differences projection equal；
- forbidden field被标 volatile时 projection verification FAIL；
- result/exit mapping、zero-test、timeout、truncation、secret and dirty/input drift。

fixture manifest 必须给每个 case 一个 closed expected verdict/reason，并由另一个实现或人工 byte audit交叉核验 golden bytes。
