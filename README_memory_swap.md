# LightLLM Memory Swap Variant

## 1. 这是什么

这份目录是基于 `LightLLM` 的一个实验性“显存替换版本”副本，当前已经接入了第一版的 **memory-aware scheduler 骨架**。

当前版本的目标不是立即完成真正的 active KV swap，而是先把后续做显存调度和显存替换所需的结构搭起来。

目前已经具备：

- 新的显存调度参数入口
- `memory_scheduler` 模块目录
- 第一版 `fair_pause` 调度器
- 在 backend 中接入 victim selection

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

当前的 `FairPauseMemoryScheduler` 是一个很简单的第一版启发式实现。

victim score 规则：

```text
score = cur_kv_len + 0.25 * cur_output_len
```

含义：

- `cur_kv_len` 越大，越适合作为 victim
- `cur_output_len` 越大，越偏向长尾任务，也更容易成为 victim

当前只筛选：

- `cur_kv_len > 0`
- 还没有 `paused`
- 还没有 `wait_pause`
- 还没有 finished

这只是第一版占位策略，后面应继续扩展：

- 用户公平性
- 最近是否刚被 pause/recover
- 请求等待时间
- 交互优先级
- idle / tool-wait 等状态

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
