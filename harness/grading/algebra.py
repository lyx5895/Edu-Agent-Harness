"""Bounded arithmetic parser: never eval/sympify student strings."""
import ast
import re
import sympy as sp

X = sp.Symbol('x')


def expression(text):
    text = text.strip().replace('×', '*').replace('÷', '/').replace('−', '-').replace('^', '**')
    if not text or len(text) > 160 or not re.fullmatch(r'[0-9x.+*/()\-\s]+', text):
        raise ValueError('Unsupported expression')
    text = re.sub(r'(\d|x|\))(?=x|\()', r'\1*', text)
    tree = ast.parse(text, mode='eval')
    if len(list(ast.walk(tree))) > 80:
        raise ValueError('Expression too complex')

    def visit(n):
        if isinstance(n, ast.Constant) and type(n.value) in (int, float) and abs(n.value) <= 100000:
            return sp.Rational(str(n.value))
        if isinstance(n, ast.Name) and n.id == 'x':
            return X
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            return visit(n.operand) * (-1 if isinstance(n.op, ast.USub) else 1)
        if isinstance(n, ast.BinOp):
            a, b = visit(n.left), visit(n.right)
            if isinstance(n.op, ast.Add): return a+b
            if isinstance(n.op, ast.Sub): return a-b
            if isinstance(n.op, ast.Mult): return a*b
            if isinstance(n.op, ast.Div) and b.is_number and b != 0: return a/b
            if isinstance(n.op, ast.Pow) and b.is_Integer and 0 <= b <= 2: return a**b
        raise ValueError('Unsupported syntax')
    value = visit(tree.body)
    if sp.Poly(value, X).degree() > 2:
        raise ValueError('Polynomial degree too high')
    return sp.expand(value)


def equation_solution(text):
    if text.count('=') != 1: raise ValueError('Expected equation')
    a, b = text.split('=')
    p = sp.Poly(expression(a)-expression(b), X)
    if p.degree() != 1: raise ValueError('Expected linear equation')
    return -p.nth(0)/p.nth(1)


def verify(item, text):
    """Return correct/incorrect/incomplete/unknown and first material error."""
    try:
        lines = [s.strip() for s in re.split(r'[\n;；→]', text.strip()) if s.strip()]
        if not lines: raise ValueError('Empty work')
        if len(lines) > 20: raise ValueError('Too many steps')
        if item['kind'] == 'equation':
            target = equation_solution(item['initial'])
            for i, line in enumerate(lines):
                if equation_solution(line) != target:
                    return {'status': 'incorrect', 'first_error': i+1, 'reason': '这一步改变了原方程的解。', 'source': 'sympy'}
            last = lines[-1].replace(' ', '')
            a, b = last.split('=')
            complete = (a == 'x' and expression(b).is_number) or (b == 'x' and expression(a).is_number)
        else:
            target = expression(item['initial'])
            for i, line in enumerate(lines):
                for part in line.split('='):
                    if expression(part) != target:
                        return {'status': 'incorrect', 'first_error': i+1, 'reason': '这一步与原式不等价。', 'source': 'sympy'}
            last = lines[-1].split('=')[-1].replace(' ', '')
            # A returned original expression is not evidence of completing simplification.
            complete = '(' not in last and ')' not in last and last.count('x') <= 1
        return {'status': 'correct' if complete else 'incomplete', 'first_error': None,
                'reason': '各步等价，已完成。' if complete else '目前步骤等价，请继续完成化简或求解。', 'source': 'sympy'}
    except (ValueError, TypeError, SyntaxError, ZeroDivisionError, RecursionError, sp.PolynomialError):
        return {'status': 'unknown', 'first_error': None, 'reason': '需要按评分规则检查文字说明。', 'source': 'unverified'}
