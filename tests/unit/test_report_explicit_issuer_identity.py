from aquant.adapters.providers.cninfo_report_identity import (
    explicit_issuer_company_names, extra_a_share_code_evidence,
    profile_security_code_evidence,
    compound_report_identity,
)
from datetime import date


def test_a_share_company_label_is_not_b_share_or_investment_number():
    lines=[(1,["公司 A 股代码：600841 公司 A 股简称：动力新科",
               "公司 B 股代码：900920 公司 B 股简称：动力 B股"])]
    assert extra_a_share_code_evidence(lines,"600841")==((1,lines[0][1][0]),)
    assert not extra_a_share_code_evidence(lines,"900920")
    assert not extra_a_share_code_evidence([(2,["投资标的600841股份有限公司"])],"600841")


def test_stock_brief_needs_explicit_header_and_unique_a_share_code():
    header="股票种类 股票上市交易所 股票简称 股票代码 变更前股票简称"
    row="A股 上海证券交易所 水星家纺 603365 /"
    assert extra_a_share_code_evidence([(4,[header,row])],"603365")==((4,header),(4,row))
    assert not extra_a_share_code_evidence([(4,[row])],"603365")
    assert not extra_a_share_code_evidence([(4,[header,"B股 上海证券交易所 水星家纺 603365 /"])],"603365")
    assert not extra_a_share_code_evidence([(4,[header,row+" 600841"])],"603365")
    assert not extra_a_share_code_evidence([(4,[header,"A股上海证券交易所水星家纺1603365/"])],"603365")


def test_issuer_company_info_is_exact_legal_name_not_subsidiary_narrative():
    lines=[(7,["公司的中文名称 青海盐湖工业股份有限公司", "公司投资西藏天路股份有限公司"])]
    assert explicit_issuer_company_names(lines)==((7,lines[0][1][0],"青海盐湖工业股份有限公司"),)
    assert not explicit_issuer_company_names([(7,["主营公司投资西藏天路股份有限公司"])])
    assert explicit_issuer_company_names([(4,["公司的中文名称", "上海水星家用纺织品股份有限公司"])])[0][2]=="上海水星家用纺织品股份有限公司"


def test_split_code_cell_requires_the_named_issuer_company_profile():
    page=(14,["第二节 公司简介和主要财务指标","一、公司信息","股票代码",
              "002460（A股）；","01772（H股）","公司的中文名称 江西赣锋锂业集团股份有限公司"])
    found=profile_security_code_evidence([page],"002460",company="江西赣锋锂业集团股份有限公司")
    assert found and (14,"股票代码") in found and (14,"002460（A股）；") in found
    assert not profile_security_code_evidence([page],"002460",company="其他股份有限公司")
    assert not profile_security_code_evidence([(30,["投资标的","股票代码","002460（A股）；"])],"002460")
    assert not profile_security_code_evidence([(14,["公司简介","股票代码","002460（B股）",
              "公司的中文名称 江西赣锋锂业集团股份有限公司"])],"002460")


def test_compound_report_identity_keeps_original_page_line_chain_and_rejects_wrong_cover():
    profile=(8,["公司信息","公司的中文名称 申通快递股份有限公司","股票代码 002468"])
    cover=(1,["申通快递 2025 年度报告 1"])
    chain=compound_report_identity([cover,profile],"002468","申通快递股份有限公司",date(2025,12,31))
    assert chain and chain[1]==((1,cover[1][0]),) and (8,profile[1][1]) in chain[0]
    assert not compound_report_identity([cover,profile],"002468","申通快递股份有限公司",date(2024,12,31))
    assert not compound_report_identity([(1,["其他股份有限公司 2025年度报告"]),profile],"002468","申通快递股份有限公司",date(2025,12,31))
    assert not compound_report_identity([cover,profile],"600841","申通快递股份有限公司",date(2025,12,31))
    assert not compound_report_identity([(1,["申通快递 2025年度报告摘要"]),profile],"002468","申通快递股份有限公司",date(2025,12,31))


def test_profile_heading_late_in_native_reading_order_remains_explicit_same_page():
    page=(8,["股票代码 002468","公司的中文名称 申通快递股份有限公司"]+
              ["公司信息联系地址与外文简称"]*20+["一、公司信息"])
    assert profile_security_code_evidence([page],"002468",company="申通快递股份有限公司")
    narrative=(8,["股票代码 002468","公司的中文名称 申通快递股份有限公司","叙述公司信息见其他年度报告"])
    assert not profile_security_code_evidence([narrative],"002468",company="申通快递股份有限公司")
