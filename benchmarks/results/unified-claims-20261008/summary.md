# Memory retrieve quality bench

_Generated 2026-10-08 02:56:49 UTC_


## Setup
- Corpus turns published: **40**
- Ingestion wait time:    **325.8s**
- Palace state at query time:  fragments=**60**, entities=28, triples_total=26, triples_active=25

## Overall
- Queries: **48**, fully correct: **20/48** = **41.7%**
- Evidence-group recall: **65.2%**, omissions: **23**, clean abstention: **0/9**
- Latency (per query, end-to-end MCP round-trip): p50 **20.2ms**, p95 **22.4ms**, mean **20.3ms**, min/max 17.3/25.3ms

- Query execution: errors **0**, degraded **0**, valid quality run: **True**

## Per-category breakdown

| Category | n | correct | rate | evidence recall | omissions | abstain | p50 ms | p95 ms |
|----------|--:|--------:|-----:|----------------:|----------:|--------:|-------:|-------:|
| abstention | 5 | 0/5 | 0.0% | 0.0% | 0 | 0/5 | 18.1 | 21.2 |
| canonical_entity | 7 | 2/7 | 28.6% | 66.7% | 4 | 0/1 | 20.6 | 24.1 |
| emotion | 3 | 3/3 | 100.0% | 100.0% | 0 | 0/0 | 19.1 | 20.1 |
| event | 3 | 2/3 | 66.7% | 100.0% | 0 | 0/1 | 19.7 | 21.5 |
| future_plans | 3 | 0/3 | 0.0% | 0.0% | 4 | 0/1 | 18.8 | 18.9 |
| kinship_alias | 8 | 5/8 | 62.5% | 81.2% | 3 | 0/0 | 20.2 | 20.7 |
| preference | 4 | 2/4 | 50.0% | 60.0% | 2 | 0/0 | 18.8 | 19.5 |
| pronoun | 3 | 3/3 | 100.0% | 100.0% | 0 | 0/0 | 21.0 | 22.4 |
| time | 4 | 1/4 | 25.0% | 33.3% | 2 | 0/1 | 21.8 | 25.3 |
| topic | 8 | 2/8 | 25.0% | 42.9% | 8 | 0/0 | 21.5 | 22.4 |

## Per-query detail

| id | category | query | ms | evidence | omitted | returned | abstain | matched |
|----|----------|-------|---:|---------:|--------:|---------:|:-------:|---------|
| canonical-001 | canonical_entity | 张丽最近怎样 | 24.1 | 2/2 | 0 | 14 | · | kg:mother, vec:失眠 |
| canonical-002 | canonical_entity | 王芳呢 | 21.6 | 1/2 | 1 | 14 | · | vec:王芳 |
| canonical-003 | canonical_entity | 铁锤是什么品种 | 20.14 | 2/2 | 0 | 9 | · | kg:pet:铁锤, vec:边境牧羊犬 |
| canonical-004 | canonical_entity | OP-3091 项目进展 | 20.9 | 1/2 | 1 | 13 | · | vec:OP-3091 |
| canonical-005 | canonical_entity | 李总找我谈话 | 20.35 | 1/2 | 1 | 6 | · | vec:李总 |
| canonical-006 | canonical_entity | 北京出差的事 | 19.93 | 0/0 | 0 | 9 | ✗ |  |
| canonical-007 | canonical_entity | 日本旅行计划 | 20.55 | 1/2 | 1 | 13 | · | vec:日本 |
| kinship-001 | kinship_alias | 我妈最近怎样 | 20.66 | 2/2 | 0 | 14 | · | kg:mother, vec:失眠 |
| kinship-002 | kinship_alias | 妈妈睡得怎样 | 19.89 | 2/2 | 0 | 14 | · | kg:mother, vec:失眠 |
| kinship-003 | kinship_alias | 我老婆和我吵架 | 20.24 | 2/2 | 0 | 14 | · | kg:wife, vec:吵架 |
| kinship-004 | kinship_alias | 我和老婆和好了吗 | 20.13 | 2/2 | 0 | 14 | · | kg:wife, vec:和好 |
| kinship-005 | kinship_alias | 我家狗多大 | 20.6 | 1/2 | 1 | 16 | · | kg:pet:铁锤 |
| kinship-006 | kinship_alias | 我家小狗最近怎么样 | 19.9 | 2/2 | 0 | 16 | · | kg:pet:铁锤, vec:铁锤 |
| kinship-007 | kinship_alias | 我们公司的项目 | 18.54 | 1/2 | 1 | 5 | · | vec:OP-3091 |
| kinship-008 | kinship_alias | 老板最近找我 | 20.57 | 1/2 | 1 | 15 | · | vec:李总 |
| pronoun-001 | pronoun | 她最近睡得好吗 | 20.67 | 2/2 | 0 | 15 | · | kg:mother, vec:失眠 |
| pronoun-002 | pronoun | 它身体怎么样 | 20.97 | 2/2 | 0 | 17 | · | kg:pet:铁锤, vec:铁锤 |
| pronoun-003 | pronoun | 我们和好了 | 22.38 | 2/2 | 0 | 14 | · | kg:wife, vec:和好 |
| time-001 | time | 最近几天我状态怎样 | 22.16 | 1/1 | 0 | 14 | · | vec:焦虑 |
| time-002 | time | 上周聊了什么 | 21.35 | 0/1 | 1 | 13 | · |  |
| time-004 | time | 我答应了什么事情 | 21.18 | 0/0 | 0 | 13 | ✗ |  |
| time-005 | time | 我今天做了什么 | 25.28 | 0/1 | 1 | 14 | · |  |
| topic-001 | topic | 我最近工作压力大吗 | 21.68 | 0/2 | 2 | 13 | · |  |
| topic-002 | topic | 我有焦虑情绪吗 | 21.71 | 1/1 | 0 | 13 | · | vec:焦虑 |
| topic-003 | topic | 我有什么健康问题 | 21.84 | 1/2 | 1 | 13 | · | vec:医生 |
| topic-004 | topic | 家里人健康吗 | 22.41 | 0/2 | 2 | 15 | · |  |
| topic-005 | topic | 我和家人的关系 | 19.97 | 1/2 | 1 | 14 | · | kg:wife |
| topic-006 | topic | 我和宠物的事 | 21.27 | 2/2 | 0 | 13 | · | kg:pet:铁锤, vec:铁锤 |
| topic-007 | topic | 最近开心的事 | 20.37 | 0/1 | 1 | 14 | · |  |
| topic-008 | topic | 我最近担心什么 | 19.69 | 1/2 | 1 | 13 | · | vec:焦虑 |
| preference-001 | preference | 我喜欢喝什么 | 19.47 | 1/1 | 0 | 13 | · | vec:茶 |
| preference-002 | preference | 我听什么播客 | 18.78 | 1/1 | 0 | 13 | · | vec:Acquired |
| preference-003 | preference | 我有什么爱好 | 18.4 | 0/1 | 1 | 14 | · |  |
| preference-004 | preference | 我喜欢去哪里玩 | 18.75 | 1/2 | 1 | 13 | · | vec:杭州 |
| future-001 | future_plans | 我以后想做什么 | 18.79 | 0/2 | 2 | 13 | · |  |
| future-002 | future_plans | 我答应了妈妈什么 | 18.87 | 0/0 | 0 | 15 | ✗ |  |
| future-003 | future_plans | 我计划去哪里 | 17.34 | 0/2 | 2 | 5 | · |  |
| negative-001 | abstention | 我家鸟会说话吗 | 21.23 | 0/0 | 0 | 16 | ✗ | VIOLATE-kg:pet:铁锤 |
| negative-002 | abstention | 我哥结婚的事 | 19.02 | 0/0 | 0 | 5 | ✗ |  |
| negative-003 | abstention | 我爸去世的事 | 17.85 | 0/0 | 0 | 5 | ✗ |  |
| negative-004 | abstention | 前任的事 | 18.15 | 0/0 | 0 | 14 | ✗ |  |
| negative-005 | abstention | 我儿子的学习 | 18.0 | 0/0 | 0 | 14 | ✗ |  |
| event-001 | event | 铁锤打疫苗 | 21.47 | 2/2 | 0 | 16 | · | kg:pet:铁锤, vec:疫苗 |
| event-002 | event | 我去医院的事 | 19.67 | 1/1 | 0 | 16 | · | vec:医院 |
| event-003 | event | 我跟客户吵架了 | 19.21 | 0/0 | 0 | 14 | ✗ |  |
| emotion-001 | emotion | 我什么时候开心 | 19.1 | 1/1 | 0 | 13 | · | vec:和好 |
| emotion-002 | emotion | 我什么时候焦虑 | 20.09 | 1/1 | 0 | 13 | · | vec:焦虑 |
| emotion-003 | emotion | 我半夜睡不着 | 18.95 | 1/1 | 0 | 14 | · | vec:焦虑 |

## Misses (composite-correct=false)

- `canonical-002` (canonical_entity): "王芳呢" — kg objects returned: wife, 拉伸, self, pet:小白; error=None; degraded=False; reason=None
- `canonical-004` (canonical_entity): "OP-3091 项目进展" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `canonical-005` (canonical_entity): "李总找我谈话" — kg objects returned: person:李总, 老板; error=None; degraded=False; reason=None
- `canonical-006` (canonical_entity): "北京出差的事" — kg objects returned: pet:铁锤, 边境牧羊犬, self, pet:铁锤; error=None; degraded=False; reason=None
- `canonical-007` (canonical_entity): "日本旅行计划" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `kinship-005` (kinship_alias): "我家狗多大" — kg objects returned: pet:铁锤, place:北京, pet:铁锤, 边境牧羊犬; error=None; degraded=False; reason=None
- `kinship-007` (kinship_alias): "我们公司的项目" — kg objects returned: —; error=None; degraded=False; reason=None
- `kinship-008` (kinship_alias): "老板最近找我" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `time-002` (time): "上周聊了什么" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `time-004` (time): "我答应了什么事情" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `time-005` (time): "我今天做了什么" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `topic-001` (topic): "我最近工作压力大吗" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `topic-003` (topic): "我有什么健康问题" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `topic-004` (topic): "家里人健康吗" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `topic-005` (topic): "我和家人的关系" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `topic-007` (topic): "最近开心的事" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `topic-008` (topic): "我最近担心什么" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `preference-003` (preference): "我有什么爱好" — kg objects returned: self, pet:铁锤, self, wife; error=None; degraded=False; reason=None
- `preference-004` (preference): "我喜欢去哪里玩" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `future-001` (future_plans): "我以后想做什么" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `future-002` (future_plans): "我答应了妈妈什么" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `future-003` (future_plans): "我计划去哪里" — kg objects returned: —; error=None; degraded=False; reason=None
- `negative-001` (abstention): "我家鸟会说话吗" — kg objects returned: self, wife, self, 烦躁; error=None; degraded=False; reason=None
- `negative-002` (abstention): "我哥结婚的事" — kg objects returned: —; error=None; degraded=False; reason=None
- `negative-003` (abstention): "我爸去世的事" — kg objects returned: —; error=None; degraded=False; reason=None
- `negative-004` (abstention): "前任的事" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `negative-005` (abstention): "我儿子的学习" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
- `event-003` (event): "我跟客户吵架了" — kg objects returned: self, pet:小白, self, pet:铁锤; error=None; degraded=False; reason=None
