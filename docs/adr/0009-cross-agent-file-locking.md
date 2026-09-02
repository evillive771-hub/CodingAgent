# ADR-0009：跨 Agent 逐文件写锁

## 状态

已接受。

## 背景

同一 `ToolExecutor` 的 mutation lock 不能协调其他 Executor 或宿主进程。两个 Agent 如果共享工作区，
可能同时读取旧内容并写回不同结果。仅靠提示词约束不能形成强制互斥。

`learn-claude-code` s13 对共享任务存储组合使用进程内锁和 OS 文件锁，并通过 task-bound worktree
隔离并行代码编辑。本项目当前仍以单 Agent 为主，但独立会话和未来 Agent Team 需要一个兼容 Windows
的宿主级文件操作防线。

## 决策

1. 文件锁属于 `ToolExecutor` 宿主层，不暴露为模型工具。
2. 每个工作区相对路径映射到 `.aicoding-agent/locks/` 下的稳定 SHA-256 锁文件。
3. 同时使用进程内异步锁和 OS advisory lock；Windows 使用 `msvcrt.locking`，POSIX 使用 `flock`。
4. 只对通过 PolicyEngine 且声明单一 `path` 的 `WRITE` 工具加锁；锁包住完整读-校验-写过程。
5. 锁有有限等待时间，超时失败关闭，并通过临时元数据报告当前 `run_id` 持有者。
6. 获得和释放写入追加式运行事件；取消和异常必须可靠释放，崩溃释放由 OS 保证。
7. 内容先写同目录临时文件并同步，再原子发布，避免其他进程看到半写文件。
8. 多文件并行任务仍优先使用隔离 worktree；文件锁不替代任务所有权、差异合并和测试资源隔离。

## 结果

共享工作区中的同文件写操作会串行，不同文件仍可并发。精确补丁在获得锁后重新读取文件，陈旧修改
会失败而不是覆盖。锁文件本身保留以避免 Unix 删除锁文件导致 inode 竞态；持有者元数据在正常释放
时删除，且整个 `.aicoding-agent/` 继续被 Git 忽略并受工具策略保护。
