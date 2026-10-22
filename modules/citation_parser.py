import difflib
import html
import re
from modules.config import CIRCLED_TO_ARABIC, ARABIC_TO_CIRCLED

def to_circled_hang(s: str) -> str:
    """아라비아 숫자 항(제1항, 1항 등)을 원문자 항(제①항, ①항 등)으로 변환"""
    def _repl(m):
        prefix = m.group(1) or ""
        num = m.group(2)
        suffix = m.group(3) or ""
        if num in ARABIC_TO_CIRCLED:
            return f"{prefix}{ARABIC_TO_CIRCLED[num]}{suffix}"
        return m.group(0)
    return re.sub(r"(제\s*)?(\d{1,2})(\s*항)", _repl, s)

def to_arabic_hang(s: str) -> str:
    """원문자 항(제①항, ①항 등)을 아라비아 숫자 항(제1항, 1항 등)으로 변환"""
    def _repl(m):
        prefix = m.group(1) or ""
        char = m.group(2)
        suffix = m.group(3) or ""
        return f"{prefix}{CIRCLED_TO_ARABIC.get(char, char)}{suffix}"
    return re.sub(r"(제\s*)?([①-⑳])(\s*항)?", _repl, s)

def normalize_art_input(text: str) -> str:
    """조항 번호 입력 정규화 (예: '20조' -> '제20조')"""
    s = text.strip() if text else ""
    if re.match(r"^\d+조", s):
        s = f"제{s}"
    return s

def get_hang_variants(text: str) -> list:
    """입력된 조항/키워드에 대해 원문자(①)/숫자(1), 띄어쓰기 유무 등 가능한 모든 동등 표현 생성"""
    if not text:
        return []
    s = normalize_art_input(text)
    raw_list = [s]
    ar = to_arabic_hang(s)
    ci = to_circled_hang(s)
    raw_list.extend([ar, ci])
    
    expanded = []
    for item in raw_list:
        expanded.append(item)
        nospace = re.sub(r"\s+", "", item)
        expanded.append(nospace)
        m = re.search(r"(제\d+조(?:의\d+)?)(제?[①-⑳\d]+항.*)", nospace)
        if m:
            expanded.append(f"{m.group(1)} {m.group(2)}")
            
    for item in list(expanded):
        if re.match(r"^제([①-⑳\d]+항)$", item):
            expanded.append(re.sub(r"^제", "", item))
        elif re.match(r"^([①-⑳\d]+항)$", item):
            expanded.append(f"제{item}")
            
    seen = set()
    result = []
    for item in expanded:
        clean = item.strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result

def get_citation_terms(target_reg: str, target_art: str):
    """관심 규정 및 조항에 대한 내부/파트너/타 규정 인용 검색어(동등 표현 포함) 생성"""
    clean_art = normalize_art_input(target_art)
    variants = get_hang_variants(clean_art)
    if not variants:
        variants = [clean_art]
        
    is_rule = "시행세칙" in target_reg
    partner_reg_name = target_reg.replace(" 시행세칙", "").replace("시행세칙", "").strip() if is_rule else f"{target_reg} 시행세칙"
    
    terms_internal = list(variants)
    
    terms_partner = []
    prefix_p = "세칙" if is_rule else "규정"
    for v in variants:
        terms_partner.extend([f"{prefix_p} {v}", f"{prefix_p}{v}"])
        
    terms_external = []
    for v in variants:
        terms_external.extend([
            f"「{target_reg}」 {v}",
            f"「{target_reg}」{v}",
            f"{target_reg} {v}",
            f"{target_reg}{v}",
        ])
    short_name = target_reg.split()[-1]
    if len(short_name) >= 3 and short_name != target_reg:
        for v in variants:
            terms_external.extend([f"{short_name} {v}", f"{short_name}{v}"])
            
    def _dedup(l):
        seen = set()
        return [x for x in l if not (x in seen or seen.add(x))]
        
    return clean_art, partner_reg_name, _dedup(terms_internal), _dedup(terms_partner), _dedup(terms_external)

def highlight_terms(text: str, terms: list, color: str = "red") -> str:
    """텍스트 내 일치하는 검색어들을 지정 색상 마크다운으로 하이라이트 (긴 구문 우선 매칭)"""
    if not text or not terms:
        return text or ""
    sorted_terms = sorted(set(terms), key=len, reverse=True)
    pattern = re.compile("(" + "|".join(re.escape(t) for t in sorted_terms) + ")")
    return pattern.sub(rf":{color}[**\1**]", text)

def is_external_prefixed(content: str, start: int, is_rule: bool) -> bool:
    """
    content 내 start 위치에서 시작하는 조항 표현(term)이
    내부 조항이 아닌 외부 법규/상위 규정(또는 하위 세칙)에 종속된 인용인지 판별.
    """
    chunk = content[max(0, start - 150):start]
    parts = re.split(r'[\.\n;]', chunk)
    before = parts[-1]
    
    if is_rule:
        if re.search(r'(?:이\s*|동\s*|당해\s*|해당\s*)?[가-힣]*(?:규정|법|영|시행령|규칙)(?:」|』|\"|\')?\s*$', before):
            return True
        m = re.search(r'(?:이\s*|동\s*|당해\s*|해당\s*)?[가-힣]*(?:규정|법|영|시행령|규칙)(?:」|』|\"|\')?\s+(.+)$', before)
        if m:
            between = m.group(1)
            if re.match(r'^(?:제\s*[\d①-⑳가-힣호목조항의\s]+(?:,|및|·|부터|~|\s+및\s+)\s*)+$', between):
                return True
    else:
        if re.search(r'(?:이\s*|동\s*|당해\s*|해당\s*)?[가-힣]*(?:시행세칙|세칙|규칙|법|영|시행령)(?:」|』|\"|\')?\s*$', before):
            return True
        m = re.search(r'(?:이\s*|동\s*|당해\s*|해당\s*)?[가-힣]*(?:시행세칙|세칙|규칙|법|영|시행령)(?:」|』|\"|\')?\s+(.+)$', before)
        if m:
            between = m.group(1)
            if re.match(r'^(?:제\s*[\d①-⑳가-힣호목조항의\s]+(?:,|및|·|부터|~|\s+및\s+)\s*)+$', between):
                return True

    return False

# =========================================================
# 신구대조표 (Inline Diff 시각화)
# =========================================================
def generate_inline_diff_html(old_text: str, new_text: str) -> str:
    """이전 텍스트와 현재 텍스트의 어절/단어 단위 변경 사항을 시각화하는 인라인 Diff HTML 생성"""
    if not old_text and not new_text:
        return ""
    if not old_text:
        return f'<ins style="background-color: #e6ffed; color: #22863a; text-decoration: none; font-weight: bold; padding: 2px 4px; border-radius: 3px;">{html.escape(new_text)}</ins>'
    if not new_text:
        return f'<del style="background-color: #ffeef0; color: #cb2431; text-decoration: line-through; padding: 2px 4px; border-radius: 3px;">{html.escape(old_text)}</del>'

    old_tokens = re.findall(r'\S+|\s+', old_text)
    new_tokens = re.findall(r'\S+|\s+', new_text)

    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens)
    result = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        old_chunk = "".join(old_tokens[i1:i2])
        new_chunk = "".join(new_tokens[j1:j2])

        if tag == 'equal':
            result.append(html.escape(old_chunk))
        elif tag == 'delete':
            esc_old = html.escape(old_chunk)
            result.append(f'<del style="background-color: #ffeef0; color: #cb2431; text-decoration: line-through; padding: 2px 4px; border-radius: 3px; margin: 0 1px;">{esc_old}</del>')
        elif tag == 'insert':
            esc_new = html.escape(new_chunk)
            result.append(f'<ins style="background-color: #e6ffed; color: #22863a; text-decoration: none; font-weight: bold; padding: 2px 4px; border-radius: 3px; margin: 0 1px;">{esc_new}</ins>')
        elif tag == 'replace':
            esc_old = html.escape(old_chunk)
            esc_new = html.escape(new_chunk)
            result.append(f'<del style="background-color: #ffeef0; color: #cb2431; text-decoration: line-through; padding: 2px 4px; border-radius: 3px; margin: 0 1px;">{esc_old}</del>')
            result.append(f'<ins style="background-color: #e6ffed; color: #22863a; text-decoration: none; font-weight: bold; padding: 2px 4px; border-radius: 3px; margin: 0 1px;">{esc_new}</ins>')

    return "".join(result)

# =========================================================
# 조항 인용 파서 (범위 인용 및 대명사 문맥 해석 확장)
# =========================================================
def parse_clause_citations(text: str):
    """
    본문 텍스트 내에서 모든 조항 언급과 이를 지배하는 규정/법령 접두어를 순서대로 추출.
    - 조문 범위 인용(예: '제23조부터 제27조까지', '제5조 내지 제8조') 인식
    - 대명사 인용('같은 조', '동조', '같은 항', '동항', '전항') 자동 문맥 복원
    - 연속 열거(예: '규정 제6조제2항, 제25조 및 ...') 시 선행 접두어 지배 상속
    - 새로운 법규명 접두어 등장 시 전환
    - 문장 종결 또는 서술어 등장 시 단절
    반환값: list of dict(
        'clause': 정규화된 조항 문자열 (예: '제6조제2항', '제25조', '제23조~제27조'),
        'raw': 원문 조항 표기,
        'span': (start, end),
        'gov_prefix': 지배 접두어 문자열 (예: '규정', '이 세칙', '「유가증권시장 업무규정」', None),
        'range_info': dict(start=(조, 조의), end=(조, 조의)) or None
    )
    """
    if not text:
        return []

    # 복합 패턴: 범위 / 일반 조항 / 단독 항 / 같은조(동조) / 같은항(동항) / 전항
    pattern = re.compile(
        r'(?P<range>제\s*(?P<r_s1>\d+)(?:의\s*(?P<r_s2>\d+))?\s*조\s*(?:부터|~|내지)\s*제\s*(?P<r_e1>\d+)(?:의\s*(?P<r_e2>\d+))?\s*조\s*(?:까지)?)'
        r'|(?P<clause>제\s*\d+(?:의\s*\d+)?\s*조(?:\s*제?\s*[①-⑳\d]+\s*항)?(?:\s*제?\s*\d+\s*호)?(?:\s*[가-하]\s*목)?)'
        r'|(?P<hang_only>제\s*[①-⑳\d]+\s*항(?:\s*제?\s*\d+\s*호)?(?:\s*[가-하]\s*목)?)'
        r'|(?P<same_art>(?:같은\s*조|동조)(?:\s*제?\s*[①-⑳\d]+\s*항)?(?:\s*제?\s*\d+\s*호)?(?:\s*[가-하]\s*목)?)'
        r'|(?P<same_hang>(?:같은\s*항|동항)(?:\s*제?\s*\d+\s*호)?(?:\s*[가-하]\s*목)?)'
        r'|(?P<prev_hang>전항(?:\s*제?\s*\d+\s*호)?(?:\s*[가-하]\s*목)?)'
    )

    prefix_pattern = re.compile(
        r'(?:「[^」]+」|『[^』]+』|"[^"]+"|'
        r'(?:(?<=[^\w가-힣])|^)(?:이|동|당해|해당|본)?\s*(?:시행세칙|세칙|규정|규칙|법률|법|시행령|영))'
    )
    continuation_pattern = re.compile(r'^[\s,·~및또는내지부터]+$')

    matches = list(pattern.finditer(text))
    citations = []

    last_article = None
    last_hang = None

    for i, m in enumerate(matches):
        start, end = m.span()
        raw = m.group(0)

        prev_end = matches[i - 1].end() if i > 0 else 0
        between = text[prev_end:start]

        p_matches = list(prefix_pattern.finditer(between))
        gov_prefix = None

        if p_matches:
            last_p = p_matches[-1]
            p_text = last_p.group().strip()
            after_p = between[last_p.end():]

            # after_p에 단절 요인이 있는지 검사
            if not re.search(r'[\.\n;]|\b(?:을|를|은|는|이|가|에서|에게|위반|경우|인하여)\b|에\s*따라|에\s*의하', after_p):
                gov_prefix = p_text

        if gov_prefix is None:
            clean_between = between.strip()
            if clean_between and continuation_pattern.match(clean_between) and i > 0 and citations:
                gov_prefix = citations[-1]['gov_prefix']
            else:
                gov_prefix = None

        range_info = None
        clause_str = raw.replace(" ", "")

        # 1. 조문 범위 인용인 경우 (예: 제23조부터 제27조까지)
        if m.group('range'):
            s1 = int(m.group('r_s1'))
            s2 = int(m.group('r_s2')) if m.group('r_s2') else 0
            e1 = int(m.group('r_e1'))
            e2 = int(m.group('r_e2')) if m.group('r_e2') else 0
            raw_start = f"제{s1}조" if s2 == 0 else f"제{s1}조의{s2}"
            raw_end = f"제{e1}조" if e2 == 0 else f"제{e1}조의{e2}"
            range_info = {
                "start": (s1, s2),
                "end": (e1, e2),
                "raw_start": raw_start,
                "raw_end": raw_end
            }
            clause_str = f"{raw_start}~{raw_end}"
            last_article = raw_end
            last_hang = None

        # 2. '같은 조' 또는 '동조' 대명사 복원
        elif m.group('same_art'):
            sub_part = re.sub(r'^(?:같은\s*조|동조)\s*', '', raw).replace(" ", "")
            if last_article:
                clause_str = f"{last_article}{sub_part}"
                m_h = re.search(r'제?[①-⑳\d]+항', sub_part)
                if m_h:
                    last_hang = m_h.group(0)

        # 3. '같은 항' 또는 '동항' 대명사 복원
        elif m.group('same_hang'):
            sub_part = re.sub(r'^(?:같은\s*항|동항)\s*', '', raw).replace(" ", "")
            if last_article and last_hang:
                clause_str = f"{last_article}{last_hang}{sub_part}"
            elif last_article:
                clause_str = f"{last_article}{sub_part}"

        # 4. '전항' 대명사 복원
        elif m.group('prev_hang'):
            sub_part = re.sub(r'^전항\s*', '', raw).replace(" ", "")
            prev_h = "제1항"
            if last_hang:
                m_num = re.search(r'\d+', last_hang)
                if m_num:
                    val = max(1, int(m_num.group(0)) - 1)
                    prev_h = f"제{val}항"
            if last_article:
                clause_str = f"{last_article}{prev_h}{sub_part}"

        # 5. 일반 조항
        else:
            m_art = re.search(r'제\s*\d+(?:의\s*\d+)?\s*조', raw)
            if m_art:
                last_article = m_art.group(0).replace(" ", "")
                m_h = re.search(r'제?[①-⑳\d]+\s*항', raw)
                last_hang = m_h.group(0).replace(" ", "") if m_h else None
            elif m.group('hang_only') and last_article:
                clause_str = f"{last_article}{clause_str}"
                m_h = re.search(r'제?[①-⑳\d]+\s*항', raw)
                last_hang = m_h.group(0).replace(" ", "") if m_h else None

        citations.append({
            'clause': clause_str,
            'raw': raw,
            'span': (start, end),
            'gov_prefix': gov_prefix,
            'range_info': range_info
        })

    return citations

def _parse_art_num_key(art_str: str):
    """'제25조' 또는 '제25조의2'에서 (조, 조의) 숫자 튜플 추출"""
    m = re.search(r'제\s*(\d+)(?:의\s*(\d+))?조', art_str)
    if m:
        main = int(m.group(1))
        sub = int(m.group(2)) if m.group(2) else 0
        return (main, sub)
    return None

def match_article(clause_item, target_variants: list) -> bool:
    """
    본문에서 추출된 조항 표기(문자열 또는 dict)가 검색 대상 조항 변형(target_variants)에 부합하는지 판별.
    - 단일 조항 일치 검사
    - 조문 범위 인용(예: '제23조부터 제27조까지')에 대상 조항이 포함되는지 여부 검사
    """
    if isinstance(clause_item, dict):
        clause_str = clause_item.get('clause', '')
        range_info = clause_item.get('range_info')
    else:
        clause_str = str(clause_item)
        range_info = None

    norm_clause = normalize_art_input(clause_str)
    clause_hang_variants = get_hang_variants(norm_clause) or [norm_clause]

    for tv in target_variants:
        norm_tv = normalize_art_input(tv)

        # 1. 기본 일치 검사
        if any(v == norm_tv for v in clause_hang_variants):
            return True

        if re.match(r'^제\d+조$', norm_tv):
            if re.match(rf'^{norm_tv}(?:제?[①-⑳\d]+항)?(?:제?\d+호)?(?:[가-하]목)?$', norm_clause):
                return True

        # 2. 범위 인용(제A조~제B조) 포함 검사
        if range_info:
            target_key = _parse_art_num_key(norm_tv)
            if target_key:
                s_key = range_info['start']
                e_key = range_info['end']
                if s_key <= target_key <= e_key:
                    return True

    return False

def classify_reference(curr_reg: str, target_reg: str, partner_reg_name: str, gov_prefix: str):
    """
    인용 조항의 지배 접두어(gov_prefix)와 현재 규정(curr_reg), 대상 규정(target_reg)을 기반으로
    내부 참조('internal'), 파트너 참조('partner'), 타 규정 참조('external'), 또는 'ignore' 반환.
    """
    is_curr_rule = "시행세칙" in curr_reg or "세칙" in curr_reg or "규칙" in curr_reg
    is_target_rule = "시행세칙" in target_reg or "세칙" in target_reg or "규칙" in target_reg

    # 1. 내부 참조: curr_reg == target_reg
    if curr_reg == target_reg:
        if gov_prefix is None:
            return "internal"
        p_clean = gov_prefix.replace("「", "").replace("」", "").replace("“", "").replace("”", "").replace('"', '').strip()
        if p_clean in [target_reg, f"이 {p_clean.split()[-1]}", f"본 {p_clean.split()[-1]}", f"동 {p_clean.split()[-1]}",
                       "이 규정", "본 규정", "동 규정", "이 세칙", "본 세칙", "동 세칙", "이 규칙", "본 규칙"]:
            return "internal"
        return "ignore"

    # 2. 파트너 규정 참조: partner_reg_name in curr_reg
    elif partner_reg_name in curr_reg:
        if gov_prefix is None:
            return "ignore"
        p_clean = gov_prefix.replace("「", "").replace("」", "").replace("“", "").replace("”", "").replace('"', '').strip()
        
        if not is_target_rule and is_curr_rule:
            short_target = target_reg.split()[-1]
            if p_clean in ["규정", short_target, target_reg, f"이 {short_target}", f"당해 {short_target}"]:
                return "partner"
        elif is_target_rule and not is_curr_rule:
            short_target = target_reg.split()[-1]
            if p_clean in ["세칙", "시행세칙", short_target, target_reg]:
                return "partner"
        return "ignore"

    # 3. 타 규정 참조: 제3의 규정
    else:
        if gov_prefix is None:
            return "ignore"
        p_clean = gov_prefix.replace("「", "").replace("」", "").replace("“", "").replace("”", "").replace('"', '').strip()
        if p_clean == target_reg or (len(target_reg) >= 4 and target_reg in p_clean):
            return "external"
        return "ignore"

def find_valid_internal_matches(content: str, terms: list, is_rule: bool):
    """
    content 내에서 terms(조항 표현 목록)가 등장하는 위치들 중,
    상위 '규정' 등의 외부 접두어가 붙지 않은 '순수 내부 참조' 위치[(start, end), ...]를 반환.
    """
    if not content or not terms:
        return []
    sorted_terms = sorted(set(terms), key=len, reverse=True)
    pattern = re.compile("(" + "|".join(re.escape(t) for t in sorted_terms) + ")")
    valid_spans = []
    for m in pattern.finditer(content):
        start, end = m.span()
        if not is_external_prefixed(content, start, is_rule):
            valid_spans.append((start, end))
    return valid_spans

def highlight_spans(text: str, spans: list, color: str = "red") -> str:
    """주어진 인덱스 구간(spans)들만 지정 색상 마크다운으로 하이라이트"""
    if not text or not spans:
        return text or ""
    sorted_spans = sorted(set(spans), key=lambda x: (x[0], -x[1]))
    merged = []
    for s, e in sorted_spans:
        if not merged:
            merged.append([s, e])
        else:
            prev = merged[-1]
            if s <= prev[1]:
                prev[1] = max(prev[1], e)
            else:
                merged.append([s, e])
    res = text
    for start, end in reversed(merged):
        res = res[:start] + f":{color}[**{res[start:end]}**]" + res[end:]
    return res

# =========================================================
# 정방향 참조 (Outgoing Citations) 추출기
# =========================================================
def extract_outgoing_citations(content: str, curr_reg: str = "") -> list:
    """
    관심 조항 본문 텍스트 내에서 인용하고 있는 외부 법령, 상/하위 규정, 내부 조항 등
    정방향 인용(Outgoing References)을 추출하여 구조화된 목록으로 반환.
    """
    if not content:
        return []
    citations = parse_clause_citations(content)
    outgoing = []
    seen = set()

    for c in citations:
        prefix = c['gov_prefix'] or "자체 내부"
        clause = c['clause']
        label = f"{prefix} {clause}".strip()
        if label not in seen:
            seen.add(label)
            outgoing.append({
                "prefix": prefix,
                "clause": clause,
                "raw": c['raw'],
                "full_label": label,
                "span": c['span']
            })
    return outgoing
