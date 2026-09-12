"""
backend/validator.py
Deterministic validator for KaTeX/LaTeX compliance and math delimiters.
Enforces Section 3 of the SAT Platform Implementation Plan.
"""

import re
from typing import Dict, List, Tuple

# Approved KaTeX command whitelist
ALLOWED_LATEX_COMMANDS = {
    # Arithmetic / algebra
    r"\frac", r"\sqrt", r"\times", r"\div", r"\cdot", r"\pm", r"\mp",
    r"\leq", r"\geq", r"\neq", r"\approx", r"\equiv",
    # Sets / logic
    r"\in", r"\notin", r"\subset", r"\cup", r"\cap", r"\emptyset", r"\forall", r"\exists",
    # Functions
    r"\sin", r"\cos", r"\tan", r"\sec", r"\csc", r"\cot", r"\log", r"\ln", r"\exp", r"\lim",
    # Calculus
    r"\int", r"\iint", r"\sum", r"\prod", r"\partial", r"\nabla", r"\infty",
    # Vectors / matrices / systems
    r"\vec", r"\overrightarrow", r"\begin{pmatrix}", r"\end{pmatrix}",
    r"\begin{vmatrix}", r"\end{vmatrix}", r"\det",
    r"\begin{cases}", r"\end{cases}",
    # Geometry
    r"\circ", r"\angle", r"\triangle", r"\perp", r"\parallel", r"\overline", r"\hat",
    # Greek letters
    r"\alpha", r"\beta", r"\gamma", r"\theta", r"\pi", r"\mu", r"\sigma", r"\phi",
    r"\Delta", r"\Sigma", r"\Omega",
    # Misc
    r"\binom", r"\text",
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
    for math_type, math_content in math_spans:
        # Find all TeX commands (\commandname)
        commands = re.findall(r"\\[a-zA-Z]+(?:\{[a-zA-Z]+\})?", math_content)
        for cmd in commands:
            # Handle special cases like \begin{pmatrix} vs \begin
            base_cmd = cmd.split("{")[0] if "{" in cmd else cmd
            if cmd not in ALLOWED_LATEX_COMMANDS and base_cmd not in ALLOWED_LATEX_COMMANDS:
                # Check for standard formatting macros allowed inside text mode (e.g. \text)
                if not cmd.startswith(r"\text") and not cmd.startswith(r"\frac"):
                    errors.append(f"Disallowed LaTeX command '{cmd}' inside {math_type} math: '\\({math_content}\\)'.")

        # Check bracket balancing inside math
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
