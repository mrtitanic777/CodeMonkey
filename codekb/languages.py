"""The languages themselves: keywords by version, and features a later version added that code can be
checked for without compiling it. From the ISO C and C++ standards (keywords are closed lists).
"""
import re

C_KEYWORDS = {
    "c89": "auto break case char const continue default do double else enum extern float for goto if int "
           "long register return short signed sizeof static struct switch typedef union unsigned void "
           "volatile while".split(),
    "c99": "inline restrict _Bool _Complex _Imaginary".split(),
    "c11": "_Alignas _Alignof _Atomic _Generic _Noreturn _Static_assert _Thread_local".split(),
    "c17": [],
    "c23": "alignas alignof bool constexpr false nullptr static_assert thread_local true typeof "
           "typeof_unqual _BitInt _Decimal32 _Decimal64 _Decimal128".split(),
}

CPP_KEYWORDS = {
    "c++98": "and and_eq asm auto bitand bitor bool break case catch char class compl const const_cast "
             "continue default delete do double dynamic_cast else enum explicit export extern false float "
             "for friend goto if inline int long mutable namespace new not not_eq operator or or_eq private "
             "protected public register reinterpret_cast return short signed sizeof static static_cast "
             "struct switch template this throw true try typedef typeid typename union unsigned using "
             "virtual void volatile wchar_t while xor xor_eq".split(),
    "c++03": [],
    "c++11": "alignas alignof char16_t char32_t constexpr decltype noexcept nullptr static_assert "
             "thread_local".split(),
    "c++14": [],
    "c++17": [],
    "c++20": "char8_t concept consteval constinit co_await co_return co_yield requires".split(),
    "c++23": [],
}


def keyword_since(word, language):
    table = C_KEYWORDS if language == "c" else CPP_KEYWORDS
    for version, words in table.items():
        if word in words:
            return version
    return None


def all_keywords(language):
    table = C_KEYWORDS if language == "c" else CPP_KEYWORDS
    return {w for words in table.values() for w in words}


# Features a version introduced, found by their shape in code (comments and strings already blanked).
# (name, language, version that introduced it, pattern, the C++11 feature name a platform may list)
FEATURES = [
    ("line comments (//)", "c", "c99", None, None),     # checked on the raw text
    ("declaration after a statement", "c", "c99", None, None),
    ("a declaration in a for loop", "c", "c99", re.compile(r"\bfor\s*\(\s*(?:const\s+)?(?:unsigned\s+|signed\s+)?"
                                                       r"(?:int|char|long|short|size_t|float|double|[A-Z]\w*)\s+\w+\s*="), None),
    ("long long", "c", "c99", re.compile(r"\blong\s+long\b"), None),
    ("designated initializers", "c", "c99", re.compile(r"[{,]\s*\.[A-Za-z_]\w*\s*="), None),
    ("auto type deduction", "c++", "c++11", re.compile(r"\bauto\s+[A-Za-z_*&]"), "auto"),
    ("range-based for", "c++", "c++11", re.compile(r"\bfor\s*\([^;()]*\w\s*:\s*[^:]"), "range-based for"),
    ("lambda", "c++", "c++11", re.compile(r"\[[=&]?[\w\s,&=]*\]\s*\([^)]*\)\s*(?:mutable\s*)?(?:->[^{]*)?\{"), "lambda"),
    ("nullptr", "c++", "c++11", re.compile(r"\bnullptr\b"), "nullptr"),
    ("enum class", "c++", "c++11", re.compile(r"\benum\s+(?:class|struct)\b"), "enum class"),
    ("constexpr", "c++", "c++11", re.compile(r"\bconstexpr\b"), "constexpr"),
    ("noexcept", "c++", "c++11", re.compile(r"\bnoexcept\b"), "noexcept"),
    ("override and final keywords", "c++", "c++11", re.compile(r"\)\s*(?:const\s*)?(?:override|final)\b"),
     "override and final keywords"),
    ("= default / = delete", "c++", "c++11", re.compile(r"\)\s*=\s*(?:default|delete)\s*;"), "= default / = delete"),
    ("variadic templates", "c++", "c++11", re.compile(r"template\s*<[^>]*\.\.\."), "variadic templates"),
    ("alias templates (using X = ...)", "c++", "c++11", re.compile(r"\busing\s+[A-Za-z_]\w*\s*="), "alias templates (using X = ...)"),
    ("static_assert", "c++", "c++11", re.compile(r"\bstatic_assert\s*\("), "static_assert"),
    ("structured bindings", "c++", "c++17", re.compile(r"\bauto\s*&{0,2}\s*\[[^\]]+\]\s*="), None),
    ("if constexpr", "c++", "c++17", re.compile(r"\bif\s+constexpr\b"), None),
    ("concepts / requires", "c++", "c++20", re.compile(r"\b(?:concept|requires)\b"), None),
    ("coroutines", "c++", "c++20", re.compile(r"\bco_(?:await|return|yield)\b"), None),
]


def blank(code):
    """Comments and string contents blanked, newlines kept."""
    def b(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    code = re.sub(r"/\*.*?\*/|//[^\n]*", b, code, flags=re.S)
    return re.sub(r'"(?:\\.|[^"\\\n])*"' + r"|'(?:\\.|[^'\\\n])*'",
                  lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2) + m.group(0)[-1], code)
