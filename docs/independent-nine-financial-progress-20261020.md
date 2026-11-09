# 2026-10-20 五报告九项财务续批

Stage33正式补齐9项。固定120只1780/1920，原585累计补445、剩140；目标模式自182起已补42。

| 证券 | 期间 | 实际金额（元） |
| --- | --- | --- |
| 000612 | 2025-06-30 | operating_cashflow=461941411.05, revenue=3308523506.56 |
| 000612 | 2026-06-30 | operating_cashflow=1164918122.48, revenue=4148082651.65 |
| 000612 | 2025-12-31 | revenue=6494970140.46 |
| 000737 | 2025-06-30 | operating_cashflow=-877502678.84, revenue=12810895390.34 |
| 002209 | 2025-06-30 | operating_cashflow=60618153.63, revenue=957480369.78 |

Root唯一正常追加9事实/5审核/9成员，1868→1877；实际运行2026-10-20T04:06:48.599500+00:00至2026-10-20T04:10:53.503964+00:00，可用时间2026-10-21T00:45:00+00:00。未复制dry时钟或倒灌旧截止。焦作万方两期现金流FX仍null、现金流恒等式false，依原完整格线路径真实通过；北方铜业和达意隆H125保留真实FX。原source41其余失败字段及FY25完整source4均保留，目标分别仍7/16、13/16、8/16。

一次实际API33 HTTP200、1877/1877，46.746秒、0重试；309条续批精确SQL、旧1771需求全对象/104桥/101信号/125市值、PIT与生产SSR及完整独立复核通过。19日快照仍1738当前可用，42pending、220缺，额外5只80需求独立；104输入桥与101合法S2不同，策略CLOSED。

首次隔离dry因包装脚本读取不存在的索引ref状态字段，在SQL/备份/writer前失败；修正后repair1正常dry通过。reader两次Docker启动，首次WAL挂载参数顺序错误在SQL前被拒，修正后唯一完整旧6b新9cohort reader通过。SQL及JSON保全一次通过；首次SSR使用了错误审计字段名，仅修正该属性后单独SSR通过，没有重跑API或SQL。原失败日志和执行代码快照均保留。生产源码、服务、配置和镜像未变，既有首次及长读性能限制保留。002209 H126五项维护仍未获答复，与本H125两项分开；M/E/B五日已闭合。

额外记录：Root误用600841的旧标题诊断，重复派发了一次只读source/strict核验；实际该FY24收入已在stage29正式闭合。重复结果与真实日志保留，支线已停止，没有current采集、写入或新增计数，也不重开stage29。

实际依据：[normal](../data/s2-structured/remaining149-independent9-normal-commit-20261020.json)、[API33](../data/s2-structured/remaining585-api-stage33-20261020.json)、[consumer33](../data/s2-structured/remaining585-actual-api-consumer-stage33-20261020.json)、[保全](../data/s2-structured/remaining149-independent9-stage33-saved-preservation-20261020.json)、[生产SSR](../data/s2-structured/remaining149-independent9-stage33-production-render-20261020.json)、[整批复核](../tmp/remaining149-five-report-independent-normal7-runtime/review.json)。
