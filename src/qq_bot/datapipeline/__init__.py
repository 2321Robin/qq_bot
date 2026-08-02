"""数据管道（阶段 3）：契约化、可追溯、增量、把关、可分发、可索引的离线数据刷新。

模块边界（S3 需求组，不得揉成单个大函数）：
- schemas / validation：Pydantic 契约与 quarantine
- manifest：逐文件哈希、dataset_hash 与一致性校验
- fetch：三级增量抓取与变更集
- quality：八项质量门禁
- diff：差异报告
- publish：staging 与原子发布编排
- index：n-gram 倒排索引构建与只读查询

本包与运行时（services/）的唯一共享契约是详情 JSON 的 schema 与
manifest 的 dataset_hash；管道不在运行进程内操作数据缓存。
"""

DATA_PIPELINE_SCHEMA_VERSION = 1
