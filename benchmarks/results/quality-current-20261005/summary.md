# Memory retrieve quality bench

_Generated 2026-10-05 15:03:38 UTC_


## Setup
- Corpus turns published: **40**
- Ingestion wait time:    **296.0s**
- Palace state at query time:  fragments=**42**, entities=33, triples_total=30, triples_active=26

## Overall
- Queries: **48**, fully correct: **16/48** = **33.3%**
- Evidence-group recall: **52.1%**, omissions: **35**, clean abstention: **0/5**
- Latency (per query, end-to-end MCP round-trip): p50 **16.5ms**, p95 **17.6ms**, mean **16.7ms**, min/max 15.6/22.8ms

- Query execution: errors **0**, degraded **0**, valid quality run: **True**

## Per-category breakdown

| Category | n | correct | rate | evidence recall | omissions | abstain | p50 ms | p95 ms |
|----------|--:|--------:|-----:|----------------:|----------:|--------:|-------:|-------:|
| abstention | 5 | 0/5 | 0.0% | 0.0% | 0 | 0/5 | 16.0 | 16.1 |
| canonical_entity | 7 | 2/7 | 28.6% | 57.1% | 6 | 0/0 | 17.1 | 22.8 |
| emotion | 3 | 2/3 | 66.7% | 66.7% | 1 | 0/0 | 16.9 | 17.3 |
| event | 3 | 2/3 | 66.7% | 60.0% | 2 | 0/0 | 17.0 | 17.6 |
| future_plans | 3 | 0/3 | 0.0% | 16.7% | 5 | 0/0 | 16.6 | 16.8 |
| kinship_alias | 8 | 3/8 | 37.5% | 68.8% | 5 | 0/0 | 16.9 | 17.1 |
| preference | 4 | 3/4 | 75.0% | 60.0% | 2 | 0/0 | 16.1 | 16.4 |
| pronoun | 3 | 1/3 | 33.3% | 66.7% | 2 | 0/0 | 16.6 | 17.0 |
| time | 4 | 1/4 | 25.0% | 25.0% | 3 | 0/0 | 16.1 | 16.6 |
| topic | 8 | 2/8 | 25.0% | 35.7% | 9 | 0/0 | 16.0 | 16.8 |

## Per-query detail

| id | category | query | ms | evidence | omitted | returned | abstain | matched |
|----|----------|-------|---:|---------:|--------:|---------:|:-------:|---------|
| canonical-001 | canonical_entity | 张丽最近怎样 | 22.78 | 1/2 | 1 | 14 | · | kg:mother |
| canonical-002 | canonical_entity | 王芳呢 | 20.6 | 0/2 | 2 | 15 | · |  |
| canonical-003 | canonical_entity | 铁锤是什么品种 | 17.64 | 2/2 | 0 | 12 | · | kg:pet:铁锤, vec:边境牧羊犬 |
| canonical-004 | canonical_entity | OP-3091 项目进展 | 17.06 | 1/2 | 1 | 14 | · | vec:OP-3091 |
| canonical-005 | canonical_entity | 李总找我谈话 | 16.97 | 1/2 | 1 | 14 | · | vec:李总 |
| canonical-006 | canonical_entity | 北京出差的事 | 16.96 | 2/2 | 0 | 18 | · | kg:place:北京, vec:北京 |
| canonical-007 | canonical_entity | 日本旅行计划 | 16.05 | 1/2 | 1 | 13 | · | vec:日本 |
| kinship-001 | kinship_alias | 我妈最近怎样 | 16.98 | 1/2 | 1 | 14 | · | kg:mother |
| kinship-002 | kinship_alias | 妈妈睡得怎样 | 16.1 | 1/2 | 1 | 14 | · | kg:mother |
| kinship-003 | kinship_alias | 我老婆和我吵架 | 17.1 | 1/2 | 1 | 19 | · | kg:wife |
| kinship-004 | kinship_alias | 我和老婆和好了吗 | 16.89 | 2/2 | 0 | 14 | · | kg:wife, vec:和好 |
| kinship-005 | kinship_alias | 我家狗多大 | 16.94 | 2/2 | 0 | 19 | · | kg:pet:铁锤, vec:铁锤 |
| kinship-006 | kinship_alias | 我家小狗最近怎么样 | 16.72 | 2/2 | 0 | 19 | · | kg:pet:铁锤, vec:铁锤 |
| kinship-007 | kinship_alias | 我们公司的项目 | 16.87 | 1/2 | 1 | 15 | · | vec:OP-3091 |
| kinship-008 | kinship_alias | 老板最近找我 | 16.47 | 1/2 | 1 | 15 | · | vec:李总 |
| pronoun-001 | pronoun | 她最近睡得好吗 | 16.96 | 1/2 | 1 | 14 | · | kg:wife |
| pronoun-002 | pronoun | 它身体怎么样 | 16.57 | 1/2 | 1 | 19 | · | kg:pet:铁锤 |
| pronoun-003 | pronoun | 我们和好了 | 15.97 | 2/2 | 0 | 13 | · | kg:wife, vec:和好 |
| time-001 | time | 最近几天我状态怎样 | 16.17 | 0/1 | 1 | 14 | · |  |
| time-002 | time | 上周聊了什么 | 15.94 | 1/1 | 0 | 13 | · | vec:项目 |
| time-004 | time | 我答应了什么事情 | 16.58 | 0/1 | 1 | 13 | · |  |
| time-005 | time | 我今天做了什么 | 16.13 | 0/1 | 1 | 14 | · |  |
| topic-001 | topic | 我最近工作压力大吗 | 16.45 | 0/2 | 2 | 13 | · |  |
| topic-002 | topic | 我有焦虑情绪吗 | 16.8 | 1/1 | 0 | 13 | · | vec:焦虑 |
| topic-003 | topic | 我有什么健康问题 | 15.93 | 0/2 | 2 | 14 | · |  |
| topic-004 | topic | 家里人健康吗 | 16.15 | 0/2 | 2 | 14 | · |  |
| topic-005 | topic | 我和家人的关系 | 15.95 | 1/2 | 1 | 14 | · | kg:wife |
| topic-006 | topic | 我和宠物的事 | 15.89 | 1/2 | 1 | 15 | · | kg:pet |
| topic-007 | topic | 最近开心的事 | 15.57 | 1/1 | 0 | 13 | · | vec:和好 |
| topic-008 | topic | 我最近担心什么 | 16.68 | 1/2 | 1 | 13 | · | vec:焦虑 |
| preference-001 | preference | 我喜欢喝什么 | 16.19 | 1/1 | 0 | 14 | · | vec:茶 |
| preference-002 | preference | 我听什么播客 | 16.42 | 1/1 | 0 | 14 | · | vec:Acquired |
| preference-003 | preference | 我有什么爱好 | 15.85 | 1/1 | 0 | 14 | · | vec:播客 |
| preference-004 | preference | 我喜欢去哪里玩 | 15.99 | 0/2 | 2 | 13 | · |  |
| future-001 | future_plans | 我以后想做什么 | 16.63 | 0/2 | 2 | 13 | · |  |
| future-002 | future_plans | 我答应了妈妈什么 | 16.79 | 0/2 | 2 | 13 | · |  |
| future-003 | future_plans | 我计划去哪里 | 15.85 | 1/2 | 1 | 13 | · | vec:日本 |
| negative-001 | abstention | 我家鸟会说话吗 | 15.95 | 0/0 | 0 | 14 | ✗ |  |
| negative-002 | abstention | 我哥结婚的事 | 15.89 | 0/0 | 0 | 14 | ✗ |  |
| negative-003 | abstention | 我爸去世的事 | 16.01 | 0/0 | 0 | 13 | ✗ |  |
| negative-004 | abstention | 前任的事 | 16.11 | 0/0 | 0 | 14 | ✗ |  |
| negative-005 | abstention | 我儿子的学习 | 15.96 | 0/0 | 0 | 14 | ✗ |  |
| event-001 | event | 铁锤打疫苗 | 16.97 | 2/2 | 0 | 18 | · | kg:pet:铁锤, vec:疫苗 |
| event-002 | event | 我去医院的事 | 16.69 | 1/1 | 0 | 14 | · | vec:医院 |
| event-003 | event | 我跟客户吵架了 | 17.58 | 0/2 | 2 | 13 | · |  |
| emotion-001 | emotion | 我什么时候开心 | 17.27 | 0/1 | 1 | 13 | · |  |
| emotion-002 | emotion | 我什么时候焦虑 | 16.88 | 1/1 | 0 | 13 | · | vec:焦虑 |
| emotion-003 | emotion | 我半夜睡不着 | 16.41 | 1/1 | 0 | 13 | · | vec:焦虑 |

## Misses (composite-correct=false)

- `canonical-001` (canonical_entity): "张丽最近怎样" — kg objects returned: mother, insomnia, self, pet:小白; error=None; degraded=False; reason=None
- `canonical-002` (canonical_entity): "王芳呢" — kg objects returned: self, wife, wife, 拉伸; error=None; degraded=False; reason=None
- `canonical-004` (canonical_entity): "OP-3091 项目进展" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `canonical-005` (canonical_entity): "李总找我谈话" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `canonical-007` (canonical_entity): "日本旅行计划" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `kinship-001` (kinship_alias): "我妈最近怎样" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `kinship-002` (kinship_alias): "妈妈睡得怎样" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `kinship-003` (kinship_alias): "我老婆和我吵架" — kg objects returned: wife, 拉伸, self, pet:小白; error=None; degraded=False; reason=None
- `kinship-007` (kinship_alias): "我们公司的项目" — kg objects returned: pet:铁锤, 公, self, pet:小白; error=None; degraded=False; reason=None
- `kinship-008` (kinship_alias): "老板最近找我" — kg objects returned: self, wife, self, 沙丘2; error=None; degraded=False; reason=None
- `pronoun-001` (pronoun): "她最近睡得好吗" — kg objects returned: self, wife, wife, 拉伸; error=None; degraded=False; reason=None
- `pronoun-002` (pronoun): "它身体怎么样" — kg objects returned: pet:铁锤, 公, pet:铁锤, place:北京; error=None; degraded=False; reason=None
- `time-001` (time): "最近几天我状态怎样" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `time-004` (time): "我答应了什么事情" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `time-005` (time): "我今天做了什么" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `topic-001` (topic): "我最近工作压力大吗" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `topic-003` (topic): "我有什么健康问题" — kg objects returned: self, wife, self, 沙丘2; error=None; degraded=False; reason=None
- `topic-004` (topic): "家里人健康吗" — kg objects returned: self, pet:小白, self, 沙丘2; error=None; degraded=False; reason=None
- `topic-005` (topic): "我和家人的关系" — kg objects returned: self, pet:小白, self, 沙丘2; error=None; degraded=False; reason=None
- `topic-006` (topic): "我和宠物的事" — kg objects returned: self, pet:小白, self, 沙丘2; error=None; degraded=False; reason=None
- `topic-008` (topic): "我最近担心什么" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `preference-004` (preference): "我喜欢去哪里玩" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `future-001` (future_plans): "我以后想做什么" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `future-002` (future_plans): "我答应了妈妈什么" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `future-003` (future_plans): "我计划去哪里" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `negative-001` (abstention): "我家鸟会说话吗" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `negative-002` (abstention): "我哥结婚的事" — kg objects returned: self, pet:小白, self, 沙丘2; error=None; degraded=False; reason=None
- `negative-003` (abstention): "我爸去世的事" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `negative-004` (abstention): "前任的事" — kg objects returned: self, pet:小白, self, 沙丘2; error=None; degraded=False; reason=None
- `negative-005` (abstention): "我儿子的学习" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `event-003` (event): "我跟客户吵架了" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
- `emotion-001` (emotion): "我什么时候开心" — kg objects returned: self, pet:小白, self, wife; error=None; degraded=False; reason=None
