"""PyramidSeek 批处理包。

分层（单向向下依赖）：
    datasource/  数据源抽象与实现（AkShare 主 + 官方源兜底）
    pipeline/    采集 → 清洗标准化 → 落库
    engine/      指标 → 子分 → 评分 → 行业分位
    build/       产物生成与校验
    batch.py     总入口（一条命令跑完）
"""

__all__ = ["config"]
