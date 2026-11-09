# 十四项财务补齐与获准维护（2026-10-22）

stage39 已正式 CLOSED。原182项目标累计补齐99项，剩83项；原585累计补502项，剩83项。固定原120只核验1837/1920，优先20只320/320保持，其余100只1517/1600、88只输入完整。归档事实1934/1934被实际API接受，不能把归档行数作为固定1920需求的分母。

## 本批实际范围

| 证券与报告期 | 字段 | 正式新增 |
|---|---|---:|
| 000899 赣能股份 FY25 | 归母净利润、合并净利润、经营现金流、收入 | 4 |
| 600578 京能电力 FY25 | 归母净利润、合并净利润、经营现金流、收入 | 4 |
| 600578 京能电力 H126 | 归母净利润、合并净利润、经营现金流、归母权益、收入 | 5 |
| 000612 焦作万方 FY25 | 经营现金流 | 1 |

人类实际批准 call_c7260d77577d47468b3e6a36c38834d2/0“允许这次十四项维护（推荐）”。API与scheduler分别仅受控替换一次为dc619aaa／c5a31831，非镜像配置和挂载精确保全，原卷保留。原002209 H126五项及601069、600578 H124/FY24/H125不在该许可内。

## 真实写入和验收

Root唯一正常writer于22日21:31:14–21:36:25CST完成四报告追加：14事实、4审核、14成员，四表1920/608/1920/3变为1934/612/1934/3；旧四表及归档元数据精确保全。真实复核、ingestion、proof与正式归档根均由正常流程重新派生，未复制clone时钟、版本或路径。新增事实均最早23日08:45可用，不倒灌旧快照或决策。

受控替换后，原1920事实先经过一次真实完整API（HTTP200、1920/1920、1733.813秒、1请求0重试）及精确SQL旧对象门禁，才开始唯一writer。追加后一次API39实际HTTP200、1934/1934、459.373秒、1请求0重试。SQL消费核对所有旧1920原始行、旧1823需求的金额/来源/版本/时间/接受链接及新14事实、审核、成员、PIT。生产FinancialCoverage组件SSR通过，组件字节不变，未使用浏览器。完整不同作者review为PASS_COMPLETE_STAGE39、open0、rootMayClose；Root据此正式关闭stage39。

000612经营现金流的FX仍未知，现金恒等式为false；实际通过的是原生经营流入小计减流出小计证明，未填零。600578所选CURRENT的有限版本处置不表示企业合并没有经济影响，真实历史变额与其他未准入期间仍保留。

## 保留的失败与运行边界

22日原20:30日更实际FAILED，469.5秒，限定模拟池证据采集失败；旧研究发布和current保留。本维护没有恢复日更、重采来源或手工advance。

首次guard在备份PASS前自然退出并自动移除，原exit/stderr未记录；financial/snapshot部分备份与原输入在Docker Linux独立归属核查后rename完整保留。随后有界元库诊断实际复现全目录只读、WAL/SHM缺失时无法打开数据库；该诊断不冒充首次guard的实际stderr。必要修复仅开放SQLite元数据协调父目录，原数据库、已有WAL与raw分别只读，源SQL保持mode=ro/query_only/BEGIN/finallyROLLBACK。repair1实际保护备份通过。

首次SSR验收脚本误从API pair读取由consumer target推导的标志，在生产组件渲染前断言失败。原脚本、bundle和失败日志保留；仅修正该一个对象引用后，repair1生产SSR通过，没有重写事实、重读API或重跑SQL。历史600/1800秒首次失败及长读性能限制仍未解决。

22日21:57:15CST，guard按正常release-window协议释放锁并自然--rm退出；实际检查lock不存在、精确guard已不存在，两个owner运行且healthy。consumer runner已自行移除其已结束任务容器。没有手删锁、终止进程、删除卷或宿主文件，没有宿主磁盘回收量声明；所有备份、clone、失败证据及当前/回退/待批准镜像保留。这只是一次运行观察，不证明持续健康或缓存预热。

## 当前产品与后续

依据保存的20日快照，产品125只/2000需求与固定120/1920分母独立，额外5只80项不替换固定需求。当前截止1759可用、78待后续截止、163缺（固定83+额外80），125市值缺项0。108个归档完整输入含4个待截止完整证券，合法截止S2/排名仍104，PARTIAL_READY、family CLOSED；不称108只决策可用。

剩83分类：revision47、identity25、independent6、heading5。原182总目标尚未完成，继续具体必要证据和正常准入；候选、源资格或clone均不计正式补齐。M/E/B五日已闭合，本批不重验或延长比较。

实际证据：

- [唯一正常写入](../tmp/remaining97-current14-integration-runtime/live-preparation/live-output/result/normal-live-fourteen.json)
- [实际API39](../data/s2-structured/remaining97-current14-live-api-stage39-20261022.json)
- [实际SQL/PIT消费者](../data/s2-structured/remaining97-current14-live-api-consumer-stage39-20261022.json)
- [生产SSR](../tmp/remaining97-current14-integration-runtime/live-preparation/post-live-preparation/out/financial-coverage-stage39-20261022-repair1.json)
- [完整不同作者review](../tmp/remaining97-current14-integration-runtime/review.json)
- [保护窗口释放及owner观察](../tmp/remaining97-current14-integration-runtime/live-preparation/window/after-close-owner-and-resource-observation.json)
