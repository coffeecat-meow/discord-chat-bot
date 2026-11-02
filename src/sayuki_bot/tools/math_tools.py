from __future__ import annotations

import asyncio
import io
import math
import multiprocessing
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Callable

import sympy


MAX_EXPRESSION_CHARS = 512
MAX_EXPRESSION_TOKENS = 256
MAX_PARSE_DEPTH = 32
MAX_NUMERIC_LITERAL_CHARS = 80
MAX_ABSOLUTE_EXPONENT = 1000
MAX_RESULT_CHARS = 2000
MAX_LATEX_CHARS = 4000
MAX_IMAGE_BYTES = 5 * 1024 * 1024
CALCULATION_TIMEOUT_SECONDS = 3.0
PLOT_TIMEOUT_SECONDS = 8.0
LATEX_TIMEOUT_SECONDS = 8.0
MAX_PLOT_BOUND = 1_000_000.0
MAX_PLOT_SPAN = 2_000_000.0
MAX_PLOT_ABSOLUTE_Y = 1e12


class MathToolError(ValueError):
    """A safe, user-displayable math tool error."""


class MathToolTimeout(MathToolError):
    """Raised when an isolated math worker exceeds its deadline."""


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    position: int


_NUMBER_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NORMALIZATION_TABLE = str.maketrans(
    {
        "（": "(",
        "）": ")",
        "，": ",",
        "×": "*",
        "÷": "/",
        "−": "-",
        "π": "pi",
    }
)


def _factorial(value: sympy.Expr) -> sympy.Expr:
    if value.free_symbols or value.is_integer is not True or value.is_nonnegative is not True:
        raise MathToolError("factorial只接受非負整數")
    integer = int(value)
    if integer > MAX_ABSOLUTE_EXPONENT:
        raise MathToolError(f"factorial輸入不可大於{MAX_ABSOLUTE_EXPONENT}")
    return sympy.factorial(integer)


def _log(*arguments: sympy.Expr) -> sympy.Expr:
    if len(arguments) == 1:
        return sympy.log(arguments[0])
    if len(arguments) == 2:
        return sympy.log(arguments[0], arguments[1])
    raise MathToolError("log只接受一或兩個參數")


def _min_function(*arguments: sympy.Expr) -> sympy.Expr:
    if not 1 <= len(arguments) <= 8:
        raise MathToolError("min接受1到8個參數")
    return sympy.Min(*arguments)


def _max_function(*arguments: sympy.Expr) -> sympy.Expr:
    if not 1 <= len(arguments) <= 8:
        raise MathToolError("max接受1到8個參數")
    return sympy.Max(*arguments)


_FUNCTIONS: dict[str, tuple[Callable[..., sympy.Expr], int, int]] = {
    "sin": (sympy.sin, 1, 1),
    "cos": (sympy.cos, 1, 1),
    "tan": (sympy.tan, 1, 1),
    "cot": (sympy.cot, 1, 1),
    "sec": (sympy.sec, 1, 1),
    "csc": (sympy.csc, 1, 1),
    "asin": (sympy.asin, 1, 1),
    "acos": (sympy.acos, 1, 1),
    "atan": (sympy.atan, 1, 1),
    "sinh": (sympy.sinh, 1, 1),
    "cosh": (sympy.cosh, 1, 1),
    "tanh": (sympy.tanh, 1, 1),
    "sqrt": (sympy.sqrt, 1, 1),
    "exp": (sympy.exp, 1, 1),
    "log": (_log, 1, 2),
    "ln": (sympy.log, 1, 1),
    "abs": (sympy.Abs, 1, 1),
    "floor": (sympy.floor, 1, 1),
    "ceil": (sympy.ceiling, 1, 1),
    "factorial": (_factorial, 1, 1),
    "min": (_min_function, 1, 8),
    "max": (_max_function, 1, 8),
}
_CONSTANTS = {
    "pi": sympy.pi,
    "e": sympy.E,
    "tau": 2 * sympy.pi,
}


def _tokenize(expression: str) -> list[_Token]:
    normalized = expression.translate(_NORMALIZATION_TABLE).strip()
    if not normalized:
        raise MathToolError("算式不可為空")
    if len(normalized) > MAX_EXPRESSION_CHARS:
        raise MathToolError(f"算式不可超過{MAX_EXPRESSION_CHARS}個字元")

    tokens: list[_Token] = []
    index = 0
    while index < len(normalized):
        char = normalized[index]
        if char.isspace():
            index += 1
            continue

        number_match = _NUMBER_RE.match(normalized, index)
        if number_match:
            value = number_match.group(0)
            if len(value) > MAX_NUMERIC_LITERAL_CHARS:
                raise MathToolError("數字常值過長")
            exponent_match = re.search(r"[eE]([+-]?\d+)$", value)
            if exponent_match and abs(int(exponent_match.group(1))) > MAX_ABSOLUTE_EXPONENT:
                raise MathToolError("科學記號指數過大")
            tokens.append(_Token("NUMBER", value, index))
            index = number_match.end()
            continue

        identifier_match = _IDENTIFIER_RE.match(normalized, index)
        if identifier_match:
            value = identifier_match.group(0).lower()
            tokens.append(_Token("IDENT", value, index))
            index = identifier_match.end()
            continue

        two_chars = normalized[index : index + 2]
        if two_chars in {"**", "//"}:
            tokens.append(_Token("OP", two_chars, index))
            index += 2
            continue
        if char in "+-*/%^!(),":
            kind = {
                "(": "LPAREN",
                ")": "RPAREN",
                ",": "COMMA",
            }.get(char, "OP")
            tokens.append(_Token(kind, char, index))
            index += 1
            continue
        raise MathToolError(f"算式包含不支援的字元（位置{index + 1}）")

    if len(tokens) > MAX_EXPRESSION_TOKENS:
        raise MathToolError(f"算式不可超過{MAX_EXPRESSION_TOKENS}個token")
    tokens.append(_Token("EOF", "", len(normalized)))
    return tokens


class _ExpressionParser:
    def __init__(self, expression: str, *, allow_variable: bool):
        self.tokens = _tokenize(expression)
        self.index = 0
        self.allow_variable = allow_variable
        self.depth = 0

    @property
    def current(self) -> _Token:
        return self.tokens[self.index]

    def _advance(self) -> _Token:
        token = self.current
        self.index += 1
        return token

    def _accept(self, kind: str, value: str | None = None) -> _Token | None:
        token = self.current
        if token.kind != kind or (value is not None and token.value != value):
            return None
        return self._advance()

    def _expect(self, kind: str, value: str | None = None) -> _Token:
        token = self._accept(kind, value)
        if token is None:
            expected = value if value is not None else kind
            raise MathToolError(
                f"預期{expected}，但在位置{self.current.position + 1}遇到"
                f"{self.current.value or '算式結尾'}"
            )
        return token

    def _enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_PARSE_DEPTH:
            raise MathToolError(f"算式巢狀不可超過{MAX_PARSE_DEPTH}層")

    def _leave(self) -> None:
        self.depth -= 1

    def parse(self) -> sympy.Expr:
        value = self._parse_additive()
        if self.current.kind != "EOF":
            raise MathToolError(
                f"位置{self.current.position + 1}有無法解析的內容：{self.current.value}"
            )
        return value

    def _parse_additive(self) -> sympy.Expr:
        value = self._parse_multiplicative()
        while self.current.kind == "OP" and self.current.value in {"+", "-"}:
            operator = self._advance().value
            right = self._parse_multiplicative()
            value = value + right if operator == "+" else value - right
        return value

    def _parse_multiplicative(self) -> sympy.Expr:
        value = self._parse_unary()
        while True:
            if self.current.kind == "OP" and self.current.value in {"*", "/", "//", "%"}:
                operator = self._advance().value
                right = self._parse_unary()
                if operator == "*":
                    value *= right
                elif operator == "/":
                    value /= right
                elif operator == "//":
                    value = sympy.floor(value / right)
                else:
                    value = sympy.Mod(value, right)
                continue
            if self.current.kind in {"NUMBER", "IDENT", "LPAREN"}:
                value *= self._parse_unary()
                continue
            return value

    def _parse_unary(self) -> sympy.Expr:
        if self._accept("OP", "+"):
            return self._parse_unary()
        if self._accept("OP", "-"):
            return -self._parse_unary()
        return self._parse_power()

    def _parse_power(self) -> sympy.Expr:
        value = self._parse_postfix()
        if self.current.kind == "OP" and self.current.value in {"^", "**"}:
            self._advance()
            exponent = self._parse_unary()
            if exponent.free_symbols:
                if not self.allow_variable:
                    raise MathToolError("純計算不接受含變數的指數")
            elif exponent.is_real is not True:
                raise MathToolError("指數必須是實數")
            else:
                try:
                    exponent_value = float(exponent)
                except (TypeError, ValueError, OverflowError):
                    raise MathToolError("指數無法安全判定") from None
                if abs(exponent_value) > MAX_ABSOLUTE_EXPONENT:
                    raise MathToolError(f"指數絕對值不可大於{MAX_ABSOLUTE_EXPONENT}")
            value = sympy.Pow(value, exponent)
        return value

    def _parse_postfix(self) -> sympy.Expr:
        value = self._parse_primary()
        while self._accept("OP", "!"):
            value = _factorial(value)
        return value

    def _parse_primary(self) -> sympy.Expr:
        number = self._accept("NUMBER")
        if number:
            try:
                if "." in number.value or "e" in number.value.lower():
                    return sympy.Float(number.value)
                return sympy.Integer(number.value)
            except (TypeError, ValueError):
                raise MathToolError("數字格式無效") from None

        identifier = self._accept("IDENT")
        if identifier:
            name = identifier.value
            if self._accept("LPAREN"):
                function = _FUNCTIONS.get(name)
                if function is None:
                    raise MathToolError(f"不支援的函式：{name}")
                arguments: list[sympy.Expr] = []
                self._enter()
                try:
                    if not self._accept("RPAREN"):
                        while True:
                            arguments.append(self._parse_additive())
                            if self._accept("RPAREN"):
                                break
                            self._expect("COMMA")
                finally:
                    self._leave()
                callable_function, minimum, maximum = function
                if not minimum <= len(arguments) <= maximum:
                    if minimum == maximum:
                        detail = f"{minimum}個"
                    else:
                        detail = f"{minimum}到{maximum}個"
                    raise MathToolError(f"{name}需要{detail}參數")
                try:
                    return callable_function(*arguments)
                except MathToolError:
                    raise
                except Exception as exc:
                    raise MathToolError(f"{name}參數無效：{exc}") from None
            if name in _CONSTANTS:
                return _CONSTANTS[name]
            if name == "x" and self.allow_variable:
                return sympy.Symbol("x", real=True)
            if name in _FUNCTIONS:
                raise MathToolError(f"函式{name}必須使用括號，例如{name}(x)")
            raise MathToolError(f"不支援的名稱：{name}")

        if self._accept("LPAREN"):
            self._enter()
            try:
                value = self._parse_additive()
                self._expect("RPAREN")
                return value
            finally:
                self._leave()

        raise MathToolError(f"位置{self.current.position + 1}缺少數字、常數、變數或函式")


def _parse_math_expression(expression: str, *, allow_variable: bool = False) -> sympy.Expr:
    return _ExpressionParser(expression, allow_variable=allow_variable).parse()


def _format_calculation_result(expression: str) -> str:
    parsed = _parse_math_expression(expression, allow_variable=False)
    if parsed.free_symbols:
        raise MathToolError("純計算不可包含變數")
    result = sympy.N(parsed, 12)
    if result.has(sympy.nan, sympy.zoo, sympy.oo, -sympy.oo):
        raise MathToolError("結果不是有限數值")
    if result.is_number is not True:
        raise MathToolError("結果不是數值")
    text = str(result)
    if re.fullmatch(r"-?\d+\.\d+", text):
        text = text.rstrip("0").rstrip(".")
    if len(text) > MAX_RESULT_CHARS:
        raise MathToolError("結果過長")
    return text


def _build_plot(expression: str, x_min: float, x_max: float) -> bytes:
    if not math.isfinite(x_min) or not math.isfinite(x_max):
        raise MathToolError("繪圖範圍必須是有限數值")
    if x_min >= x_max:
        raise MathToolError("x_min必須小於x_max")
    if abs(x_min) > MAX_PLOT_BOUND or abs(x_max) > MAX_PLOT_BOUND:
        raise MathToolError(f"繪圖邊界絕對值不可超過{MAX_PLOT_BOUND:g}")
    if x_max - x_min > MAX_PLOT_SPAN:
        raise MathToolError(f"繪圖範圍不可超過{MAX_PLOT_SPAN:g}")

    import matplotlib

    matplotlib.use("Agg")
    import numpy as np
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    x = sympy.Symbol("x", real=True)
    parsed = _parse_math_expression(expression, allow_variable=True)
    if parsed.free_symbols - {x}:
        raise MathToolError("函數只能包含變數x")
    function = sympy.lambdify(x, parsed, modules=["numpy"])
    x_values = np.linspace(x_min, x_max, 400)
    with np.errstate(all="ignore"):
        y_values = function(x_values)
    if np.isscalar(y_values):
        y_values = np.broadcast_to(y_values, x_values.shape)
    try:
        y_values = np.asarray(y_values)
        if np.iscomplexobj(y_values):
            if np.any(np.abs(y_values.imag) > 1e-10):
                raise MathToolError("繪圖結果包含複數")
            y_values = y_values.real
        y_values = y_values.astype(float)
    except (TypeError, ValueError):
        raise MathToolError("函數結果無法轉成實數序列") from None
    if y_values.shape != x_values.shape:
        try:
            y_values = np.broadcast_to(y_values, x_values.shape).astype(float)
        except ValueError:
            raise MathToolError("函數結果維度不正確") from None
    y_values = np.where(
        np.isfinite(y_values) & (np.abs(y_values) <= MAX_PLOT_ABSOLUTE_Y),
        y_values,
        np.nan,
    )
    if np.all(np.isnan(y_values)):
        raise MathToolError("指定範圍內沒有可繪製的有限實數")

    figure = Figure(figsize=(8, 6))
    canvas = FigureCanvasAgg(figure)
    axis = figure.subplots()
    axis.plot(
        x_values,
        y_values,
        label=f"y = {expression}",
        color="#3498db",
        linewidth=2,
    )
    axis.grid(True, linestyle="--", alpha=0.7)
    axis.axhline(0, color="black", linewidth=1)
    axis.axvline(0, color="black", linewidth=1)
    axis.set_title(f"Plot of $f(x) = {sympy.latex(parsed)}$", fontsize=14)
    axis.legend()
    output = io.BytesIO()
    canvas.print_png(output)
    payload = output.getvalue()
    if len(payload) > MAX_IMAGE_BYTES:
        raise MathToolError("繪圖輸出過大")
    return payload


def _apply_worker_limits() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (10, 11))
    except (ImportError, OSError, ValueError):
        pass


def _sanitize_worker_environment() -> None:
    preserved = {
        key: value for key, value in os.environ.items() if key in {"LANG", "LC_ALL", "PATH", "TZ"}
    }
    os.environ.clear()
    os.environ.update(preserved)
    cache_root = os.path.join(tempfile.gettempdir(), "sayuki-math-cache")
    matplotlib_cache = os.path.join(cache_root, "matplotlib")
    os.makedirs(matplotlib_cache, exist_ok=True)
    os.environ["MPLBACKEND"] = "Agg"
    os.environ["MPLCONFIGDIR"] = matplotlib_cache
    os.environ["XDG_CACHE_HOME"] = cache_root


def _isolated_worker(operation: str, payload: dict[str, Any], connection) -> None:
    _sanitize_worker_environment()
    _apply_worker_limits()
    try:
        if operation == "calculate":
            result: str | bytes = _format_calculation_result(str(payload["expression"]))
        elif operation == "plot":
            result = _build_plot(
                str(payload["expression"]),
                float(payload["x_min"]),
                float(payload["x_max"]),
            )
        elif operation == "latex":
            result = _build_latex(str(payload["latex"]))
        else:
            raise MathToolError("不支援的隔離運算")
        connection.send(("ok", result))
    except BaseException as exc:
        try:
            connection.send(("error", str(exc)[:800]))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


async def _stop_process(process: multiprocessing.Process) -> None:
    if process.is_alive():
        process.terminate()
    await asyncio.to_thread(process.join, 1.0)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        await asyncio.to_thread(process.join, 1.0)


async def _run_isolated(
    operation: str,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> str | bytes:
    context = multiprocessing.get_context("spawn")
    receiving_connection, sending_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_isolated_worker,
        args=(operation, payload, sending_connection),
        daemon=True,
    )
    process.start()
    sending_connection.close()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise MathToolTimeout(f"運算超過{timeout:g}秒，已終止")
            ready = await asyncio.to_thread(
                receiving_connection.poll,
                min(0.05, remaining),
            )
            if not ready:
                if process.exitcode is not None:
                    raise MathToolError("隔離運算程序異常結束")
                continue
            try:
                status, result = receiving_connection.recv()
            except EOFError:
                raise MathToolError("隔離運算程序未回傳結果") from None
            if status != "ok":
                raise MathToolError(str(result) or "運算失敗")
            return result
    except asyncio.CancelledError:
        await asyncio.shield(_stop_process(process))
        raise
    finally:
        receiving_connection.close()
        if process.is_alive():
            await asyncio.shield(_stop_process(process))
        else:
            await asyncio.to_thread(process.join, 0)


def _build_latex(latex: str) -> bytes:
    latex = latex.strip()
    if not latex:
        raise MathToolError("LaTeX不可為空")
    if len(latex) > MAX_LATEX_CHARS:
        raise MathToolError(f"LaTeX不可超過{MAX_LATEX_CHARS}個字元")
    latex = re.sub(r"^\$\$?(.*?)\$\$?$", r"\1", latex, flags=re.DOTALL).strip()

    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    processed = latex.replace(r"\operatorname", r"\mathrm")
    processed = processed.replace(r"\bmod", r"\, \mathrm{mod} \,")
    processed = re.sub(r"\\le(?![a-zA-Z])", r"\\leq", processed)
    processed = re.sub(r"\\ge(?![a-zA-Z])", r"\\geq", processed)
    figure = Figure(figsize=(0.1, 0.1))
    FigureCanvasAgg(figure)
    figure.text(
        0.05,
        0.5,
        f"${processed}$",
        fontsize=24,
        ha="left",
        va="center",
        multialignment="left",
    )
    output = io.BytesIO()
    try:
        figure.savefig(
            output,
            format="png",
            bbox_inches="tight",
            pad_inches=0.2,
            dpi=200,
        )
    except Exception as exc:
        raise MathToolError(f"本地LaTeX渲染失敗：{exc}") from None
    if output.tell() > MAX_IMAGE_BYTES:
        raise MathToolError("LaTeX圖片輸出過大")
    return output.getvalue()


class MathToolkit:
    """Allowlisted math DSL with isolated calculation and plotting workers."""

    @classmethod
    async def calculate(cls, formula: str) -> str:
        try:
            result = await _run_isolated(
                "calculate",
                {"expression": formula},
                timeout=CALCULATION_TIMEOUT_SECONDS,
            )
            if not isinstance(result, str):
                raise MathToolError("運算結果型別錯誤")
            return result
        except MathToolError as exc:
            return f"計算錯誤: {exc}"

    @classmethod
    async def plot(
        cls,
        function_str: str,
        x_min: float = -10.0,
        x_max: float = 10.0,
    ) -> io.BytesIO:
        result = await _run_isolated(
            "plot",
            {
                "expression": function_str,
                "x_min": x_min,
                "x_max": x_max,
            },
            timeout=PLOT_TIMEOUT_SECONDS,
        )
        if not isinstance(result, bytes):
            raise MathToolError("繪圖結果型別錯誤")
        return io.BytesIO(result)

    @classmethod
    async def render_latex(cls, latex_code: str) -> io.BytesIO:
        result = await _run_isolated(
            "latex",
            {"latex": latex_code},
            timeout=LATEX_TIMEOUT_SECONDS,
        )
        if not isinstance(result, bytes):
            raise MathToolError("LaTeX渲染結果型別錯誤")
        return io.BytesIO(result)
