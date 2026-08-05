# Reviewer Refactor Operations and Execution Standard

状态：`PROPOSED_INERT`

本文件细化主规格第 12、14、15、16、17、18 节，定义 signer/key binding、fleet、observability、canary 数学、数据/证据生命周期和 machine execution cards。

## 1. Owner 与 signer binding

逻辑 role 不能直接签名。Stage A 前必须发布 `reviewer-principal-registry/v1`，每个 active role exact-bind：

- stable `principal_id`、human/workload 类型和组织身份来源；
- key ids、算法/encoding、purpose allowlist、not-before/not-after；
- environment/repository/stage scope；
- separation-of-duty conflicts；
- rotation/revocation/compromise procedure和审计 owner。

registry 不保存 private key。`TBD`、共享个人账号、仅邮箱文本、可导出到 Worker env 的 key 或同一 principal 同时担任互斥角色均无效。

最低职责分离：

| Record | 必需签名 | 同一人/身份禁止兼任 |
|---|---|---|
| RR decision FREEZE | architecture/governance owner | packet implementer 的唯一 reviewer |
| Stage B PASS | Worker owner + independent reviewer | evidence aggregator service |
| B→B2 | architecture owner + benchmark owner | candidate implementer |
| benchmark attestation | benchmark owner + release operator | raw-run producer/evaluator service |
| D release attestation | release operator + security/reliability reviewer | generation transaction service |
| D→E / canary start | release operator + deployment operator | candidate code owner作为唯一 signer |
| E→F | release operator + deployment operator | telemetry aggregator service |

human signatures 需要 phishing-resistant MFA和受控 signing UI/CLI显示 exact body digest/purpose/stage/write set/forbidden set。workload signatures只能证明自动 evidence，不能代替 human authorization。

默认 cryptoperiod 上限：human release key 90 天、workload key 24 小时、session credential 15 分钟、source/artifact grant 5 分钟。rotation overlap最多 24 小时；record按签名时有效性验证，并在每次 stage transition重新检查当前 revocation。key compromise使受影响且尚未消费的 advance/promotion立即 `NOT_AUTHORIZED`；已执行操作进入incident审计，不能改写旧证据。

实际 principal/key 尚未进入 registry前，`SPEC-READY-11-RELEASE` 为 `OPEN`。

## 2. Fleet contract

每个 Worker process只有一个 slot。Server registration allowlist exact-bind worker build、contract root、runtime、SDK/CLI、Skill、tool、sandbox/image/kernel和capability report digests。未知或 stale tuple进入 `QUARANTINED`，只能 health/diagnostic，不能 claim。

fleet state closed enum：`STARTING/ATTESTING/READY/BUSY/DRAINING/QUARANTINED/STOPPED`。Server是 claim eligibility authority；Worker自报 READY不够。health至少包含 session age、heartbeat lag、slot/outbox state、disk/temp capacity、clock offset、provider/control reachability和last capability self-check。

- clock offset超过冻结 bound时不签发/接受 lease、grant或signature-bearing record；
- disk低于一次最大 source + validation copy + outbox + safety reserve时不 claim；
- runtime/contract/Skill digest变化先 drain，不能原地把 BUSY process变成新 generation；
- revoke/drain不改写已 frozen outbox，仍只允许 exact terminal replay或operator fence；
- crash-loop、sandbox sentinel failure、secret scan、receipt-chain fault或same-id/different-digest立即 quarantine并auto-stop promotion；
- mixed v1/v2 current authority永远禁止；closed capacity不是流向旧 Worker的fallback。

## 3. Observability contract

所有指标、日志、trace和audit event从 closed registry生成；free-text不驱动自动门。允许的低基数 metric labels仅：environment、region/pool、release digest短id、contract version、runtime family、worker state、terminal classification、closed reason family。tenant/repository/path/job/run/attempt/finding/token不得做metric label。

最低 metrics：

- fleet：registered/ready/busy/draining/quarantined slots、heartbeat lag、clock offset、disk reserve；
- claim/run：accept、claim latency、active attempts、deadline/cancel、terminalization latency/classification；
- runtime：SDK/turn/tool errors、interrupt/close、context preflight/rejection、tool calls/bytes；
- gateway：instruction/source receipt count、sequence/hash faults、validation outcomes；
- publish：outbox age/replay、artifact upload/commit latency、CAS accept/reject/idempotency conflict；
- safety：credential/network/path/sandbox/tenant/private-public redaction violations；
- cost：provider usage/cost microunits按 closed model billing unit，不记录prompt/source。

每个 accepted task必须在 telemetry completeness ledger有 exactly one current accounting row，最终映射到 evaluable terminal或带 closed reason的未决/censored状态。metrics聚合不能代替 event/audit ledger。log/trace body经过同一 redaction library，默认不记录source、prompt、finding正文、target instruction、credential、absolute path或raw SDK event；需要debug内容时写 access-controlled artifact并使用descriptor。

Stage D freeze exact SLO/alert thresholds、scrape/event loss detection、clock source、dashboard/query version和auto-stop action。telemetry gap自身使canary eligible clock暂停；“没有告警”不是 telemetry PASS。

## 4. Canary capacity mathematics

canary 是 current v2 contract 的 capacity gate，不是 v1/v2 traffic split。每个 window start record固定一个 `eligible-capacity-snapshot/v1`：同 environment/pool、exact release/runtime/contract、attested且READY的slot集合和content root。

对目标百分比 `p` 和 snapshot slot数 `C`：

```text
K = (C * p + 99) // 100
```

其中每slot capacity固定1，`//`为integer floor，所以K是向上取整。5% window要求 `C >= 20`，25% window要求 `C >= 4`；否则无法诚实声称capacity百分比，结果 `INDETERMINATE`。只启用snapshot中按signed sorted slot list选出的前K个slot，其余v2 capacity关闭且绝不导向v1。pool/region策略在start record冻结；不得用总体百分比掩盖某个独立故障域100%暴露。

window期间：

- selected slot缺失、available selected capacity `< K`、非selected slot获得claim、release/runtime/contract变更或denominator集合变化时eligible clock暂停；
- replacement slot需要新的signed capacity snapshot并重新开始该window，不能后台替换后继续累计；
- queue backlog可以增长，但不能开启旧路径；达到业务保护阈值时stop-intake/fence；
- 5%窗口最长14个calendar days，25%最长30天。到期仍未满足task门为`INDETERMINATE`，需新window/新授权，不能只延长时间字段。

低流量没有捷径：5%必须同时满足至少24 eligible hours与200个window-local evaluable terminal tasks；25%必须重新满足72小时与另1,000个tasks。时间与task均不足时不promote。达到窗口截止后，允许最多一个冻结的settlement horizon（不大于2倍最大scan deadline）等待已accepted task终态；仍未决的task按canary policy计failure或INDETERMINATE，不能从分母删除。

## 5. Canary telemetry and promotion gates

Stage E前签发 `canary-policy/v1`，绑定metric query/version、stable telemetry cohort、numerator/denominator、integer rounding、confidence method、threshold、missing-data和auto-stop。stable cohort使用cutover前冻结的current stable连续14个eligible days，按同task eligibility/mapping；无法映射的relative metric为`INDETERMINATE`。

每个window至少计算：

| Metric | 定义/门 |
|---|---|
| telemetry completeness | expected accounting rows全部可复算，`missing_count=0` |
| authority/security violations | tenant escape、credential/network/source mutation、mixed contract、stale/conflicting terminal、public source/secret leak均observed `0` |
| internal failure rate | Worker/runtime/sandbox/gateway/artifact/CAS internal failure task / accepted task；one-sided 95% upper `<= 50000 ppm`且相对stable增加upper `<= 20000 ppm` |
| partial rate |非用户取消、非预注册source-policy拒绝的PARTIAL / evaluable tasks；相对stable增加upper `<= 50000 ppm` |
| terminalization |在`2 * scan_deadline`内达到global terminal / accepted；one-sided 95% lower不低于stable lower减`30000 ppm` |
| p95 wall time |同eligible task strata verified-success task-level p95 `<= stable * 120 / 100` |
| cost |verified-success task cost microunits p95 `<= stable * 120 / 100` |
| output audit |signed deterministic random sample；5%至少50 tasks/20 findings，25%至少200 tasks/50 findings；critical false verified=0、private leak=0、rubric/DTO valid=100% |

rate与delta以integer ppm或numerator/denominator签名；Wilson/paired bootstrap的decimal endpoint以canonical decimal string + scale保存，不在JCS body使用float。若stable denominator为0、stratum无法匹配、query漂移、sample未完成或confidence bound不可算，门为`INDETERMINATE`。

任一authority/security zero-tolerance event立即：stop new claims、写auto-stop record、保留selected workers/outboxes、启动incident runbook；不得等窗口结束。其他门FAIL停止promotion并按同contract rollback或stop-intake处理。所有门PASS也只允许生成下一promotion proposal，仍需独立签名。

## 6. Data and evidence lifecycle

Stage A必须发布 `reviewer-data-lifecycle-policy/v1`，对每个data class给出非空exact值：owner、tenant scope、classification、storage locations、encryption/key、retention start、minimum/maximum retention、legal-hold、deletion trigger/SLA、backup/replica/cache处理、deletion evidence和允许reader。不能使用`forever/default/TBD`。

至少覆盖：

- source transport object、private ingest、immutable snapshot、inventory/instruction bytes；
- model-turn scratch、CODEX_HOME/session state、validation copy、temp/cache；
- raw Agent output、validated result、bounded evidence span、debug/audit/public artifact；
- active marker/outbox/candidate/receipt、Server rows/events/projection outbox；
- GOV/eval/release/canary evidence、decision history、CI artifacts；
- logs/metrics/traces、credential/key metadata和revocation records。

安全下限：scratch/validation/temp在attempt结束或crash recovery后优先删除且不备份；source和credential bytes不得进入logs/metrics/general CI artifact；tenant deletion覆盖object/database/index/cache/replica并保留不含内容的deletion tombstone；legal hold只由受控principal签发且有expiry/review。加密at-rest/in-transit、key scope/rotation/destruction和restore test必须有直接证据。

删除是状态机：`REQUESTED -> FENCED -> PRIMARY_DELETED -> REPLICAS_EXPIRED -> VERIFIED`，失败/unknown保持可见并告警。hash-only audit是否构成个人/客户数据由policy明确；不能假设“只有digest所以可无限保留”。release/decision evidence的合规保留与客户source保留分开，不允许为审计把source复制进长期证据。

## 7. Machine execution cards

`docs/reviewer-refactor/execution-cards.json` 是工作包的machine index，不是authority。每张card exact keys至少为：

```text
schema_id/id/title/stage/authority_state
owner_role/reviewer_roles/repositories
write_set/forbidden_set/dependencies
decision_bindings/input_artifacts
red_commands/green_commands/focused_commands/full_commands/ci_commands
outputs/rollback/pass_predicates/line_policy
```

命令不是自由字符串；每项含`command_id/cwd_repo/argv/timeout_seconds/expected_exit/evidence_outputs`。`argv`保持顺序，禁止shell解释、placeholder和`latest`。尚未冻结的真实path/command不得伪造：card以closed blocking predicate标记`NOT_AUTHORIZED`，并把其定义artifact列为dependency；只有FREEZE/advance生成的新card generation可填exact bytes。

规则：

- card id与主规格work-package ledger一一对应；unknown/missing/duplicate/case-collision均FAIL；
- dependency DAG必须acyclic，依赖只能引用同或更早stage的signed PASS；
- 并行card的write set必须disjoint；glob/prefix重叠由verifier按canonical path计算；
- owner/reviewer role在实施前必须解析到principal registry，且满足职责分离；
- `authority_state`由verifier从decision/advance/evidence推导，不能由implementer手改成PASS；
- red必须先以预期closed reason失败，green不能靠改expected/fixture/exclusion；focused/full/CI各有直接bytes；
- output artifact必须有schema/path/manifest identity与consumer state；non-consumed draft不可被build/runtime/release引用；
- rollback仅撤销card声明的未发布target，禁止reset/restore用户其他改动；已经current的数据/contract只按runbook state machine处理。

## 8. Runbook command contract

Stage D runbook的每一步必须是machine card或受控operator command，含precondition、argv/API request digest、expected state/version、timeout、idempotency/retry、evidence output、abort和next step。禁止“部署服务”“观察正常”“必要时回滚”这类不可验证句。

runbook至少包含：snapshot/authority check、stop-intake、drain/fence、active/outbox inventory、v1 credential revoke、stop old Worker/Server、migration lock、schema apply、deploy-disabled、synthetic smoke、D24 activation CAS、capacity snapshot/start、telemetry query、auto-stop、same-contract rollback、evidence export和finalize。每个mutating step先有read-only exact-target check；destructive data action另需适用授权和recoverability证明。
