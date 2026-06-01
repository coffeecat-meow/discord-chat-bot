import sympy
from sympy import parse_expr
from sympy.parsing.sympy_parser import standard_transformations, implicit_multiplication_application
import matplotlib.pyplot as plt
import numpy as np
import io
import re
import requests
import asyncio
import logging

# 設定 Matplotlib 後端為 'Agg' (非互動模式)
plt.switch_backend('Agg')

logger = logging.getLogger(__name__)

class MathToolkit:
    """簡化版安全數學工具箱 (計算、繪圖、LaTeX渲染)"""

    @staticmethod
    def _safe_parse(expr_str: str, allow_eq=False):
        """安全地解析算式字串"""
        expr_str = expr_str.replace('^', '**').replace('（', '(').replace('）', ')')
        expr_str = expr_str.replace('×', '*').replace('÷', '/')
        
        transformations = (standard_transformations + (implicit_multiplication_application,))
        local_dict = {
            'sin': sympy.sin, 'cos': sympy.cos, 'tan': sympy.tan, 
            'cot': sympy.cot, 'sec': sympy.sec, 'csc': sympy.csc,
            'pi': sympy.pi, 'e': sympy.E, 'E': sympy.E
        }
        
        if allow_eq:
            local_dict['x'] = sympy.Symbol('x')
            local_dict['Eq'] = sympy.Eq

        return parse_expr(expr_str, transformations=transformations, local_dict=local_dict)

    @staticmethod
    def _render_latex_sync(latex_str: str) -> io.BytesIO:
        """將 LaTeX 渲染成圖片的核心邏輯 (優先使用 CodeCogs POST)"""
        # 清理頭尾可能不小心多寫的 $, $$
        latex_str = latex_str.strip()
        latex_str = re.sub(r'^\$\$?(.*?)\$\$?$', r'\1', latex_str, flags=re.DOTALL).strip()
        
        # 1. 優先使用 CodeCogs POST API (能處理所有複雜 LaTeX，如換行、對齊、超長公式)
        try:
            url = r"https://latex.codecogs.com/png.image?\dpi{300}\bg_white"
            headers = {"Content-Type": "text/plain"}
            # 使用 encode('utf-8') 確保特殊字元與換行傳送正確
            response = requests.post(url, data=latex_str.encode('utf-8'), headers=headers, timeout=8)
            if response.status_code == 200:
                return io.BytesIO(response.content)
            else:
                logger.warning(f"CodeCogs API 失敗: HTTP {response.status_code}")
        except Exception as e:
            logger.warning(f"CodeCogs 網路異常: {e}")

        # 2. Fallback 到本地 Matplotlib 渲染 (萬一網路掛掉)
        processed_latex = latex_str.replace(r'\operatorname', r'\mathrm')
        processed_latex = processed_latex.replace(r'\bmod', r'\, \mathrm{mod} \,')
        processed_latex = re.sub(r'\\le(?![a-zA-Z])', r'\\leq', processed_latex)
        processed_latex = re.sub(r'\\ge(?![a-zA-Z])', r'\\geq', processed_latex)
        
        buf = io.BytesIO()
        fig = plt.figure(figsize=(0.1, 0.1))
        
        # 簡單為 Matplotlib 補上 $ 符號
        txt = f"${processed_latex}$"
        
        try:
            fig.text(0.05, 0.5, txt, fontsize=24, ha='left', va='center', multialignment='left')
            plt.savefig(buf, format='png', bbox_inches='tight', pad_inches=0.2, dpi=200)
        except Exception as e:
            plt.close(fig)
            raise RuntimeError(f"Matplotlib 渲染 LaTeX 失敗: {e}")
            
        plt.close(fig)
        buf.seek(0)
        return buf

    @classmethod
    async def render_latex(cls, latex_code: str) -> io.BytesIO:
        """非同步：將 LaTeX 字串轉成圖片"""
        return await asyncio.to_thread(cls._render_latex_sync, latex_code)

    @classmethod
    async def calculate(cls, formula: str) -> str:
        """非同步：計算數學算式"""
        def _calc():
            expr = cls._safe_parse(formula)
            result_val = expr.evalf()
            return f"{result_val:.10f}".rstrip('0').rstrip('.') if '.' in str(result_val) else str(result_val)
            
        try:
            return await asyncio.to_thread(_calc)
        except Exception as e:
            return f"計算錯誤: {str(e)}"

    @classmethod
    async def plot(cls, function_str: str, x_min: float = -10.0, x_max: float = 10.0) -> io.BytesIO:
        """非同步：安全地繪製函數圖形"""
        def _plot():
            x = sympy.Symbol('x')
            expr = cls._safe_parse(function_str, allow_eq=True)
            f = sympy.lambdify(x, expr, modules=['numpy'])
            
            x_vals = np.linspace(x_min, x_max, 400)
            y_vals = f(x_vals)
            
            if np.isscalar(y_vals):
                y_vals = np.broadcast_to(y_vals, x_vals.shape)

            buf = io.BytesIO()
            plt.figure(figsize=(8, 6))
            plt.plot(x_vals, y_vals, label=f'y = {function_str}', color='#3498db', linewidth=2)
            plt.grid(True, linestyle='--', alpha=0.7)
            plt.axhline(0, color='black', linewidth=1)
            plt.axvline(0, color='black', linewidth=1)
            # 使用 sympy.latex 來顯示漂亮的圖表標題
            plt.title(f"Plot of $f(x) = {sympy.latex(expr)}$", fontsize=14)
            plt.legend()
            
            plt.savefig(buf, format='png')
            plt.close()
            buf.seek(0)
            return buf

        return await asyncio.to_thread(_plot)