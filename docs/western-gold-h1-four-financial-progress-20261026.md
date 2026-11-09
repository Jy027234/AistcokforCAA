# 西部黄金H1四项正式财务补齐（2026-10-26）

stage41完整验收关闭。原182项目标累计正式补106、剩76；原585累计补509、剩76，固定原120只为1844/1920，优先20只320/320保持。实际原始事实与接受数均1946，四表为1946事实／615审核／1946成员／3补件。新增9个真实版本对应4个唯一需求，不能按9项扣数。

| 唯一需求 | 最新接受的源值（元） |
|---|---:|
| 601069 H125归母净利润 | 131,522,116.30 |
| 601069 H125合并净利润 | 122,352,180.44 |
| 601069 H125经营现金流净额 | −1,345,956,935.44 |
| 601069 H126经营现金流净额 | 1,385,369,804.24 |

H125两项净利润保留原报告1224590046、更正附件1225082560、H126报告1225494335呈现的H125比较列，共6个版本。H125 CFO原值−1,312,511,105.67与后续比较值真实相差−33,445,829.77；CFO两需求保留3个版本。H126的source report period、H125的fact period和PRIOR列角色分别记录，没有把比较列塞入CURRENT，实际变额没有记作NO_EFFECT。未知FX仍为null、cashflow identity为false，独立路径与原1pt门槛保持；未补零或产生真人审核。

Root唯一实际入口在北京时间26日01:18:43至01:22:47完成，先NP2的6事实／1审核／6成员，再CFO的3／1／3，属于两个独立原子事务。NP2实际入库01:21:53，CFO实际入库01:22:37；最早26日08:45可用。值、版本、归档根和时钟均由正常writer重新派生，没有复制25日clone。

本次依据用户25日持续自动执行授权，API b14e6eed／scheduler55541883各受控替换一次，保留非镜像配置、挂载和数据卷。新reader先完整验证旧1937：首个实际请求1800.159秒超时保留，随后一次必要故障恢复HTTP200、1937/1937、393.572秒；没有重启或增加超时。实际入库后的唯一完整API为HTTP200、1946/1946、421.258秒、1请求0重试。旧1937四表全对象、原1840需求的金额／源／版本／时钟／接受链接保全，年度依赖3项与新两条版本链、PIT、生产SSR及完整不同作者复核均通过。

26日必要索引单次13个CNINFO请求（两regular页及共享broad的11页、309条公告）和正常日历成功，原PDF复用、下载0；25日已闭合采集和原生资格未重做。两日完整索引的业务条目无新增、删除或变化，26日receipt及实际query window仍由正常验证器重派。日历历史行情截至最近完成交易日23日，不声称已有26日行情。

保留的真实失败还包括：准备阶段原始source launcher、输出、scope、hash绑定和DTO tuple失败；fresh reader首次年度第三字段断言错误，发生在公共reader调用前；首个实际SQL consumer误用旧额外证券名单减当前产品池，修复为绑定原stage40不可变API来确定固定120／优先20。原120全在当前池，额外证券由001256变为603699属于既有产品快照变化，未换固定分母。独立scope修复后的SQL consumer通过，未重复source、金融writer或API。Root关闭脚本首次字段名与reviewStatus不一致，在状态写入前失败；仅修metadata适配，未重做业务验收。原错误、执行代码及回执均保留。

实际产品快照为`snap-eod-2026-10-23-3c08e6c0f685426c`，cutoff为25日14:49:25.311093Z：125产品／2000需求，1840截止可用、4待截止、156缺（固定76＋额外80），市值缺0。108归档完整输入／108合法S2；同一快照下原108个完整证券的financialInputs和signal完全一致。策略仍PARTIAL_READY／family CLOSED。新4项未倒灌旧截止；旧stage40快照和当时104合法S2的记录保留。

保护窗口在实际API通过后正常release，26日01:31:29锁已释放。Root独立核对并清理本正式窗口4个结束容器；业务卷、源、证据、备份、clone和当前／回退／待排障镜像保留，没有宿主磁盘回收量声明。浏览器未验证。历史600/1800秒失败、1463秒冷读及本次首次超时限制仍未解决，后续读取时长不代表冷读性能修复。产品日更继续由原scheduler负责，26日20:30执行结果尚未观察；已关闭的M/E/B五日比较不重开。

实际依据：

- [正常入库回执](../tmp/remaining80-western-gold-h1-four-integration-runtime/live-preparation/append-window/20261025T171750Z/normal-output/normal/normal-live-four.json)
- [实际完整API41](../data/s2-structured/remaining80-westgold-h1-four-live-api-stage41-20261026.json)
- [正式consumer41](../data/s2-structured/remaining80-westgold-h1-four-live-api-consumer-stage41-20261026.json)
- [完整不同作者复核](../tmp/remaining80-western-gold-h1-four-integration-runtime/live-preparation/actual-wholebatch-review.json)
- [生产SSR](../tmp/remaining80-western-gold-h1-four-integration-runtime/live-preparation/post-live-preparation/out/ssr-current26/financial-coverage-westgold-four-current26.json)
- [保护窗口与保全](../tmp/remaining80-western-gold-h1-four-integration-runtime/live-preparation/append-window/20261025T171750Z/guard-output/guard/append-result.json)

原182财务目标尚未完成，继续剩76；准备、source-only和派生截止变化不计为新增补齐。
