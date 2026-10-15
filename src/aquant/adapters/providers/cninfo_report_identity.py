"""Explicit issuer-stock identity rows from original front-matter text only."""
import re
from aquant.domain.fundamentals.disclosure_link import report_period_from_title

IDENTITY_READER_VERSION = "explicit-a-share-compound-profile-v3"
IDENTITY_PAGE_LIMIT = 20


def extra_a_share_code_evidence(page_lines, code):
    """Read A-share labels or the issuer's stock-brief header/row, never any code hit.

    Callers retain their existing direct-label evidence first. This fallback
    cannot reinterpret investment holdings, an H/B-share row, or an unlabelled
    six-digit number as the requested issuer's A-share security code.
    """
    if not re.fullmatch(r"\d{6}",code): return ()
    found=[]
    for page, lines in page_lines:
        for index,line in enumerate(lines):
            compact=re.sub(r"\s+","",line)
            if re.search(rf"公司A股代码[:：]?{code}(?!\d)",compact):
                found.append((page,line))
                continue
            # The original row must be an A-share row in the immediately
            # preceding explicit issuer-stock table, not a narrative mention.
            if not re.match(r"^A股(?:上海|深圳|北京)",compact): continue
            if re.findall(r"(?<!\d)\d{6}(?!\d)",compact) != [code]: continue
            previous=lines[max(0,index-2):index]
            headers=[x for x in previous if
                re.search(r"(?:股票|证券)种类",re.sub(r"\s+","",x)) and
                re.search(r"(?:股票|证券)代码",re.sub(r"\s+","",x)) and
                re.search(r"(?:股票|证券)简称",re.sub(r"\s+","",x))]
            if len(headers)==1:
                found.extend(((page,headers[0]),(page,line)))
    return tuple(found)


def explicit_issuer_company_names(page_lines):
    """Legal issuer names from exact company-info labels, no alias matching."""
    result=[]
    name=r"[\u4e00-\u9fffA-Za-z（）()]+(?:股份有限公司|股份公司|有限公司)"
    label=r"(?:公司的中文名称|公司中文名称|公司名称)(?:[（(]如有[）)])?[:：]?"
    for page,lines in page_lines:
        for index,line in enumerate(lines):
            compact=re.sub(r"\s+","",line)
            match=re.fullmatch(label+f"({name})",compact)
            if match:
                result.append((page,line,match[1]));continue
            if re.fullmatch(label,compact) and index+1<len(lines):
                next_line=lines[index+1]
                match=re.fullmatch(f"({name})",re.sub(r"\s+","",next_line))
                if match:result.append((page,line+'\n'+next_line,match[1]))
    return tuple(result)


def profile_security_code_evidence(page_lines, code, *, company=None):
    """Adjacent label/value cells on the issuer's explicit company-info page.

    Long annual front matter can place this page beyond page12. A page must
    identify itself as company information and explicitly name the issuer;
    code labels elsewhere in the report cannot satisfy this fallback.
    """
    if not re.fullmatch(r"\d{6}",code):return ()
    result=[]
    for page,lines in page_lines:
        compact_lines=[re.sub(r"\s+","",x) for x in lines]
        if not any(re.fullmatch(
                r"(?:(?:第[一二三四五六七八九十\d]+[节章]|[一二三四五六七八九十\d]+[、.]))?"
                r"公司(?:简介(?:和主要财务指标)?|信息)",x) for x in compact_lines):continue
        names=explicit_issuer_company_names([(page,lines)])
        declared={row[2] for row in names}
        if len(declared)!=1 or (company is not None and company not in declared):continue
        for index,line in enumerate(compact_lines):
            if not re.search(r"(?:股票代码|证券代码|公司代码)",line):continue
            candidates=[(index,lines[index])]
            if not re.search(rf"(?<!\d){code}(?!\d)",line):
                for next_index in range(index+1,min(index+4,len(lines))):
                    next_line=compact_lines[next_index]
                    if re.search(r"(?:股票上市|公司的|公司中文|注册|办公|联系)",next_line):break
                    candidates.append((next_index,lines[next_index]))
            matches=[(i,text) for i,text in candidates if
                     re.findall(r"(?<!\d)\d{6}(?!\d)",re.sub(r"\s+","",text))==[code]]
            if len(matches)!=1:continue
            i,text=matches[0]
            compact_value=re.sub(r"\s+","",text)
            # Explicit B/H tags cannot be silently treated as A-share codes.
            if re.search(rf"{code}[(（][BH]股[)）]",compact_value):continue
            result.extend([(page,names[0][1]),(page,lines[index])])
            if i!=index:result.append((page,text))
    return tuple(dict.fromkeys(result))


def compound_report_identity(page_lines, code, company, period_end):
    """Exact report-title + legal company-info + A-share code source chain.

    A branded/image cover is allowed only through these explicit cells from
    the same original PDF. An explicit different issuer or report period in
    the first three pages remains a rejection, never an alias inference.
    """
    pages=[(p,lines) for p,lines in page_lines if p<=IDENTITY_PAGE_LIMIT]
    names=explicit_issuer_company_names(pages)
    if {x[2] for x in names}!={company}:return None
    code_rows=profile_security_code_evidence(pages,code,company=company) or extra_a_share_code_evidence(pages,code)
    if not code_rows:return None
    titles=[]
    for page,lines in pages:
        if page>3:continue
        for line in lines:
            compact=re.sub(r"\s+","",line)
            if re.search(r"(?:股份有限公司|股份公司|有限公司)",compact) and company not in compact:
                return None
            # Page furniture may share the physical text line. The retained
            # evidence is the untouched original line, not this recognition key.
            key=re.sub(r"\s+\d+(?:\s*/\s*\d+)?\s*$","",line.split('|',1)[0]).strip()
            declared=report_period_from_title(key)
            if declared is None:continue
            if declared!=period_end:return None
            titles.append((page,line))
    if not titles:return None
    issuer=tuple(dict.fromkeys([(p,line) for p,line,_ in names]+list(code_rows)))
    return issuer,tuple(titles)
