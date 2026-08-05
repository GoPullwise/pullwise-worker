# Reviewer Refactor Runtime Contract and Security

状态：`PROPOSED_INERT`

本文件细化主规格第 5、7、8、9 节，关闭 canonical package root、source acquisition、artifact publication、authn/authz、tenant isolation 和 durability 的 wire-level 空白。

## 1. 一个 layered atomic contract root

Server-owned canonical source 只有一个 root：

```text
pullwise-server/contracts/reviewer-refactor/v2/
  manifest.json
  registry.json
  generator.lock.json
  shared/
    schemas/*.schema.json
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

`manifest.json` 是唯一 package authority，闭合全部目录、schema `$ref` DAG、registry、fixtures、projection 和 generator input。`private/public` 不拥有独立 version、root digest 或可单独发布的 manifest；允许有派生 index，但其 digest必须被 root manifest 收录且不能成为 consumer pin。任一 required family 缺失、引用越 root、cycle、unknown registry value 或 fixture 未运行时，整个 root 不可发布。

逻辑 package identity 固定为 `pullwise-reviewer-contract-v2`。Server generator 从同一 root bytes 产生 Python 与 npm thin wrappers：

- 两个 wrapper 都携带 path/size/SHA-256 完全一致的 canonical `bundle/` tree、root manifest bytes、logical version 和 root digest；
- Python/npm 自有 loader/type files 是派生产物，不是 schema authority；其 manifest另列并 byte-compare regenerate；
- Worker exact-pin Python distribution version + logical root digest，只允许 private/shared loader exports；
- Web/Admin exact-pin npm version + 同一 logical root digest，只允许 public loader/type exports；private import 在 lint/build FAIL；
- browser/server public bundles 的 reference graph 必须证明 private schemas、private fixture和字段名均未进入可交付资产；build cache 中存在 unified package不能变成 browser exposure；
- Server canonical tree、Python wrapper 和 npm wrapper 的 root bytes 任一不一致时 generation transaction 不得 COMMIT。

这保留 D28 的 single logical bundle/generated wrappers 和 D29 的 layered atomic-root 不变量，同时撤销 D29 对旧 Agent foundation families 的具体闭包。

## 2. Private wire 的完整 schema set

root 的 private family 至少包含：

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

每个 message 使用共同 envelope，但 auth credential、secret header、presigned URL query 和 local absolute path 不进入 canonical body、日志、evidence 或 Agent input。transport layer 把它们放在受控 credential handle 中，并在审计事件中只记录 capability id/digest/expiry/outcome。

## 3. Principals 与 trust registry

`reviewer-trust-registry/v1` 必须在 Stage A 绑定真实 principal ids、issuer/audience、key ids 和责任分离，不能保留 `TBD`/空值：

| Logical principal | 可做 | 不可做 |
|---|---|---|
| `server-control` | 签发 session/claim/cancel/grant、验证 Worker event/candidate、执行 terminal CAS | 代替 release/benchmark signer |
| `worker-runtime/<worker_id>/<session_id>` | 在一个 tenant/run/attempt/lease scope 内 claim/heartbeat/fetch/upload/submit | 自定 tenant、扩大 scope、签 stage advance |
| `source-store` | 对 exact grant 返回 bound immutable source object | 接受任意 path/key、读取其他 tenant |
| `artifact-store` | 对 exact grant 暂存并 commit digest-bound artifact | 决定 terminal truth、生成 public URL |
| `release-orchestrator` | 在 signed stage/write set 内生成 release transaction | 使用生产 Worker credential、改变 decision authority |
| `architecture/governance-owner` | 按 policy 签 decision/stage records | 运行部署或自审实现证据 |
| `benchmark-owner` | 冻结 corpus/oracle/eval policy | promote/deploy |
| `release-operator` | 核验并签 release/promotion | 产生 benchmark raw result |
| `deployment-operator` | 执行 exact runbook/capacity transaction | 修改 policy/threshold/evidence |
| `agent-tool-process` | 使用 attempt-local typed capability channel | 持有任何 control/source-store/artifact-store/provider credential |

control transport 使用双向认证的 workload channel，并在其上使用 Server 签发的短期 session credential。具体 PKI/OIDC backend 由 RR-TRUST-FREEZE-A exact-pin，但语义下限固定：

- audience/issuer/principal/session/worker/build/runtime digest 全绑定；
- session credential 最长 15 分钟，且不晚于 worker session/lease；自动轮换不改变 principal/session identity；
- source/upload grant 最长 5 分钟、不晚于 lease/deadline、single-use、method/object/tenant/run/attempt/size/digest bound；
- token/key 从 Worker private credential store以 handle 传给 transport，不进入 env、argv、filesystem mount、SDK event或 exception text；
- revoke worker session 会立即使其所有未消费 claim/grant 失效；cutover 前撤销全部 v1 audiences/scopes；
- trust registry 冻结算法、public-key encoding、key-id derivation、domain separator、rotation overlap、revocation propagation SLA、clock skew和 emergency revoke；
- signing private keys由受控 keystore/HSM 或等价不可导出设施持有，operator identity 与 workload identity分开；实际 principal/key binding 缺失时 `SPEC-READY-07/11` 不能 PASS。

## 4. Tenant 与对象边界

tenant identity 只由 Server 从已认证 job/repository ownership 推导，进入 private trusted envelope和数据库 composite constraints；Agent payload、source metadata、HTTP header自由文本或 Worker配置都不能创建/覆盖 tenant。

所有 claim、source grant、artifact grant、event、candidate、receipt 和 database/storage key至少绑定：

```text
tenant_id + job_id + run_id + attempt_id + lease_epoch + contract_digest
```

source 另绑定 repository lineage + commit/tree/content manifest；artifact 另绑定 artifact id/kind/size/content digest/storage generation/redaction class。Server 在每次 lookup/CAS 中同时使用 tenant key，禁止先按 globally guessable id读取再事后过滤。cross-tenant id、capability replay、same id/different tenant、same digest/different redaction class 都有 invalid fixtures且无 side effect。

metrics/logs/traces 不使用 tenant id、repo id、path、finding id 或 token 作高基数 label。需要关联时只在 access-controlled audit record 保存 keyed pseudonymous handle，并受 retention policy约束。

## 5. Source acquisition protocol

唯一正常流：

1. Server 在 `review-task-claim/v2` 中提供 immutable source descriptor/digest、transport kind、上限和 opaque source object id，不提供 host path/secret。
2. Worker 校验 claim/session/lease/deadline/runtime，然后发送 `review-source-grant-request/v2`，绑定 expected descriptor/digest/bytes。
3. Server 在同 tenant/run/attempt 下签发 one-use `review-source-grant/v2`；transport secret 通过 credential handle交付，canonical grant只保存 capability id/secret digest/purpose/expiry。
4. Worker 的 source-fetch service在 model sandbox外、network allowlist内下载到 private ingest staging；只允许 `git-bundle-v2` 或 `content-archive-v2` closed transport。redirect、代理注入、协议降级、未知 content type或超 cap均拒绝。
5. 下载后先验证传输 object size/digest/signature，再安全展开：拒绝 absolute/`..`/separator ambiguity、case/prefix collision、symlink/reparse escape、device、hardlink、zip bomb和 declared/actual size drift。
6. 对 Git bundle验证 repository lineage、commit/tree/object closure；对 content archive验证 Server-owned per-entry manifest。随后产生 immutable source snapshot/inventory root。
7. Worker发送 `review-source-acquired/v2`，包含 grant/object/source/inventory digests和 closed status；无 exact ACK 不启动 main turn。credential随后销毁，重试必须申请新 grant但引用同 expected source identity。

生产不允许 `git clone <model-supplied-url>`、复用 host developer credential、让 Agent访问 remote、用 mutable branch/tag作为 source identity或从任意本地目录取 source。

## 6. Artifact upload 与 durability protocol

artifact bytes在 Worker private outbox先 freeze，之后任何 retry都使用同一 bytes/id/digest：

1. Worker发送 `review-artifact-upload-request/v2`，列出 exact descriptor、candidate/outbox generation和期望 storage class。
2. Server验证 tenant/attempt/lease/kind/size/redaction policy，签发 single-artifact one-use grant；一个 grant不能写多个 object或改变 content type。
3. Worker流式上传并本地/远端同时计算 digest；store仅写 uncommitted generation，partial object不可被 reader列出。
4. Worker发送 `review-artifact-upload-complete/v2`；Server/store重新验证实际 size/digest/encryption metadata/tenant key，并以幂等 compare-and-commit把 generation变为 immutable。
5. Server返回 committed `review-artifact-descriptor/v2`。同 capability + 同 digest replay返回原 descriptor；同 capability + 不同 bytes为 conflict并吊销。
6. terminal candidate只能引用已 committed artifact set root。terminal CAS在同事务验证 descriptor generation/digest与tenant；uncommitted/missing/quarantined artifact使 candidate拒绝且无 public projection。
7. public download URL只由 Server projection按 policy即时签发，短期、GET-only、artifact/tenant/audience bound；Worker/Agent不能产生。private/debug artifacts永不获得 public URL。

“upload HTTP 2xx”不等于 durable commit。commit ACK 只有在 object data、metadata和索引满足选定 storage durability class且可读回 digest后产生；storage backend/consistency/durability/encryption policy必须由 RR-TRUTH/CUT freeze并有 ACK-loss、partial upload、read-after-write、duplicate、corruption、quota和delete-race fixtures。

## 7. Trusted/untrusted field ownership

| 来源 | 可成为 trusted fact | 处理 |
|---|---|---|
| Server DB/CAS | tenant、job/run、lease/cancel/version、deadline/budget、current contract | private envelope注入 |
| Worker measured runtime | executable/package/Skill/tool/sandbox/source/receipt/usage/artifact digests | validator注入并留直接证据 |
| Source store | signed object/source digest与immutable manifest | acquisition verifier确认 |
| Agent final payload | finding语义、受约束 location components、limitations/coverage claims | 全部不可信；schema + source + receipt验证 |
| target `AGENTS.md` | repo-local审查偏好和约束 | untrusted policy input；不能扩大权限，见 Skill配套文件 |
| free-text message | 人类诊断 | 不驱动 auth、retry、terminal、billing、projection或gate |

任何 schema让 Agent填写 trusted binding、让 Worker自定 Server authority或让 public consumer接收private field都使 `SPEC-READY-06/07` FAIL。

## 8. Required security fixtures

Stage A/B/C至少覆盖：

- wrong issuer/audience/principal/session/build/runtime、expired/not-yet-valid/revoked/rotated key；
- cross-tenant claim/source/artifact/event/candidate/receipt、id probing和storage key collision；
- grant replay、method/object/size/digest改变、redirect、proxy、DNS/IP target改变、credential出现在env/argv/log；
- archive traversal/collision/symlink/hardlink/device/bomb、Git incomplete object/lineage mismatch；
- partial/corrupt upload、ACK loss、same id different digest、commit-before-durable、quarantine/delete race；
- private field进入public DTO/browser bundle/debug URL、source/secret进入logs/metrics/traces；
- Agent/target instruction请求network/token/host path/permission expansion，且tool surface稳定拒绝；
- revoked v1 Worker在cutover后heartbeat/claim/fetch/upload/submit全部无side effect。

所有拒绝用 closed registry code；未知 auth/storage错误为 `INDETERMINATE`，不得重试成更宽权限路径。
