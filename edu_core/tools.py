"""Deterministic checks used before (or instead of) a model: answer keys, numbers with units,
algebraic equivalence and chemical-equation balance. Student strings are never eval'd.

Each check returns True / False when it can decide and None when it cannot (the model decides then).
"""

import ast
import re
import unicodedata
from collections import Counter
from fractions import Fraction

import sympy as sp

from edu_core.text import normalize_answer

# ---------- answer keys ----------

TRUE_WORDS = {'对', '正确', '√', '✓', 't', 'true', '是', 'yes', 'y', 'r', 'right'}
FALSE_WORDS = {'错', '错误', '×', '✗', 'f', 'false', '否', 'no', 'n', 'wrong'}


def choice_letters(text: str) -> set[str]:
    text = unicodedata.normalize('NFKC', text or '').upper()
    # "BD", "B、D", "选 B,D" -> {B, D}; letters inside English words are not choices.
    compact = re.sub(r'[^A-Z]', '', re.sub(r'[一-鿿]', ' ', text))
    if compact and re.fullmatch(r'[A-H]+', compact) and len(compact) <= 8:
        return set(compact)
    return set(re.findall(r'(?<![A-Z])[A-H](?![A-Z])', text))


def choice_from_option_text(answer: str, options: list[str]) -> set[str] | None:
    """Letters of the options whose full text the answer reproduces (students sometimes copy the option).

    Only used when the answer is essentially option text; a bare letter answer returns None.
    """
    got = normalize_answer(answer)
    if not got or not options or re.fullmatch(r'[a-h]+', got):
        return None
    texts = [normalize_answer(o) for o in options]
    exact = {'ABCDEFGH'[i] for i, t in enumerate(texts) if t and t == got}
    if exact:
        return exact
    contained = {'ABCDEFGH'[i] for i, t in enumerate(texts) if len(t) >= 4 and t in got}
    return contained if len(contained) == 1 else None


def match_choice(answer: str, key: str) -> bool | None:
    got, want = choice_letters(answer), choice_letters(key)
    if not want or not got:
        return None
    return got == want


def match_true_false(answer: str, key: str) -> bool | None:
    def polarity(text):
        t = normalize_answer(text)
        if t in TRUE_WORDS:
            return True
        if t in FALSE_WORDS:
            return False
        return None

    got, want = polarity(answer), polarity(key)
    if got is None or want is None:
        return None
    return got == want


# ---------- numbers and units ----------

UNIT_FACTORS = {
    # length (m)
    'km': ('m', 1000),
    'm': ('m', 1),
    'dm': ('m', 0.1),
    'cm': ('m', 0.01),
    'mm': ('m', 0.001),
    '千米': ('m', 1000),
    '米': ('m', 1),
    '厘米': ('m', 0.01),
    '毫米': ('m', 0.001),
    # time (s)
    'h': ('s', 3600),
    'min': ('s', 60),
    's': ('s', 1),
    'ms': ('s', 0.001),
    '小时': ('s', 3600),
    '分钟': ('s', 60),
    '秒': ('s', 1),
    # mass (kg)
    't': ('kg', 1000),
    'kg': ('kg', 1),
    'g': ('kg', 0.001),
    'mg': ('kg', 1e-6),
    '千克': ('kg', 1),
    '克': ('kg', 0.001),
    # volume (m3)
    'l': ('m3', 0.001),
    'ml': ('m3', 1e-6),
    '升': ('m3', 0.001),
    '毫升': ('m3', 1e-6),
    # others (base only)
    'n': ('N', 1),
    'j': ('J', 1),
    'kj': ('J', 1000),
    'w': ('W', 1),
    'kw': ('W', 1000),
    'pa': ('Pa', 1),
    'kpa': ('Pa', 1000),
    'v': ('V', 1),
    'a': ('A', 1),
    'ω': ('Ohm', 1),
    'mol': ('mol', 1),
    'm/s': ('m/s', 1),
    'km/h': ('m/s', 1 / 3.6),
    'mol/l': ('mol/m3', 1000),
    '%': ('%', 1),
}

NUMBER = r'[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:\s*[/／]\s*\d+(?:\.\d+)?)?(?:[eE][-+]?\d+)?'
QUANTITY = re.compile(
    rf'({NUMBER})\s*((?:km/h|m/s|mol/L|[a-zA-Zμ%Ω]+|千米|米|厘米|毫米|小时|分钟|秒|千克|克|升|毫升)?)'
)


def _number(text: str) -> float:
    text = text.replace('／', '/').replace(' ', '')
    if '/' in text and 'e' not in text.lower():
        return float(Fraction(text))
    return float(text)


def quantities(text: str) -> list[tuple[float, str]]:
    text = unicodedata.normalize('NFKC', text or '').replace('×10^', 'e').replace('x10^', 'e')
    out = []
    for value, unit in QUANTITY.findall(text):
        try:
            out.append((_number(value), unit.strip()))
        except (ValueError, ZeroDivisionError):
            continue
    return out


def to_base(value: float, unit: str):
    if not unit:
        return value, ''
    base = UNIT_FACTORS.get(unit.lower()) or UNIT_FACTORS.get(unit)
    if base is None:
        return None
    return value * base[1], base[0]


def numeric_equal(got: tuple[float, str], want: tuple[float, str], tol: float = 1e-3) -> bool | None:
    a, b = to_base(*got), to_base(*want)
    if a is None or b is None:
        return None
    if want[1] and got[1] and a[1] != b[1]:
        return False
    if want[1] and not got[1]:
        # A bare number cannot prove the requested unit; compare the value in the expected unit only.
        a = (got[0] * (to_base(1, want[1])[0]), b[1])
    scale = max(abs(b[0]), 1e-12)
    return abs(a[0] - b[0]) <= tol * scale + 1e-12


def check_numeric(answer: str, expected: str, tol: float = 1e-3) -> tuple[bool | None, str]:
    """Is the expected quantity the final answer? Looks at the last sentence first.

    Returns (decision, evidence sentence). True only if a final-sentence number matches; False only if
    the final sentence has numbers, none matches, and the expected value appears nowhere in the answer.
    """
    want_list = quantities(expected)
    if not want_list:
        return None, ''
    want = want_list[0]
    lines = [s for s in re.split(r'[\n。；;]', answer or '') if s.strip()]
    if not lines:
        return None, ''
    last = lines[-1].strip()
    final = quantities(last)
    if any(numeric_equal(q, want, tol) for q in final):
        return True, last
    anywhere = any(numeric_equal(q, want, tol) for q in quantities(answer))
    if final and not anywhere:
        return False, last
    return None, ''


# ---------- algebra ----------


def expression(text: str):
    """Bounded arithmetic/polynomial parser over single-letter variables (migrated from harness.grading.algebra)."""
    text = unicodedata.normalize('NFKC', text or '').strip()
    text = text.replace('×', '*').replace('÷', '/').replace('−', '-').replace('^', '**')
    if not text or len(text) > 200 or not re.fullmatch(r'[0-9a-z.+*/()\-\s]+', text):
        raise ValueError('Unsupported expression')
    text = re.sub(r'(\d|[a-z]|\))(?=[a-z]|\()', r'\1*', text)
    tree = ast.parse(text, mode='eval')
    if len(list(ast.walk(tree))) > 120:
        raise ValueError('Expression too complex')

    def visit(n):
        if isinstance(n, ast.Constant) and type(n.value) in (int, float) and abs(n.value) <= 10**6:
            return sp.Rational(str(n.value))
        if isinstance(n, ast.Name) and len(n.id) == 1:
            return sp.Symbol(n.id)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.UAdd | ast.USub):
            return visit(n.operand) * (-1 if isinstance(n.op, ast.USub) else 1)
        if isinstance(n, ast.BinOp):
            a, b = visit(n.left), visit(n.right)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            if isinstance(n.op, ast.Div) and b != 0:
                return a / b
            if isinstance(n.op, ast.Pow) and b.is_Integer and 0 <= b <= 6:
                return a**b
        raise ValueError('Unsupported syntax')

    return sp.simplify(visit(tree.body))


def algebra_equal(answer: str, expected: str) -> bool | None:
    try:
        a = expression(answer.split('=')[-1])
        b = expression(expected.split('=')[-1])
    except (ValueError, TypeError, SyntaxError, ZeroDivisionError, RecursionError):
        return None
    return bool(sp.simplify(a - b) == 0)


# ---------- chemistry ----------

SUBSCRIPTS = str.maketrans('₀₁₂₃₄₅₆₇₈₉', '0123456789')


def _formula_atoms(formula: str) -> Counter:
    """Atom counts of one formula, e.g. Ca(OH)2 -> {Ca:1, O:2, H:2}; hydrates split at '·'."""
    formula = formula.translate(SUBSCRIPTS)
    formula = re.sub(r'[↑↓]|\((?:s|l|g|aq)\)', '', formula).replace(' ', '')
    total = Counter()
    for part in formula.split('·'):
        m = re.match(r'^(\d*)(.*)$', part)
        mult = int(m.group(1) or 1)
        atoms, pos = _group(m.group(2), 0)
        if pos != len(m.group(2)):
            return Counter()
        for k, v in atoms.items():
            total[k] += v * mult
    return total


def _group(text: str, pos: int):
    atoms = Counter()
    while pos < len(text):
        ch = text[pos]
        if ch in '([':
            inner, pos = _group(text, pos + 1)
            if pos >= len(text) or text[pos] not in ')]':
                return atoms, -1
            pos += 1
            count, pos = _count(text, pos)
            for k, v in inner.items():
                atoms[k] += v * count
        elif ch in ')]':
            return atoms, pos
        elif ch.isupper():
            m = re.match(r'[A-Z][a-z]?', text[pos:])
            pos += len(m.group(0))
            count, pos = _count(text, pos)
            atoms[m.group(0)] += count
        else:
            return atoms, -1
    return atoms, pos


def _count(text: str, pos: int):
    m = re.match(r'\d+', text[pos:])
    return (int(m.group(0)), pos + len(m.group(0))) if m else (1, pos)


def _side(text: str) -> Counter | None:
    total = Counter()
    for term in re.split(r'\s+\+\s+|\+(?=[A-Z0-9(])', text.strip()):
        term = term.strip()
        if not term:
            continue
        m = re.match(r'^(\d+)?\s*(.+)$', term)
        coef = int(m.group(1) or 1)
        atoms = _formula_atoms(m.group(2))
        if not atoms:
            return None
        for k, v in atoms.items():
            total[k] += v * coef
    return total


def chem_balanced(equation: str) -> bool | None:
    """True if both sides have the same atoms; None if the text is not a parseable equation."""
    text = unicodedata.normalize('NFKC', equation or '')
    # Reaction conditions written over the arrow (e.g. "=点燃=") are dropped.
    text = re.sub(r'=+[^=A-Z]*?=+', '=', text)
    parts = [p for p in re.split(r'==|=|→|⟶|->|⇌', text) if p.strip()]
    if len(parts) != 2:
        return None
    left, right = (_side(p) for p in parts)
    if not left or not right:
        return None
    return left == right
