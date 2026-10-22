"""
Como a janela do Chrome sobe.

Padrao: --ozone-platform=headless. O Chrome continua sendo o Chrome normal
(mesmo User-Agent, mesmo perfil, mesmos cookies, renderizacao de verdade) -
ele so desenha fora da tela. Nao e --headless: aquele muda o User-Agent para
"HeadlessChrome", que o hCaptcha usa para apertar o desafio.

Vantagem pratica: a janela nao fica na frente de quem esta usando a maquina e
ninguem a fecha por engano - foi assim que o lote morreu algumas vezes.

Em servidor Linux sem X isso ja e o suficiente: o modo headless do Ozone nao
abre conexao com display nenhum. So falta o WebGL por software (ver abaixo).

ROBO_JANELA=visivel volta a janela para a tela, quando ha tela.
"""

import os

VISIVEL = ("visivel", "visível", "sim", "1", "true")


def janela_visivel():
    return os.environ.get("ROBO_JANELA", "").strip().lower() in VISIVEL


def sem_display():
    """True em servidor sem X/Wayland."""
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def flags_janela():
    if janela_visivel() and not sem_display():
        return ["--start-maximized"]
    flags = ["--ozone-platform=headless", "--window-size=1920,1080"]
    if sem_display():
        # sem X nao ha GPU para o ANGLE, e o Chrome fica sem WebGL nenhum -
        # pagina sem WebGL e sinal de robo para o hCaptcha. O SwiftShader
        # desenha por software e devolve o WebGL.
        flags += ["--use-gl=angle", "--use-angle=swiftshader"]
    return flags
