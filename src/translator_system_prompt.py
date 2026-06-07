"""
Translator Lambda system prompt.

외국인 노동자 커뮤니티 게시글/댓글의 다국어 번역에 특화된 시스템 프롬프트.
챗봇과는 별개 Lambda 이며, 입출력 계약이 단순(1회 InvokeModel) 하므로
JSON 응답을 강제한다.
"""

# 지원 언어 코드 → (영문 언어명, 자국어 표기)
# system_prompt.py 의 _LANG_NAMES 와 동일 정책 (프론트 SETTING_LANGUAGES 와 동기화).
_LANG_NAMES: dict[str, tuple[str, str]] = {
    "ko": ("Korean", "한국어"),
    "en": ("English", "English"),
    "vi": ("Vietnamese", "Tiếng Việt"),
    "fil": ("Filipino", "Filipino"),
    "th": ("Thai", "ไทย"),
    "id": ("Indonesian", "Bahasa Indonesia"),
    "tl": ("Tagalog", "Tagalog"),
}

SUPPORTED_LANGS: set[str] = set(_LANG_NAMES.keys())


def lang_display(code: str) -> str:
    """언어 코드 → '영문명(자국어)' 표기. 미지원 코드는 코드 그대로 반환."""
    english, native = _LANG_NAMES.get(code, (code, code))
    if english == native:
        return english
    return f"{english} ({native})"


def build_translator_system_prompt(source_lang: str, target_lang: str, kind: str) -> str:
    """번역 Lambda 시스템 프롬프트.

    Args:
        source_lang: 원문 언어 코드 (ko/en/vi/fil ...)
        target_lang: 번역 대상 언어 코드
        kind: "post" (제목 + 본문) 또는 "comment" (본문만)

    Returns:
        시스템 프롬프트 문자열. JSON 응답을 강제하고, 게시글/댓글 톤·서식 유지 규칙을 박는다.
    """
    src = lang_display(source_lang)
    tgt = lang_display(target_lang)

    has_title = kind == "post"

    schema_hint = (
        '{"title": "<번역된 제목>", "content": "<번역된 본문>"}'
        if has_title
        else '{"title": null, "content": "<번역된 본문>"}'
    )

    return (
        "당신은 외국인 노동자 커뮤니티 게시글·댓글을 정확하게 번역하는 전문 번역기입니다.\n"
        f"원문 언어: {src}\n"
        f"번역 대상 언어: {tgt}\n"
        f"입력 종류: {'게시글(제목+본문)' if has_title else '댓글(본문만)'}\n\n"
        "## 번역 규칙\n"
        "- 원문의 의미를 보존하고, 누락·과장·임의 요약 금지.\n"
        "- 원문의 톤(존댓말/캐주얼/욕설/이모지 등)을 그대로 유지합니다.\n"
        "- URL, 이메일, 숫자, 날짜, 통화 단위, 해시태그(@user, #tag) 는 원본 그대로 둡니다.\n"
        "- 코드블록(```...```) 안의 내용은 절대 번역하지 않고 원본 그대로 둡니다.\n"
        "- 한국 고유명사(기관명·지명·법령명)는 대상 언어로 표기하되, 필요 시 한국어 원문을 괄호로 병기합니다.\n"
        "- 줄바꿈(\\n) 구조는 가능한 한 유지합니다.\n"
        "- 원문과 대상 언어가 같다면 원문을 그대로 반환합니다(이미 호출 전에 분기되지만 안전장치).\n\n"
        "## 응답 형식 (반드시 준수)\n"
        f"- JSON 객체 1개만 응답합니다. 형식: {schema_hint}\n"
        + (
            "- 댓글 번역에서는 title 을 반드시 null 로 둡니다.\n"
            if not has_title
            else "- 게시글 번역에서는 title 에 번역된 제목 문자열을 넣습니다.\n"
        )
        + "- JSON 외의 설명·머리말·꼬리말·코드블록 표기(```json 등)는 절대 출력하지 않습니다.\n"
        "- 응답 본문은 순수 JSON 문자열이어야 합니다.\n"
    )
