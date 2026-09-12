"""
backend/validator.py
Deterministic validator for KaTeX/LaTeX compliance and math delimiters.
Enforces Section 3 of the SAT Platform Implementation Plan.
"""

import re
from typing import Dict, List, Tuple

# Approved KaTeX command whitelist — covers all standard notation used in GCSE/A-Level/SAT/university maths
ALLOWED_LATEX_COMMANDS = {
    # ── Arithmetic / algebra ──────────────────────────────────────────────────
    r"\frac", r"\sqrt", r"\times", r"\div", r"\cdot", r"\pm", r"\mp",
    r"\leq", r"\geq", r"\neq", r"\approx", r"\equiv", r"\not",
    r"\ll", r"\gg", r"\le", r"\ge",
    # ── Fractions / roots / powers ───────────────────────────────────────────
    r"\dfrac", r"\tfrac", r"\cfrac", r"\over",
    r"\overline", r"\underline", r"\overrightarrow", r"\overleftarrow",
    r"\widehat", r"\widetilde", r"\hat", r"\bar", r"\vec",
    r"\underbrace", r"\overbrace",
    # ── Sets / logic / implications ───────────────────────────────────────────
    r"\in", r"\notin", r"\ni", r"\subset", r"\subseteq", r"\supset", r"\supseteq",
    r"\nsubset", r"\nsubseteq", r"\cup", r"\cap", r"\emptyset", r"\varnothing",
    r"\forall", r"\exists", r"\nexists",
    r"\implies", r"\impliedby", r"\iff",
    r"\neg", r"\sim", r"\land", r"\lor", r"\lnot",
    r"\Rightarrow", r"\Leftarrow", r"\Leftrightarrow",
    r"\rightarrow", r"\leftarrow", r"\leftrightarrow",
    r"\to", r"\gets",
    r"\mapsto", r"\longmapsto",
    r"\setminus", r"\mid", r"\nmid", r"\pmod", r"\mod",
    r"\prime", r"\backslash",
    # ── Trig functions ────────────────────────────────────────────────────────
    r"\sin", r"\cos", r"\tan", r"\sec", r"\csc", r"\cot",
    r"\arcsin", r"\arccos", r"\arctan",
    r"\sinh", r"\cosh", r"\tanh", r"\coth",
    r"\log", r"\ln", r"\exp", r"\lim", r"\limsup", r"\liminf",
    r"\min", r"\max", r"\sup", r"\inf", r"\gcd", r"\lcm",
    r"\ker", r"\dim", r"\deg", r"\hom", r"\arg",
    # ── Calculus / analysis ───────────────────────────────────────────────────
    r"\int", r"\iint", r"\iiint", r"\oint",
    r"\sum", r"\prod", r"\coprod",
    r"\partial", r"\nabla", r"\infty", r"\varnothing",
    r"\lim_{}", r"\to",
    # ── Number theory / combinatorics ─────────────────────────────────────────
    r"\binom", r"\dbinom", r"\tbinom",
    r"\pmod", r"\mod", r"\div",
    # ── Vectors / matrices ────────────────────────────────────────────────────
    r"\vec", r"\overrightarrow",
    r"\begin{pmatrix}", r"\end{pmatrix}",
    r"\begin{bmatrix}", r"\end{bmatrix}",
    r"\begin{vmatrix}", r"\end{vmatrix}",
    r"\begin{Vmatrix}", r"\end{Vmatrix}",
    r"\begin{matrix}", r"\end{matrix}",
    r"\begin{array}", r"\end{array}",
    r"\begin{cases}", r"\end{cases}",
    r"\begin{aligned}", r"\end{aligned}",
    r"\begin{align}", r"\end{align}",
    r"\begin{gather}", r"\end{gather}",
    r"\det", r"\text{tr}", r"\operatorname",
    r"\mathbf", r"\boldsymbol",
    # ── Geometry ──────────────────────────────────────────────────────────────
    r"\circ", r"\angle", r"\measuredangle", r"\triangle", r"\square",
    r"\perp", r"\parallel", r"\nparallel",
    r"\overline", r"\hat", r"\widehat",
    # ── Delimiters ────────────────────────────────────────────────────────────
    r"\left", r"\right",
    r"\lfloor", r"\rfloor", r"\lceil", r"\rceil",
    r"\langle", r"\rangle",
    r"\|", r"\Vert", r"\vert",
    # ── Greek letters — lowercase ─────────────────────────────────────────────
    r"\alpha", r"\beta", r"\gamma", r"\delta", r"\epsilon", r"\varepsilon",
    r"\zeta", r"\eta", r"\theta", r"\vartheta", r"\iota", r"\kappa",
    r"\lambda", r"\mu", r"\nu", r"\xi", r"\pi", r"\varpi",
    r"\rho", r"\varrho", r"\sigma", r"\varsigma", r"\tau",
    r"\upsilon", r"\phi", r"\varphi", r"\chi", r"\psi", r"\omega",
    # ── Greek letters — uppercase ─────────────────────────────────────────────
    r"\Gamma", r"\Delta", r"\Theta", r"\Lambda", r"\Xi", r"\Pi",
    r"\Sigma", r"\Upsilon", r"\Phi", r"\Psi", r"\Omega",
    # ── Ellipses / misc symbols ───────────────────────────────────────────────
    r"\dots", r"\ldots", r"\cdots", r"\vdots", r"\ddots",
    r"\propto", r"\therefore", r"\because",
    r"\infty", r"\hbar", r"\ell",
    r"\nabla", r"\partial",
    r"\otimes", r"\oplus", r"\ominus", r"\odot",
    r"\wedge", r"\vee", r"\oplus",
    r"\cap", r"\cup",
    # ── Spacing ───────────────────────────────────────────────────────────────
    r"\quad", r"\qquad", r"\,", r"\:", r"\;", r"\!", r"\ ",
    # ── Fonts / formatting ────────────────────────────────────────────────────
    r"\text", r"\textbf", r"\textit", r"\textrm",
    r"\mathbf", r"\mathrm", r"\mathit", r"\mathsf", r"\mathtt",
    r"\mathbb", r"\mathcal", r"\mathfrak", r"\mathscr",
    r"\boldsymbol", r"\bm",
    # ── Decorators ────────────────────────────────────────────────────────────
    r"\tilde", r"\acute", r"\grave", r"\check", r"\breve", r"\dot", r"\ddot",
    r"\mathop", r"\limits", r"\nolimits",
    # ── Arrows ────────────────────────────────────────────────────────────────
    r"\uparrow", r"\downarrow", r"\updownarrow",
    r"\Uparrow", r"\Downarrow", r"\Updownarrow",
    r"\nearrow", r"\searrow", r"\nwarrow", r"\swarrow",
    r"\hookrightarrow", r"\hookleftarrow",
    r"\rightharpoonup", r"\leftharpoonup",
    # ── Number set symbols (via \mathbb) ──────────────────────────────────────
    # These are handled as \mathbb{R} etc — covered by \mathbb above
}

# Regex to extract math spans delimited by \( ... \) or \[ ... \]
INLINE_MATH_PATTERN = re.compile(r"\\\((.*?)\\\)", re.DOTALL)
DISPLAY_MATH_PATTERN = re.compile(r"\\\[(.*?)\\\]", re.DOTALL)

# Regex to detect unescaped dollar signs used as math delimiters rather than currency ($25, $100.50)
# Matches $ followed by non-digits or closed with matching $
SUSPICIOUS_DOLLAR_PATTERN = re.compile(r"(?<!\\)\$(?!\d+(?:\.\d{2})?(?:\s|$|\.|\,|[a-zA-Z]))")


def extract_math_spans(text: str) -> List[Tuple[str, str]]:
    """Extract all math expressions along with their type ('inline' or 'display')."""
    spans = []
    for match in INLINE_MATH_PATTERN.finditer(text):
        spans.append(("inline", match.group(1)))
    for match in DISPLAY_MATH_PATTERN.finditer(text):
        spans.append(("display", match.group(1)))
    return spans


def validate_math_text(text: str) -> Tuple[bool, List[str]]:
    """
    Validate a single text string containing KaTeX math.
    Returns (is_valid, list_of_errors).
    """
    errors = []
    if not text or not isinstance(text, str):
        return True, []

    if r"\begin{array}" in text or r"\hline" in text:
        errors.append("Table content found inside math — should be in the 'table' field, not KaTeX.")
    # 1. Check for raw $ or $$ delimiters
    if "$$" in text:
        errors.append("Forbidden $$ delimiter detected. Use \\[...\\] for display math.")
    
    # Check if single $ seems to be used as math delimiter instead of currency
    # Currency pattern: $ followed by digits (e.g. $25, $10.50, $1,000)
    # Math delimiter pattern: $ followed by non-digits and closed by $ with math tokens
    math_dollar_matches = re.findall(r"(?<!\\)\$(?!\d)([^$\n]+?)(?<!\\)\$", text)
    for span in math_dollar_matches:
        # If the span contains algebra variables, math operators, or backslashes
        if re.search(r"[a-zA-Z\\=+\-*/^_{}]", span):
            errors.append(f"Illegal '$...$' math delimiter found around '${span}$'. Use \\(...\\) for inline math.")

    # 2. Check all commands in math spans
    math_spans = extract_math_spans(text)
    seen = set()
    for math_type, math_content in math_spans:
        commands = re.findall(r"\\[a-zA-Z]+(?:\{[a-zA-Z]+\})?", math_content)
        for cmd in commands:
            key = (cmd, math_type)
            if key in seen:
                continue
            seen.add(key)
            base_cmd = cmd.split("{")[0] if "{" in cmd else cmd
            if cmd not in ALLOWED_LATEX_COMMANDS and base_cmd not in ALLOWED_LATEX_COMMANDS:
                if not cmd.startswith(r"\text") and not cmd.startswith(r"\frac"):
                    errors.append(f"Disallowed LaTeX command '{cmd}' inside {math_type} math.")

        if math_content.count("{") != math_content.count("}"):
            errors.append(f"Unbalanced braces in math span: '{math_content}'")

    return (len(errors) == 0, errors)


def validate_question_dict(q_dict: dict) -> Tuple[bool, List[str]]:
    """
    Validate a complete structured question dictionary.
    Fields checked: stem, choices, explanation, visible_correct_answer.
    """
    all_errors = []

    # Check stem
    stem = q_dict.get("stem") or q_dict.get("raw_stem") or q_dict.get("reworded_stem") or ""
    valid, errs = validate_math_text(stem)
    if not valid:
        all_errors.extend([f"Stem: {e}" for e in errs])

    # Check choices
    choices = q_dict.get("choices") or q_dict.get("raw_choices") or q_dict.get("reworded_choices") or []
    if isinstance(choices, list):
        if len(choices) != 4 and len(choices) != 0:
            all_errors.append(f"Question must have exactly 4 choices (found {len(choices)}).")
        for i, c in enumerate(choices):
            valid, errs = validate_math_text(str(c))
            if not valid:
                all_errors.extend([f"Choice {i+1}: {e}" for e in errs])

    # Check explanation if present
    explanation = q_dict.get("explanation") or q_dict.get("explanation_latex") or ""
    if explanation:
        valid, errs = validate_math_text(explanation)
        if not valid:
            all_errors.extend([f"Explanation: {e}" for e in errs])

    return (len(all_errors) == 0, all_errors)


def get_whitelist_prompt_string() -> str:
    """Returns a formatted list of allowed LaTeX commands for prompt injection."""
    return (
        r"\frac, \sqrt, \sqrt[n]{}, ^{}, _{}, \times, \div, \cdot, \pm, \mp, \leq, \geq, \neq, \approx, \equiv, "
        r"\in, \notin, \subset, \cup, \cap, \emptyset, \forall, \exists, "
        r"\sin, \cos, \tan, \sec, \csc, \cot, \log, \ln, \exp, \lim, "
        r"\int, \iint, \sum, \prod, \frac{d}{dx}, \partial, \nabla, \infty, "
        r"\vec{}, \overrightarrow{}, \begin{pmatrix}...\end{pmatrix}, \begin{vmatrix}...\end{vmatrix}, \det, "
        r"\begin{cases}...\end{cases}, "
        r"\circ, \angle, \triangle, \perp, \parallel, \overline{AB}, \hat{}, "
        r"\alpha, \beta, \gamma, \theta, \pi, \mu, \sigma, \phi, \Delta, \Sigma, \Omega, "
        r"\binom{}{}, \text{}"
    )
