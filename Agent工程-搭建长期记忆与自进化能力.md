# Agent工程-搭建长期记忆与自进化能力

# 背景

参考HermesAgent的长期记忆结构，用基础文件系统、TriggerFlow、Agent Direct Request 和 Workspace，搭建可解释的自进化闭环。

**问题**：任务结束后，哪些经历值得留下？留下之后怎样被修正、被以往？下一次任务开始时，怎样把该带的带回来？

烫知识：

*   模型本身没有记忆；
    
*   模型的多轮对话，依赖我们提供上下文；
    
*   最简单的上下文，就是把所有的会话记录都给ta；
    
*   模型有上下文窗口限制；
    

![image](https://alidocs.oss-cn-zhangjiakou.aliyuncs.com/a/de50mnO8xs021MNb/04238f3ae93b4d92904281cfdd15371e0397.png)

# 记忆系统

记忆系统面对的是不断产生的经历，至少要应对四个问题：

| 动作 | 要应对的问题 | 常见工程实现 |
| --- | --- | --- |
| ingestion 写入 | 什么经历进入记忆系统 | 事件流、工具结果、用户偏好、决策记录 |
| revision 修正 | 旧记忆怎样被改写 | 摘要重写、同义合并、重新关联证据 |
| forgetting 遗忘 | 什么不再进入候选范围 | 过期、降权、归档、删除 |
| retrieval 召回 | 新任务取回哪些记忆 | 文本检索、规则过滤、上下文打包 |

## RAG

**检索增强生成**（Retrieval-Augmented Generation）

RAG覆盖“召回”，而整个记忆系统是四个动作的闭环

![image.png](https://alidocs.oss-cn-zhangjiakou.aliyuncs.com/res/Mp7ld75PeLPv8OBQ/img/ee5e1636-ed03-456f-aaa1-2c936c7c207f.png)

RAG的核心思路是：**先检索，再生成**。大模型本身的知识有截止日期，也无法掌握企业内部或私有的数据。RAG通过引入一个外部知识库（比如向量数据库），在模型回答问题前，先从库里检索出最相关的信息片段，然后把这段信息连同用户的问题一起喂给大模型，让它基于这些“参考资料”来生成答案。

典型的RAG流程包括：文档切分 → 向量化存储 → 用户提问向量化 → 语义相似度检索 → 拼接提示词 → 大模型生成最终回复

![image](https://alidocs.oss-cn-zhangjiakou.aliyuncs.com/a/de50mnO8xs021MNb/ff3bd0e30ee94e688242d39d5355ed960397.png)

## 四层记忆理论

![image.png](https://alidocs.oss-cn-zhangjiakou.aliyuncs.com/res/Mp7ld75PeLPv8OBQ/img/671e89fc-b1d6-443a-a23d-c443fbd1bfe9.png)

| 层 | 放什么 | 默认进入上下文 | 文件产物 |
| --- | --- | --- | --- |
| Raw Episodic | 原始事件、工具输出、对话 | 否 | `materials/simulated_long_conversation.jsonl` |
| Consolidated Episodic | 会话摘要、候选记忆、过程记忆 | 候选 | `session_memory.json` |
| Semantic / Policy | 稳定规则、偏好、长期事实、能力方法 | 按任务召回 | `long_term_memory.json` / `capability_memory.json` |
| Working Memory | 当前任务必要信息 | 是 | `new_session_context.json` / `process_context.json` |

分层是为了三件事：只把少量稳定信息放进上下文；需要证据时能回读原始记录；记忆变旧或冲突时能定位并修正。raw 层只追加、不修饰；semantic 层少而稳定，每条都要有升格理由；working memory 是临时产物，用完可以重建。

Hermes Agent 的实际结构可以对进这张表：`MEMORY.md` / `USER.md` 对应稳定规则与用户偏好（semantic 层），SQLite + `session_search` 对应完整会话历史（raw 层），会话结束抽取和后台 review 对应整理动作，Skills 自改进对应能力/方法记忆。这套实现不照搬它的文件格式，只沿用分工：稳定层小而可信，历史层完整可查，后台整理负责把经历压成可复用经验。

### 提示词构建

![image.png](https://alidocs.oss-cn-zhangjiakou.aliyuncs.com/res/Mp7ld75PeLPv8OBQ/img/237a048e-4d21-4d38-b0b3-28a6da22f8e4.png)

![image.png](https://alidocs.oss-cn-zhangjiakou.aliyuncs.com/res/Mp7ld75PeLPv8OBQ/img/9fa5ce93-0848-48c2-9b4c-c3ef478ff4fa.png)

![image.png](https://alidocs.oss-cn-zhangjiakou.aliyuncs.com/res/Mp7ld75PeLPv8OBQ/img/c1d9bebe-dc4a-401a-8aae-49276a5843f0.png)

## Map-Reduce

大量历史的处理：分配抽取、再合并升格

| 阶段 | 模型看到什么 | 输出什么 | 为什么这样切 |
| --- | --- | --- | --- |
| Map | 一个会话或一个时间片 | 会话摘要、候选记忆、过程记忆 | 上下文短，证据完整，失败可重跑 |
| Reduce | 多批候选记忆 | 合并后的长期记忆 | 同义合并、跨会话重复、证据核对 |
| Checkpoint | 已处理批次和 session | 断点文件 | 中断后从未完成批次继续 |

## 升格：三条通道，宁紧勿松

候选记忆还不是长期记忆。长期记忆会在未来任务里优先进入上下文，进去一条错的，以后每个任务都要为它买单。升格通道只有三条，满足任意一条才升：

| 升格通道 | 适合什么内容 | 例子 |
| --- | --- | --- |
| 用户显式长期要求 | 用户明确说"以后都这样" | "以后输出行程要附检查结论" |
| 跨会话重复 | 多次独立出现的偏好或规则 | 多次亲子游都强调不要太累 |
| 工具失败证据 | 失败后形成的稳定修正规则 | 远距离大项不要塞进半天 |

升格时，候选的四类 `kind` 各归各位：

```text
user_preference → user_preference（用户偏好）
fact            → stable_fact（稳定事实）
lesson          → project_rule（项目规则）
skill           → capability_method（能力/方法，下一小节单独展开）

```

还有一个字段要注意：`recall_keywords` 由模型根据语义生成，是这条记忆自带的检索词。**召回不会用代码写死的词表**——固定关键词列表换个场景就失效，检索词应该从记忆本身和当前任务里长出来。

## 能力/方法记忆：把"会做的事"沉淀成方法卡

长期记忆不只有"用户喜欢什么"和"项目规则是什么"。系统长期运行还会积累一种更值钱的内容：**能力/方法记忆**——它可以视作长期记忆的一类（上一步的 `capability_method`），接近 CoALA 里的 procedural memory。它记录的是可复用做法，不是某一次任务的结论：

| 字段 | 含义 |
| --- | --- |
| `applies_when` | 什么场景下适用 |
| `method` | 具体步骤 |
| `validation` | 怎么确认做对了 |
| `failure_signals` | 什么现象说明这个方法失效 |
| `evidence` | 来自哪些事件 |

## 两种触发：什么时候激活压缩管线

![image.png](https://alidocs.oss-cn-zhangjiakou.aliyuncs.com/res/Mp7ld75PeLPv8OBQ/img/2dd820b1-7b2f-4f7a-af13-5cd355f4c6d6.png)

**触发一：每轮对话完成后检测。** 一轮对话结束时，输入、模型输出、工具结果都齐了，做一次轻量检测，满足条件就投递整理任务。检测看的都是可计算信号，不需要模型：

| 信号 | 怎么检测 |
| --- | --- |
| 新事件数 | 当前 session 的事件数是否超过上次整理的位置 |
| 上下文压力 | 会话记录的 token 估算是否接近窗口阈值 |
| 过程变化 | 是否出现 failed tool、人工修正、用户明确说"以后都这样" |
| 写入锁 | 这个 session 是否已经有整理任务在跑（避免重复触发） |

**触发二：定时触发。** 低频会话、离线导入的历史不经过对话钩子，靠系统定时任务兜底。

:::
安全提醒：后台自动写记忆要留三根钉子——来源标记（这条记忆从哪来）、证据链接（evidence 能追回原事件）、可回滚（写错了能定位撤销）。没有这三样，"自动整理"会变成"自动污染"（测量数据见资料来源里的 HEARTBEAT 论文）。
:::

最小实现先用文件记忆库完成召回；换成 Workspace 或向量库时，变的是检索后端，不变的是"按当前任务打包上下文"这件事。

![image.png](https://alidocs.oss-cn-zhangjiakou.aliyuncs.com/res/Mp7ld75PeLPv8OBQ/img/ae72c22a-9e34-4df3-8d7e-921e6bc86c67.png)

## 渐进式披露

根据有效性判断，逐步读取出记忆

![image](https://alidocs.oss-cn-zhangjiakou.aliyuncs.com/a/de50mnO8xs021MNb/4bf7c4ad359a44f3a72adf20359f58960397.png)

# 应用总览

![image](https://alidocs.oss-cn-zhangjiakou.aliyuncs.com/a/de50mnO8xs021MNb/840a17e551de4b4bb051df01a7a06dc20397.png)