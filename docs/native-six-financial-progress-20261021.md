# 六项正式财务补齐与读取修复（2026-10-21）

stage36完整不同作者复核通过，原182项目标累计正式补齐71项，剩111项；原585累计补474项，固定原120只为1809/1920，优先20只320/320保持。额外5只80项需求独立列示。

本批为001309 FY24收入，以及000612 H125、FY25、H126，000737 H125和002209 H125各一项合并净利润。Root唯一正常写入在21日22:15:13至22:21:44 CST完成6事实、6审核、6成员追加，四表为1906/601/1906/3。旧1900事实及原审核、金额、版本、来源、时间、补审和接受链接保全。新增六项最早22日08:45可用，不倒灌20日快照或此前决策。

六项维护经明确人类授权。初次完整API虽然HTTP200，却只接受1892/1906：同报告旧CFO/收入审核与新字段子集审核同时存在时，聚合读取错误地拒绝整个报告组，使9条旧事实和5条新事实在接受视图中缺失。物理账本未丢失，Root暂停扣数，保留原响应。

另获明确读取修复授权后，仅修多审核的互斥聚合，保留singleton原路径。9项相关回归和完整实际上下文public reader 15/15通过；API与scheduler额外各替换一次，非镜像配置、挂载和全部数据卷保持。修复阶段没有新增事实、审核或重写时钟。当前API为c4149ad6、scheduler为760af431，后续Compose须继续引用`tmp/remaining117-six-live-runtime/prepared-reader-repair1-pins.yml`，不可用旧声明镜像覆盖。

修复后一次实际完整API HTTP200、1906/1906被接受，耗时1233.331秒、0重试。Docker Linux只读SQL/PIT、全部旧对象及新六项链接对照通过；生产FinancialCoverage组件SSR通过，browser=false。原首次SQL消费者缺WAL/SHM挂载失败及本地摘要/检查错误保留，必要挂载修复后通过，没有重做正常入库或重复采源。

保存20日快照仍为125只/2000需求，1759项在截止可用、50项待后续截止、191项缺失（固定111＋额外80）。105个完整输入桥包含001309这一待截止完整输入，合法截止S2/信号仍104。所有原1803需求对象和原104桥/信号保全，新桥与新字段的待截止状态单列；不称105只均决策可用。PARTIAL_READY、策略家族CLOSED保持。

正常写入逐案重派真实证明和时钟；未复制clone时间、版本或根路径。五份净利润使用真实利润总额减所得税独立路径，原minority空值仍空，旧成功路径保持；未填0或放宽1pt。原002209 H126五项仍未获独立许可，不在本批范围。首次长读1124秒、本次1233秒及历史600/1800秒超时限制尚未解决。

21日日更仍为原FAILED/exit1，BaoStock限定模拟池登录超时；原请求及成功上游源保留，本维护没有重新daily或manual advance。五交易日M/E/B已完整关闭，不重验或追第六日。继续财务剩111项，产品既有调度保持。

实际证据：

- [唯一正常回执](../tmp/remaining117-six-live-runtime/normal-live-1/result/normal-live-six.json)
- [修复后实际API](../data/s2-structured/remaining117-six-live-api-stage36-20261021-reader-repair1.json)
- [实际消费者与SQL/PIT](../data/s2-structured/remaining117-six-live-api-consumer-stage36-20261021.json)
- [生产SSR](../data/s2-structured/remaining117-six-live-production-render-stage36-20261021.json)
- [完整不同作者复核](../tmp/remaining117-six-live-runtime/review.json)
