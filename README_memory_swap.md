# LightLLM Memory Swap Variant

## 1. 这是什么

这份目录是基于 `LightLLM` 的一个实验性“显存替换版本”副本，当前已经接入了第一版的 **memory-aware scheduler 骨架**。

当前版本的目标不是立即完成真正的 active KV swap，而是先把后续做显存调度和显存替换所需的结构搭起来。

目前已经具备：

- 新的显存调度参数入口
- `memory_scheduler` 模块目录
- 第一版 `fair_pause` 调度器
- 在 backend 中接入 victim selection
- 在请求对象上补充等待债务和恢复保护状态

当前还**没有**完成：

- active request KV 真正换出到 CPU
- swap-in / swap-out 元数据管理
- block-wise partial swap
- active KV 的 CPU 持久化存储管理器

所以这份代码当前更准确地说是：

> 显存替换版本的第一阶段骨架，已实现“公平 pause 调度”，未实现真正的 active KV swap。

## 2. 已做改动

### 2.1 新增启动参数

在下面两个文件中新增了显存调度相关参数：

- [lightllm/server/api_cli.py](./lightllm/server/api_cli.py)
- [lightllm/server/core/objs/start_args_type.py](./lightllm/server/core/objs/start_args_type.py)

新增参数如下：

- `--mem_scheduler`
  - 可选值：`none`、`fair_pause`、`fair_swap`
- `--enable_active_kv_swap`
  - 是否启用 active KV swap 框架开关
- `--swap_block_size`
  - 未来 active swap 的 block 粒度，单位 token
- `--swap_threshold_tokens`
  - 未来判断请求是否适合走 swap 后端的最小 KV 长度
- `--victim_policy`
  - victim 选择策略名，当前作为预留参数
- `--victim_min_ratio_to_need`
  - victim 释放量必须至少达到当前请求需求的多少倍，默认 `5.0`

### 2.2 新增 `memory_scheduler` 目录

新增目录：

- [lightllm/server/router/memory_scheduler](./lightllm/server/router/memory_scheduler)

当前文件包括：

- `base.py`
  - 显存调度器基类接口
- `fair_pause.py`
  - 第一版公平 pause 调度器
- `fair_swap.py`
  - 第二版 active swap 调度器占位实现
- `__init__.py`
  - 调度器工厂函数 `build_memory_scheduler(args)`

### 2.3 在 backend 中接入调度器

修改文件：

- [lightllm/server/router/model_infer/mode_backend/base_backend.py](./lightllm/server/router/model_infer/mode_backend/base_backend.py)

已做的行为变化：

- 初始化 `ModeBackend` 时，会构造 `self.memory_scheduler`
- 在 `_get_classed_reqs()` 中，当当前请求所需 token 超过可分配 token 时：
  - 不再只让“当前请求自己倒霉”
  - 而是先调用 `memory_scheduler.select_victims(...)`
  - 如果挑到了 victim，就优先让 victim 进入 `wait_pause`
  - 然后再尝试让当前请求进入 prefill/decode 队列
- 在恢复 paused 请求前，会先经过 `select_resume_reqs(...)`

### 2.4 在请求对象上补充调度状态

修改文件：

- [lightllm/server/router/model_infer/infer_batch.py](./lightllm/server/router/model_infer/infer_batch.py)

新增状态字段：

- `pause_count`
- `enqueue_ts`
- `last_pause_ts`
- `last_resume_ts`
- `total_wait_time`
- `last_wait_refresh_ts`
- `last_start_ts`
- `finish_ts`
- `last_execution_time`
- `output_tokens_at_resume`

这些字段的作用是：

- `pause_count`
  - 记录请求被牺牲了多少次
- `enqueue_ts`
  - 预留给后续更精细的等待时间分析
- `last_pause_ts`
  - 记录最近一次 pause 的时间
- `last_resume_ts`
  - 记录最近一次恢复的时间
- `total_wait_time`
  - 当前定义下的等待时间
- `last_wait_refresh_ts`
  - 当前这一轮等待的起点
- `last_start_ts`
  - 最近一次真正开始执行的时间
- `finish_ts`
  - 请求完成时刻
- `last_execution_time`
  - 定义为 `finish_ts - last_start_ts`
- `output_tokens_at_resume`
  - 用来判断请求恢复后是否已经取得足够进度，避免刚恢复又立刻被再次暂停

另外，在：

- `pause_reqs()`
- `recover_paused_reqs()`

中已经接入这些状态的更新逻辑。

当前版本中，时间语义定义为：

- **等待时间**
  - 如果请求当前**还没有执行/仍在等待**，则为：`当前时刻 - 入队时刻`
  - 如果请求当前**已经开始执行**，则为：`上次开始执行时刻 - 入队时刻`
- **执行时间**
  - `finish_ts - last_start_ts`
  - 表示最后一次恢复/开始执行到完成之间的执行时长

## 3. 当前逻辑和原版 LightLLM 的区别

原版 LightLLM 在显存不足时，更接近：

```text
哪个请求在当前遍历里先发现 token 不够
哪个请求就被 wait_pause
```

当前版本改成了：

```text
如果当前请求 token 不够
先从 running req 中找一个更适合让出显存的 victim
让 victim pause
再尝试推进当前请求
```

所以这已经是一个“显存调度策略补丁”，只是还不是“显存换出换入补丁”。

## 4. 当前 `fair_pause` 的策略

当前的 `FairPauseMemoryScheduler` 已经不是最初那种“只看谁的 KV 更大”的简单启发式了。

它现在综合考虑四类因素：

- 释放收益：这个 victim 让出的 KV 是否对当前缺口有足够帮助
- 重算代价：pause 这个 victim 后，将来恢复重算会有多贵
- 等待债务：这个 victim 是否已经被拖了太久
- 反饥饿保护：它是否最近刚恢复、刚被 pause 过，或者恢复后还没取得足够进度

可以把它概括成：

```text
victim_score =
    release_usefulness
  - recompute_penalty
  - starvation_penalty
```

### 4.1 释放收益 `release_usefulness`

当前实现中，victim 让出的 KV 不是简单追求“刚好贴近缺口”，而是强调：

- 太小：对当前请求帮助不够，不值得抢占
- 只略高于当前显存缺口：不够理想，因为这类 victim 往往本身也偏弱
- **必须大于当前请求本身所需的 token 空间的固定倍数**，默认是 `5 倍`
- 明显大于当前请求所需空间：收益更好，因为 pause 代价更容易被摊薄
- 如果不存在这样的单个 victim：当前版本直接放弃替代，不再拼多个 victim

也就是说，它已经不是“最大请求优先”，而更接近：

```text
强势任务让位优先
```

### 4.2 重算代价 `recompute_penalty`

当前实现中，重算代价近似依赖于：

- 当前逻辑完成度
- 当前 `cur_kv_len`

这里没有再沿用最初“只看显存大小”的方式，而是近似考虑：

- `cur_output_len / max_new_tokens`
- `cur_kv_len / (input_len + max_new_tokens)`

所以：

- 越接近完成的请求，越不适合作为 victim
- 当前 KV 越大，越不适合作为 victim

### 4.3 等待债务 `wait_debt`

当前实现引入了等待债务：

```text
wait_debt = accumulated_wait / estimated_standalone_latency
```

这表示：

- 不是只看一个请求绝对等了多久
- 而是看相对于它本来应有的完成时间，它已经被拖得多惨

另外，当前版本会维护一个全局：

```text
avg_wait_ratio
```

它来自：

- 每个请求完成后
- 记录该请求的：
  - `累计等待时间 / 预计独立完成时间`
- 再按 **实际执行时间** 作为权重并入系统平均等待比

因此当前策略额外要求：

> 如果一个候选 victim 的等待债务已经高于当前平均等待比，那么它不应再被替换。

### 4.4 反饥饿保护 `starvation_penalty`

当前实现里，以下情况都会提高 victim 惩罚：

- `pause_count` 很高
- `wait_debt` 很高
- 最近刚恢复，处于冷却期
- 恢复后还没取得足够输出进度

这能缓解两种问题：

- 长任务反复被牺牲
- 一个请求刚恢复又立刻被再次 pause

### 4.5 victim 选择流程

当前 `select_victims()` 的流程是：

1. 先算当前显存缺口 `gap`
2. 过滤掉：
   - 已经 paused
   - 已经 `wait_pause`
   - 已 finished
   - 刚恢复且还在冷却期的请求
3. 优先尝试寻找：
   - 单个就能释放出 **大于当前请求所需 token 固定倍数** 的空间
   - 且净收益更合理的 victim
   - 且该 victim 的等待债务不高于当前平均等待比
4. 如果找不到这样的单个 victim：
   - 当前版本直接放弃替代，不再组合多个弱 victim

所以当前策略已经不是：

```text
谁的 KV 最大就暂停谁
```

也不是：

```text
哪个请求先撞上资源不足，哪个请求自己暂停
```

而是：

```text
优先选择释放收益高、重算代价低、等待债务低、且不容易造成饥饿的 victim
```

## 5. 当前 `fair_swap` 的状态

`fair_swap.py` 现在只是占位实现。

它目前：

- 只是复用了 `fair_pause` 的 victim 选择行为
- 还没有真正实现：
  - KV block 换出
  - CPU block 映射
  - swap-in 决策
  - recompute vs swap-in 决策

所以即使你传：

```bash
--mem_scheduler fair_swap --enable_active_kv_swap
```

现在也不会真的做 active KV CPU swap，只是为下一阶段留好了模式入口。

## 6. 如何使用当前版本

### 6.1 默认行为

如果不指定参数：

```bash
python -m lightllm.server.api_server --model_dir /path/to/model
```

则行为与原版基本一致，因为：

```text
--mem_scheduler 默认为 none
```

### 6.2 启用第一版公平 pause 调度

可以这样启动：

```bash
python -m lightllm.server.api_server \
  --model_dir /path/to/model \
  --mem_scheduler fair_pause
```

如果你想连第二阶段参数一起带上，也可以：

```bash
python -m lightllm.server.api_server \
  --model_dir /path/to/model \
  --mem_scheduler fair_pause \
  --swap_block_size 128 \
  --swap_threshold_tokens 4096 \
  --victim_policy kv_idle_fair
```

注意：

- 当前这些 swap 相关参数大多数还是“结构预留”
- 真正生效的是 `fair_pause` 的 victim selection

## 7. 建议的下一步开发顺序

建议严格按下面顺序做，不要跳步骤。

### 第一步：验证 `fair_pause`

建议先观察：

- 哪些请求被 pause
- pause 次数是否比原版更集中在长上下文任务
- 是否能避免“当前请求一撞上资源不足就自己 pause”

### 第二步：增强 victim score

下一版建议补入：

- 用户维度服务量
- 最近恢复保护
- 请求等待时间
- 交互型请求保护
- idle / 非活跃请求识别

### 第三步：引入 `ActiveKvSwapManager`

建议新增模块：

```text
lightllm/server/router/memory_scheduler/active_swap_manager.py
```

职责包括：

- active KV block 元数据管理
- GPU block -> CPU block 映射
- swap-out
- swap-in
- recompute vs swap-in 决策

### 第四步：接入真正的 `fair_swap`

在 `fair_swap.py` 中实现：

- victim 选择
- 只换出部分 block
- 被换出请求恢复时优先 swap-in 最小必需集

## 8. 这份版本当前最适合做什么

当前版本最适合：

- 作为显存调度改造起点
- 验证 memory-aware pause 的行为
- 验证“释放收益 + 重算代价 + 等待债务 + 反饥饿保护”这套 victim 选择思路
- 继续开发 active KV swap
- 把 VTC 风格的公平调度思想迁移到 LightLLM

当前版本不适合直接宣称：

- 已支持 active KV CPU swap
- 已完成 partial block swap
- 已实现完整显存替换系统

## 9. 总结

这份版本已经完成了“显存替换版本”的第一步：

> 先把显存调度逻辑抽成可替换的 `memory_scheduler`，并让 backend 在显存不够时优先选 victim，而不是简单地暂停当前请求。

后续真正的工作重点将是：

- `fair_swap` 的 active KV swap 实现
- swap block 管理
- 恢复路径
- 公平性指标和 admission control 的继续接入
