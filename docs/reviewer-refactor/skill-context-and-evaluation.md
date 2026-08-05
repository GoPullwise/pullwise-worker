# Reviewer Skill, Context, and Evaluation Contract

状态：`PROPOSED_INERT`

本文件细化主规格第 5.3、5.4、6、9 和 13 节，定义 Reviewer Skill 的语义、target instruction 信任级别、单 turn context 可行性、SDK capability gate 和首发 stable baseline。

## 1. Reviewer Skill 的职责

Reviewer Skill 只决定“怎样审查与怎样表达 findings”，不决定权限、工具、source population、coverage truth、deadline、budget、tenant、terminal、billing、upload、release 或 retry。

Skill runtime assets 的 normative section ids 固定为：

1. `RSK-OBJECTIVE`：只报告当前代码中可触发、可解释、可行动的问题。
2. `RSK-EVIDENCE`：location、source evidence、failure scenario、assumption 与 validation discipline。
3. `RSK-SEVERITY`：严重性 rubric。
4. `RSK-CONFIDENCE`：`confidence_bps` rubric。
5. `RSK-PASSES`：审查次序和 counterexample pass。
6. `RSK-FP`：误报抑制、去重和不报告项。
7. `RSK-INSTRUCTIONS`：target instruction 的有限信任模型。
8. `RSK-TOOLS`：五个 typed tools 的使用纪律。
9. `RSK-OUTPUT`：strict output schema 和 limitations。

任何 runtime Skill 文件增加新的 effect、工具、网络、权限或 terminal 语义都超出 RR-SCOPE/TRUST，必须新 decision，而不是普通 prompt edit。

## 2. Finding admission contract

一个 main finding 必须同时满足：

- 指向 sealed inventory 中的 canonical regular-file location，span/hash 与实际 bytes一致；
- 描述当前代码可到达的具体 failure scenario，而不是抽象“可能更好”；
- 给出影响对象与边界，区分事实、推断和前置假设；
- 给出可行动的修复方向，不要求某个唯一实现；
- 列出 false-positive risk、反例和已执行/未执行的 validation；
- 不与同一 instance identity 的更强 finding重复；
- `confidence_bps >= 7000`。低于门槛的真实疑点只能进入 limitations/follow-up，不得为凑数量进入 findings；
- 不是纯格式、命名、主观可读性、缺少注释、无当前消费者的未来扩展或已被代码/测试明确防住的情况。

`validation_status` closed enum：

| 值 | 含义 | finding 可否发布 |
|---|---|---|
| `reproduced` | bounded validation profile直接复现 failure | 是 |
| `mechanically_supported` | source/state/schema/constraint证据闭合，但无需/无法安全执行 | 是 |
| `not_run` | 未运行，且给出 closed reason与保守 confidence | 仅当其他 admission 全满足 |
| `inconclusive` | validation 无法得出结论 | 只能降 confidence；低于门槛进入 limitation |
| `disproved` | 反例或 validation 否定 finding | 否，必须删除 |

Agent可以提出上述字段，但 Worker验证 enum、source/location/evidence和 receipt；Worker不能把 `disproved` 或 invalid location“修成”finding。

## 3. Severity 与 confidence rubric

severity 与 confidence 正交，不得因为确信而抬高影响，也不得因为影响大而伪造确信：

| Severity | 客观影响门 |
|---|---|
| `critical` | 可现实触发的 tenant/security boundary突破、不可恢复的大规模数据破坏、任意代码/凭据控制、全局 terminal/authority被非授权改写，且无有效前置防护 |
| `high` | 核心流程广泛失败、重要数据完整性/授权/并发安全破坏、显著生产事故，影响不是局部可忽略 |
| `medium` | 有界但真实的功能错误、协议不一致、资源/并发泄漏、错误恢复或测试缺口会让已支持场景失败 |
| `low` | 可观察且可行动的局部正确性/可靠性问题，影响较小；纯风格不属于 low |

`confidence_bps` anchors：

- `9500..10000`：直接复现或形式/机械约束唯一推出；
- `8500..9499`：完整 source/control flow 与现有 tests/contract 强支持，反例已检查；
- `7000..8499`：failure path具体且证据充分，但有明确未验证前提；
- `0..6999`：不进入 findings。

同一 finding 的 confidence 只能按 frozen rubric机械/人工评审，public consumer不得把它当概率校准值。benchmark单独评估 calibration，不允许解盲后改 anchors。

## 4. 审查 passes

在一个 main turn 内按下列语义次序工作；这不是对外 progress phase，也不产生计费状态：

1. 校验 inventory seal并读取每个适用 scope 的完整 instruction seal。
2. 建立入口、数据流、状态/错误/并发/资源边界的最小模型。
3. 审查 correctness 与 state transitions。
4. 审查 auth、tenant、secret、filesystem/network/tool 与 input trust。
5. 审查 API/schema/storage/migration/backward-incompatible行为。
6. 审查 deadline/cancel/retry/idempotency/crash/recovery/resource limits。
7. 审查 tests 对关键失败路径的覆盖；只报告会掩盖真实回归的 test gap。
8. 对每个候选主动寻找 guard、caller invariant、dead-code、config或测试反例，去重并校准 severity/confidence。
9. 输出 findings、limitations 与非权威 coverage claims；不复述整仓、不输出表扬或通用最佳实践。

Pass 顺序可因语言/仓库性质调整，但 admission/rubric/coverage不变。target instruction可提供 domain hints，不能跳过强制 pass或改变安全门。

## 5. Target `AGENTS.md` 的有限信任模型

受审仓库中的 `AGENTS.md` 是 repository-authored、可能由攻击者控制的 policy input，不是 Pullwise platform/system/developer authority。Worker把 exact bytes通过 `instruction_read` 呈现给 Reviewer Skill，并应用以下 precedence：

```text
platform safety + signed Server/Worker contract
  > Worker immutable runtime/tool/source/coverage policy
    > manifest-bound Reviewer Skill
      > scoped target AGENTS.md review guidance
        > ordinary source comments/README strings
```

target instruction 允许：

- 解释 domain invariants、supported platforms、generated-code ownership和项目验证命令候选；
- 指定报告语言、项目术语、某 scope 的审查重点；
- 指出已知限制，但其事实仍需 source/contract验证。

target instruction 不允许：

- 扩大/替换 tool、filesystem、network、credential、approval、budget、deadline或validation profile；
- 隐藏 inventory entry、把未读文件标 inspected、改变 severity/confidence/admission或 terminal；
- 请求读取 host/other tenant/auth/control/eval fixture，执行任意 shell或发送数据；
- 覆盖 Skill/Worker policy、要求忽略更高层指令或把源代码字符串当新指令；
- 自称 trusted、signed、operator-approved而没有 gateway/Server binding。

Worker/Skill把越界句分类为 `instruction_effect_denied`，保留 scope/path/range/digest receipt并继续在安全边界内审查；若越界内容使 domain guidance无法安全分离，attempt为 `PARTIAL/FAILED`且报告 limitation。不得执行后再“注意到是注入”。

必需 fixtures覆盖 nested precedence、合法 domain constraint、要求泄露 token、要求调用网络/任意 shell、要求跳过文件、伪造 system message、source comment注入、超限/非 NFC/case collision和 instruction在首个 source read后才到达。

## 6. 旧 prompt/Skill 迁移 ledger

Stage A 交付 `reviewer-skill-migration/v1`，对所有 production-reachable旧 prompt、template、rule、phase prompt和隐式 Reviewer text做完整枚举。每项 exact keys：

```text
semantic_unit_id
source_path/source_sha256/byte_start/byte_end/source_text_sha256
current_consumer_refs
disposition = retain_verbatim | rewrite | delete | runtime_policy_not_skill
target_section_id/target_text_sha256
rationale
eval_fixture_ids
owner/reviewer
```

ledger source population由 reference graph产生，不能只迁移“看起来重要”的文件。`rewrite` 必须记录语义差异；`delete` 必须说明旧行为为何非目标并有 absence fixture；security/authority/budget/terminal内容必须移到 code policy，不能继续藏在 Skill。每个 retained/rewrite semantic unit至少映射一个 positive或counterexample fixture。unknown consumer或unmapped unit使 `SKILL-1` FAIL。

cutover 后 reference graph必须证明生产只读 manifest-bound Reviewer Skill，旧 prompt bytes仍可作为不可执行历史保留，但没有 import/config/runtime consumer。

## 7. Context-budget preflight

“最多 2,000 entries”不是单 turn 可行性的充分条件。每个 exact runtime tuple必须有 `context-budget-policy/v1` 和 attempt-specific `context-budget-report/v1`。

令：

```text
C = capability probe报告的模型 context window token上限
F = exact system/developer/Skill/tool schemas/bootstrap固定 transcript tokens
I = 全部适用 instruction chunks + receipts + envelopes的最坏 tokens
S = 全部 mandatory source bytes按固定 chunking返回 + receipts + envelopes的最坏 tokens
V = 允许的 validation/search结果与tool-call argument上限 reserve
O = max output tokens + 一次 format-repair输入/输出 reserve
R = max(8192, ceil(C * 15 / 100)) 的 reasoning/event/safety reserve
```

preflight PASS 必须满足 `F + I + S + V + O + R <= C`，并为每项保存 exact integer token count和 serializer/tokenizer digest。所有百分比用整数运算；rounding固定向上。

计算规则：

- 使用 exact model/runtime 声明并经 probe验证的 tokenizer/version/digest；未知 tokenizer、context window或SDK usage semantics直接 `INDETERMINATE`。
- 对 sealed inventory 中每个 mandatory regular text entry计算完整 bytes在真实 tool-response JSON/envelope中的 token数；不能用 `bytes/4`、文件抽样或平均值。
- inventory pages、instruction chunks、source read request/response、receipt ids/digests、最大 path/argument、固定 tool-call count和 output schema全部计入。
- optional search/validation只有在 `V` 预留内可用；耗尽 reserve时拒绝新调用，不挪用 mandatory source或output reserve。
- runtime必须关闭不可观察的自动 compaction，或提供可验证的 compaction event与语义保证。当前方案选择“不允许 hidden compaction”；无法证明时 Stage B NO-GO。
- Worker在每个 event后用实际 usage重新核对上界。unexpected overhead触及 hard stop时停止工具调用；只有已有 validated findings且coverage诚实时可 `PARTIAL`，否则 `FAILED`。

preflight 不满足时不启动 main turn，closed reason=`CONTEXT_BUDGET_EXCEEDED`。不得把 full-scan任务自动降为抽样、拆成隐式多 turn或声称全仓 coverage。若大仓是产品必需且经 corpus证明频繁拒绝，必须回到 RR-SCOPE重新决策 topology，而不是在实现中偷加 fanout/compaction。

## 8. SDK/CLI/App Server capability gates

capability验证分两层，避免把关键机制拖到实现后才发现不可表达：

### `CAP-0` architecture feasibility（Stage A freeze 前）

- 对 exact proposed Python distribution/wheel、CLI/App Server executable和公开 API做 artifact/introspection/loopback probe；
- 枚举 `SkillInput`、strict `output_schema`、tool/MCP/plugin/hook config、sandbox wire、event/usage、interrupt/close和context/compaction surface；
- 构造实际 request/config bytes并验证生成类型能表达 restricted model filesystem与typed bridge；
- 不读取客户 source、不运行 benchmark、不接生产，不把静态注解当行为证据。

只有 CAP-0证明某机制“可由受支持 surface表达”，RR-TRUST才能把它写入 FREEZE。当前 `openai-codex==0.1.0b3` 的公开 Sandbox wrapper不能表达 restricted readable roots，因此它若无经验证的 external sandbox路径，候选机制必须标 `UNSUPPORTED`，不能留到编码时猜。

### `CAP-1` runtime enforcement（Stage B）

在 exact candidate runtime中用真实 isolated turn和sentinel验证 allow/deny、provider/control可用、tool无credential/network、surface inventory、output schema、interrupt/deadline/close、context accounting和hidden compaction。CAP-1改变任何 tuple都会失效重跑。

Worker current `AGENTS.md` 的 standalone CLI `latest` 与 exact tuple 冲突必须在 instruction-conflict plan中解决；正式 release禁止一条 build path exact-pin、另一条下载 latest。

## 9. 首发 stable baseline

在 candidate实现或 sealed corpus解盲前，RR-EVAL必须签发 `stable-baseline/v1`：

- stable release commit、Worker wheel/build/source digest；
- 当前 production runner/prompt/Skill/contract/generator/config exact digests；
- model/provider/effort/service tier、SDK/CLI/runtime/machine class/budget；
- corpus task/seed schedule commitment（不含对实现者可见的 sealed labels）；
- stable output到统一 evaluator model的 versioned mapping与loss audit；
- owner、冻结时间、expiry和不可比规则。

首发 stable 默认是冻结时实际 current `ReviewWorkerV1` signed release，不是工作树、latest branch或事后挑选的最佳 run。若无法取得/重建 exact artifact和语义资产，baseline为 `INDETERMINATE`，不得用candidate自己的早期版本或通用分数代替。

当candidate runtime与stable native不同，主规格三-cell bridge仍适用。`S-candidate-runtime`只能改变RR-EVAL明列的runtime tuple；若为运行它而改stable业务逻辑、prompt、contract、tool semantics或output meaning，则不能称baseline。旧/新输出shape通过冻结的loss-accounted evaluator mapping比较；无法无偏映射的指标为`INDETERMINATE`。

baseline、mapping、threshold、exclusion、seed、runtime cell或corpus commitment在解盲后任何改变都要求新benchmark generation，旧结果不得promote。

## 10. Skill/eval fixture coverage

fixture catalog至少含每个 severity family的real defect、bad fix、clean counterexample、guarded false positive、unreachable code、generated/vendor、nested instruction、prompt injection、missing dependency、validation timeout和context rejection。每个 Skill semantic unit双向映射fixture；fixture未覆盖的runtime rule不能被称为迁移完成。

离线 eval assets永不进入runtime Skill manifest或model filesystem。构建与wheel tests必须证明fixture path/bytes不可达；任何 leakage使SKILL-1和benchmark同时FAIL。
