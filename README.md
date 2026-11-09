# A-Quant Lab

2026-11-09：v18完整核验2039条来源证据通过，冷作业20分54秒、提交0.54秒；188份纯页面缓存逐项重验，原时点、修订和正式S2门禁保留。三份数据库68表从ZIP实际恢复一致。两处后续财务配对仍未正式入库，五批次真实新发布首读及20交易日观察尚未全部验收，见[当前范围与限制](docs/financial20-source-resolution-20261107.md)。

源码仓库不发布运行数据库、`work/` / `tmp/` 留证目录、本机配置或第三方原文页面缓存。缺少私有缓存时按正常PDF路径重新解析，不缓存准入结论；本机合格镜像中的缓存锚点单独固定。私有布局回归资料的来源与哈希见 `tests/fixtures/private-native-layouts.json`，资料未安装时对应测试会明确跳过。

本次源码集成保留已提交的原生表格解析兼容性，并使用 `financial-check-v19` 区分已验收的本机v18镜像，避免复用不同代码版本的财务结果。提交与推送不会更新运行容器；源码构建尚不能替代原文齐备环境下的完整财务验收。

2026-11-07：原20项财务来源已补齐并上线，固定覆盖含待生效为1920/1920；实际当前完整核验2039/2039条事实通过，旧数据与必要回滚保留。新增20点最早11月9日08:45生效，正式S2和五批次其余观察门槛继续保留。见[本轮结果与边界](docs/financial20-source-resolution-20261107.md)。

2026-11-05 凌晨：发布后的读取预热修复已上线，API 80项、调度器128项检查通过。相同真实Nov3→Nov4资料回放中，发布后固定13秒开始的研究首读8.18→0.91秒，三路完整响应一致；当前线上研究/目录/模拟上下文为1.20/1.39/2.01秒。三库68表、当前财务结果、数据门禁与原调度保持，直接回滚和经64成员校验的代码配置增量包已保留。财务仍缺20项合格证据；新版本下一次正常发布首读和20个真实交易日观察仍待实际记录，五批次未标完成。[本批验收](work/goal-five-batches-20261102/remaining-work-20261104/delivery.json)

2026-11-04 当前验证状态：BaoStock手动恢复已发布11月4日数据，129只模拟池与同日市值齐、stale=false。当前财务2019条来源事实全量核验通过，提交0.572秒/完整16分59秒；固定财务仍剩20项，S2保持关闭。三库隔离恢复和旧数据保全通过，20个真实交易日稳定性及新发布首读3秒建议目标仍未完全验收；没有恢复Codex自动补缺任务。实际最新证据见[五批次计划](docs/product-usability-plan-20261101.md)。


2026-11-02 已按原五批次计划进入目标执行：模拟资格读取复用经完整校验的原始字节，慢读取使用独立连接；受限研究助手已完成真实公开摘录调用、原子预算与归档重放，日预算上限¥0.10，本次用量按官方登记价格上界计¥0.00389。核心键盘、失败重试及390像素比较页面已验；独立恢复目录的1960事实及3份补充证明已通过完整生产reader核验。原固定财务仍剩64项，下一次正常日更与20个真实交易日观察继续推进；没有恢复Codex自动补缺任务。进度与证据见[五批次目标执行](docs/product-usability-plan-20261101.md#11-五批次目标执行2026-11-02)。

2026-11-02 研究候选名单与快照目录加载优化已上线：保留逐次完整哈希与来源验证，复用已验证的解析结果和小摘要，启动时预热。候选接口从约35秒降至3.3–3.8秒，目录从约46秒降至3.1–3.7秒（并发采样）；桌面候选首屏3.64秒、390像素首屏2.98秒。另合并同页重叠的目录GET，完成或失败后不缓存结果。原数据、正式S2关闭状态和必要回滚保持，完整结果及冷读限制见[本批执行记录](docs/product-usability-plan-20261101.md#10-研究候选与快照目录2026-11-02)。

2026-11-02 已将数据从10月28日恢复到10月29日：复用原日收盘后的有效收据，补采历史模拟证据，保留实际观察时间及全部准入门禁。财务提交的实际接口耗时降至约0.92–1.18秒，独立Chrome实测1.73秒；29日新输入首次提交0.96秒，完整核验11分59秒成功，版本变化时旧结果仍明确失效。10月30日缺少合格当日收据，下一次既有定时日更为11月2日20:30。实施进度、实测值和未完成验收见[可用性计划执行记录](docs/product-usability-plan-20261101.md#9-后续推进2026-11-02)。

当前本机的已验证版本通过 `tools/start_current_docker_trial.ps1` 启动，固定镜像在 `deploy/local-trial/usability-20261101.compose.json`。本机运行以固定镜像为准，源码集成与未验收候选不自动升级现场；对应版本验收完成前，不能用根目录 Dockerfile 全量重建替换现行环境。原镜像和前一稳定版保留供回滚。

2026-10-28 按用户要求，本聊天的自动补缺任务已删除，尚在进行的代理工作已停止。正式财务保留stage45／原182剩64的实际结果和全部证据；产品原有日更调度未改。

2026-10-27 京能电力FY24收入一项正式补齐，stage45完整验收关闭：原182累计补118／剩64，固定1856/1920（当前1849、待截止7），实际接口1960/1960、819.023秒、1请求0重试。两真实版本正常追加2/1/2/0，只计一个需求；旧1958全行／接受链接、SQL／PIT、生产SSR及完整不同作者复核通过。实际27日新快照下111归档桥／110截止合法S2，产品126/2016缺160，原快照不回填；新收入28日08:45可用，family仍CLOSED。API／scheduler各替换一次、窗口正常释放，原失败保留，未重做writer或完整GET。27日正常日更已PUBLISHED／10步完成；600988下一十字段仍只是候选。见[实际结果和边界](docs/600578-fy24-revenue-financial-progress-20261027.md)，[当前生产pins](tmp/financial-latency-20261027/next-600578-versions/release-annual-v4/image-pins.yml)。

2026-10-27 达意隆H126五项正式补齐，stage44完整验收关闭：原182累计补117／剩65，固定1855/1920（当前1844、待截止11），实际接口1958/1958、541.871秒、1请求0重试。原1953全行／接受链接及三个补充证明、SQL／PIT、真实生产SSR四态和独立整批复核通过。111归档输入桥／108截止合法S2，旧26日快照和当前信号保持，实验CLOSED；新五项28日08:45起可用。normal一次真实提交后的工具清理exit1及零请求恢复失败均保留，没有重做写入。见[实际结果与边界](docs/002209-h126-financial-progress-20261027.md)，[当前生产pins](tmp/financial-latency-20261027/next-native6-current27/release-v1/image-pins.yml)。

2026-10-27 京能电力H125归母权益1项正式补齐，stage43完整验收关闭：原182累计补112／剩70，固定1850/1920，实际API1953/1953、581.956秒、1请求0重试。旧1952全行／接受链接（含3补充证明）、SQL／PIT、真实生产SSR四态及不同作者整批复核通过；新事实最早28日08:45可用，600578仍缺6项，当前输入和信号未回填。110完整输入桥／108合法S2与CLOSED保持；下一6项隔离候选34pins／46契约测试通过，尚未准入。见[实际结果和失败边界](docs/600578-parent-financial-progress-20261027.md)，[当前生产pins](tmp/financial-latency-20261027/next-600578-parent/release-current-v2/image-pins.yml)。

2026-10-27 财务深查性能修复已部署并完成独立验收：真实接口单次543.549秒（约9分04秒），原1864.988秒（约31分05秒），耗时减少约71%；1952/1952接受及全部业务语义保留，深查期间四个轻量接口均200、0.004–0.274秒。四进程执行原核验、五类报表标题定位及32项相关Linux检查通过，旧事实／链接／快照和日常修复保留；原严格JSON比较对两项既有动态试算时钟的失败与独立契约核验均如实保存，未重发完整接口。该性能窗口正式财务仍剩71项；后续补齐以首段最新记录为准。冷启动仍以分钟计、浏览器及多人负载未验。见[实际性能与限制](docs/financial-deep-check-latency-20261027.md)，[性能发布pins](tmp/financial-latency-20261027/release-recall-v3/image-pins.yml)。

2026-10-27 五项历史营业收入正式补齐，stage42完整验收关闭：原182累计补111／剩71，固定1849/1920，实际完整API1952/1952（1864.988秒、1请求0重试）；6个版本对应5个唯一需求，旧账／SQL／PIT／生产SSR及整批独立复核通过。API与scheduler新镜像已健康运行，日常研究和人工模拟就绪，原快照及已关闭实验保留。财务长读与浏览器验证限制仍在，静默补缺继续。见[实际结果和证据](docs/historical-revenue-five-financial-progress-20261027.md)，[当前生产pins](tmp/daily-recovery-20261026/release-financial-annual-calendar-20261027/image-pins.yml)。

2026-10-26 日更恢复及持续运行修复已部署：26日126只模拟池、15116条行情、126只同日市值和9步正常流水线均通过，完整S2为108只有限输入／全局CLOSED；原失败、123只partial和修复启动错误如实保留。API与scheduler各一次受控替换，30业务表／4财务表／2归档表及current精确保全，12个现有财务源码模块未变。SDK查询接收失败一次重连、同日15分钟后最多两次有限恢复、请求原子去重及就绪按需读取上线；实际就绪49.86秒降至0.522秒，人工模拟ready。后台自动补缺已恢复，常规通知关闭。正式财务仍stage41／1946条／剩76，额外五项候选已通过1946条事实及旧链接的完整保全检查，尚未部署或正式追加，财务完整验算1629.2秒的延迟仍在。见[实际恢复、部署与限制](tmp/daily-recovery-20261026/runtime-validation.json)，[最新生产pins](tmp/daily-recovery-20261026/release-daily/image-pins.yml)。

2026-10-26 西部黄金H1四项正式补齐，stage41完整验收关闭：原182累计补106／剩76，固定1844/1920，实际API1946/1946；新增9版本对应4个唯一需求。正常writer、旧1937对象／SQL／PIT、原108完整输入与信号、生产SSR及完整不同作者复核通过，最早26日08:45可用，旧快照未回填。保护锁已正常释放，当前b14／555镜像保持，冷读性能限制保留。见[完整实际结果](docs/western-gold-h1-four-financial-progress-20261026.md)。

2026-10-26 财务四项续批已通过组合隔离演练和独立准备复核，API／scheduler已各受控替换一次，当前生产镜像见[实际pins](tmp/remaining80-western-gold-h1-four-integration-runtime/live-preparation/prepared-image-pins.yml)。首个完整API读取1800秒超时保留；随后一次必要故障恢复实际HTTP200、1937／1937接受，393.572秒，未重启或增加超时。保护窗口已正常释放，30业务表、4财务表、2归档表及快照指针保持。26日必要索引和日历已采集，但尚无正式财务追加，原182仍补102／剩80；冷读性能仍未解决。下面已闭合日常修复的镜像为历史基底。见[实际恢复读取结果](tmp/remaining80-western-gold-h1-four-integration-runtime/live-preparation/reader-window/old1937-api-recovery1.json)。

2026-10-25 自动补缺续批已验收：125只历史市值已按真实双时钟重放并发布为新不可变快照 `3c08e6c0f685426c`，原23日14996条行情及研究文件保持，23/24日旧截止仍拒绝使用25日生成的清单。人工模拟就绪；研究候选实际首读14.263秒、热读2.789秒，期间状态接口0.064秒。API与scheduler各一次受控替换，30业务表和4财务表保全、实际HTTP及完整独立复核通过。正式财务仍stage40：1937条、原182已补102/剩80；财务深查1463秒及readiness约10秒的延迟仍待改进，正式S2未据此开放，下一日更尚待实际观察。见[完整执行与复核](tmp/auto-fill-20261025/repair1/review.json)；当前生产镜像固定在[最新pins](tmp/auto-fill-20261025/repair1/image-pins.yml)。

2026-10-25 日常可用性修复已部署：失败/休市跳过/新发布的新鲜度状态修复、顶栏告警和财务纯PDF同键并发合并均已上线，保留当前已准入reader兼容代码。API与scheduler各替换一次且健康，30业务表/4财务表及后续旧账本保全通过；财务事实1937条未改。容器BaoStock登录/查询成功，23日125只价格池已按真实25日时钟恢复并发布为`19d829bd224d42a1`，实际状态/研究/模拟就绪/日程/前向接口通过，下一既有日更26日20:30。历史23日无合法市值批次清单，市值0/125、S2关闭与只读增强扫描中断记录如实保留；未回填收据时钟或放宽契约。研究首读约24秒，单次财务冷读性能、后续连续日更和浏览器仍未验证。原失败及首次API旧采集入口的中断均保留。见[实际部署、恢复与限制](tmp/daily-stability-20261025/runtime-validation.json)及[BaoStock诊断](tmp/daily-stability-20261025/provider/diagnostic.md)。

2026-10-23 获准西部黄金FY25三项正式财务补齐，stage40完整验收通过：原182累计补102/剩80，固定1840/1920；实际API1937/1937、旧1934全对象/SQL/PIT/生产SSR及完整不同作者review通过。新增3最早23日08:45可用，108归档桥/104合法S2保持；已正常释放维护锁，长读限制保留。见[实际结果](docs/western-gold-fy25-three-financial-progress-20261023.md)。

2026-10-22 获准十四项财务补齐与维护完成，stage39完整验收通过：原182累计补99/剩83，固定1837/1920；实际API1934/1934、旧1920/SQL/PIT/生产SSR和完整不同作者review通过。新增14最早23日08:45可用，108归档桥/104合法S2；原卷与配置保留，正常释放保护锁。见[本批实际结果](docs/current-fourteen-financial-progress-20261022.md)。


2026-10-22 焦作万方FY25归母净利润一项正式补齐，stage38完整验收通过：原182累计补85/剩97，固定1823/1920；实际归档API1920/1920、旧1919/SQL/PIT/生产SSR和完整不同作者review通过。当前镜像不变、服务动作0，最早23日08:45可用；107桥/104合法S2保持。归档1920条不等于固定需求全完成。见[实际结果](docs/wanfang-fy25-attributable-financial-progress-20261022.md)。


2026-10-22 获准十三项正式财务补齐与维护完成，stage37完整验收通过：原182累计补84/剩98，固定1822/1920，实际API1919/1919、旧1906/SQL/PIT/生产SSR及不同作者整批复核通过。107完整输入桥含3待截止，合法S2仍104；新增13最早22日08:45可用，长读限制保留。见[本批实际结果](docs/native-thirteen-financial-progress-20261022.md)。


2026-10-21 六项正式财务补齐及获准读取修复完成，stage36整批验收通过：原182项目标累计补71/剩111，固定1809/1920。实际API1906/1906、旧对象/SQL/PIT/生产SSR和完整独立复核通过；105输入桥含1待截止，合法S2仍104。新增六项最早22日08:45可用，首次长读限制保留。见[本批实际结果](docs/native-six-financial-progress-20261021.md)。

2026-10-21 获准独立19项维护与正式准入完成，stage35整批验收通过：原182项目标累计补65/剩117，固定1803/1920。实际API1900/1900，旧1881及SQL/PIT保全、生产SSR和完整独立复核通过；新增19最早22日08:45可用，104完整输入桥保持。见[本批实际结果](docs/finite19-financial-progress-20261021.md)。

A股研究与模拟决策工作台。首期定位为**自用研究与模拟**，不连接券商、不自动交易、不提供付费荐股或代客理财。

2026-10-20 西部黄金H126四项正式补齐，stage34完整验收通过：固定1784/1920，目标182已补46/剩136。实际API1881/1881、313续批精确SQL、旧对象/PIT/生产SSR及整批复核通过。见[实际结果](docs/western-gold-four-financial-progress-20261020.md)。

2026-10-20 五报告九项财务正式补齐，stage33完整验收通过：固定120只1780/1920，原585补445/剩140；目标模式自182起正式补42。实际API1877/1877、309续批精确SQL、旧对象/PIT/生产SSR与整批复核通过；104输入桥、合法S2仍101，服务未重启。见[本批实际结果](docs/independent-nine-financial-progress-20261020.md)。

2026-10-20 山推H125现金流/收入两项正式补齐，stage32完整验收通过：固定120只1771/1920，原585补436/剩149；目标模式自182起正式补33。实际API1868/1868、300续批精确SQL、旧对象/PIT/生产SSR及整批复核通过；104完整输入桥、当前合法S2仍101，服务未重启。见[本批实际结果](docs/shantui-h125-financial-progress-20261020.md)。

2026-10-20 世纪华通H126五项正式补齐，stage31完整验收通过：固定120只1769/1920，原585补434/剩151；目标模式自182起正式补31。实际API1866/1866、298续批精确SQL、旧对象/PIT保全、生产SSR及整批复核通过。104完整输入桥、当前合法S2仍101；服务未重启。见[本批实际结果](docs/century-huatong-h126-financial-progress-20261020.md)。

2026-10-20 世纪华通H125五项正式补齐，stage30完整验收通过：固定120只1764/1920，原585补429/剩156；目标模式自182起正式补26。实际API1861/1861、293续批精确SQL、旧对象/PIT保全、生产SSR及整批复核通过。104完整输入桥、当前合法S2仍101；服务未重启。见[本批实际结果](docs/century-huatong-financial-progress-20261020.md)。

2026-10-20 两份FY24收入正式补齐，stage29完整验收通过：固定120只1759/1920，原585补424/剩161；目标模式自182起正式补21。实际API1856/1856、288续批精确SQL、旧对象/PIT保全、生产SSR和整批独立复核通过。104完整输入桥含3待截止，当前合法S2仍101；19日快照1738当前可用/21pending，新2最早20日08:45可用。服务未重启，读取延迟限制保留。见[本批实际结果](docs/two-fy24-financial-progress-20261020.md)。

2026-10-20 云铝四原报告15项正式补齐，stage28完整验收通过：固定120只1757/1920，原585补422/剩163；目标模式自182起已正式补19。实际API1854/1854、286续批精确SQL、旧对象/PIT保全、生产SSR及整批独立复核通过。102完整输入桥含1待截止，当前合法S2仍101；19日快照1738当前可用/19pending，新15最早20日08:45可用。服务未重启，本次54.983秒不代表首次冷读限制解决。见[本批实际结果](docs/yunnan-aluminium-financial-progress-20261020.md)。

2026-10-20 目标模式财务续批：申通快递002468 FY25四项正式补齐，固定120只1742/1920，原585补407/剩178。主Docker实际API1839/1839、271续批精确SQL、旧对象/PIT保全、生产SSR和整批独立复核通过。19日快照1738当前可用/4待截止、101合法S2；新四项20日08:45可用，申通FY24收入仍缺。服务未重启，API1154.743秒长读限制仍在；云铝15项仅源验证通过、尚未入库。见[本轮实际结果](docs/shentong-financial-progress-20261020.md)。

2026-10-19 M/E/B五个真实交易日对照完成：M/E各净值1,001,388.86元（+0.138886%），B986,703.29元（−1.329671%）；累计费用各203.14/556.71元，19日新增订单/成交/费用均0。M−B14,685.57元=价格贡献差14,332元＋少付费用353.57元，E−M及归因残差0。19日唯一正常离线恢复22:30:57 PUBLISHED/exit0，9步全过，三个实际API通过；原DONE/SKIPPED请求及旧账本保留。新快照125池、市值缺0、101只合法截止S2，策略CLOSED；财务原585仍补403/剩182，隔离准备不计入正式。见[五日完整对照与日更恢复](docs/meb-five-day-results-20261019.md)。

2026-10-18 西部矿业FY25四项正式补齐：普通4事实/1审核/4成员追加，主Docker实际API1835/1835，固定120只1738/1920、101输入桥；原585补403/剩182。新四项19日08:45后可用，当前96只合法S2与16日决策保持。267条精确SQL、旧对象保全、生产SSR和整批独立复核通过；单次完整API1065.570秒，读取延迟仍未解决，浏览器未验。辅助6源已取得但不计正式；M/E/B4/5继续，下一19日20:30日更后核第五日。见[本轮完整结果](docs/western-mining-financial-and-meb-20261018.md)。

2026-10-16 晚间完整结果：正常日更21:07 PUBLISHED/exit0，10步骤通过、研究3270只、市值124/124；M/E/B已观察4/5日并完成账本/API/独立复核。恺英网络历史收入1项正式补齐，固定120只1734/1920、100财务输入桥，原585补399/剩186；当前96只合法S2计算、策略CLOSED。完整结果、首次API超时限制和下一批见[本轮交付](docs/meb-day4-and-financial-progress-20261016.md)。

2026-10-16 M/E/B第4/5日真实观察完成：已保存16日实际模拟档案，原冻结源/spec/research保持；M/E净值991,366.86元（−0.863314%）、B979,853.29元（−2.014671%），当日新增订单/成交/费用均0，累计费用M/E各203.14元、B556.71元。M−B11,513.57元=价格11,160元+少付费用353.57元，E−M和归因残差0。独立账本核验、两个实际API与完整不同作者复核通过；完整日更运行状态单列，不以本观察结论代替。见[本日完整核验](data/s2-structured/remaining585-meb-observation-20261016.md)。

2026-10-16 002517FY24收入一项财务续批完成：1报告1项正常追加，实际API1831/1831、固定120只1734/1920、实际100桥。原585补399/剩186；真实时钟/PIT与旧对象保持。见[完整验收](docs/002517-fy24-financial-and-meb-20261016.md)。

2026-10-16 002946FY25四项财务续批完成：1报告4项正常追加，实际API1830/1830、固定120只1733/1920、实际99桥。原585补398/剩187；真实时钟/PIT与旧对象保持。见[完整验收](docs/002946-fy25-financial-and-meb-20261016.md)。

2026-10-16 605499H125五项财务续批完成：1报告5项正常追加，实际API1826/1826、固定120只1729/1920、实际98桥。原585补394/剩191；真实时钟/PIT与旧对象保持。见[完整验收](docs/605499-h125-financial-and-meb-20261016.md)。

2026-10-16 603162两报告9项财务续批完成：2报告9项正常追加，实际API1821/1821、固定120只1724/1920、实际97桥。原585补389/剩196；真实时钟/PIT与旧对象保持。见[完整验收](docs/603162-two-report-financial-and-meb-20261016.md)。

2026-10-16 春秋航空H1财务续批完成：3报告11项正常追加，实际API1812/1812、固定120只1715/1920、实际96桥。原585补380/剩205；真实时钟/PIT与旧对象保持。见[完整验收](docs/spring-air-h1-financial-and-meb-20261016.md)。

2026-10-16 年报财务续批完成：2报告8项正常追加，实际API1801/1801、固定120只1704/1920、实际96桥。原585补369/剩216；真实时钟/PIT与旧对象保持。见[完整验收](docs/annual-financial-and-meb-20261016.md)。

2026-10-16 三家公司历史财务14项续批完成：8报告普通门禁真实追加14项，实际API1793/1793（117.224秒）、固定120只1696/1920、真实96输入桥与SSR、SQL及最终独立复核通过。原585补361/剩224，自剩306后补82；旧对象/链接与15日决策保持。M/E/B仍保存3/5，不制造16日结果。见[完整验收](docs/three-company-history-financial-and-meb-20261016.md)。

2026-10-16 宇通历史收入两项续批完成：两份2024历史收入正常入库，主Docker实际API1779/1779接受（128.429秒），固定120只1682/1920、实际94财务输入桥及SSR、精确SQL和整批独立复核通过。自剩306累计补68、现剩238，原585累计补347；旧对象/链接及15日决策保持。新增数据最早16日08:45可用，当前仍85只合法截止S2、策略家族CLOSED。M/E/B3/5继续，今晚20:30后跟进新真实档案。见[实际金额与完整验收](docs/yutong-history-financial-and-meb-20261016.md)。

2026-10-16 渤海租赁与宇通客车正式20项续批完成：本批6+14普通门禁真实入库，主Docker完整API1777/1777接受（121.351秒），固定120只1680/1920、实际93完整财务输入桥与生产SSR，精确SQL及最终整批独立复核通过。自剩306后累计补66、现剩240，原585累计补345；旧1757对象/1660固定字段、15日快照与决策保持。新增20项最早16日08:45可用，当前截止85只S2、策略家族CLOSED；M/E/B仍原保存3/5日，下一产品更新16日20:30。见[金额、真实时点与完整验收](docs/bohai-yutong-financial-and-meb-20261016.md)。

2026-10-16 三份上年半年报续批完成：万华化学/福达合金/西部矿业各5项正式入库，自剩306后补46、现剩260，原585累计补325。当前主Docker完整API1757/1757接受（209.692秒），固定120只1660/1920、92个完整财务输入与生产SSR、精确SQL和整批独立复核通过。无新增parser改动，source15实际通过；4格线事实另经同主API旧6b镜像仅新3cohort读取15/15通过。旧对象/链接及15日决策保持，新增15项最早16日08:45可用，当前合法截止仍85只S2、策略家族CLOSED。M/E/B3/5日保持，今晚16日20:30跟进新实际成交、费用、净值及归因。见[实际金额与完整验收](docs/revenue-h125-financial-and-meb-20261016.md)。

2026-10-16 盐湖股份正式续批完成：三期11项正常入库，加上星宇等前批20项，自剩306后补31、现剩275；原585累计补310。实际主Docker完整API1742/1742接受（131.434秒），固定120只1645/1920、92个完整财务输入及生产SSR通过，旧对象/链接与15日决策保持。跨日使用Luna真实16日索引/日历，H124依原合法历史上限窗和当日全类别索引准入；51/92检查、旧身份兼容与整批独立复核通过，年报异公司仍拒绝。新增数据不倒灌，当前合法截止仍85只可算S2，策略家族CLOSED。M/E/B3/5日保持，今晚16日20:30等待新实际档案核账。见[实际金额与完整验收](docs/saltlake-identity-and-meb-20261016.md)。

2026-10-15 星宇股份续批完成：星宇2026半年报五项及万华化学/福达合金/西部矿业同类布局15项正式入库，原585累计补299、剩286。当前主Docker完整API1731/1731接受（171.142秒），固定120只1634/1920、92个完整财务输入，163条精确SQL及生产92桥SSR、整批独立复核通过。旧对象/链接和15日决策不改，新增20项最早16日08:45可用；当前cutoff仍1544可用、90待截止、85只可算S2，策略家族CLOSED。主API未重启，首次延迟限制及浏览器未验边界保留。M/E/B保持已保存3/5日，下次日更16日20:30，等待实际新档案核成交、费用、净值及归因。见[实际金额、兼容范围和正式验收](docs/xingyu-and-four-report-financial-meb-20261015.md)。

2026-10-15 株冶与独立凭证续批完成：株冶九项及招商轮船/星宇股份/山推股份13项正式入库，自328补22、剩306；原585累计补279。当前API1711/1711接受（354.636秒），固定原120只1614/1920、91个完整财务计算输入，143条精确SQL及生产SSR通过。甘肃能源五字段严格外部源证明完成，原24项专项最后缺口关闭，旧canonical链接及585计数不改。今晚日更经原证据恢复于22:00 PUBLISHED/exit0，9步骤通过、新外部请求0；M/E/B第三日已保存并核账/API，M/E净值990,338.86元（−0.966114%）、B977,064.29元（−2.293571%），当日新订单/成交/费用均0，累计M−B13,274.57元、残差零。125只同日市值无缺项、当前85只可计算S2，策略家族仍CLOSED；新增22项最早16日08:45可用。状态摘要已同步成功，原20:30失败历史保持；主API未重启，下次日更16日20:30、观察3/5继续。见[完整金额、来源与验收](docs/zhuye-gansu-and-meb-20261015.md)。

2026-10-15 现金流完整批次结算：自用户提出剩371项后正式补齐43项，现剩328；原585累计补257。当前主Docker完整API1689/1689接受（44.803秒），固定120只1592/1920、89个API财务计算桥，121条续批SQL与89桥SSR通过。完成现金流断行/跨页、完整括注和归母权益粘行修复；235项相关检查、旧8/19和3份补审重放、隔离20项及整批独立复核通过。旧CFO外部源审计24项金额全一致、23项严格独立通过，另1项甘肃能源旧凭证缺口单列，89桥不等于该专项全部通过。本轮43项最早16日08:45可用；今晚20:30日更与M/E/B第三日等待实际档案，原ACTIVE跟进保持。见[实际金额、财务计算与完整验收](docs/cashflow-formal-batch-and-meb-20261015.md)。

2026-10-15 现金流续批：东鹏饮料605499 H126五项18:41正常入库，主Docker完整API1651/1651接受（99.981秒）。修复原文三行CFO布局，122测试、旧8份证明/19条事实完整重放、隔离晋升及独立复核通过；原1646条及旧审核/链接保持。原585累计补219、剩366，固定原120只1554/1920核验、85只完整；新增五项最早16日08:45可用，旧快照1539项可用/15项待截止。83条续批SQL及85桥生产SSR通过。今晚15日20:30日更和M/E/B第三观察日仍待实际保存，现有跟进保持。见[金额、正式验收与后续缺项](docs/dongpeng-cashflow-and-meb-followup-20261015.md)。

2026-10-15 最新财务结算：渤海租赁000415 H126五项15日12:49正常入库，主Docker完整API1646/1646接受（125.532秒），原585累计补214、剩371；固定120只1549/1920核验、85只完整、优先20只320/320。五项最早16日08:45可用，当前旧快照1539项可用、10项待后续截止；原对象、旧链接及78条续批SQL核对通过，85个生产计算桥重新渲染通过。用户明确要求今晚跟进后，已恢复原有跟进任务；产品日更仍为15日20:30，M/E/B已保存2/5日，第三日等待今晚实际档案。见[本次五项金额、正式验收与试运行汇总](docs/bohai-financial-and-meb-followup-20261015.md)。

2026-10-15 续批：修复14日日更的跨平台来源路径，复用原证据于15日08:29补跑保存14日实际价格快照与M/E/B第2/5日观察，08:47全流程PUBLISHED/exit0，9步骤通过、提供方调用0次。M/E净值1,006,264.86元（+0.626486%）、B996,557.29元（−0.344271%），14日无新成交/费用，累计M−B9,707.57元、归因残差零。山推股份000680 H126五项正常入库，当前完整API1641/1641接受；固定原120只1544/1920核验、85只完整，原585累计补209、剩376，新增五项15日08:45后可用。14日原行情中已有124只市值原值，但缺当日正式聚合收据链，S2保持关闭。scheduler来源修复及60分钟总预算已部署，68项来源测试、31项调度测试及独立复核通过，预热API未重启；下一产品日更15日20:30。见[恢复过程、实际金额和完整验收](docs/daily-recovery-and-formal-s2-stage6-20261015.md)。

2026-10-13 Docker 真实部署已更新：当前入口 [http://127.0.0.1:8080](http://127.0.0.1:8080/)，API 与调度器均健康。原数据和账本保留，Windows API/worker及恢复任务已停用。前端产物逐字节匹配，五宽度浏览器 44 项和实际证券交接 5 项通过；首次财务深查的长时 PDF 重放单独记录。见[Docker 更新与验收](docs/docker-frontend-release-20261013.md)。

2026-10-14 正式财务第五批：渤海租赁000415 H125五项经正常门禁实际入库，当前Docker完整API1636/1636事实接受。固定原120只核验1539/1920、85只完整，原585已补204、剩381；优先20只320/320保持。新增事实最早14日08:45可用，13日快照仍225项、11只完整，全局S2门禁保持。Luna补采14日真实日历和完整索引，旧原文、旧事实/审核/链接及85个计算输入保持；52测试、独立复核、旧19/8源重放和68条精确SQL通过。实际完整API耗时163.630秒，保留首次延迟限制，未重启已预热服务。产品14日20:30日更继续；聊天跟进自动化当前PAUSED，保持暂停。见[本批实际金额、来源与验收](docs/formal-s2-stage5-20261014.md)。

2026-10-13 剩余585项续批：已补齐199项、仍缺386项，固定120只正式核验1534/1920、85只财务输入完整，优先20只320/320保持。当前Docker完整API1631/1631事实通过，63条续批SQL及旧对象保持逐项核对，85桥生产组件实际渲染通过；首次冷读仍1800秒超时，后续查询129秒，耗时限制单独记录。新增事实14日08:45或更晚才可用。今晚日更PUBLISHED，13日实际价格模拟M/E各20笔成交、费用203.14元、净值1,001,325.86元；B110笔、费用556.71元、净值1,002,095.29元。M−B为−769.43元（价格贡献−1,123.00、少付费用+353.57），E−M为零，账本与归因残差为零，已观察1/5日；Docker迁移后已再次逐账核对。当前快照正式S2实际可用225项、11只完整，全局门禁保持；下次日更14日20:30，由Docker scheduler执行。下一批渤海租赁续表边界、春秋航空扫描财报源和真实修订版本已完成只读分组，原文直接采集由Luna负责。见 [财务实际阶段结果](docs/formal-s2-remaining585-20261013.md)及[首日成交净值与归因](docs/remaining585-meb-observation-20261013.md)。

2026-10-13 正式财务补缺收束：原 95 项全部补齐，优先 20 只达到 320/320；其余 100 只的 500 个报告期首轮处理完成，正式接受 1015/1600 项、49 只财务完整，全池合计 1335/1920 项、69 只完整。未通过的身份、修订和核验缺口保留，11 条原始事实未计入通过数。财务读取隔离及单次清单/原文标题加速通过独立复核，新服务冷读 HTTP200 为 258.2 秒，刷新 9.0 秒，生产组件 69 个真实计算桥渲染通过。新增事实最早 14 日 08:45 可用；今晚 20:30 日更和 M/E/B 成交、费用、净值及归因跟进保持。见 [完整实际结果与证据](docs/formal-s2-priority-and-pool-expansion-20261013.md)。

2026-10-13 个人使用流程优化：主要入口收敛为今日、研究、模拟组合、复盘；研究选择接入同版本模拟资格核验，维持不动可保存原判断与理由。未计算状态、草稿复核和确认后的计划恢复已修正；数据诊断、原快照工具和工程对照按需展开。构建、11 项组件交互检查及相关接口/界面契约验证通过；独立 Chrome 最终真实服务和隔离服务五种宽度各 44 项、隔离真实点击 16 项通过，服务冷启动等待单独记录。见 [前端流程优化记录](docs/frontend-user-flow-20261013.md)及[浏览器验收记录](docs/frontend-browser-acceptance-20261013.md)。

2026-10-13 正式 S2 输入续批：复用原报补核 8 个失败证券期间，新增 18 条正式事实；优先 20 只需求覆盖 225/320，完整 120 模拟池为 225/1920，公式财务完整证券从 7 增至 11。逐股真实分母、精确代码映射、原始市值收据重放和有限完整输入排名已接入研究页；旧研究严格重放保持 6,179 个代码和 3,268 个排名。日更新增 Luna 同日收盘市值工具和实际 S2 输入报告，市值失败不阻断 S1/M/E/B；13 日 20:30 的真实采集与成交净值仍待运行，自动跟进已更新。原快照、全局 S2 门禁及前向规格保留。见 [正式 S2 与市值续批记录](docs/formal-s2-market-inputs-20261013.md)。

2026-10-13 财务与归因续批：Luna 补采 92 只证券、三期间共 276 条实际利润记录，模拟池可选财务及有效 TTM 从 28/120 提升至 120/120；新快照保留 12 日行情及 13 日实际 cutoff，旧账本和前向方案不改。20 家五期间的 100 份官方 PDF 已完成核验：28 份全字段通过、34 份部分通过、38 份无字段入库，新增 207 条正式事实。7 只证券公式财务事实完整但待后续 cutoff，正式 S2 仍关闭。12 日逐券/行业归因、现金与净资产桥已上线，残差为零；下一日更及五日 M/E/B 已设置自动跟进。实际数据、核验和剩余范围见 [财务与归因交付记录](docs/financial-attribution-followup-20261013.md)。

2026-10-13 最新候选工程账本已完成 10 月 12 日实际价格模拟：20 笔成交、0 拒单、费用 200.33 元、净资产 986,677.67 元、净收益 −1.332233%，对账通过。行情在 13 日补采，明确标为延迟工程计算。日更恢复后发布 6,172 条报价、3,268 个排名，120 只模拟证据中 115 只合格；M/E/B 已在 13 日开盘前登记连续五交易日观察，当前 0 个观察日，H 等待用户选择与确认。下一更新为 13 日 20:30。实际数字、核验和限制见 [本批交付记录](docs/engineering-forward-trial-20261013.md)。

2026-10-11 原设计续批已完成优先候选官方公告刷新（20 个完整查询、27 份核验原文）、真实四组合暴露/行业集中度/换手/未成交与容量参与率统计，以及实验页原判断和工程草稿的真实读取。新信息日期与旧行情日期分别保留，旧草稿不改写；日更已接公告刷新和非阻断降级。实际数字、来源与下一交易日验证见 [原设计续批记录](docs/original-plan-next-batch-20261011.md)。

2026-10-11 最新研究已接入人工模拟：真实核验 120 只主板输入，116 只通过当前资格；20 券工程草稿已保存，等待 10 月 12 日执行行情。每日工作日 20:30 更新与独立 Windows worker 已恢复。九月 M/E/H/B 真实归档回放及逐券、逐行业会计贡献已接入实验页。当前本机入口为 `http://127.0.0.1:8000/#research`、`#experiments`；启动入口 `tools/start_local_trial.ps1` 沿用已存费率配置。范围、真实试运行数字和未完成验证见 [本批交付记录](docs/latest-simulation-trial-20261011.md)。

2026-10-09 研究候选 v2 已补齐 349 个活跃行业缺项：观察 6,179 个官方代码、取得 6,172 条报价，3,267 只普通工商排名，其中北交所 166 只；活跃报价行业未知降至 0。行业体系、版本与逐代码原文证据分别展示，金融单列新增五项原报告值及同分组、同期间、同报表族比较。原 3,101 个排名代码日线已覆盖研究日，新增 166 个仍缺历史窗口，页面明确标注；研究卡及三股比较可查看财务计算桥、权重贡献、反证、有限公告与日线留痕。范围、缺口及试运行证据见 [S2 研究候选交付记录](docs/s2-structured-delivery.md)。该观察清单独立于现有 S1 快照和模拟资格，不以旧 900 只样本代表全部范围。

## 先跑起来（Docker）

```powershell
docker compose up --build      # 构建并启动
# 打开 http://127.0.0.1:8080
docker compose down            # 停
docker compose down -v         # 停并清空数据（从零开始）
```

单容器同源：前端用相对路径请求 `/api`，因此不需要 CORS，也不需要反向代理。
Compose 把端口绑定到 `127.0.0.1`，只用于本机试运行。
容器**用内置合成夹具自举一份快照**，所以一起来界面就有内容，不需要挂载任何东西——
顶部黄色水印会写明这是虚构示例数据。

> ⚠️ **这不是生产形态。** Compose 显式使用 `AQUANT_IDENTITY_MODE=LOCAL_LOOPBACK_DEMO`，
> 并把 API 端口绑定到宿主机 `127.0.0.1`；API 把 `user:demo` 作为固定的单用户主体标签。
> `X-Aquant-Subject` 仍然是自报 header，不是身份认证、Bearer 令牌或生产凭证。
> 未声明身份模式的 API 会保留开发兼容接线，但 `/api/v1/readiness` 的 `trial.ready` 会被身份门禁阻断。
> 生产形态（API 与前端分开、令牌纪律）见 ADR-013，尚未落实。

身份模式通过 `AQUANT_IDENTITY_MODE` 显式声明。可用值是：

- `LOCAL_LOOPBACK_DEMO`：本机单用户试运行；同时满足部署端口只绑定宿主机 loopback，
  以及固定 `AQUANT_TRIAL_SUBJECT`。容器内的 TCP 对端可能是 bridge 地址，不能把它当作宿主机绑定证明。
- `DEVELOPMENT_SELF_REPORTED`：开发测试用自报 `X-Aquant-Subject`，不具备试运行身份 assurance。

`AQUANT_TRIAL_SUBJECT` 只是主体标签，不是共享秘密，不应被当作前端凭据。API 未设置
`AQUANT_IDENTITY_MODE` 时会报告 `UNDECLARED_SELF_REPORTED`，因此不会把自报 header
误报成可试运行的身份边界。示例配置见 [`configs/api.example.env`](configs/api.example.env)。

想在本机开发 API 指向真实快照，设 `AQUANT_DATA_DIR` 指向发布数据根目录。
不显式设置 `AQUANT_SNAPSHOT_ID` 时，API/worker 会读取该目录的
`current_snapshot.json`；只有回放某个已发布物理快照时才需要显式指定 ID。
真实计划还需要配置券商佣金（见下文「费率」）：

```powershell
$env:AQUANT_DATA_DIR='E:\IT\A股量化交易\deploy\universe-snapshot'
# 可选：回放一个已发布的物理快照；省略时跟随 current_snapshot.json
# $env:AQUANT_SNAPSHOT_ID='snap-eod-2026-09-18-<uuid>'
$env:AQUANT_COMMISSION_RATE='0.00025'        # 你的券商费率，万分之 2.5 写作 0.00025
$env:AQUANT_COMMISSION_MIN_CENTS='500'       # 最低 5 元
$env:AQUANT_IDENTITY_MODE='LOCAL_LOOPBACK_DEMO'
$env:AQUANT_TRIAL_SUBJECT='user:demo'         # 标签，不是共享秘密
$env:PYTHONPATH='src'
# 费率在 API 启动时读取；修改环境变量后必须停止并重新启动 API。
python -m uvicorn main:app --app-dir apps/api --host 127.0.0.1 --port 8000
```

### 用 Docker 读取调度器维护的真实快照

已有本机部署使用已验收的固定版本：

```powershell
& tools/start_current_docker_trial.ps1
```

该入口不重新构建、不迁移或重置数据，启动 API、scheduler 和独立财务核验进程。以下构建与跨系统交接步骤用于首次安装；当前现场继续使用上面的固定版本入口。

基础 `docker-compose.yml` 使用独立命名卷和合成快照。真实试运行必须显式叠加
`docker-compose.real.yml`；覆盖层同时启动 API 与独立 scheduler，并让它们在同一 Linux
运行环境中访问 `/data`。不要让 Windows worker 同时持有这份 SQLite：Windows 与 Linux
容器跨系统并发打开同一个 WAL 文件会产生 `disk I/O error`。

```powershell
if (-not (Test-Path real-trial.env)) { Copy-Item configs/real-trial.example.env real-trial.env }
# 编辑实际配置；已存在的 real-trial.env 不要覆盖。
python tools/docker_runtime_env.py

docker compose --env-file deploy/local-trial/docker-runtime.env `
  -f docker-compose.yml -f docker-compose.real.yml build aquant

# 首次从 Windows 迁移：核验原绑定/进程，停止宿主运行、备份后启动 Docker。
# Docker 已运行时，此命令会拒绝 Windows 再操作它持有的 WAL。
& tools/switch_to_docker_trial.ps1

# 已在 Docker 运行时更新：不重复上面的跨系统交接。
docker compose --env-file deploy/local-trial/docker-runtime.env `
  -f docker-compose.yml -f docker-compose.real.yml up --build -d

Invoke-RestMethod http://127.0.0.1:8080/api/v1/readiness | ConvertTo-Json -Depth 8

# 明确停止真实 Docker 后，才能恢复宿主 worker。
docker compose --env-file deploy/local-trial/docker-runtime.env `
  -f docker-compose.yml -f docker-compose.real.yml down
Enable-ScheduledTask -TaskName 'AQuant Scheduler Worker'
Start-ScheduledTask -TaskName 'AQuant Scheduler Worker'
```

覆盖层要求 `AQUANT_HOST_DATA_DIR` 与 `AQUANT_HOST_PIPELINE_DIR` 已存在，并继续沿用
基础 Compose 的 `127.0.0.1:8080` 绑定。前者保存快照、账本与 worker 心跳，后者保存行情
缓存和流水线留痕。停止容器不会删除宿主机真实数据；不要对这条真实目录使用
`docker compose down -v` 作为清理手段。

未配置佣金时，真实数据仍可用于状态、候选、研究卡和证据等只读接口；
`preview` / `freeze` / `execute` / `value` 会在领域费率闸门返回拒绝，
因为合成费率算出的成交与净值没有依据（§12.6）。要走模拟写链路，必须提供使用者实际券商约定的费率。

## 本地开发（不用 Docker）

```powershell
# --- 终端 1：启动 API（主入口所需） ---
$env:PYTHONPATH='src'
$env:AQUANT_IDENTITY_MODE='LOCAL_LOOPBACK_DEMO'
$env:AQUANT_TRIAL_SUBJECT='user:demo'
python -m uvicorn main:app --app-dir apps/api --host 127.0.0.1 --port 8000

# --- 终端 2：启动前端 ---
cd apps\web; npm install; npm run dev          # http://localhost:5173
```

前端在开发与预览两种模式下都把 `/api` 代理到 `127.0.0.1:8000`（`AQUANT_API_TARGET` 可改）。
默认入口完全读取 API；API 不在线时会明确报错，不会静默回退到夹具或伪造一次成功的冻结。
只有显式访问 `http://localhost:5173/?mode=demo` 才读取 `workspace.json`。使用该模式前运行
`python tools\build_workspace_fixture.py`，页面会显示演示模式水印并禁用写操作。

### 最小本机试运行（合成快照）

先用合成数据确认界面和 API 能启动；这一步不需要数据源、券商佣金或模型密钥。

```powershell
# 终端 1：启动 API
$env:PYTHONPATH='src'
python -m uvicorn main:app --app-dir apps/api --host 127.0.0.1 --port 8000

# 终端 2：检查健康与数据状态，再启动前端
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
Invoke-RestMethod http://127.0.0.1:8000/api/v1/status
Invoke-RestMethod http://127.0.0.1:8000/api/v1/readiness
cd apps\web
npm install
npm run dev                         # http://localhost:5173
```

页面顶部应显示合成数据水印；`/api/v1/status` 能返回 `snap-syn-001`。
`/api/v1/readiness` 会显示 `identity.assurance=LOCAL_SINGLE_USER_LOOPBACK`，
并明确 `productionAuthentication=false`；这条试运行只验证本机链路，
不代表真实数据质量或生产部署已验收。

## 早期开发基线（保留历史记录）

以下状态与 900 只/65 日等数字属于此前开发基线；2026-10-11 的实际研究、模拟及日更状态以顶部交付记录为准。

**研究链路与模拟账本在合成与真实数据上都已跑通并有机器可验的断言；
数据层受免费源限制，还不足以支撑投资结论。**

| 范围 | 状态 | 说明 |
|---|---|---|
| 资料包自检 | ✅ | contracts/schema/configs/examples，裸 Python 可跑 |
| M1 数据与证据底座 | 🟡 | 合成快照 D01–D08 通过；真实 current 快照 900 只 / 65 日行情 / 535 日交易日历已发布 |
| M2 基线与模拟账本 | ✅ | 双快照两日 + 跨进程重启的合成验收 16/16 通过；T+1 真的跨日生效 |
| 产品闭环 | 🟡 | 预览→确认→冻结→执行→估值→对账的合成闭环通过；冻结/已执行计划可在刷新后恢复；真实双快照跨日证据仍待下一交易日形成 |
| 工作台界面 | 🟡 | 四个主要任务入口，研究—模拟—复盘衔接已优化；独立 Chrome 五宽度/真实点击通过，冷启动性能观察见浏览器记录；**界面上的数字有出处检查**（见下文） |
| 研究作业与证据 | ✅ | 作业提交/执行/幂等、模型独立抽取、引用可定位、交叉核对 |
| 每日流水线 | ✅ | 采集→快照→**因子落库**→质量闸门；界面**设置页**可配时间与启停（ADR-014） |
| 研究卡上的因子数值 | 🟡 | 因子落库与卡片读取链已接通；旧 `f10-v1` 因 TTM 公式错误已撤回，须以 `f10-v2` 和决策日总市值重新验收 |
| agentctl 接入 | 🟡 | Q0 成立；唯一只读能力 `aquant.research_card.read` 已接 M1 真实存储（含因子值）；其余七个能力未接 |

**数据层的已知限制（这些不影响流程验证，但影响结论）：**

- 只有 **65 个交易日**，做不了回测；当前仅有 2 个真实快照，历史时点均为抓取后重建
- 行业分类**没有变更历史**，只能标注为 `RECONSTRUCTED`，回答不了历史时点
- 公司行为在真实数据里只有 **2 条**分红记录；代码路径验证过，覆盖率没验证过
- **券商佣金没有权威值**（券商约定），必须由使用者提供；印花税与过户费有权威来源并已留证
- **上市天数门槛（120 个交易日）已接长日历**：行情仍只保存因子所需的 65 日，
  快照另带至少 120 个交易日的权威日历；旧快照仍按其自身短日历披露「不足以判定」

## 验证（全部以退出码为结论）

| 检查 | 命令 | 目的 |
|---|---|---|
| 资料包自检 | `python tests/validate_spec.py` | 契约、示例、SQL、约束 |
| 领域与接口测试 | `pytest tests` | 711 个用例（本次全量通过） |
| 界面交互 + 数字出处 | `python tools/check_ui_flow.py` | 真浏览器点击；每个区块的数字必须说得出出处 |
| 归档字节离线复核 | `python tools/verify_archive.py` | 留证字节与摘要一致 |
| 真实数据闭环 | `python tools/check_real_flow.py` | 真实快照上走完人工点选、差异留痕与模拟账本闭环；条件不足时写 `ENV_NOT_READY` |
| 双侧验收 | `python tools/check_both_sides.py` | **同一套断言**在合成与真实上各跑一遍 |
| 因子落库 | `python tools/compute_factors.py` | 结果带特征版本并回读；`f10-v1` 结果只供审计，不能沿用旧 832/900 结论 |
| 数字出处（单跑） | `node apps/web/tools/check_number_provenance.mjs --url …` | 见下文 |
| HEAD 可复现性 | `python tools/check_reproducible.py` | 干净导出后全量测试仍通过 |

联网的脚本需要放行本机透明代理网段（`AQUANT_TRUSTED_PROXY_NETWORKS`，见 ADR-002）。
全部只做只读抓取，不下单、不连券商。

各类数据对 S1、F10/S2、模拟账本和 AI 分析的必要性、缺失时的降级边界及当前
主备来源，见 [`docs/data-requirement-matrix.md`](docs/data-requirement-matrix.md)。

## 数据流水线

```powershell
# 全市场采集（首次，可续跑）
python tools\collect_universe.py --start 2026-06-22 --end 2026-09-18
# 上市日期（逐只查 BaoStock ipoDate，只查研究池里的几百只）
python tools\collect_listing_dates.py --write
# 从采集结果重建研究池（会**保留**已采到的上市日期）
python tools\build_pool_from_universe.py --size 300 --write
# 每个交易日：采集 -> 快照 -> 因子落库 -> 质量闸门（并发锁 + 休市判断）
python tools\daily_run.py --skip-if-done
python tools\show_alerts.py --days 7      # 有 ERROR 时退出码 1
```

日常生产路径由 `daily_run.py` 串起，生产发布步骤实际调用
`tools/publish_universe_snapshot.py`，每次生成新的唯一物理 ID。S1 行情门槛通过后
即可原子切换 `deploy/universe-snapshot/current_snapshot.json`；F10 缺失或失败会
标记降级并告警，不再拖停价格型 S1。旧物理目录与 `meta.sqlite` 不会被覆盖。

需要手工发布或回放时，先延迟切换 current，记下命令输出的 `snapshot_id`，再完成后续闸门：

```powershell
python tools\collect_market_caps.py --as-of 2026-09-18
python tools\publish_universe_snapshot.py `
  --data-dir deploy\universe-snapshot `
  --cache deploy\agentctl-q0\universe-bars.json `
  --pool configs\real-pool-csrc.yaml `
  --financials deploy\agentctl-q0\financials-cache.json `
  --actions deploy\agentctl-q0\dividend-actions.json `
  --market-caps deploy\agentctl-q0\decision-market-caps.json `
  --check-market-caps `
  --window-start 2026-06-22 `
  --window-end 2026-09-18 `
  --defer-promotion

$snapshotId='<上一步输出的 snap-eod-...>'
python tools\compute_factors.py `
  --snapshot-dir deploy\universe-snapshot `
  --snapshot-id $snapshotId `
  --json-out deploy\agentctl-q0\factors-persist.json
python -m tests.integration.t12_f10_real
python tools\promote_snapshot.py `
  --data-dir deploy\universe-snapshot `
  --snapshot-id $snapshotId `
  --require-factors
```

### 每天自动跑：界面配置 + 独立 worker（ADR-014）

在界面**设置**页填时间与解释器并保存，然后让 worker 长驻：

```powershell
python tools\scheduler_worker.py            # 到点自动跑，也执行界面的「立刻运行一次」
python tools\scheduler_worker.py --once     # 只处理一轮（cron / 排查）
python tools\scheduler_worker.py --now      # 立刻跑一次
```

Windows 上建议把长驻 worker 注册成带恢复触发器的计划任务：

```powershell
& tools\install_scheduler_task.ps1 `
  -PythonPath "E:\IT\Agent\.venv\Scripts\python.exe" `
  -DataDir "E:\IT\A股量化交易\deploy\universe-snapshot"
```

该脚本会先验证解释器可导入 `baostock` 与 `pytest`，再注册登录启动和异常退出后的
5 分钟恢复检查；worker 存活时不会重复启动，也不会改变界面中配置的实际采集时间。

**worker 不在跑时，界面上的配置不会让任何东西自动跑**——这句话写在设置页上。
界面上的「立刻运行一次」只登记请求，执行仍由 worker 做，因此点按钮与到点自动跑
走的是同一条路径（并发锁、休市判断、留痕、告警都在那条路上）。
调度默认**停用**，启用时必须填解释器：默认 python 没装 baostock，
用错的失败信息看起来像数据源坏了。

**因子必须落库，否则研究卡上没有数值。** `tools/compute_factors.py` 走的是产品
自己的落库路径（与 `POST /api/v1/research/jobs` 的因子作业同一条代码路径），
写 `research_run` 与 `feature_value`：

```powershell
python tools\compute_factors.py `
  --snapshot-dir deploy\universe-snapshot `
  --snapshot-id $snapshotId `
  --json-out deploy\agentctl-q0\factors-persist.json
# 旧 f10-v1 的 832/900 结果已撤回；修正公式与决策日总市值输入后重新验收，
# 不再沿用旧版本的覆盖率或排除数量。
```

`tests/integration/t12_f10_real` 仍然跑，但它的角色是**质量闸门**（值域、亏损股
是否被截断为 0、覆盖率），不是"算因子"。二者曾经被合成一步：流水线每天报成功，
而 `research_run` 一直是 0 行。

### 采集缓存 v3 与升级注意事项

采集器当前要求 `rows_format=3`，每个行情行还包括
`adjusted_close_cents`。采集器遇到仍含旧 `rows_format=2` 行的标的时，会对该标的按目标
窗口做一次全量重采，并在成功后写回 v3；之后才恢复增量续跑。升级期间不要用
`--prev-close-only` 跳过这次重采；网络中断会保留旧行并记入 `failed`，下次继续补齐。
不需要为了这个升级手工删除缓存，只有确实要重抓全部数据时才使用 `--refresh`。

### 两类用上市日期的判定

| 判定 | 依据 | 长日历的作用 |
|---|---|---|
| 决策时点是否已上市 | `listed_on` 与决策日比较 | 可判（与窗口无关） |
| 上市未满 120 个**交易日** | 快照内独立交易日历计数 | 上市日在日历范围内精确计算；早于日历起点且日历已覆盖 120 日时可由单调性判定达标 |

旧快照或降级回放若没有足够长的日历，窗口外标的仍不会被误排除，且预览会在
`notes` 明示「不足以判定」；严格生产发布会拒绝少于 120 个交易日的日历。

调度接入、退出码语义、运行限制与升级注意事项见 **`docs/daily-pipeline.md`**。

分红金额以**整数微元**（10⁻⁶ 元）记账：真实分红常常不是整数分
（茅台 2025 年度每股 28.02423 元），按分存储只能截断，误差随股数放大。

## 费率（§12.6）

费率分两类，不能混为一谈：

| 项 | 取值 | 性质 |
|---|---|---|
| 印花税（卖出） | 0.0005 | 法定——财政部/税务总局公告 2023 年第 39 号 |
| 过户费（双向） | 0.00001 | 行业标准——中国结算通知，2022-04-29 起 |
| **券商佣金** | **无权威值** | **合同约定**，必须由使用者提供 |

```powershell
python tools\fetch_fee_sources.py    # 抓取并留证（URL + 时间 + 内容哈希）
```

合成费率**不得**用于真实数据：`preview` / `freeze` / `execute` / `value` 四个入口都会拦。
真实快照 + 未配置佣金时 API 仍可启动并提供只读接口；只有依赖费用的模拟与估值入口拒绝。
配置费率是放行这些入口的唯一方式，不能用开关绕过。

### 真实 S1 试运行怎样录入和复核佣金

佣金是券商与客户的合同参数，没有统一的公开权威值；优先由使用者从自己的券商约定中填写。费率用小数比例表示，
例如万分之 2.5 写成 `0.00025`；最低佣金用整数分表示，5 元写成 `500`。两个环境变量必须同时设置，
缺少任意一个时仍只能读真实数据，`trial.ready` 不会放行真实模拟写链路，也不会把缺失的最低佣金当成 0：

```powershell
$env:AQUANT_COMMISSION_RATE='0.00025'
$env:AQUANT_COMMISSION_MIN_CENTS='500'
# 已运行的 API 不会热加载；停止旧进程后，用同一组环境变量重新启动 API。
```

无法立即取得合同费率时，只有在使用者明确接受的情况下，才可把万 2.5、最低 5 元作为试运行假设，并额外设置：

```powershell
$env:AQUANT_COMMISSION_SOURCE='USER_APPROVED_ASSUMPTION'
```

该来源可以放行本地试运行费用计算，但界面会持续提示它不代表真实合同或账单。取得实际费率后，替换两个数值并把
`AQUANT_COMMISSION_SOURCE` 改为 `USER_CONFIGURED`。不设置来源时，为兼容既有部署，成对填写的数值仍按
`USER_CONFIGURED` 处理。

重启后用 API 回读实际生效的费率和来源：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/fees | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:8000/api/v1/readiness | ConvertTo-Json -Depth 8
```

真实 S1 试运行的硬门槛是 `readiness.trial.ready=true`，而不是顶层 `ready` 或
`trial` 之外的 `data.readiness=READY`。合同费率回读应显示 `commissionSource=USER_CONFIGURED`；明确批准的临时
假设显示 `commissionSource=USER_APPROVED_ASSUMPTION`。两者都必须同时满足 `syntheticTestRate=false` 并回显
实际生效的 `commissionRate` 和 `commissionMinCents`；
`commissionSource=UNCONFIGURED_DEFAULT` 表示示例假设，不能当作券商费率。`provenance` 会把印花税和过户费列为
有出处的规则，把佣金列为 `CONTRACTUAL`（没有权威来源）；它证明的是来源分类，不替用户证明合同数字。

当前可编辑位置是仓库根目录、不会提交的 `real-trial.env`；修改后重建 API 容器。后续设置页将提供同一组字段的可视化
编辑入口。运行状态抽屉会区分 `USER_CONFIGURED`、`USER_APPROVED_ASSUMPTION` 与
`UNCONFIGURED_DEFAULT`。`python tools\check_real_flow.py` 也要求两个数值成对出现；
不完整时只做只读与时点检查并明确跳过真实写路径。

决策快照除了 `asOfTime` 必须早于执行日，还必须在执行日 09:30（北京时间）之前已经实际发布。
事后补发的历史重建快照会保留 `RECONSTRUCTED` 水印，可用于工程复核，但不会被 `trial.ready`
或 `tools/check_real_flow.py` 当成当时可用的决策证据。

## 界面上的数字从哪来

每个区块用 `data-source` 声明来源（`apps/web/src/components/ui.tsx`）：

| 声明 | 含义 | 检查 |
|---|---|---|
| `api` | 数字来自服务端响应 | 带小数的金额**必须全部**能在响应里找到 |
| `fixture` | 随前端分发的示例数据 | 必须带演示标注（区块内或页面级水印） |
| `static` | 常量文案 | 不查数字 |
| `noNumericValue` | 本区块没有业务数字 | **声明必须属实**——出现金额形状即报红 |

为什么需要它：组合页曾经整块「我的模拟草稿」显示的都是夹具数字，
而徽章写着「API 在线」——页面根本没请求过预览接口。
夹具的数字看起来完全正常，这不是肉眼能发现的问题。

**改界面时必读：新增区块必须声明 `dataSource`，否则检查会报红。**
声明为 `api` 的区块，其金额必须真的来自服务端——不能拿夹具的数字填空。

## 工作台

四个主要任务入口（主文档 §5.2）：**今日 / 研究 / 模拟组合 / 复盘**。数据诊断与高级工具、每日更新设置在「数据与设置」入口。
视图可深链：`/#today`、`/#research`、`/#portfolio`、`/#experiments`；旧工具深链 `/#workspace`、`/#settings` 继续有效。

### 写链路

| 步骤 | 接口 | 说明 |
|---|---|---|
| 预览 | `POST /api/v1/plans/preview` | 只算不冻；订单只用执行日**之前**的价格；可用 `selected_instrument_ids` 提交当次 S1 候选子集 |
| 取令牌 | `POST /api/v1/plans/{id}/confirmation` | 服务端签发，绑定主体/计划/快照/账户版本/预览哈希 |
| 冻结 | `POST /api/v1/plans/{id}/freeze` | 五项复核；令牌一次性消费；成功后写入与计划绑定的决策日志 |
| 恢复 | `GET /api/v1/plans?portfolio_id=...` | 返回冻结/已执行计划及双快照绑定；页面刷新后可继续执行、估值和对账 |
| 执行 | `POST /api/v1/plans/{id}/execute` | 按 §12 规则模拟成交 |
| 估值 | `POST /api/v1/valuations` | 强制使用请求中的已发布 EOD `snapshot_id`，保存快照、执行计划和 `as_of`；不变量失败则不发布净值 |
| 对账 | `GET /api/v1/portfolios/{id}/reconcile` | 逐项核验订单、费用、现金、批次 |

真实生产计划必须显式绑定两份已发布且不同的物理快照：
`decision_snapshot_id`（决策截止时间必须早于执行日开盘）和
`execution_snapshot_id`（必须是执行日收盘快照）。单快照兼容路径只用于合成演示；
冻结时会把两份快照及各自截止时间持久化，之后切换 `current_snapshot.json` 不会改写已冻结计划。
人工计划不会让客户端提交任意证券：服务端会用决策快照重新计算 S1，并拒绝不在该候选集内或重复的标的。
冻结后，模型原方案、人工最终方案与客观差异分别留档；直接接受模型方案记为 `ACCEPT_MODEL`，
点选后方案发生变化记为 `MODIFY_MODEL`。

### 研究作业与证据

| 步骤 | 接口 |
|---|---|
| 提交作业 | `POST /api/v1/research/jobs`（只入队；幂等键由服务端算） |
| 执行作业 | `POST /api/v1/research/jobs/{id}/run`（生产由独立 worker 调用 `run_pending()`） |
| 研究卡 | `GET /api/v1/instruments/{id}/research`（按标的/快照/交易日**留档**，重复打开不刷新） |
| 证据 | `GET /api/v1/instruments/{id}/evidence`（引用带字符偏移，可定位） |
| 助手 | `POST /api/v1/assistant/messages`（材料先过外发闸门；只给草稿，不执行） |
| 助手回放 | `POST /api/v1/assistant/replay`（仅同一受信任主体映射、同一固定输入可读取已归档输出；回放单独登记） |
| 模型审计 | `GET /api/v1/assistant/calls`（按受信任主体映射返回成功、拒绝与失败调用） |
| 费率口径 | `GET /api/v1/fees`（当前费率与出处） |

**安全边界**：确认主体由服务端从受信任凭证取得，**不接受请求体自报**；
模型外发只有一条路径且先过闸门（未登记来源、未声明个人信息一律整批拒绝，**没有绕过开关**）；
API 中没有下单、写账本、Shell、SQL 或任意抓取路径（有测试断言路由表里不存在）。

### 界面遵循的硬约束（都不是文案约定，而是数据形状）

- **不出现任何概率或预期收益字段**；排名一律标注为「横截面排名百分位」。
- **不出现综合评分**（无 `totalScore` 之类字段）。
- **不可模拟时显示规则、适用日期与修复方法**，不显示「失败」。
- **未接入的后端能力明确显示未接入**，不用占位数字冒充。
- 风险状态一律「颜色 + 文字」双通道；虚构数据全程带水印横幅。

## 检查界面

```powershell
python tools\check_ui_flow.py            # 交互 + 数字出处（推荐）
node apps\web\tools\check_user_flow.mjs  # 组件交互与状态检查，不启动浏览器
cd apps\web
node tools\screenshot.mjs --url http://127.0.0.1:8080 --out ../../deploy/screens
node tools\page_dump.mjs --url http://127.0.0.1:8080 --tab portfolio   # 页面请求了哪些 URL
```

`page_dump` 回答的是「页面上的数字从哪来」——截图回答不了这个。

## 文档

**阅读顺序：** 主规格 → **修订记录** → 实施状态 → 专题文档。

| 文件 | 性质 |
|---|---|
| `A-Quant-Lab_开发文档_v0.2.md` | **主规格**：需求、技术架构、研究规范、验收、阶段 |
| `docs/spec-revisions-2026-09.md` | **主规格定点修订**：哪一段被取代、取代它的是什么、依据在哪。**冲突时以它为准** |
| `A-Quant-Lab_v0.2_结构化摘要.md` | 主规格结构化导读（含章节行号索引） |
| `docs/implementation-baseline.md` | 任务表 + **§8 实施状态**（每条都指向可复核的证据） |
| `docs/daily-pipeline.md` | 每日流水线：调度接入、退出码、告警、已知限制 |
| `docs/data-rights-register.md` | 数据权利登记表 + 费率来源 |
| `docs/adr/` | 架构决策记录（ADR-001…014） |
| `docs/integration/q0-readiness-report.md` | Q0 接入就绪报告（本机实测） |

## 目录

```text
apps/web/                         工作台前端（React + TS + Vite，浅色主题）
apps/api/                         工作台 API（FastAPI；容器里同时提供前端静态文件）
src/aquant/domain/data/           证券、日历、行情、快照、前向归档、研究读取
src/aquant/domain/evidence/       引用定位与证据落库
src/aquant/domain/ai/             模型窄接口、外发闸门、证据抽取
src/aquant/domain/simulation/     费用、模拟器、公司行为、板块规则
src/aquant/domain/portfolio/      组合构建、日终估值、计划生命周期
src/aquant/application/           视图模型、研究卡留档、助手编排
src/aquant/operations/            作业机制、每日流水线的锁与留痕、告警
src/aquant/adapters/              数据源守卫与降级链、模型提供方
contracts/  schema/  configs/  examples/    资料包
tests/{pit,golden,integration,security,api,acceptance}/
tools/                            采集、建快照、验收、每日流水线
docs/                             基线、ADR、数据权利、流水线说明
```

**验收口径**（基线 §5）：结论须标注 L1 现象 / L2 行为 / L3 推断；
探针必须用退出码表达结论；测试接线本身是安全前提，必须断言而非假定。

## Q0 复核

```powershell
git -C E:\IT\Agent log -1 --format='%H %cI %s'
agentctl serve --config deploy\agentctl-q0\runtime.config.yaml --host 127.0.0.1 --port 8765 --token <TOKEN>
python tests\acceptance\q0_probe.py --base-url http://127.0.0.1:8765 --token <TOKEN> \
  --json-out deploy\agentctl-q0\q0-probe-evidence.json
```

**部署纪律**：静态 `--token` 是 master 凭证，不得下发到产品；部署一律用 `--token-env`。
模型密钥仅在后端读取，不进入前端、日志或仓库。
